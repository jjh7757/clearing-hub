from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank, require_admin
from clearing_hub.core.database import get_db
from clearing_hub.core.exception_handlers import error_body, spec_http_status
from clearing_hub.core.exceptions import ApiException, NotFound
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.transfers import evidence, service
from clearing_hub.domains.transfers.models import Transfer, TransferEvent, TransferStatus
from clearing_hub.domains.transfers.schemas import TransferDetail, TransferRequest, TransferResolve, TransferResult

router = APIRouter(prefix="/v1/transfers", tags=["transfers"])
admin_router = APIRouter(prefix="/admin/transfers", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]
Sender = Annotated[Bank, Depends(current_bank)]


def _result_response(transfer: Transfer) -> JSONResponse:
    """SPEC 3.3의 세 가지 응답만 쓴다: 200 COMPLETED / 202 PENDING / 4xx FAILED. 멱등 재전송에도 같은 형식.

    재전송 때 거래가 아직 ACCEPTED·DELIVERING(허브가 받는 은행을 부르는 중)이거나 UNKNOWN(재시도를 다 해도 모름)이면
    3.3에는 없는 상태라 202 `PENDING`으로 답한다. 보내는 은행이 할 일이 같기 때문이다: 되돌리지 말고 3.4로 확인한다.
    3.4 결과 조회는 실제 상태를 그대로 보여 준다."""
    result = TransferResult.model_validate(transfer, from_attributes=True).model_dump(mode="json")
    match transfer.status:
        case TransferStatus.COMPLETED:
            return JSONResponse(status_code=200, content=result)
        case TransferStatus.FAILED:
            code = transfer.fail_code or "INTERNAL_ERROR"
            return JSONResponse(status_code=spec_http_status(code), content={
                **error_body(code, transfer.fail_message or "받는 은행이 거절했습니다", transfer.transfer_id), "status": "FAILED",
            })
        case _:
            return JSONResponse(status_code=202, content={**result, "status": TransferStatus.PENDING.value})


@router.post("", responses={200: {"model": TransferResult}, 202: {"model": TransferResult}})
async def create_transfer(data: TransferRequest, db: DB, sender: Sender, request: Request):
    """이체 요청 (SPEC 3.3). 보내는 은행은 이 요청 전에 출금을 끝내 둔다."""
    try:
        # 서명 검증을 통과한 요청 원문을 증거로 넘긴다 (core/auth.current_bank가 request.state에 둔다)
        transfer, _ = await service.accept_and_deliver(db, sender, data,
                                                        request_evidence=getattr(request.state, "verified", None))
    except ApiException as exc:
        # 접수 전 거절(SAME_BANK·LIMIT_EXCEEDED(1회 상한·순채무한도)·BANK_NOT_FOUND·BANK_INACTIVE·ACCOUNT_REF_INVALID):
        # 거래가 허브에 없으므로
        # 확정 실패다. `status: FAILED`를 붙여 보내는 은행이 되돌려도 된다고 알린다 (SPEC 3.3·6).
        # 403 FORBIDDEN(남의 거래 ID)은 그 거래가 있다는 뜻이라 FAILED가 아니다
        if exc.status_code not in (400, 404):
            raise
        return JSONResponse(status_code=exc.status_code,
                            content={**error_body(exc.code, exc.message, data.transfer_id), "status": "FAILED"})
    return _result_response(transfer)


@router.get("/{transfer_id}", response_model=TransferDetail)
async def get_transfer(transfer_id: str, db: DB, sender: Sender):
    """처리 결과 조회 (SPEC 3.4). 자기 은행이 보낸 거래만."""
    return TransferDetail.model_validate(await service.get_transfer(db, sender, transfer_id), from_attributes=True)


@admin_router.get("")
async def admin_list_transfers(db: DB, status: str | None = None, limit: int = 100):
    stmt = select(Transfer).order_by(Transfer.requested_at.desc()).limit(min(limit, 500))
    if status:
        stmt = stmt.where(Transfer.status == status)
    return [_admin_view(t) for t in (await db.scalars(stmt)).all()]


def _admin_view(t: Transfer) -> dict:
    return {**TransferDetail.model_validate(t, from_attributes=True).model_dump(mode="json"), "from_bank": t.from_bank,
            "to_account": "****" + t.to_account[-4:], "retry_count": t.retry_count}


@admin_router.get("/{transfer_id}/events")
async def admin_transfer_events(transfer_id: str, db: DB):
    stmt = select(TransferEvent).where(TransferEvent.transfer_id == transfer_id).order_by(TransferEvent.event_id)
    return [{"from": e.from_status, "to": e.to_status, "detail": e.detail, "at": e.created_at} for e in (await db.scalars(stmt)).all()]


@admin_router.get("/{transfer_id}/evidence")
async def admin_transfer_evidence(transfer_id: str, db: DB):
    """분쟁 조회 (SETTLEMENT_PLAN 12.5): 접수 요청·/receive 요청·응답 원문과 서명 검증 결과.
    body는 원본 바이트의 UTF-8 문자열 (디코드 안 되면 base64, body_encoding). 서명은 이 바이트로 다시 검증할 수 있다."""
    if await db.get(Transfer, transfer_id) is None:
        raise NotFound("TRANSFER_NOT_FOUND", "거래를 찾을 수 없습니다", transfer_id)
    return await evidence.list_for_transfer(db, transfer_id)


@admin_router.post("/confirm-pending")
async def admin_confirm_pending(db: DB):
    """관리 화면의 '다시 확인' 버튼."""
    return {"confirmed": await service.confirm_pending(db)}


@admin_router.post("/{transfer_id}/recheck")
async def admin_recheck_unknown(transfer_id: str, db: DB):
    """UNKNOWN 거래를 다시 자동 확인에 넣는다 (받는 은행 장애가 풀린 뒤). 지금 한 번 확인하고 결과 상태를 돌려준다."""
    return _admin_view(await service.recheck_unknown(db, transfer_id))


@admin_router.post("/{transfer_id}/resolve")
async def admin_resolve_unknown(transfer_id: str, data: TransferResolve, db: DB):
    """UNKNOWN 거래를 받는 은행과 직접 확인한 결과(COMPLETED/FAILED)로 확정한다.
    FAILED면 보내는 은행이 출금을 되돌리므로, 받는 은행에 입금이 없다는 것을 확인한 뒤에만 쓴다."""
    return _admin_view(await service.resolve_unknown(db, transfer_id, data))

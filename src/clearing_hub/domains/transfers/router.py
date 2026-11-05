from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank, require_admin
from clearing_hub.core.database import get_db
from clearing_hub.core.exception_handlers import error_body
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.transfers import service
from clearing_hub.domains.transfers.models import Transfer, TransferEvent, TransferStatus
from clearing_hub.domains.transfers.schemas import TransferDetail, TransferRequest, TransferResult

router = APIRouter(prefix="/v1/transfers", tags=["transfers"])
admin_router = APIRouter(prefix="/admin/transfers", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]
Sender = Annotated[Bank, Depends(current_bank)]


def _result_response(transfer: Transfer) -> JSONResponse:
    """상태에 따라 200 / 202 / 4xx (SPEC 3.3). 멱등 재전송에도 같은 형식으로 돌려준다."""
    result = TransferResult.model_validate(transfer, from_attributes=True).model_dump(mode="json")
    match transfer.status:
        case TransferStatus.COMPLETED:
            return JSONResponse(status_code=200, content=result)
        case TransferStatus.FAILED:
            http = 404 if transfer.fail_code in ("ACCOUNT_NOT_FOUND", "TRANSFER_NOT_FOUND") else 400
            return JSONResponse(status_code=http, content={**error_body(transfer.fail_code or "FAILED", transfer.fail_message or "", transfer.transfer_id), "status": "FAILED"})
        case _:
            return JSONResponse(status_code=202, content=result)


@router.post("", responses={200: {"model": TransferResult}, 202: {"model": TransferResult}})
async def create_transfer(data: TransferRequest, db: DB, sender: Sender):
    """이체 요청 (SPEC 3.3). 보내는 은행은 이 요청 전에 출금을 끝내 둔다."""
    transfer, _ = await service.accept_and_deliver(db, sender, data)
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
    rows = (await db.scalars(stmt)).all()
    return [{**TransferDetail.model_validate(t, from_attributes=True).model_dump(mode="json"), "from_bank": t.from_bank,
             "to_account": "****" + t.to_account[-4:], "retry_count": t.retry_count} for t in rows]


@admin_router.get("/{transfer_id}/events")
async def admin_transfer_events(transfer_id: str, db: DB):
    stmt = select(TransferEvent).where(TransferEvent.transfer_id == transfer_id).order_by(TransferEvent.event_id)
    return [{"from": e.from_status, "to": e.to_status, "detail": e.detail, "at": e.created_at} for e in (await db.scalars(stmt)).all()]


@admin_router.post("/confirm-pending")
async def admin_confirm_pending(db: DB):
    """관리 화면의 '다시 확인' 버튼."""
    return {"confirmed": await service.confirm_pending(db)}

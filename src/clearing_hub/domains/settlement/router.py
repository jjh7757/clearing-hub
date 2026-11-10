import csv
import io
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank, require_admin
from clearing_hub.core.config import get_settings
from clearing_hub.core.database import get_db
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.settlement import auto_confirm, service
from clearing_hub.domains.settlement.models import (
    AckResult, SettlementAck, SettlementPosition, SettlementRun, SettlementStatus,
)
from clearing_hub.workers import settlement as settlement_worker
from clearing_hub.domains.settlement.schemas import (
    AckAdminOut, AckOut, BankPositionOut, BankSettlementDetail, BankSettlementList, BankSettlementOut, CountTotal,
    DisputeIn, InFlightOut, PairOut, PositionOut, ReconTransferOut, SettlementConfirm, SettlementDetail,
    SettlementPreview, SettlementRunCreate, SettlementRunOut, SettlementTransferOut,
)

# 은행용 대사 API (SETTLEMENT_PLAN 6.1·13절, SPEC 10.4·10.5·10.7). 쿼리스트링 없이 부른다 (SPEC 2.2). 요청한 은행의 몫만 준다
router = APIRouter(prefix="/v1/settlements", tags=["settlements"])
# 관리자용 정산 API (SETTLEMENT_PLAN 6.2)
admin_router = APIRouter(prefix="/admin/settlements", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]
Me = Annotated[Bank, Depends(current_bank)]
CSV_COLUMNS = ("transfer_id", "from_bank", "to_bank", "amount", "finished_at", "holder_name")


def _my_position(position: SettlementPosition | None) -> BankPositionOut:
    """내 포지션. 그 run에 내 거래가 없으면 행이 없으므로 0·null."""
    return BankPositionOut() if position is None else BankPositionOut.model_validate(position, from_attributes=True)


@router.get("", response_model=BankSettlementList)
async def my_settlements(db: DB, bank: Me):
    """내 정산 목록 (SPEC 10.4): 최근 30일 run, 최신순, 최대 100개. CANCELLED는 빠진다.
    `net_amount`·`result`는 요청한 은행의 것이다 (그 run에 내 거래가 없으면 0·null)."""
    rows = await service.list_for_bank(db, bank.bank_code, service.bank_list_since())
    return BankSettlementList(settlements=[
        BankSettlementOut(**SettlementRunOut.model_validate(run, from_attributes=True).model_dump(),
                          net_amount=position.net_amount if position else 0,
                          result=position.result if position else None, my_ack=_my_ack(ack))
        for run, position, ack in rows
    ])


def _my_ack(ack: SettlementAck | None) -> str | None:
    return ack.result if ack else None


@router.get("/{run_id}", response_model=BankSettlementDetail)
async def my_settlement(run_id: int, db: DB, bank: Me):
    """대사 파일 (SPEC 10.5): run 요약 + 내 포지션 + 그 run에서 내가 보내거나 받은 거래(끝난 시각 순).
    다른 은행의 포지션, 계좌번호·이름은 넣지 않는다. 없는 run이면 404 SETTLEMENT_NOT_FOUND.
    CANCELLED run은 200이고 `transfers`가 빈 목록이다 (포지션은 기록된 값)."""
    view = await service.bank_view(db, run_id, bank.bank_code)
    me = bank.bank_code
    return BankSettlementDetail(
        **SettlementRunOut.model_validate(view.run, from_attributes=True).model_dump(),
        position=_my_position(view.position),
        transfers=[ReconTransferOut(transfer_id=t.transfer_id, direction="SENT" if t.from_bank == me else "RECEIVED",
                                    counterparty_bank=t.to_bank if t.from_bank == me else t.from_bank,
                                    amount=t.amount, finished_at=t.finished_at)
                   for t in view.transfers],
        my_ack=_my_ack(view.ack),
    )


async def _record(db: AsyncSession, run_id: int, bank: Bank, result: AckResult, data: DisputeIn | None = None) -> AckOut:
    # current_bank가 이미 세션을 썼으므로(autobegin) begin() 대신 commit한다. 예외면 커밋하지 않고 세션이 닫히며 롤백된다
    outcome = await auto_confirm.record(db, run_id, bank.bank_code, result,
                                        data.transfer_ids if data else None, data.message if data else None)
    await db.commit()
    return AckOut(run_id=outcome.run.run_id, status=outcome.run.status, settled_at=outcome.run.settled_at,
                  my_ack=outcome.ack.result)


@router.post("/{run_id}/ack", response_model=AckOut)
async def ack_settlement(run_id: int, db: DB, bank: Me):
    """대사 일치 (SPEC 10.7). 본문 없음(또는 `{}`). DRAFT에만 보낸다 (SETTLED·CANCELLED는 409 SETTLEMENT_NOT_DRAFT).
    포지션이 있는 모든 은행이 ack하고 dispute가 없으면 이 요청 안에서 확정돼 응답의 `status`가 `SETTLED`다.
    그 run에 내 거래가 없어도 받는다 (기록만, 확정 조건에는 세지 않는다). 다시 보내면 덮어쓴다."""
    return await _record(db, run_id, bank, AckResult.ACK)


@router.post("/{run_id}/dispute", response_model=AckOut)
async def dispute_settlement(run_id: int, data: DisputeIn, db: DB, bank: Me):
    """대사 불일치 (SPEC 10.7) `{transfer_ids, message?}`. 그 run은 자동 확정되지 않고 허브 관리자에게 알림이 간다.
    관리자가 확정하거나 취소한다. 나중에 맞으면 ack를 다시 보내 풀 수 있다. DRAFT에만 (409 SETTLEMENT_NOT_DRAFT)."""
    return await _record(db, run_id, bank, AckResult.DISPUTE, data)


def _confirm_deadline(run: SettlementRun) -> datetime | None:
    """DRAFT의 다음 확정 마감 시각 (workers/settlement.py가 auto_confirm.confirm_at_deadline을 부르는 시각).
    워커가 꺼져 있으면 마감 작업이 돌지 않으므로 None (관리자가 확정해야 한다)."""
    if run.status != SettlementStatus.DRAFT or not get_settings().settlement_worker_enabled:
        return None
    return settlement_worker.next_deadline(datetime.now(UTC))


def _detail(run: SettlementRun) -> SettlementDetail:
    acks = [AckAdminOut(bank_code=a.bank_code, result=a.result, transfer_ids=auto_confirm.transfer_ids(a),
                        message=a.message, created_at=a.created_at, updated_at=a.updated_at) for a in run.acks]
    answered = {a.bank_code for a in acks}
    return SettlementDetail(
        **SettlementRunOut.model_validate(run, from_attributes=True).model_dump(),
        positions=[PositionOut.model_validate(p, from_attributes=True) for p in run.positions],
        pairs=[PairOut.model_validate(p, from_attributes=True) for p in run.pairs],
        acks=acks, disputed=any(a.result == AckResult.DISPUTE for a in acks),
        unanswered=[p.bank_code for p in run.positions if p.bank_code not in answered],
        confirm_deadline_at=_confirm_deadline(run),
    )


@admin_router.post("/run", response_model=SettlementDetail, status_code=201)
async def admin_run_settlement(db: DB, data: SettlementRunCreate | None = None):
    """정산 DRAFT 생성 (8·9단계). 본문 `{settle_date?}`: 생략하면 어제, 오늘이면 지금 즉시 마감, 내일 이후는 422.
    미정산 COMPLETED 거래 중 마감 전에 끝난 것을 모아 은행별 포지션과 쌍별 표를 만든다. 예치금은 아직 그대로다.
    확정하지 않은 DRAFT가 있으면 409 SETTLEMENT_DRAFT_EXISTS (`run_id`): 확정하거나 취소한 뒤 다시 돌린다."""
    async with db.begin():
        run = await service.create_draft(db, data.settle_date if data else None)
    return _detail(run)


@admin_router.get("", response_model=list[SettlementRunOut])
async def admin_list_settlements(db: DB, limit: Annotated[int, Query(ge=1, le=365)] = 30):
    """최근 정산 목록, 최신순."""
    return [SettlementRunOut.model_validate(r, from_attributes=True) for r in await service.list_runs(db, limit)]


@admin_router.get("/preview", response_model=SettlementPreview)
async def admin_preview_settlement(db: DB):
    """"지금 정산하면" 미리보기 (읽기 전용). 본문 없는 `POST /admin/settlements/run`(어제, 마감 오늘 00:00 KST)이
    지금 모을 거래로 포지션·쌍별 표를 계산한다. run을 만들지 않고 거래에 표시하지 않는다.
    `after_cutoff`(마감 뒤 완료 → 다음 정산), `in_flight`(아직 안 끝남), `draft_run_id`(DRAFT가 있으면 그 거래는 빠진다).
    `/{run_id}`보다 먼저 선언한다 (경로 충돌)."""
    p = await service.preview(db)
    count, total, by_status = p.in_flight
    return SettlementPreview(
        settle_date=p.settle_date, cutoff_at=p.cutoff_at, tx_count=p.tx_count, total_amount=p.total_amount,
        positions=[PositionOut.model_validate(x, from_attributes=True) for x in p.positions],
        pairs=[PairOut.model_validate(x, from_attributes=True) for x in p.pairs],
        after_cutoff=CountTotal(count=p.after_cutoff[0], total=p.after_cutoff[1]),
        in_flight=InFlightOut(count=count, total=total, by_status=by_status),
        draft_run_id=p.draft_run_id,
    )


@admin_router.get("/{run_id}", response_model=SettlementDetail)
async def admin_get_settlement(run_id: int, db: DB):
    """정산 1회: 요약 + 은행별 포지션(은행 코드순, 합 0) + 쌍별 표 + 은행의 ack/dispute(`acks`, `disputed`)."""
    return _detail(await service.get_run(db, run_id))


async def _transfers(db: AsyncSession, run_id: int) -> list[SettlementTransferOut]:
    return [SettlementTransferOut.model_validate(t, from_attributes=True) for t in await service.run_transfers(db, run_id)]


@admin_router.get("/{run_id}/transfers", response_model=list[SettlementTransferOut])
async def admin_settlement_transfers(run_id: int, db: DB):
    """그 정산에 들어간 거래 목록, 끝난 시각 순."""
    return await _transfers(db, run_id)


@admin_router.get("/{run_id}/transfers.csv")
async def admin_settlement_transfers_csv(run_id: int, db: DB):
    """같은 거래 목록의 CSV (헤더 행 포함, UTF-8, BOM 없음). 시각은 UTC ISO 8601."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for t in await _transfers(db, run_id):
        row = t.model_dump(mode="json")
        writer.writerow(["" if row[c] is None else row[c] for c in CSV_COLUMNS])
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=settlement_{run_id}.csv"})


@admin_router.post("/{run_id}/cancel", response_model=SettlementRunOut)
async def admin_cancel_settlement(run_id: int, db: DB):
    """DRAFT 취소: 거래를 미정산으로 되돌려 다음 정산에 다시 들어가게 한다. DRAFT가 아니면 409 SETTLEMENT_NOT_DRAFT."""
    async with db.begin():
        run = await service.cancel_draft(db, run_id)
    return SettlementRunOut.model_validate(run, from_attributes=True)


@admin_router.post("/{run_id}/confirm", response_model=SettlementDetail)
async def admin_confirm_settlement(run_id: int, db: DB, data: SettlementConfirm | None = None):
    """확정 (11·12단계): 은행별 차액을 예치금 원장에 반영하고 run을 SETTLED로. 본문 `{reason?}`(200자까지, 로그에 남김).
    응답의 포지션에 `result`(OK / OVERDRAWN / OVERDUE)와 `balance_after`(반영 뒤 잔액)가 채워진다.
    DRAFT가 아니면 409 SETTLEMENT_NOT_DRAFT (`run_id`), 없으면 404. 확정한 run은 취소할 수 없다.
    dispute가 있어도 확정한다 (관리자 판정, SETTLEMENT_PLAN 13절). 은행 ack를 기다리지 않는 강제 확정이기도 하다."""
    async with db.begin():
        run = await service.confirm(db, run_id, confirmed_by="admin", reason=data.reason if data else "")
    return _detail(run)

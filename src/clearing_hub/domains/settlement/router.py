import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import require_admin
from clearing_hub.core.database import get_db
from clearing_hub.domains.settlement import service
from clearing_hub.domains.settlement.models import SettlementRun
from clearing_hub.domains.settlement.schemas import (
    PairOut, PositionOut, SettlementConfirm, SettlementDetail, SettlementRunCreate, SettlementRunOut,
    SettlementTransferOut,
)

# 관리자용 정산 API (SETTLEMENT_PLAN 6.2). 은행용 대사 API(/v1/settlements)는 5단계
admin_router = APIRouter(prefix="/admin/settlements", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]
CSV_COLUMNS = ("transfer_id", "from_bank", "to_bank", "amount", "finished_at", "holder_name")


def _detail(run: SettlementRun) -> SettlementDetail:
    return SettlementDetail(
        **SettlementRunOut.model_validate(run, from_attributes=True).model_dump(),
        positions=[PositionOut.model_validate(p, from_attributes=True) for p in run.positions],
        pairs=[PairOut.model_validate(p, from_attributes=True) for p in run.pairs],
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


@admin_router.get("/{run_id}", response_model=SettlementDetail)
async def admin_get_settlement(run_id: int, db: DB):
    """정산 1회: 요약 + 은행별 포지션(은행 코드순, 합 0) + 쌍별 표."""
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
    DRAFT가 아니면 409 SETTLEMENT_NOT_DRAFT (`run_id`), 없으면 404. 확정한 run은 취소할 수 없다."""
    async with db.begin():
        run = await service.confirm(db, run_id, confirmed_by="admin", reason=data.reason if data else "")
    return _detail(run)

"""정산 배치 DRAFT: 8단계 수집, 9단계 다자간 차액 계산 (SETTLEMENT_PLAN 2절 2·3·4번, 5.1).

커밋은 하지 않는다. 라우터(`async with db.begin()`)나 4단계 워커가 트랜잭션을 정한다.
예치금 반영(11·12단계, `confirm` → `reserve.apply_instruction`)은 4단계다. 여기서는 지시서(`instruction_for`)만 만든다.
"""

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from clearing_hub.core.config import get_settings
from clearing_hub.core.exceptions import ApiException, NotFound
from clearing_hub.domains.settlement.models import (
    PairDirection, SettlementPair, SettlementPosition, SettlementRun, SettlementStatus,
)
from clearing_hub.domains.transfers.models import Transfer, TransferStatus

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _invalid(message: str) -> ApiException:
    return ApiException(422, "VALIDATION_ERROR", message)


def compute_cutoff(settle_date: date | None, now: datetime) -> tuple[date, datetime]:
    """정산일과 마감 시각(UTC)을 정한다 (2절 3번, 6.2).

    - 생략: 어제(settlement_timezone 기준). 마감 = 정산일 다음 날 00:00(tz)
    - 오늘: 마감 = 지금 (즉시 마감, 시연용). 아래의 min 규칙과 같은 결과다
    - 내일 이후: 422 VALIDATION_ERROR
    마감은 미래가 될 수 없다: min(정산일 다음 날 00:00, now)
    """
    tz = ZoneInfo(get_settings().settlement_timezone)
    today = now.astimezone(tz).date()
    if settle_date is None:
        settle_date = today - timedelta(days=1)
    if settle_date > today:
        raise _invalid(f"정산일은 오늘({today.isoformat()}) 이전이어야 합니다 (요청 {settle_date.isoformat()})")
    cutoff = datetime.combine(settle_date + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    return settle_date, min(cutoff, now.astimezone(UTC))


def _draft_exists(run_id: int) -> ApiException:
    return ApiException(409, "SETTLEMENT_DRAFT_EXISTS",
                        f"확정하지 않은 정산(run_id {run_id})이 있습니다. 확정하거나 취소한 뒤 다시 돌리세요",
                        extra={"run_id": run_id})


async def _current_draft(db: AsyncSession) -> SettlementRun | None:
    return await db.scalar(select(SettlementRun).where(SettlementRun.status == SettlementStatus.DRAFT))


async def create_draft(db: AsyncSession, settle_date: date | None, created_by: str = "admin") -> SettlementRun:
    """정산 DRAFT를 만든다: 수집(8단계) → 상계(9단계). 호출한 쪽의 트랜잭션 하나 안에서 끝난다.

    1. DRAFT가 이미 있으면 409 SETTLEMENT_DRAFT_EXISTS (동시에 하나만. 동시 실행은 부분 유니크 인덱스가 막는다)
    2. run(DRAFT) 생성
    3. 수집: 미정산 COMPLETED 거래 중 cutoff_at 전에 끝난 것에 run_id를 찍는다 — UPDATE 한 문장이라, 도는 중에
       COMPLETED 된 거래와 경쟁하지 않는다 (10절). 날짜가 아니라 '미정산' 기준이라 늦게 COMPLETED 된 거래도
       빠지지 않는다 (2절 3번)
    4. 상계: 그 run의 거래를 (from_bank, to_bank)로 묶은 합계 하나에서 은행별 포지션과 쌍별 표를 만든다
    5. 검증: Σ net_amount = 0, 건수·금액이 수집한 것과 같아야 한다. 아니면 RuntimeError(계산 버그) → 롤백

    거래가 0건이어도 run은 만든다 (tx_count 0, 포지션 없음). 이것도 DRAFT라 확정·취소 전에는 다음 run이 막힌다.
    """
    settle_date, cutoff_at = compute_cutoff(settle_date, _now())
    if (draft := await _current_draft(db)) is not None:
        raise _draft_exists(draft.run_id)

    run = SettlementRun(settle_date=settle_date, cutoff_at=cutoff_at, status=SettlementStatus.DRAFT,
                        tx_count=0, total_amount=0, created_by=created_by, positions=[], pairs=[])
    db.add(run)
    try:
        await db.flush()
    except IntegrityError:  # 동시에 들어온 다른 run이 먼저 DRAFT를 만들었다 (uq_settlement_runs_single_draft)
        raise ApiException(409, "SETTLEMENT_DRAFT_EXISTS", "다른 정산 실행이 방금 DRAFT를 만들었습니다") from None

    # 8단계: 수집. ORM 객체를 읽지 않았으므로 세션 동기화는 하지 않는다
    collected = await db.execute(
        update(Transfer)
        .where(Transfer.status == TransferStatus.COMPLETED, Transfer.settlement_run_id.is_(None),
               Transfer.finished_at < cutoff_at)
        .values(settlement_run_id=run.run_id)
        .execution_options(synchronize_session=False)
    )
    run.tx_count = collected.rowcount

    # 9단계: 상계
    rows = (await db.execute(
        select(Transfer.from_bank, Transfer.to_bank, func.count(), func.sum(Transfer.amount))
        .where(Transfer.settlement_run_id == run.run_id)
        .group_by(Transfer.from_bank, Transfer.to_bank)
    )).all()
    flows = {(from_bank, to_bank): (int(count), int(total)) for from_bank, to_bank, count, total in rows}
    run.positions.extend(_positions(flows))
    run.pairs.extend(_pairs(flows))
    run.total_amount = sum(total for _, total in flows.values())

    _verify(run, sum(count for count, _ in flows.values()))
    await db.flush()
    logger.info("정산 DRAFT run %s: %s, 마감 %s, %s건 %s원", run.run_id, settle_date, cutoff_at.isoformat(),
                run.tx_count, run.total_amount)
    return run


def _positions(flows: dict[tuple[str, str], tuple[int, int]]) -> list[SettlementPosition]:
    """은행별 [sent_count, sent_total, recv_count, recv_total]. 거래에 나타난 은행만, 은행 코드순."""
    acc: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for (from_bank, to_bank), (count, total) in flows.items():
        acc[from_bank][0] += count
        acc[from_bank][1] += total
        acc[to_bank][2] += count
        acc[to_bank][3] += total
    return [SettlementPosition(bank_code=code, sent_count=sc, sent_total=st, recv_count=rc, recv_total=rt,
                               net_amount=rt - st)
            for code, (sc, st, rc, rt) in sorted(acc.items())]


def _pairs(flows: dict[tuple[str, str], tuple[int, int]]) -> list[SettlementPair]:
    """은행 쌍별 표. bank_a < bank_b로 한 쌍에 한 행. net_amount는 절댓값, 방향은 net_direction."""
    banks = sorted({(min(f, t), max(f, t)) for f, t in flows})
    pairs = []
    for a, b in banks:
        ab_count, ab_total = flows.get((a, b), (0, 0))
        ba_count, ba_total = flows.get((b, a), (0, 0))
        net = ab_total - ba_total
        direction = PairDirection.A_TO_B if net > 0 else PairDirection.B_TO_A if net < 0 else PairDirection.NONE
        pairs.append(SettlementPair(bank_a=a, bank_b=b, a_to_b_count=ab_count, a_to_b_total=ab_total,
                                    b_to_a_count=ba_count, b_to_a_total=ba_total, net_amount=abs(net),
                                    net_direction=direction))
    return pairs


def _verify(run: SettlementRun, grouped_count: int) -> None:
    """계산 버그 방어 (5.1의 9단계). 예외가 나면 호출한 쪽의 트랜잭션이 롤백돼 run도 거래 표시도 남지 않는다."""
    net_sum = sum(p.net_amount for p in run.positions)
    if net_sum != 0:
        raise RuntimeError(f"정산 run {run.run_id}: 포지션 합이 0이 아닙니다 ({net_sum})")
    if grouped_count != run.tx_count:
        raise RuntimeError(f"정산 run {run.run_id}: 수집 {run.tx_count}건과 집계 {grouped_count}건이 다릅니다")
    if sum(p.sent_total for p in run.positions) != run.total_amount:
        raise RuntimeError(f"정산 run {run.run_id}: 보낸 금액 합이 거래 금액 합과 다릅니다")


async def _lock_run(db: AsyncSession, run_id: int) -> SettlementRun:
    """run 행을 잠그고 읽는다 (취소와 4단계 확정이 겹치지 않게). 없으면 404."""
    stmt = (select(SettlementRun).where(SettlementRun.run_id == run_id)
            .with_for_update().execution_options(populate_existing=True))
    if (run := await db.scalar(stmt)) is None:
        raise _not_found(run_id)
    return run


def _not_found(run_id: int) -> ApiException:
    return NotFound("SETTLEMENT_NOT_FOUND", f"정산 run {run_id}을 찾을 수 없습니다")


async def cancel_draft(db: AsyncSession, run_id: int) -> SettlementRun:
    """DRAFT를 취소한다: 그 run의 거래를 미정산(settlement_run_id NULL)으로 되돌리고 run을 CANCELLED로.
    되돌린 거래는 다음 run에 다시 들어간다. 포지션·쌍별 표는 기록으로 남긴다. DRAFT가 아니면 409 SETTLEMENT_NOT_DRAFT."""
    run = await _lock_run(db, run_id)
    if run.status != SettlementStatus.DRAFT:
        raise ApiException(409, "SETTLEMENT_NOT_DRAFT", f"DRAFT인 정산만 취소할 수 있습니다 (지금 {run.status})",
                           extra={"run_id": run_id})
    await db.execute(update(Transfer).where(Transfer.settlement_run_id == run_id).values(settlement_run_id=None)
                     .execution_options(synchronize_session=False))
    run.status = SettlementStatus.CANCELLED
    await db.flush()
    logger.warning("정산 DRAFT run %s 취소 (%s건 미정산으로 되돌림)", run_id, run.tx_count)
    return run


async def get_run(db: AsyncSession, run_id: int) -> SettlementRun:
    """run과 포지션(은행 코드순)·쌍별 표. 없으면 404 SETTLEMENT_NOT_FOUND."""
    stmt = (select(SettlementRun).where(SettlementRun.run_id == run_id)
            .options(selectinload(SettlementRun.positions), selectinload(SettlementRun.pairs))
            .execution_options(populate_existing=True))
    if (run := await db.scalar(stmt)) is None:
        raise _not_found(run_id)
    return run


async def list_runs(db: AsyncSession, limit: int = 30) -> list[SettlementRun]:
    """최근 run 목록, 최신순 (포지션은 읽지 않는다)."""
    stmt = select(SettlementRun).order_by(SettlementRun.run_id.desc()).limit(limit)
    return list((await db.scalars(stmt)).all())


async def run_transfers(db: AsyncSession, run_id: int) -> list[Transfer]:
    """그 run이 모은 거래, 끝난 시각 순 (대사 파일·CSV용). run이 없으면 404."""
    if await db.get(SettlementRun, run_id) is None:
        raise _not_found(run_id)
    stmt = (select(Transfer).where(Transfer.settlement_run_id == run_id)
            .order_by(Transfer.finished_at, Transfer.transfer_id))
    return list((await db.scalars(stmt)).all())


def instruction_for(run: SettlementRun) -> list[tuple[str, int]]:
    """포지션 → 정산 지시서 [(bank_code, net_amount)], 은행 코드순 (5.1의 11단계).
    4단계 confirm이 이것을 `reserve.apply_instruction(db, run.run_id, ...)`에 넘긴다. positions가 읽혀 있어야 한다
    (create_draft의 결과나 get_run)."""
    return [(p.bank_code, p.net_amount) for p in sorted(run.positions, key=lambda p: p.bank_code)]

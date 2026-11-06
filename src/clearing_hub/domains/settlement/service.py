"""정산 배치: 8단계 수집·9단계 다자간 차액 계산(DRAFT), 11·12단계 확정·예치금 반영 (SETTLEMENT_PLAN 2절 2·3·4·7번, 5.1),
10단계 은행용 대사 파일 (`list_for_bank`·`bank_view`, 6.1, SPEC 10절).
은행의 ack/dispute와 자동 확정(전원 ack·마감 시각)은 `auto_confirm.py` (13절).

커밋은 하지 않는다. 라우터(`async with db.begin()`)나 워커(`workers/settlement.py`)가 트랜잭션을 정한다.
예치금은 이 도메인이 직접 바꾸지 않는다. `confirm`이 지시서(`instruction_for`)를 `reserve.apply_instruction`에 넘긴다 (2절 1번).
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from clearing_hub.core import alerts
from clearing_hub.core.config import get_settings
from clearing_hub.core.exceptions import ApiException, NotFound
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.reserve import service as reserve_service
from clearing_hub.domains.reserve.models import PositionStatus
from clearing_hub.domains.settlement.models import (
    PairDirection, SettlementAck, SettlementPair, SettlementPosition, SettlementRun, SettlementStatus,
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

    거래가 0건이어도 run은 만든다 (tx_count 0, 포지션 없음). 이것도 DRAFT라 확정·취소 전에는 다음 run이 막힌다
    (일일 워커는 빈 run을 바로 확정한다 — `workers/settlement.run_daily`).
    """
    settle_date, cutoff_at = compute_cutoff(settle_date, _now())
    if (draft := await _current_draft(db)) is not None:
        raise _draft_exists(draft.run_id)

    run = SettlementRun(settle_date=settle_date, cutoff_at=cutoff_at, status=SettlementStatus.DRAFT,
                        tx_count=0, total_amount=0, created_by=created_by, positions=[], pairs=[], acks=[])
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
    """run 행을 잠그고 읽는다 (취소와 확정이 겹치지 않게, 확정이 두 번 돌지 않게). 없으면 404."""
    stmt = (select(SettlementRun).where(SettlementRun.run_id == run_id)
            .with_for_update().execution_options(populate_existing=True))
    if (run := await db.scalar(stmt)) is None:
        raise _not_found(run_id)
    return run


def _not_found(run_id: int) -> ApiException:
    return NotFound("SETTLEMENT_NOT_FOUND", f"정산 run {run_id}을 찾을 수 없습니다")


def _not_draft(run: SettlementRun, action: str) -> ApiException:
    return ApiException(409, "SETTLEMENT_NOT_DRAFT", f"DRAFT인 정산만 {action}할 수 있습니다 (지금 {run.status})",
                        extra={"run_id": run.run_id})


async def cancel_draft(db: AsyncSession, run_id: int) -> SettlementRun:
    """DRAFT를 취소한다: 그 run의 거래를 미정산(settlement_run_id NULL)으로 되돌리고 run을 CANCELLED로.
    되돌린 거래는 다음 run에 다시 들어간다. 포지션·쌍별 표는 기록으로 남긴다. DRAFT가 아니면 409 SETTLEMENT_NOT_DRAFT."""
    run = await _lock_run(db, run_id)
    if run.status != SettlementStatus.DRAFT:
        raise _not_draft(run, "취소")
    await db.execute(update(Transfer).where(Transfer.settlement_run_id == run_id).values(settlement_run_id=None)
                     .execution_options(synchronize_session=False))
    run.status = SettlementStatus.CANCELLED
    await db.flush()
    logger.warning("정산 DRAFT run %s 취소 (%s건 미정산으로 되돌림)", run_id, run.tx_count)
    return run


async def confirm(db: AsyncSession, run_id: int, *, confirmed_by: str = "admin", reason: str = "") -> SettlementRun:
    """DRAFT를 확정한다: 정산 지시서를 예치금 원장에 반영하고 run을 SETTLED로 (5.1의 11·12단계).
    호출한 쪽의 트랜잭션 하나 안에서 끝난다.

    1. run 행을 잠근다(`_lock_run`, FOR UPDATE). DRAFT가 아니면 409 SETTLEMENT_NOT_DRAFT(`run_id`) — 두 번 확정,
       취소와 겹친 확정을 막는다 (`cancel_draft`도 같은 잠금을 쓴다)
    2. 포지션(은행 코드순) → 지시서 `instruction_for(run)` → `reserve.apply_instruction(db, run_id, 지시서)`
       (분개의 created_by는 "settlement". 누가 확정했는지는 run의 `settled_by`에 적는다: 관리자 / "auto" /
       "auto-ack" / "auto-deadline")
    3. 결과(OK / OVERDRAWN / OVERDUE, 반영 뒤 잔액)를 포지션의 `result`·`balance_after`에 옮겨 적는다
    4. `status = SETTLED`, `settled_at`, `settled_by`. 사유(`reason`)는 로그에만 남긴다
    5. OVERDUE 은행이 있으면 logger.error + 알림 웹훅 `overdue`. 그 은행은 접수 때 순채무한도 검사에 걸려 송금이 막히고
       받기만 된다. 관리자가 예치하면 풀린다 (2절 7번)

    **순채무(exposure, 4.4)와의 관계**: exposure는 SETTLED가 아닌 run의 거래를 계속 센다. SETTLED로 바꾸는 것과
    예치금 반영이 **같은 트랜잭션**이라, 커밋하는 순간 순채무에서 빠지는 금액과 잔액에 들어가는 금액이 동시에
    바뀐다. 그래서 `available = balance + cap − exposure`는 확정 전후로 같다.

    포지션이 없는 run(거래 0건)은 지시서가 빈 목록이고, `apply_instruction([])`은 합 0이라 아무것도 바꾸지 않고
    지나간다. 그대로 SETTLED가 된다.
    `apply_instruction`이 409 RESERVE_ALREADY_APPLIED를 던지면(run은 DRAFT인데 분개가 이미 있음 = 장부 불일치)
    그대로 전파한다. 트랜잭션이 롤백돼 run은 DRAFT로 남는다. 다른 예외도 마찬가지로 아무것도 남지 않는다.
    """
    run = await _lock_run(db, run_id)
    if run.status != SettlementStatus.DRAFT:
        raise _not_draft(run, "확정")
    run = await get_run(db, run_id)  # 포지션·쌍별 표 (잠금은 이미 잡았다)

    results = await reserve_service.apply_instruction(db, run_id, instruction_for(run))
    by_bank = {r.bank_code: r for r in results}
    for position in run.positions:
        result = by_bank[position.bank_code]
        position.result, position.balance_after = result.status.value, result.balance_after
    run.status = SettlementStatus.SETTLED
    run.settled_at, run.settled_by = _now(), confirmed_by
    await db.flush()

    for r in results:
        if r.status == PositionStatus.OVERDUE:
            cap = (await db.get(Bank, r.bank_code)).net_debit_cap
            logger.error("정산 run %s 확정: 은행 %s 예치금 부족 OVERDUE (잔액 %s원, 순채무한도 %s원). "
                         "송금이 막힙니다. 관리자 예치가 필요합니다", run_id, r.bank_code, r.balance_after, cap,
                         extra={"bank_code": r.bank_code})
            alerts.notify("overdue", f"정산 run {run_id} 확정 결과 은행 {r.bank_code}가 OVERDUE입니다 (잔액 {r.balance_after:,}원, "
                          f"한도 {cap:,}원). 송금이 막혔습니다. 예치금 입금 또는 한도 상향이 필요합니다",
                          run_id=run_id, bank_code=r.bank_code)
    logger.info("정산 run %s 확정 (%s%s): %s개 은행, %s건 %s원", run_id, confirmed_by,
                f", 사유: {reason}" if reason else "", len(results), run.tx_count, run.total_amount)
    return run


async def get_run(db: AsyncSession, run_id: int) -> SettlementRun:
    """run과 포지션(은행 코드순)·쌍별 표·은행의 ack/dispute. 없으면 404 SETTLEMENT_NOT_FOUND."""
    stmt = (select(SettlementRun).where(SettlementRun.run_id == run_id)
            .options(selectinload(SettlementRun.positions), selectinload(SettlementRun.pairs),
                     selectinload(SettlementRun.acks))
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
    `confirm`이 이것을 `reserve.apply_instruction(db, run.run_id, ...)`에 넘긴다. positions가 읽혀 있어야 한다
    (create_draft의 결과나 get_run)."""
    return [(p.bank_code, p.net_amount) for p in sorted(run.positions, key=lambda p: p.bank_code)]


# --- 은행용 대사 API (SETTLEMENT_PLAN 6.1, 5.1의 10단계, SPEC 10절) --------------------------------------------------
# 요청한 은행 자신의 몫만 돌려준다. 다른 은행의 포지션·잔액, 계좌번호·이름은 읽지도 않는다.

BANK_LIST_DAYS = 30  # GET /v1/settlements: settle_date가 오늘(tz) − 30일 이후인 run만
BANK_LIST_LIMIT = 100


def bank_list_since(now: datetime | None = None) -> date:
    """은행용 목록의 시작 정산일: 오늘(settlement_timezone) − 30일."""
    today = (now or _now()).astimezone(ZoneInfo(get_settings().settlement_timezone)).date()
    return today - timedelta(days=BANK_LIST_DAYS)


async def list_for_bank(
    db: AsyncSession, bank_code: str, since: date, limit: int = BANK_LIST_LIMIT,
) -> list[tuple[SettlementRun, SettlementPosition | None, SettlementAck | None]]:
    """`since` 이후 정산일의 run과 그 run에서의 내 포지션(내 거래가 없으면 None)·내 ack(안 보냈으면 None), 최신순(run_id desc).

    CANCELLED는 뺀다. 은행이 전에 본 DRAFT가 취소되면 목록에서 사라지고, 상세(`bank_view`)에서는 CANCELLED로 보인다.
    포지션·ack는 (run_id, bank_code)로 바깥 조인하므로 다른 은행의 행은 읽지 않는다."""
    stmt = (select(SettlementRun, SettlementPosition, SettlementAck)
            .outerjoin(SettlementPosition, (SettlementPosition.run_id == SettlementRun.run_id)
                       & (SettlementPosition.bank_code == bank_code))
            .outerjoin(SettlementAck, (SettlementAck.run_id == SettlementRun.run_id)
                       & (SettlementAck.bank_code == bank_code))
            .where(SettlementRun.settle_date >= since, SettlementRun.status != SettlementStatus.CANCELLED)
            .order_by(SettlementRun.run_id.desc()).limit(limit))
    return [(run, position, ack) for run, position, ack in (await db.execute(stmt)).all()]


@dataclass(frozen=True)
class BankView:
    """대사 파일 하나: run, 그 run에서의 내 포지션(없으면 None), 내가 보내거나 받은 거래(끝난 시각 순), 내 ack(없으면 None)."""

    run: SettlementRun
    position: SettlementPosition | None
    transfers: list[Transfer]
    ack: SettlementAck | None = None


async def bank_view(db: AsyncSession, run_id: int, bank_code: str) -> BankView:
    """은행 `bank_code`에게 줄 대사 파일. 없는 run이면 404 SETTLEMENT_NOT_FOUND.

    - 포지션: 그 run에 내 거래가 없으면 None (라우터가 0·null로 채운다). CANCELLED run도 기록된 값 그대로
    - 거래: 그 run의 거래 중 `from_bank` 또는 `to_bank`가 나인 것. CANCELLED run은 빈 목록이다
      (취소하면 거래의 settlement_run_id가 NULL로 돌아가 이미 비어 있지만, 다음 run에 들어간 거래를 여기서
      보여 주지 않는다는 뜻을 분명히 하려고 따로 거른다)
    """
    if (run := await db.get(SettlementRun, run_id)) is None:
        raise _not_found(run_id)
    position = await db.get(SettlementPosition, (run_id, bank_code))
    ack = await db.get(SettlementAck, (run_id, bank_code))
    transfers: list[Transfer] = []
    if run.status != SettlementStatus.CANCELLED:
        stmt = (select(Transfer)
                .where(Transfer.settlement_run_id == run_id,
                       (Transfer.from_bank == bank_code) | (Transfer.to_bank == bank_code))
                .order_by(Transfer.finished_at, Transfer.transfer_id))
        transfers = list((await db.scalars(stmt)).all())
    return BankView(run, position, transfers, ack)

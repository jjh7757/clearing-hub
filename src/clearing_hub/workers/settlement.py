"""일일 정산 배치 태스크 (SETTLEMENT_PLAN 2절 3·4번, 5.1, 10절). 앱 lifespan에서 시작한다 (`settlement_worker_enabled`).

매일 `settlement_cutoff_time`(기본 00:05, `settlement_timezone` 기준)에 어제 정산일로 DRAFT를 만들고
(`create_draft(None)`, 마감 = 오늘 00:00), `settlement_auto_confirm`이면 이어서 확정한다.
- 시작할 때 오늘 배치를 놓쳤으면(허브가 꺼져 있었음) 바로 한 번 돌린다 (catch-up, 10절)
- 확정하지 않은 DRAFT가 남아 있으면 그날은 건너뛴다 (경고 로그 + 알림 `batch_skipped`). 그 DRAFT가 확정·취소돼야
  다음 배치가 돈다. 건너뛴 날의 거래는 미정산으로 남아 다음 run에 들어가므로 빠지지 않는다 (2절 3번)
- 거래가 0건인 run은 대사할 것이 없으므로 `settlement_auto_confirm`과 상관없이 바로 확정한다
  (DRAFT로 두면 거래가 없던 날마다 관리자가 정리해야 다음 배치가 돈다)

매일 `settlement_confirm_deadline`(기본 09:00)에는 확정 마감 작업(`run_deadline`)을 한다 (13절): DRAFT가 있고 dispute가
없으면 확정한다. 은행이 모두 ack하면 그 전에 이미 확정돼 있다 (settlement/auto_confirm.py). 한 루프가 두 시각 중 가까운
쪽까지 잔다. 시작할 때 오늘 마감 시각이 지났는데 그 전에 만든 DRAFT가 남아 있으면(허브가 꺼져 있었음) 마감 작업부터 한다.
"""

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core import alerts
from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.core.exceptions import ApiException
from clearing_hub.domains.settlement import auto_confirm
from clearing_hub.domains.settlement import service as settlement
from clearing_hub.domains.settlement.models import SettlementRun, SettlementStatus

logger = logging.getLogger(__name__)

AUTO = "auto"


def _now() -> datetime:
    return datetime.now(UTC)


def _today_at(now: datetime, hhmm: str) -> datetime:
    """now가 속한 날(settlement_timezone)의 HH:MM, UTC."""
    tz = ZoneInfo(get_settings().settlement_timezone)
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(now.astimezone(tz).date(), time(hour, minute), tzinfo=tz).astimezone(UTC)


def _next_at(now: datetime, hhmm: str) -> datetime:
    """다음 HH:MM(UTC): 오늘(settlement_timezone)의 그 시각. 이미 지났거나 지금이면 내일 같은 시각."""
    at = _today_at(now, hhmm)
    if now >= at:
        tz = ZoneInfo(get_settings().settlement_timezone)
        local = at.astimezone(tz)
        at = datetime.combine(local.date() + timedelta(days=1), local.time(), tzinfo=tz).astimezone(UTC)
    return at


def _today_cutoff(now: datetime) -> datetime:
    """now가 속한 날(settlement_timezone)의 배치 시각, UTC."""
    return _today_at(now, get_settings().settlement_cutoff_time)


def next_cutoff(now: datetime) -> datetime:
    """다음 배치 시각(UTC): 오늘(settlement_timezone)의 settlement_cutoff_time. 이미 지났거나 지금이면 내일 같은 시각."""
    return _next_at(now, get_settings().settlement_cutoff_time)


def next_deadline(now: datetime) -> datetime:
    """다음 확정 마감 시각(UTC): 오늘(settlement_timezone)의 settlement_confirm_deadline. 지났거나 지금이면 내일."""
    return _next_at(now, get_settings().settlement_confirm_deadline)


async def needs_deadline_catch_up(db: AsyncSession, now: datetime) -> bool:
    """오늘 마감 시각이 지났고, 그 전에 만든 DRAFT가 아직 있으면 참 (허브가 꺼져 있어 마감 작업을 놓쳤다)."""
    deadline = _today_at(now, get_settings().settlement_confirm_deadline)
    if now < deadline:
        return False
    return bool(await db.scalar(select(exists().where(SettlementRun.status == SettlementStatus.DRAFT,
                                                      SettlementRun.created_at < deadline))))


async def needs_catch_up(db: AsyncSession, now: datetime) -> bool:
    """오늘(settlement_timezone) 만들어진 run이 하나도 없고 오늘의 배치 시각이 지났으면 참 (배치를 놓침, 10절).
    관리자가 오늘 돌린 run(즉시 마감, 취소한 것 포함)도 '오늘 run'으로 본다."""
    if now < _today_cutoff(now):
        return False
    tz = ZoneInfo(get_settings().settlement_timezone)
    midnight = datetime.combine(now.astimezone(tz).date(), time(0), tzinfo=tz).astimezone(UTC)
    return not await db.scalar(select(exists().where(SettlementRun.created_at >= midnight)))


async def run_daily(db: AsyncSession) -> SettlementRun | None:
    """어제 정산일로 DRAFT를 만들고(트랜잭션 1), 자동 확정이거나 거래가 0건이면 확정한다(트랜잭션 2).
    db는 트랜잭션이 열려 있지 않은 세션이어야 한다. DRAFT가 이미 있으면 경고하고 None (관리자가 정리해야 한다).
    확정에 실패해도 DRAFT는 남는다(관리자가 확정·취소)."""
    try:
        async with db.begin():
            run = await settlement.create_draft(db, None, created_by=AUTO)
    except ApiException as exc:
        if exc.code != "SETTLEMENT_DRAFT_EXISTS":
            raise
        logger.warning("일일 정산 배치를 건너뜁니다: %s", exc.message)
        run_id = exc.extra.get("run_id")
        alerts.notify("batch_skipped", f"일일 정산 배치를 건너뛰었습니다: 확정하지 않은 DRAFT(run {run_id})가 남아 있습니다. "
                      "dispute를 판정(확정 또는 취소)해야 다음 배치가 돕니다", run_id=run_id)
        return None

    if get_settings().settlement_auto_confirm or run.tx_count == 0:
        async with db.begin():
            run = await settlement.confirm(db, run.run_id, confirmed_by=AUTO,
                                           reason="자동 확정" if run.tx_count else "거래 0건")
    return run


async def run_deadline(db: AsyncSession) -> SettlementRun | None:
    """확정 마감 작업 (트랜잭션 1개): dispute 없는 DRAFT를 `auto-deadline`으로 확정한다. DRAFT가 없으면 None,
    dispute가 있으면 확정하지 않은 그 DRAFT (알림만). db는 트랜잭션이 열려 있지 않은 세션이어야 한다."""
    async with db.begin():
        return await auto_confirm.confirm_at_deadline(db)


async def _deadline_once(label: str) -> None:
    try:
        async with SessionLocal() as db:
            run = await run_deadline(db)
        if run is not None:
            logger.info("정산 확정 마감(%s): run %s %s", label, run.run_id, run.status)
    except Exception:  # 마감 작업이 실패해도 루프는 계속 돈다 (DRAFT는 남아 있다)
        logger.exception("정산 확정 마감 오류 (%s)", label)


async def _run_once(label: str) -> None:
    try:
        async with SessionLocal() as db:
            run = await run_daily(db)
        if run is not None:
            logger.info("일일 정산 배치(%s): run %s %s", label, run.run_id, run.status)
    except Exception:  # 태스크가 죽으면 다음 날부터 배치가 멈추므로 기록만 하고 계속 돈다
        logger.exception("일일 정산 배치 오류 (%s)", label)


async def settlement_loop() -> None:
    """시작 때 catch-up 검사(마감 → 배치 순) → 배치 시각과 마감 시각 중 가까운 쪽까지 잠듦 → 그 작업 → 반복.

    마감 catch-up을 먼저 하는 까닭: 놓친 마감으로 남은 DRAFT가 있으면 배치 catch-up이 그 DRAFT 때문에 건너뛰기 때문이다."""
    try:
        async with SessionLocal() as db:
            missed_deadline = await needs_deadline_catch_up(db, _now())
    except Exception:
        logger.exception("정산 확정 마감 catch-up 검사 오류")
        missed_deadline = False
    if missed_deadline:
        logger.warning("오늘 정산 확정 마감 작업이 돌지 않았습니다 (허브가 꺼져 있었음). 지금 돌립니다")
        await _deadline_once("catch-up")
    try:
        async with SessionLocal() as db:
            missed = await needs_catch_up(db, _now())
    except Exception:
        logger.exception("정산 배치 catch-up 검사 오류")
        missed = False
    if missed:
        logger.warning("오늘 정산 배치가 돌지 않았습니다 (허브가 꺼져 있었음). 지금 돌립니다")
        await _run_once("catch-up")

    cutoff, deadline = next_cutoff(_now()), next_deadline(_now())
    while True:
        target = min(cutoff, deadline)
        await asyncio.sleep(max((target - _now()).total_seconds(), 0))
        # 일찍 깨어 목표 시각 전에 끝났더라도 같은 날 두 번 돌지 않게, 지난 목표 시각 다음으로 잡는다.
        # 두 시각이 같게 설정됐으면 배치 → 마감 순으로 둘 다 한다
        if target == cutoff:
            await _run_once("정시")
            cutoff = next_cutoff(max(_now(), cutoff))
        if target == deadline:
            await _deadline_once("정시")
            deadline = next_deadline(max(_now(), deadline))

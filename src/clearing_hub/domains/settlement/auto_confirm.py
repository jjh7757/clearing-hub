"""은행 확인 기반 자동 확정 (SETTLEMENT_PLAN 13절, SPEC 10.6·10.8). 관리자가 매일 DRAFT를 확정하지 않아도 되게 한다.

은행은 대사 파일(10.5)을 자기 장부와 맞춰 본 뒤 결과를 보낸다: 맞으면 ack, 안 맞으면 dispute (`record`).

1. **전원 ack**: ack를 받을 때마다 본다. 그 run이 DRAFT이고, dispute가 하나도 없고, **포지션이 있는 모든 은행**이
   ACK면 같은 요청(트랜잭션) 안에서 `confirm(confirmed_by="auto-ack")`. 포지션이 없는 은행의 ack는 기록만 하고 세지 않는다
2. **마감 시각**: 워커가 매일 `SETTLEMENT_CONFIRM_DEADLINE`(기본 09:00 KST)에 `confirm_at_deadline`을 부른다.
   DRAFT가 있고 dispute가 없으면 ack하지 않은 은행이 있어도 `confirm(confirmed_by="auto-deadline")`. 대사를 하지 않는
   은행(가짜 은행 902 등) 때문에 확정이 멈추지 않게 하기 위해서다
3. **dispute가 있으면 자동으로 확정하지 않는다.** 관리자가 증거를 보고 확정(강제) 또는 취소한다. 취소해도 ack 기록은 남는다.
   같은 은행이 나중에 ack를 다시 보내면 dispute가 풀리고 1·2가 다시 적용된다

기존 `SETTLEMENT_AUTO_CONFIRM`(생성 즉시 확정, 시연용)과 거래 0건 run의 즉시 확정(workers/settlement.run_daily)은 그대로다.
커밋은 하지 않는다 (service.py와 같다). 라우터·워커가 트랜잭션을 정한다.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core import alerts
from clearing_hub.domains.settlement import service
from clearing_hub.domains.settlement.models import (
    AckResult, SettlementAck, SettlementPosition, SettlementRun, SettlementStatus,
)

logger = logging.getLogger(__name__)

AUTO_ACK = "auto-ack"
AUTO_DEADLINE = "auto-deadline"
ALERT_IDS = 10  # 알림에 넣을 transfer_id 수 (나머지는 "외 N건")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AckState:
    """한 run의 확인 현황. waiting: 포지션이 있는데 아직 ACK하지 않은 은행, disputed: DISPUTE를 보낸 은행 (코드순)."""

    waiting: list[str]
    disputed: list[str]

    @property
    def all_acked(self) -> bool:
        return not self.waiting and not self.disputed


async def ack_state(db: AsyncSession, run_id: int) -> AckState:
    position_banks = set((await db.scalars(
        select(SettlementPosition.bank_code).where(SettlementPosition.run_id == run_id))).all())
    acks = (await db.scalars(select(SettlementAck).where(SettlementAck.run_id == run_id))).all()
    acked = {a.bank_code for a in acks if a.result == AckResult.ACK}
    return AckState(waiting=sorted(position_banks - acked),
                    disputed=sorted(a.bank_code for a in acks if a.result == AckResult.DISPUTE))


def transfer_ids(ack: SettlementAck) -> list[str]:
    return json.loads(ack.transfer_ids_json or "[]")


def _short_ids(ids: list[str]) -> str:
    shown = ",".join(ids[:ALERT_IDS])
    return f"{shown} 외 {len(ids) - ALERT_IDS}건" if len(ids) > ALERT_IDS else shown


@dataclass(frozen=True)
class AckOutcome:
    run: SettlementRun
    ack: SettlementAck
    auto_confirmed: bool  # 이 ack로 전원 ack가 돼 확정했다


async def record(db: AsyncSession, run_id: int, bank_code: str, result: AckResult,
                 ids: list[str] | None = None, message: str | None = None) -> AckOutcome:
    """은행의 ack/dispute를 기록한다 (자기 몫만, 은행당 한 행 덮어쓰기). DRAFT가 아니면 409 SETTLEMENT_NOT_DRAFT, 없으면 404.

    run 행을 잠그고(`_lock_run`, 확정·취소와 같은 잠금) 하므로, 두 은행의 마지막 ack가 동시에 와도 확정은 한 번만 돈다
    (뒤의 요청은 앞의 커밋을 기다린 뒤 run이 SETTLED인 것을 보고 409를 받는다 — 은행은 10.5로 SETTLED를 확인하면 된다).
    ack면 이어서 전원 ack인지 보고 맞으면 같은 트랜잭션에서 확정한다. dispute면 logger.error + 알림 `dispute`."""
    run = await service._lock_run(db, run_id)
    action = "ack" if result == AckResult.ACK else "dispute"
    if run.status != SettlementStatus.DRAFT:
        raise service._not_draft(run, action)

    ids_json = json.dumps(list(ids or []))
    now = _now()
    ack = await db.get(SettlementAck, (run_id, bank_code))
    if ack is None:
        ack = SettlementAck(run_id=run_id, bank_code=bank_code, result=result, transfer_ids_json=ids_json,
                            message=message, created_at=now, updated_at=now)
        db.add(ack)
    else:  # 다시 보냄: ACK ↔ DISPUTE 모두 덮어쓴다
        ack.result, ack.transfer_ids_json, ack.message, ack.updated_at = result, ids_json, message, now
    await db.flush()

    if result == AckResult.DISPUTE:
        logger.error("정산 run %s: 은행 %s가 대사 불일치(dispute)를 보냈습니다 (%s건: %s, %s). 자동 확정하지 않습니다. "
                     "관리자가 확정 또는 취소해야 합니다", run_id, bank_code, len(ids or []), _short_ids(ids or []),
                     message or "설명 없음", extra={"bank_code": bank_code})
        alerts.notify("dispute", f"정산 run {run_id}: 은행 {bank_code}가 대사 불일치를 알렸습니다 ({message or '설명 없음'}). "
                      "자동 확정을 멈췄습니다. 증거를 보고 확정 또는 취소하세요",
                      run_id=run_id, bank_code=bank_code, transfer_ids=_short_ids(ids or []) or None)
        return AckOutcome(run, ack, False)

    logger.info("정산 run %s: 은행 %s ack", run_id, bank_code, extra={"bank_code": bank_code})
    if not (await ack_state(db, run_id)).all_acked:
        return AckOutcome(run, ack, False)
    run = await service.confirm(db, run_id, confirmed_by=AUTO_ACK, reason="포지션이 있는 은행 전원 ack")
    _announce(run, "포지션이 있는 은행 전원 ack")
    return AckOutcome(run, ack, True)


def _announce(run: SettlementRun, why: str) -> None:
    alerts.notify("auto_confirm", f"정산 run {run.run_id} 자동 확정 ({why}): {len(run.positions)}개 은행, "
                  f"{run.tx_count}건 {run.total_amount:,}원", run_id=run.run_id, settled_by=run.settled_by)


async def confirm_at_deadline(db: AsyncSession) -> SettlementRun | None:
    """마감 시각 작업: 지금 DRAFT가 없으면 None. dispute가 있으면 확정하지 않고 logger.error + 알림 `deadline_held`
    (그 DRAFT를 그대로 돌려준다). 없으면 ack하지 않은 은행이 있어도 확정한다 (`auto-deadline`)."""
    draft = await service._current_draft(db)
    if draft is None:
        return None
    state = await ack_state(db, draft.run_id)
    if state.disputed:
        logger.error("정산 run %s: 마감 시각이지만 dispute가 있어 확정하지 않습니다 (은행 %s). 관리자 판정 필요",
                     draft.run_id, ", ".join(state.disputed))
        alerts.notify("deadline_held", f"정산 run {draft.run_id}: 확정 마감 시각이지만 dispute가 있어 확정하지 않았습니다 "
                      f"(은행 {', '.join(state.disputed)}). 판정하기 전까지 다음 정산 배치도 건너뜁니다",
                      run_id=draft.run_id)
        return draft
    why = "마감 시각" + (f", ack 안 한 은행 {', '.join(state.waiting)}" if state.waiting else "")
    run = await service.confirm(db, draft.run_id, confirmed_by=AUTO_DEADLINE, reason=why)
    _announce(run, why)
    return run

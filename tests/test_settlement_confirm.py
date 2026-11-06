"""정산 확정·예치금 반영, 일일 워커 (SETTLEMENT_PLAN 2절 4·7번, 4.4, 5.1의 11·12단계, 6.2, 7절 4단계, 8절 S5~S7, 10절).

이체는 허브 API로 만든다 (`banks` 픽스처는 한도가 넉넉하다). 워커 함수는 직접 부른다 (conftest가 lifespan의 워커를 끈다).
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.reserve import service as reserve
from clearing_hub.domains.reserve.models import ReserveAccount, ReserveLedgerEntry
from clearing_hub.domains.settlement import service as settlement
from clearing_hub.domains.settlement.models import SettlementRun, SettlementStatus
from clearing_hub.domains.transfers.models import Transfer
from clearing_hub.workers import settlement as worker
from tests.conftest import ADMIN, send_902_to_901, set_net_debit_cap, transfer_payload

pytestmark = pytest.mark.anyio
KST = ZoneInfo("Asia/Seoul")


def _today() -> date:
    return datetime.now(KST).date()


async def _run(client) -> dict:
    res = await client.post("/admin/settlements/run", json={"settle_date": str(_today())}, headers=ADMIN)
    assert res.status_code == 201, res.text
    return res.json()


async def _confirm(client, run_id: int, body: dict | None = None):
    return await client.post(f"/admin/settlements/{run_id}/confirm", json=body or {}, headers=ADMIN)


async def _deposit(client, bank_code: str, amount: int) -> None:
    res = await client.post(f"/admin/reserve/{bank_code}/deposit", json={"amount": amount}, headers=ADMIN)
    assert res.status_code == 200, res.text


async def _send(sender, amount: int):
    return await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=amount))


async def _s1(banks, monkeypatch) -> None:
    """S1의 하루치: 901→902 5만 2건, 902→901 3만 1건 → 901 −7만, 902 +7만."""
    sender, receiver = banks
    for _ in range(2):
        assert (await _send(sender, 50_000)).json()["status"] == "COMPLETED"
    await send_902_to_901(sender, receiver, monkeypatch, 30_000)


async def _balances() -> dict[str, int]:
    async with SessionLocal() as db:
        return {a.bank_code: a.balance for a in (await db.scalars(select(ReserveAccount))).all()}


async def _settle_entries(run_id: int | None = None) -> list[tuple[str, str, int, int]]:
    async with SessionLocal() as db:
        stmt = (select(ReserveLedgerEntry).where(ReserveLedgerEntry.run_id.is_not(None))
                .order_by(ReserveLedgerEntry.bank_code))
        if run_id is not None:
            stmt = stmt.where(ReserveLedgerEntry.run_id == run_id)
        return [(e.bank_code, e.kind, e.amount, e.run_id) for e in (await db.scalars(stmt)).all()]


async def _status(run_id: int) -> str:
    async with SessionLocal() as db:
        return (await db.get(SettlementRun, run_id)).status


# --- S5 확정 ---------------------------------------------------------------------------------------------------------

async def test_s5_confirm_moves_reserve(client, banks, monkeypatch):
    """S5: 901 예치 10만, S1 → 확정 → 901 3만 OK, 902 7만 OK. SETTLE_DEBIT/CREDIT 각 1건, 순채무 0."""
    sender, receiver = banks
    await _deposit(client, "901", 100_000)
    await _s1(banks, monkeypatch)
    run_id = (await _run(client))["run_id"]

    res = await _confirm(client, run_id, {"reason": "대사 완료"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "SETTLED" and body["settled_by"] == "admin" and body["created_by"] == "admin"
    assert datetime.fromisoformat(body["settled_at"]).tzinfo is not None
    assert [(p["bank_code"], p["net_amount"], p["result"], p["balance_after"]) for p in body["positions"]] == [
        ("901", -70_000, "OK", 30_000), ("902", 70_000, "OK", 70_000)]
    assert len(body["pairs"]) == 1

    assert await _balances() == {"901": 30_000, "902": 70_000}
    assert await _settle_entries() == [("901", "SETTLE_DEBIT", -70_000, run_id),
                                       ("902", "SETTLE_CREDIT", 70_000, run_id)]
    assert (await client.get(f"/admin/settlements/{run_id}", headers=ADMIN)).json() == body
    for bank in (sender, receiver):
        assert (await bank.get("/v1/reserve")).json()["exposure"] == 0


# --- S6 두 번 확정·취소한 run·없는 run --------------------------------------------------------------------------------

async def test_s6_confirm_only_once(client, banks, monkeypatch):
    """S6: 같은 run 두 번 → 409 SETTLEMENT_NOT_DRAFT(run_id), 잔액·분개 그대로. 취소한 run → 409. 없는 run → 404."""
    await _deposit(client, "901", 100_000)
    await _s1(banks, monkeypatch)
    run_id = (await _run(client))["run_id"]
    assert (await _confirm(client, run_id)).status_code == 200
    balances, entries = await _balances(), await _settle_entries()

    res = await _confirm(client, run_id)
    assert res.status_code == 409
    assert res.json()["error"] == "SETTLEMENT_NOT_DRAFT" and res.json()["run_id"] == run_id
    assert await _balances() == balances and await _settle_entries() == entries
    res = await client.post(f"/admin/settlements/{run_id}/cancel", headers=ADMIN)
    assert res.status_code == 409 and res.json()["error"] == "SETTLEMENT_NOT_DRAFT"

    cancelled = (await _run(client))["run_id"]  # 거래 0건
    assert (await client.post(f"/admin/settlements/{cancelled}/cancel", headers=ADMIN)).status_code == 200
    res = await _confirm(client, cancelled)
    assert res.status_code == 409 and res.json()["error"] == "SETTLEMENT_NOT_DRAFT"
    assert await _status(cancelled) == SettlementStatus.CANCELLED

    res = await _confirm(client, 999)
    assert res.status_code == 404 and res.json()["error"] == "SETTLEMENT_NOT_FOUND"
    draft = (await _run(client))["run_id"]
    res = await _confirm(client, draft, {"reason": "가" * 201})
    assert res.status_code == 422 and res.json()["error"] == "VALIDATION_ERROR"
    assert (await client.post(f"/admin/settlements/{draft}/confirm")).status_code == 401  # 관리자 토큰 필요
    assert await _status(draft) == SettlementStatus.DRAFT


# --- S7 예치금 부족 --------------------------------------------------------------------------------------------------

async def test_s7_overdue_blocks_sending_until_deposit(client, banks, monkeypatch, caplog):
    """S7: 901 예치 5만·한도 0, net −7만 → OVERDUE, 잔액 −2만, 관리자 알림(logger.error).
    그 뒤 901 송금은 LIMIT_EXCEEDED, 902→901 입금은 된다. 관리자가 2만 이상 예치하면 다시 보낼 수 있다."""
    sender, receiver = banks
    await _deposit(client, "901", 50_000)
    await _s1(banks, monkeypatch)  # 보내는 동안은 한도가 넉넉하다
    run_id = (await _run(client))["run_id"]
    await set_net_debit_cap(client, "901", 0)

    with caplog.at_level(logging.ERROR):
        res = await _confirm(client, run_id)
    assert res.status_code == 200, res.text
    assert [(p["bank_code"], p["result"], p["balance_after"]) for p in res.json()["positions"]] == [
        ("901", "OVERDUE", -20_000), ("902", "OK", 70_000)]
    alerts = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR and r.name == settlement.logger.name]
    assert len(alerts) == 1 and "901" in alerts[0] and "-20000원" in alerts[0] and "순채무한도 0원" in alerts[0]

    res = await _send(sender, 10_000)
    assert res.status_code == 400 and res.json()["error"] == "LIMIT_EXCEEDED" and res.json()["status"] == "FAILED"
    await send_902_to_901(sender, receiver, monkeypatch, 10_000)  # 받기는 된다 (COMPLETED)
    assert (await _send(sender, 1)).json()["error"] == "LIMIT_EXCEEDED"  # available = −2만 + 1만 받은 것

    await _deposit(client, "901", 20_000)  # 잔액 0, available 1만
    assert (await sender.get("/v1/reserve")).json()["available"] == 10_000
    assert (await _send(sender, 10_000)).json()["status"] == "COMPLETED"


async def test_s7_overdrawn_within_cap_can_still_send(client, banks, monkeypatch):
    """같은 상황에서 한도가 10만이면 OVERDRAWN(한도 안의 마이너스). 송금은 남은 한도만큼 된다."""
    sender, _ = banks
    await _deposit(client, "901", 50_000)
    await _s1(banks, monkeypatch)
    run_id = (await _run(client))["run_id"]
    await set_net_debit_cap(client, "901", 100_000)

    res = await _confirm(client, run_id)
    assert [(p["bank_code"], p["result"], p["balance_after"]) for p in res.json()["positions"]] == [
        ("901", "OVERDRAWN", -20_000), ("902", "OK", 70_000)]
    assert (await sender.get("/v1/reserve")).json()["available"] == 80_000
    assert (await _send(sender, 50_000)).json()["status"] == "COMPLETED"


# --- 빈 run·원자성·장부 불일치 ---------------------------------------------------------------------------------------

async def test_confirm_empty_run(client, banks):
    """거래 0건 run도 확정된다: 지시서가 빈 목록이라 예치금·분개는 그대로, run만 SETTLED."""
    run_id = (await _run(client))["run_id"]
    res = await _confirm(client, run_id)
    assert res.status_code == 200 and res.json()["status"] == "SETTLED" and res.json()["positions"] == []
    assert await _settle_entries() == [] and await _balances() == {"901": 0, "902": 0}
    assert (await _run(client))["run_id"] != run_id  # DRAFT가 비었으므로 다음 run을 만들 수 있다


async def test_confirm_is_atomic(client, banks, monkeypatch):
    """예치금을 반영한 뒤 예외가 나면 트랜잭션 전체가 롤백된다: run은 DRAFT, 잔액·분개·포지션 결과 그대로."""
    await _deposit(client, "901", 100_000)
    await _s1(banks, monkeypatch)
    run_id = (await _run(client))["run_id"]
    original = reserve.apply_instruction

    async def apply_then_fail(db, run_id, instruction, **kw):
        await original(db, run_id, instruction, **kw)  # 잔액·분개를 바꾼 뒤
        raise RuntimeError("반영 도중 오류")

    monkeypatch.setattr(reserve, "apply_instruction", apply_then_fail)
    with pytest.raises(RuntimeError, match="반영 도중 오류"):
        async with SessionLocal() as db, db.begin():
            await settlement.confirm(db, run_id)
    monkeypatch.setattr(reserve, "apply_instruction", original)

    assert await _status(run_id) == SettlementStatus.DRAFT
    assert await _balances() == {"901": 100_000, "902": 0} and await _settle_entries() == []
    detail = (await client.get(f"/admin/settlements/{run_id}", headers=ADMIN)).json()
    assert detail["settled_at"] is None and all(p["result"] is None for p in detail["positions"])
    assert (await _confirm(client, run_id)).status_code == 200  # 고친 뒤 다시 확정할 수 있다


async def test_confirm_propagates_reserve_already_applied(client, banks, monkeypatch):
    """run은 DRAFT인데 그 run_id의 분개가 이미 있으면(장부 불일치) 409 RESERVE_ALREADY_APPLIED, run은 DRAFT로 남는다."""
    await _s1(banks, monkeypatch)
    run_id = (await _run(client))["run_id"]
    async with SessionLocal() as db, db.begin():
        await reserve.apply_instruction(db, run_id, [("901", -1), ("902", 1)])

    res = await _confirm(client, run_id)
    assert res.status_code == 409 and res.json()["error"] == "RESERVE_ALREADY_APPLIED"
    assert await _status(run_id) == SettlementStatus.DRAFT
    assert await _balances() == {"901": -1, "902": 1}


# --- 일일 워커 -------------------------------------------------------------------------------------------------------

def _kst(*args) -> datetime:
    return datetime(*args, tzinfo=KST)


def test_next_cutoff():
    """다음 배치 시각: 오늘 00:05(KST)가 아직이면 오늘, 지났거나 지금이면 내일. UTC로 돌려준다."""
    assert worker.next_cutoff(_kst(2026, 11, 6, 0, 0)) == datetime(2026, 11, 5, 15, 5, tzinfo=UTC)  # 오늘 00:05 KST
    assert worker.next_cutoff(_kst(2026, 11, 6, 0, 5)) == datetime(2026, 11, 6, 15, 5, tzinfo=UTC)  # 지금 → 내일
    assert worker.next_cutoff(_kst(2026, 11, 6, 12, 0)) == datetime(2026, 11, 6, 15, 5, tzinfo=UTC)
    # 자정 경계: KST 23:59(UTC 14:59)와 다음 날 00:01(UTC 15:01)은 같은 배치(11-07 00:05 KST)를 기다린다
    assert worker.next_cutoff(datetime(2026, 11, 6, 14, 59, tzinfo=UTC)) == datetime(2026, 11, 6, 15, 5, tzinfo=UTC)
    assert worker.next_cutoff(datetime(2026, 11, 6, 15, 1, tzinfo=UTC)) == datetime(2026, 11, 6, 15, 5, tzinfo=UTC)
    assert worker.next_cutoff(_kst(2026, 12, 31, 1, 0)) == datetime(2026, 12, 31, 15, 5, tzinfo=UTC)  # 해 넘김
    assert worker.next_cutoff(_kst(2026, 11, 6, 0, 0)).tzinfo == UTC


def test_next_cutoff_follows_settings(monkeypatch):
    monkeypatch.setattr(get_settings(), "settlement_cutoff_time", "23:30")
    assert worker.next_cutoff(_kst(2026, 11, 6, 23, 0)) == datetime(2026, 11, 6, 14, 30, tzinfo=UTC)
    monkeypatch.setattr(get_settings(), "settlement_timezone", "UTC")
    assert worker.next_cutoff(datetime(2026, 11, 6, 23, 31, tzinfo=UTC)) == datetime(2026, 11, 7, 23, 30, tzinfo=UTC)


async def _yesterday_and_today(sender) -> tuple[str, str]:
    """어제 23:00(KST)에 끝난 거래 1건(1만)과 오늘 끝난 거래 1건(2만)."""
    old = (await _send(sender, 10_000)).json()["transfer_id"]
    new = (await _send(sender, 20_000)).json()["transfer_id"]
    midnight = datetime.combine(_today(), datetime.min.time(), tzinfo=KST)
    async with SessionLocal() as db:
        await db.execute(update(Transfer).where(Transfer.transfer_id == old)
                         .values(finished_at=(midnight - timedelta(hours=1)).astimezone(UTC)))
        await db.commit()
    return old, new


async def test_run_daily_makes_yesterday_draft(client, banks):
    """자동 확정이 꺼져 있으면 어제 정산 DRAFT만 만든다 (created_by auto, 마감 = 오늘 00:00 KST). 예치금은 그대로."""
    sender, _ = banks
    old, new = await _yesterday_and_today(sender)
    async with SessionLocal() as db:
        run = await worker.run_daily(db)
    yesterday = _today() - timedelta(days=1)
    assert (run.status, run.created_by, run.settle_date) == (SettlementStatus.DRAFT, "auto", yesterday)
    assert (run.tx_count, run.total_amount) == (1, 10_000)
    async with SessionLocal() as db:
        assert (await db.get(Transfer, old)).settlement_run_id == run.run_id
        assert (await db.get(Transfer, new)).settlement_run_id is None
    assert await _settle_entries() == []


async def test_run_daily_auto_confirm(client, banks, monkeypatch):
    """settlement_auto_confirm이면 이어서 확정한다 (settled_by auto, 예치금 반영)."""
    sender, _ = banks
    await _yesterday_and_today(sender)
    monkeypatch.setattr(get_settings(), "settlement_auto_confirm", True)
    async with SessionLocal() as db:
        run = await worker.run_daily(db)
    assert (run.status, run.created_by, run.settled_by) == (SettlementStatus.SETTLED, "auto", "auto")
    detail = (await client.get(f"/admin/settlements/{run.run_id}", headers=ADMIN)).json()
    assert [(p["bank_code"], p["result"], p["balance_after"]) for p in detail["positions"]] == [
        ("901", "OVERDRAWN", -10_000), ("902", "OK", 10_000)]  # 901은 예치 0, 한도 안의 마이너스


async def test_run_daily_confirms_empty_run(client, banks):
    """거래가 0건이면 자동 확정이 꺼져 있어도 바로 확정한다 (DRAFT로 남겨 다음 날 배치를 막지 않게)."""
    async with SessionLocal() as db:
        run = await worker.run_daily(db)
    assert (run.status, run.tx_count, run.settled_by) == (SettlementStatus.SETTLED, 0, "auto")
    async with SessionLocal() as db:
        assert (await worker.run_daily(db)).status == SettlementStatus.SETTLED  # 다음 배치도 막히지 않는다


async def test_run_daily_skips_when_draft_exists(client, banks, caplog):
    """확정하지 않은 DRAFT가 있으면 None + 경고. 관리자가 정리해야 한다."""
    draft = (await _run(client))["run_id"]
    with caplog.at_level(logging.WARNING):
        async with SessionLocal() as db:
            assert await worker.run_daily(db) is None
    assert any(r.levelno == logging.WARNING and str(draft) in r.getMessage() for r in caplog.records)
    async with SessionLocal() as db:
        assert [r.run_id for r in (await db.scalars(select(SettlementRun))).all()] == [draft]


async def test_needs_catch_up(client, banks):
    """오늘(KST) 만든 run이 없고 오늘 배치 시각이 지났으면 참. 배치 시각 전이거나 오늘 run이 있으면 거짓."""
    async with SessionLocal() as db:
        assert await worker.needs_catch_up(db, _kst(2026, 11, 6, 0, 4)) is False  # 배치 시각(00:05) 전
        assert await worker.needs_catch_up(db, _kst(2026, 11, 6, 0, 5)) is True  # 지났고 run 없음
        assert await worker.needs_catch_up(db, _kst(2026, 11, 6, 1, 0)) is True

    run_id = (await _run(client))["run_id"]  # 지금 만든 run (관리자 즉시 마감)
    now = datetime.now(UTC)
    if now < worker._today_cutoff(now):  # KST 00:00~00:05에 돌면 배치 시각 전이라 판단이 다르다
        pytest.skip("배치 시각 전")
    async with SessionLocal() as db:
        assert await worker.needs_catch_up(db, now) is False
        yesterday = datetime.combine(_today(), datetime.min.time(), tzinfo=KST) - timedelta(minutes=1)
        await db.execute(update(SettlementRun).where(SettlementRun.run_id == run_id)
                         .values(created_at=yesterday.astimezone(UTC)))
        await db.commit()
        assert await worker.needs_catch_up(db, now) is True  # 어제 만든 run만 있음


async def test_settlement_loop_catches_up_and_survives_errors(monkeypatch):
    """시작 때 놓친 배치를 바로 돌리고, 그 뒤 배치 시각마다 돈다. run_daily가 예외를 내도 태스크는 계속 돈다."""
    calls = []

    async def missed(db, now):
        return True

    async def failing_run_daily(db):
        calls.append(datetime.now(UTC))
        raise RuntimeError("배치 실패")

    monkeypatch.setattr(worker, "needs_catch_up", missed)
    monkeypatch.setattr(worker, "run_daily", failing_run_daily)
    monkeypatch.setattr(worker, "next_cutoff", lambda now: now + timedelta(seconds=0.05))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(worker.settlement_loop(), timeout=0.4)
    assert len(calls) >= 3  # catch-up 1번 + 정시 여러 번

"""은행 확인 기반 자동 확정 (SETTLEMENT_PLAN 13절, SPEC 10.6·10.7).

S1의 하루치(901→902 5만 2건, 902→901 3만 1건)로 DRAFT를 만들고, 은행이 ack/dispute를 보낸다.
포지션이 있는 은행(901·902)이 모두 ack하면 그 요청 안에서 확정(`auto-ack`), 아니면 마감 시각 워커가 dispute 없을 때 확정
(`auto-deadline`). dispute가 있으면 자동 확정하지 않고 알림만 간다.
"""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.settlement.models import SettlementStatus
from clearing_hub.workers import settlement as worker
from tests.conftest import ADMIN, register_bank, send_902_to_901, sent_alerts, transfer_payload

pytestmark = pytest.mark.anyio
KST = ZoneInfo("Asia/Seoul")


def _today() -> date:
    return datetime.now(KST).date()


async def _draft(client, banks, monkeypatch) -> tuple[int, list[str]]:
    """S1의 하루치로 오늘 즉시 마감 DRAFT. (run_id, 901이 보낸 transfer_id 2개)"""
    sender, receiver = banks
    sent = []
    for _ in range(2):
        payload = transfer_payload("110-123-0001")
        assert (await sender.post("/v1/transfers", payload)).json()["status"] == "COMPLETED"
        sent.append(payload["transfer_id"])
    await send_902_to_901(sender, receiver, monkeypatch, 30_000)
    res = await client.post("/admin/settlements/run", json={"settle_date": str(_today())}, headers=ADMIN)
    assert res.status_code == 201, res.text
    return res.json()["run_id"], sent


async def _ack(bank, run_id: int):
    return await bank.post(f"/v1/settlements/{run_id}/ack")


async def _dispute(bank, run_id: int, ids: list[str], message: str | None = "허브에만 있는 거래"):
    body = {"transfer_ids": ids} | ({"message": message} if message else {})
    return await bank.post(f"/v1/settlements/{run_id}/dispute", body)


async def _admin(client, run_id: int) -> dict:
    res = await client.get(f"/admin/settlements/{run_id}", headers=ADMIN)
    assert res.status_code == 200, res.text
    return res.json()


async def test_all_acks_confirm_immediately(client, banks, monkeypatch, alert_sink):
    """901 ack → DRAFT 유지, 902 ack → 그 요청 안에서 SETTLED (settled_by auto-ack), 예치금 반영, 알림."""
    sender, receiver = banks
    run_id, _ = await _draft(client, banks, monkeypatch)

    res = await _ack(sender, run_id)
    assert res.status_code == 200, res.text
    assert res.json() == {"run_id": run_id, "status": "DRAFT", "settled_at": None, "my_ack": "ACK"}
    res = await _ack(receiver, run_id)
    assert res.status_code == 200, res.text
    assert (res.json()["status"], res.json()["my_ack"]) == ("SETTLED", "ACK") and res.json()["settled_at"]

    detail = await _admin(client, run_id)
    assert (detail["status"], detail["settled_by"], detail["disputed"]) == ("SETTLED", "auto-ack", False)
    assert [(a["bank_code"], a["result"], a["transfer_ids"]) for a in detail["acks"]] == [
        ("901", "ACK", []), ("902", "ACK", [])]
    assert [(p["bank_code"], p["balance_after"]) for p in detail["positions"]] == [("901", -70_000), ("902", 70_000)]
    # 은행 목록·상세에 my_ack
    listed = (await sender.get("/v1/settlements")).json()["settlements"]
    assert (listed[0]["run_id"], listed[0]["status"], listed[0]["my_ack"]) == (run_id, "SETTLED", "ACK")
    assert (await sender.get(f"/v1/settlements/{run_id}")).json()["my_ack"] == "ACK"

    texts = await sent_alerts(alert_sink, "auto_confirm")
    assert len(texts) == 1 and f"run {run_id}" in texts[0] and "2개 은행" in texts[0] and "130,000원" in texts[0]


async def test_partial_ack_stays_draft(client, banks, monkeypatch):
    """한 은행만 ack하면 DRAFT. 내 ack만 보인다 (902의 my_ack는 null)."""
    sender, receiver = banks
    run_id, _ = await _draft(client, banks, monkeypatch)
    assert (await _ack(sender, run_id)).json()["status"] == "DRAFT"
    assert (await _ack(sender, run_id)).json()["status"] == "DRAFT"  # 같은 은행이 다시 보내도 한 행 (덮어쓰기)

    assert (await sender.get(f"/v1/settlements/{run_id}")).json()["my_ack"] == "ACK"
    assert (await receiver.get(f"/v1/settlements/{run_id}")).json()["my_ack"] is None
    assert (await receiver.get("/v1/settlements")).json()["settlements"][0]["my_ack"] is None
    detail = await _admin(client, run_id)
    assert detail["status"] == "DRAFT" and [a["bank_code"] for a in detail["acks"]] == ["901"]


async def test_ack_from_bank_without_position_is_not_counted(client, banks, monkeypatch):
    """그 run에 거래가 없는 903의 ack는 기록만 한다. 903이 ack해도 902가 안 했으면 DRAFT, 902가 하면 확정."""
    sender, receiver = banks
    c_bank = await register_bank(client, "903", "CBank", base_url="http://c-bank/interbank")
    run_id, _ = await _draft(client, banks, monkeypatch)

    assert (await _ack(c_bank, run_id)).json() == {"run_id": run_id, "status": "DRAFT", "settled_at": None, "my_ack": "ACK"}
    assert (await _ack(sender, run_id)).json()["status"] == "DRAFT"
    assert [a["bank_code"] for a in (await _admin(client, run_id))["acks"]] == ["901", "903"]
    assert (await _ack(receiver, run_id)).json()["status"] == "SETTLED"


async def test_dispute_blocks_auto_confirm_until_re_ack(client, banks, monkeypatch, alert_sink):
    """902가 dispute → 901이 ack해도 DRAFT, 관리자 상세에 disputed·차이 ID, 알림. 902가 나중에 ack하면(DISPUTE → ACK) 확정."""
    sender, receiver = banks
    run_id, sent = await _draft(client, banks, monkeypatch)

    res = await _dispute(receiver, run_id, sent[:1])
    assert res.status_code == 200, res.text
    assert res.json() == {"run_id": run_id, "status": "DRAFT", "settled_at": None, "my_ack": "DISPUTE"}
    assert (await _ack(sender, run_id)).json()["status"] == "DRAFT"

    detail = await _admin(client, run_id)
    assert detail["disputed"] is True and detail["status"] == "DRAFT"
    assert [(a["bank_code"], a["result"], a["transfer_ids"], a["message"]) for a in detail["acks"]] == [
        ("901", "ACK", [], None), ("902", "DISPUTE", sent[:1], "허브에만 있는 거래")]
    assert (await receiver.get(f"/v1/settlements/{run_id}")).json()["my_ack"] == "DISPUTE"
    texts = await sent_alerts(alert_sink, "dispute")
    assert len(texts) == 1 and f"run {run_id}" in texts[0] and "902" in texts[0] and sent[0] in texts[0]

    res = await _ack(receiver, run_id)  # 은행이 다시 맞춰 보니 맞음 → dispute가 풀린다
    assert res.json()["status"] == "SETTLED"
    detail = await _admin(client, run_id)
    assert (detail["settled_by"], detail["disputed"]) == ("auto-ack", False)


async def test_dispute_validation(client, banks, monkeypatch):
    sender, _ = banks
    run_id, _ = await _draft(client, banks, monkeypatch)
    for bad in ({}, {"transfer_ids": "x"}, {"transfer_ids": [""]}, {"transfer_ids": [], "message": "x" * 201}):
        res = await sender.post(f"/v1/settlements/{run_id}/dispute", bad)
        assert (res.status_code, res.json()["error"]) == (422, "VALIDATION_ERROR"), bad
    res = await _dispute(sender, run_id, [], message=None)  # 빈 목록·설명 없음도 된다 (차액만 다를 때)
    assert res.status_code == 200 and res.json()["my_ack"] == "DISPUTE"


async def test_ack_only_on_draft(client, banks, monkeypatch):
    """SETTLED·CANCELLED run에 ack/dispute → 409 SETTLEMENT_NOT_DRAFT, 없는 run → 404 SETTLEMENT_NOT_FOUND."""
    sender, _ = banks
    run_id, sent = await _draft(client, banks, monkeypatch)
    assert (await client.post(f"/admin/settlements/{run_id}/confirm", json={}, headers=ADMIN)).status_code == 200
    for res in (await _ack(sender, run_id), await _dispute(sender, run_id, sent)):
        assert (res.status_code, res.json()["error"], res.json()["run_id"]) == (409, "SETTLEMENT_NOT_DRAFT", run_id)
    res = await _ack(sender, 999)
    assert (res.status_code, res.json()["error"]) == (404, "SETTLEMENT_NOT_FOUND")

    res = await client.post("/admin/settlements/run", json={"settle_date": str(_today())}, headers=ADMIN)
    empty = res.json()["run_id"]
    assert (await client.post(f"/admin/settlements/{empty}/cancel", headers=ADMIN)).status_code == 200
    assert (await _ack(sender, empty)).json()["error"] == "SETTLEMENT_NOT_DRAFT"


async def test_deadline_confirms_without_dispute(client, banks, monkeypatch, alert_sink):
    """마감 시각: 901만 ack했어도 dispute가 없으면 확정 (auto-deadline). DRAFT가 없으면 None."""
    sender, _ = banks
    async with SessionLocal() as db:
        assert await worker.run_deadline(db) is None
    run_id, _ = await _draft(client, banks, monkeypatch)
    await _ack(sender, run_id)

    async with SessionLocal() as db:
        run = await worker.run_deadline(db)
    assert (run.run_id, run.status, run.settled_by) == (run_id, SettlementStatus.SETTLED, "auto-deadline")
    assert [p["balance_after"] for p in (await _admin(client, run_id))["positions"]] == [-70_000, 70_000]
    texts = await sent_alerts(alert_sink, "auto_confirm")
    assert len(texts) == 1 and "마감 시각" in texts[0] and "ack 안 한 은행 902" in texts[0]


async def test_deadline_holds_disputed_run_then_cancel_keeps_acks(client, banks, monkeypatch, alert_sink):
    """마감 시각에 dispute가 있으면 확정하지 않고 알림만. 다음 배치는 건너뛰고 알림. 관리자가 취소하면 CANCELLED, ack 기록은 남는다."""
    sender, receiver = banks
    run_id, sent = await _draft(client, banks, monkeypatch)
    await _ack(sender, run_id)
    await _dispute(receiver, run_id, sent)

    async with SessionLocal() as db:
        run = await worker.run_deadline(db)
    assert (run.run_id, run.status) == (run_id, SettlementStatus.DRAFT)
    texts = await sent_alerts(alert_sink, "deadline_held")
    assert len(texts) == 1 and f"run {run_id}" in texts[0] and "902" in texts[0]

    async with SessionLocal() as db:
        assert await worker.run_daily(db) is None  # DRAFT가 남아 일일 배치를 건너뜀
    texts = await sent_alerts(alert_sink, "batch_skipped")
    assert len(texts) == 1 and f"run {run_id}" in texts[0]

    assert (await client.post(f"/admin/settlements/{run_id}/cancel", headers=ADMIN)).json()["status"] == "CANCELLED"
    detail = await _admin(client, run_id)
    assert detail["status"] == "CANCELLED" and detail["disputed"] is True
    assert [(a["bank_code"], a["result"]) for a in detail["acks"]] == [("901", "ACK"), ("902", "DISPUTE")]
    assert (await receiver.get(f"/v1/settlements/{run_id}")).json()["my_ack"] == "DISPUTE"


async def test_admin_can_force_confirm_disputed(client, banks, monkeypatch):
    """dispute가 있어도 관리자 확정은 된다 (판정)."""
    _, receiver = banks
    run_id, sent = await _draft(client, banks, monkeypatch)
    await _dispute(receiver, run_id, sent)
    res = await client.post(f"/admin/settlements/{run_id}/confirm", json={"reason": "증거 확인"}, headers=ADMIN)
    assert res.status_code == 200 and (res.json()["status"], res.json()["settled_by"], res.json()["disputed"]) == (
        "SETTLED", "admin", True)


def _kst(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=KST).astimezone(UTC)


def test_next_deadline(monkeypatch):
    assert worker.next_deadline(_kst(2026, 11, 7, 8, 59)) == _kst(2026, 11, 7, 9, 0)
    assert worker.next_deadline(_kst(2026, 11, 7, 9, 0)) == _kst(2026, 11, 8, 9, 0)  # 지금이면 내일
    monkeypatch.setattr(get_settings(), "settlement_confirm_deadline", "18:30")
    assert worker.next_deadline(_kst(2026, 11, 7, 9, 0)) == _kst(2026, 11, 7, 18, 30)


async def test_needs_deadline_catch_up(client, banks, monkeypatch):
    """오늘 마감 시각이 지났고 그 전에 만든 DRAFT가 있으면 참."""
    tomorrow = _today() + timedelta(days=1)
    after = datetime.combine(tomorrow, time(10, 0), tzinfo=KST).astimezone(UTC)
    before = datetime.combine(tomorrow, time(8, 0), tzinfo=KST).astimezone(UTC)
    async with SessionLocal() as db:
        assert await worker.needs_deadline_catch_up(db, after) is False  # DRAFT 없음
    await _draft(client, banks, monkeypatch)
    async with SessionLocal() as db:
        assert await worker.needs_deadline_catch_up(db, after) is True
        assert await worker.needs_deadline_catch_up(db, before) is False  # 아직 마감 전


async def test_settlement_loop_runs_deadline(monkeypatch):
    """마감 시각이 배치 시각보다 가까우면 마감 작업을 돈다. 실패해도 루프는 계속 돈다."""
    calls = []

    async def no(db, now):
        return False

    async def failing_deadline(db):
        calls.append(datetime.now(UTC))
        raise RuntimeError("마감 실패")

    monkeypatch.setattr(worker, "needs_catch_up", no)
    monkeypatch.setattr(worker, "needs_deadline_catch_up", no)
    monkeypatch.setattr(worker, "run_deadline", failing_deadline)
    monkeypatch.setattr(worker, "next_cutoff", lambda now: now + timedelta(days=1))
    monkeypatch.setattr(worker, "next_deadline", lambda now: now + timedelta(seconds=0.05))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(worker.settlement_loop(), timeout=0.4)
    assert len(calls) >= 3

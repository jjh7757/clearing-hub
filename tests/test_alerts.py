"""알림 웹훅 (core/alerts.py, SETTLEMENT_PLAN 13절).

웹훅은 httpx MockTransport로 흉내 낸다 (`alert_sink`). 알림은 fire-and-forget이라 `sent_alerts`가 보내는 중인 것을 기다린다.
dispute·자동 확정·배치 건너뜀·마감 보류는 test_settlement_ack.py, 셀프 시험·가짜 은행 알림은 test_self_test.py에서 본다.
"""

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

import bank_sim.main as sim
from clearing_hub.core import alerts
from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.core.signing import generate_keypair
from clearing_hub.domains.transfers.service import confirm_pending
from tests.conftest import ADMIN, send_902_to_901, sent_alerts, set_net_debit_cap, transfer_payload
from tests.test_onboarding import invite, self_register

pytestmark = pytest.mark.anyio


async def test_notify_posts_slack_and_discord_keys(alert_sink):
    task = alerts.notify("dispute", "정산 run 12: 은행 903 불일치", run_id=12, bank_code="903", none_is_dropped=None)
    assert isinstance(task, asyncio.Task)
    await task
    assert alert_sink == [{"text": "[ClearingHub·dispute] 정산 run 12: 은행 903 불일치\nrun_id=12 bank_code=903",
                           "content": "[ClearingHub·dispute] 정산 run 12: 은행 903 불일치\nrun_id=12 bank_code=903"}]


async def test_notify_without_url_does_nothing(monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_webhook_url", "")
    assert alerts.notify("unknown", "x") is None


def test_notify_outside_event_loop_does_nothing(alert_sink):
    assert alerts.notify("unknown", "x") is None and alert_sink == []


async def test_notify_never_raises_or_blocks(monkeypatch, caplog):
    """웹훅이 죽었거나(연결 실패·500) 느려도 호출한 쪽은 바로 돌아가고, 예외 없이 warning만 남는다."""
    monkeypatch.setattr(get_settings(), "alert_webhook_url", "http://alerts.test/hook")
    monkeypatch.setattr(alerts, "TIMEOUT_SECONDS", 0.1)

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    def broken(request):
        raise httpx.ConnectError("죽음")

    with caplog.at_level(logging.WARNING, logger=alerts.logger.name):
        for handler in (broken, lambda r: httpx.Response(500), slow):
            monkeypatch.setattr(alerts, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            started = time.monotonic()
            task = alerts.notify("overdue", "느린 웹훅")
            assert time.monotonic() - started < 0.05  # 기다리지 않는다
            await task  # 예외를 올리지 않는다
    failures = [r.getMessage() for r in caplog.records if r.name == alerts.logger.name]
    assert len(failures) == 3 and all("알림 웹훅 실패 (overdue)" in m for m in failures)


async def test_long_text_is_cut(alert_sink):
    await alerts.notify("unknown", "가" * 5000)
    assert len(alert_sink[0]["text"]) == alerts.MAX_TEXT


async def test_alert_on_self_register_and_ping(client, banks, monkeypatch, alert_sink):
    """셀프 등록(코드·이름·ping 결과), PATCH /v1/me의 ping 실패."""
    res = await self_register(client, await invite(client), "http://c-bank/interbank")
    assert res.status_code == 201
    other = await self_register(client, await invite(client), "http://d-bank/interbank", generate_keypair()[1], "DBank")
    texts = await sent_alerts(alert_sink, "register")
    assert len(texts) == 2
    assert "903 CBank" in texts[0] and "ping 정상" in texts[0]
    assert f"{other.json()['bank_code']} DBank" in texts[1] and "ping 실패" in texts[1]

    _, receiver = banks
    res = await receiver.patch("/v1/me", {"base_url": "http://sim/wrong-path"})
    assert res.json()["error"] == "PING_FAILED"
    texts = await sent_alerts(alert_sink, "ping_failed")
    assert len(texts) == 1 and "902" in texts[0] and "http://sim/wrong-path" in texts[0]


async def test_alert_on_overdue(client, banks, monkeypatch, alert_sink):
    """확정 결과 OVERDUE (901 예치금 0·한도 0, net −2만)."""
    sender, receiver = banks
    for _ in range(2):
        assert (await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=25_000))).status_code == 200
    await send_902_to_901(sender, receiver, monkeypatch, 30_000)
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    run = await client.post("/admin/settlements/run", json={"settle_date": str(today)}, headers=ADMIN)
    run_id = run.json()["run_id"]
    await set_net_debit_cap(client, "901", 0)
    res = await client.post(f"/admin/settlements/{run_id}/confirm", json={}, headers=ADMIN)
    assert [p["result"] for p in res.json()["positions"]] == ["OVERDUE", "OK"]
    texts = await sent_alerts(alert_sink, "overdue")
    assert len(texts) == 1 and f"run {run_id}" in texts[0] and "901" in texts[0] and "-20,000원" in texts[0]


async def test_alert_on_unknown(banks, alert_sink):
    """재시도를 다 해도 모르면 UNKNOWN 전이 알림 (RETRY_MAX_COUNT = 3)."""
    sender, _ = banks
    payload = transfer_payload("110-123-0003")
    await sender.post("/v1/transfers", payload)
    sim.received.clear()
    for _ in range(3):
        async with SessionLocal() as db:
            await confirm_pending(db)
    texts = await sent_alerts(alert_sink, "unknown")
    assert len(texts) == 1 and payload["transfer_id"] in texts[0] and "901 → 902" in texts[0]

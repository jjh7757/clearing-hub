"""관리자 정산 화면 (/admin/ui/settlements)과 화면이 읽는 관리자 API.

S1의 하루치(901→902 5만 2건, 902→901 3만 1건)로 DRAFT를 만들고, 목록·상세(포지션·쌍별 표·ack/dispute·미응답 은행·
확정 마감 시각)와 인증(토큰 없음·틀림 401), 없는 run 404, 페이지 서빙을 본다.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from clearing_hub.core.config import get_settings
from tests.conftest import ADMIN, register_bank, send_902_to_901, transfer_payload

pytestmark = pytest.mark.anyio
KST = ZoneInfo("Asia/Seoul")
WRONG = {"X-Admin-Token": "x" * 40}


async def _draft(client, banks, monkeypatch) -> tuple[int, list[str]]:
    sender, receiver = banks
    sent = []
    for _ in range(2):
        payload = transfer_payload("110-123-0001")
        assert (await sender.post("/v1/transfers", payload)).json()["status"] == "COMPLETED"
        sent.append(payload["transfer_id"])
    await send_902_to_901(sender, receiver, monkeypatch, 30_000)
    res = await client.post("/admin/settlements/run", json={"settle_date": str(datetime.now(KST).date())}, headers=ADMIN)
    assert res.status_code == 201, res.text
    return res.json()["run_id"], sent


@pytest.mark.parametrize("headers", [{}, WRONG])
@pytest.mark.parametrize("path", ["/admin/settlements", "/admin/settlements/1", "/admin/settlements/1/transfers",
                                  "/admin/banks"])
async def test_admin_read_apis_need_token(client, path, headers):
    res = await client.get(path, headers=headers)
    assert res.status_code == 401
    assert res.json()["error"] == "UNAUTHORIZED"


async def test_list_and_detail_for_screen(client, banks, monkeypatch):
    """목록 한 줄과 상세: 포지션(합 0), 쌍별 표, 901 ACK·902 DISPUTE(메시지·ID), 903 ack(포지션 없음), 미응답 은행 없음."""
    sender, receiver = banks
    c_bank = await register_bank(client, "903", "SpiderBank", base_url="http://c-bank/interbank")
    run_id, sent = await _draft(client, banks, monkeypatch)

    listed = (await client.get("/admin/settlements", headers=ADMIN)).json()
    assert listed[0]["run_id"] == run_id
    assert {k: listed[0][k] for k in ("status", "tx_count", "total_amount", "created_by", "settled_at", "settled_by")} == {
        "status": "DRAFT", "tx_count": 3, "total_amount": 130_000, "created_by": "admin", "settled_at": None,
        "settled_by": None}

    detail = (await client.get(f"/admin/settlements/{run_id}", headers=ADMIN)).json()
    assert detail["unanswered"] == ["901", "902"] and detail["acks"] == [] and detail["disputed"] is False

    assert (await sender.post(f"/v1/settlements/{run_id}/ack")).status_code == 200
    assert (await c_bank.post(f"/v1/settlements/{run_id}/ack")).status_code == 200
    body = {"transfer_ids": sent[:1], "message": "허브에만 있는 거래"}
    assert (await receiver.post(f"/v1/settlements/{run_id}/dispute", body)).status_code == 200

    detail = (await client.get(f"/admin/settlements/{run_id}", headers=ADMIN)).json()
    assert detail["status"] == "DRAFT" and detail["disputed"] is True and detail["unanswered"] == []
    assert [(p["bank_code"], p["sent_count"], p["sent_total"], p["recv_count"], p["recv_total"], p["net_amount"])
            for p in detail["positions"]] == [("901", 2, 100_000, 1, 30_000, -70_000), ("902", 1, 30_000, 2, 100_000, 70_000)]
    assert sum(p["net_amount"] for p in detail["positions"]) == 0
    assert detail["pairs"] == [{"bank_a": "901", "bank_b": "902", "a_to_b_count": 2, "a_to_b_total": 100_000,
                                "b_to_a_count": 1, "b_to_a_total": 30_000, "net_amount": 70_000,
                                "net_direction": "a_to_b"}]
    assert [(a["bank_code"], a["result"], a["transfer_ids"], a["message"]) for a in detail["acks"]] == [
        ("901", "ACK", [], None), ("902", "DISPUTE", sent[:1], "허브에만 있는 거래"), ("903", "ACK", [], None)]
    assert all(a["created_at"] and a["updated_at"] for a in detail["acks"])

    banks_list = (await client.get("/admin/banks", headers=ADMIN)).json()  # 화면이 은행 이름을 여기서 읽는다
    assert {b["bank_code"]: b["name"] for b in banks_list} == {"901": "AvengersBank", "902": "SimBank", "903": "SpiderBank"}


async def test_confirm_deadline_only_for_draft_with_worker(client, banks, monkeypatch):
    """DRAFT + 워커 켜짐이면 다음 마감 시각(설정한 HH:MM, KST), 워커가 꺼져 있거나 확정 뒤에는 null."""
    run_id, _ = await _draft(client, banks, monkeypatch)
    assert (await client.get(f"/admin/settlements/{run_id}", headers=ADMIN)).json()["confirm_deadline_at"] is None

    monkeypatch.setattr(get_settings(), "settlement_worker_enabled", True)
    monkeypatch.setattr(get_settings(), "settlement_confirm_deadline", "09:00")
    before = datetime.now(UTC)
    at = datetime.fromisoformat((await client.get(f"/admin/settlements/{run_id}", headers=ADMIN)).json()["confirm_deadline_at"])
    local = at.astimezone(KST)
    assert local.time() == time(9, 0) and before < at <= before + timedelta(days=1)

    res = await client.post(f"/admin/settlements/{run_id}/confirm", json={"reason": "화면 시험"}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["status"] == "SETTLED"
    assert res.json()["confirm_deadline_at"] is None and res.json()["settled_by"] == "admin"


async def test_unknown_run_is_404(client):
    for path in ("/admin/settlements/999", "/admin/settlements/999/transfers"):
        res = await client.get(path, headers=ADMIN)
        assert res.status_code == 404 and res.json()["error"] == "SETTLEMENT_NOT_FOUND"


async def test_page_is_served_without_token(client):
    """페이지·스크립트·스타일은 토큰 없이 200. 토큰은 들어 있지 않고, CSP가 붙는다."""
    page = await client.get("/admin/ui/settlements")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "정산 관리" in page.text and "/admin/ui/settlements.js" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert get_settings().admin_token.get_secret_value() not in page.text

    script = await client.get("/admin/ui/settlements.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert "sessionStorage" in script.text and "innerHTML" not in script.text
    assert get_settings().admin_token.get_secret_value() not in script.text
    assert (await client.get("/admin/ui/settlements.css")).status_code == 200

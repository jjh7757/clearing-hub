"""관리 API: 은행 정보 변경, UNKNOWN 거래 정리 (재확인·직접 확정)."""

import pytest

import bank_sim.main as sim
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.transfers.service import confirm_pending
from tests.conftest import ADMIN, transfer_payload

pytestmark = pytest.mark.anyio


async def _make_unknown(sender) -> str:
    """0003(지연)으로 보내고 RETRY_MAX_COUNT(3)번 확인해 UNKNOWN으로 만든다."""
    payload = transfer_payload("110-123-0003")
    await sender.post("/v1/transfers", payload)
    for _ in range(3):
        async with SessionLocal() as db:
            await confirm_pending(db)
    assert (await sender.get(f"/v1/transfers/{payload['transfer_id']}")).json()["status"] == "UNKNOWN"
    sim.received.clear()  # 지연 중에 커밋된 입금 기록은 없앤다: 은행이 "입금한 적 없음"인 상태로 시험한다
    return payload["transfer_id"]


# --- PATCH /admin/banks/{bank_code} ---

async def test_update_base_url_used_for_next_call(client, banks):
    sender, _ = banks
    res = await client.patch("/admin/banks/902", json={"base_url": "http://elsewhere.invalid/interbank/"}, headers=ADMIN)
    assert res.status_code == 200, res.text
    assert res.json()["base_url"] == "http://elsewhere.invalid/interbank"  # 끝 / 제거
    assert res.json()["name"] == "SimBank"  # 넣지 않은 필드는 그대로
    assert "secret" not in res.json() and "api_key" not in res.json()

    # 다음 호출부터 새 주소로 간다 (가짜 은행 클라이언트는 호스트를 무시하므로 경로로 확인)
    res = await client.patch("/admin/banks/902", json={"base_url": "http://sim/wrong-path"}, headers=ADMIN)
    ping = await client.post("/admin/banks/902/ping", headers=ADMIN)
    assert ping.json()["status"] == 404 and ping.json()["body"].get("error") != "ACCOUNT_NOT_FOUND"

    await client.patch("/admin/banks/902", json={"base_url": "http://sim/interbank"}, headers=ADMIN)
    ping = await client.post("/admin/banks/902/ping", headers=ADMIN)
    assert ping.json()["status"] == 200  # 정상 (가짜 은행은 ping 계좌에도 이름을 준다)


async def test_update_name(client, banks):
    res = await client.patch("/admin/banks/902", json={"name": "SimBank2"}, headers=ADMIN)
    assert res.json()["name"] == "SimBank2"
    assert res.json()["base_url"] == "http://sim/interbank"


async def test_update_bank_errors(client, banks):
    assert (await client.patch("/admin/banks/902", json={}, headers=ADMIN)).status_code == 422
    assert (await client.patch("/admin/banks/902", json={"base_url": "not-a-url"}, headers=ADMIN)).status_code == 422
    res = await client.patch("/admin/banks/999", json={"name": "X"}, headers=ADMIN)
    assert res.status_code == 404 and res.json()["error"] == "BANK_NOT_FOUND"
    assert (await client.patch("/admin/banks/902", json={"name": "X"})).status_code == 401  # 관리 토큰 없음


# --- POST /admin/transfers/{id}/recheck ---

async def test_recheck_after_receiver_recovers(banks, monkeypatch):
    sender, _ = banks
    tid = await _make_unknown(sender)
    monkeypatch.setenv("SIM_DELAY_SECONDS", "0")  # 받는 은행 장애가 풀림
    res = await sender.client.post(f"/admin/transfers/{tid}/recheck", headers=ADMIN)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "COMPLETED" and res.json()["retry_count"] == 0
    assert len(sim.received) == 1  # 입금은 한 번
    assert (await sender.get(f"/v1/transfers/{tid}")).json()["status"] == "COMPLETED"
    events = (await sender.client.get(f"/admin/transfers/{tid}/events", headers=ADMIN)).json()
    assert [e["to"] for e in events[-2:]] == ["PENDING", "COMPLETED"]


async def test_recheck_still_failing_goes_back_to_pending(banks):
    sender, _ = banks
    tid = await _make_unknown(sender)
    res = await sender.client.post(f"/admin/transfers/{tid}/recheck", headers=ADMIN)
    assert res.json()["status"] == "PENDING" and res.json()["retry_count"] == 1  # 재시도 태스크가 이어서 확인
    for _ in range(2):
        async with SessionLocal() as db:
            await confirm_pending(db)
    assert (await sender.get(f"/v1/transfers/{tid}")).json()["status"] == "UNKNOWN"  # 다시 횟수를 다 쓰면 UNKNOWN


# --- POST /admin/transfers/{id}/resolve ---

async def test_resolve_failed(banks):
    sender, _ = banks
    tid = await _make_unknown(sender)
    res = await sender.client.post(f"/admin/transfers/{tid}/resolve", headers=ADMIN,
                                   json={"status": "FAILED", "reason": "SimBank 담당자와 통화, 입금 기록 없음"})
    assert res.status_code == 200, res.text
    detail = (await sender.get(f"/v1/transfers/{tid}")).json()
    assert detail["status"] == "FAILED" and detail["fail_code"] == "RECEIVER_ERROR" and detail["finished_at"]
    assert "통화" not in detail["fail_message"]  # 관리자 사유는 보내는 은행에 보이지 않는다

    # 보내는 은행이 같은 ID로 재전송하면 3.3 형식의 FAILED (되돌리면 된다)
    resend = await sender.post("/v1/transfers", transfer_payload("110-123-0003", tid))
    assert resend.status_code == 400 and resend.json()["status"] == "FAILED"
    events = (await sender.client.get(f"/admin/transfers/{tid}/events", headers=ADMIN)).json()
    assert events[-1]["detail"].startswith("관리자 정리: SimBank 담당자와 통화")


async def test_resolve_completed_with_holder_name(banks):
    sender, _ = banks
    tid = await _make_unknown(sender)
    res = await sender.client.post(f"/admin/transfers/{tid}/resolve", headers=ADMIN,
                                   json={"status": "COMPLETED", "reason": "받는 은행 원장 확인", "holder_name": "김철수"})
    assert res.json()["status"] == "COMPLETED"
    detail = (await sender.get(f"/v1/transfers/{tid}")).json()
    assert detail["holder_name"] == "김철수" and detail["fail_code"] is None
    resend = await sender.post("/v1/transfers", transfer_payload("110-123-0003", tid))
    assert resend.status_code == 200 and resend.json()["status"] == "COMPLETED"


async def test_resolve_only_unknown(banks):
    sender, _ = banks
    done = transfer_payload("110-123-0001")
    await sender.post("/v1/transfers", done)
    pending = transfer_payload("110-123-0003")
    await sender.post("/v1/transfers", pending)
    for tid in (done["transfer_id"], pending["transfer_id"]):
        for path, body in (("resolve", {"status": "FAILED", "reason": "x"}), ("recheck", None)):
            res = await sender.client.post(f"/admin/transfers/{tid}/{path}", json=body, headers=ADMIN)
            assert res.status_code == 409 and res.json()["error"] == "TRANSFER_NOT_UNKNOWN"
    assert (await sender.get(f"/v1/transfers/{done['transfer_id']}")).json()["status"] == "COMPLETED"  # 바뀌지 않음

    res = await sender.client.post("/admin/transfers/00000000-0000-4000-8000-000000000000/resolve", headers=ADMIN,
                                   json={"status": "FAILED", "reason": "x"})
    assert res.status_code == 404


async def test_resolve_validation(banks):
    sender, _ = banks
    tid = await _make_unknown(sender)
    for body in ({"status": "PENDING", "reason": "x"}, {"status": "FAILED"}, {"status": "FAILED", "reason": ""}):
        assert (await sender.client.post(f"/admin/transfers/{tid}/resolve", json=body, headers=ADMIN)).status_code == 422
    assert (await sender.client.post(f"/admin/transfers/{tid}/resolve", json={"status": "FAILED", "reason": "x"})).status_code == 401
    assert (await sender.get(f"/v1/transfers/{tid}")).json()["status"] == "UNKNOWN"

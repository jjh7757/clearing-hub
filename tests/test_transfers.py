"""이체 중계: 성공·실패·멱등·타임아웃·재시도. 어느 경우에도 가짜 은행에 돈이 한 번만 들어가야 한다."""

import pytest

import bank_sim.main as sim
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.transfers.service import confirm_pending
from tests.conftest import ADMIN, transfer_payload

pytestmark = pytest.mark.anyio


async def test_inquiry(banks):
    sender, _ = banks
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})
    assert res.status_code == 200 and res.json()["holder_name"] == "김철수"
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0002"})
    assert res.status_code == 404 and res.json()["error"] == "ACCOUNT_NOT_FOUND"
    res = await sender.post("/v1/inquiry", {"to_bank": "999", "to_account": "110-123-0001"})
    assert res.json()["error"] == "BANK_NOT_FOUND"


async def test_inquiry_receiver_failure_codes(banks, monkeypatch):
    """받는 은행 5xx는 502 RECEIVER_ERROR, 응답 없음은 504 RECEIVER_TIMEOUT (은행의 오류 코드를 넘기지 않는다)."""
    from clearing_hub.core import bank_client as bank_client_module
    from clearing_hub.core.bank_client import BankReply

    sender, _ = banks
    replies = iter([BankReply(500, {"error": "INTERNAL_ERROR"}), BankReply(None, {})])

    async def failing(self, bank, account_num, request_id=None):
        return next(replies)

    monkeypatch.setattr(bank_client_module.BankClient, "inquiry", failing)
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})
    assert res.status_code == 502 and res.json()["error"] == "RECEIVER_ERROR"
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})
    assert res.status_code == 504 and res.json()["error"] == "RECEIVER_TIMEOUT"


async def test_transfer_completed(banks):
    sender, _ = banks
    payload = transfer_payload("110-123-0001")
    res = await sender.post("/v1/transfers", payload)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "COMPLETED" and res.json()["holder_name"] == "김철수"
    assert payload["transfer_id"] in sim.received

    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["status"] == "COMPLETED" and detail.json()["amount"] == 50000

    events = await sender.client.get(f"/admin/transfers/{payload['transfer_id']}/events", headers=ADMIN)
    assert [e["to"] for e in events.json()] == ["ACCEPTED", "DELIVERING", "COMPLETED"]


async def test_transfer_failed_account_not_found(banks):
    sender, _ = banks
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0002"))
    assert res.status_code == 404
    assert res.json()["error"] == "ACCOUNT_NOT_FOUND" and res.json()["status"] == "FAILED"
    assert sim.received == {}


async def test_idempotent_resend_returns_same_result(banks):
    """통신이 끊겨 같은 transfer_id로 다시 보내도 입금은 한 번, 응답은 같다."""
    sender, _ = banks
    payload = transfer_payload("110-123-0001")
    first = await sender.post("/v1/transfers", payload)
    second = await sender.post("/v1/transfers", {**payload, "amount": 99999})  # 내용이 달라도 저장된 결과
    assert first.status_code == second.status_code == 200
    assert first.json()["transfer_id"] == second.json()["transfer_id"]
    assert len(sim.received) == 1 and sim.received[payload["transfer_id"]]["amount"] == 50000


async def test_other_bank_cannot_see_transfer(client, banks):
    sender, receiver = banks
    payload = transfer_payload("110-123-0001")
    await sender.post("/v1/transfers", payload)
    res = await receiver.get(f"/v1/transfers/{payload['transfer_id']}")
    assert res.status_code == 403
    # 다른 은행이 같은 ID로 보내도 가로채지 못한다
    res = await receiver.post("/v1/transfers", {**payload, "to_bank": "901"})
    assert res.status_code == 403


async def test_validation(banks):
    sender, _ = banks
    res = await sender.post("/v1/transfers", {**transfer_payload("110-123-0001"), "amount": 0})
    assert res.status_code == 422 and res.json()["error"] == "VALIDATION_ERROR"
    res = await sender.post("/v1/transfers", {**transfer_payload("110-123-0001"), "transfer_id": "not-a-uuid"})
    assert res.status_code == 422
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=200_000_000))
    assert res.json()["error"] == "LIMIT_EXCEEDED"
    res = await sender.post("/v1/transfers", {**transfer_payload("110-123-0001"), "to_bank": "901"})
    assert res.status_code == 400 and res.json()["error"] == "SAME_BANK"  # 같은 은행 안의 이체


async def test_timeout_then_retry_delivers_once(banks):
    """0003: 은행이 입금 전에 지연 → PENDING → 재시도에서 확인(404) → 다시 보내 입금. 돈은 한 번."""
    sender, _ = banks
    payload = transfer_payload("110-123-0003")
    res = await sender.post("/v1/transfers", payload)
    assert res.status_code == 202 and res.json()["status"] == "PENDING"

    sim.received.clear()  # 지연 중이던 첫 요청이 늦게 입금해도 (실제로는 은행 멱등 처리) 테스트는 재전달만 본다
    import os

    os.environ["SIM_DELAY_SECONDS"] = "0"  # 재시도 때는 은행이 정상 응답
    try:
        async with SessionLocal() as db:
            assert await confirm_pending(db) == 1
    finally:
        os.environ["SIM_DELAY_SECONDS"] = "1"
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["status"] == "COMPLETED"
    assert len(sim.received) == 1


async def test_received_but_no_answer_confirmed_by_status(banks):
    """0005: 은행이 입금은 했는데 응답이 늦음 → PENDING → 결과 조회로 COMPLETED. 다시 보내지 않는다."""
    sender, _ = banks
    payload = transfer_payload("110-123-0005")
    res = await sender.post("/v1/transfers", payload)
    assert res.status_code == 202
    assert payload["transfer_id"] in sim.received  # 은행 쪽에는 이미 입금됨

    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["status"] == "COMPLETED"
    assert detail.json()["holder_name"] == "김철수"  # 입금 확인(4.3) 응답의 이름
    assert len(sim.received) == 1


async def test_confirmed_without_holder_name_fills_by_inquiry(banks, monkeypatch):
    """입금 확인(4.3) 응답에 holder_name이 없는 은행이면 수취 조회(4.1)로 채운다."""
    from clearing_hub.core import bank_client as bank_client_module

    sender, _ = banks
    payload = transfer_payload("110-123-0005")
    await sender.post("/v1/transfers", payload)

    original = bank_client_module.BankClient.receive_status

    async def without_name(self, bank, transfer_id, request_id=None):
        reply = await original(self, bank, transfer_id, request_id)
        reply.body.pop("holder_name", None)
        return reply

    monkeypatch.setattr(bank_client_module.BankClient, "receive_status", without_name)
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["status"] == "COMPLETED" and detail.json()["holder_name"] == "김철수"


async def test_server_error_then_success(banks):
    """0004: 은행 500 두 번 → PENDING → 재시도마다 다시 보내 세 번째에 성공."""
    sender, _ = banks
    payload = transfer_payload("110-123-0004")
    res = await sender.post("/v1/transfers", payload)
    assert res.status_code == 202
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 0  # 두 번째 500
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1  # 성공
    assert len(sim.received) == 1


async def test_unknown_after_max_retries(banks, monkeypatch):
    """받는 은행이 계속 응답이 없으면 RETRY_MAX_COUNT 뒤 UNKNOWN (관리자 확인)."""
    sender, _ = banks
    payload = transfer_payload("110-123-0003")
    await sender.post("/v1/transfers", payload)
    sim.received.clear()
    for _ in range(3):  # RETRY_MAX_COUNT = 3. 0003은 재전달도 지연되어 계속 응답 없음
        async with SessionLocal() as db:
            await confirm_pending(db)
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["status"] == "UNKNOWN"
    listed = await sender.client.get("/admin/transfers", params={"status": "UNKNOWN"}, headers=ADMIN)
    assert listed.json()[0]["to_account"] == "****0003"

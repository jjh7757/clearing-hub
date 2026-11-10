"""예치금 입금·인출 신청 (SPEC 10.9): 은행이 신청하고 허브 관리자가 승인·거절한다."""

import pytest

from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.reserve.models import ReserveAccount
from tests.conftest import ADMIN, send_902_to_901, sent_alerts, set_net_debit_cap, transfer_payload

pytestmark = pytest.mark.anyio


@pytest.fixture
def net_debit_cap() -> int:
    return 0


def _req(ref: str = "dep-1", kind: str = "DEPOSIT", amount: int = 1_000_000, memo: str = "") -> dict:
    return {"request_ref": ref, "kind": kind, "amount": amount, "memo": memo}


async def _balance(bank_code: str) -> int:
    async with SessionLocal() as db:
        return (await db.get(ReserveAccount, bank_code)).balance


async def _approve(client, request_id: int, **body):
    return await client.post(f"/admin/reserve/requests/{request_id}/approve", json=body or None, headers=ADMIN)


async def _deposit_approved(client, bank, amount: int, ref: str = "seed") -> None:
    res = await bank.post("/v1/reserve/requests", _req(ref, amount=amount))
    assert res.status_code == 201, res.text
    assert (await _approve(client, res.json()["request_id"])).status_code == 200


# --- 입금 신청 ---

async def test_deposit_request_then_approve(client, banks, alert_sink):
    sender, _ = banks
    res = await sender.post("/v1/reserve/requests", _req(memo="3월 예치"))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "PENDING" and body["kind"] == "DEPOSIT" and body["amount"] == 1_000_000
    assert body["entry_id"] is None and body["balance_after"] is None and body["decided_at"] is None
    assert await _balance("901") == 0  # 신청만으로는 잔액이 바뀌지 않는다
    alerts_sent = await sent_alerts(alert_sink, "reserve_request")
    assert len(alerts_sent) == 1 and "901" in alerts_sent[0] and "1,000,000원" in alerts_sent[0]

    pending = (await client.get("/admin/reserve/requests?status=PENDING", headers=ADMIN)).json()
    assert [(r["request_id"], r["bank_code"]) for r in pending] == [(body["request_id"], "901")]

    res = await _approve(client, body["request_id"], memo="입금 확인")
    assert res.status_code == 200, res.text
    approved = res.json()
    assert approved["status"] == "APPROVED" and approved["decided_by"] == "admin"
    assert approved["entry_id"] is not None and approved["balance_after"] == 1_000_000
    assert await _balance("901") == 1_000_000

    ledger = (await client.get("/admin/reserve/901/ledger", headers=ADMIN)).json()
    assert ledger[0]["kind"] == "DEPOSIT" and ledger[0]["entry_id"] == approved["entry_id"]
    assert ledger[0]["memo"] == f"신청 #{body['request_id']} (dep-1) 3월 예치 / 입금 확인"

    mine = (await sender.get(f"/v1/reserve/requests/{body['request_id']}")).json()
    assert mine["status"] == "APPROVED" and mine["balance_after"] == 1_000_000
    assert (await sender.get("/v1/reserve")).json()["balance"] == 1_000_000
    assert (await client.get("/admin/reserve/requests?status=PENDING", headers=ADMIN)).json() == []


async def test_approve_twice_and_reject_after_approve(client, banks):
    sender, _ = banks
    rid = (await sender.post("/v1/reserve/requests", _req())).json()["request_id"]
    assert (await _approve(client, rid)).status_code == 200
    res = await _approve(client, rid)
    assert res.status_code == 409 and res.json()["error"] == "RESERVE_REQUEST_NOT_PENDING"
    assert res.json()["status"] == "APPROVED"
    res = await client.post(f"/admin/reserve/requests/{rid}/reject", json={"reason": "x"}, headers=ADMIN)
    assert res.status_code == 409
    assert await _balance("901") == 1_000_000  # 한 번만 반영


async def test_reject(client, banks):
    sender, _ = banks
    rid = (await sender.post("/v1/reserve/requests", _req())).json()["request_id"]
    res = await client.post(f"/admin/reserve/requests/{rid}/reject", json={"reason": "  입금 내역이 없습니다 "},
                            headers=ADMIN)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "REJECTED" and res.json()["reject_reason"] == "입금 내역이 없습니다"
    mine = (await sender.get(f"/v1/reserve/requests/{rid}")).json()
    assert mine["status"] == "REJECTED" and mine["reject_reason"] == "입금 내역이 없습니다"
    assert await _balance("901") == 0
    for body in ({}, {"reason": ""}, {"reason": "   "}):
        res = await client.post(f"/admin/reserve/requests/{rid}/reject", json=body, headers=ADMIN)
        assert res.status_code == 422, body


async def test_cancel(client, banks):
    sender, receiver = banks
    rid = (await sender.post("/v1/reserve/requests", _req())).json()["request_id"]
    res = await receiver.post(f"/v1/reserve/requests/{rid}/cancel")  # 다른 은행은 못 본다
    assert res.status_code == 404 and res.json()["error"] == "RESERVE_REQUEST_NOT_FOUND"
    res = await sender.post(f"/v1/reserve/requests/{rid}/cancel")
    assert res.status_code == 200 and res.json()["status"] == "CANCELLED"
    res = await sender.post(f"/v1/reserve/requests/{rid}/cancel")
    assert res.status_code == 409 and res.json()["error"] == "RESERVE_REQUEST_NOT_PENDING"
    res = await _approve(client, rid)
    assert res.status_code == 409 and await _balance("901") == 0
    admin = (await client.get("/admin/reserve/requests?bank_code=901", headers=ADMIN)).json()
    assert admin[0]["status"] == "CANCELLED" and admin[0]["decided_by"] == "bank"


async def test_idempotent_request_ref(client, banks):
    sender, receiver = banks
    first = await sender.post("/v1/reserve/requests", _req("same", memo="m"))
    again = await sender.post("/v1/reserve/requests", _req("same", memo="m"))
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["request_id"] == first.json()["request_id"]
    res = await sender.post("/v1/reserve/requests", _req("same", amount=2_000_000, memo="m"))
    assert res.status_code == 409 and res.json()["error"] == "REQUEST_REF_CONFLICT"
    assert res.json()["request_id"] == first.json()["request_id"]
    # 다른 은행은 같은 request_ref를 따로 쓴다
    assert (await receiver.post("/v1/reserve/requests", _req("same", memo="m"))).status_code == 201
    assert len((await sender.get("/v1/reserve/requests")).json()["requests"]) == 1


async def test_bank_sees_only_own_requests(client, banks):
    sender, receiver = banks
    rid = (await sender.post("/v1/reserve/requests", _req())).json()["request_id"]
    await receiver.post("/v1/reserve/requests", _req("r2", amount=5))
    res = await receiver.get(f"/v1/reserve/requests/{rid}")
    assert res.status_code == 404 and res.json()["error"] == "RESERVE_REQUEST_NOT_FOUND"
    listed = (await receiver.get("/v1/reserve/requests")).json()["requests"]
    assert [r["request_ref"] for r in listed] == ["r2"]
    assert (await client.get("/v1/reserve/requests")).status_code == 401
    assert (await client.get("/admin/reserve/requests")).status_code == 401
    assert (await _approve(client, 999)).status_code == 404


async def test_validation(client, banks):
    sender, _ = banks
    for body in (_req(amount=0), _req(amount=-5), _req(kind="ADJUST"), _req(ref=""), _req(ref="한글"),
                 _req(ref="x" * 65), _req(amount=10**12 + 1), _req(memo="m" * 201)):
        res = await sender.post("/v1/reserve/requests", body)
        assert res.status_code == 422 and res.json()["error"] == "VALIDATION_ERROR", body


async def test_too_many_pending(client, banks):
    sender, _ = banks
    for i in range(5):
        assert (await sender.post("/v1/reserve/requests", _req(f"p{i}"))).status_code == 201
    res = await sender.post("/v1/reserve/requests", _req("p5"))
    assert res.status_code == 409 and res.json()["error"] == "TOO_MANY_PENDING_REQUESTS"
    await sender.post(f"/v1/reserve/requests/{(await sender.get('/v1/reserve/requests')).json()['requests'][0]['request_id']}/cancel")
    assert (await sender.post("/v1/reserve/requests", _req("p5"))).status_code == 201


# --- 인출 신청 ---

async def test_withdraw_request_approve(client, banks):
    sender, _ = banks
    await _deposit_approved(client, sender, 1_000_000)
    res = await sender.post("/v1/reserve/requests", _req("w1", kind="WITHDRAW", amount=300_000))
    assert res.status_code == 201, res.text
    res = await _approve(client, res.json()["request_id"])
    assert res.status_code == 200, res.text
    assert res.json()["balance_after"] == 700_000 and await _balance("901") == 700_000
    ledger = (await client.get("/admin/reserve/901/ledger", headers=ADMIN)).json()
    assert ledger[0]["kind"] == "WITHDRAW" and ledger[0]["amount"] == -300_000


async def test_withdraw_cannot_use_net_debit_cap(client, banks):
    """은행 신청으로는 잔액까지만 뺀다. 순채무한도(빌린 돈)는 쓰지 못한다."""
    sender, _ = banks
    await set_net_debit_cap(client, "901", 5_000_000)
    await _deposit_approved(client, sender, 100_000)
    res = await sender.post("/v1/reserve/requests", _req("w1", kind="WITHDRAW", amount=100_001))
    assert res.status_code == 400 and res.json()["error"] == "INSUFFICIENT_RESERVE"
    assert "100000원" in res.json()["message"]
    # 대기 중인 인출 신청도 뺀 만큼만 더 신청할 수 있다
    assert (await sender.post("/v1/reserve/requests", _req("w2", kind="WITHDRAW", amount=60_000))).status_code == 201
    res = await sender.post("/v1/reserve/requests", _req("w3", kind="WITHDRAW", amount=40_001))
    assert res.status_code == 400 and res.json()["error"] == "INSUFFICIENT_RESERVE"
    assert (await sender.post("/v1/reserve/requests", _req("w4", kind="WITHDRAW", amount=40_000))).status_code == 201


async def test_withdraw_approve_rechecks_exposure(client, banks):
    """신청 뒤 송금으로 미정산 순채무가 생기면 승인 때 다시 재서 거절하고, 신청은 PENDING으로 남는다."""
    sender, _ = banks
    await set_net_debit_cap(client, "901", 0)
    await _deposit_approved(client, sender, 100_000)
    rid = (await sender.post("/v1/reserve/requests", _req("w1", kind="WITHDRAW", amount=80_000))).json()["request_id"]
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=50_000))
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text

    res = await _approve(client, rid)
    assert res.status_code == 400 and res.json()["error"] == "INSUFFICIENT_RESERVE"
    assert "50000원" in res.json()["message"]
    mine = (await sender.get(f"/v1/reserve/requests/{rid}")).json()
    assert mine["status"] == "PENDING" and await _balance("901") == 100_000


async def test_received_money_does_not_raise_withdrawable(client, banks, monkeypatch):
    """미정산으로 받은 돈(순채무 음수)은 정산 전에는 인출 가능액에 넣지 않는다."""
    sender, receiver = banks
    await send_902_to_901(sender, receiver, monkeypatch, 70_000)
    res = await sender.post("/v1/reserve/requests", _req("w1", kind="WITHDRAW", amount=1))
    assert res.status_code == 400 and res.json()["error"] == "INSUFFICIENT_RESERVE"


async def test_admin_list_order_and_filters(client, banks):
    sender, receiver = banks
    a = (await sender.post("/v1/reserve/requests", _req("a"))).json()["request_id"]
    b = (await receiver.post("/v1/reserve/requests", _req("b"))).json()["request_id"]
    c = (await sender.post("/v1/reserve/requests", _req("c"))).json()["request_id"]
    await _approve(client, b)
    pending = (await client.get("/admin/reserve/requests?status=PENDING", headers=ADMIN)).json()
    assert [r["request_id"] for r in pending] == [a, c]  # 대기는 오래된 것부터
    everything = (await client.get("/admin/reserve/requests", headers=ADMIN)).json()
    assert [r["request_id"] for r in everything] == [c, b, a]  # 나머지는 최신순
    only_902 = (await client.get("/admin/reserve/requests?bank_code=902", headers=ADMIN)).json()
    assert [r["request_id"] for r in only_902] == [b]
    assert (await client.get("/admin/reserve/requests?status=NOPE", headers=ADMIN)).status_code == 422


async def test_admin_page_is_served_without_token(client):
    page = await client.get("/admin/ui/reserve-requests")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "예치금 신청" in page.text and "/admin/ui/reserve-requests.js" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    script = await client.get("/admin/ui/reserve-requests.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    assert "innerHTML" not in script.text  # 은행이 보낸 문자열은 textContent로만 그린다


async def test_times_are_spec_format(client, banks):
    """SPEC 1절: 초 단위 UTC, Z."""
    sender, _ = banks
    rid = (await sender.post("/v1/reserve/requests", _req())).json()["request_id"]
    await _approve(client, rid)
    body = (await sender.get(f"/v1/reserve/requests/{rid}")).json()
    import re
    for key in ("created_at", "decided_at"):
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body[key]), body[key]

"""은행 셀프 시험: 받는 쪽 (SETTLEMENT_PLAN 13절, SPEC 3.7). 허브가 가짜 은행 902 명의로 시험 은행에 이체를 만든다.

시험하는 은행은 901이다. 901의 base_url도 가짜 은행(bank_sim)이라, 이 파일에서는 bank_sim이 901의 키로 응답에 서명하게
바꾼다 (허브는 901의 응답 서명을 901 공개키로 검증한다). 그래서 `sim.received`가 곧 "901 서버의 입금 장부"다.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import bank_sim.main as sim
from clearing_hub.core.config import get_settings
from tests.conftest import ADMIN, sent_alerts, set_net_debit_cap, transfer_payload

pytestmark = pytest.mark.anyio
KST = ZoneInfo("Asia/Seoul")
REPLY_KEYS = {"reachable", "status", "body", "signature_valid"}


@pytest.fixture
def tester(banks, monkeypatch):
    sender, _ = banks
    monkeypatch.setenv("SIM_SIGNING_KEY", sender.private_key)
    return sender


async def _test(bank, to_account: str = "110-123-0001", amount: int = 1000):
    return await bank.post("/v1/test-receive", {"to_account": to_account, "amount": amount})


async def test_self_test_success(client, tester):
    """정상 계좌 → 200 COMPLETED(3.3 형식) + receiver_reply. 902 명의, 시험 계좌·보낸 사람·메모, 입금 1번."""
    res = await _test(tester)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"transfer_id", "status", "holder_name", "finished_at", "receiver_reply"}
    assert (body["status"], body["holder_name"]) == ("COMPLETED", "김철수")
    assert set(body["receiver_reply"]) == REPLY_KEYS
    assert (body["receiver_reply"]["status"], body["receiver_reply"]["signature_valid"]) == (200, True)
    assert body["receiver_reply"]["body"]["status"] == "COMPLETED"

    record = sim.received[body["transfer_id"]]
    assert (record["from_bank"], record["from_bank_name"], record["from_account"], record["sender_name"], record["memo"],
            record["amount"]) == ("902", "SimBank", "902-TEST-0001", "ClearingHub 시험", "셀프 시험", 1000)
    listed = (await client.get("/admin/transfers", headers=ADMIN)).json()
    assert [(t["transfer_id"], t["from_bank"], t["to_bank"]) for t in listed] == [(body["transfer_id"], "902", "901")]


async def test_self_test_rejected(tester):
    """…0002 → 받는 은행의 404 ACCOUNT_NOT_FOUND를 3.3대로 FAILED로. receiver_reply에 은행이 준 404."""
    res = await _test(tester, "110-123-0002")
    assert res.status_code == 404, res.text
    body = res.json()
    assert (body["error"], body["status"]) == ("ACCOUNT_NOT_FOUND", "FAILED") and body["transfer_id"]
    assert (body["receiver_reply"]["status"], body["receiver_reply"]["body"]["error"]) == (404, "ACCOUNT_NOT_FOUND")


async def test_resend_is_idempotent(client, tester):
    """resend: 같은 transfer_id를 한 번 더 보내도 입금은 1번, 은행은 다시 200. 허브 상태는 그대로, 증거는 남는다."""
    transfer_id = (await _test(tester)).json()["transfer_id"]
    before = len((await client.get(f"/admin/transfers/{transfer_id}/evidence", headers=ADMIN)).json())

    res = await tester.post(f"/v1/test-receive/{transfer_id}/resend")
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["transfer_id"], body["hub_status"]) == (transfer_id, "COMPLETED")
    assert (body["receiver_reply"]["status"], body["receiver_reply"]["signature_valid"]) == (200, True)
    assert [k for k in sim.received] == [transfer_id]  # 가짜 은행의 입금 기록은 1건

    evidence = (await client.get(f"/admin/transfers/{transfer_id}/evidence", headers=ADMIN)).json()
    assert [e["direction"] for e in evidence[before:]] == ["RECEIVE_OUT", "RECEIVE_REPLY"]
    events = (await client.get(f"/admin/transfers/{transfer_id}/events", headers=ADMIN)).json()
    assert events[-1]["to"] == "COMPLETED"  # 상태 머신은 건드리지 않았다


async def test_resend_and_status_only_my_self_tests(tester, banks):
    """남의(보통) 거래 → 403, 없는 ID → 404, 거절된(FAILED) 셀프 시험의 resend → 409 TRANSFER_NOT_COMPLETED."""
    sender, _ = banks
    normal = transfer_payload("110-123-0001")
    # 보통 거래 901 → 902 (이 파일에서는 가짜 은행이 901 키로 서명하므로 902의 응답 서명이 틀려 202 PENDING이 된다)
    assert (await sender.post("/v1/transfers", normal)).status_code == 202
    res = await tester.post(f"/v1/test-receive/{normal['transfer_id']}/resend")
    assert (res.status_code, res.json()["error"]) == (403, "FORBIDDEN")
    res = await tester.get(f"/v1/test-receive/{normal['transfer_id']}/status")
    assert (res.status_code, res.json()["error"]) == (403, "FORBIDDEN")
    res = await tester.post("/v1/test-receive/00000000-0000-4000-8000-000000000000/resend")
    assert (res.status_code, res.json()["error"]) == (404, "TRANSFER_NOT_FOUND")

    failed = (await _test(tester, "110-123-0002")).json()["transfer_id"]
    res = await tester.post(f"/v1/test-receive/{failed}/resend")
    assert (res.status_code, res.json()["error"]) == (409, "TRANSFER_NOT_COMPLETED")


async def test_status_calls_receive_status(tester):
    """status: 허브가 4.3을 불러 결과를 그대로 보여 준다. 입금한 거래는 200 COMPLETED, 거절한 거래는 404."""
    done = (await _test(tester)).json()["transfer_id"]
    res = await tester.get(f"/v1/test-receive/{done}/status")
    assert res.status_code == 200, res.text
    reply = res.json()["receiver_reply"]
    assert (reply["status"], reply["body"]["status"], reply["signature_valid"]) == (200, "COMPLETED", True)

    failed = (await _test(tester, "110-123-0002")).json()["transfer_id"]
    reply = (await tester.get(f"/v1/test-receive/{failed}/status")).json()["receiver_reply"]
    assert (reply["status"], reply["body"]["error"]) == (404, "TRANSFER_NOT_FOUND")


async def test_daily_limit(tester, monkeypatch):
    monkeypatch.setattr(get_settings(), "self_test_daily_limit", 2)
    assert (await _test(tester)).status_code == 200
    assert (await _test(tester, "110-123-0002")).status_code == 404  # 거절도 센다
    res = await _test(tester)
    assert (res.status_code, res.json()["error"]) == (429, "TOO_MANY_TESTS")


async def test_validation(tester):
    for bad in ({"to_account": "110-123-0001", "amount": 0}, {"to_account": "110-123-0001", "amount": 100_001},
                {"to_account": "110-123-0001", "amount": "1000"}, {"amount": 1000}):
        res = await tester.post("/v1/test-receive", bad)
        assert (res.status_code, res.json()["error"]) == (422, "VALIDATION_ERROR"), bad
    assert (await _test(tester, amount=100_000)).status_code == 200


async def test_sim_bank_unavailable(client, tester, monkeypatch, alert_sink):
    """902가 비활성이거나 없으면 503 SIM_BANK_UNAVAILABLE. 902 한도가 모자라도 은행 잘못이 아니라 503 (+ 알림)."""
    res = await client.patch("/admin/banks/902/active", params={"active": "false"}, headers=ADMIN)
    assert res.status_code == 200
    res = await _test(tester)
    assert (res.status_code, res.json()["error"]) == (503, "SIM_BANK_UNAVAILABLE")
    await client.patch("/admin/banks/902/active", params={"active": "true"}, headers=ADMIN)

    monkeypatch.setattr(get_settings(), "sim_bank_code", "999")
    assert (await _test(tester)).json()["error"] == "SIM_BANK_UNAVAILABLE"
    monkeypatch.setattr(get_settings(), "sim_bank_code", "902")

    await set_net_debit_cap(client, "902", 0)
    res = await _test(tester)
    assert (res.status_code, res.json()["error"]) == (503, "SIM_BANK_UNAVAILABLE")
    assert (await client.get("/admin/transfers", headers=ADMIN)).json() == []  # 거래가 남지 않는다
    texts = await sent_alerts(alert_sink, "sim_bank")
    assert len(texts) == 3 and "순채무한도" in texts[-1]


async def test_server_error_alerts(tester, alert_sink):
    """…0004: 은행이 5xx → 202 PENDING(재시도는 보통 거래처럼), receiver_reply 500, 관리자 알림."""
    res = await _test(tester, "110-123-0004")
    assert res.status_code == 202 and res.json()["status"] == "PENDING"
    assert (res.json()["receiver_reply"]["status"], res.json()["receiver_reply"]["signature_valid"]) == (500, None)
    texts = await sent_alerts(alert_sink, "self_test")
    assert len(texts) == 1 and "901" in texts[0] and "HTTP 500" in texts[0] and res.json()["transfer_id"] in texts[0]


async def test_bad_response_signature_alerts(tester, monkeypatch, alert_sink):
    """은행이 다른 키로 응답에 서명 → 허브는 응답 없음(PENDING)으로 보고, receiver_reply에 받은 200과 signature_valid false."""
    from clearing_hub.core.signing import generate_keypair

    monkeypatch.setenv("SIM_SIGNING_KEY", generate_keypair()[0])
    res = await _test(tester)
    assert res.status_code == 202 and res.json()["status"] == "PENDING"
    reply = res.json()["receiver_reply"]
    assert (reply["status"], reply["signature_valid"], reply["body"]["status"]) == (200, False, "COMPLETED")
    texts = await sent_alerts(alert_sink, "self_test")
    assert len(texts) == 1 and "응답 서명 검증 실패" in texts[0]


async def test_self_test_goes_into_settlement(client, tester):
    """셀프 시험 거래도 보통 거래처럼 정산에 들어간다 (902 → 901 RECEIVED)."""
    transfer_id = (await _test(tester, amount=5000)).json()["transfer_id"]
    today: date = datetime.now(KST).date()
    run_id = (await client.post("/admin/settlements/run", json={"settle_date": str(today)}, headers=ADMIN)).json()["run_id"]
    detail = (await tester.get(f"/v1/settlements/{run_id}")).json()
    assert [(t["transfer_id"], t["direction"], t["counterparty_bank"], t["amount"]) for t in detail["transfers"]] == [
        (transfer_id, "RECEIVED", "902", 5000)]
    assert detail["position"]["net_amount"] == 5000

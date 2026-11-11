"""SimBank(902) 보내기 (SIMBANK_PLAN 2단계): 허브 앱과 bank_sim을 ASGI로 붙여 902 고객 → 901로 보낸다.

허브는 901의 base_url도 bank_sim으로 부른다. 그래서 901을 902와 같은 SIM 키 쌍으로 등록해, bank_sim의 /interbank 응답
서명(SIM 키)이 901 공개키로도 맞게 한다. 901로 간 돈은 bank_sim 장부에 고객 아닌 계좌의 IN으로만 남는다 (잔액 없음).
허브 결과를 마음대로 정해야 하는 경우(타임아웃·서명 틀림·3.4 FAILED)는 httpx MockTransport로 만든 가짜 허브를 쓴다.
"""

import json
import os
from dataclasses import dataclass, field

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import bank_sim.main as sim
from clearing_hub.core.database import SessionLocal
from clearing_hub.core.signing import generate_keypair, sign_response
from clearing_hub.domains.transfers.models import Transfer
from clearing_hub.domains.transfers.self_test import is_self_test
from clearing_hub.domains.transfers.service import confirm_pending
from clearing_hub.main import app as hub_app
from tests.conftest import HUB_PRIVATE_KEY, SIM_PRIVATE_KEY, register_bank, set_net_debit_cap

pytestmark = pytest.mark.anyio

PASSWORD = "demo-password-1234"
FROM = "110-123-0001"  # 김철수 자유입출금 (잔액 1,000,000으로 시작)
TO_OK = "333-901-0001"  # 901(=bank_sim) 쪽 정상 입금, 고객 계좌가 아니라 기록만 남는다


@dataclass
class Env:
    api: AsyncClient  # bank_sim /sim/api (비밀번호 헤더 포함)
    hub_calls: list[str] = field(default_factory=list)  # SimBank가 허브에 보낸 요청 "METHOD /path"


@pytest.fixture
async def env(client, monkeypatch):
    await register_bank(client, "901", "AvengersBank", private_key=SIM_PRIVATE_KEY)
    b902 = await register_bank(client, "902", "SimBank", private_key=SIM_PRIVATE_KEY)
    calls: list[str] = []

    async def record(request: httpx.Request) -> None:
        calls.append(f"{request.method} {request.url.path}")

    hub_http = AsyncClient(transport=ASGITransport(app=hub_app), base_url="http://hub", event_hooks={"request": [record]})
    monkeypatch.setattr(sim.hub, "client", hub_http)
    monkeypatch.setenv("SIM_API_KEY", b902.api_key)
    monkeypatch.setenv("SIM_UI_PASSWORD", PASSWORD)
    async with AsyncClient(transport=ASGITransport(app=sim.app), base_url="http://simbank",
                           headers={"X-Sim-Password": PASSWORD}) as api:
        yield Env(api, calls)
    await hub_http.aclose()


def send_body(to_account: str = TO_OK, amount: int = 30_000, **kw) -> dict:
    return {"from_account": FROM, "to_bank": "901", "to_account": to_account, "amount": amount, "memo": "점심값", **kw}


def out_rows() -> list[dict]:
    return [t for t in sim.ledger.transactions(FROM) if t["direction"] == "OUT"]


async def hub_transfer(transfer_id: str) -> Transfer | None:
    async with SessionLocal() as db:
        return await db.get(Transfer, transfer_id)


# ---- 끝까지 ----

async def test_send_902_to_901_completes(env):
    res = await env.api.post("/sim/api/transfers", json=send_body())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "COMPLETED" and body["holder_name"] == "김철수" and body["balance"] == 970_000
    assert body["counterpart_bank_name"] == "AvengersBank" and body["direction"] == "OUT"
    tid = body["transfer_id"]
    assert tid == tid.lower() and len(tid) == 36 and tid[14] == "4"  # 소문자 UUID v4

    assert sim.ledger.balance(FROM) == 970_000
    [row] = out_rows()
    assert row["status"] == "COMPLETED" and row["transfer_id"] == tid and row["counterpart_account"] == TO_OK
    assert sim.received[tid]["to_account"] == TO_OK and sim.received[tid]["from_bank"] == "902"  # 901(bank_sim) 쪽 입금

    transfer = await hub_transfer(tid)
    assert transfer is not None and str(getattr(transfer.status, "value", transfer.status)) == "COMPLETED"
    assert transfer.from_bank == "902" and transfer.from_account == FROM != "902-TEST-0001"
    assert transfer.sender_name == "김철수"
    assert not is_self_test(transfer, "901")

    # 화면용 읽기 API
    customers = (await env.api.get("/sim/api/customers")).json()["customers"]
    assert customers[0]["name"] == "김철수" and customers[0]["accounts"][0]["balance"] == 970_000
    txs = (await env.api.get(f"/sim/api/accounts/{FROM}/transactions")).json()
    assert txs["balance"] == 970_000 and txs["transactions"][0]["transfer_id"] == tid
    assert (await env.api.get("/sim/api/accounts/333-000-0001/transactions")).status_code == 404


async def test_banks_excludes_self_and_inquiry_passes_through(env):
    banks = (await env.api.get("/sim/api/banks")).json()["banks"]
    assert [b["bank_code"] for b in banks] == ["901"]
    res = await env.api.post("/sim/api/inquiry", json={"to_bank": "901", "to_account": TO_OK})
    assert res.status_code == 200 and res.json()["holder_name"] == "김철수"
    res = await env.api.post("/sim/api/inquiry", json={"to_bank": "901", "to_account": "333-901-0002"})
    assert res.status_code == 404 and res.json()["error"] == "ACCOUNT_NOT_FOUND"
    res = await env.api.post("/sim/api/inquiry", json={"to_bank": "999", "to_account": TO_OK})
    assert res.status_code == 404 and res.json()["error"] == "BANK_NOT_FOUND"


async def test_inquiry_failure_leaves_balance(env):
    res = await env.api.post("/sim/api/transfers", json=send_body("333-901-0002"))
    assert res.status_code == 404 and res.json()["error"] == "ACCOUNT_NOT_FOUND"
    assert sim.ledger.balance(FROM) == 1_000_000 and out_rows() == []
    assert "POST /v1/transfers" not in env.hub_calls


# ---- 허브를 부르지 않는 거절 ----

async def test_rejected_before_hub(env):
    cases = [
        (send_body(amount=0), 400, "AMOUNT_INVALID"),
        (send_body(amount=1_000_001), 400, "AMOUNT_INVALID"),
        (send_body(amount="5000"), 422, "VALIDATION_ERROR"),
        (send_body(amount=5000.0), 422, "VALIDATION_ERROR"),
        ({**send_body(), "from_account": "333-000-0001"}, 404, "FROM_ACCOUNT_NOT_FOUND"),
        ({**send_body(), "from_account": "902-TEST-0001"}, 404, "FROM_ACCOUNT_NOT_FOUND"),
        ({**send_body(), "to_bank": "902"}, 400, "SAME_BANK"),
    ]
    for payload, status, code in cases:
        res = await env.api.post("/sim/api/transfers", json=payload)
        assert (res.status_code, res.json()["error"]) == (status, code), (payload, res.text)

    with sim.ledger.transaction() as conn:
        conn.execute("UPDATE accounts SET balance = 500 WHERE account_num = ?", (FROM,))
    res = await env.api.post("/sim/api/transfers", json=send_body(amount=1000))
    assert res.status_code == 400 and res.json()["error"] == "INSUFFICIENT_BALANCE" and res.json()["balance"] == 500

    assert env.hub_calls == [] and out_rows() == [] and sim.ledger.balance(FROM) == 500


async def test_password(env, monkeypatch):
    paths = [("GET", "/sim/api/customers"), ("GET", "/sim/api/banks"), ("POST", "/sim/api/inquiry"),
             ("POST", "/sim/api/transfers"), ("GET", f"/sim/api/accounts/{FROM}/transactions"),
             ("POST", "/sim/api/transfers/x/refresh")]
    for method, path in paths:
        res = await env.api.request(method, path, headers={"X-Sim-Password": "wrong"}, json=send_body())
        assert res.status_code == 401 and res.json()["error"] == "UNAUTHORIZED", path
    async with AsyncClient(transport=ASGITransport(app=sim.app), base_url="http://simbank") as bare:
        assert (await bare.get("/sim/api/customers")).status_code == 401  # 헤더 없음
    monkeypatch.setenv("SIM_UI_PASSWORD", "")
    for method, path in paths:
        res = await env.api.request(method, path, json=send_body())
        assert res.status_code == 403 and res.json()["error"] == "FORBIDDEN", path
    assert env.hub_calls == [] and sim.ledger.balance(FROM) == 1_000_000
    assert (await env.api.get("/")).status_code == 200  # 헬스체크는 그대로


# ---- 허브 결과에 따른 확정·환불 ----

async def test_hub_failed_refunds(env, client):
    await set_net_debit_cap(client, "902", 0)  # 허브가 접수 전에 LIMIT_EXCEEDED + status FAILED
    res = await env.api.post("/sim/api/transfers", json=send_body())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "FAILED" and body["fail_code"] == "LIMIT_EXCEEDED" and "순채무한도" in body["fail_message"]
    assert body["balance"] == 1_000_000 and sim.ledger.balance(FROM) == 1_000_000
    [row] = out_rows()
    assert row["status"] == "FAILED" and row["fail_code"] == "LIMIT_EXCEEDED"
    # 이미 확정된 거래의 refresh는 허브를 부르지 않고 그대로
    before = list(env.hub_calls)
    again = await env.api.post(f"/sim/api/transfers/{body['transfer_id']}/refresh")
    assert again.status_code == 200 and again.json()["status"] == "FAILED" and env.hub_calls == before
    assert sim.ledger.balance(FROM) == 1_000_000


async def test_hub_202_stays_pending_then_refresh_completes(env):
    """0003: 받는 쪽이 지연 → 허브 202 → PENDING(차감 유지). 허브가 확인을 끝낸 뒤 refresh → COMPLETED."""
    res = await env.api.post("/sim/api/transfers", json=send_body("333-901-0003"))
    assert res.status_code == 202 and res.json()["status"] == "PENDING"
    tid = res.json()["transfer_id"]
    assert sim.ledger.balance(FROM) == 970_000

    res = await env.api.post(f"/sim/api/transfers/{tid}/refresh")  # 허브도 아직 PENDING
    assert res.status_code == 202 and res.json()["status"] == "PENDING" and sim.ledger.balance(FROM) == 970_000

    os.environ["SIM_DELAY_SECONDS"] = "0"
    try:
        async with SessionLocal() as db:
            assert await confirm_pending(db) == 1
    finally:
        os.environ["SIM_DELAY_SECONDS"] = "1"
    res = await env.api.post(f"/sim/api/transfers/{tid}/refresh")
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED" and res.json()["balance"] == 970_000
    res = await env.api.post(f"/sim/api/transfers/{tid}/refresh")  # 두 번째: 변화 없음
    assert res.json()["status"] == "COMPLETED" and sim.ledger.balance(FROM) == 970_000
    assert [r["status"] for r in out_rows()] == ["COMPLETED"]


def test_stage1_file_gets_new_columns(tmp_path):
    """1단계에서 만든 장부 파일(fail_code 등 칸 없음)을 열면 칸을 붙이고, 있던 잔액·거래는 그대로다."""
    import sqlite3

    from bank_sim.store import SCHEMA, Ledger

    path = tmp_path / "stage1.db"
    old = SCHEMA.replace(",\n    fail_code             TEXT,\n    fail_message          TEXT,\n    finished_at           TEXT", "")
    old = old.split("CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_out_transfer_id")[0]
    assert "fail_code" not in old and "finished_at" not in old
    conn = sqlite3.connect(path)
    conn.executescript(old)
    conn.execute("INSERT INTO accounts VALUES ('110-123-0001', '김철수', '자유입출금', 1234)")
    conn.commit()
    conn.close()

    ledger = Ledger(str(path))
    try:
        assert ledger.balance("110-123-0001") == 1234
        assert ledger.start_send(transfer_id="t1", from_account="110-123-0001", amount=1000, to_bank="901",
                                 to_bank_name="", to_account="x", to_name="모아", memo="")
        assert ledger.finish_send("t1", "FAILED", "LIMIT_EXCEEDED", "한도") is True
        assert ledger.sent("t1")["fail_code"] == "LIMIT_EXCEEDED" and ledger.balance("110-123-0001") == 1234
    finally:
        ledger.close()


async def test_refresh_unknown_transfer(env):
    res = await env.api.post("/sim/api/transfers/00000000-0000-4000-8000-000000000000/refresh")
    assert res.status_code == 404 and res.json()["error"] == "TRANSFER_NOT_FOUND"


# ---- 가짜 허브 (MockTransport): 타임아웃·서명 틀림·3.4 FAILED·3.4 404 ----

class FakeHub:
    """3.1·3.2는 정상으로 답하고, 3.3·3.4는 `transfer`·`status` 함수가 정한다. 응답은 허브 키로 서명 (key로 바꿀 수 있다)."""

    def __init__(self):
        self.key = HUB_PRIVATE_KEY
        self.transfer = lambda req: (200, {"status": "COMPLETED"})
        self.status = lambda req: (200, {"status": "PENDING"})
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path == "/v1/banks":
            status, body = 200, {"banks": [{"bank_code": "901", "name": "AvengersBank"},
                                           {"bank_code": "902", "name": "SimBank"}]}
        elif path == "/v1/inquiry":
            status, body = 200, {**json.loads(request.content), "holder_name": "모아"}
        elif path == "/v1/transfers" and request.method == "POST":
            status, body = self.transfer(request)
        else:
            status, body = self.status(request)
        content = json.dumps(body, ensure_ascii=False).encode()
        headers = sign_response(self.key, status, request.headers.get("x-signature", ""), content)
        return httpx.Response(status, content=content, headers={**headers, "Content-Type": "application/json"})


@pytest.fixture
def fake_hub(env, monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(sim.hub, "client", AsyncClient(transport=httpx.MockTransport(fake), base_url="http://hub"))
    return fake


def _timeout(request: httpx.Request):
    raise httpx.ReadTimeout("시간 초과", request=request)


async def test_timeout_stays_pending_then_failed_refunds_once(env, fake_hub):
    fake_hub.transfer = _timeout
    res = await env.api.post("/sim/api/transfers", json=send_body())
    assert res.status_code == 202 and res.json()["status"] == "PENDING" and res.json()["holder_name"] == "모아"
    tid = res.json()["transfer_id"]
    assert sim.ledger.balance(FROM) == 970_000  # 돈이 나갔을 수 있으니 환불하지 않는다

    fake_hub.status = lambda req: (200, {"transfer_id": tid, "status": "FAILED", "fail_code": "RECEIVER_ERROR",
                                         "fail_message": "관리자 확정"})
    res = await env.api.post(f"/sim/api/transfers/{tid}/refresh")
    assert res.status_code == 200 and res.json()["status"] == "FAILED" and res.json()["fail_code"] == "RECEIVER_ERROR"
    assert sim.ledger.balance(FROM) == 1_000_000
    res = await env.api.post(f"/sim/api/transfers/{tid}/refresh")  # 두 번째: 환불 한 번만
    assert res.json()["status"] == "FAILED" and sim.ledger.balance(FROM) == 1_000_000
    assert sim.ledger.finish_send(tid, "FAILED") is False  # 직접 다시 불러도 조건부 UPDATE라 그대로
    assert sim.ledger.balance(FROM) == 1_000_000


async def test_bad_hub_signature_stays_pending(env, fake_hub):
    """3.3 응답이 COMPLETED여도 서명이 틀리면 믿지 않는다 → PENDING. 서명이 맞는 3.4로 확정."""
    fake_hub.key = generate_keypair()[0]
    fake_hub.transfer = lambda req: (200, {"status": "COMPLETED"})
    res = await env.api.post("/sim/api/transfers", json=send_body())
    assert res.status_code == 502 and res.json()["error"] == "HUB_SIGNATURE_INVALID"  # 수취 조회부터 믿지 않는다
    assert sim.ledger.balance(FROM) == 1_000_000 and out_rows() == []

    signed_ok = FakeHub()  # 조회는 맞는 서명, 3.3만 틀린 서명
    wrong = generate_keypair()[0]

    def handler(request: httpx.Request) -> httpx.Response:
        signed_ok.key = wrong if request.url.path == "/v1/transfers" else HUB_PRIVATE_KEY
        return signed_ok(request)

    sim.hub.client = AsyncClient(transport=httpx.MockTransport(handler), base_url="http://hub")
    res = await env.api.post("/sim/api/transfers", json=send_body())
    assert res.status_code == 202 and res.json()["status"] == "PENDING" and sim.ledger.balance(FROM) == 970_000
    tid = res.json()["transfer_id"]
    signed_ok.status = lambda req: (200, {"transfer_id": tid, "status": "COMPLETED"})
    res = await env.api.post(f"/sim/api/transfers/{tid}/refresh")
    assert res.json()["status"] == "COMPLETED" and sim.ledger.balance(FROM) == 970_000


async def test_status_less_4xx_and_5xx_stay_pending(env, fake_hub):
    """SPEC 3.3: status 없는 4xx(요청 자체 거절)·5xx는 결과가 아니다 → 환불하지 않는다."""
    for reply in [(401, {"error": "SIGNATURE_INVALID", "message": "서명이 맞지 않습니다"}),
                  (422, {"error": "VALIDATION_ERROR", "message": "형식"}),
                  (500, {"error": "INTERNAL_ERROR", "message": "오류"})]:
        fake_hub.transfer = lambda req, reply=reply: reply
        res = await env.api.post("/sim/api/transfers", json=send_body(amount=1000))
        assert res.status_code == 202 and res.json()["status"] == "PENDING", reply
    assert sim.ledger.balance(FROM) == 997_000


async def test_refresh_404_resends_same_transfer_id(env, fake_hub):
    """3.4가 404 TRANSFER_NOT_FOUND(허브에 접수 기록 없음) → 되돌리지 않고 같은 transfer_id로 3.3을 다시 보낸다."""
    fake_hub.transfer = _timeout
    res = await env.api.post("/sim/api/transfers", json=send_body())
    tid = res.json()["transfer_id"]
    sent: list[dict] = []

    def accept(request):
        sent.append(json.loads(request.content))
        return 200, {"transfer_id": tid, "status": "COMPLETED"}

    fake_hub.status = lambda req: (404, {"error": "TRANSFER_NOT_FOUND", "message": "없음", "transfer_id": tid})
    fake_hub.transfer = accept
    res = await env.api.post(f"/sim/api/transfers/{tid}/refresh")
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED"
    assert sent[0]["transfer_id"] == tid and sent[0]["amount"] == 30_000 and sent[0]["sender_name"] == "김철수"
    assert sim.ledger.balance(FROM) == 970_000

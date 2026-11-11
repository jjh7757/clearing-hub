"""가짜 은행 902의 장부(bank_sim/store.py): 잔액·거래내역·멱등·파일 유지 (SIMBANK_PLAN 3절 1단계)."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

import bank_sim.main as sim
from bank_sim.store import INITIAL_BALANCE, MEMORY, Ledger, ReceivedView, open_ledger

pytestmark = pytest.mark.anyio

KIM = "110-123-0001"  # 김철수 자유입출금 (고객 계좌)
SEED = [{"account_num": KIM, "holder": "김철수", "product_name": "자유입출금"},
        {"account_num": "110-789-0001", "holder": "홍길동", "product_name": "자유입출금"}]


def _receive(ledger: Ledger, transfer_id: str, to_account: str = KIM, amount: int = 10_000) -> bool:
    return ledger.receive(transfer_id=transfer_id, to_account=to_account, amount=amount, from_bank="901",
                          from_bank_name="AvengersBank", from_account="333-01-000001", sender_name="홍길동",
                          memo="점심값")


@pytest.fixture
def ledger():
    led = Ledger()
    led.seed_accounts(SEED)
    yield led
    led.close()


# ---- 장부 단독 ----

def test_customer_deposit_increases_balance(ledger):
    assert ledger.balance(KIM) == INITIAL_BALANCE
    assert _receive(ledger, "t-1", amount=10_000) is True
    assert ledger.balance(KIM) == INITIAL_BALANCE + 10_000
    [tx] = ledger.transactions(KIM)
    assert tx["direction"] == "IN" and tx["amount"] == 10_000 and tx["status"] == "COMPLETED"
    assert (tx["counterpart_bank"], tx["counterpart_bank_name"], tx["counterpart_name"]) == ("901", "AvengersBank", "홍길동")
    assert ledger.received("t-1")["to_account"] == KIM


def test_same_transfer_id_deposits_once(ledger):
    assert _receive(ledger, "t-1") is True
    assert _receive(ledger, "t-1") is False
    assert ledger.balance(KIM) == INITIAL_BALANCE + 10_000
    assert len(ledger.transactions(KIM)) == 1


def test_non_customer_account_records_only(ledger):
    assert _receive(ledger, "t-x", to_account="999-000-0001") is True
    assert ledger.account("999-000-0001") is None  # 잔액은 없다
    assert ledger.received("t-x")["to_account"] == "999-000-0001"
    assert ledger.balance(KIM) == INITIAL_BALANCE


def test_reopen_same_file_keeps_balance_and_transactions(tmp_path):
    path = str(tmp_path / "simbank.db")
    first = Ledger(path)
    first.seed_accounts(SEED)
    _receive(first, "t-1", amount=5_000)
    first.close()

    again = Ledger(path)
    again.seed_accounts(SEED)  # 시작할 때마다 시드해도 잔액을 덮어쓰지 않는다
    assert again.balance(KIM) == INITIAL_BALANCE + 5_000
    assert [t["transfer_id"] for t in again.transactions(KIM)] == ["t-1"]
    assert _receive(again, "t-1") is False  # 재시작 뒤에도 멱등
    assert again.balance(KIM) == INITIAL_BALANCE + 5_000
    again.close()


def test_seed_does_not_overwrite_balance(ledger):
    _receive(ledger, "t-1", amount=7_000)
    ledger.seed_accounts(SEED)
    assert ledger.balance(KIM) == INITIAL_BALANCE + 7_000


def test_received_view_and_clear(ledger):
    view = ReceivedView(ledger)
    assert view == {} and len(view) == 0
    _receive(ledger, "t-1")
    assert "t-1" in view and list(view) == ["t-1"] and view["t-1"]["amount"] == 10_000
    view.clear()  # 테스트용: 장부 전체를 처음 상태로
    assert view == {} and ledger.balance(KIM) == INITIAL_BALANCE and ledger.transactions(KIM) == []


def test_open_ledger_uses_memory_without_env(tmp_path):
    assert open_ledger({}).path == MEMORY
    assert open_ledger({"SIM_DB_PATH": ""}).path == MEMORY
    path = str(tmp_path / "x.db")
    assert open_ledger({"SIM_DB_PATH": path}).path == path


# ---- bank_sim 앱을 거쳐 (서명 검증은 끈다) ----

@pytest.fixture
async def sim_client(monkeypatch):
    monkeypatch.delenv("SIM_HUB_PUBLIC_KEY", raising=False)
    sim.received.clear()
    sim.server_errors_left.clear()
    sim.seen_request_ids.clear()
    async with AsyncClient(transport=ASGITransport(app=sim.app), base_url="http://sim") as ac:
        yield ac
    sim.received.clear()


def _payload(to_account: str, amount: int = 10_000) -> dict:
    return {"transfer_id": str(uuid.uuid4()), "from_bank": "901", "from_bank_name": "AvengersBank",
            "from_account": "333-01-000001", "to_account": to_account, "amount": amount, "sender_name": "홍길동"}


async def test_module_ledger_is_memory_without_env():
    assert sim.ledger.path == MEMORY  # 테스트 환경에는 SIM_DB_PATH가 없다


async def test_receive_endpoint_updates_balance_once(sim_client):
    body = _payload(KIM)
    for _ in range(2):
        res = await sim_client.post("/interbank/receive", json=body)
        assert res.status_code == 200 and res.json() == {"transfer_id": body["transfer_id"], "status": "COMPLETED",
                                                         "holder_name": "김철수"}
    assert sim.ledger.balance(KIM) == INITIAL_BALANCE + 10_000
    status = await sim_client.get(f"/interbank/receive/{body['transfer_id']}")
    assert status.status_code == 200 and status.json()["holder_name"] == "김철수" and status.json()["received_at"]
    assert (await sim_client.get("/health")).json() == {"service": "BankSim", "received": 1}


async def test_receive_endpoint_0006_leaves_no_trace(sim_client):
    body = _payload("110-123-0006")
    res = await sim_client.post("/interbank/receive", json=body)
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED"
    assert body["transfer_id"] not in sim.received
    assert sim.ledger.transactions("110-123-0006") == []
    assert (await sim_client.get(f"/interbank/receive/{body['transfer_id']}")).status_code == 404


async def test_receive_endpoint_non_customer_records_only(sim_client):
    body = _payload("777-000-0001")
    assert (await sim_client.post("/interbank/receive", json=body)).status_code == 200
    assert sim.received[body["transfer_id"]]["to_account"] == "777-000-0001"
    assert sim.ledger.account("777-000-0001") is None
    assert sim.ledger.balance(KIM) == INITIAL_BALANCE

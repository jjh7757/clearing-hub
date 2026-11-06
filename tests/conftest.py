import json
import os

# 앱을 import하기 전에 테스트용 설정 (SQLite, 짧은 타임아웃)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"
os.environ["ADMIN_TOKEN"] = "test-admin-token-test-admin-token-0000"
os.environ["RECEIVER_TIMEOUT_SECONDS"] = "0.3"
os.environ["RETRY_INTERVAL_SECONDS"] = "3600"  # 테스트는 confirm_pending을 직접 부른다
os.environ["RETRY_MAX_COUNT"] = "3"
os.environ["SETTLEMENT_WORKER_ENABLED"] = "false"  # 테스트 DB에 run을 만들지 않게. 워커 함수는 테스트가 직접 부른다
os.environ["SIM_DELAY_SECONDS"] = "1"  # 허브 타임아웃(0.3초)보다 길게

import pytest
from httpx import ASGITransport, AsyncClient

import bank_sim.main as sim
from clearing_hub.core import bank_client as bank_client_module
from clearing_hub.core.bank_client import BankClient
from clearing_hub.core.database import Base, engine
from clearing_hub.core.signing import sign
from clearing_hub.main import app

ADMIN = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}
SIM_URL = "http://sim/interbank"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SignedBank:
    """테스트에서 참가 은행 역할: 발급받은 키로 서명해 허브를 부른다."""

    def __init__(self, client: AsyncClient, bank_code: str, api_key: str, secret: str):
        self.client, self.bank_code, self.api_key, self.secret = client, bank_code, api_key, secret

    def headers(self, method: str, path: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
        return {"X-Bank-Code": self.bank_code, "X-Api-Key": self.api_key, "Content-Type": "application/json",
                **sign(self.secret, method, path, body, timestamp)}

    async def post(self, path: str, payload: dict, **kw):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return await self.client.post(path, content=body, headers=self.headers("POST", path, body, **kw))

    async def get(self, path: str):
        return await self.client.get(path, headers=self.headers("GET", path, b""))


@pytest.fixture
async def client(monkeypatch):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sim.received.clear()
    sim.server_errors_left.clear()
    sim.seen_request_ids.clear()
    # 허브 → 은행 호출을 가짜 은행 앱(bank_sim)으로 보낸다
    sim_client = AsyncClient(transport=ASGITransport(app=sim.app), base_url="http://sim")
    monkeypatch.setattr(bank_client_module.bank_client, "_client", sim_client)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub") as ac:
            yield ac
    await sim_client.aclose()


async def register_bank(client: AsyncClient, bank_code: str, name: str, base_url: str = SIM_URL) -> SignedBank:
    res = await client.post("/admin/banks", json={"bank_code": bank_code, "name": name, "base_url": base_url}, headers=ADMIN)
    assert res.status_code == 201, res.text
    data = res.json()
    return SignedBank(client, bank_code, data["api_key"], data["secret"])


GENEROUS_CAP = 1_000_000_000  # 10억: 이체 테스트가 순채무한도(SETTLEMENT_PLAN 4.4)에 걸리지 않을 만큼


async def set_net_debit_cap(client: AsyncClient, bank_code: str, cap: int) -> None:
    res = await client.patch(f"/admin/banks/{bank_code}", json={"net_debit_cap": cap}, headers=ADMIN)
    assert res.status_code == 200, res.text


@pytest.fixture
def net_debit_cap() -> int:
    """`banks` 픽스처가 901·902에 줄 순채무한도. 예치금 0·한도 0이면 송금이 막히므로 기본은 넉넉히 준다.
    한도 0에서 시작해야 하는 테스트 파일은 이 픽스처를 0으로 덮어쓴다 (test_reserve, test_net_debit_cap)."""
    return GENEROUS_CAP


@pytest.fixture
async def banks(client, monkeypatch, net_debit_cap):
    """901 = 보내는 은행, 902 = 받는 은행(bank_sim). bank_sim은 902의 secret으로 허브 서명을 검증한다."""
    sender = await register_bank(client, "901", "AvengersBank")
    receiver = await register_bank(client, "902", "SimBank")
    if net_debit_cap:
        for code in ("901", "902"):
            await set_net_debit_cap(client, code, net_debit_cap)
    monkeypatch.setenv("SIM_HUB_SECRET", receiver.secret)
    return sender, receiver


def transfer_payload(to_account: str, transfer_id: str | None = None, amount: int = 50000) -> dict:
    import uuid

    return {"transfer_id": transfer_id or str(uuid.uuid4()), "to_bank": "902", "to_account": to_account,
            "from_account": "110-200-456789", "amount": amount, "sender_name": "홍길동", "memo": "생활비"}


async def send_902_to_901(sender, receiver, monkeypatch, amount: int) -> dict:
    """902 → 901 이체를 허브 API로 만든다. 901의 base_url도 bank_sim이라, 이때만 bank_sim이 901의 secret으로
    허브 서명을 검증하게 바꾼다. 902는 한도를 넉넉히 준다."""
    await set_net_debit_cap(sender.client, "902", GENEROUS_CAP)
    payload = {**transfer_payload("110-123-0001", amount=amount), "to_bank": "901"}
    monkeypatch.setenv("SIM_HUB_SECRET", sender.secret)
    try:
        res = await receiver.post("/v1/transfers", payload)
    finally:
        monkeypatch.setenv("SIM_HUB_SECRET", receiver.secret)
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text
    return payload


__all__ = ["BankClient"]

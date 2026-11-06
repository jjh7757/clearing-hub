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

from clearing_hub.core.signing import generate_keypair, public_key_of, sign_ed25519  # noqa: E402

# 허브 Ed25519 키 (SPEC 2.6, 필수 설정). 테스트 실행마다 새로 만든다. 허브의 모든 은행 호출·응답 서명에 쓴다
HUB_PRIVATE_KEY, HUB_PUBLIC_KEY = generate_keypair()
os.environ["HUB_SIGNING_KEY"] = HUB_PRIVATE_KEY
# 가짜 은행(902) 키. bank_sim은 이 키로 응답에 서명하고, 허브 공개키로 허브 요청을 검증한다
SIM_PRIVATE_KEY, SIM_PUBLIC_KEY = generate_keypair()
os.environ["SIM_SIGNING_KEY"] = SIM_PRIVATE_KEY
os.environ["SIM_HUB_PUBLIC_KEY"] = HUB_PUBLIC_KEY

import pytest
from httpx import ASGITransport, AsyncClient

import bank_sim.main as sim
from clearing_hub.core import bank_client as bank_client_module
from clearing_hub.core.bank_client import BankClient
from clearing_hub.core.config import get_settings
from clearing_hub.core.database import Base, engine
from clearing_hub.main import app

ADMIN = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}
SIM_URL = "http://sim/interbank"
GENEROUS_CAP = get_settings().default_net_debit_cap  # 10억 (DEFAULT_NET_DEBIT_CAP): 이체 테스트가 순채무한도에 걸리지 않을 만큼


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SignedBank:
    """테스트에서 참가 은행 역할: 자기 Ed25519 개인키로 서명해 허브를 부른다 (SPEC 2.2). API 키는 식별용이다."""

    def __init__(self, client: AsyncClient, bank_code: str, api_key: str, private_key: str):
        self.client, self.bank_code, self.api_key = client, bank_code, api_key
        self.use_key(private_key)

    def use_key(self, private_key: str) -> "SignedBank":
        self.private_key, self.public_key = private_key, public_key_of(private_key)
        return self

    def headers(self, method: str, path: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
        return {"X-Bank-Code": self.bank_code, "X-Api-Key": self.api_key, "Content-Type": "application/json",
                **sign_ed25519(self.private_key, method, path, body, timestamp)}

    async def put(self, path: str, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return await self.client.put(path, content=body, headers=self.headers("PUT", path, body))

    async def patch(self, path: str, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return await self.client.patch(path, content=body, headers=self.headers("PATCH", path, body))

    async def post(self, path: str, payload: dict | None = None, **kw):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else b""
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


async def register_bank(client: AsyncClient, bank_code: str, name: str, base_url: str = SIM_URL,
                        private_key: str | None = None, net_debit_cap: int | None = None) -> SignedBank:
    """관리자 직접 등록 (POST /admin/banks). 키 쌍을 만들어(또는 받은 개인키로) 공개키를 등록한다.
    허브가 이 은행의 base_url(가짜 은행)을 불러 응답 서명을 검증해야 하면 private_key=SIM_PRIVATE_KEY로 등록한다."""
    private_key = private_key or generate_keypair()[0]
    body = {"bank_code": bank_code, "name": name, "base_url": base_url, "public_key": public_key_of(private_key)}
    if net_debit_cap is not None:
        body["net_debit_cap"] = net_debit_cap
    res = await client.post("/admin/banks", json=body, headers=ADMIN)
    assert res.status_code == 201, res.text
    return SignedBank(client, bank_code, res.json()["api_key"], private_key)


async def set_net_debit_cap(client: AsyncClient, bank_code: str, cap: int) -> None:
    res = await client.patch(f"/admin/banks/{bank_code}", json={"net_debit_cap": cap}, headers=ADMIN)
    assert res.status_code == 200, res.text


@pytest.fixture
def net_debit_cap() -> int:
    """`banks` 픽스처가 901·902에 줄 순채무한도. 기본은 등록 기본값(10억)과 같다.
    한도 0에서 시작해야 하는 테스트 파일은 이 픽스처를 0으로 덮어쓴다 (test_reserve, test_net_debit_cap)."""
    return GENEROUS_CAP


@pytest.fixture
async def banks(client, net_debit_cap):
    """901 = 보내는 은행(자기 키 쌍), 902 = 받는 은행(bank_sim, 가짜 은행 키 쌍). 허브는 902의 응답 서명을 SIM 공개키로 검증한다."""
    sender = await register_bank(client, "901", "AvengersBank", net_debit_cap=net_debit_cap)
    receiver = await register_bank(client, "902", "SimBank", private_key=SIM_PRIVATE_KEY, net_debit_cap=net_debit_cap)
    return sender, receiver


def transfer_payload(to_account: str, transfer_id: str | None = None, amount: int = 50000) -> dict:
    import uuid

    return {"transfer_id": transfer_id or str(uuid.uuid4()), "to_bank": "902", "to_account": to_account,
            "from_account": "110-200-456789", "amount": amount, "sender_name": "홍길동", "memo": "생활비"}


async def send_902_to_901(sender, receiver, monkeypatch, amount: int) -> dict:
    """902 → 901 이체를 허브 API로 만든다. 901의 base_url도 bank_sim이라, 이때만 bank_sim이 901의 키로 응답에 서명하게
    바꾼다 (허브는 901의 응답 서명을 901 공개키로 검증한다). 902는 한도를 넉넉히 준다."""
    await set_net_debit_cap(sender.client, "902", GENEROUS_CAP)
    payload = {**transfer_payload("110-123-0001", amount=amount), "to_bank": "901"}
    monkeypatch.setenv("SIM_SIGNING_KEY", sender.private_key)
    try:
        res = await receiver.post("/v1/transfers", payload)
    finally:
        monkeypatch.setenv("SIM_SIGNING_KEY", SIM_PRIVATE_KEY)
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text
    return payload


__all__ = ["BankClient"]

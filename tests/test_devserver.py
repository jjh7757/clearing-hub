"""로컬 개발 서버 clearing_hub.devserver: 키·토큰 생성과 `.env.dev` 재사용, 환경 변수 채우기, 902 등록·예치금, 초대 문구.

서버(uvicorn)는 띄우지 않고 함수 단위로 본다. DB 함수는 테스트 DB(conftest의 SQLite)에서 돈다.
"""

import pytest

from clearing_hub import devserver
from clearing_hub.core.signing import fingerprint, generate_keypair, public_key_of
from tests.conftest import ADMIN, HUB_PUBLIC_KEY


def test_ensure_dev_env_creates_and_reuses(tmp_path):
    path = tmp_path / ".env.dev"
    first = devserver.ensure_dev_env(path)
    assert set(first) == set(devserver.GENERATED_KEYS)
    assert len(first["ADMIN_TOKEN"]) >= 32  # core/config.py: ADMIN_TOKEN은 32자 이상
    for key in ("HUB_SIGNING_KEY", "SIM_SIGNING_KEY"):
        public_key_of(first[key])  # Ed25519 개인키 형식 (틀리면 ValueError)
    assert first["HUB_SIGNING_KEY"] != first["SIM_SIGNING_KEY"]
    saved = path.read_text(encoding="utf-8")
    assert saved.startswith("#") and all(f"{k}={v}" in saved for k, v in first.items())

    assert devserver.ensure_dev_env(path) == first  # 다음 실행은 같은 값
    assert path.read_text(encoding="utf-8") == saved  # 다 있으면 다시 쓰지 않는다


def test_ensure_dev_env_fills_only_missing(tmp_path):
    path = tmp_path / ".env.dev"
    hub_key = generate_keypair()[0]
    path.write_text(f"# 손으로 쓴 값\nHUB_SIGNING_KEY={hub_key}\nEXTRA=1\n\n", encoding="utf-8")
    values = devserver.ensure_dev_env(path)
    assert values["HUB_SIGNING_KEY"] == hub_key and values["EXTRA"] == "1"
    assert values["ADMIN_TOKEN"] and values["SIM_SIGNING_KEY"]
    assert devserver.load_env_file(path) == values


def test_apply_env_defaults_and_precedence():
    values = {"ADMIN_TOKEN": "t" * 40, "HUB_SIGNING_KEY": generate_keypair()[0], "SIM_SIGNING_KEY": generate_keypair()[0]}
    env: dict[str, str] = {}
    devserver.apply_env(values, env)
    assert env["DATABASE_URL"] == "sqlite+aiosqlite:///./dev_hub.db"
    assert env["HUB_PUBLIC_URL"] == "http://127.0.0.1:8000"
    assert (env["SETTLEMENT_WORKER_ENABLED"], env["ALERT_WEBHOOK_URL"]) == ("false", "")
    assert env["SIM_HUB_PUBLIC_KEY"] == public_key_of(values["HUB_SIGNING_KEY"])  # 가짜 은행이 허브 서명을 검증할 키

    # 이미 있는 환경 변수가 먼저다. 가짜 은행의 허브 공개키는 실제로 쓰는 허브 키에서 다시 만든다
    own_hub = generate_keypair()[0]
    env = {"DATABASE_URL": "postgresql+psycopg://x", "HUB_SIGNING_KEY": own_hub, "SIM_HUB_PUBLIC_KEY": "stale"}
    devserver.apply_env(values, env, hub_port=8001)
    assert env["DATABASE_URL"] == "postgresql+psycopg://x" and env["HUB_SIGNING_KEY"] == own_hub
    assert env["SIM_HUB_PUBLIC_KEY"] == public_key_of(own_hub)
    assert env["HUB_PUBLIC_URL"] == "http://127.0.0.1:8001"


@pytest.mark.anyio
async def test_ensure_sim_bank_and_invite(client):
    sim_private = generate_keypair()[0]
    sim_public, url = public_key_of(sim_private), "http://127.0.0.1:8100/interbank"
    assert await devserver.ensure_sim_bank(sim_public, url) is True
    banks = {b["bank_code"]: b for b in (await client.get("/admin/banks", headers=ADMIN)).json()}
    assert (banks["902"]["name"], banks["902"]["base_url"]) == ("SimBank", url)
    reserve = {r["bank_code"]: r for r in (await client.get("/admin/reserve", headers=ADMIN)).json()}
    assert reserve["902"]["balance"] == devserver.SIM_DEPOSIT == 10_000_000

    # 다시 실행: 등록·예치를 반복하지 않는다. 포트·키가 바뀌었으면 맞춘다
    new_public, new_url = public_key_of(generate_keypair()[0]), "http://127.0.0.1:8101/interbank"
    assert await devserver.ensure_sim_bank(new_public, new_url) is False
    banks = {b["bank_code"]: b for b in (await client.get("/admin/banks", headers=ADMIN)).json()}
    assert banks["902"]["base_url"] == new_url and banks["902"]["public_key_fingerprint"] == fingerprint(new_public)
    reserve = {r["bank_code"]: r for r in (await client.get("/admin/reserve", headers=ADMIN)).json()}
    assert reserve["902"]["balance"] == 10_000_000

    message = await devserver.new_invite_message("http://127.0.0.1:8000")
    code = next(line.split(": ", 1)[1] for line in message.splitlines() if line.startswith("- 초대 코드: "))
    for part in (code, "http://127.0.0.1:8000", HUB_PUBLIC_KEY, fingerprint(HUB_PUBLIC_KEY), "빠른 시작"):
        assert part in message, part
    invites = (await client.get("/admin/invites", headers=ADMIN)).json()
    assert [(i["code_prefix"], i["status"], i["note"]) for i in invites] == [(code[:8], "UNUSED", "devserver")]

    banner = devserver.banner({"DATABASE_URL": "sqlite://", "ADMIN_TOKEN": "tok"}, message, 8000, 8100)
    assert message in banner and "tok" in banner and "110-123-0001" in banner and "127.0.0.1:3000/interbank" in banner


@pytest.mark.anyio
async def test_self_register_accepts_local_http_base_url(client):
    """로컬 은행은 http://127.0.0.1:3000/interbank로 셀프 등록할 수 있다 (허브는 base_url의 https를 강제하지 않는다)."""
    invite = (await client.post("/admin/invites", json={}, headers=ADMIN)).json()["code"]
    res = await client.post("/v1/banks/register", json={
        "invite_code": invite, "name": "LocalBank", "base_url": "http://127.0.0.1:3000/interbank",
        "public_key": public_key_of(generate_keypair()[0])})
    assert res.status_code == 201, res.text
    me = {b["bank_code"]: b for b in (await client.get("/admin/banks", headers=ADMIN)).json()}[res.json()["bank_code"]]
    assert me["base_url"] == "http://127.0.0.1:3000/interbank"

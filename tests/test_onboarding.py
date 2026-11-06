"""셀프 등록·기본 한도·은행 셀프 서비스 (SETTLEMENT_PLAN 12.11 8c, SPEC 3.5·3.6·7절·10.2, docs/ONBOARDING.md).

관리자가 하는 일은 초대 코드 발급뿐이고, 은행은 그 코드로 스스로 등록한다. 테스트의 가짜 은행 클라이언트는 호스트를
무시하므로 `http://<아무 호스트>/interbank`는 모두 bank_sim으로 간다. 가짜 은행은 SIM 키로 응답에 서명하므로,
ping이 정상이려면 SIM 키 쌍으로 등록해야 한다.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.core.signing import fingerprint, generate_keypair, verify_response
from clearing_hub.domains.banks import router as bank_router
from clearing_hub.domains.banks.models import Invite
from tests.conftest import (
    ADMIN, GENEROUS_CAP, HUB_PUBLIC_KEY, SIM_PRIVATE_KEY, SIM_PUBLIC_KEY, SignedBank, register_bank, transfer_payload,
)

pytestmark = pytest.mark.anyio


async def invite(client, note: str = "C팀") -> str:
    res = await client.post("/admin/invites", json={"note": note}, headers=ADMIN)
    assert res.status_code == 201, res.text
    assert res.json()["code"].startswith("inv_") and res.json()["note"] == note
    return res.json()["code"]


async def self_register(client, code: str, base_url: str, public_key: str = SIM_PUBLIC_KEY, name: str = "CBank"):
    return await client.post("/v1/banks/register",
                             json={"invite_code": code, "name": name, "base_url": base_url, "public_key": public_key})


async def test_self_register_end_to_end(client, banks):
    """초대 코드 → 등록(코드 배정, 기본 한도, 허브 공개키, ping) → 바로 송금·수신."""
    sender, _ = banks
    code = await invite(client)
    res = await self_register(client, code, "http://c-bank/interbank/")
    assert res.status_code == 201, res.text
    body = res.json()
    assert (body["bank_code"], body["name"], body["net_debit_cap"]) == ("903", "CBank", GENEROUS_CAP)
    assert body["api_key"].startswith("chk_")
    assert (body["hub_public_key"], body["hub_fingerprint"]) == (HUB_PUBLIC_KEY, fingerprint(HUB_PUBLIC_KEY))
    assert body["ping"] == {"ok": True, "reachable": True, "status": 200, "body": {"holder_name": "김철수"},
                            "response_signature_valid": True}
    # 응답도 허브가 서명한다 (등록 요청에는 서명이 없으므로 request_signature는 "")
    assert verify_response(HUB_PUBLIC_KEY, res.headers["x-response-timestamp"], 201, "", res.content,
                           res.headers["x-response-signature"]) is None

    c_bank = SignedBank(client, "903", body["api_key"], SIM_PRIVATE_KEY)
    me = (await c_bank.get("/v1/me")).json()
    assert me == {"bank_code": "903", "name": "CBank", "base_url": "http://c-bank/interbank", "active": True,
                  "net_debit_cap": GENEROUS_CAP, "public_key_fingerprint": fingerprint(SIM_PUBLIC_KEY),
                  "created_at": me["created_at"]}
    reserve = (await c_bank.get("/v1/reserve")).json()
    assert (reserve["balance"], reserve["available"]) == (0, GENEROUS_CAP)  # 예치금 0이어도 기본 한도로 바로 보낼 수 있다

    # 보내는 쪽: 903 → 902
    res = await c_bank.post("/v1/transfers", transfer_payload("110-123-0001"))
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text
    # 받는 쪽: 901 → 903 (903의 base_url도 가짜 은행, SIM 키로 서명)
    res = await sender.post("/v1/transfers", {**transfer_payload("110-123-0001"), "to_bank": "903"})
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text
    assert "903" in [b["bank_code"] for b in (await sender.get("/v1/banks")).json()["banks"]]

    listed = (await client.get("/admin/invites", headers=ADMIN)).json()
    assert [(i["code_prefix"], i["note"], i["status"], i["used_by_bank_code"]) for i in listed] == [
        (code[:8], "C팀", "USED", "903")]
    assert listed[0]["used_at"] and "code" not in listed[0]  # 목록에는 코드 전체를 다시 보여 주지 않는다


async def test_invite_invalid(client):
    """없는·만료된·이미 쓴 초대 코드는 400 INVITE_INVALID. 아무것도 만들어지지 않는다."""
    res = await self_register(client, "inv_nothing", "http://x/interbank")
    assert (res.status_code, res.json()["error"]) == (400, "INVITE_INVALID")

    expired = await invite(client, "만료")
    async with SessionLocal() as db, db.begin():
        await db.execute(update(Invite).where(Invite.code == expired).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    res = await self_register(client, expired, "http://x/interbank")
    assert (res.status_code, res.json()["error"]) == (400, "INVITE_INVALID")

    used = await invite(client, "한 번만")
    assert (await self_register(client, used, "http://y/interbank")).status_code == 201
    res = await self_register(client, used, "http://z/interbank")
    assert (res.status_code, res.json()["error"]) == (400, "INVITE_INVALID")

    statuses = {i["note"]: i["status"] for i in (await client.get("/admin/invites", headers=ADMIN)).json()}
    assert statuses == {"만료": "EXPIRED", "한 번만": "USED"}
    assert len((await client.get("/admin/banks", headers=ADMIN)).json()) == 1


async def test_invite_message(client, monkeypatch):
    """발급 응답의 message: 관리자가 그대로 DM으로 보낼 초대 문구 (ONBOARDING.md 1)."""
    res = await client.post("/admin/invites", json={"note": "C팀"}, headers=ADMIN)
    body = res.json()
    message = body["message"]
    for part in (body["code"], body["expires_at"], "http://hub", "docs/SPEC.md", HUB_PUBLIC_KEY, fingerprint(HUB_PUBLIC_KEY),
                 "POST http://hub/v1/banks/register", "invite_code·name·base_url·public_key", "한 번만", "공개 채널"):
        assert part in message, part
    assert "KST" in message

    monkeypatch.setattr(get_settings(), "hub_public_url", "https://clearing-hub.example.com/")
    message = (await client.post("/admin/invites", json={}, headers=ADMIN)).json()["message"]
    assert "- 허브 주소: https://clearing-hub.example.com\n" in message and "http://hub" not in message
    assert "POST https://clearing-hub.example.com/v1/banks/register" in message


async def test_invite_ttl(client):
    res = await client.post("/admin/invites", json={}, headers=ADMIN)
    expires = datetime.strptime(res.json()["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    days = get_settings().invite_ttl_days
    assert timedelta(days=days) - timedelta(minutes=1) < expires - datetime.now(UTC) <= timedelta(days=days)
    assert (await client.post("/admin/invites", json={})).status_code == 401  # 관리자만
    assert (await client.get("/admin/invites")).status_code == 401


async def test_register_validation(client):
    code = await invite(client)
    for body in ({"invite_code": code, "name": "X", "base_url": "http://x/interbank"},  # public_key 없음
                 {"invite_code": code, "name": "X", "base_url": "http://x/interbank", "public_key": "AAAA"},
                 {"invite_code": code, "name": "X", "base_url": "not-a-url", "public_key": SIM_PUBLIC_KEY},
                 {"invite_code": code, "name": "", "base_url": "http://x/interbank", "public_key": SIM_PUBLIC_KEY}):
        res = await client.post("/v1/banks/register", json=body)
        assert (res.status_code, res.json()["error"]) == (422, "VALIDATION_ERROR"), body
    statuses = [i["status"] for i in (await client.get("/admin/invites", headers=ADMIN)).json()]
    assert statuses == ["UNUSED"]  # 형식 오류는 초대 코드를 쓰지 않는다


async def test_same_base_url_is_bank_exists(client, banks):
    """같은 base_url로 두 번 등록 → 409 BANK_EXISTS (끝의 / 는 같은 주소). 초대 코드는 쓰지 않은 채로 남는다."""
    code = await invite(client)
    res = await self_register(client, code, "http://sim/interbank/")  # 901·902의 주소 (테스트에서는 둘 다 가짜 은행)
    assert (res.status_code, res.json()["error"]) == (409, "BANK_EXISTS") and "(901)" in res.json()["message"]
    assert (await self_register(client, code, "http://other/interbank")).status_code == 201


async def test_bank_code_assignment_skips_used(client, monkeypatch):
    """BANK_CODE_START부터 비어 있는 가장 작은 코드. 관리자가 넣은 코드(990 등)와 중간의 빈 칸도 맞게 다룬다."""
    await register_bank(client, "903", "Admin903")
    await register_bank(client, "905", "Admin905", "http://a905/interbank")
    codes = []
    for host in ("p", "q", "r"):
        res = await self_register(client, await invite(client, host), f"http://{host}/interbank", generate_keypair()[1])
        codes.append(res.json()["bank_code"])
    assert codes == ["904", "906", "907"]

    monkeypatch.setattr(get_settings(), "bank_code_start", "998")
    await register_bank(client, "999", "Admin999", "http://a999/interbank")
    res = await self_register(client, await invite(client), "http://s/interbank", generate_keypair()[1])
    assert res.json()["bank_code"] == "998"
    res = await self_register(client, await invite(client), "http://t/interbank", generate_keypair()[1])
    assert (res.status_code, res.json()["error"]) == (409, "BANK_CODE_EXHAUSTED")


async def test_concurrent_registrations_get_different_codes(client):
    codes = [await invite(client, str(i)) for i in range(3)]
    results = await asyncio.gather(*(self_register(client, c, f"http://h{i}/interbank", generate_keypair()[1])
                                     for i, c in enumerate(codes)))
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    assert sorted(r.json()["bank_code"] for r in results) == ["903", "904", "905"]


async def test_ping_failure_keeps_registration(client, monkeypatch):
    """ping이 실패해도(응답 서명 키가 다름) 등록은 유지되고 active다. 은행이 고친 뒤 /v1/me/ping으로 다시 확인한다."""
    other_private, other_public = generate_keypair()
    res = await self_register(client, await invite(client), "http://d-bank/interbank", other_public)
    assert res.status_code == 201
    body = res.json()
    assert (body["ping"]["ok"], body["ping"]["reachable"], body["ping"]["status"]) == (False, True, 200)
    assert body["ping"]["response_signature_valid"] is False
    d_bank = SignedBank(client, body["bank_code"], body["api_key"], other_private)
    assert (await d_bank.get("/v1/me")).json()["active"] is True

    monkeypatch.setenv("SIM_SIGNING_KEY", other_private)  # 은행이 응답 서명을 고침
    ping = (await d_bank.post("/v1/me/ping")).json()
    assert (ping["ok"], ping["response_signature_valid"]) == (True, True)

    monkeypatch.setenv("SIM_HUB_PUBLIC_KEY", generate_keypair()[1])  # 은행이 허브 공개키를 잘못 넣음 → 서명된 401
    ping = (await d_bank.post("/v1/me/ping")).json()
    assert (ping["ok"], ping["status"], ping["body"]["error"], ping["response_signature_valid"]) == (
        False, 401, "SIGNATURE_INVALID", True)


async def test_patch_me(client, banks, monkeypatch):
    """PATCH /v1/me: 이름은 바로, base_url은 새 주소의 ping이 정상일 때만. 안 되면 400 PING_FAILED, 아무것도 안 바뀐다."""
    _, receiver = banks  # 902: 가짜 은행 SIM 키
    res = await receiver.patch("/v1/me", {"name": "SimBank2"})
    assert res.status_code == 200 and res.json()["name"] == "SimBank2"

    res = await receiver.patch("/v1/me", {"base_url": "http://sim/wrong-path", "name": "Nope"})
    assert (res.status_code, res.json()["error"]) == (400, "PING_FAILED")
    assert res.json()["ping"]["status"] == 404 and res.json()["ping"]["ok"] is False
    me = (await receiver.get("/v1/me")).json()
    assert (me["name"], me["base_url"]) == ("SimBank2", "http://sim/interbank")  # 이름도 바뀌지 않았다

    res = await receiver.patch("/v1/me", {"base_url": "http://sim-new/interbank/"})
    assert res.status_code == 200 and res.json()["base_url"] == "http://sim-new/interbank"
    listed = {b["bank_code"]: b for b in (await client.get("/admin/banks", headers=ADMIN)).json()}
    assert listed["902"]["base_url"] == "http://sim-new/interbank"

    # 404 ACCOUNT_NOT_FOUND도 정상이다 (실제 은행에는 ping 계좌가 없다). 가짜 은행은 끝자리 0002에 404를 준다
    monkeypatch.setattr(bank_router, "PING_ACCOUNT", "000-000-0002")
    res = await receiver.patch("/v1/me", {"base_url": "http://sim-third/interbank"})
    assert res.status_code == 200, res.text
    ping = (await receiver.post("/v1/me/ping")).json()
    assert (ping["ok"], ping["status"], ping["body"]["error"]) == (True, 404, "ACCOUNT_NOT_FOUND")

    for bad in ({}, {"base_url": "not-a-url"}, {"net_debit_cap": 1}):
        res = await receiver.patch("/v1/me", bad)
        assert res.status_code == 422, bad
    assert (await client.patch("/v1/me", json={"name": "x"})).status_code == 401  # 인증 필요


async def test_default_net_debit_cap(client):
    """관리자 등록도 기본 한도(10억)를 쓰고, 본문으로 덮어쓸 수 있다. 공개키는 필수다."""
    private, public = generate_keypair()
    res = await client.post("/admin/banks", json={"bank_code": "950", "name": "A", "base_url": "http://a/interbank",
                                                  "public_key": public}, headers=ADMIN)
    assert res.status_code == 201
    assert (res.json()["net_debit_cap"], res.json()["public_key_fingerprint"]) == (GENEROUS_CAP, fingerprint(public))
    assert "secret" not in res.json() and res.json()["api_key"]
    res = await client.post("/admin/banks", json={"bank_code": "951", "name": "B", "base_url": "http://b/interbank",
                                                  "public_key": generate_keypair()[1], "net_debit_cap": 0}, headers=ADMIN)
    assert res.json()["net_debit_cap"] == 0
    res = await client.post("/admin/banks", json={"bank_code": "952", "name": "C", "base_url": "http://c/interbank"},
                            headers=ADMIN)
    assert (res.status_code, res.json()["error"]) == (422, "VALIDATION_ERROR")  # 공개키 없음
    reserve = (await client.get("/admin/reserve", headers=ADMIN)).json()
    assert {r["bank_code"]: r["available"] for r in reserve} == {"950": GENEROUS_CAP, "951": 0}

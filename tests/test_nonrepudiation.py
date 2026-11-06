"""부인방지 (SETTLEMENT_PLAN 12절 8a·8c, 12.8 K4~K10): Ed25519 요청·응답 서명, 키 등록·회전, 증거 보관.

서명 방식은 Ed25519 하나다 (8c에서 HMAC과 서명 모드를 뺐다). 다른 테스트 파일의 이체 시나리오도 모두 Ed25519로 돈다.
901 = 보내는 은행(테스트의 SignedBank), 902 = 받는 은행(bank_sim). 둘 다 base_url이 가짜 은행이다.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select, update

import bank_sim.main as sim
from clearing_hub.core import bank_client as bank_client_module
from clearing_hub.core.database import SessionLocal
from clearing_hub.core.signing import (
    fingerprint, generate_keypair, hash_api_key, public_key_of, sign_ed25519, verify_ed25519, verify_response,
)
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.transfers.service import confirm_pending
from tests.conftest import (
    ADMIN, HUB_PRIVATE_KEY, HUB_PUBLIC_KEY, SIM_PRIVATE_KEY, SIM_PUBLIC_KEY, transfer_payload,
)

pytestmark = pytest.mark.anyio


async def evidence(client: AsyncClient, transfer_id: str) -> list[dict]:
    res = await client.get(f"/admin/transfers/{transfer_id}/evidence", headers=ADMIN)
    assert res.status_code == 200, res.text
    return res.json()


def _body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


# ---- 허브 키·응답 서명 ----

async def test_hub_key_endpoint(client):
    res = await client.get("/v1/hub-key")  # 인증 없음
    assert res.status_code == 200
    assert res.json() == {"algorithm": "ed25519", "public_key": HUB_PUBLIC_KEY, "fingerprint": fingerprint(HUB_PUBLIC_KEY)}


async def test_hub_signing_key_is_required(monkeypatch):
    """HUB_SIGNING_KEY는 필수다. 없거나 비었거나 형식이 틀리면 설정 단계에서 실패한다 (서버가 시작하지 않는다)."""
    from pydantic import ValidationError

    from clearing_hub.core.config import Settings

    for value in ("", "   ", "not-a-key", "A" * 43):
        monkeypatch.setenv("HUB_SIGNING_KEY", value)
        with pytest.raises(ValidationError):
            Settings()
    monkeypatch.delenv("HUB_SIGNING_KEY")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("HUB_SIGNING_KEY", HUB_PRIVATE_KEY)
    assert Settings().hub_signing_key.get_secret_value() == HUB_PRIVATE_KEY


async def test_hub_response_signature(banks):
    """허브 응답(2xx·4xx)의 X-Response-Signature를 허브 공개키로 검증한다. 요청의 X-Signature에 묶여 있다 (SPEC 2.5)."""
    sender, _ = banks
    hub_public = (await sender.client.get("/v1/hub-key")).json()["public_key"]
    headers = sender.headers("GET", "/v1/reserve", b"")
    res = await sender.client.get("/v1/reserve", headers=headers)
    assert res.status_code == 200 and res.headers["x-signature-alg"] == "ed25519"
    assert verify_response(hub_public, res.headers["x-response-timestamp"], 200, headers["X-Signature"], res.content,
                           res.headers["x-response-signature"]) is None
    assert verify_response(hub_public, res.headers["x-response-timestamp"], 200, "other-request", res.content,
                           res.headers["x-response-signature"]) == "SIGNATURE_INVALID"

    # 4xx도 서명한다. 요청에 서명이 없으면 request_signature는 ""
    res = await sender.client.get("/v1/reserve")
    assert res.status_code == 401
    assert verify_response(hub_public, res.headers["x-response-timestamp"], 401, "", res.content,
                           res.headers["x-response-signature"]) is None
    # /v1 밖(관리자 API)은 서명하지 않는다
    assert "x-response-signature" not in (await sender.client.get("/admin/banks", headers=ADMIN)).headers


# ---- 알고리즘 헤더·공개키 없는 은행 ----

async def test_signature_alg_header(banks):
    """X-Signature-Alg는 생략하면 ed25519로 본다. ed25519(대소문자 무관) 밖의 값은 401 (hmac도 이제 모르는 값이다)."""
    sender, _ = banks
    base = sender.headers("GET", "/v1/banks", b"")
    assert (await sender.client.get("/v1/banks", headers=base)).status_code == 200
    without = {k: v for k, v in base.items() if k != "X-Signature-Alg"}
    assert (await sender.client.get("/v1/banks", headers=without)).status_code == 200
    assert (await sender.client.get("/v1/banks", headers={**base, "X-Signature-Alg": "Ed25519"})).status_code == 200
    for alg in ("hmac", "rsa"):
        res = await sender.client.get("/v1/banks", headers={**base, "X-Signature-Alg": alg})
        assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID"), alg
        assert "ed25519" in res.json()["message"]


async def _drop_public_key(bank_code: str) -> None:
    """8c 전에 등록돼 공개키가 없는 운영 은행(901·902)을 흉내 낸다."""
    async with SessionLocal() as db, db.begin():
        await db.execute(update(Bank).where(Bank.bank_code == bank_code).values(public_key=None))


async def test_bank_without_public_key(banks):
    """공개키가 없는 은행: 보내면 401 "등록된 공개키가 없습니다", 받는 은행으로는 접수 전 거절 PUBLIC_KEY_MISSING(FAILED).
    관리자가 공개키를 등록하면 바로 살아난다 (배포 전에 901·902를 등록하는 이유, DEPLOY.md)."""
    sender, receiver = banks
    await _drop_public_key("901")
    res = await sender.get("/v1/banks")
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")
    assert "등록된 공개키가 없습니다" in res.json()["message"]
    res = await sender.client.post("/admin/banks/901/public-key", json={"public_key": sender.public_key}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["public_key_fingerprint"] == fingerprint(sender.public_key)
    assert (await sender.get("/v1/banks")).status_code == 200

    await _drop_public_key("902")
    payload = transfer_payload("110-123-0001")
    res = await sender.post("/v1/transfers", payload)
    assert (res.status_code, res.json()["error"], res.json()["status"]) == (400, "PUBLIC_KEY_MISSING", "FAILED")
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})
    assert (res.status_code, res.json()["error"]) == (400, "PUBLIC_KEY_MISSING")
    assert sim.received == {}
    listed = {b["bank_code"]: b for b in (await sender.client.get("/admin/banks", headers=ADMIN)).json()}
    assert listed["902"]["public_key_fingerprint"] is None


# ---- 다른 은행 키 (K4)·허브가 위조할 수 없음 (K5) ----

async def test_k4_other_banks_key_rejected(banks):
    sender, receiver = banks
    sender.private_key = receiver.private_key  # 902의 개인키로 901 명의 요청
    res = await sender.get("/v1/banks")
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")


async def test_k5_hub_db_cannot_forge_bank_request(banks):
    """핵심: 허브 DB의 은행 행에 있는 모든 값(API 키 해시, 공개키, 이전 공개키 …)으로는 은행 명의 서명을 만들 수 없다.
    허브는 은행의 비밀을 하나도 갖지 않는다. 허브 DB가 통째로 새도 은행 명의의 이체는 만들 수 없다는 뜻이다."""
    sender, _ = banks
    _, new_public = generate_keypair()
    assert (await sender.put("/v1/keys", {"public_key": new_public})).status_code == 200  # 이전 키 칸도 채운다
    async with SessionLocal() as db:
        row = await db.scalar(select(Bank).where(Bank.bank_code == "901"))
        columns = {c.key: getattr(row, c.key) for c in inspect(Bank).mapper.column_attrs}
    # 비밀 칸이 없다: 저장된 것은 해시·공개키·설정뿐이다 (8c에서 hmac_secret을 지웠다)
    assert set(columns) == {"bank_code", "name", "base_url", "api_key_hash", "active", "net_debit_cap", "created_at",
                            "public_key", "previous_public_key", "previous_key_expires_at", "public_key_registered_at"}
    assert columns["api_key_hash"] == hash_api_key(sender.api_key) != sender.api_key

    body = _body(transfer_payload("110-123-0001"))
    candidates = [v for v in columns.values() if isinstance(v, str)]
    tried = 0
    for value in candidates:
        try:
            signed = sign_ed25519(value.strip(), "POST", "/v1/transfers", body)  # 그 값을 개인키로 쓴다 (44자 base64만 된다)
        except ValueError:
            continue
        tried += 1
        # 허브가 API 키 원문까지 알고 있다고 해도(로그·요청 가로채기) 서명이 맞지 않는다
        forged = {"X-Bank-Code": "901", "X-Api-Key": sender.api_key, "Content-Type": "application/json", **signed}
        res = await sender.client.post("/v1/transfers", content=body, headers=forged)
        assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID"), value
    assert tried >= 2  # 공개키 둘 (현재·이전)은 개인키 모양이라 실제로 서명까지 해 봤다
    assert sim.received == {}


# ---- 응답 서명 (K6·K7) ----

@pytest.mark.parametrize("broken", ["missing", "wrong_key"])
async def test_k6_bad_reply_signature_goes_pending_then_confirms(banks, monkeypatch, broken):
    """받는 은행이 입금하고 200을 줬지만 응답 서명이 없거나 틀림 → FAILED가 아니라 PENDING (12.4).
    서명을 고친 뒤 4.3 확인으로 COMPLETED, 입금은 1번."""
    sender, _ = banks
    if broken == "missing":
        monkeypatch.delenv("SIM_SIGNING_KEY")
    else:
        monkeypatch.setenv("SIM_SIGNING_KEY", generate_keypair()[0])
    payload = transfer_payload("110-123-0001")
    res = await sender.post("/v1/transfers", payload)
    assert res.status_code == 202 and res.json()["status"] == "PENDING"
    assert payload["transfer_id"] in sim.received  # 받는 은행에는 입금됨
    reply = [e for e in await evidence(sender.client, payload["transfer_id"]) if e["direction"] == "RECEIVE_REPLY"]
    assert reply[0]["http_status"] == 200 and reply[0]["signature_valid"] is False

    monkeypatch.setenv("SIM_SIGNING_KEY", SIM_PRIVATE_KEY)
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["status"] == "COMPLETED" and len(sim.received) == 1


async def test_k6_unsigned_4xx_is_not_a_rejection(banks, monkeypatch):
    """서명 없는 404 ACCOUNT_NOT_FOUND도 믿지 않는다: FAILED로 끝내지 않고 PENDING (이중 지급 방지, SPEC 2.5)."""
    sender, _ = banks
    monkeypatch.delenv("SIM_SIGNING_KEY")
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0002"))
    assert res.status_code == 202 and res.json()["status"] == "PENDING"


async def test_k7_reply_from_other_request_rejected(banks, monkeypatch):
    """앞 거래의 서명된 응답(진짜 902 서명)을 뒤 거래의 응답으로 바꿔치기 → request_signature가 달라 검증 실패 → PENDING."""
    sender, _ = banks
    first = transfer_payload("110-123-0001")
    assert (await sender.post("/v1/transfers", first)).status_code == 200
    old = next(e for e in await evidence(sender.client, first["transfer_id"]) if e["direction"] == "RECEIVE_REPLY")
    assert old["signature_valid"] is True

    def replay(request: httpx.Request) -> httpx.Response:
        return httpx.Response(old["http_status"], headers=old["headers"], content=old["body"].encode())

    async with AsyncClient(transport=httpx.MockTransport(replay)) as replaying:
        monkeypatch.setattr(bank_client_module.bank_client, "_client", replaying)
        second = transfer_payload("110-123-0001")
        res = await sender.post("/v1/transfers", second)
    assert res.status_code == 202 and res.json()["status"] == "PENDING"  # 200 COMPLETED를 믿지 않는다
    reply = next(e for e in await evidence(sender.client, second["transfer_id"]) if e["direction"] == "RECEIVE_REPLY")
    assert reply["signature_valid"] is False


async def test_receiver_rejects_hub_signature(banks, monkeypatch):
    """가짜 은행이 허브 공개키를 잘못 가짐 → 서명된 401 → 입금 전 거절 FAILED(RECEIVER_ERROR), 돈은 안 움직임."""
    sender, _ = banks
    monkeypatch.setenv("SIM_HUB_PUBLIC_KEY", generate_keypair()[1])
    payload = transfer_payload("110-123-0001")
    res = await sender.post("/v1/transfers", payload)
    assert (res.status_code, res.json()["error"], res.json()["status"]) == (400, "RECEIVER_ERROR", "FAILED")
    assert sim.received == {}


async def test_inquiry_bad_reply_signature_is_receiver_error(banks, monkeypatch):
    sender, _ = banks
    monkeypatch.delenv("SIM_SIGNING_KEY")
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})
    assert (res.status_code, res.json()["error"]) == (502, "RECEIVER_ERROR")


async def test_ping_shows_reply_signature(banks, monkeypatch):
    sender, _ = banks
    res = (await sender.client.post("/admin/banks/902/ping", headers=ADMIN)).json()
    assert (res["ok"], res["status"], res["response_signature_valid"]) == (True, 200, True)
    monkeypatch.delenv("SIM_SIGNING_KEY")
    res = (await sender.client.post("/admin/banks/902/ping", headers=ADMIN)).json()
    assert (res["ok"], res["reachable"], res["status"], res["response_signature_valid"]) == (False, True, 200, False)
    assert res["body"]["holder_name"] == "김철수"  # 서명이 틀려도 받은 본문은 보여 준다 (고칠 곳 찾기)


async def test_sim_rejects_non_ed25519(banks):
    """가짜 은행도 Ed25519만 받는다: X-Signature-Alg: hmac이면 401, 4xx에도 응답 서명."""
    body = b'{"account_num":"110-123-0001"}'
    headers = {"X-Bank-Code": "000", "Content-Type": "application/json", **sign_ed25519(HUB_PRIVATE_KEY, "POST", "/inquiry", body)}
    async with AsyncClient(transport=ASGITransport(app=sim.app), base_url="http://sim") as sim_client:
        ok = await sim_client.post("/interbank/inquiry", content=body, headers=headers)
        res = await sim_client.post("/interbank/inquiry", content=body, headers={**headers, "X-Signature-Alg": "hmac"})
    assert ok.status_code == 200
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")
    assert verify_response(SIM_PUBLIC_KEY, res.headers["x-response-timestamp"], 401, headers["X-Signature"], res.content,
                           res.headers["x-response-signature"]) is None


async def test_sim_requires_signing_key_at_startup(monkeypatch):
    """가짜 은행도 서버로 띄울 때 SIM_SIGNING_KEY가 없거나 틀리면 시작하지 않는다."""
    for value in (None, "x"):
        if value is None:
            monkeypatch.delenv("SIM_SIGNING_KEY")
        else:
            monkeypatch.setenv("SIM_SIGNING_KEY", value)
        with pytest.raises(ValueError):
            async with sim.app.router.lifespan_context(sim.app):
                pass
    monkeypatch.setenv("SIM_SIGNING_KEY", SIM_PRIVATE_KEY)
    async with sim.app.router.lifespan_context(sim.app):
        pass


# ---- 키 회전 (K8)·자기 것만 (K10) ----

async def test_k8_previous_key_accepted_for_24_hours(banks):
    sender, _ = banks
    old_private = sender.private_key
    new_private, new_public = generate_keypair()
    res = await sender.put("/v1/keys", {"public_key": new_public})  # 지금(옛) 키로 서명
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["fingerprint"] == fingerprint(new_public) and body["previous_fingerprint"] == fingerprint(public_key_of(old_private))
    expires = datetime.strptime(body["previous_key_expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert timedelta(hours=23) < expires - datetime.now(UTC) <= timedelta(hours=24)

    assert (await sender.get("/v1/banks")).status_code == 200  # 옛 키 (24시간 안)
    sender.use_key(new_private)
    assert (await sender.get("/v1/banks")).status_code == 200  # 새 키

    async with SessionLocal() as db, db.begin():
        await db.execute(update(Bank).where(Bank.bank_code == "901")
                         .values(previous_key_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    sender.use_key(old_private)
    res = await sender.get("/v1/banks")
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")
    sender.use_key(new_private)
    assert (await sender.get("/v1/banks")).status_code == 200


async def test_k8_reply_signed_with_previous_key_accepted(banks, monkeypatch):
    """받는 은행이 키를 바꾼 직후 아직 옛 키로 응답에 서명해도 24시간 안에는 맞는 응답으로 본다."""
    _, receiver = banks
    _, new_public = generate_keypair()
    assert (await receiver.put("/v1/keys", {"public_key": new_public})).status_code == 200
    res = (await receiver.client.post("/admin/banks/902/ping", headers=ADMIN)).json()  # 가짜 은행은 아직 옛 키(SIM)로 서명
    assert (res["ok"], res["response_signature_valid"]) == (True, True)


async def test_k10_put_keys_changes_only_own_bank(banks):
    sender, receiver = banks
    before = (await sender.get("/v1/keys")).json()
    other_private, other_public = generate_keypair()
    # 902가 자기 인증으로 부르면 902의 키만 바뀐다 (본문에 은행 코드를 넣어도 무시)
    res = await receiver.put("/v1/keys", {"public_key": other_public, "bank_code": "901"})
    assert res.status_code == 200 and res.json()["bank_code"] == "902"
    assert (await sender.get("/v1/keys")).json()["fingerprint"] == before["fingerprint"]
    # 902의 개인키로 901 명의를 대면 서명이 틀린다
    body = _body({"public_key": other_public})
    headers = {**receiver.headers("PUT", "/v1/keys", body), "X-Bank-Code": "901", "X-Api-Key": sender.api_key}
    res = await sender.client.put("/v1/keys", content=body, headers=headers)
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")
    assert (await sender.get("/v1/keys")).json()["fingerprint"] == before["fingerprint"]


async def test_key_status_and_validation(banks):
    sender, _ = banks
    status = (await sender.get("/v1/keys")).json()
    assert status["fingerprint"] == fingerprint(sender.public_key) and status["registered_at"]  # 등록 때 넣은 키
    assert status["previous_key_expires_at"] is None and "signature_mode" not in status
    assert status["hub_fingerprint"] == fingerprint(HUB_PUBLIC_KEY)
    for bad in ("not-base64", "AAAA", "A" * 44, sender.public_key[:-4] + "===="):
        res = await sender.put("/v1/keys", {"public_key": bad})
        assert (res.status_code, res.json()["error"]) == (422, "VALIDATION_ERROR"), bad
    res = await sender.put("/v1/keys", {"public_key": sender.public_key})  # 같은 키를 다시 넣으면 그대로
    assert res.status_code == 200 and res.json()["previous_key_expires_at"] is None


# ---- 관리자 ----

async def test_admin_public_key_and_api_key_rotation(banks):
    """관리자 공개키 대신 등록(이전 키 24시간), API 키 재발급(공개키는 그대로, 옛 API 키는 바로 401)."""
    sender, _ = banks
    new_private, new_public = generate_keypair()
    res = await sender.client.post("/admin/banks/901/public-key", json={"public_key": new_public}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["public_key_fingerprint"] == fingerprint(new_public)
    assert "signature_mode" not in res.json()
    assert (await sender.client.post("/admin/banks/901/public-key", json={"public_key": "x"}, headers=ADMIN)).status_code == 422
    assert (await sender.get("/v1/banks")).status_code == 200  # 옛 키도 24시간
    sender.use_key(new_private)

    res = await sender.client.post("/admin/banks/901/keys", headers=ADMIN)
    assert res.status_code == 200 and "secret" not in res.json()
    assert res.json()["public_key_fingerprint"] == fingerprint(new_public)
    old_api_key, sender.api_key = sender.api_key, res.json()["api_key"]
    assert (await sender.get("/v1/banks")).status_code == 200
    sender.api_key = old_api_key
    assert (await sender.get("/v1/banks")).json()["error"] == "UNAUTHORIZED"
    # 서명 모드 API는 없다
    res = await sender.client.patch("/admin/banks/901/signature-mode", json={"signature_mode": "hmac"}, headers=ADMIN)
    assert res.status_code == 404


# ---- 증거 (K9) ----

async def test_k9_evidence_raw_bytes_and_signatures(banks):
    sender, receiver = banks
    payload = transfer_payload("110-123-0001")
    sent = _body(payload)
    assert (await sender.post("/v1/transfers", payload)).status_code == 200
    rows = await evidence(sender.client, payload["transfer_id"])
    assert [r["direction"] for r in rows] == ["REQUEST_IN", "RECEIVE_OUT", "RECEIVE_REPLY"]
    req, out, reply = rows

    # 접수 요청: 보낸 바이트 그대로, 901 공개키로 다시 검증된다. API 키는 남기지 않는다
    assert req["body"].encode() == sent and req["body_encoding"] == "utf-8"
    assert (req["method"], req["path"], req["signature_alg"], req["signature_valid"]) == ("POST", "/v1/transfers", "ed25519", True)
    assert "X-Api-Key" not in req["headers"]
    h = req["headers"]
    assert verify_ed25519(sender.public_key, h["X-Timestamp"], req["method"], req["path"], req["body"].encode(),
                          h["X-Signature"], max_skew=None) is None
    # 허브 → 902 요청: 허브 공개키로 검증된다 (base_url 뒤의 경로)
    h = out["headers"]
    assert (out["method"], out["path"], out["signature_alg"]) == ("POST", "/receive", "ed25519")
    assert json.loads(out["body"])["transfer_id"] == payload["transfer_id"]
    assert verify_ed25519(HUB_PUBLIC_KEY, h["X-Timestamp"], "POST", "/receive", out["body"].encode(), h["X-Signature"],
                          max_skew=None) is None
    # 902 응답: 902 공개키 + 짝 요청의 X-Signature로 검증된다 (분쟁 때 "입금 완료라고 답했다"의 증거)
    assert (reply["http_status"], reply["signature_alg"], reply["signature_valid"]) == (200, "ed25519", True)
    assert verify_response(receiver.public_key, reply["headers"]["X-Response-Timestamp"], 200, h["X-Signature"],
                           reply["body"].encode(), reply["headers"]["X-Response-Signature"], max_skew=None) is None

    # 멱등 재전송·수취 조회는 증거를 더하지 않는다
    assert (await sender.post("/v1/transfers", payload)).status_code == 200
    assert (await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})).status_code == 200
    assert len(await evidence(sender.client, payload["transfer_id"])) == 3
    res = await sender.client.get("/admin/transfers/00000000-0000-4000-8000-000000000000/evidence", headers=ADMIN)
    assert (res.status_code, res.json()["error"]) == (404, "TRANSFER_NOT_FOUND")


async def test_evidence_of_retry(banks):
    """0005: 첫 /receive는 응답 없음(요청만), 4.3 확인 요청·응답이 이어서 남는다."""
    sender, _ = banks
    payload = transfer_payload("110-123-0005")
    assert (await sender.post("/v1/transfers", payload)).status_code == 202
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1
    rows = await evidence(sender.client, payload["transfer_id"])
    assert [(r["direction"], r["method"], r["path"] and r["path"].split("/")[1]) for r in rows] == [
        ("REQUEST_IN", "POST", "v1"), ("RECEIVE_OUT", "POST", "receive"),
        ("RECEIVE_OUT", "GET", "receive"), ("RECEIVE_REPLY", None, None),
    ]
    assert rows[-1]["signature_valid"] is True and json.loads(rows[-1]["body"])["status"] == "COMPLETED"


async def test_receiving_bank_with_own_key(banks, monkeypatch):
    """901이 받는 쪽일 때(902 → 901): 허브는 901의 응답 서명을 901 공개키로 검증한다 (SIM 키로 서명하면 PENDING)."""
    sender, receiver = banks
    payload = {**transfer_payload("110-123-0001"), "to_bank": "901"}
    res = await receiver.post("/v1/transfers", payload)  # 가짜 은행이 SIM 키로 서명 → 901 키와 다름
    assert res.status_code == 202 and res.json()["status"] == "PENDING"
    monkeypatch.setenv("SIM_SIGNING_KEY", sender.private_key)
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1
    assert (await receiver.get(f"/v1/transfers/{payload['transfer_id']}")).json()["status"] == "COMPLETED"

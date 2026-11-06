"""부인방지 (SETTLEMENT_PLAN 12절 8a, 12.8 K2~K10): Ed25519 요청·응답 서명, 전환 모드, 키 등록·회전, 증거 보관.

901 = 보내는 은행(테스트의 SignedBank), 902 = 받는 은행(bank_sim). 둘 다 base_url이 가짜 은행이다.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

import bank_sim.main as sim
from clearing_hub.core import bank_client as bank_client_module
from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.core.signing import (
    fingerprint, generate_keypair, sign, verify_ed25519, verify_response,
)
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.transfers.service import confirm_pending
from tests.conftest import ADMIN, HUB_PUBLIC_KEY, SignedBank, transfer_payload

pytestmark = pytest.mark.anyio


async def register_key(bank: SignedBank) -> dict:
    """은행이 자기 공개키를 등록한다 (지금 인증으로 서명한 PUT /v1/keys, SPEC 2.6)."""
    res = await bank.put("/v1/keys", {"public_key": bank.public_key})
    assert res.status_code == 200, res.text
    return res.json()


async def set_mode(client: AsyncClient, bank_code: str, mode: str) -> httpx.Response:
    return await client.patch(f"/admin/banks/{bank_code}/signature-mode", json={"signature_mode": mode}, headers=ADMIN)


def sim_ready(monkeypatch, receiver: SignedBank, mode: str) -> None:
    """가짜 은행의 받는 쪽 준비: 허브 공개키로 검증, 자기 키로 응답 서명 (SPEC 2.6 순서의 2단계)."""
    monkeypatch.setenv("SIM_SIGNING_KEY", receiver.private_key)
    monkeypatch.setenv("SIM_HUB_PUBLIC_KEY", HUB_PUBLIC_KEY)
    monkeypatch.setenv("SIM_SIGNATURE_MODE", mode)


async def switch(bank: SignedBank, mode: str) -> None:
    """키 등록(HMAC 부트스트랩) → 관리자 전환 → 보내는 쪽 Ed25519."""
    if bank.alg != "ed25519":
        await register_key(bank)
    res = await set_mode(bank.client, bank.bank_code, mode)
    assert res.status_code == 200 and res.json()["signature_mode"] == mode, res.text
    bank.use_ed25519()


@pytest.fixture
async def ed25519_banks(banks, monkeypatch):
    """901·902 둘 다 ed25519 모드. 902(가짜 은행)는 both로 받는 쪽을 먼저 열고, 허브를 올린 뒤 ed25519만 받는다."""
    sender, receiver = banks
    sim_ready(monkeypatch, receiver, "both")
    await switch(receiver, "ed25519")
    monkeypatch.setenv("SIM_SIGNATURE_MODE", "ed25519")
    await switch(sender, "ed25519")
    return sender, receiver


@pytest.fixture(params=["both", "ed25519"])
async def mode_banks(request, banks, monkeypatch):
    """기존 이체 시나리오를 다른 모드로 한 번 더 (12.7 8a 완료 기준)."""
    sender, receiver = banks
    sim_ready(monkeypatch, receiver, "both")
    await switch(receiver, request.param)
    monkeypatch.setenv("SIM_SIGNATURE_MODE", request.param)
    await switch(sender, request.param)
    return sender, receiver


async def evidence(client: AsyncClient, transfer_id: str) -> list[dict]:
    res = await client.get(f"/admin/transfers/{transfer_id}/evidence", headers=ADMIN)
    assert res.status_code == 200, res.text
    return res.json()


# ---- 허브 키·응답 서명 ----

async def test_hub_key_endpoint(client, monkeypatch):
    res = await client.get("/v1/hub-key")  # 인증 없음
    assert res.status_code == 200
    assert res.json() == {"algorithm": "ed25519", "public_key": HUB_PUBLIC_KEY, "fingerprint": fingerprint(HUB_PUBLIC_KEY)}
    monkeypatch.setattr(get_settings(), "hub_signing_key", None)
    res = await client.get("/v1/hub-key")
    assert (res.status_code, res.json()["error"]) == (404, "HUB_KEY_MISSING")


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


# ---- 전환 모드 (K2·K3) ----

async def test_hmac_mode_rejects_ed25519(banks):
    """hmac(기본) 모드는 공개키를 등록해도 HMAC만 받는다. 등록이 모드를 바꾸지 않는다."""
    sender, _ = banks
    status = await register_key(sender)
    assert status["signature_mode"] == "hmac" and status["fingerprint"] == fingerprint(sender.public_key)
    res = await sender.use_ed25519().get("/v1/banks")
    assert res.status_code == 401 and res.json()["error"] == "SIGNATURE_INVALID" and "hmac 모드" in res.json()["message"]


async def test_k2_both_accepts_hmac_and_ed25519(banks):
    sender, _ = banks
    await register_key(sender)
    assert (await set_mode(sender.client, "901", "both")).status_code == 200
    assert (await sender.get("/v1/banks")).status_code == 200  # HMAC
    assert (await sender.use_ed25519().get("/v1/banks")).status_code == 200  # Ed25519


async def test_k3_ed25519_mode_rejects_hmac(ed25519_banks):
    sender, _ = ed25519_banks
    assert (await sender.get("/v1/banks")).status_code == 200
    sender.alg = "hmac"
    res = await sender.get("/v1/banks")
    assert res.status_code == 401 and res.json()["error"] == "SIGNATURE_INVALID" and "ed25519 모드" in res.json()["message"]


async def test_unknown_algorithm_rejected(ed25519_banks):
    sender, _ = ed25519_banks
    headers = {**sender.headers("GET", "/v1/banks", b""), "X-Signature-Alg": "rsa"}
    res = await sender.client.get("/v1/banks", headers=headers)
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")


async def test_k4_other_banks_key_rejected(ed25519_banks):
    sender, receiver = ed25519_banks
    sender.private_key = receiver.private_key  # 902의 개인키로 901 명의 요청
    res = await sender.get("/v1/banks")
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")


async def test_k5_hub_cannot_forge_ed25519_bank_request(ed25519_banks):
    """핵심: 허브는 은행의 HMAC secret을 갖고 있지만, ed25519 모드 은행 명의의 요청은 만들 수 없다."""
    sender, _ = ed25519_banks
    async with SessionLocal() as db:
        hub_held_secret = (await db.get(Bank, "901")).hmac_secret  # 허브 DB에 있는 secret
    body = json.dumps(transfer_payload("110-123-0001"), separators=(",", ":")).encode()
    forged = {"X-Bank-Code": "901", "X-Api-Key": sender.api_key, "Content-Type": "application/json",
              **sign(hub_held_secret, "POST", "/v1/transfers", body)}
    res = await sender.client.post("/v1/transfers", content=body, headers=forged)
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")
    # 알고리즘 헤더만 ed25519로 바꿔도 HMAC 값은 Ed25519 서명이 아니다
    res = await sender.client.post("/v1/transfers", content=body, headers={**forged, "X-Signature-Alg": "ed25519"})
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")
    assert sim.received == {}

    # 비교: hmac 모드였다면 같은 위조 요청이 통과했다 (12.1의 위협)
    assert (await set_mode(sender.client, "901", "hmac")).status_code == 200
    forged = {**forged, **sign(hub_held_secret, "POST", "/v1/transfers", body)}
    assert (await sender.client.post("/v1/transfers", content=body, headers=forged)).status_code == 200


# ---- 응답 서명 (K6·K7) ----

@pytest.mark.parametrize("broken", ["missing", "wrong_key"])
async def test_k6_bad_reply_signature_goes_pending_then_confirms(ed25519_banks, monkeypatch, broken):
    """받는 은행이 입금하고 200을 줬지만 응답 서명이 없거나 틀림 → FAILED가 아니라 PENDING (12.4).
    서명을 고친 뒤 4.3 확인으로 COMPLETED, 입금은 1번."""
    sender, receiver = ed25519_banks
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

    monkeypatch.setenv("SIM_SIGNING_KEY", receiver.private_key)
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["status"] == "COMPLETED" and len(sim.received) == 1


async def test_k7_reply_from_other_request_rejected(ed25519_banks, monkeypatch):
    """앞 거래의 서명된 응답(진짜 902 서명)을 뒤 거래의 응답으로 바꿔치기 → request_signature가 달라 검증 실패 → PENDING."""
    sender, _ = ed25519_banks
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


async def test_hmac_mode_reply_signature_not_required(banks, monkeypatch):
    """hmac 모드 은행의 응답은 서명이 없어도 그대로 믿는다 (기존 은행). 있으면 검증 결과만 남긴다."""
    sender, receiver = banks
    payload = transfer_payload("110-123-0001")
    assert (await sender.post("/v1/transfers", payload)).status_code == 200
    reply = next(e for e in await evidence(sender.client, payload["transfer_id"]) if e["direction"] == "RECEIVE_REPLY")
    assert reply["signature_valid"] is None and reply["signature_alg"] is None

    # 공개키를 등록해 두고 서명을 붙이면 (모드는 hmac 그대로) 검증해 True로 남긴다. 틀려도 이체는 그대로 COMPLETED
    await register_key(receiver)
    monkeypatch.setenv("SIM_SIGNING_KEY", receiver.private_key)
    payload = transfer_payload("110-123-0001")
    assert (await sender.post("/v1/transfers", payload)).status_code == 200
    reply = next(e for e in await evidence(sender.client, payload["transfer_id"]) if e["direction"] == "RECEIVE_REPLY")
    assert reply["signature_valid"] is True
    monkeypatch.setenv("SIM_SIGNING_KEY", generate_keypair()[0])
    payload = transfer_payload("110-123-0001")
    assert (await sender.post("/v1/transfers", payload)).status_code == 200
    reply = next(e for e in await evidence(sender.client, payload["transfer_id"]) if e["direction"] == "RECEIVE_REPLY")
    assert reply["signature_valid"] is False


async def test_receiver_rejects_hub_signature_in_ed25519(ed25519_banks, monkeypatch):
    """가짜 은행이 허브 공개키를 잘못 가짐 → 서명된 401 → 입금 전 거절 FAILED(RECEIVER_ERROR), 돈은 안 움직임."""
    sender, _ = ed25519_banks
    monkeypatch.setenv("SIM_HUB_PUBLIC_KEY", generate_keypair()[1])
    payload = transfer_payload("110-123-0001")
    res = await sender.post("/v1/transfers", payload)
    assert (res.status_code, res.json()["error"], res.json()["status"]) == (400, "RECEIVER_ERROR", "FAILED")
    assert sim.received == {}


async def test_inquiry_bad_reply_signature_is_receiver_error(ed25519_banks, monkeypatch):
    sender, _ = ed25519_banks
    monkeypatch.delenv("SIM_SIGNING_KEY")
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})
    assert (res.status_code, res.json()["error"]) == (502, "RECEIVER_ERROR")


async def test_ping_shows_reply_signature(ed25519_banks, monkeypatch):
    sender, _ = ed25519_banks
    res = (await sender.client.post("/admin/banks/902/ping", headers=ADMIN)).json()
    assert (res["status"], res["signature_mode"], res["response_signature_valid"]) == (200, "ed25519", True)
    monkeypatch.delenv("SIM_SIGNING_KEY")
    res = (await sender.client.post("/admin/banks/902/ping", headers=ADMIN)).json()
    assert (res["reachable"], res["status"], res["response_signature_valid"]) == (True, 200, False)


# ---- 키 회전 (K8)·자기 것만 (K10) ----

async def test_k8_previous_key_accepted_for_24_hours(ed25519_banks):
    sender, _ = ed25519_banks
    old_private = sender.private_key
    new_private, new_public = generate_keypair()
    res = await sender.put("/v1/keys", {"public_key": new_public})  # 지금(옛) 키로 서명
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["fingerprint"] == fingerprint(new_public) and body["previous_fingerprint"]
    expires = datetime.strptime(body["previous_key_expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert timedelta(hours=23) < expires - datetime.now(UTC) <= timedelta(hours=24)

    assert (await sender.get("/v1/banks")).status_code == 200  # 옛 키 (24시간 안)
    sender.use_ed25519(new_private)
    assert (await sender.get("/v1/banks")).status_code == 200  # 새 키

    async with SessionLocal() as db, db.begin():
        await db.execute(update(Bank).where(Bank.bank_code == "901")
                         .values(previous_key_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    sender.use_ed25519(old_private)
    res = await sender.get("/v1/banks")
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")
    sender.use_ed25519(new_private)
    assert (await sender.get("/v1/banks")).status_code == 200


async def test_k10_put_keys_changes_only_own_bank(banks):
    sender, receiver = banks
    await register_key(sender)
    before = (await sender.get("/v1/keys")).json()
    other_private, other_public = generate_keypair()
    # 902가 자기 인증으로 부르면 902의 키만 바뀐다 (본문에 은행 코드를 넣어도 무시)
    res = await receiver.put("/v1/keys", {"public_key": other_public, "bank_code": "901"})
    assert res.status_code == 200 and res.json()["bank_code"] == "902"
    assert (await sender.get("/v1/keys")).json()["fingerprint"] == before["fingerprint"]
    # 902의 secret으로 901 명의를 대면 서명이 틀린다
    body = json.dumps({"public_key": other_public}, separators=(",", ":")).encode()
    headers = {**receiver.headers("PUT", "/v1/keys", body), "X-Bank-Code": "901", "X-Api-Key": sender.api_key}
    res = await sender.client.put("/v1/keys", content=body, headers=headers)
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")
    assert (await sender.get("/v1/keys")).json()["fingerprint"] == before["fingerprint"]


async def test_key_status_and_validation(banks):
    sender, _ = banks
    status = (await sender.get("/v1/keys")).json()
    assert status["fingerprint"] is None and status["signature_mode"] == "hmac"
    assert status["hub_fingerprint"] == fingerprint(HUB_PUBLIC_KEY)
    for bad in ("not-base64", "AAAA", "A" * 44, sender.public_key[:-4] + "===="):
        res = await sender.put("/v1/keys", {"public_key": bad})
        assert (res.status_code, res.json()["error"]) == (422, "VALIDATION_ERROR"), bad
    status = await register_key(sender)
    assert status["registered_at"] and status["previous_key_expires_at"] is None
    assert (await register_key(sender))["previous_key_expires_at"] is None  # 같은 키를 다시 넣으면 그대로


# ---- 관리자 ----

async def test_admin_signature_mode_rules(banks, monkeypatch):
    sender, _ = banks
    res = await set_mode(sender.client, "901", "ed25519")
    assert (res.status_code, res.json()["error"]) == (400, "PUBLIC_KEY_MISSING")
    assert (await set_mode(sender.client, "901", "both")).status_code == 200  # 공개키 없이도 both는 된다 (HMAC 계속)
    assert (await set_mode(sender.client, "901", "rsa")).status_code == 422

    _, public = generate_keypair()
    res = await sender.client.post("/admin/banks/901/public-key", json={"public_key": public}, headers=ADMIN)
    assert res.status_code == 200
    assert res.json()["public_key_fingerprint"] == fingerprint(public) and res.json()["signature_mode"] == "both"
    assert (await sender.client.post("/admin/banks/901/public-key", json={"public_key": "x"}, headers=ADMIN)).status_code == 422

    hub_key = get_settings().hub_signing_key
    monkeypatch.setattr(get_settings(), "hub_signing_key", None)
    res = await set_mode(sender.client, "901", "ed25519")
    assert (res.status_code, res.json()["error"]) == (400, "HUB_KEY_MISSING")
    monkeypatch.setattr(get_settings(), "hub_signing_key", hub_key)
    assert (await set_mode(sender.client, "901", "ed25519")).status_code == 200
    assert (await set_mode(sender.client, "901", "hmac")).status_code == 200  # 내리는 것은 언제든 (유출 대응)

    # HMAC 키 재발급은 공개키·모드를 바꾸지 않는다
    res = await sender.client.post("/admin/banks/901/keys", headers=ADMIN)
    assert res.json()["public_key_fingerprint"] == fingerprint(public) and res.json()["signature_mode"] == "hmac"
    listed = {b["bank_code"]: b for b in (await sender.client.get("/admin/banks", headers=ADMIN)).json()}
    assert listed["901"]["public_key_fingerprint"] == fingerprint(public) and listed["902"]["public_key_fingerprint"] is None


# ---- 증거 (K9) ----

async def test_k9_evidence_raw_bytes_and_signatures(ed25519_banks):
    sender, receiver = ed25519_banks
    payload = transfer_payload("110-123-0001")
    sent = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
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


async def test_evidence_of_retry(ed25519_banks):
    """0005: 첫 /receive는 응답 없음(요청만), 4.3 확인 요청·응답이 이어서 남는다."""
    sender, _ = ed25519_banks
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


async def test_hmac_bank_evidence(banks):
    """hmac 모드에서도 접수 요청·호출 원문은 남는다 (alg hmac). 기존 은행에는 바뀌는 것이 없다."""
    sender, _ = banks
    payload = transfer_payload("110-123-0001")
    assert (await sender.post("/v1/transfers", payload)).status_code == 200
    rows = await evidence(sender.client, payload["transfer_id"])
    assert [(r["direction"], r["signature_alg"]) for r in rows] == [
        ("REQUEST_IN", "hmac"), ("RECEIVE_OUT", "hmac"), ("RECEIVE_REPLY", None)]


# ---- 기존 이체 시나리오를 both·ed25519 모드로 (12.7 8a 완료 기준) ----

async def test_modes_inquiry(mode_banks):
    sender, _ = mode_banks
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})
    assert res.status_code == 200 and res.json()["holder_name"] == "김철수"
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0002"})
    assert (res.status_code, res.json()["error"]) == (404, "ACCOUNT_NOT_FOUND")


async def test_modes_completed_and_idempotent(mode_banks):
    sender, _ = mode_banks
    payload = transfer_payload("110-123-0001")
    first = await sender.post("/v1/transfers", payload)
    assert first.status_code == 200 and first.json()["holder_name"] == "김철수"
    second = await sender.post("/v1/transfers", {**payload, "amount": 99999})
    assert second.status_code == 200 and len(sim.received) == 1
    assert sim.received[payload["transfer_id"]]["amount"] == 50000
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["status"] == "COMPLETED"


async def test_modes_account_not_found(mode_banks):
    sender, _ = mode_banks
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0002"))
    assert (res.status_code, res.json()["error"], res.json()["status"]) == (404, "ACCOUNT_NOT_FOUND", "FAILED")
    assert sim.received == {}


async def test_modes_timeout_then_retry(mode_banks, monkeypatch):
    sender, _ = mode_banks
    payload = transfer_payload("110-123-0003")
    assert (await sender.post("/v1/transfers", payload)).status_code == 202
    sim.received.clear()
    monkeypatch.setenv("SIM_DELAY_SECONDS", "0")
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1
    assert (await sender.get(f"/v1/transfers/{payload['transfer_id']}")).json()["status"] == "COMPLETED"
    assert len(sim.received) == 1


async def test_modes_received_but_no_answer(mode_banks):
    sender, _ = mode_banks
    payload = transfer_payload("110-123-0005")
    assert (await sender.post("/v1/transfers", payload)).status_code == 202
    assert payload["transfer_id"] in sim.received
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1
    detail = (await sender.get(f"/v1/transfers/{payload['transfer_id']}")).json()
    assert detail["status"] == "COMPLETED" and detail["holder_name"] == "김철수" and len(sim.received) == 1


async def test_sim_rejects_hmac_in_ed25519_mode(banks, monkeypatch):
    """가짜 은행을 ed25519로 두고 허브는 아직 hmac이면 가짜 은행이 HMAC을 401로 거절한다 (전환 순서를 지켜야 하는 이유)."""
    sender, receiver = banks
    sim_ready(monkeypatch, receiver, "ed25519")
    sim_client = AsyncClient(transport=ASGITransport(app=sim.app), base_url="http://sim")
    body = b'{"account_num":"110-123-0001"}'
    async with sim_client:
        res = await sim_client.post("/interbank/inquiry", content=body,
                                    headers={"X-Bank-Code": "000", **sign(receiver.secret, "POST", "/inquiry", body)})
    assert (res.status_code, res.json()["error"]) == (401, "SIGNATURE_INVALID")
    assert res.headers["x-response-signature"]  # 4xx에도 응답 서명

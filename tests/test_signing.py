import json
import time

import pytest

from clearing_hub.core.signing import (
    fingerprint, generate_keypair, public_key_of, sign, sign_ed25519, sign_response, verify, verify_ed25519, verify_response,
)
from tests.conftest import ADMIN, register_bank

# SPEC 2.3·2.5 검증용 고정 키 쌍 (예시 전용. 운영에서는 쓰지 않는다). 다른 팀이 자기 구현을 이 값으로 확인한다
EXAMPLE_BANK_PRIVATE = "D7frsUqSl9ZuC+7EpHfY1+w6+dAlWqg8vuHVtZfdpg4="
EXAMPLE_BANK_PUBLIC = "gxCDPKbN/rIXKDJgn4GZh5JY7I65Cwbk3A/mJjTtPzY="
EXAMPLE_HUB_PRIVATE = "cycR/h4ydSTPkBGuRFO340cDtdAdwrXQjKC4N1GPgtQ="
EXAMPLE_HUB_PUBLIC = "yZdBWoR7swp/3CQvh4c58oahhbjxVeaxWWtqmqwDXdo="
EXAMPLE_TID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _example1_body() -> bytes:
    return json.dumps(
        {"transfer_id": EXAMPLE_TID, "to_bank": "902", "to_account": "110-123-0001",
         "from_account": "110-200-456789", "amount": 50000, "sender_name": "홍길동", "memo": "생활비"},
        ensure_ascii=False, separators=(",", ":"),
    ).encode()


def test_spec_example_signature():
    """SPEC 2.3의 검증용 예시 값과 같아야 한다 (다른 팀이 이 값으로 자기 구현을 확인한다)."""
    body = json.dumps(
        {"transfer_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "to_bank": "902", "to_account": "110-123-0001",
         "from_account": "110-200-456789", "amount": 50000, "sender_name": "홍길동", "memo": "생활비"},
        ensure_ascii=False, separators=(",", ":"),
    ).encode()
    assert len(body) == 190
    headers = sign("example-secret-change-me", "POST", "/v1/transfers", body, timestamp=1762300800)
    assert headers["X-Signature"] == "1afb099015e80126587211e3a2ef82bfbb9c0a4af2d9065519029985e7797f65"
    get = sign("example-secret-change-me", "GET", "/receive/7c9e6679-7425-40de-944b-e07fc1f90ae7", b"", 1762300800)
    assert get["X-Signature"] == "d1c18e703107fcf4132c301e468014f1487099ec302b0571806694972ea5bc89"


def test_spec_example_ed25519_signatures():
    """K1: SPEC 2.3 예시 1·2의 Ed25519 서명과 2.5 응답 서명 예시 값 (Ed25519는 같은 키·메시지면 서명이 늘 같다)."""
    assert public_key_of(EXAMPLE_BANK_PRIVATE) == EXAMPLE_BANK_PUBLIC
    assert public_key_of(EXAMPLE_HUB_PRIVATE) == EXAMPLE_HUB_PUBLIC
    assert fingerprint(EXAMPLE_BANK_PUBLIC) == "d61a340fe933017d"
    assert fingerprint(EXAMPLE_HUB_PUBLIC) == "239a9acfc7ecb8de"

    # 예시 1: 은행 → 허브 이체 요청, 은행 개인키로 서명
    body = _example1_body()
    e1 = sign_ed25519(EXAMPLE_BANK_PRIVATE, "POST", "/v1/transfers", body, timestamp=1762300800)
    assert e1 == {"X-Timestamp": "1762300800", "X-Signature-Alg": "ed25519", "X-Signature":
                  "SY/3NCjTxAR1dC5UXHM/PkQZR9/SMa+wcDBSIrAit0jeHW5JKKgw4vZWKiN2Z7o5V+ywCY7EyWsTEfLf/F3mBw=="}
    assert verify_ed25519(EXAMPLE_BANK_PUBLIC, "1762300800", "POST", "/v1/transfers", body, e1["X-Signature"],
                          max_skew=None) is None

    # 예시 2: 허브 → 은행 입금 확인 GET, 허브 개인키로 서명
    path = f"/receive/{EXAMPLE_TID}"
    e2 = sign_ed25519(EXAMPLE_HUB_PRIVATE, "GET", path, b"", timestamp=1762300800)
    assert e2["X-Signature"] == "IMn6yCKkn/eZGPNY74b3+cNvA/4ax9knE97hh4wCQnPEHe6IEdKREh/Pit3+7bJJjx801OiRbJIvJD0HbKzPAA=="
    assert verify_ed25519(EXAMPLE_HUB_PUBLIC, "1762300800", "GET", path, b"", e2["X-Signature"], max_skew=None) is None

    # 예시 3 (SPEC 2.5): 예시 2에 대한 은행의 200 응답, 은행 개인키로 서명. request_signature는 예시 2의 X-Signature
    reply = json.dumps({"transfer_id": EXAMPLE_TID, "status": "COMPLETED", "holder_name": "김철수",
                        "received_at": "2026-11-05T01:20:02Z"}, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(reply) == 138
    e3 = sign_response(EXAMPLE_BANK_PRIVATE, 200, e2["X-Signature"], reply, timestamp=1762300801)
    assert e3 == {"X-Response-Timestamp": "1762300801", "X-Signature-Alg": "ed25519", "X-Response-Signature":
                  "D84tooOZEJ6jehp/BN6dVPw1KZCB+eejxpZOqgggeTW7iJznoyNjQGEFwShbhnX7prHkg9uG9e7rP9IxZS91Dg=="}
    assert verify_response(EXAMPLE_BANK_PUBLIC, "1762300801", 200, e2["X-Signature"], reply,
                           e3["X-Response-Signature"], max_skew=None) is None
    # 같은 응답을 다른 요청(예시 1)에 붙이면 틀린다 (K7과 같은 성질)
    assert verify_response(EXAMPLE_BANK_PUBLIC, "1762300801", 200, e1["X-Signature"], reply,
                           e3["X-Response-Signature"], max_skew=None) == "SIGNATURE_INVALID"


def test_keypair_and_fingerprint():
    private, public = generate_keypair()
    assert len(private) == len(public) == 44 and private != public
    assert public_key_of(private) == public
    assert len(fingerprint(public)) == 16 and int(fingerprint(public), 16) >= 0
    signed = sign_ed25519(private, "POST", "/v1/inquiry", b"{}")
    assert len(signed["X-Signature"]) == 88
    with pytest.raises(ValueError):
        fingerprint("not-a-key")


@pytest.mark.parametrize(
    ("public_key", "signature"),
    [
        ("not base64!!", None),  # 공개키가 base64 아님
        ("AAAA", None),  # 길이가 다름 (3바이트)
        ("A" * 43 + "=", None),  # 형식(44자, 32바이트)은 맞지만 쓸 수 없는 키(거의 0) → 예외 없이 INVALID
        (None, None),  # 공개키 없음
        (EXAMPLE_BANK_PUBLIC, "서명"),  # 비 ASCII 서명
        (EXAMPLE_BANK_PUBLIC, "QUJD"),  # 서명 길이가 다름
        (EXAMPLE_BANK_PUBLIC, "!" * 88),  # 88자지만 base64 아님
        (EXAMPLE_BANK_PUBLIC, ""),  # 빈 서명
    ],
)
def test_ed25519_bad_input_is_invalid_not_exception(public_key, signature):
    """잘못된 키·서명 입력은 예외(500) 대신 SIGNATURE_INVALID (401)."""
    body = _example1_body()
    good = sign_ed25519(EXAMPLE_BANK_PRIVATE, "POST", "/v1/transfers", body)
    sig = good["X-Signature"] if signature is None else signature
    assert verify_ed25519(public_key, good["X-Timestamp"], "POST", "/v1/transfers", body, sig) == "SIGNATURE_INVALID"
    assert verify_response(public_key, good["X-Timestamp"], 200, "", body, sig) == "SIGNATURE_INVALID"


def test_ed25519_rejects_tamper_and_skew():
    body = b'{"a":1}'
    h = sign_ed25519(EXAMPLE_BANK_PRIVATE, "POST", "/v1/inquiry", body)
    ts, sig = h["X-Timestamp"], h["X-Signature"]
    assert verify_ed25519(EXAMPLE_BANK_PUBLIC, ts, "POST", "/v1/inquiry", body, sig) is None
    assert verify_ed25519(EXAMPLE_BANK_PUBLIC, ts, "POST", "/v1/inquiry", b'{"a":2}', sig) == "SIGNATURE_INVALID"
    assert verify_ed25519(EXAMPLE_BANK_PUBLIC, ts, "POST", "/v1/transfers", body, sig) == "SIGNATURE_INVALID"
    assert verify_ed25519(EXAMPLE_HUB_PUBLIC, ts, "POST", "/v1/inquiry", body, sig) == "SIGNATURE_INVALID"  # 다른 키
    assert verify_ed25519(EXAMPLE_BANK_PUBLIC, "abc", "POST", "/v1/inquiry", body, sig) == "SIGNATURE_INVALID"
    old = sign_ed25519(EXAMPLE_BANK_PRIVATE, "POST", "/v1/inquiry", body, timestamp=int(time.time()) - 600)
    assert verify_ed25519(EXAMPLE_BANK_PUBLIC, old["X-Timestamp"], "POST", "/v1/inquiry", body,
                          old["X-Signature"]) == "SIGNATURE_EXPIRED"
    # 응답 서명: 상태 코드도 메시지에 들어간다
    r = sign_response(EXAMPLE_BANK_PRIVATE, 200, sig, body)
    ts2, rsig = r["X-Response-Timestamp"], r["X-Response-Signature"]
    assert verify_response(EXAMPLE_BANK_PUBLIC, ts2, 200, sig, body, rsig) is None
    assert verify_response(EXAMPLE_BANK_PUBLIC, ts2, 404, sig, body, rsig) == "SIGNATURE_INVALID"
    assert verify_response(EXAMPLE_BANK_PUBLIC, None, 200, sig, body, rsig) == "SIGNATURE_INVALID"  # 헤더 없음


def test_verify_rejects_tamper_and_skew():
    body = b'{"a":1}'
    h = sign("s", "POST", "/v1/inquiry", body)
    ts, sig = h["X-Timestamp"], h["X-Signature"]
    assert verify("s", ts, "POST", "/v1/inquiry", body, sig) is None
    assert verify("s", ts, "post", "/v1/inquiry", body, sig) is None  # 메서드는 대문자로 맞춰 비교
    assert verify("s", ts, "POST", "/v1/inquiry", b'{"a":2}', sig) == "SIGNATURE_INVALID"
    assert verify("other", ts, "POST", "/v1/inquiry", body, sig) == "SIGNATURE_INVALID"
    assert verify("s", ts, "PUT", "/v1/inquiry", body, sig) == "SIGNATURE_INVALID"
    assert verify("s", ts, "POST", "/v1/transfers", body, sig) == "SIGNATURE_INVALID"
    assert verify("s", ts, "POST", "/v1/inquiry", body, "서명") == "SIGNATURE_INVALID"  # 비 ASCII도 500이 아니라 401
    old = sign("s", "POST", "/v1/inquiry", body, timestamp=int(time.time()) - 600)
    assert verify("s", old["X-Timestamp"], "POST", "/v1/inquiry", body, old["X-Signature"]) == "SIGNATURE_EXPIRED"


def test_get_signature_is_bound_to_path():
    """본문 없는 GET도 경로가 다르면 서명이 다르다 (v0.5 전에는 같은 초의 GET 서명이 모두 같았다)."""
    a = sign("s", "GET", "/receive/aaaa", b"", timestamp=1762300800)
    assert verify("s", a["X-Timestamp"], "GET", "/receive/bbbb", b"", a["X-Signature"], max_skew=10**10) == "SIGNATURE_INVALID"


@pytest.mark.anyio
async def test_bank_auth(client):
    bank = await register_bank(client, "901", "AvengersBank")
    res = await bank.get("/v1/banks")
    assert res.status_code == 200 and res.json()["banks"][0]["bank_code"] == "901"

    # 헤더 없음, 틀린 키, 오래된 시각, 본문 변조
    assert (await client.get("/v1/banks")).status_code == 401
    bad = await client.get("/v1/banks", headers={**bank.headers("GET", "/v1/banks", b""), "X-Api-Key": "chk_wrong"})
    assert bad.json()["error"] == "UNAUTHORIZED"
    old = await client.get("/v1/banks", headers=bank.headers("GET", "/v1/banks", b"", timestamp=int(time.time()) - 1000))
    assert old.json()["error"] == "SIGNATURE_EXPIRED"
    tampered = await client.post("/v1/inquiry", content=b'{"to_bank":"902","to_account":"x"}',
                                 headers=bank.headers("POST", "/v1/inquiry", b'{"to_bank":"902","to_account":"y"}'))
    assert tampered.json()["error"] == "SIGNATURE_INVALID"

    # 비활성 은행
    await client.patch("/admin/banks/901/active", params={"active": "false"}, headers=ADMIN)
    assert (await bank.get("/v1/banks")).json()["error"] == "BANK_INACTIVE"


@pytest.mark.anyio
async def test_admin_token_required(client):
    assert (await client.get("/admin/banks")).status_code == 401
    assert (await client.get("/admin/banks", headers=ADMIN)).status_code == 200

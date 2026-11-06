import json
import time

import pytest

from clearing_hub.core.signing import sign, verify
from tests.conftest import ADMIN, register_bank


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

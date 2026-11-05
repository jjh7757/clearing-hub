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
    headers = sign("example-secret-change-me", body, timestamp=1762300800)
    assert headers["X-Signature"] == "a05b6c21278364c75bd36df9cbedbbca72e241466da38d7aa2ee3a1d0725a8c7"


def test_verify_rejects_tamper_and_skew():
    body = b'{"a":1}'
    headers = sign("s", body)
    assert verify("s", headers["X-Timestamp"], body, headers["X-Signature"]) is None
    assert verify("s", headers["X-Timestamp"], b'{"a":2}', headers["X-Signature"]) == "SIGNATURE_INVALID"
    assert verify("other", headers["X-Timestamp"], body, headers["X-Signature"]) == "SIGNATURE_INVALID"
    old = sign("s", body, timestamp=int(time.time()) - 600)
    assert verify("s", old["X-Timestamp"], body, old["X-Signature"]) == "SIGNATURE_EXPIRED"


@pytest.mark.anyio
async def test_bank_auth(client):
    bank = await register_bank(client, "901", "AvengersBank")
    res = await bank.get("/v1/banks")
    assert res.status_code == 200 and res.json()["banks"][0]["bank_code"] == "901"

    # 헤더 없음, 틀린 키, 오래된 시각, 본문 변조
    assert (await client.get("/v1/banks")).status_code == 401
    bad = await client.get("/v1/banks", headers={**bank.headers(b""), "X-Api-Key": "chk_wrong"})
    assert bad.json()["error"] == "UNAUTHORIZED"
    old = await client.get("/v1/banks", headers=bank.headers(b"", timestamp=int(time.time()) - 1000))
    assert old.json()["error"] == "SIGNATURE_EXPIRED"
    tampered = await client.post("/v1/inquiry", content=b'{"to_bank":"902","to_account":"x"}',
                                 headers=bank.headers(b'{"to_bank":"902","to_account":"y"}'))
    assert tampered.json()["error"] == "SIGNATURE_INVALID"

    # 비활성 은행
    await client.patch("/admin/banks/901/active", params={"active": "false"}, headers=ADMIN)
    assert (await bank.get("/v1/banks")).json()["error"] == "BANK_INACTIVE"


@pytest.mark.anyio
async def test_admin_token_required(client):
    assert (await client.get("/admin/banks")).status_code == 401
    assert (await client.get("/admin/banks", headers=ADMIN)).status_code == 200

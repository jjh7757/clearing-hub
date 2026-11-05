"""SPEC 준수 점검에서 찾은 것: 받는 은행 인증 오류, 500의 X-Request-ID, 시각 형식, UUID v4."""

import re
import uuid

import pytest

import bank_sim.main as sim
from clearing_hub.domains.banks import service as bank_service
from tests.conftest import transfer_payload

pytestmark = pytest.mark.anyio

SPEC_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


async def test_receiver_auth_error_is_not_passed_as_sender_signature_error(banks, monkeypatch):
    """허브에 받는 은행 secret이 잘못 등록된 상황: 받는 은행이 허브 서명을 401로 거절한다.
    보내는 은행에는 자기 서명 오류(401 SIGNATURE_INVALID)처럼 보이면 안 된다 (SPEC 3.2, 5절)."""
    sender, _ = banks
    monkeypatch.setenv("SIM_HUB_SECRET", "not-the-secret-the-hub-uses")

    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})
    assert res.status_code == 502 and res.json()["error"] == "RECEIVER_ERROR"

    payload = transfer_payload("110-123-0001")
    res = await sender.post("/v1/transfers", payload)
    body = res.json()
    assert res.status_code == 400 and body["status"] == "FAILED"  # 입금 전 거절 → 보내는 은행은 되돌린다
    assert body["error"] == "RECEIVER_ERROR" and "SIGNATURE" not in body["error"]
    assert payload["transfer_id"] not in sim.received  # 돈은 들어가지 않았다
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["fail_code"] == "RECEIVER_ERROR"


async def test_unexpected_error_has_request_id(banks, monkeypatch):
    """처리하지 못한 예외의 500도 SPEC 5절 형식이고 X-Request-ID가 붙는다 (SPEC 2.4 "모든 응답")"""
    sender, _ = banks

    async def broken(*args, **kwargs):
        raise RuntimeError("DB 끊김")

    monkeypatch.setattr(bank_service, "list_banks", broken)
    body = b""
    headers = {**sender.headers(body), "X-Request-ID": "probe-500-0001"}
    res = await sender.client.get("/v1/banks", headers=headers)
    assert res.status_code == 500
    assert res.json() == {"error": "INTERNAL_ERROR", "message": "서버에서 오류가 발생했습니다"}
    assert res.headers["x-request-id"] == "probe-500-0001"
    assert "DB 끊김" not in res.text


async def test_times_are_utc_seconds(banks):
    """SPEC 1절: ISO 8601, UTC, 초 단위 ("2026-11-05T01:20:00Z")"""
    sender, _ = banks
    payload = transfer_payload("110-123-0001")
    res = await sender.post("/v1/transfers", payload)
    assert SPEC_TIME.match(res.json()["finished_at"]), res.json()["finished_at"]

    detail = (await sender.get(f"/v1/transfers/{payload['transfer_id']}")).json()
    assert SPEC_TIME.match(detail["requested_at"]) and SPEC_TIME.match(detail["finished_at"])


@pytest.mark.parametrize(
    "transfer_id",
    [
        str(uuid.uuid1()),  # v1
        str(uuid.uuid4()).upper(),  # 대문자
        uuid.uuid4().hex,  # 하이픈 없음
        "not-a-uuid",
    ],
)
async def test_transfer_id_must_be_lowercase_uuid4(banks, transfer_id):
    sender, _ = banks
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", transfer_id=transfer_id))
    assert res.status_code == 422 and res.json()["error"] == "VALIDATION_ERROR"

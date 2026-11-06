"""SPEC 준수 점검에서 찾은 것: 받는 은행 인증 오류, 500의 X-Request-ID, 시각 형식, UUID v4."""

import re
import uuid

import pytest
from sqlalchemy import update

import bank_sim.main as sim
from clearing_hub.core.bank_client import BankClient, BankReply
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.transfers.models import Transfer
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
    headers = {**sender.headers("GET", "/v1/banks", body), "X-Request-ID": "probe-500-0001"}
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


@pytest.mark.parametrize("stored_status", ["ACCEPTED", "DELIVERING", "UNKNOWN"])
async def test_resend_answers_only_spec_statuses(banks, stored_status):
    """3.3 응답은 200 COMPLETED / 202 PENDING / 4xx FAILED 셋뿐. 재전송 때 거래가 그 밖의 상태면 202 PENDING.
    3.4 결과 조회는 실제 상태를 보여 준다."""
    sender, _ = banks
    payload = transfer_payload("110-123-0001")
    assert (await sender.post("/v1/transfers", payload)).status_code == 200
    async with SessionLocal() as db, db.begin():
        await db.execute(update(Transfer).where(Transfer.transfer_id == payload["transfer_id"]).values(status=stored_status))

    res = await sender.post("/v1/transfers", payload)
    assert res.status_code == 202 and res.json()["status"] == "PENDING"
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.json()["status"] == stored_status


@pytest.mark.parametrize(
    ("reply", "http", "code"),
    [
        (BankReply(409, {"error": "DUPLICATE_DEPOSIT", "message": "이미 처리 중"}), 400, "RECEIVER_ERROR"),  # 표에 없는 코드
        (BankReply(422, {"error": "VALIDATION_ERROR", "message": "memo"}), 422, "VALIDATION_ERROR"),  # 표의 HTTP 422
        (BankReply(400, {"error": "ACCOUNT_INACTIVE", "message": "정지"}), 400, "ACCOUNT_INACTIVE"),
        (BankReply(400, {}), 400, "RECEIVER_ERROR"),  # 5절 형식이 아닌 본문 (BankClient가 dict가 아닌 본문을 {}로 바꾼다)
    ],
)
async def test_receiver_rejection_uses_spec_table(banks, monkeypatch, reply, http, code):
    """받는 은행의 거절은 5절 표의 코드와 그 코드의 HTTP 상태로 보내는 은행에 전달한다"""
    sender, _ = banks

    async def rejecting(self, bank, payload, request_id=None):
        return reply

    monkeypatch.setattr(BankClient, "receive", rejecting)
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001"))
    assert (res.status_code, res.json()["error"], res.json()["status"]) == (http, code, "FAILED")


async def test_inquiry_rejection_uses_spec_table(banks, monkeypatch):
    sender, _ = banks

    async def rejecting(self, bank, account_num, request_id=None):
        return BankReply(409, {"error": "SOMETHING_ELSE", "message": "x"})

    monkeypatch.setattr(BankClient, "inquiry", rejecting)
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"})
    assert (res.status_code, res.json()["error"]) == (400, "RECEIVER_ERROR")


async def test_framework_errors_use_spec_codes(client):
    """허용하지 않는 메서드도 표의 404 NOT_FOUND ("없는 경로"). METHOD_NOT_ALLOWED·HTTP_ERROR 같은 표 밖 코드는 없다"""
    res = await client.delete("/v1/banks")
    assert (res.status_code, res.json()["error"]) == (404, "NOT_FOUND")
    res = await client.get("/v1/nothing-here")
    assert (res.status_code, res.json()["error"]) == (404, "NOT_FOUND")


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

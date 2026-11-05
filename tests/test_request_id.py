"""X-Request-ID (SPEC 2.4): 받기·만들기·응답에 붙이기·받는 은행에 전달·재시도에도 같은 값."""

import json
import os

import pytest

import bank_sim.main as sim
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.transfers.service import confirm_pending
from tests.conftest import transfer_payload

pytestmark = pytest.mark.anyio


async def post_with_id(bank, path: str, payload: dict, request_id: str | None):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = bank.headers(body)
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return await bank.client.post(path, content=body, headers=headers)


def forwarded(path_suffix: str) -> list[str | None]:
    return [rid for path, rid in sim.seen_request_ids if path.endswith(path_suffix)]


async def test_request_id_is_echoed_and_forwarded(banks):
    sender, _ = banks
    res = await post_with_id(sender, "/v1/transfers", transfer_payload("110-123-0001"), "avb-req-0001")
    assert res.status_code == 200, res.text
    assert res.headers["x-request-id"] == "avb-req-0001"  # 보내는 은행이 준 값 그대로
    assert forwarded("/receive") == ["avb-req-0001"]  # 받는 은행에도 그대로

    res = await post_with_id(sender, "/v1/inquiry", {"to_bank": "902", "to_account": "110-123-0001"}, "avb-req-0002")
    assert res.headers["x-request-id"] == "avb-req-0002"
    assert forwarded("/inquiry") == ["avb-req-0002"]


async def test_missing_or_invalid_request_id_is_generated(banks):
    sender, _ = banks
    res = await post_with_id(sender, "/v1/transfers", transfer_payload("110-123-0001"), None)
    generated = res.headers["x-request-id"]
    assert generated and forwarded("/receive") == [generated]

    # 형식이 틀리면 거절하지 않고 새로 만든다 (추적용이라 돈의 흐름에 영향이 없다)
    res = await post_with_id(sender, "/v1/transfers", transfer_payload("110-123-0001"), "bad id with spaces")
    assert res.status_code == 200
    assert res.headers["x-request-id"] != "bad id with spaces"


async def test_retry_reuses_transfer_request_id(banks):
    """허브 재시도 태스크는 요청 밖에서 돌지만, 거래에 저장한 처음 추적 ID를 다시 보낸다"""
    sender, _ = banks
    res = await post_with_id(sender, "/v1/transfers", transfer_payload("110-123-0003"), "avb-req-retry")
    assert res.status_code == 202

    sim.seen_request_ids.clear()
    os.environ["SIM_DELAY_SECONDS"] = "0"
    try:
        async with SessionLocal() as db:
            assert await confirm_pending(db) == 1
    finally:
        os.environ["SIM_DELAY_SECONDS"] = "1"
    # 입금 확인(4.3) → 없음 → 재전송(4.2). 둘 다 처음 값
    assert {rid for _, rid in sim.seen_request_ids} == {"avb-req-retry"}
    assert forwarded("/receive") == ["avb-req-retry"]


async def test_request_id_not_part_of_signature(banks):
    """서명을 만든 뒤 X-Request-ID를 붙이거나 바꿔도 서명 검증은 그대로 통과한다"""
    sender, _ = banks
    res = await post_with_id(sender, "/v1/transfers", transfer_payload("110-123-0001"), "changed-after-signing")
    assert res.status_code == 200

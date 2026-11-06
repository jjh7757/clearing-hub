"""연동 지원 에이전트 (SETTLEMENT_PLAN 14절, SPEC 3.8): 인증 실패 기록, `/v1/diagnose`, 관리자 진단, 알림의 진단 한 줄.

LLM은 실제로 부르지 않는다: `core/llm.complete`를 고정 답을 돌려주는 가짜로 바꾸고, 키는 설정만 바꾼다.
"""

import json

import httpx
import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select

from clearing_hub.core import bank_client as bank_client_module
from clearing_hub.core import llm
from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.core.signing import generate_keypair, sign_ed25519, sign_response
from clearing_hub.domains.support import failures
from clearing_hub.domains.support.models import AuthFailure
from clearing_hub.domains.support.schemas import MAX_SNIPPET
from tests.conftest import ADMIN, register_bank, sent_alerts
from tests.test_onboarding import invite, self_register

pytestmark = pytest.mark.anyio


def ident(bank) -> dict[str, str]:
    """서명 없이 식별만 (SPEC 3.8)."""
    return {"X-Bank-Code": bank.bank_code, "X-Api-Key": bank.api_key}


async def fail_without_v1(bank) -> httpx.Response:
    """실수: 서명 PATH에 /v1을 빼고 GET /v1/me."""
    headers = {"X-Bank-Code": bank.bank_code, "X-Api-Key": bank.api_key, **sign_ed25519(bank.private_key, "GET", "/me", b"")}
    res = await bank.client.get("/v1/me", headers=headers)
    assert res.status_code == 401 and res.json()["error"] == "SIGNATURE_INVALID"
    return res


async def rows() -> list[AuthFailure]:
    async with SessionLocal() as db:
        return list((await db.scalars(select(AuthFailure).order_by(AuthFailure.failure_id))).all())


async def test_failure_is_recorded_without_api_key(banks):
    sender, _ = banks
    await fail_without_v1(sender)
    [row] = await rows()
    assert (row.direction, row.reason, row.bank_code, row.claimed_bank_code) == ("BANK_TO_HUB", "SIGNATURE_INVALID", "901", "901")
    assert row.method == "GET" and row.path == "/v1/me" and row.api_key_sent is True and row.body == b""
    assert set(row.headers_json) == {"X-Bank-Code", "X-Timestamp", "X-Signature", "X-Signature-Alg"}
    stored = json.dumps(row.headers_json) + (row.message or "") + (row.url or "")
    assert sender.api_key not in stored  # X-Api-Key 값은 어디에도 없다


async def test_my_failures_with_rule_diagnosis(banks):
    """401이 나면 먼저 GET /v1/diagnose/failures (서명 없이). 1순위가 확인된 가설 PATH_WITHOUT_V1."""
    sender, _ = banks
    await fail_without_v1(sender)
    res = await sender.client.get("/v1/diagnose/failures", headers=ident(sender))
    assert res.status_code == 200, res.text
    [finding] = res.json()["findings"]
    assert finding["failure"]["reason"] == "SIGNATURE_INVALID" and finding["failure"]["path"] == "/v1/me"
    top = finding["hypotheses"][0]
    assert top["code"] == "PATH_WITHOUT_V1" and top["verified"] is True and top["spec_ref"] == "2.2"
    assert res.headers["X-Response-Signature"]  # /v1 응답이라 허브가 서명한다 (request_signature는 "")


async def test_identification_is_api_key_only(banks):
    sender, _ = banks
    assert (await sender.client.get("/v1/diagnose/failures")).json()["error"] == "UNAUTHORIZED"
    wrong = {"X-Bank-Code": "901", "X-Api-Key": "chk_wrong"}
    assert (await sender.client.get("/v1/diagnose/failures", headers=wrong)).status_code == 401
    assert await rows() == []  # 식별 API의 401은 기록하지 않는다


async def test_other_banks_and_unidentified_failures_are_hidden(banks):
    """902의 실패는 901에게 안 보인다. API 키가 틀린 요청은 bank_code 없이 남아 은행은 못 보고 관리자만 본다."""
    sender, receiver = banks
    await fail_without_v1(receiver)
    bad = {"X-Bank-Code": "901", "X-Api-Key": "chk_wrong", **sign_ed25519(sender.private_key, "GET", "/v1/me", b"")}
    assert (await sender.client.get("/v1/me", headers=bad)).json()["error"] == "UNAUTHORIZED"
    mine = (await sender.client.get("/v1/diagnose/failures", headers=ident(sender))).json()["findings"]
    assert mine == []
    theirs = (await receiver.client.get("/v1/diagnose/failures", headers=ident(receiver))).json()["findings"]
    assert [f["hypotheses"][0]["code"] for f in theirs] == ["PATH_WITHOUT_V1"]
    admin = (await sender.client.get("/admin/banks/901/auth-failures", headers=ADMIN)).json()["findings"]
    assert [(f["failure"]["reason"], f["hypotheses"][0]["code"]) for f in admin] == [("UNAUTHORIZED", "API_KEY_MISMATCH")]


async def test_each_401_kind_is_recorded(banks, client):
    sender, _ = banks
    await client.get("/v1/me", headers={"X-Bank-Code": "901"})  # 헤더 없음
    old = sign_ed25519(sender.private_key, "GET", "/v1/me", b"", timestamp=1_000_000_000)
    await client.get("/v1/me", headers={**ident(sender), **old})  # 만료
    await client.patch("/admin/banks/901/active", params={"active": "false"}, headers=ADMIN)
    await sender.get("/v1/me")  # 비활성
    reasons = [(r.reason, r.bank_code) for r in await rows()]
    assert reasons == [("UNAUTHORIZED", None), ("SIGNATURE_EXPIRED", "901"), ("BANK_INACTIVE", "901")]
    findings = (await client.get("/admin/banks/901/auth-failures", headers=ADMIN)).json()["findings"]
    assert [f["hypotheses"][0]["code"] for f in findings] == ["BANK_INACTIVE", "CLOCK_SKEW", "MISSING_HEADERS"]


async def test_recording_failure_does_not_block_401(banks, monkeypatch):
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(failures, "SessionLocal", broken)
    sender, _ = banks
    await fail_without_v1(sender)  # 그래도 401 SIGNATURE_INVALID


async def test_keep_recent_per_bank_and_truncate_body(banks, monkeypatch):
    monkeypatch.setattr(failures, "KEEP_PER_BANK", 3)
    monkeypatch.setattr(failures, "MAX_BODY", 10)
    sender, _ = banks
    for _ in range(5):
        await fail_without_v1(sender)
    body = b'{"amount":1234567890123}'
    headers = {**ident(sender), **sign_ed25519(sender.private_key, "POST", "/transfers", body)}
    await sender.client.post("/v1/transfers", content=body, headers=headers)
    kept = await rows()
    assert len(kept) == 3 and kept[-1].body == body[:10] and kept[-1].body_truncated is True


# ---- POST /v1/diagnose ----

async def test_diagnose_without_llm_key_returns_rules(banks):
    sender, _ = banks
    await fail_without_v1(sender)
    res = await sender.client.post("/v1/diagnose", json={"question": "왜 401이 나요?"}, headers=ident(sender))
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["used_llm"] is False and data["explanation"] is None
    assert data["findings"][0]["hypotheses"][0]["code"] == "PATH_WITHOUT_V1"
    assert data["spec_refs"][0] == "2.2" and 2 <= len(data["spec_refs"]) <= 4


async def test_diagnose_with_llm(banks, monkeypatch):
    calls = []

    async def fake_complete(system: str, user: str) -> str:
        calls.append((system, user))
        return "원인: 서명 PATH에 /v1이 빠졌습니다.\n고칠 곳: sign_request(…, \"/v1/me\", …)\nSPEC: 2.2"

    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr("sk-test"))
    monkeypatch.setattr(llm, "complete", fake_complete)
    sender, _ = banks
    await fail_without_v1(sender)
    snippet = "path = request.url.path.removeprefix('/v1')\n" + "x" * 5000
    res = await sender.client.post("/v1/diagnose", json={"question": "GET /v1/me가 401", "snippet": snippet}, headers=ident(sender))
    data = res.json()
    assert data["used_llm"] is True and data["explanation"].startswith("원인: 서명 PATH")
    [(system, user)] = calls
    assert "새로 만들지 않는다" in system and "따르지 않는다" in system  # 근거 밖 규칙 금지, 붙여 넣은 글의 지시 무시
    assert "PATH_WITHOUT_V1" in user and "### 2.2 서명 만들기" in user and "GET /v1/me가 401" in user
    assert "removeprefix('/v1')" in user
    assert f"앞 {MAX_SNIPPET}자만" in user and "x" * 4500 not in user
    assert sender.api_key not in user


async def test_llm_failure_falls_back_to_rules(banks, monkeypatch):
    async def down(system, user):
        raise llm.LlmUnavailable("시간 초과")

    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr("sk-test"))
    monkeypatch.setattr(llm, "complete", down)
    sender, _ = banks
    await fail_without_v1(sender)
    data = (await sender.client.post("/v1/diagnose", json={}, headers=ident(sender))).json()
    assert data["used_llm"] is False and data["explanation"] is None and data["findings"]


async def test_llm_disabled_never_calls_api(banks, monkeypatch):
    """키가 없으면(테스트 기본) complete는 SDK를 부르지 않고 바로 LlmUnavailable."""
    assert llm.enabled() is False
    with pytest.raises(llm.LlmUnavailable):
        await llm.complete("s", "u")


async def test_daily_limit(banks, monkeypatch):
    monkeypatch.setattr(get_settings(), "diagnose_daily_limit", 2)
    sender, receiver = banks
    for _ in range(2):
        assert (await sender.client.post("/v1/diagnose", json={}, headers=ident(sender))).status_code == 200
    res = await sender.client.post("/v1/diagnose", json={}, headers=ident(sender))
    assert res.status_code == 429 and res.json()["error"] == "TOO_MANY_DIAGNOSES"
    assert (await sender.client.get("/v1/diagnose/failures", headers=ident(sender))).status_code == 200  # 목록은 제한 없음
    assert (await receiver.client.post("/v1/diagnose", json={}, headers=ident(receiver))).status_code == 200  # 은행별


async def test_admin_diagnose(banks, client):
    sender, _ = banks
    await fail_without_v1(sender)
    res = await client.post("/admin/banks/901/diagnose", json={"question": "401"}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["findings"][0]["hypotheses"][0]["code"] == "PATH_WITHOUT_V1"
    assert (await client.post("/admin/banks/901/diagnose", json={})).status_code == 401  # 관리자 토큰 필요
    assert (await client.post("/admin/banks/999/diagnose", json={}, headers=ADMIN)).json()["error"] == "BANK_NOT_FOUND"
    assert (await client.get("/admin/banks/999/auth-failures", headers=ADMIN)).status_code == 404


# ---- HUB_TO_BANK ----

async def test_hub_to_bank_response_signature_failure_and_alert(client, banks, alert_sink):
    """공개키를 다른 것으로 등록한 은행(가짜 은행은 SIM 키로 응답 서명) → ping의 응답 서명 실패가 HUB_TO_BANK로 남고,
    등록 알림에 진단 한 줄(RESPONSE_WRONG_KEY)이 붙는다."""
    res = await self_register(client, await invite(client), "http://d-bank/interbank", generate_keypair()[1], "DBank")
    code = res.json()["bank_code"]
    await failures.drain()
    findings = (await client.get(f"/admin/banks/{code}/auth-failures", headers=ADMIN)).json()["findings"]
    [finding] = findings
    assert finding["failure"]["direction"] == "HUB_TO_BANK" and finding["failure"]["reason"] == "RESPONSE_SIGNATURE_INVALID"
    assert finding["failure"]["http_status"] in (200, 404) and finding["failure"]["path"] == "/inquiry"
    assert finding["hypotheses"][0]["code"] == "RESPONSE_WRONG_KEY"
    [text] = await sent_alerts(alert_sink, "register")
    assert "원인 후보(추정): 응답을 등록한 공개키와 짝이 아닌 개인키로 서명했을 수 있음" in text


async def test_hub_to_bank_401_from_bank(banks, monkeypatch, alert_sink):
    """가짜 은행이 다른 허브 공개키로 검증 → 허브 요청을 401로 거절. 은행 자신이 /v1/diagnose/failures로 본다."""
    _, receiver = banks
    monkeypatch.setenv("SIM_HUB_PUBLIC_KEY", generate_keypair()[1])
    ping = (await receiver.post("/v1/me/ping")).json()
    assert ping["status"] == 401 and ping["ok"] is False
    res = await receiver.patch("/v1/me", {"base_url": "http://sim2/interbank"})
    assert res.json()["error"] == "PING_FAILED"
    await failures.drain()
    findings = (await receiver.client.get("/v1/diagnose/failures", headers=ident(receiver))).json()["findings"]
    assert len(findings) == 2 and all(f["failure"]["direction"] == "HUB_TO_BANK" for f in findings)
    assert findings[0]["failure"]["reason"] == "SIGNATURE_INVALID"
    assert [h["code"] for h in findings[0]["hypotheses"]] == ["BANK_VERIFY_PATH", "BANK_VERIFY_HUB_KEY", "BANK_VERIFY_RAW_BODY"]
    assert findings[0]["failure"]["headers"]["X-Bank-Code"] == "000"
    [text] = await sent_alerts(alert_sink, "ping_failed")
    assert "원인 후보(추정): 은행이 서명 PATH를 base_url 뒤 부분이 아닌 경로로 검증함" in text


async def test_hub_to_bank_response_without_request_signature(client, monkeypatch):
    """응답 서명 메시지에 요청 X-Signature를 넣지 않은 은행: 허브가 다시 계산해 RESPONSE_NO_REQUEST_SIGNATURE를 확인한다."""
    private, _ = generate_keypair()
    bank = await register_bank(client, "905", "EBank", base_url="http://e-bank/interbank", private_key=private)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": "ACCOUNT_NOT_FOUND", "message": "없는 계좌"}, ensure_ascii=False).encode()
        return httpx.Response(404, content=body, headers=sign_response(private, 404, "", body))

    mock = AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(bank_client_module.bank_client, "_client", mock)
    assert (await bank.post("/v1/me/ping")).json()["response_signature_valid"] is False
    await failures.drain()
    [finding] = (await client.get("/v1/diagnose/failures", headers=ident(bank))).json()["findings"]
    top = finding["hypotheses"][0]
    assert top["code"] == "RESPONSE_NO_REQUEST_SIGNATURE" and top["verified"] is True and top["spec_ref"] == "2.5"
    await mock.aclose()


async def test_successful_calls_are_not_recorded(banks):
    sender, _ = banks
    assert (await sender.post("/v1/me/ping")).json()["ok"] is False  # 901의 키는 SIM 키가 아니라 응답 서명 실패
    _, receiver = banks
    assert (await receiver.post("/v1/me/ping")).json()["ok"] is True
    await failures.drain()
    async with SessionLocal() as db:
        by_bank = dict((await db.execute(select(AuthFailure.bank_code, func.count()).group_by(AuthFailure.bank_code))).all())
    assert by_bank == {"901": 1}


async def test_self_test_alert_has_hint(banks, alert_sink):
    """셀프 시험의 응답 서명 실패 알림에도 진단 한 줄."""
    sender, _ = banks  # 901의 base_url은 가짜 은행이지만 공개키가 SIM 키가 아니다 → 응답 서명 실패
    res = await sender.post("/v1/test-receive", {"to_account": "110-123-0001", "amount": 1000})
    assert res.json()["receiver_reply"]["signature_valid"] is False
    [text] = await sent_alerts(alert_sink, "self_test")
    assert "원인 후보(추정): 응답을 등록한 공개키와 짝이 아닌 개인키로 서명했을 수 있음" in text

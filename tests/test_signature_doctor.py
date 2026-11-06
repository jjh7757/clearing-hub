"""서명 진단기 단위 테스트 (SETTLEMENT_PLAN 14절, core/signature_doctor.py).

흔한 실수마다 **일부러 그 실수로 서명한 요청**을 만들고, 진단기가 정확히 그 가설을 1순위로, 그리고 다시 계산해 확인한
가설(`verified`)로 내는지 본다. DB는 쓰지 않는다 (진단기는 실패 한 건과 공개키만 받는다).
"""

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from clearing_hub.core.signature_doctor import CATALOG, DoctorContext, diagnose
from clearing_hub.core.signing import fingerprint, generate_keypair, sign_response

PRIVATE, PUBLIC = generate_keypair()
PAYLOAD = {"transfer_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "to_bank": "902", "amount": 50000, "sender_name": "홍길동"}
BODY = json.dumps(PAYLOAD, ensure_ascii=False, separators=(",", ":")).encode()  # 규격대로 보낸 바이트


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def raw_sign(private_b64: str, message: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_b64)).sign(message)


def bank_failure(*, method="POST", path="/v1/transfers", body=BODY, signed_path=None, signed_body=None, signed_method=None,
                 sep="\n", trailing=True, ts=None, signed_ts=None, encode=None, key=PRIVATE, reason="SIGNATURE_INVALID",
                 query=None, url=None, alg="ed25519", headers=None, api_key_sent=True):
    """은행 → 허브 요청 실패 한 건. signed_* 로 서명할 때만 바꾼 값(실수)을 준다. 보낸 것은 method·path·body."""
    ts = str(_now()) if ts is None else ts
    m = signed_method or method
    p = path if signed_path is None else signed_path
    b = body if signed_body is None else signed_body
    message = f"{signed_ts or ts}{sep}{m}{sep}{p}{sep if trailing else ''}".encode() + b
    raw = raw_sign(key, message)
    signature = encode(raw) if encode else base64.b64encode(raw).decode()
    h = {"X-Bank-Code": "903", "X-Timestamp": ts, "X-Signature": signature, "X-Signature-Alg": alg}
    if headers is not None:
        h = headers
    return SimpleNamespace(
        failure_id=1, direction="BANK_TO_HUB", reason=reason, message=None, claimed_bank_code="903", bank_code="903",
        method=method, path=path, query=query, url=url or f"http://hub{path}", headers_json=h, api_key_sent=api_key_sent,
        body=body, body_truncated=False, http_status=None, response_headers_json=None, response_body=None,
        response_signature_valid=None, created_at=datetime.now(UTC),
    )


CTX = DoctorContext(current_key=PUBLIC, base_url="https://c-bank.example.com/interbank",
                    hub_public_url="https://clearing-hub.example.com")


def top_codes(failure, ctx=CTX) -> list[str]:
    return [h.code for h in diagnose(failure, ctx).hypotheses]


def assert_top(failure, code: str, ctx=CTX):
    result = diagnose(failure, ctx)
    assert result.top is not None and result.top.code == code, [(h.code, h.detail) for h in result.hypotheses]
    assert result.top.verified, result.top
    assert result.top.spec_ref and result.top.fix and result.top.title == CATALOG[code][0]
    return result


# ---- PATH ----

def test_path_without_v1():
    result = assert_top(bank_failure(signed_path="/transfers"), "PATH_WITHOUT_V1")
    assert "`/transfers`" in result.top.detail and result.top.spec_ref == "2.2"


def test_path_fix_uses_failed_path():
    """fix의 예는 실패한 실제 경로이고, 특정 언어의 함수 이름(`sign_request`)을 쓰지 않는다 (SPEC 1.2.2)."""
    f = bank_failure(method="GET", path="/v1/banks", body=b"", signed_path="/banks")
    fix = assert_top(f, "PATH_WITHOUT_V1").top.fix
    assert "`/v1/banks`" in fix and "/v1/transfers" not in fix and "sign_request" not in fix
    assert "서명 함수에 넘기는 PATH" in fix
    f = bank_failure(method="GET", path="/v1/settlements", body=b"", signed_path="/v1/settlements/")
    assert "`/v1/settlements`" in assert_top(f, "PATH_TRAILING_SLASH").top.fix
    assert all("{path}" not in h.fix for h in diagnose(f, CTX).hypotheses)  # 자리 표시가 남지 않는다


def test_path_full_url():
    assert_top(bank_failure(signed_path="https://clearing-hub.example.com/v1/transfers"), "PATH_FULL_URL")


def test_path_with_query():
    f = bank_failure(method="GET", path="/v1/settlements", body=b"", signed_path="/v1/settlements?limit=5", query="limit=5")
    assert_top(f, "PATH_WITH_QUERY")


def test_path_trailing_slash():
    assert_top(bank_failure(signed_path="/v1/transfers/"), "PATH_TRAILING_SLASH")


def test_path_bank_prefix():
    assert_top(bank_failure(signed_path="/interbank/v1/transfers"), "PATH_BANK_PREFIX")


# ---- 본문 다시 직렬화 ----

def test_body_default_json_dumps():
    result = assert_top(bank_failure(signed_body=json.dumps(PAYLOAD).encode()), "BODY_DEFAULT_DUMPS")
    assert "보낸 본문" in result.top.detail


def test_body_ensure_ascii():
    assert_top(bank_failure(signed_body=json.dumps(PAYLOAD, separators=(",", ":")).encode()), "BODY_ENSURE_ASCII")


def test_body_spaces():
    assert_top(bank_failure(signed_body=json.dumps(PAYLOAD, ensure_ascii=False).encode()), "BODY_SPACES")


def test_body_sorted_keys():
    sorted_body = json.dumps(PAYLOAD, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    assert sorted_body != BODY
    assert_top(bank_failure(signed_body=sorted_body), "BODY_SORTED_KEYS")


def test_body_signed_as_empty():
    assert_top(bank_failure(signed_body=b""), "BODY_EMPTY_SIGNED")


def test_get_signed_with_body():
    assert_top(bank_failure(method="GET", path="/v1/me", body=b"", signed_body=b"{}"), "BODY_SIGNED_NONEMPTY")


# ---- 구분자·메서드 ----

def test_missing_newline_after_path():
    """2.3 예시 2: 본문이 비어도 경로 뒤 \\n을 넣는다."""
    assert_top(bank_failure(method="GET", path="/v1/me", body=b"", trailing=False), "NO_TRAILING_NEWLINE")


def test_crlf():
    assert_top(bank_failure(sep="\r\n"), "CRLF")


def test_method_lowercase():
    assert_top(bank_failure(signed_method="post"), "METHOD_LOWERCASE")


# ---- 서명 인코딩·키 ----

def test_signature_hex():
    assert_top(bank_failure(encode=lambda raw: raw.hex()), "SIGNATURE_HEX")


def test_signature_base64url():
    for _ in range(50):  # 표준 base64에 + 나 / 가 있는 서명이 나올 때까지 (거의 항상 첫 번째)
        f = bank_failure(encode=lambda raw: base64.urlsafe_b64encode(raw).decode())
        if "-" in f.headers_json["X-Signature"] or "_" in f.headers_json["X-Signature"]:
            break
        f.headers_json["X-Timestamp"] = str(int(f.headers_json["X-Timestamp"]) + 1)
    assert_top(f, "SIGNATURE_BASE64URL")


def test_signature_without_padding():
    assert_top(bank_failure(encode=lambda raw: base64.b64encode(raw).decode().rstrip("=")), "SIGNATURE_NO_PADDING")


def test_signature_not_ed25519():
    f = bank_failure()
    f.headers_json["X-Signature"] = base64.b64encode(b"x" * 32).decode()  # HMAC-SHA256 길이
    assert_top(f, "SIGNATURE_FORMAT")


def test_old_key_after_expiry():
    old_private, old_public = generate_keypair()
    ctx = DoctorContext(current_key=PUBLIC, previous_key=old_public, previous_valid=False)
    result = assert_top(bank_failure(key=old_private), "OLD_KEY_EXPIRED", ctx)
    assert fingerprint(old_public) in result.top.detail


def test_wrong_key_when_nothing_matches():
    other_private, _ = generate_keypair()
    result = diagnose(bank_failure(key=other_private), CTX)
    assert [h.code for h in result.hypotheses] == ["WRONG_KEY", "MESSAGE_DIFFERENT"]
    assert not result.top.verified and fingerprint(PUBLIC) in result.top.detail


def test_two_mistakes_at_once():
    """`/v1` 빠짐 + json.dumps 기본값: 둘 다 확인된 가설로."""
    f = bank_failure(signed_path="/transfers", signed_body=json.dumps(PAYLOAD).encode())
    assert top_codes(f)[:2] == ["PATH_WITHOUT_V1", "BODY_DEFAULT_DUMPS"]
    assert all(h.verified for h in diagnose(f, CTX).hypotheses)


def test_alg_header_and_missing_key():
    assert top_codes(bank_failure(alg="hmac-sha256"))[0] == "ALG_UNSUPPORTED"
    assert top_codes(bank_failure(), DoctorContext())[-1] == "PUBLIC_KEY_MISSING"


def test_spec_example_1_is_valid_signature():
    """SPEC 2.3 예시 1 값 그대로면 서명 자체는 맞다 (NOW_VALID). 진단기가 규격 메시지를 정확히 만든다는 확인."""
    body = ('{"transfer_id":"7c9e6679-7425-40de-944b-e07fc1f90ae7","to_bank":"902","to_account":"110-123-0001",'
            '"from_account":"110-200-456789","amount":50000,"sender_name":"홍길동","memo":"생활비"}').encode()
    headers = {"X-Bank-Code": "903", "X-Timestamp": "1762300800",
               "X-Signature": "SY/3NCjTxAR1dC5UXHM/PkQZR9/SMa+wcDBSIrAit0jeHW5JKKgw4vZWKiN2Z7o5V+ywCY7EyWsTEfLf/F3mBw=="}
    f = bank_failure(body=body, headers=headers)
    assert_top(f, "NOW_VALID", DoctorContext(current_key="gxCDPKbN/rIXKDJgn4GZh5JY7I65Cwbk3A/mJjTtPzY="))


# ---- 시각·식별 ----

def test_clock_skew_seconds():
    f = bank_failure(ts=str(_now() + 420), reason="SIGNATURE_EXPIRED")
    result = assert_top(f, "CLOCK_SKEW")
    assert "빠름" in result.top.detail and "서명 계산 자체는 맞다" in result.top.detail
    slow = diagnose(bank_failure(ts=str(_now() - 900), reason="SIGNATURE_EXPIRED"), CTX)
    assert [h.code for h in slow.hypotheses] == ["CLOCK_SKEW", "STALE_SIGNATURE"] and "느림" in slow.top.detail


def test_timestamp_in_milliseconds():
    result = assert_top(bank_failure(ts=str(_now() * 1000), reason="SIGNATURE_EXPIRED"), "TIMESTAMP_MILLIS")
    assert "밀리초" in result.top.detail


def test_unauthorized_missing_headers_and_key_mismatch():
    f = bank_failure(reason="UNAUTHORIZED", api_key_sent=False)
    del f.headers_json["X-Signature"]
    result = diagnose(f, CTX)
    assert result.top.code == "MISSING_HEADERS" and "X-Api-Key" in result.top.detail and "X-Signature" in result.top.detail
    assert top_codes(bank_failure(reason="UNAUTHORIZED")) == ["API_KEY_MISMATCH"]
    assert top_codes(bank_failure(reason="UNAUTHORIZED"), DoctorContext(claimed_bank_exists=False))[0] == "BANK_CODE_UNKNOWN"
    assert top_codes(bank_failure(reason="BANK_INACTIVE")) == ["BANK_INACTIVE"]


# ---- HUB_TO_BANK ----

BANK_PRIVATE, BANK_PUBLIC = generate_keypair()
REQUEST_SIGNATURE = base64.b64encode(b"r" * 64).decode()


def hub_failure(status=404, reason="RESPONSE_SIGNATURE_INVALID", response_headers=None, response_body=None, message=None):
    response_body = response_body if response_body is not None else '{"error":"ACCOUNT_NOT_FOUND","message":"없음"}'.encode()
    return SimpleNamespace(
        failure_id=2, direction="HUB_TO_BANK", reason=reason, message=message, claimed_bank_code=None, bank_code="903",
        method="POST", path="/inquiry", query=None, url="https://c-bank.example.com/interbank/inquiry",
        headers_json={"X-Bank-Code": "000", "X-Timestamp": str(_now()), "X-Signature": REQUEST_SIGNATURE,
                      "X-Signature-Alg": "ed25519"},
        api_key_sent=None, body=b'{"account_num":"000-000-000000"}', body_truncated=False, http_status=status,
        response_headers_json=response_headers or {}, response_body=response_body,
        response_signature_valid=False if reason == "RESPONSE_SIGNATURE_INVALID" else True, created_at=datetime.now(UTC),
    )


HUB_CTX = DoctorContext(current_key=BANK_PUBLIC, hub_fingerprint="239a9acfc7ecb8de")


def test_response_without_request_signature():
    body = b'{"error":"ACCOUNT_NOT_FOUND","message":"\xec\x97\x86\xec\x9d\x8c"}'
    headers = sign_response(BANK_PRIVATE, 404, "", body)  # 실수: 요청의 X-Signature 대신 ""
    result = assert_top(hub_failure(response_headers=headers, response_body=body), "RESPONSE_NO_REQUEST_SIGNATURE", HUB_CTX)
    assert result.top.spec_ref == "2.5"


def test_response_body_redumped():
    body = '{"error":"ACCOUNT_NOT_FOUND","message":"없음"}'.encode()
    signed = json.dumps(json.loads(body)).encode()  # 은행이 다시 직렬화한 본문으로 서명
    headers = sign_response(BANK_PRIVATE, 404, REQUEST_SIGNATURE, signed)
    assert_top(hub_failure(response_headers=headers, response_body=body), "RESPONSE_BODY_REDUMPED", HUB_CTX)


def test_response_signature_missing_and_wrong_key():
    result = assert_top(hub_failure(response_headers={}), "RESPONSE_SIGNATURE_MISSING", HUB_CTX)
    assert "HTTP 404" in result.top.detail
    other_private, _ = generate_keypair()
    headers = sign_response(other_private, 404, REQUEST_SIGNATURE, hub_failure().response_body)
    wrong = diagnose(hub_failure(response_headers=headers), HUB_CTX)
    assert wrong.top.code == "RESPONSE_WRONG_KEY" and not wrong.top.verified


@pytest.mark.parametrize(("status", "reason", "first"), [
    (401, "SIGNATURE_INVALID", "BANK_VERIFY_PATH"),
    (401, "SIGNATURE_EXPIRED", "BANK_CLOCK"),
    (401, "UNAUTHORIZED", "BANK_HEADER_CHECK"),
    (403, "HTTP_403", "BANK_FORBIDDEN"),
])
def test_bank_rejected_hub_request(status, reason, first):
    body = json.dumps({"error": reason, "message": "x"}).encode()
    headers = sign_response(BANK_PRIVATE, status, REQUEST_SIGNATURE, body)
    codes = top_codes(hub_failure(status=status, reason=reason, response_headers=headers, response_body=body), HUB_CTX)
    assert codes[0] == first
    if reason == "SIGNATURE_INVALID":
        assert codes == ["BANK_VERIFY_PATH", "BANK_VERIFY_HUB_KEY", "BANK_VERIFY_RAW_BODY"]


def test_one_line_and_never_raises():
    result = diagnose(bank_failure(signed_path="/transfers"), CTX)
    assert result.one_line().startswith("원인 후보(확인됨): 서명 PATH에 `/v1`이 빠짐") and "SPEC 2.2" in result.one_line()
    broken = SimpleNamespace(failure_id=None, direction="BANK_TO_HUB", reason="SIGNATURE_INVALID")  # 칸이 모자라도
    assert diagnose(broken, CTX).top.code == "UNKNOWN_REASON"

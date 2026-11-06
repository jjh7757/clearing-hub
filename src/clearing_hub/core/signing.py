"""SPEC 2절: HMAC-SHA256 요청 서명. 은행 → 허브, 허브 → 은행 모두 같은 방식.

message   = X-Timestamp + "\\n" + METHOD + "\\n" + PATH + "\\n" + body(전송 바이트 그대로)
signature = hex(HMAC-SHA256(secret, message))

METHOD는 대문자. PATH는 규격에 정한 경로다 (SPEC 2.2): 허브 API는 `/v1/...`,
은행 API는 base_url 뒤의 부분(`/inquiry`, `/receive/{transfer_id}` 등). 쿼리스트링은 넣지 않는다.
"""

import hashlib
import hmac
import secrets
import time

HUB_BANK_CODE = "000"  # 허브가 은행을 부를 때의 X-Bank-Code


def _digest(secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    message = f"{timestamp}\n{method.upper()}\n{path}\n".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign(secret: str, method: str, path: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    return {"X-Timestamp": ts, "X-Signature": _digest(secret, ts, method, path, body)}


def verify(
    secret: str, timestamp: str, method: str, path: str, body: bytes, signature: str, max_skew: int = 300
) -> str | None:
    """맞으면 None, 틀리면 오류 코드(SIGNATURE_EXPIRED / SIGNATURE_INVALID)."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return "SIGNATURE_INVALID"
    if abs(int(time.time()) - ts) > max_skew:
        return "SIGNATURE_EXPIRED"
    expected = _digest(secret, timestamp, method, path, body)
    try:
        return None if hmac.compare_digest(expected.encode(), (signature or "").encode("ascii")) else "SIGNATURE_INVALID"
    except UnicodeEncodeError:  # 비 ASCII 서명 헤더는 예외(500) 대신 401
        return "SIGNATURE_INVALID"


def new_api_key() -> str:
    return "chk_" + secrets.token_urlsafe(24)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()

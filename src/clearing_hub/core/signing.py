"""SPEC 2절: Ed25519 요청 서명(2.2·2.3)과 응답 서명(2.5). 서명 방식은 Ed25519 하나다 (v1.0, HMAC은 8c에서 뺐다).

요청 message = X-Timestamp + "\\n" + METHOD + "\\n" + PATH + "\\n" + body(전송 바이트 그대로)
  X-Signature = base64(Ed25519(보내는 쪽 개인키, message)), X-Signature-Alg: ed25519 (생략하면 ed25519로 본다)

METHOD는 대문자. PATH는 규격에 정한 경로다 (SPEC 2.2): 허브 API는 `/v1/...`,
은행 API는 base_url 뒤의 부분(`/inquiry`, `/receive/{transfer_id}` 등). 쿼리스트링은 넣지 않는다.
허브는 은행의 비밀을 갖지 않는다: 은행 개인키는 은행만, 허브 개인키는 허브만 갖고 서로 공개키만 안다 (SPEC 1절).

응답 message = X-Response-Timestamp + "\\n" + STATUS + "\\n" + 요청의 X-Signature 값 + "\\n" + 응답 body (SPEC 2.5)
  응답을 그 요청에 묶는다. 다른 요청의 응답을 가져다 붙이면 검증이 틀린다 (12.2 3번).

키 표현 (SPEC 2.6): 개인키·공개키 모두 raw 32바이트의 표준 base64(패딩 포함, 44자). 서명은 raw 64바이트의 base64(88자).
지문 = sha256(raw 공개키)의 hex 앞 16자. 공개키를 처음 받을 때 다른 경로로 받은 지문과 맞춰 본다.
"""

import base64
import binascii
import hashlib
import secrets
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

HUB_BANK_CODE = "000"  # 허브가 은행을 부를 때의 X-Bank-Code

# 헤더 이름 (SPEC 2.1·2.5)
ALG_HEADER = "X-Signature-Alg"
RESPONSE_TIMESTAMP_HEADER = "X-Response-Timestamp"
RESPONSE_SIGNATURE_HEADER = "X-Response-Signature"
ALG_ED25519 = "ed25519"

KEY_B64_LENGTH = 44  # 32바이트 → base64 44자
SIGNATURE_B64_LENGTH = 88  # 64바이트 → base64 88자


def _message(timestamp: str, method: str, path: str, body: bytes) -> bytes:
    return f"{timestamp}\n{method.upper()}\n{path}\n".encode() + body


def _timestamp(timestamp: int | None) -> str:
    return str(int(time.time()) if timestamp is None else timestamp)


def _check_time(timestamp: str, max_skew: int | None) -> str | None:
    """시각 헤더가 숫자가 아니면 SIGNATURE_INVALID, 허용 오차를 넘으면 SIGNATURE_EXPIRED. max_skew None은 검사 안 함."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return "SIGNATURE_INVALID"
    if max_skew is not None and abs(int(time.time()) - ts) > max_skew:
        return "SIGNATURE_EXPIRED"
    return None


# ---- Ed25519 키 (SPEC 2.6) ----

def _b64decode(value: str | None, length: int) -> bytes | None:
    """표준 base64(패딩 포함)를 엄격하게 풀어 정확히 length 바이트일 때만 돌려준다. 그 밖에는 None."""
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    return raw if len(raw) == length else None


def decode_public_key(public_key_b64: str | None) -> bytes | None:
    """공개키 base64 → raw 32바이트. 형식이 틀리면 None (등록 API는 422, 검증은 SIGNATURE_INVALID)."""
    if not isinstance(public_key_b64, str) or len(public_key_b64) != KEY_B64_LENGTH:
        return None
    return _b64decode(public_key_b64, 32)


def _private_key(private_key_b64: str) -> Ed25519PrivateKey:
    raw = _b64decode(private_key_b64.strip(), 32)
    if raw is None:
        raise ValueError("Ed25519 개인키는 raw 32바이트의 base64(44자)여야 합니다")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def generate_keypair() -> tuple[str, str]:
    """(개인키 base64, 공개키 base64). 개인키는 만든 쪽만 갖고, 공개키만 상대에게 준다."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return _b64(private), _b64(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def public_key_of(private_key_b64: str) -> str:
    return _b64(_private_key(private_key_b64).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def fingerprint(public_key_b64: str) -> str:
    """sha256(raw 공개키) hex 앞 16자. 공개키 형식이 틀리면 ValueError."""
    raw = decode_public_key(public_key_b64)
    if raw is None:
        raise ValueError("Ed25519 공개키는 raw 32바이트의 base64(44자)여야 합니다")
    return hashlib.sha256(raw).hexdigest()[:16]


def _ed25519_sign(private_key_b64: str, message: bytes) -> str:
    return _b64(_private_key(private_key_b64).sign(message))


def _ed25519_verify(public_key_b64: str | None, message: bytes, signature_b64: str | None) -> bool:
    raw_key = decode_public_key(public_key_b64)
    raw_sig = _b64decode(signature_b64, 64) if isinstance(signature_b64, str) and len(signature_b64) == SIGNATURE_B64_LENGTH else None
    if raw_key is None or raw_sig is None:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(raw_sig, message)
    except (InvalidSignature, ValueError):
        return False
    return True


# ---- Ed25519 요청 서명 (SPEC 2.3) ----

def sign_ed25519(private_key_b64: str, method: str, path: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    ts = _timestamp(timestamp)
    return {"X-Timestamp": ts, "X-Signature": _ed25519_sign(private_key_b64, _message(ts, method, path, body)),
            ALG_HEADER: ALG_ED25519}


def verify_ed25519(
    public_key_b64: str | None, timestamp: str, method: str, path: str, body: bytes, signature: str,
    max_skew: int | None = 300,
) -> str | None:
    """맞으면 None, 아니면 SIGNATURE_EXPIRED / SIGNATURE_INVALID. 잘못된 입력도 예외 없이 INVALID (500이 아니라 401)."""
    if error := _check_time(timestamp, max_skew):
        return error
    return None if _ed25519_verify(public_key_b64, _message(timestamp, method, path, body), signature) else "SIGNATURE_INVALID"


# ---- 응답 서명 (SPEC 2.5) ----

def _response_message(timestamp: str, status: int, request_signature: str, body: bytes) -> bytes:
    return f"{timestamp}\n{status}\n{request_signature}\n".encode() + body


def sign_response(
    private_key_b64: str, status: int, request_signature: str, body: bytes, timestamp: int | None = None
) -> dict[str, str]:
    """`request_signature`는 그 요청의 X-Signature 값 그대로 (없으면 "")."""
    ts = _timestamp(timestamp)
    signature = _ed25519_sign(private_key_b64, _response_message(ts, status, request_signature, body))
    return {RESPONSE_TIMESTAMP_HEADER: ts, RESPONSE_SIGNATURE_HEADER: signature, ALG_HEADER: ALG_ED25519}


def verify_response(
    public_key_b64: str | None, timestamp: str | None, status: int, request_signature: str, body: bytes,
    signature: str | None, max_skew: int | None = 300,
) -> str | None:
    """맞으면 None. 헤더 없음·형식 오류·불일치는 SIGNATURE_INVALID, 시각 초과는 SIGNATURE_EXPIRED.
    분쟁 때 보관한 응답을 다시 검증할 때는 max_skew=None으로 시각 검사를 끈다."""
    if not timestamp or not signature:
        return "SIGNATURE_INVALID"
    if error := _check_time(timestamp, max_skew):
        return error
    message = _response_message(timestamp, status, request_signature, body)
    return None if _ed25519_verify(public_key_b64, message, signature) else "SIGNATURE_INVALID"


# ---- 발급 ----

def new_api_key() -> str:
    """은행 식별용 API 키 (X-Api-Key). 허브는 해시만 저장한다. 서명 키가 아니라서 이것만으로는 은행 명의 요청을 만들 수 없다."""
    return "chk_" + secrets.token_urlsafe(24)


def new_invite_code() -> str:
    """셀프 등록 초대 코드 (SPEC 3.5·7절). 관리자가 발급할 때 한 번만 보여 준다."""
    return "inv_" + secrets.token_urlsafe(16)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()

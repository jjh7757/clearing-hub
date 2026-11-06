"""서명 진단기 (SETTLEMENT_PLAN 14절, SPEC 3.8). **규칙만 쓴다.**

인증 실패 한 건(`auth_failures`)을 받아 원인 가설 목록을 돌려준다. 서명 오류는 원인이 늘 같은 몇 가지라서, 실패한 요청을
**흔한 실수 변형으로 다시 계산해 실제로 검증해 보고** 일치하는 변형을 찾는다. Ed25519 검증이 맞았다면 그 변형이 원인이라는
것은 추측이 아니라 증명이다 (`verified: true`). 아무 변형과도 맞지 않으면 "다른 키로 서명", "메시지 구성이 전혀 다름"을 낸다.

- BANK_TO_HUB `SIGNATURE_INVALID`: PATH(`/v1` 뺌, 전체 URL, 쿼리, 끝 `/`, 은행 접두사 `/interbank`) × 본문(다시 직렬화:
  `json.dumps` 기본값, `ensure_ascii`, 공백, 키 정렬, 빈 본문) × 구분자(경로 뒤 `\\n` 누락, `\\r\\n`) × 메서드 소문자 ×
  서명 인코딩(hex, base64url, 패딩 없음) × 키(현재, 이전). 바꾼 것이 적은 조합부터 검증한다
- `SIGNATURE_EXPIRED`: 헤더 시각과 받은 시각의 차이(초), 밀리초
- `UNAUTHORIZED`: 빠진 헤더, API 키 불일치. `BANK_INACTIVE`, 공개키 없음: 안내
- HUB_TO_BANK: 은행이 준 오류 코드별 가설(은행 쪽 검증 PATH·허브 공개키·원본 바이트·시계). 응답 서명이 틀렸으면
  은행 응답 헤더를 보고, 있으면 응답 서명도 변형으로 다시 검증한다(요청 서명 빠짐, STATUS, 본문 다시 직렬화, 인코딩)

각 가설: `code`, `title`(한 줄), `detail`(이번 실패의 실제 값), `spec_ref`(SPEC 절), `fix`(고치는 방법 한 줄, 참고 구현 함수까지).
"""

import base64
import binascii
import itertools
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from clearing_hub.core.signing import decode_public_key, fingerprint

MAX_DEVIATIONS = 3  # 한 조합에서 동시에 바꿔 보는 것의 최대 개수


@dataclass
class Hypothesis:
    code: str
    title: str
    detail: str
    spec_ref: str
    fix: str
    verified: bool = False  # 다시 계산해 실제로 서명이 맞음을 확인했다 (추측이 아니다)


@dataclass
class Diagnosis:
    failure_id: int | None
    direction: str
    reason: str
    hypotheses: list[Hypothesis] = field(default_factory=list)

    @property
    def top(self) -> Hypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None

    def one_line(self) -> str:
        """알림에 붙이는 1순위 가설 한 줄."""
        top = self.top
        if top is None:
            return "원인 후보 없음"
        mark = "확인됨" if top.verified else "추정"
        return f"원인 후보({mark}): {top.title} → {top.fix} (SPEC {top.spec_ref})"

    def to_dict(self) -> dict:
        return {"failure_id": self.failure_id, "direction": self.direction, "reason": self.reason,
                "hypotheses": [asdict(h) for h in self.hypotheses]}


@dataclass
class DoctorContext:
    """진단에 필요한 것 (domains/support/service.context_for가 은행·설정에서 채운다)."""

    current_key: str | None = None  # 은행의 지금 공개키
    previous_key: str | None = None  # 교체 전 공개키 (만료됐어도 넣는다: 옛 키로 서명하는 실수를 찾는다)
    previous_valid: bool = False  # 이전 키를 허브가 아직 받아 주는가 (교체 뒤 24시간 안)
    base_url: str | None = None  # 은행 base_url (은행 쪽 접두사 변형, 허브가 부른 주소)
    hub_public_url: str | None = None  # HUB_PUBLIC_URL (전체 URL 변형)
    hub_fingerprint: str | None = None
    max_skew: int = 300
    claimed_bank_exists: bool | None = None  # 관리자 진단에서만: X-Bank-Code의 은행이 있는가


# ---- 가설 목록 (제목·SPEC 절·고치는 법) ----

_REF_SIGN = "2.3의 `sign_request`"
CATALOG: dict[str, tuple[str, str, str]] = {
    # 은행 → 허브 요청 서명
    "PATH_WITHOUT_V1": ("서명 PATH에 `/v1`이 빠짐", "2.2",
                        f"서명 PATH는 `/v1`부터 넣는다(`/v1/transfers`). {_REF_SIGN}에 넘기는 path를 확인"),
    "PATH_FULL_URL": ("서명 PATH에 전체 URL(스킴·호스트)을 넣음", "2.2",
                      f"`https://…`를 빼고 `/v1/…` 경로만 넣는다. {_REF_SIGN}의 path 인자"),
    "PATH_WITH_QUERY": ("서명 PATH에 쿼리스트링을 넣음", "2.2", "쿼리 없이 경로만 넣는다 (규격 API는 쿼리를 쓰지 않는다)"),
    "PATH_TRAILING_SLASH": ("서명 PATH 끝에 `/`를 붙임", "2.2", "끝의 `/` 없이 `/v1/transfers`처럼 넣는다"),
    "PATH_BANK_PREFIX": ("서명 PATH에 은행 쪽 경로 접두사가 들어감", "2.2",
                         "은행 → 허브 PATH는 `/v1`부터다. 자기 base_url 경로(`/interbank` 등)를 붙이지 않는다"),
    "BODY_DEFAULT_DUMPS": ("본문을 `json.dumps(obj)` 기본값으로 다시 만들어 서명함 (보낸 바이트와 다름)", "2.2",
                           "직렬화는 한 번만: `body = json.dumps(payload, ensure_ascii=False, separators=(\",\", \":\")).encode()`"
                           " → `sign_request(…, body)` → `content=body`로 전송 (`json=` 금지)"),
    "BODY_ENSURE_ASCII": ("한글을 `\\uXXXX`로 이스케이프한 본문으로 서명함 (보낸 바이트와 다름)", "2.2",
                          "서명한 바이트를 그대로 보낸다. 같은 `body` 변수로 서명·전송 (`ensure_ascii=False`를 한쪽에만 쓰지 않는다)"),
    "BODY_SPACES": ("공백(`, `·`: `)이 있는 직렬화로 서명함 (보낸 바이트와 다름)", "2.2",
                    "서명한 바이트를 그대로 보낸다. HTTP 라이브러리의 `json=`이 다시 직렬화하지 않게 `content=body`"),
    "BODY_SORTED_KEYS": ("키를 정렬한 직렬화로 서명함 (보낸 바이트와 다름)", "2.2",
                         "`sort_keys` 없이 한 번 만든 바이트로 서명·전송한다"),
    "BODY_EMPTY_SIGNED": ("본문을 빈 문자열로 서명했는데 본문을 보냄", "2.2",
                          "본문이 있는 요청은 보내는 본문 바이트로 서명한다 (`body = \"\"`는 본문 없는 GET·POST만)"),
    "BODY_SIGNED_NONEMPTY": ("본문 없이 보냈는데 `{}` 등 다른 본문으로 서명함", "2.2",
                             "본문이 없는 GET·POST는 `body = b\"\"`로 서명한다 (2.3 예시 2)"),
    "NO_TRAILING_NEWLINE": ("메시지에서 PATH 뒤 `\\n`이 빠짐", "2.3",
                            "본문이 비어도 `f\"{ts}\\n{METHOD}\\n{PATH}\\n\".encode() + body` (2.3 예시 2)"),
    "CRLF": ("메시지 줄바꿈을 `\\r\\n`으로 씀", "2.2", "`\\n`(LF)만 쓴다. Windows에서 여러 줄 문자열로 만들지 않는다"),
    "METHOD_LOWERCASE": ("메시지의 METHOD를 소문자로 넣음", "2.2", "`method.upper()` (`POST`, `GET`)"),
    "SIGNATURE_HEX": ("서명을 hex로 보냄", "2.2", "`base64.b64encode(raw_signature).decode()` (88자, 2.3 `_sign`)"),
    "SIGNATURE_BASE64URL": ("서명을 URL-safe base64(`-`·`_`)로 보냄", "2.2",
                            "표준 base64(`+`·`/`, 패딩 `=`): `base64.b64encode` (2.3 `_sign`)"),
    "SIGNATURE_NO_PADDING": ("서명 base64의 패딩 `=`이 빠짐", "2.2", "패딩 포함 88자: `base64.b64encode` (2.3 `_sign`)"),
    "SIGNATURE_FORMAT": ("서명이 Ed25519 서명(64바이트)의 base64가 아님", "2.2",
                         "raw 64바이트 서명의 표준 base64(88자). PEM·DER·HMAC이 아니다 (2.3 `_sign`)"),
    "ALG_UNSUPPORTED": ("`X-Signature-Alg`가 `ed25519`가 아님", "2.1", "`X-Signature-Alg: ed25519`로 보내거나 헤더를 뺀다"),
    "OLD_KEY_EXPIRED": ("교체 전(만료된) 개인키로 서명함", "2.6",
                        "`PUT /v1/keys`로 바꾼 새 개인키를 서버 환경 변수에 넣었는지 확인 (옛 키는 24시간만 받는다)"),
    "TIMESTAMP_FORMAT": ("`X-Timestamp`가 정수 초 문자열이 아님", "2.1", "`str(int(time.time()))` (2.3 `sign_request`)"),
    "NOW_VALID": ("지금 등록된 키로는 맞는 서명", "2.6",
                  "실패 뒤에 공개키가 바뀌었으면 이미 해결됐을 수 있다. 같은 요청을 새 서명으로 다시 보내 확인"),
    "WRONG_KEY": ("등록된 공개키와 짝이 아닌 개인키로 서명했을 수 있음", "2.6",
                  "`public_key_of(내 개인키)`의 지문을 `GET /v1/keys`의 `fingerprint`와 비교. 다르면 키 쌍을 다시 확인"),
    "MESSAGE_DIFFERENT": ("서명 메시지 구성이 규격과 전혀 다를 수 있음", "2.3",
                          "2.3 예시 1의 `X-Signature`(`SY/3NCjT…`)가 내 `sign_request`로 똑같이 나오는지 먼저 확인"),
    "PUBLIC_KEY_MISSING": ("허브에 공개키가 등록되지 않음", "2.6",
                           "허브 관리자에게 공개키 등록을 요청한다 (`POST /admin/banks/{code}/public-key`)"),
    "CLOCK_SKEW": ("서버 시계가 틀어짐", "2.2", "NTP로 시계를 맞춘다 (chrony·`timedatectl set-ntp true`). 허용 오차 300초"),
    "TIMESTAMP_MILLIS": ("`X-Timestamp`를 밀리초로 보냄", "2.1",
                         "초 단위: `str(int(time.time()))`, JavaScript면 `Math.floor(Date.now() / 1000)`"),
    "STALE_SIGNATURE": ("재전송·재시도할 때 옛 서명을 그대로 씀", "2.2", "보낼 때마다 `X-Timestamp`·`X-Signature`를 새로 만든다"),
    "MISSING_HEADERS": ("인증 헤더가 빠짐", "2.1", "`X-Bank-Code`·`X-Api-Key`·`X-Timestamp`·`X-Signature`를 모두 넣는다"),
    "API_KEY_MISMATCH": ("`X-Bank-Code`와 `X-Api-Key`가 맞지 않음", "2.1",
                         "등록 응답의 `api_key`를 그대로(앞뒤 공백·따옴표 없이) 넣는다. 재발급 뒤면 새 키, 잃어버렸으면 관리자에게 재발급 요청"),
    "BANK_CODE_UNKNOWN": ("`X-Bank-Code`의 은행이 등록돼 있지 않음", "3.5", "등록 응답의 `bank_code`를 넣는다"),
    "BANK_INACTIVE": ("은행이 비활성 상태", "5", "허브 관리자에게 문의한다 (키 유출 대응 등으로 막아 두었을 수 있다)"),
    # 허브 → 은행 (은행이 거절)
    "BANK_VERIFY_PATH": ("은행이 서명 PATH를 base_url 뒤 부분이 아닌 경로로 검증함", "2.2",
                         "받은 경로에서 base_url 경로를 떼고 검증: `request.url.path.partition(\"/interbank\")[2]` (2.3 끝)"),
    "BANK_VERIFY_HUB_KEY": ("은행이 허브 공개키가 아닌 키로 검증함", "2.6",
                            "설정한 허브 공개키의 지문을 `GET /v1/hub-key`·초대 문구의 지문과 맞춰 본다"),
    "BANK_VERIFY_RAW_BODY": ("은행이 원본 바이트가 아닌 다시 만든 JSON으로 검증함", "2.2",
                             "`await request.body()`로 받은 바이트 그대로 `verify_request`한 뒤 JSON으로 푼다"),
    "BANK_CLOCK": ("은행 서버 시계가 틀어짐 (허브 서명을 만료로 거절)", "2.2", "은행 서버를 NTP로 맞춘다"),
    "BANK_HEADER_CHECK": ("은행이 허브 요청에 다른 헤더를 기대함", "2.1",
                          "허브는 `X-Bank-Code: 000`·`X-Timestamp`·`X-Signature`·`X-Signature-Alg: ed25519`만 보내고 "
                          "`X-Api-Key`는 보내지 않는다 (8절 3). API 키를 요구하지 않는다"),
    "BANK_FORBIDDEN": ("은행이 허브 요청을 403으로 막음", "4", "방화벽·WAF·IP 제한·`X-Bank-Code` 검사를 확인한다"),
    # 은행 응답 서명
    "RESPONSE_SIGNATURE_MISSING": ("은행 응답에 `X-Response-Signature`가 없음", "2.5",
                                   "서명 미들웨어를 서명 검증보다 바깥에 두고 모든 2xx·4xx(401 포함)에 `sign_response`"),
    "RESPONSE_TIMESTAMP_MISSING": ("은행 응답에 `X-Response-Timestamp`가 없음", "2.5", "`sign_response`가 돌려준 헤더 셋을 모두 붙인다"),
    "RESPONSE_ALG": ("은행 응답의 `X-Signature-Alg`가 `ed25519`가 아님", "2.5", "`X-Signature-Alg: ed25519`"),
    "RESPONSE_NO_REQUEST_SIGNATURE": ("응답 서명 메시지에 요청의 `X-Signature`를 넣지 않음", "2.5",
                                      "`sign_response(key, status, request.headers[\"X-Signature\"], body)` (2.5 참고 구현)"),
    "RESPONSE_STATUS_MISMATCH": ("응답 서명의 STATUS가 실제 HTTP 상태와 다름", "2.5", "응답을 만든 뒤 그 `status_code`로 서명한다"),
    "RESPONSE_BODY_REDUMPED": ("응답 본문을 다시 직렬화해 서명함 (보낸 바이트와 다름)", "2.5",
                               "실제로 보내는 `response.body` 바이트로 서명한다 (2.5 끝, 순수 ASGI 미들웨어)"),
    "RESPONSE_SIGNATURE_HEX": ("응답 서명을 hex로 보냄", "2.5", "`base64.b64encode(raw_signature).decode()` (88자)"),
    "RESPONSE_SIGNATURE_BASE64URL": ("응답 서명을 URL-safe base64로 보냄", "2.5", "표준 base64: `base64.b64encode`"),
    "RESPONSE_SIGNATURE_NO_PADDING": ("응답 서명 base64의 패딩 `=`이 빠짐", "2.5", "패딩 포함 88자: `base64.b64encode`"),
    "RESPONSE_SIGNATURE_FORMAT": ("응답 서명이 Ed25519 서명(64바이트)의 base64가 아님", "2.5", "raw 64바이트 서명의 표준 base64(88자)"),
    "RESPONSE_EXPIRED": ("은행 응답 시각이 허브 시각과 5분 넘게 차이 남", "2.5", "은행 서버를 NTP로 맞춘다"),
    "RESPONSE_OLD_KEY_EXPIRED": ("교체 전(만료된) 개인키로 응답에 서명함", "2.6", "키를 바꾼 뒤 응답 서명 쪽 개인키도 새 키로 바꾼다"),
    "RESPONSE_WRONG_KEY": ("응답을 등록한 공개키와 짝이 아닌 개인키로 서명했을 수 있음", "2.5",
                           "응답 서명 키가 등록한 키(`GET /v1/keys`의 지문)와 같은지, 키를 바꾼 뒤 응답 쪽도 바꿨는지 확인"),
    "RESPONSE_MESSAGE_DIFFERENT": ("응답 서명 메시지 구성이 규격과 다를 수 있음", "2.5",
                                   "2.5 예시 3의 `X-Response-Signature`(`D84tooOZ…`)가 내 `sign_response`로 똑같이 나오는지 확인"),
    "RESPONSE_NOW_VALID": ("지금 등록된 키로는 맞는 응답 서명", "2.6", "실패 뒤 공개키가 바뀌었으면 해결됐을 수 있다. `POST /v1/me/ping`으로 확인"),
    "UNKNOWN_REASON": ("규칙에 없는 실패", "5", "`message`와 SPEC 5절 표를 본다"),
}


def _h(code: str, detail: str, verified: bool = False) -> Hypothesis:
    title, ref, fix = CATALOG[code]
    return Hypothesis(code=code, title=title, detail=detail, spec_ref=ref, fix=fix, verified=verified)


# ---- 공통 ----

def _public_keys(ctx: DoctorContext) -> list[tuple[str | None, Ed25519PublicKey, str]]:
    """(변형 코드 또는 None, 키, 지문). 만료된 이전 키는 'OLD_KEY_EXPIRED' 변형이다."""
    keys = []
    for code, b64 in ((None, ctx.current_key), (None if ctx.previous_valid else "OLD_KEY_EXPIRED", ctx.previous_key)):
        raw = decode_public_key(b64) if b64 else None
        if raw is not None:
            keys.append((code, Ed25519PublicKey.from_public_bytes(raw), fingerprint(b64)))
    return keys


def _signature_candidates(signature: str, prefix: str = "") -> tuple[list[tuple[str | None, bytes]], str]:
    """헤더 값 → (변형 코드, raw 64바이트) 후보들, 그리고 형식 설명. 규격대로면 코드 None."""
    s = (signature or "").strip()
    out: list[tuple[str | None, bytes]] = []
    try:
        raw = base64.b64decode(s, validate=True)
        if len(raw) == 64 and len(s) == 88:
            out.append((None, raw))
    except (binascii.Error, ValueError):
        pass
    if re.fullmatch(r"[0-9a-fA-F]{128}", s):
        out.append((f"{prefix}SIGNATURE_HEX", bytes.fromhex(s)))
    if re.fullmatch(r"[A-Za-z0-9_\-+/]+=*", s) and not out:
        try:
            raw = base64.urlsafe_b64decode(s.replace("+", "-").replace("/", "_") + "=" * (-len(s) % 4))
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) == 64:
            out.append((f"{prefix}SIGNATURE_BASE64URL" if ("-" in s or "_" in s) else f"{prefix}SIGNATURE_NO_PADDING", raw))
    return out, f"{len(s)}자"


def _verifies(key: Ed25519PublicKey, raw_sig: bytes, message: bytes) -> bool:
    try:
        key.verify(raw_sig, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def _body_variants(body: bytes, truncated: bool, empty_code: str | None, nonempty_code: str | None,
                   redump_code: str | None = None) -> list[tuple[str | None, bytes, str]]:
    """(변형 코드, 서명했을지 모르는 본문, 설명). 첫 항목은 받은 바이트 그대로."""
    out: list[tuple[str | None, bytes, str]] = [(None, body, "받은 바이트 그대로")]
    seen = {body}
    if body and not truncated:
        try:
            obj = json.loads(body)
        except ValueError:
            obj = None
        if obj is not None:
            forms = [
                ("BODY_DEFAULT_DUMPS", json.dumps(obj), "json.dumps(obj) 기본값 (공백, \\u 이스케이프)"),
                ("BODY_ENSURE_ASCII", json.dumps(obj, separators=(",", ":")), "공백 없음 + \\u 이스케이프"),
                ("BODY_SPACES", json.dumps(obj, ensure_ascii=False), "공백 있음 (ensure_ascii=False)"),
                ("BODY_SORTED_KEYS", json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True), "키 정렬"),
                ("BODY_SORTED_KEYS", json.dumps(obj, sort_keys=True), "키 정렬 + 기본값"),
            ]
            for code, text, how in forms:
                raw = text.encode()
                if raw not in seen:
                    seen.add(raw)
                    out.append((redump_code or code, raw, how))
    if body and empty_code:
        out.append((empty_code, b"", "빈 본문"))
    if not body and nonempty_code:
        for raw in (b"{}", b"null"):
            out.append((nonempty_code, raw, raw.decode()))
    return out


def _received_epoch(failure, now: datetime | None) -> int:
    created = getattr(failure, "created_at", None) or now or datetime.now(UTC)
    created = created if created.tzinfo else created.replace(tzinfo=UTC)  # SQLite는 시간대 없이 준다
    return int(created.timestamp())


def _skew_text(diff: int) -> str:
    return f"{abs(diff)}초 {'빠름' if diff > 0 else '느림'}"


# ---- BANK_TO_HUB ----

def _bank_request(failure, ctx: DoctorContext) -> list[Hypothesis]:
    reason = failure.reason
    headers = failure.headers_json or {}
    if reason == "UNAUTHORIZED":
        return _unauthorized(failure, headers, ctx)
    if reason == "BANK_INACTIVE":
        return [_h("BANK_INACTIVE", f"은행 {failure.bank_code}은 허브에서 비활성으로 되어 있다")]
    if reason == "SIGNATURE_EXPIRED":
        return _expired(failure, headers, ctx)
    if reason == "SIGNATURE_INVALID":
        return _invalid(failure, headers, ctx)
    return [_h("UNKNOWN_REASON", f"{reason}: {failure.message or ''}")]


def _unauthorized(failure, headers: dict, ctx: DoctorContext) -> list[Hypothesis]:
    missing = [name for name in ("X-Bank-Code", "X-Timestamp", "X-Signature") if not headers.get(name)]
    if not failure.api_key_sent:
        missing.insert(1, "X-Api-Key")
    if missing:
        return [_h("MISSING_HEADERS", f"없는 헤더: {', '.join(missing)}")]
    hyps = []
    if ctx.claimed_bank_exists is False:
        hyps.append(_h("BANK_CODE_UNKNOWN", f"`X-Bank-Code: {failure.claimed_bank_code}`인 은행이 없다"))
    hyps.append(_h("API_KEY_MISMATCH", f"`X-Bank-Code: {failure.claimed_bank_code}`와 보낸 API 키가 짝이 아니다 "
                                       "(허브는 API 키 값을 저장하지 않아 어떻게 다른지는 모른다)"))
    return hyps


def _expired(failure, headers: dict, ctx: DoctorContext) -> list[Hypothesis]:
    ts = headers.get("X-Timestamp") or ""
    received = _received_epoch(failure, None)
    try:
        value = int(ts)
    except ValueError:
        return [_h("TIMESTAMP_FORMAT", f"`X-Timestamp: {ts}`")]
    keys = _public_keys(ctx)
    sigs, _ = _signature_candidates(headers.get("X-Signature") or "")
    body = failure.body or b""

    def signed_with(ts_text: str) -> bool:
        message = f"{ts_text}\n{failure.method.upper()}\n{failure.path}\n".encode() + body
        return any(_verifies(k, raw, message) for _, k, _ in keys for _, raw in sigs)

    if value >= 10**11:  # 밀리초 (지금 초 단위 시각은 10자리)
        seconds = value // 1000
        ok = signed_with(ts) or signed_with(str(seconds))
        return [_h("TIMESTAMP_MILLIS", f"`X-Timestamp: {ts}`({len(ts)}자리)는 밀리초다. 초로는 {seconds}, 받은 시각과 "
                                       f"{_skew_text(seconds - received)}" + (" — 서명 계산 자체는 맞다" if ok else ""), ok)]
    diff = value - received
    ok = signed_with(ts)
    detail = (f"`X-Timestamp: {ts}`, 허브가 받은 시각 {received}: 은행 시각이 {_skew_text(diff)} "
              f"(허용 {ctx.max_skew}초)" + (" — 서명 계산 자체는 맞다 (시각만 문제)" if ok else ""))
    hyps = [_h("CLOCK_SKEW", detail, ok)]
    if diff < 0:  # 옛 시각: 시계가 느리거나, 예전에 만든 서명을 다시 보냈다
        hyps.append(_h("STALE_SIGNATURE", f"{abs(diff)}초 전에 만든 서명일 수 있다"))
    return hyps


def _path_variants(failure, ctx: DoctorContext) -> list[tuple[str | None, str]]:
    path = failure.path
    out: list[tuple[str | None, str]] = [(None, path)]
    if path.startswith("/v1/") or path == "/v1":
        out.append(("PATH_WITHOUT_V1", path.removeprefix("/v1") or "/"))
    urls = []
    if failure.url:
        urls.append(failure.url)
    if ctx.hub_public_url:
        urls.append(ctx.hub_public_url.rstrip("/") + path)
    for url in list(urls):  # 프록시 뒤에서는 스킴이 다르게 보일 수 있다
        urls.append(url.replace("http://", "https://", 1) if url.startswith("http://") else url.replace("https://", "http://", 1))
    for url in dict.fromkeys(urls):
        out.append(("PATH_FULL_URL", url))
    if failure.query:
        out.append(("PATH_WITH_QUERY", f"{path}?{failure.query}"))
    out.append(("PATH_TRAILING_SLASH", path + "/"))
    prefixes = []
    if ctx.base_url:
        prefixes.append(urlparse(ctx.base_url).path.rstrip("/"))
    prefixes += ["/interbank", "/api"]
    for prefix in dict.fromkeys(p for p in prefixes if p and p != "/"):
        out.append(("PATH_BANK_PREFIX", prefix + path))
    return out


def _search(keys, sigs, dimensions: list[list[tuple]], build) -> tuple[list[tuple], tuple] | None:
    """dimensions의 각 차원에서 하나씩 골라 메시지를 만들고(build), 바꾼 것(코드가 None이 아닌 것)이 적은 조합부터 검증한다.
    맞으면 (고른 항목들, (키 코드, 지문, 서명 코드)). 키·서명 인코딩의 변형도 바꾼 것으로 센다."""
    combos = []
    for choice in itertools.product(*dimensions):
        deviations = sum(1 for item in choice if item[0] is not None)
        if deviations <= MAX_DEVIATIONS:
            combos.append((deviations, choice))
    combos.sort(key=lambda c: c[0])  # 안정 정렬: 같은 개수면 위 목록 순서 (흔한 실수가 앞)
    for deviations, choice in combos:
        message = build(choice)
        for key_code, key, fp in keys:
            for sig_code, raw in sigs:
                extra = (key_code is not None) + (sig_code is not None)
                if deviations + extra > MAX_DEVIATIONS:
                    continue
                if _verifies(key, raw, message):
                    return list(choice), (key_code, fp, sig_code)
    return None


def _invalid(failure, headers: dict, ctx: DoctorContext) -> list[Hypothesis]:
    hyps: list[Hypothesis] = []
    alg = headers.get("X-Signature-Alg")
    if alg and alg.strip().lower() != "ed25519":
        hyps.append(_h("ALG_UNSUPPORTED", f"`X-Signature-Alg: {alg}`", True))
    keys = _public_keys(ctx)
    if not keys:
        return hyps + [_h("PUBLIC_KEY_MISSING", f"은행 {failure.bank_code}의 공개키가 허브에 없다", True)]
    ts = headers.get("X-Timestamp") or ""
    if not ts.isdigit():
        hyps.append(_h("TIMESTAMP_FORMAT", f"`X-Timestamp: {ts}` (허브는 정수가 아니면 SIGNATURE_INVALID로 거절한다)", True))
    signature = headers.get("X-Signature") or ""
    sigs, length = _signature_candidates(signature)
    if not sigs:
        return hyps + [_h("SIGNATURE_FORMAT", f"`X-Signature`가 {length}이고 표준 base64·hex·base64url 어느 것으로 풀어도 "
                                              "64바이트가 아니다", True)]
    body = failure.body or b""
    method = failure.method.upper()
    dims = [
        _path_variants(failure, ctx),
        _body_variants(body, failure.body_truncated, "BODY_EMPTY_SIGNED", "BODY_SIGNED_NONEMPTY"),
        [(None, "\n", True), ("NO_TRAILING_NEWLINE", "\n", False), ("CRLF", "\r\n", True)],
        [(None, method), ("METHOD_LOWERCASE", method.lower())],
    ]

    def build(choice) -> bytes:
        (_, path), (_, signed_body, *_), (_, sep, trailing), (_, m) = choice
        return f"{ts}{sep}{m}{sep}{path}{sep if trailing else ''}".encode() + signed_body

    found = _search(keys, sigs, dims, build)
    if found is None:
        fps = ", ".join(fp for _, _, fp in keys)
        tried = len(list(itertools.product(*dims)))
        note = " 본문이 64KB를 넘어 잘려서 본문 변형은 시험하지 못했다." if failure.body_truncated else ""
        return hyps + [
            _h("WRONG_KEY", f"흔한 실수 변형 {tried}가지를 다시 계산했지만 등록된 공개키(지문 {fps})로 맞는 것이 없다.{note}"),
            _h("MESSAGE_DIFFERENT", f"허브가 기대한 메시지: `{ts}\\n{method}\\n{failure.path}\\n` + 본문 {len(body)}바이트"),
        ]
    choice, (key_code, fp, sig_code) = found
    (path_code, path), (body_code, _, how), (sep_code, *_), (method_code, _) = choice
    if path_code:
        hyps.append(_h(path_code, f"허브는 `{failure.path}`로 검증했고, 서명은 `{path}`로 만들었다", True))
    if body_code:
        hyps.append(_h(body_code, f"서명한 본문: {how}. 보낸 본문 {len(body)}바이트와 다르다", True))
    if sep_code:
        hyps.append(_h(sep_code, "이 형식으로 다시 계산하면 서명이 맞는다", True))
    if method_code:
        hyps.append(_h(method_code, f"`{method.lower()}`로 다시 계산하면 서명이 맞는다", True))
    if sig_code:
        hyps.append(_h(sig_code, f"`X-Signature`({length})를 그 형식으로 풀면 맞는 서명이다", True))
    if key_code:
        hyps.append(_h(key_code, f"이전 키(지문 {fp})로 맞는 서명이다", True))
    if not hyps:
        hyps.append(_h("NOW_VALID", f"지문 {fp}인 지금 키로 다시 검증하면 맞다", True))
    return hyps


# ---- HUB_TO_BANK ----

def _hub_request(failure, ctx: DoctorContext) -> list[Hypothesis]:
    reason = failure.reason
    status = failure.http_status
    sent = f"허브는 `{failure.method} {failure.url or failure.path}`를 PATH `{failure.path}`로 서명해 보냈다"
    hyps: list[Hypothesis] = []
    if status in (401, 403):
        said = f"은행이 HTTP {status} `{reason}`" + (f" ({failure.message})" if failure.message else "") + "로 거절했다"
        fp = f"허브 공개키 지문은 {ctx.hub_fingerprint}" if ctx.hub_fingerprint else "허브 공개키는 `GET /v1/hub-key`"
        if reason == "SIGNATURE_EXPIRED":
            hyps.append(_h("BANK_CLOCK", f"{said}. 허브가 서명한 시각은 {(failure.headers_json or {}).get('X-Timestamp')}"))
        elif status == 403 or reason in ("FORBIDDEN", "HTTP_403"):
            hyps += [_h("BANK_FORBIDDEN", said), _h("BANK_HEADER_CHECK", sent)]
        elif reason == "UNAUTHORIZED":
            hyps += [_h("BANK_HEADER_CHECK", f"{said}. {sent}"), _h("BANK_VERIFY_PATH", sent)]
        else:  # SIGNATURE_INVALID, 표에 없는 코드, 본문 없는 401
            hyps += [_h("BANK_VERIFY_PATH", f"{said}. {sent}"), _h("BANK_VERIFY_HUB_KEY", fp),
                     _h("BANK_VERIFY_RAW_BODY", f"허브가 보낸 본문 {len(failure.body or b'')}바이트 (공백 없는 JSON)")]
        if failure.response_signature_valid is False:
            hyps += _response(failure, ctx)
        return hyps
    if reason == "RESPONSE_SIGNATURE_INVALID":
        return _response(failure, ctx)
    return [_h("UNKNOWN_REASON", f"{reason}: {failure.message or ''}")]


def _response(failure, ctx: DoctorContext) -> list[Hypothesis]:
    headers = failure.response_headers_json or {}
    status = failure.http_status
    rts, rsig, ralg = (headers.get("X-Response-Timestamp"), headers.get("X-Response-Signature"), headers.get("X-Signature-Alg"))
    shown = ", ".join(f"{k}: {v}" for k, v in headers.items()) or "없음"
    if not rsig:
        return [_h("RESPONSE_SIGNATURE_MISSING", f"HTTP {status} 응답. 받은 서명 헤더: {shown}", True)]
    if not rts:
        return [_h("RESPONSE_TIMESTAMP_MISSING", f"HTTP {status} 응답. 받은 서명 헤더: {shown}", True)]
    if ralg and ralg.strip().lower() != "ed25519":
        return [_h("RESPONSE_ALG", f"`X-Signature-Alg: {ralg}`", True)]
    keys = _public_keys(ctx)
    if not keys:
        return [_h("PUBLIC_KEY_MISSING", f"은행 {failure.bank_code}의 공개키가 허브에 없어 응답 서명을 검증할 수 없다", True)]
    sigs, length = _signature_candidates(rsig, "RESPONSE_")
    if not sigs:
        return [_h("RESPONSE_SIGNATURE_FORMAT", f"`X-Response-Signature`가 {length}이고 풀어도 64바이트가 아니다", True)]
    request_signature = (failure.headers_json or {}).get("X-Signature") or ""
    body = failure.response_body or b""
    hyps: list[Hypothesis] = []
    try:
        diff = int(rts) - _received_epoch(failure, None)
    except ValueError:
        diff = None
    if diff is None:
        hyps.append(_h("TIMESTAMP_FORMAT", f"`X-Response-Timestamp: {rts}`", True))
    elif abs(diff) > ctx.max_skew:
        hyps.append(_h("RESPONSE_EXPIRED", f"`X-Response-Timestamp: {rts}`: 은행 시각이 {_skew_text(diff)} (허용 {ctx.max_skew}초)", True))
    dims = [
        [(None, request_signature, True), ("RESPONSE_NO_REQUEST_SIGNATURE", "", True),
         ("RESPONSE_NO_REQUEST_SIGNATURE", "", False)],
        [(None, status)] + ([("RESPONSE_STATUS_MISMATCH", 200)] if status != 200 else []),
        _body_variants(body, False, "RESPONSE_BODY_REDUMPED", None, redump_code="RESPONSE_BODY_REDUMPED"),
    ]

    def build(choice) -> bytes:
        (_, req_sig, with_line), (_, signed_status), (_, signed_body, *_) = choice
        line = f"{req_sig}\n" if with_line else ""
        return f"{rts}\n{signed_status}\n{line}".encode() + signed_body

    found = _search(keys, sigs, dims, build)
    if found is None:
        fps = ", ".join(fp for _, _, fp in keys)
        return hyps + [
            _h("RESPONSE_WRONG_KEY", f"흔한 변형을 다시 계산했지만 등록된 공개키(지문 {fps})로 맞는 것이 없다"),
            _h("RESPONSE_MESSAGE_DIFFERENT", f"허브가 기대한 메시지: `{rts}\\n{status}\\n` + 요청 X-Signature + `\\n` + 본문 {len(body)}바이트"),
        ]
    choice, (key_code, fp, sig_code) = found
    (req_code, _, with_line), (status_code, signed_status), (body_code, _, how) = choice
    if req_code:
        hyps.append(_h(req_code, "요청 서명 줄을 " + ("빈 문자열로 넣어" if with_line else "빼고") + " 다시 계산하면 맞는다", True))
    if status_code:
        hyps.append(_h(status_code, f"서명은 STATUS {signed_status}, 실제 응답은 {status}", True))
    if body_code:
        hyps.append(_h(body_code, f"서명한 본문: {how}. 보낸 본문 {len(body)}바이트와 다르다", True))
    if sig_code:
        hyps.append(_h(sig_code, f"`X-Response-Signature`({length})를 그 형식으로 풀면 맞는 서명이다", True))
    if key_code:
        hyps.append(_h("RESPONSE_OLD_KEY_EXPIRED", f"이전 키(지문 {fp})로 맞는 서명이다", True))
    if not hyps:
        hyps.append(_h("RESPONSE_NOW_VALID", f"지문 {fp}인 지금 키로 다시 검증하면 맞다", True))
    return hyps


def diagnose(failure, ctx: DoctorContext | None = None) -> Diagnosis:
    """auth_failures 한 건(또는 같은 속성을 가진 저장하지 않은 객체) → 가설 목록 (앞이 1순위). 예외를 올리지 않는다."""
    ctx = ctx or DoctorContext()
    result = Diagnosis(getattr(failure, "failure_id", None), failure.direction, failure.reason)
    try:
        result.hypotheses = (_hub_request if failure.direction == "HUB_TO_BANK" else _bank_request)(failure, ctx)
    except Exception as exc:  # 진단기 버그가 401 조회·알림을 막지 않게
        result.hypotheses = [_h("UNKNOWN_REASON", f"진단 중 오류: {type(exc).__name__}")]
    return result

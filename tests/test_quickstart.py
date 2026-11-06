"""SPEC "빠른 시작"의 코드 블록을 실제로 실행한다 (문서가 틀리면 여기서 깨진다).

코드 블록 바로 앞의 마커 주석 `<!-- quickstart:이름 -->`으로 찾는다.
- keypair: 키 쌍 만들기 (출력 형식)
- signing: 서명 2개 + 검증 2개. 2.3 예시 1·2, 2.5 예시 3의 고정 값, 허브 구현(core/signing.py)과 같은 결과
- signing-check: 문서의 "확인" 블록 그대로
- receiver: 받는 쪽 최소 구현. 허브 키로 서명한 요청에 답하고, 응답 서명이 허브 쪽 verify_response로 맞는지, 멱등인지.
  허브의 실제 ping·셀프 시험(3.6·3.7)도 이 앱을 상대로 돌려 본다
- sender·selftest는 실제 허브 주소로 나가는 HTTP 호출이라 여기서 실행하지 않는다 (README의 devserver로 손으로 확인)
"""

import base64
import contextlib
import io
import re
import sys
import types
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_private_key, load_der_public_key
from httpx import ASGITransport, AsyncClient

from clearing_hub.core import bank_client as bank_client_module
from clearing_hub.core.signing import generate_keypair, public_key_of, sign_ed25519, verify_response
from tests.conftest import ADMIN, HUB_PRIVATE_KEY, HUB_PUBLIC_KEY, SIM_PRIVATE_KEY, SignedBank, register_bank

SPEC = Path(__file__).resolve().parents[1] / "docs" / "SPEC.md"
BLOCK = re.compile(r"<!-- quickstart:([\w-]+) -->\n```python\n(.*?)```", re.S)

# 2.3·2.5의 예시 값 (문서의 "확인" 블록과 따로 적어 둔다: 확인 블록을 고쳐도 이 값과는 맞아야 한다)
EX_BANK_PRIVATE, EX_BANK_PUBLIC = "D7frsUqSl9ZuC+7EpHfY1+w6+dAlWqg8vuHVtZfdpg4=", "gxCDPKbN/rIXKDJgn4GZh5JY7I65Cwbk3A/mJjTtPzY="
EX_HUB_PRIVATE = "cycR/h4ydSTPkBGuRFO340cDtdAdwrXQjKC4N1GPgtQ="
EX_TID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
EX_BODY1 = ('{"transfer_id":"7c9e6679-7425-40de-944b-e07fc1f90ae7","to_bank":"902","to_account":"110-123-0001",'
            '"from_account":"110-200-456789","amount":50000,"sender_name":"홍길동","memo":"생활비"}').encode()
EX_BODY3 = ('{"transfer_id":"7c9e6679-7425-40de-944b-e07fc1f90ae7","status":"COMPLETED","holder_name":"김철수",'
            '"received_at":"2026-11-05T01:20:02Z"}').encode()
EX_SIG1 = "SY/3NCjTxAR1dC5UXHM/PkQZR9/SMa+wcDBSIrAit0jeHW5JKKgw4vZWKiN2Z7o5V+ywCY7EyWsTEfLf/F3mBw=="
EX_SIG2 = "IMn6yCKkn/eZGPNY74b3+cNvA/4ax9knE97hh4wCQnPEHe6IEdKREh/Pit3+7bJJjx801OiRbJIvJD0HbKzPAA=="
EX_SIG3 = "D84tooOZEJ6jehp/BN6dVPw1KZCB+eejxpZOqgggeTW7iJznoyNjQGEFwShbhnX7prHkg9uG9e7rP9IxZS91Dg=="

TEST_ACCOUNT = "110-123-000001"  # receiver 블록의 ACCOUNTS에 있는 시험 계좌


def quickstart_blocks() -> dict[str, str]:
    text = SPEC.read_text(encoding="utf-8")
    quickstart = text[text.index("## 빠른 시작"):text.index("### 구현 체크리스트")]
    return {name: code for name, code in BLOCK.findall(quickstart)}


def test_quickstart_has_all_blocks():
    """마커가 빠지거나 이름이 바뀌면 아래 테스트가 조용히 건너뛰지 않게 먼저 확인한다."""
    assert set(quickstart_blocks()) == {"keypair", "signing", "signing-check", "receiver", "sender", "selftest"}


def _run(code: str, name: str) -> tuple[dict, str]:
    namespace = {"__name__": name}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(code, f"SPEC.md quickstart:{name}", "exec"), namespace)
    return namespace, out.getvalue()


@pytest.fixture
def signing(monkeypatch) -> types.ModuleType:
    """signing 블록을 `signing` 모듈로 올린다 (다른 블록이 `from signing import …`로 쓴다)."""
    module = types.ModuleType("signing")
    exec(compile(quickstart_blocks()["signing"], "SPEC.md quickstart:signing", "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, "signing", module)
    return module


def test_keypair_block():
    _, out = _run(quickstart_blocks()["keypair"], "keypair")
    private_line, public_line = out.splitlines()
    private, public = private_line.removeprefix("BANK_PRIVATE_KEY="), public_line.removeprefix("BANK_PUBLIC_KEY=")
    assert len(private) == len(public) == 44
    assert public_key_of(private) == public


def test_signing_fixed_vectors(signing):
    """2.3 예시 1·2, 2.5 예시 3과 같은 값."""
    assert signing.sign_request(EX_BANK_PRIVATE, "POST", "/v1/transfers", EX_BODY1, ts="1762300800")["X-Signature"] == EX_SIG1
    headers = signing.sign_request(EX_HUB_PRIVATE, "GET", f"/receive/{EX_TID}", b"", ts="1762300800")
    assert headers == {"X-Timestamp": "1762300800", "X-Signature": EX_SIG2, "X-Signature-Alg": "ed25519"}
    signed = signing.sign_response(EX_BANK_PRIVATE, 200, EX_SIG2, EX_BODY3, ts="1762300801")
    assert signed == {"X-Response-Timestamp": "1762300801", "X-Response-Signature": EX_SIG3, "X-Signature-Alg": "ed25519"}
    # 예시 시각은 오래전이라 verify_*는 시각 검사(5분)에서 False다
    assert not signing.verify_request(EX_BANK_PUBLIC, "1762300800", "POST", "/v1/transfers", EX_BODY1, EX_SIG1)


def test_signing_check_block(signing):
    _, out = _run(quickstart_blocks()["signing-check"], "signing-check")
    assert out.strip() == "서명 함수 OK"


def test_signing_matches_hub(signing):
    """문서의 함수와 허브 구현이 서로의 서명을 받아 준다 (요청·응답, 지금 시각)."""
    private, public = generate_keypair()
    body = '{"account_num":"110-123-0001"}'.encode()
    hub_made = sign_ed25519(private, "POST", "/inquiry", body)
    assert signing.verify_request(public, hub_made["X-Timestamp"], "POST", "/inquiry", body, hub_made["X-Signature"])
    doc_made = signing.sign_request(private, "post", "/inquiry", body)  # 메서드는 대문자로 바꿔 서명한다
    assert signing.verify_request(public, doc_made["X-Timestamp"], "POST", "/inquiry", body, doc_made["X-Signature"])
    assert not signing.verify_request(public, doc_made["X-Timestamp"], "POST", "/receive", body, doc_made["X-Signature"])
    response = signing.sign_response(private, 404, doc_made["X-Signature"], b'{"error":"X"}')
    assert verify_response(public, response["X-Response-Timestamp"], 404, doc_made["X-Signature"], b'{"error":"X"}',
                           response["X-Response-Signature"]) is None
    assert not signing.verify_response(public, None, 200, "", b"", None)  # 헤더 없음 → 예외 없이 False
    assert not signing.verify_request("not-base64!", "1", "GET", "/x", b"", "also-not")


def test_der_prefixes_in_quickstart():
    """'다른 언어' 안내의 DER 앞부분(PKCS#8·SPKI)이 raw 32바이트 키를 같은 키로 읽게 한다."""
    text = SPEC.read_text(encoding="utf-8")
    pkcs8, spki = "302e020100300506032b657004220420", "302a300506032b6570032100"
    assert pkcs8 in text and spki in text
    private_raw, public_raw = (base64.b64decode(k) for k in (EX_BANK_PRIVATE, EX_BANK_PUBLIC))
    private = load_der_private_key(bytes.fromhex(pkcs8) + private_raw, password=None)
    assert private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) == public_raw
    public = load_der_public_key(bytes.fromhex(spki) + public_raw)
    assert public.public_bytes(Encoding.Raw, PublicFormat.Raw) == public_raw


# ---- 받는 쪽 최소 구현 ----

@pytest.fixture
def receiver(signing, monkeypatch):
    """receiver 블록을 실행해 (namespace, 은행 개인키, 은행 공개키)를 돌려준다. 허브 공개키는 테스트 허브의 것."""
    bank_private, bank_public = generate_keypair()
    monkeypatch.setenv("BANK_PRIVATE_KEY", bank_private)
    monkeypatch.setenv("HUB_PUBLIC_KEY", HUB_PUBLIC_KEY)
    namespace, _ = _run(quickstart_blocks()["receiver"], "bank")
    return namespace, bank_private, bank_public


async def _hub_call(http: AsyncClient, method: str, path: str, body: bytes = b"", key: str = HUB_PRIVATE_KEY,
                    signed_path: str | None = None, bank_code: str = "000"):
    """허브처럼 서명해 부른다. 서명 PATH는 base_url 뒤 부분(`/interbank` 뒤)."""
    headers = {"X-Bank-Code": bank_code, "Content-Type": "application/json",
               **sign_ed25519(key, method, signed_path or path.partition("/interbank")[2], body)}
    res = await http.request(method, path, content=body, headers=headers)
    return res, headers["X-Signature"]


def _response_ok(res, request_signature: str, bank_public: str) -> bool:
    return verify_response(bank_public, res.headers.get("x-response-timestamp"), res.status_code, request_signature,
                           res.content, res.headers.get("x-response-signature")) is None


@pytest.mark.anyio
async def test_receiver_block(receiver):
    namespace, _, bank_public = receiver
    received = namespace["received"]
    async with AsyncClient(transport=ASGITransport(app=namespace["app"]), base_url="http://bank") as http:
        # 4.1 수취 조회: 있는 계좌 200, ping의 없는 계좌 404 ACCOUNT_NOT_FOUND. 둘 다 응답 서명
        res, sig = await _hub_call(http, "POST", "/interbank/inquiry", f'{{"account_num":"{TEST_ACCOUNT}"}}'.encode())
        assert (res.status_code, res.json()) == (200, {"holder_name": "김민수"}) and _response_ok(res, sig, bank_public)
        res, inquiry_sig = await _hub_call(http, "POST", "/interbank/inquiry", b'{"account_num":"000-000-000000"}')
        assert res.status_code == 404 and res.json()["error"] == "ACCOUNT_NOT_FOUND" and _response_ok(res, inquiry_sig, bank_public)

        # 4.2 입금 → 같은 ID 다시 (멱등: 입금 1번, 같은 답)
        tid = str(uuid.uuid4())
        body = (f'{{"transfer_id":"{tid}","from_bank":"902","from_bank_name":"SimBank","from_account":"902-TEST-0001",'
                f'"to_account":"{TEST_ACCOUNT}","amount":1000,"sender_name":"ClearingHub 시험","memo":"셀프 시험"}}').encode()
        first, sig1 = await _hub_call(http, "POST", "/interbank/receive", body)
        again, sig2 = await _hub_call(http, "POST", "/interbank/receive", body)
        for res, sig in ((first, sig1), (again, sig2)):
            assert res.status_code == 200 and _response_ok(res, sig, bank_public)
            assert res.json() == {"transfer_id": tid, "status": "COMPLETED", "holder_name": "김민수"}
        assert list(received) == [tid]
        assert not _response_ok(again, inquiry_sig, bank_public)  # 응답은 그 요청의 서명에 묶여 있다

        # 4.3 입금 확인: 입금한 거래 200, 모르는 거래 404
        res, sig = await _hub_call(http, "GET", f"/interbank/receive/{tid}")
        assert res.status_code == 200 and _response_ok(res, sig, bank_public)
        assert res.json()["status"] == "COMPLETED" and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", res.json()["received_at"])

        # 없는 계좌로 입금 → 404, 기록하지 않음 → 4.3도 404 TRANSFER_NOT_FOUND
        bad_tid = str(uuid.uuid4())
        res, sig = await _hub_call(http, "POST", "/interbank/receive", body.replace(tid.encode(), bad_tid.encode())
                                   .replace(TEST_ACCOUNT.encode(), b"999-999-999999"))
        assert res.status_code == 404 and res.json()["transfer_id"] == bad_tid and _response_ok(res, sig, bank_public)
        res, sig = await _hub_call(http, "GET", f"/interbank/receive/{bad_tid}")
        assert res.status_code == 404 and res.json()["error"] == "TRANSFER_NOT_FOUND" and _response_ok(res, sig, bank_public)
        assert list(received) == [tid]


@pytest.mark.anyio
async def test_receiver_rejects_bad_signatures(receiver):
    """다른 키·전체 경로로 서명·X-Bank-Code가 000이 아님·서명 없음 → 401 SIGNATURE_INVALID, 입금 없음, 401에도 응답 서명."""
    namespace, _, bank_public = receiver
    body = f'{{"transfer_id":"{uuid.uuid4()}","to_account":"{TEST_ACCOUNT}","amount":1000}}'.encode()
    async with AsyncClient(transport=ASGITransport(app=namespace["app"]), base_url="http://bank") as http:
        for kwargs in ({"key": generate_keypair()[0]}, {"signed_path": "/interbank/receive"}, {"bank_code": "901"}):
            res, sig = await _hub_call(http, "POST", "/interbank/receive", body, **kwargs)
            assert res.status_code == 401 and res.json()["error"] == "SIGNATURE_INVALID", kwargs
            assert _response_ok(res, sig, bank_public), kwargs
        res = await http.post("/interbank/receive", content=body)
        assert res.status_code == 401 and _response_ok(res, "", bank_public)  # 서명 없는 요청: request_signature ""
    assert namespace["received"] == {}


@pytest.mark.anyio
async def test_receiver_with_real_hub(client, receiver, monkeypatch):
    """허브의 실제 코드로: 셀프 등록 ping(3.6) ok, 셀프 시험(3.7) 정상·재전송·입금 확인·없는 계좌."""
    namespace, bank_private, bank_public = receiver
    await register_bank(client, "902", "SimBank", private_key=SIM_PRIVATE_KEY)  # 셀프 시험의 보내는 은행
    bank_http = AsyncClient(transport=ASGITransport(app=namespace["app"]), base_url="http://bank")
    monkeypatch.setattr(bank_client_module.bank_client, "_client", bank_http)  # 허브 → 은행 호출을 빠른 시작 앱으로
    try:
        invite = (await client.post("/admin/invites", json={"note": "quickstart"}, headers=ADMIN)).json()["code"]
        res = await client.post("/v1/banks/register", json={"invite_code": invite, "name": "QBank",
                                                            "base_url": "http://bank/interbank", "public_key": bank_public})
        assert res.status_code == 201, res.text
        registered = res.json()
        assert registered["ping"]["ok"] is True, registered["ping"]

        bank = SignedBank(client, registered["bank_code"], registered["api_key"], bank_private)
        res = await bank.post("/v1/test-receive", {"to_account": TEST_ACCOUNT, "amount": 1000})
        assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text
        assert res.json()["receiver_reply"]["signature_valid"] is True
        tid = res.json()["transfer_id"]
        res = await bank.post(f"/v1/test-receive/{tid}/resend")
        assert res.status_code == 200 and res.json()["receiver_reply"]["status"] == 200, res.text
        res = await bank.get(f"/v1/test-receive/{tid}/status")
        assert res.json()["receiver_reply"]["body"]["status"] == "COMPLETED", res.text
        assert list(namespace["received"]) == [tid]  # 재전송해도 입금 1번
        res = await bank.post("/v1/test-receive", {"to_account": "999-999-999999", "amount": 1000})
        assert res.status_code == 404 and res.json()["status"] == "FAILED", res.text
    finally:
        await bank_http.aclose()


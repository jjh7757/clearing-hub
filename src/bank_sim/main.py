"""가짜 은행 서버: SPEC 4절(허브 → 은행 API)만 구현한 참고 구현.

계좌번호 끝 4자리로 동작을 고른다 (PROJECT_PLAN 3.5).
  0001 정상 입금 · 0002 계좌 없음 · 0003 지연(허브 타임아웃) · 0004 500 두 번 뒤 성공 · 0005 입금은 됐는데 응답 안 함
  0006 입금하지 않고 200 COMPLETED (속이는 은행, 대사 시연용. SETTLEMENT_PLAN 9절 8)
그 밖의 번호는 정상 입금.

장부(store.py, sqlite3): `SIM_DB_PATH`가 있으면 그 파일, 없으면 메모리(재시작하면 사라짐).
  시작할 때 SIM_CUSTOMERS의 계좌를 잔액 1,000,000원으로 넣는다 (이미 있으면 건드리지 않음 = 재시작해도 잔액 유지).
  잔액 규칙: 받는 계좌가 고객 계좌면 입금과 같은 트랜잭션에서 잔액 증가 + 거래내역 IN.
            고객 계좌가 아니면(끝자리 시나리오·외부 은행 시험 송금) 거래내역만 남고 잔액은 없다.
            0006은 잔액도 기록도 없다. 같은 transfer_id는 한 번만 입금한다 (멱등).

본인 계좌 조회(SPEC 9.4): 김철수 / 19850315 / 010-9876-5432 → 계좌 2개, 홍길동 / 19900101 / 010-0000-0000 → 1개
(AvengersBank 시연 고객 demo와 같은 사람). 시험 고객의 계좌는 수취 조회·입금 응답에 그 고객 이름이 나온다.

환경변수 (서명은 SPEC 2절, Ed25519만):
  SIM_SIGNING_KEY     이 은행의 Ed25519 개인키(raw 32바이트 base64). **필수**: 없거나 형식이 틀리면 서버가 시작하지 않는다.
                      /interbank/* 2xx·4xx 응답에 응답 서명(SPEC 2.5)을 붙인다. 공개키는 허브에 902로 등록한다
  SIM_HUB_PUBLIC_KEY  허브 공개키(GET /v1/hub-key). 허브 요청의 서명을 검증한다. 비어 있으면 검증을 건너뛴다 (로컬 시험용)
  SIM_DELAY_SECONDS   지연(기본 8), SIM_CI_KEY 가상 CI 키(기본 example-ci-key-change-me)
  SIM_DB_PATH         장부 SQLite 파일 경로 (예: /data/simbank.db). 비어 있으면 메모리

보내기·화면 API (`/sim/api/*`, hub_client.py): 이 은행 고객이 다른 은행으로 보낸다 (허브 3.1~3.4, SPEC 6절 순서)
  SIM_HUB_URL         허브 주소 (운영 http://clearing-hub:8000). 비어 있으면 허브를 부르는 API는 503
  SIM_API_KEY         허브가 902에 준 API 키 (X-Api-Key)
  SIM_UI_PASSWORD     `/sim/api/*`의 `X-Sim-Password`. 비어 있으면 모두 403(기능 꺼짐), 틀리면 401
  SIM_BANK_CODE       이 은행 코드 (기본 902)
실행: SIM_SIGNING_KEY=<개인키> uv run uvicorn bank_sim.main:app --port 8100
"""

import asyncio
import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from clearing_hub.core.ci import virtual_ci
from clearing_hub.core.log_setup import configure_logging
from clearing_hub.core.response_signing import ResponseSigningMiddleware
from clearing_hub.core.signing import ALG_ED25519, public_key_of, verify_ed25519

from .hub_client import HubReply, bank_code, hub, transfer_outcome
from .store import ReceivedView, open_ledger

HOLDER_NAME = "김철수"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버로 띄울 때 서명 키를 확인한다 (테스트는 ASGI로 바로 불러 요청마다 환경 변수를 읽는다)."""
    public_key_of(os.environ.get("SIM_SIGNING_KEY", ""))  # 없거나 틀리면 ValueError → 시작 실패
    yield


app = FastAPI(title="BankSim (가짜 은행)", lifespan=lifespan)
ledger = open_ledger(os.environ)  # SIM_DB_PATH 또는 메모리. 고객 계좌 시드는 SIM_CUSTOMERS 정의 뒤 (아래)
received = ReceivedView(ledger)  # transfer_id → 입금 기록의 읽기 겉면 (멱등성). .clear()는 장부를 처음 상태로 (테스트용)
server_errors_left: dict[str, int] = {}  # 0004: transfer_id → 남은 500 횟수
seen_request_ids: list[tuple[str, str | None]] = []  # (경로, 받은 X-Request-ID). 최근 것만 둔다
logger = logging.getLogger("bank_sim")
configure_logging()  # 허브와 같은 JSON 로그
# 자기 응답에 서명 (SPEC 2.5). 허브와 같은 미들웨어를 쓴다 (따로 가져갈 때는 SPEC 2.5 참고 구현으로 바꾼다)
app.add_middleware(ResponseSigningMiddleware, key_getter=lambda: os.environ.get("SIM_SIGNING_KEY"),
                   path_prefix="/interbank/")


def _error(status: int, code: str, message: str, transfer_id: str | None = None) -> JSONResponse:
    body = {"error": code, "message": message}
    if transfer_id:
        body["transfer_id"] = transfer_id
    return JSONResponse(status_code=status, content=body)


class SignatureError(Exception):
    def __init__(self, code: str):
        self.code = code


@app.exception_handler(SignatureError)
async def signature_error(request: Request, exc: SignatureError):
    return _error(401, exc.code, "서명이 맞지 않습니다")


async def verify_hub(
    request: Request,
    x_bank_code: Annotated[str | None, Header()] = None,
    x_timestamp: Annotated[str | None, Header()] = None,
    x_signature: Annotated[str | None, Header()] = None,
    x_signature_alg: Annotated[str | None, Header()] = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> None:
    """허브(X-Bank-Code 000)의 서명을 허브 공개키로 검증한다 (SPEC 2.2). `X-Signature-Alg`는 ed25519이거나 없어야 한다.
    SIM_HUB_PUBLIC_KEY가 없으면 검증하지 않는다 (로컬 시험용)."""
    # SPEC 2.4: 추적 ID는 서명 대상이 아니다. 받은 그대로 로그에 남긴다 (없어도·형식이 틀려도 거절하지 않는다)
    seen_request_ids.append((request.url.path, x_request_id))
    del seen_request_ids[:-100]  # 오래 켜 둬도 메모리가 쌓이지 않게
    logger.info("허브 요청 %s %s", request.method, request.url.path,
                extra={"request_id": x_request_id, "method": request.method, "path": request.url.path})
    if (x_signature_alg or ALG_ED25519).strip().lower() != ALG_ED25519:
        raise SignatureError("SIGNATURE_INVALID")
    key = os.environ.get("SIM_HUB_PUBLIC_KEY")
    if not key:
        return
    if x_bank_code != "000" or not (x_timestamp and x_signature):
        raise SignatureError("UNAUTHORIZED")
    # 서명 경로는 base_url(`.../interbank`) 뒤의 부분이다 (SPEC 2.2). 프록시가 앞에 무엇을 붙여도 같다
    spec_path = request.url.path.partition("/interbank")[2]
    error = verify_ed25519(key, x_timestamp, request.method, spec_path, await request.body(), x_signature)
    if error:
        raise SignatureError(error)


def _behavior(account_num: str) -> str:
    return account_num[-4:]


async def _delay() -> None:
    await asyncio.sleep(float(os.environ.get("SIM_DELAY_SECONDS", "8")))


class InquiryRequest(BaseModel):
    account_num: str


class ReceiveRequest(BaseModel):
    transfer_id: str
    from_bank: str
    from_bank_name: str = ""
    from_account: str
    to_account: str
    amount: int = Field(gt=0, strict=True)
    sender_name: str
    memo: str = ""


@app.post("/interbank/inquiry", dependencies=[Depends(verify_hub)])
async def inquiry(data: InquiryRequest):
    if _behavior(data.account_num) == "0002":
        return _error(404, "ACCOUNT_NOT_FOUND", "받는 계좌를 찾을 수 없습니다")
    return {"holder_name": holder_of(data.account_num)}


@app.post("/interbank/receive", dependencies=[Depends(verify_hub)])
async def receive(data: ReceiveRequest):
    tid = data.transfer_id
    if (record := ledger.received(tid)) is not None:  # 멱등: 같은 ID는 입금하지 않고 같은 응답
        return {"transfer_id": tid, "status": "COMPLETED", "holder_name": holder_of(record["to_account"])}
    match _behavior(data.to_account):
        case "0002":
            return _error(404, "ACCOUNT_NOT_FOUND", "받는 계좌를 찾을 수 없습니다", tid)
        case "0003":  # 처리 전에 지연 → 허브 타임아웃 → 재시도 때 확인하면 404 → 다시 받아서 입금
            await _delay()
        case "0004":
            left = server_errors_left.setdefault(tid, 2)
            if left > 0:
                server_errors_left[tid] = left - 1
                return _error(500, "INTERNAL_ERROR", "일시 장애", tid)
        case "0006":  # 입금도 기록도 하지 않고 완료라고 답한다. 허브는 COMPLETED로 기록하지만 이 은행 장부에는 없다
            logger.warning("0006: 입금하지 않고 COMPLETED로 답함 (대사 시연용)", extra={"transfer_id": tid})
            return {"transfer_id": tid, "status": "COMPLETED", "holder_name": holder_of(data.to_account)}
    # 고객 계좌면 잔액 증가 + IN, 아니면 IN만. 지연(0003) 중 같은 ID가 먼저 들어왔으면 유일 제약으로 다시 넣지 않는다
    ledger.receive(transfer_id=tid, to_account=data.to_account, amount=data.amount, from_bank=data.from_bank,
                   from_bank_name=data.from_bank_name, from_account=data.from_account,
                   sender_name=data.sender_name, memo=data.memo)
    if _behavior(data.to_account) == "0005":  # 입금은 됐는데 응답이 늦음 → 허브는 결과 조회로만 알 수 있다
        await _delay()
    return {"transfer_id": tid, "status": "COMPLETED", "holder_name": holder_of(data.to_account)}


@app.get("/interbank/receive/{transfer_id}", dependencies=[Depends(verify_hub)])
async def receive_status(transfer_id: str):
    record = ledger.received(transfer_id)
    if record is None:
        return _error(404, "TRANSFER_NOT_FOUND", "그 거래 ID로 입금한 적이 없습니다", transfer_id)
    return {"transfer_id": transfer_id, "status": "COMPLETED", "holder_name": holder_of(record["to_account"]), "received_at": record["received_at"]}


class AccountsRequest(BaseModel):
    ci: str


# SPEC 9.4 본인 계좌 조회용 고객. ci는 SIM_CI_KEY로 그때그때 계산한다 (허브가 나눠 준 CI_KEY와 같아야 한다)
SIM_CUSTOMERS = [
    {"name": HOLDER_NAME, "birth": "19850315", "phone": "010-9876-5432",
     "accounts": [{"account_num": "110-123-0001", "product_name": "자유입출금"},
                  {"account_num": "110-456-0001", "product_name": "급여통장"}]},
    # AvengersBank 시연 고객 demo(홍길동)와 같은 사람. 운영에서 '다른 은행 내 계좌 불러오기'를 바로 시연할 수 있게
    {"name": "홍길동", "birth": "19900101", "phone": "010-0000-0000",
     "accounts": [{"account_num": "110-789-0001", "product_name": "자유입출금"}]},
]


ledger.seed_accounts([{"account_num": a["account_num"], "holder": c["name"], "product_name": a["product_name"]}
                      for c in SIM_CUSTOMERS for a in c["accounts"]])


def holder_of(account_num: str) -> str:
    """시험 고객의 계좌면 그 이름, 아니면 김철수 (계좌번호 끝자리 시나리오용 공통 예금주)."""
    for customer in SIM_CUSTOMERS:
        if any(a["account_num"] == account_num for a in customer["accounts"]):
            return customer["name"]
    return HOLDER_NAME


@app.post("/interbank/accounts", dependencies=[Depends(verify_hub)])
async def accounts(data: AccountsRequest):
    ci_key = os.environ.get("SIM_CI_KEY", "example-ci-key-change-me")
    for customer in SIM_CUSTOMERS:
        if virtual_ci(ci_key, customer["name"], customer["birth"], customer["phone"]) == data.ci:
            return {"accounts": customer["accounts"]}
    return {"accounts": []}  # 고객이 없어도 빈 목록 (고객 존재 여부를 드러내지 않는다)


@app.get("/")
async def root():
    return {"service": "BankSim", "received": ledger.received_count()}


# ---- 보내기·화면 API (`/sim/api/*`): 이 은행 고객 → 다른 은행 (SPEC 3.1~3.4, 6절) ----
# `/interbank/*`와 달리 응답 서명을 붙이지 않는다 (미들웨어는 /interbank/ 경로에만). 시연 비밀번호로만 막는다.

MAX_SEND_AMOUNT = 1_000_000  # 1회 상한 (SIMBANK_PLAN 4: 902 순채무가 커져 셀프 시험이 막히지 않게)
SELF_TEST_ACCOUNT = "902-TEST-0001"  # 허브 셀프 시험 거래의 from_account. 고객 계좌만 허용하므로 들어올 수 없다


class SimApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        self.status, self.code, self.message, self.extra = status, code, message, extra


@app.exception_handler(SimApiError)
async def sim_api_error(request: Request, exc: SimApiError):
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.message, **exc.extra})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """`/sim/api/*`만 5절 형식(VALIDATION_ERROR)으로. 그 밖(`/interbank/*`)은 FastAPI 기본 그대로."""
    if not request.url.path.startswith("/sim/api/"):
        return await request_validation_exception_handler(request, exc)
    fields = ", ".join(".".join(str(x) for x in e["loc"][1:]) or "body" for e in exc.errors())
    return JSONResponse(status_code=422, content={"error": "VALIDATION_ERROR", "message": f"형식이 맞지 않습니다: {fields}"})


async def require_password(x_sim_password: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("SIM_UI_PASSWORD", "")
    if not expected:
        raise SimApiError(403, "FORBIDDEN", "시연 기능이 꺼져 있습니다 (SIM_UI_PASSWORD 없음)")
    if not hmac.compare_digest((x_sim_password or "").encode(), expected.encode()):
        raise SimApiError(401, "UNAUTHORIZED", "시연 비밀번호가 맞지 않습니다")


def _hub_failure(reply: HubReply) -> SimApiError:
    """허브가 거절했거나 응답을 못 받은 조회(3.1·3.2) → 화면에 줄 오류. 허브 오류 코드·메시지는 그대로 담는다.
    허브의 401·403(902 API 키·서명 설정 문제)은 화면이 시연 비밀번호 오류로 오해하지 않게 502로 바꾼다."""
    if reply.status is None:
        return SimApiError(504, reply.body.get("error", "HUB_UNREACHABLE"),
                           reply.body.get("message", "허브에 연결하지 못했습니다"))
    if not reply.verified:
        return SimApiError(502, "HUB_SIGNATURE_INVALID", "허브 응답 서명이 맞지 않습니다", hub_status=reply.status)
    status = reply.status if 400 <= reply.status < 500 and reply.status not in (401, 403) else 502
    return SimApiError(status, reply.body.get("error", "HUB_ERROR"), reply.body.get("message", "허브 오류"),
                       hub_status=reply.status)


def _require_hub() -> None:
    if not hub.configured():
        raise SimApiError(503, "HUB_NOT_CONFIGURED", "허브 주소(SIM_HUB_URL)가 없습니다")


async def _hub_banks() -> list[dict]:
    reply = await hub.call("GET", "/v1/banks")
    if not reply.trusted or reply.status != 200:
        raise _hub_failure(reply)
    return [b for b in reply.body.get("banks", []) if b.get("bank_code") != bank_code()]


async def _hub_inquiry(to_bank: str, to_account: str) -> str:
    reply = await hub.call("POST", "/v1/inquiry", {"to_bank": to_bank, "to_account": to_account})
    if not reply.trusted or reply.status != 200:
        raise _hub_failure(reply)
    return reply.body.get("holder_name") or ""


def _view(tx: dict) -> dict:
    """거래 한 줄 (화면용)."""
    return {"transfer_id": tx["transfer_id"], "direction": tx["direction"], "status": tx["status"],
            "amount": tx["amount"], "account_num": tx["account_num"],
            "counterpart_bank": tx["counterpart_bank"], "counterpart_bank_name": tx["counterpart_bank_name"],
            "counterpart_account": tx["counterpart_account"], "counterpart_name": tx["counterpart_name"],
            "memo": tx["memo"], "fail_code": tx.get("fail_code"), "fail_message": tx.get("fail_message"),
            "created_at": tx["created_at"], "finished_at": tx.get("finished_at")}


def _send_result(transfer_id: str) -> JSONResponse:
    """보내기·refresh 응답: 거래 한 줄 + 보낸 계좌의 지금 잔액. 확정(COMPLETED·FAILED)은 200, PENDING은 202."""
    tx = ledger.sent(transfer_id)
    body = {**_view(tx), "holder_name": tx["counterpart_name"], "balance": ledger.balance(tx["account_num"])}
    return JSONResponse(status_code=202 if tx["status"] == "PENDING" else 200, content=body)


async def _settle(transfer_id: str, reply: HubReply) -> None:
    """3.3·3.4 응답으로 확정한다. 모르면 PENDING 그대로 둔다 (돈이 나갔을 수 있으니 환불하지 않는다)."""
    status, code, message = transfer_outcome(reply)
    if status:
        ledger.finish_send(transfer_id, status, code, message)
    else:
        logger.info("보내기 PENDING 유지", extra={"transfer_id": transfer_id, "hub_status": reply.status})


def _transfer_payload(tx: dict) -> dict:
    """허브 3.3 본문. 다시 보낼 때도 같은 거래 기록에서 만든다 (같은 transfer_id, 같은 내용)."""
    return {"transfer_id": tx["transfer_id"], "to_bank": tx["counterpart_bank"], "to_account": tx["counterpart_account"],
            "from_account": tx["account_num"], "amount": tx["amount"], "sender_name": holder_of(tx["account_num"]),
            "memo": tx["memo"]}


class SimInquiryRequest(BaseModel):
    to_bank: str = Field(pattern=r"^\d{3}$")
    to_account: str = Field(min_length=1, max_length=50)


class SimTransferRequest(BaseModel):
    from_account: str = Field(min_length=1, max_length=50)
    to_bank: str = Field(pattern=r"^\d{3}$")
    to_account: str = Field(min_length=1, max_length=50)
    amount: int = Field(strict=True)  # 범위는 핸들러에서 (AMOUNT_INVALID). 50000.0·"50000"은 422
    memo: str = Field(default="", max_length=100)


@app.get("/sim/api/customers", dependencies=[Depends(require_password)])
async def sim_customers():
    """고객과 계좌·잔액 (화면의 고객 고르기)."""
    balances = {a["account_num"]: a["balance"] for a in ledger.customer_accounts()}
    return {"bank_code": bank_code(), "customers": [
        {"name": c["name"], "accounts": [{**a, "balance": balances.get(a["account_num"], 0)} for a in c["accounts"]]}
        for c in SIM_CUSTOMERS]}


@app.get("/sim/api/accounts/{account_num}/transactions", dependencies=[Depends(require_password)])
async def sim_account_transactions(account_num: str, limit: int = 50):
    """고객 계좌의 잔액과 거래내역(최근 것부터). 고객 계좌가 아니면 404."""
    acc = ledger.account(account_num)
    if acc is None:
        raise SimApiError(404, "ACCOUNT_NOT_FOUND", "SimBank 고객 계좌가 아닙니다")
    txs = ledger.transactions(account_num)[:max(1, min(limit, 200))]
    return {"account_num": account_num, "holder": acc["holder"], "product_name": acc["product_name"],
            "balance": acc["balance"], "transactions": [_view(t) for t in txs]}


@app.get("/sim/api/banks", dependencies=[Depends(require_password)])
async def sim_banks():
    """허브 3.1 목록에서 이 은행을 뺀 것 (받는 은행 고르기)."""
    _require_hub()
    return {"banks": await _hub_banks()}


@app.post("/sim/api/inquiry", dependencies=[Depends(require_password)])
async def sim_inquiry(data: SimInquiryRequest):
    """받는 분 확인 (허브 3.2 그대로). 돈은 움직이지 않는다."""
    _require_hub()
    holder = await _hub_inquiry(data.to_bank, data.to_account)
    return {"to_bank": data.to_bank, "to_account": data.to_account, "holder_name": holder}


@app.post("/sim/api/transfers", dependencies=[Depends(require_password)])
async def sim_send(data: SimTransferRequest):
    """보내기 (SPEC 6절 순서). 확인 → 수취 조회 → [차감 + OUT PENDING] → 허브 3.3 → 확정·환불 또는 PENDING 유지."""
    acc = ledger.account(data.from_account)
    if acc is None or data.from_account == SELF_TEST_ACCOUNT:
        raise SimApiError(404, "FROM_ACCOUNT_NOT_FOUND", "보내는 계좌가 SimBank 고객 계좌가 아닙니다")
    if not 1 <= data.amount <= MAX_SEND_AMOUNT:
        raise SimApiError(400, "AMOUNT_INVALID", f"금액은 1원 이상 {MAX_SEND_AMOUNT:,}원 이하입니다")
    if data.to_bank == bank_code():
        raise SimApiError(400, "SAME_BANK", "같은 은행 안의 이체는 지원하지 않습니다")
    if acc["balance"] < data.amount:
        raise SimApiError(400, "INSUFFICIENT_BALANCE", "잔액이 모자랍니다", balance=acc["balance"])
    _require_hub()

    holder = await _hub_inquiry(data.to_bank, data.to_account)  # 실패면 그대로 돌려준다 (잔액은 그대로)
    bank_name = ""
    try:  # 거래내역에 받는 은행 이름을 남기려고. 못 받아도 보내기는 진행한다
        bank_name = next((b.get("name", "") for b in await _hub_banks() if b.get("bank_code") == data.to_bank), "")
    except SimApiError:
        pass

    transfer_id = str(uuid.uuid4())  # 소문자 UUID v4 (SPEC 3.3)
    if not ledger.start_send(transfer_id=transfer_id, from_account=data.from_account, amount=data.amount,
                             to_bank=data.to_bank, to_bank_name=bank_name, to_account=data.to_account,
                             to_name=holder, memo=data.memo):  # 그사이 다른 보내기로 잔액이 줄었으면
        raise SimApiError(400, "INSUFFICIENT_BALANCE", "잔액이 모자랍니다", balance=ledger.balance(data.from_account))
    reply = await hub.call("POST", "/v1/transfers", _transfer_payload(ledger.sent(transfer_id)))
    await _settle(transfer_id, reply)
    return _send_result(transfer_id)


@app.post("/sim/api/transfers/{transfer_id}/refresh", dependencies=[Depends(require_password)])
async def sim_refresh(transfer_id: str):
    """PENDING이면 허브 3.4로 다시 보고 확정·환불한다. 이미 확정이면 그대로 (화면 폴링이 부른다).
    3.4가 404 TRANSFER_NOT_FOUND면 허브에 아직 접수 기록이 없는 것 → 되돌리지 않고 같은 transfer_id로 3.3을 다시 보낸다
    (SPEC 3.4·6절 4-1. 멱등이라 안전하다)."""
    tx = ledger.sent(transfer_id)
    if tx is None:
        raise SimApiError(404, "TRANSFER_NOT_FOUND", "그 거래 ID로 보낸 적이 없습니다", transfer_id=transfer_id)
    if tx["status"] != "PENDING":
        return _send_result(transfer_id)
    _require_hub()
    reply = await hub.call("GET", f"/v1/transfers/{transfer_id}")
    if reply.trusted and reply.status == 404 and reply.body.get("error") == "TRANSFER_NOT_FOUND":
        reply = await hub.call("POST", "/v1/transfers", _transfer_payload(tx))
    await _settle(transfer_id, reply)
    return _send_result(transfer_id)

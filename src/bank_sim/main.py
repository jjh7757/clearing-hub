"""가짜 은행 서버: SPEC 4절(허브 → 은행 API)만 구현한 참고 구현.

계좌번호 끝 4자리로 동작을 고른다 (PROJECT_PLAN 3.5).
  0001 정상 입금 · 0002 계좌 없음 · 0003 지연(허브 타임아웃) · 0004 500 두 번 뒤 성공 · 0005 입금은 됐는데 응답 안 함
그 밖의 번호는 정상 입금. 저장은 메모리(재시작하면 사라짐).

본인 계좌 조회(SPEC 9.4): 김철수 / 19850315 / 010-9876-5432 → 계좌 2개, 홍길동 / 19900101 / 010-0000-0000 → 1개
(AvengersBank 시연 고객 demo와 같은 사람). 시험 고객의 계좌는 수취 조회·입금 응답에 그 고객 이름이 나온다.

환경변수: SIM_HUB_SECRET(허브가 발급한 secret, 서명 검증), SIM_DELAY_SECONDS(지연, 기본 8),
          SIM_CI_KEY(가상 CI 키, 기본 example-ci-key-change-me)
실행: uv run uvicorn bank_sim.main:app --port 8100
"""

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from clearing_hub.core.ci import virtual_ci
from clearing_hub.core.signing import verify

HOLDER_NAME = "김철수"
app = FastAPI(title="BankSim (가짜 은행)")
received: dict[str, dict] = {}  # transfer_id → 입금 기록 (멱등성)
server_errors_left: dict[str, int] = {}  # 0004: transfer_id → 남은 500 횟수
seen_request_ids: list[tuple[str, str | None]] = []  # (경로, 받은 X-Request-ID). 최근 것만 둔다
logger = logging.getLogger("bank_sim")


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
    x_request_id: Annotated[str | None, Header()] = None,
) -> None:
    """허브(X-Bank-Code 000)의 서명을 허브가 준 secret으로 검증한다. secret이 없으면 검증하지 않는다 (로컬 시험용)."""
    # SPEC 2.4: 추적 ID는 서명 대상이 아니다. 받은 그대로 로그에 남긴다 (없어도·형식이 틀려도 거절하지 않는다)
    seen_request_ids.append((request.url.path, x_request_id))
    del seen_request_ids[:-100]  # 오래 켜 둬도 메모리가 쌓이지 않게
    logger.info("허브 요청 %s %s [request_id=%s]", request.method, request.url.path, x_request_id or "-")
    secret = os.environ.get("SIM_HUB_SECRET")
    if not secret:
        return
    if x_bank_code != "000" or not (x_timestamp and x_signature):
        raise SignatureError("UNAUTHORIZED")
    error = verify(secret, x_timestamp, await request.body(), x_signature)
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
    if tid in received:  # 멱등: 같은 ID는 입금하지 않고 같은 응답
        return {"transfer_id": tid, "status": "COMPLETED", "holder_name": holder_of(received[tid]["to_account"])}
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
    received[tid] = {**data.model_dump(), "received_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if _behavior(data.to_account) == "0005":  # 입금은 됐는데 응답이 늦음 → 허브는 결과 조회로만 알 수 있다
        await _delay()
    return {"transfer_id": tid, "status": "COMPLETED", "holder_name": holder_of(data.to_account)}


@app.get("/interbank/receive/{transfer_id}", dependencies=[Depends(verify_hub)])
async def receive_status(transfer_id: str):
    record = received.get(transfer_id)
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
    return {"service": "BankSim", "received": len(received)}

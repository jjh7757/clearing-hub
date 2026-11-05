"""가짜 은행 서버: SPEC 4절(허브 → 은행 API)만 구현한 참고 구현.

계좌번호 끝 4자리로 동작을 고른다 (PROJECT_PLAN 3.5).
  0001 정상 입금 · 0002 계좌 없음 · 0003 지연(허브 타임아웃) · 0004 500 두 번 뒤 성공 · 0005 입금은 됐는데 응답 안 함
그 밖의 번호는 정상 입금. 저장은 메모리(재시작하면 사라짐).

환경변수: SIM_HUB_SECRET(허브가 발급한 secret, 서명 검증), SIM_DELAY_SECONDS(지연, 기본 8)
실행: uv run uvicorn bank_sim.main:app --port 8100
"""

import asyncio
import os
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from clearing_hub.core.signing import verify

HOLDER_NAME = "김철수"
app = FastAPI(title="BankSim (가짜 은행)")
received: dict[str, dict] = {}  # transfer_id → 입금 기록 (멱등성)
server_errors_left: dict[str, int] = {}  # 0004: transfer_id → 남은 500 횟수


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
) -> None:
    """허브(X-Bank-Code 000)의 서명을 허브가 준 secret으로 검증한다. secret이 없으면 검증하지 않는다 (로컬 시험용)."""
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
    return {"holder_name": HOLDER_NAME}


@app.post("/interbank/receive", dependencies=[Depends(verify_hub)])
async def receive(data: ReceiveRequest):
    tid = data.transfer_id
    if tid in received:  # 멱등: 같은 ID는 입금하지 않고 같은 응답
        return {"transfer_id": tid, "status": "COMPLETED", "holder_name": HOLDER_NAME}
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
    return {"transfer_id": tid, "status": "COMPLETED", "holder_name": HOLDER_NAME}


@app.get("/interbank/receive/{transfer_id}", dependencies=[Depends(verify_hub)])
async def receive_status(transfer_id: str):
    record = received.get(transfer_id)
    if record is None:
        return _error(404, "TRANSFER_NOT_FOUND", "그 거래 ID로 입금한 적이 없습니다", transfer_id)
    return {"transfer_id": transfer_id, "status": "COMPLETED", "holder_name": HOLDER_NAME, "received_at": record["received_at"]}


@app.get("/")
async def root():
    return {"service": "BankSim", "received": len(received)}

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank
from clearing_hub.core.bank_client import bank_client
from clearing_hub.core.database import get_db
from clearing_hub.core.exceptions import ApiException
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.banks.schemas import BANK_CODE

router = APIRouter(prefix="/v1/inquiry", tags=["inquiry"])
logger = logging.getLogger(__name__)


class InquiryRequest(BaseModel):
    to_bank: str = BANK_CODE
    to_account: str = Field(min_length=1, max_length=50)


class InquiryResponse(InquiryRequest):
    holder_name: str


@router.post("", response_model=InquiryResponse)
async def inquiry(
    data: InquiryRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[Bank, Depends(current_bank)],
):
    """수취 조회 중계 (SPEC 3.2). 받는 은행의 /inquiry를 불러 예금주 이름을 돌려준다. 돈은 움직이지 않는다."""
    receiver = await bank_service.require_active_bank(db, data.to_bank)
    reply = await bank_client.inquiry(receiver, data.to_account)
    if reply.ok and reply.body.get("holder_name"):
        return InquiryResponse(**data.model_dump(), holder_name=str(reply.body["holder_name"]))
    if reply.auth_failed:
        # 받는 은행이 허브 서명을 거절: 보내는 은행에 401을 그대로 넘기면 자기 서명이 틀린 줄 안다
        logger.error("은행 %s가 허브 인증을 거절 (HTTP %s %s). secret 설정 확인 필요",
                     receiver.bank_code, reply.status, reply.body.get("error"), extra={"bank_code": receiver.bank_code})
        raise ApiException(502, "RECEIVER_ERROR", reply.message or "받는 은행에 일시 장애가 있습니다")
    if reply.rejected:
        raise ApiException(reply.status, reply.error, str(reply.body.get("message") or "받는 은행이 조회를 거절했습니다"))
    # 5xx·타임아웃은 받는 은행의 오류 코드를 그대로 넘기지 않고 허브 코드로 바꾼다 (SPEC 3.2)
    if reply.status is None:
        raise ApiException(504, "RECEIVER_TIMEOUT", "받는 은행이 응답하지 않습니다")
    raise ApiException(502, "RECEIVER_ERROR", "받는 은행에 일시 장애가 있습니다")

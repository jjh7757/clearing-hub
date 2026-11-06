"""연동 진단 API 형식 (SPEC 3.8)."""

from pydantic import BaseModel


class HypothesisOut(BaseModel):
    code: str
    title: str
    detail: str
    spec_ref: str
    fix: str
    verified: bool


class FailureOut(BaseModel):
    """실패 한 건의 원문 요약. 요청 본문은 앞 500자만 보여 준다 (진단은 원문 전체로 한다)."""

    failure_id: int | None
    direction: str
    reason: str
    message: str | None
    method: str
    path: str
    headers: dict
    body_preview: str
    body_length: int
    http_status: int | None  # HUB_TO_BANK: 은행 응답 상태
    response_headers: dict | None
    response_body_preview: str | None
    created_at: str


class Finding(BaseModel):
    failure: FailureOut
    hypotheses: list[HypothesisOut]


class FailureList(BaseModel):
    bank_code: str
    findings: list[Finding]  # 규칙 진단 (실패별, 새것부터)

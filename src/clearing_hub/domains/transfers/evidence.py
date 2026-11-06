"""이체 증거 보관 (SETTLEMENT_PLAN 12.2 9번·12.3, transfer_evidence). 추가만 하고 고치거나 지우지 않는다.

남기는 것: 이체 접수 요청(REQUEST_IN, 새 거래일 때만), 허브 → 받는 은행 /receive·/receive/{id} 요청(RECEIVE_OUT)과
그 응답(RECEIVE_REPLY, 처음과 재시도 모두). 수취 조회·본인 계좌 조회·대사 조회는 남기지 않는다 (이체 관련만).
본문은 서명한 바이트 그대로다. JSON으로 풀었다 다시 만들면 서명이 맞지 않는다.
"""

import base64

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import VerifiedRequest
from clearing_hub.core.bank_client import BankReply
from clearing_hub.domains.transfers.models import EvidenceDirection, TransferEvidence
from clearing_hub.domains.transfers.schemas import spec_time


def record_request_in(db: AsyncSession, transfer_id: str, verified: VerifiedRequest) -> None:
    """은행 → 허브 접수 요청. 인증(core/auth.current_bank)을 통과한 것만 오므로 signature_valid는 True."""
    db.add(TransferEvidence(
        transfer_id=transfer_id, direction=EvidenceDirection.REQUEST_IN, method=verified.method, path=verified.path,
        headers_json=verified.headers, body=verified.body, signature_alg=verified.signature_alg, signature_valid=True,
    ))


def record_exchange(db: AsyncSession, transfer_id: str, reply: BankReply) -> None:
    """허브 → 받는 은행 요청과 (받았으면) 그 응답. 응답의 signature_valid는 bank_client의 응답 서명 검증 결과다.
    응답을 못 받았으면(타임아웃·연결 실패) 요청만 남긴다. 실제로 오간 것이 없는 응답(테스트 대역)은 남기지 않는다."""
    ex = reply.exchange
    if ex is None:
        return
    db.add(TransferEvidence(
        transfer_id=transfer_id, direction=EvidenceDirection.RECEIVE_OUT, method=ex.method, path=ex.path,
        headers_json=ex.request_headers, body=ex.request_body, signature_alg=ex.signature_alg, signature_valid=None,
    ))
    if ex.response_status is None:
        return
    db.add(TransferEvidence(
        transfer_id=transfer_id, direction=EvidenceDirection.RECEIVE_REPLY, http_status=ex.response_status,
        headers_json=ex.response_headers, body=ex.response_body,
        signature_alg=ex.response_headers.get("X-Signature-Alg") if ex.response_headers.get("X-Response-Signature") else None,
        signature_valid=reply.signature_valid,
    ))


def _body_view(body: bytes) -> tuple[str, str]:
    try:
        return body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(body).decode("ascii"), "base64"


async def list_for_transfer(db: AsyncSession, transfer_id: str) -> list[dict]:
    """관리자 분쟁 조회용. body는 UTF-8 문자열(디코드 안 되면 base64, body_encoding으로 구분)."""
    stmt = select(TransferEvidence).where(TransferEvidence.transfer_id == transfer_id).order_by(TransferEvidence.evidence_id)
    rows = []
    for e in (await db.scalars(stmt)).all():
        body, encoding = _body_view(e.body)
        rows.append({
            "evidence_id": e.evidence_id, "direction": e.direction, "method": e.method, "path": e.path,
            "http_status": e.http_status, "headers": e.headers_json, "body": body, "body_encoding": encoding,
            "signature_alg": e.signature_alg, "signature_valid": e.signature_valid, "created_at": spec_time(e.created_at),
        })
    return rows

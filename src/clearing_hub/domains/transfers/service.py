"""이체 중계 상태 머신 (SPEC 3.3·3.4, PROJECT_PLAN 3.2).

ACCEPTED → DELIVERING → COMPLETED / FAILED / PENDING → (재시도) → COMPLETED / FAILED / UNKNOWN
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core import alerts
from clearing_hub.core.auth import VerifiedRequest
from clearing_hub.core.bank_client import BankClient, BankReply, bank_client
from clearing_hub.core.config import get_settings
from clearing_hub.core.request_id import get_request_id
from clearing_hub.core.exceptions import ApiException, BadRequest, Forbidden, NotFound
from clearing_hub.domains.accounts import service as account_service
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.reserve import service as reserve_service
from clearing_hub.domains.transfers import evidence
from clearing_hub.domains.transfers.exposure import open_exposure
from clearing_hub.domains.transfers.models import Transfer, TransferEvent, TransferStatus
from clearing_hub.domains.transfers.schemas import TransferRequest, TransferResolve

logger = logging.getLogger(__name__)
FINAL = {TransferStatus.COMPLETED, TransferStatus.FAILED}


def _now() -> datetime:
    return datetime.now(UTC)


def _set_status(db: AsyncSession, transfer: Transfer, status: TransferStatus, detail: str | None = None) -> None:
    # 정산에 들어간 거래는 바꾸지 않는다 (결제완결성, SETTLEMENT_PLAN 2절 8번). 정정은 예치금 ADJUST 분개로만.
    # 정산 대상은 COMPLETED뿐이고 COMPLETED에서 나가는 전이는 없으므로, 여기 걸리면 호출한 쪽의 버그다
    if transfer.settlement_run_id is not None:
        raise RuntimeError(f"정산된 거래는 상태를 바꿀 수 없습니다 ({transfer.transfer_id}, run {transfer.settlement_run_id})")
    db.add(TransferEvent(transfer_id=transfer.transfer_id, from_status=transfer.status, to_status=status, detail=detail))
    transfer.status = status
    if status in FINAL:
        transfer.finished_at = _now()


def _receive_payload(transfer: Transfer, sender_bank: Bank) -> dict:
    return {
        "transfer_id": transfer.transfer_id, "from_bank": transfer.from_bank, "from_bank_name": sender_bank.name,
        "from_account": transfer.from_account, "to_account": transfer.to_account, "amount": transfer.amount,
        "sender_name": transfer.sender_name, "memo": transfer.memo,
    }


def _apply_reply(db: AsyncSession, transfer: Transfer, reply: BankReply, *, no_answer_status: TransferStatus) -> None:
    """받는 은행의 /receive 응답을 상태에 반영한다."""
    if reply.ok:
        transfer.holder_name = reply.body.get("holder_name") or transfer.holder_name
        _set_status(db, transfer, TransferStatus.COMPLETED, "받는 은행 입금 확인")
    elif reply.rejected:
        # 4xx는 재시도하지 않는다 (SPEC 4.2 규칙 5). 받는 은행 인증 거절(401·403)은 입금 전이라 실패로 끝내고
        # 보내는 은행이 출금을 되돌리게 한다. 코드는 RECEIVER_ERROR로 바꿔 보낸다 (BankReply.error)
        if reply.auth_failed:
            logger.error("은행 %s가 허브 인증을 거절 (HTTP %s %s). 그 은행의 허브 공개키 설정 확인 필요", transfer.to_bank, reply.status,
                         reply.body.get("error"), extra={"request_id": transfer.request_id, "transfer_id": transfer.transfer_id,
                                                          "bank_code": transfer.to_bank})
        transfer.fail_code = reply.error
        transfer.fail_message = (reply.message or "받는 은행이 거절했습니다")[:200]
        _set_status(db, transfer, TransferStatus.FAILED, f"{transfer.fail_code}: {transfer.fail_message}")
    elif reply.signature_valid is False:
        # 받는 은행의 응답 서명이 없거나 틀림: 응답 없음과 같이 4.3 확인으로 넘긴다 (SETTLEMENT_PLAN 12.4)
        _set_status(db, transfer, no_answer_status, "응답 서명 검증 실패 (응답 없음으로 처리)")
    else:
        _set_status(db, transfer, no_answer_status, f"응답 없음 (HTTP {reply.status})")


async def accept_and_deliver(
    db: AsyncSession, sender: Bank, data: TransferRequest, client: BankClient = bank_client,
    request_evidence: VerifiedRequest | None = None,
) -> tuple[Transfer, bool]:
    """이체 접수 → 받는 은행 호출. (transfer, 이미 있던 거래인지)를 돌려준다.

    같은 transfer_id가 다시 오면 처리하지 않고 저장된 결과를 돌려준다 (멱등, SPEC 3.3). 이미 접수된 거래이므로
    순채무한도 검사도 하지 않는다.

    접수 전 검사 순서: 1회 상한 → SAME_BANK → 받는 은행 → account_ref → 순채무한도(_check_net_debit_cap).
    순채무한도 검사는 보내는 은행의 예치금 행을 잠그고 하며, 잠금은 바로 뒤 접수 저장 커밋에서 풀린다.

    request_evidence(서명 검증을 통과한 접수 요청 원문)는 새 거래일 때만 접수와 같은 커밋으로 남긴다 (REQUEST_IN, 12.3).
    멱등 재전송·접수 전 거절은 거래가 새로 생기지 않으므로 남기지 않는다.
    """
    existing = await db.get(Transfer, data.transfer_id)
    if existing is not None:
        if existing.from_bank != sender.bank_code:
            raise Forbidden()
        return existing, True

    settings = get_settings()
    if data.amount > settings.max_transfer_amount:
        raise BadRequest("LIMIT_EXCEEDED", f"1회 이체 상한({settings.max_transfer_amount:,}원)을 넘었습니다", data.transfer_id)
    if data.to_bank == sender.bank_code:
        raise BadRequest("SAME_BANK", "같은 은행 안의 이체는 중계하지 않습니다", data.transfer_id)
    receiver = await bank_service.require_active_bank(db, data.to_bank, data.transfer_id)
    to_account = data.to_account
    if data.to_account_ref is not None:  # 본인 계좌 조회로 받은 참조값 → 실제 계좌번호 (SPEC 9.3)
        to_account = await account_service.resolve_ref(db, sender, data.to_account_ref, data.to_bank, data.transfer_id)
    await _check_net_debit_cap(db, sender, data)

    # 1) 접수 저장을 먼저 커밋한다. 이후 은행 호출 중에 허브가 죽어도 거래 기록은 남는다
    transfer = Transfer(**data.model_dump(exclude={"to_account", "to_account_ref"}), to_account=to_account,
                        from_bank=sender.bank_code, status=TransferStatus.ACCEPTED,
                        request_id=get_request_id())  # 재시도에도 같은 추적 ID를 보낸다 (SPEC 2.4)
    db.add(transfer)
    db.add(TransferEvent(transfer_id=transfer.transfer_id, from_status=None, to_status=TransferStatus.ACCEPTED, detail="접수"))
    if request_evidence is not None:
        evidence.record_request_in(db, transfer.transfer_id, request_evidence)
    _set_status(db, transfer, TransferStatus.DELIVERING, f"{receiver.name}에 전달")
    transfer.delivered_at = _now()
    await db.commit()

    # 2) 받는 은행 호출. 보낸 요청과 받은 응답은 결과와 같은 커밋으로 남긴다 (RECEIVE_OUT·RECEIVE_REPLY)
    reply = await client.receive(receiver, _receive_payload(transfer, sender), transfer.request_id)
    evidence.record_exchange(db, transfer.transfer_id, reply)
    _apply_reply(db, transfer, reply, no_answer_status=TransferStatus.PENDING)
    await db.commit()
    return transfer, False


async def _check_net_debit_cap(db: AsyncSession, sender: Bank, data: TransferRequest) -> None:
    """미정산 순채무 + amount ≤ 예치금 + 순채무한도가 아니면 400 LIMIT_EXCEEDED (SETTLEMENT_PLAN 2절 6번, 4.4·5.2).

    보내는 은행이 예치금 없이 보내는 것을 막는다 (담보·일중당좌대출 역할). 1회 상한과 같은 코드이므로 message로 구분한다.
    BadRequest는 라우터가 `status: "FAILED"`를 붙여 돌려주므로 보내는 은행은 출금을 되돌린다 (SPEC 3.3 접수 전 거절).
    거래를 저장하기 전이라 허브에 거래가 남지 않는다.

    예치금 행을 SELECT ... FOR UPDATE로 잠근 **뒤에** 순채무를 계산한다. 같은 은행의 동시 접수 두 건이 둘 다
    통과하지 않게 하기 위해서다: 뒤의 요청은 앞의 요청이 접수를 커밋할 때까지 기다리고, 그다음 계산하는 순채무에는
    앞의 거래(DELIVERING)가 들어 있다. 잠금은 호출한 쪽의 접수 저장 커밋(또는 거절 뒤 세션 롤백)에서 풀린다.
    """
    account = await reserve_service.lock_for_check(db, sender.bank_code)
    exposure = await open_exposure(db, sender.bank_code)
    if exposure + data.amount > account.balance + sender.net_debit_cap:
        raise BadRequest("LIMIT_EXCEEDED",
                         f"순채무한도를 넘었습니다 (예치금 {account.balance:,}원 + 한도 {sender.net_debit_cap:,}원, "
                         f"미정산 순채무 {exposure:,}원, 요청 {data.amount:,}원)", data.transfer_id)


async def get_transfer(db: AsyncSession, sender: Bank, transfer_id: str) -> Transfer:
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise NotFound("TRANSFER_NOT_FOUND", "거래를 찾을 수 없습니다", transfer_id)
    if transfer.from_bank != sender.bank_code:
        raise Forbidden()
    return transfer


async def _fill_holder_name(transfer: Transfer, receiver: Bank, client: BankClient) -> None:
    """입금 확인(SPEC 4.3) 응답에 이름이 없으면 수취 조회(4.1)로 채운다. 실패해도 이체 결과는 그대로 둔다."""
    reply = await client.inquiry(receiver, transfer.to_account, transfer.request_id)
    if reply.ok and reply.body.get("holder_name"):
        transfer.holder_name = str(reply.body["holder_name"])[:50]


async def _confirm_one(db: AsyncSession, transfer: Transfer, client: BankClient) -> bool:
    """미확인 거래 하나를 받는 은행에 확인한다 (SPEC 4.3 → 404면 4.2 재전송). 확정됐으면 True.
    확정되지 않으면 retry_count를 올리고, RETRY_MAX_COUNT에 닿으면 UNKNOWN으로 둔다."""
    receiver = await db.get(Bank, transfer.to_bank)
    sender = await db.get(Bank, transfer.from_bank)
    if receiver is None or sender is None:
        return False
    status = await client.receive_status(receiver, transfer.transfer_id, transfer.request_id)
    evidence.record_exchange(db, transfer.transfer_id, status)  # 4.3 확인의 COMPLETED도 "답한 사실"의 증거다
    if status.ok and status.body.get("status") == "COMPLETED":
        _apply_reply(db, transfer, status, no_answer_status=TransferStatus.PENDING)
    elif status.status == 404:
        # 은행이 그 ID로 입금한 적 없음 → /receive를 다시 보낸다 (은행 쪽 멱등 처리에 기댄다)
        reply = await client.receive(receiver, _receive_payload(transfer, sender), transfer.request_id)
        evidence.record_exchange(db, transfer.transfer_id, reply)
        _apply_reply(db, transfer, reply, no_answer_status=TransferStatus.PENDING)
    else:
        reason = "응답 서명 검증 실패" if status.signature_valid is False else f"HTTP {status.status}"
        _set_status(db, transfer, TransferStatus.PENDING, f"확인 실패 ({reason})")
    logger.info("거래 재확인 %s → %s", transfer.transfer_id, transfer.status,
                extra={"request_id": transfer.request_id, "transfer_id": transfer.transfer_id, "bank_code": transfer.to_bank})
    if transfer.status == TransferStatus.COMPLETED and not transfer.holder_name:
        await _fill_holder_name(transfer, receiver, client)
    if transfer.status in FINAL:
        return True
    transfer.retry_count += 1
    if transfer.retry_count >= get_settings().retry_max_count:
        _set_status(db, transfer, TransferStatus.UNKNOWN, f"{transfer.retry_count}회 확인 실패, 관리자 확인 필요")
        logger.error("거래 %s 결과를 알 수 없음 (%s → %s)", transfer.transfer_id, transfer.from_bank, transfer.to_bank,
                     extra={"request_id": transfer.request_id, "transfer_id": transfer.transfer_id})
        alerts.notify("unknown", f"거래 결과를 알 수 없습니다 (UNKNOWN, {transfer.from_bank} → {transfer.to_bank}, "
                      f"{transfer.amount:,}원, {transfer.retry_count}회 확인 실패). 받는 은행 확인 뒤 recheck 또는 resolve",
                      transfer_id=transfer.transfer_id, bank_code=transfer.to_bank)
    return False


async def confirm_pending(db: AsyncSession, client: BankClient = bank_client) -> int:
    """미확인 거래(PENDING, 또는 호출 중 허브가 죽어 DELIVERING으로 남은 것)를 받는 은행에 확인한다.
    재시도 태스크와 관리 화면의 '다시 확인'이 부른다. 확정(COMPLETED/FAILED)된 건수를 돌려준다."""
    stmt = select(Transfer).where(Transfer.status.in_([TransferStatus.PENDING, TransferStatus.DELIVERING]))
    confirmed = 0
    for transfer in (await db.scalars(stmt)).all():
        if await _confirm_one(db, transfer, client):
            confirmed += 1
    await db.commit()
    return confirmed


async def _require_unknown(db: AsyncSession, transfer_id: str) -> Transfer:
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise NotFound("TRANSFER_NOT_FOUND", "거래를 찾을 수 없습니다", transfer_id)
    if transfer.status != TransferStatus.UNKNOWN:
        # PENDING·DELIVERING은 재시도 태스크가 아직 확인 중이고, COMPLETED·FAILED는 이미 확정됐다
        raise ApiException(409, "TRANSFER_NOT_UNKNOWN", f"UNKNOWN 상태인 거래만 정리할 수 있습니다 (지금 {transfer.status})",
                           transfer_id)
    return transfer


async def recheck_unknown(db: AsyncSession, transfer_id: str, client: BankClient = bank_client) -> Transfer:
    """UNKNOWN 거래를 자동 확인으로 되돌린다: 재시도 횟수를 0으로 하고 지금 한 번 확인한다.
    받는 은행 장애가 풀린 뒤에 쓴다. 이번에도 확정되지 않으면 PENDING으로 남아 재시도 태스크가 이어서 확인한다."""
    transfer = await _require_unknown(db, transfer_id)
    transfer.retry_count = 0
    _set_status(db, transfer, TransferStatus.PENDING, "관리자 재확인 요청")
    await _confirm_one(db, transfer, client)
    await db.commit()
    return transfer


async def resolve_unknown(db: AsyncSession, transfer_id: str, data: TransferResolve) -> Transfer:
    """UNKNOWN 거래를 관리자가 받는 은행과 직접 확인한 결과로 확정한다 (SPEC 3.4).

    FAILED로 정하면 보내는 은행이 출금을 되돌리므로, 받는 은행에 입금이 **없다는 것**을 확인한 뒤에만 쓴다.
    보내는 은행에는 fail_code RECEIVER_ERROR와 고정 설명만 보이고, 관리자가 쓴 사유는 이력에만 남는다."""
    transfer = await _require_unknown(db, transfer_id)
    if data.status == TransferStatus.COMPLETED:
        transfer.holder_name = data.holder_name or transfer.holder_name
    else:
        transfer.fail_code = "RECEIVER_ERROR"
        transfer.fail_message = "허브 관리자가 받는 은행과 확인한 결과 입금되지 않았습니다"
    _set_status(db, transfer, TransferStatus(data.status), f"관리자 정리: {data.reason}"[:300])
    logger.warning("거래 %s 관리자 정리 → %s (%s)", transfer.transfer_id, transfer.status, data.reason,
                   extra={"request_id": transfer.request_id, "transfer_id": transfer.transfer_id})
    await db.commit()
    return transfer

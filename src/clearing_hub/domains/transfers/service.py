"""이체 중계 상태 머신 (SPEC 3.3·3.4, PROJECT_PLAN 3.2).

ACCEPTED → DELIVERING → COMPLETED / FAILED / PENDING → (재시도) → COMPLETED / FAILED / UNKNOWN
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.bank_client import BankClient, BankReply, bank_client
from clearing_hub.core.config import get_settings
from clearing_hub.core.request_id import get_request_id
from clearing_hub.core.exceptions import BadRequest, Forbidden, NotFound
from clearing_hub.domains.accounts import service as account_service
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.transfers.models import Transfer, TransferEvent, TransferStatus
from clearing_hub.domains.transfers.schemas import TransferRequest

logger = logging.getLogger(__name__)
FINAL = {TransferStatus.COMPLETED, TransferStatus.FAILED}


def _now() -> datetime:
    return datetime.now(UTC)


def _set_status(db: AsyncSession, transfer: Transfer, status: TransferStatus, detail: str | None = None) -> None:
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
        transfer.fail_code = reply.error
        transfer.fail_message = str(reply.body.get("message") or "받는 은행이 거절했습니다")[:200]
        _set_status(db, transfer, TransferStatus.FAILED, f"{transfer.fail_code}: {transfer.fail_message}")
    else:
        _set_status(db, transfer, no_answer_status, f"응답 없음 (HTTP {reply.status})")


async def accept_and_deliver(
    db: AsyncSession, sender: Bank, data: TransferRequest, client: BankClient = bank_client
) -> tuple[Transfer, bool]:
    """이체 접수 → 받는 은행 호출. (transfer, 이미 있던 거래인지)를 돌려준다.

    같은 transfer_id가 다시 오면 처리하지 않고 저장된 결과를 돌려준다 (멱등, SPEC 3.3).
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

    # 1) 접수 저장을 먼저 커밋한다. 이후 은행 호출 중에 허브가 죽어도 거래 기록은 남는다
    transfer = Transfer(**data.model_dump(exclude={"to_account", "to_account_ref"}), to_account=to_account,
                        from_bank=sender.bank_code, status=TransferStatus.ACCEPTED,
                        request_id=get_request_id())  # 재시도에도 같은 추적 ID를 보낸다 (SPEC 2.4)
    db.add(transfer)
    db.add(TransferEvent(transfer_id=transfer.transfer_id, from_status=None, to_status=TransferStatus.ACCEPTED, detail="접수"))
    _set_status(db, transfer, TransferStatus.DELIVERING, f"{receiver.name}에 전달")
    transfer.delivered_at = _now()
    await db.commit()

    # 2) 받는 은행 호출
    reply = await client.receive(receiver, _receive_payload(transfer, sender), transfer.request_id)
    _apply_reply(db, transfer, reply, no_answer_status=TransferStatus.PENDING)
    await db.commit()
    return transfer, False


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


async def confirm_pending(db: AsyncSession, client: BankClient = bank_client) -> int:
    """미확인 거래(PENDING, 또는 호출 중 허브가 죽어 DELIVERING으로 남은 것)를 받는 은행에 확인한다.
    재시도 태스크와 관리 화면의 '다시 확인'이 부른다. 확정(COMPLETED/FAILED)된 건수를 돌려준다."""
    settings = get_settings()
    stmt = select(Transfer).where(Transfer.status.in_([TransferStatus.PENDING, TransferStatus.DELIVERING]))
    confirmed = 0
    for transfer in (await db.scalars(stmt)).all():
        receiver = await db.get(Bank, transfer.to_bank)
        sender = await db.get(Bank, transfer.from_bank)
        if receiver is None or sender is None:
            continue
        status = await client.receive_status(receiver, transfer.transfer_id, transfer.request_id)
        if status.ok and status.body.get("status") == "COMPLETED":
            _apply_reply(db, transfer, status, no_answer_status=TransferStatus.PENDING)
        elif status.status == 404:
            # 은행이 그 ID로 입금한 적 없음 → /receive를 다시 보낸다 (은행 쪽 멱등 처리에 기댄다)
            reply = await client.receive(receiver, _receive_payload(transfer, sender), transfer.request_id)
            _apply_reply(db, transfer, reply, no_answer_status=TransferStatus.PENDING)
        else:
            _set_status(db, transfer, TransferStatus.PENDING, f"확인 실패 (HTTP {status.status})")
        logger.info("거래 재확인 %s → %s", transfer.transfer_id, transfer.status,
                    extra={"request_id": transfer.request_id, "transfer_id": transfer.transfer_id, "bank_code": transfer.to_bank})
        if transfer.status == TransferStatus.COMPLETED and not transfer.holder_name:
            await _fill_holder_name(transfer, receiver, client)
        if transfer.status in FINAL:
            confirmed += 1
        else:
            transfer.retry_count += 1
            if transfer.retry_count >= settings.retry_max_count:
                _set_status(db, transfer, TransferStatus.UNKNOWN, f"{transfer.retry_count}회 확인 실패, 관리자 확인 필요")
                logger.error("거래 %s 결과를 알 수 없음 (%s → %s)", transfer.transfer_id, transfer.from_bank, transfer.to_bank,
                             extra={"request_id": transfer.request_id, "transfer_id": transfer.transfer_id})
    await db.commit()
    return confirmed

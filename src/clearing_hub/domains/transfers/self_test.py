"""은행 셀프 시험: 받는 쪽 (SETTLEMENT_PLAN 13절, SPEC 3.7). 관리자가 901에서 이체를 보내 주지 않아도 은행이 스스로
자기 4.2·4.3 구현을 시험한다.

- `send`: 허브가 **가짜 은행(`SIM_BANK_CODE`, 기본 902) 명의**로 요청한 은행에 이체를 만든다. 보통 이체와 같은
  `accept_and_deliver`를 쓰므로 상태 머신·재시도·증거·정산이 모두 같다. `transfer_id`는 허브가 만든다
- `resend`: 같은 `transfer_id`의 `/receive`를 **한 번 더** 보낸다 (4.2 규칙 1 멱등 시험). 허브 상태는 건드리지 않고
  `bank_client.receive`만 다시 불러 결과를 돌려준다. 오간 원문은 증거로 남긴다
- `status`: 4.3 `GET /receive/{id}`를 불러 결과를 그대로 보여 준다

셀프 시험 거래는 새 칸 없이 가린다: 보낸 은행이 `SIM_BANK_CODE`이고 `from_account`가 `SELF_TEST_ACCOUNT`인 거래다
(가짜 은행은 스스로 보내지 않으므로 902가 보낸 거래는 셀프 시험뿐이다).

**정산**: 셀프 시험 거래도 보통 거래처럼 정산에 들어간다 (902가 그 은행에 보낸 돈). 예치금이 모의라 문제없다.
902는 받는 시험(다른 은행 → 902)에서 돈을 받고 셀프 시험에서 돈을 내므로, 902의 순채무가 한도를 넘으면 셀프 시험이
`SIM_BANK_UNAVAILABLE`(503)로 막힌다. 그때는 관리자가 902에 예치금을 넣는다 (알림 `sim_bank`).
"""

import logging
import uuid
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core import alerts
from clearing_hub.core.bank_client import BankClient, BankReply, bank_client, observed
from clearing_hub.core.config import get_settings
from clearing_hub.core.exceptions import ApiException, Forbidden, NotFound
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.transfers import evidence, service
from clearing_hub.domains.transfers.models import Transfer, TransferStatus
from clearing_hub.domains.transfers.schemas import TransferRequest

logger = logging.getLogger(__name__)

SELF_TEST_ACCOUNT = "902-TEST-0001"
SELF_TEST_SENDER = "ClearingHub 시험"
SELF_TEST_MEMO = "셀프 시험"
MAX_AMOUNT = 100_000


def _now() -> datetime:
    return datetime.now(UTC)


def is_self_test(transfer: Transfer, bank_code: str) -> bool:
    """그 은행으로 간 셀프 시험 거래인가."""
    return (transfer.from_bank == get_settings().sim_bank_code and transfer.to_bank == bank_code
            and transfer.from_account == SELF_TEST_ACCOUNT)


def _unavailable(message: str) -> ApiException:
    return ApiException(503, "SIM_BANK_UNAVAILABLE", message)


async def _sim_bank(db: AsyncSession) -> Bank:
    code = get_settings().sim_bank_code
    sim = await db.get(Bank, code)
    if sim is None or not sim.active or not sim.base_url:
        logger.error("셀프 시험 불가: 가짜 은행 %s가 없거나 비활성입니다", code)
        alerts.notify("sim_bank", f"셀프 시험을 할 수 없습니다: 가짜 은행 {code}가 등록되지 않았거나 비활성입니다", bank_code=code)
        raise _unavailable(f"시험 이체를 보낼 가짜 은행({code})을 지금 쓸 수 없습니다. 허브 관리자에게 알려 주세요")
    return sim


async def _check_daily_limit(db: AsyncSession, bank_code: str) -> None:
    """오늘(settlement_timezone) 이 은행으로 만든 셀프 시험이 SELF_TEST_DAILY_LIMIT 이상이면 429 TOO_MANY_TESTS.
    동시 요청 몇 건이 한도를 조금 넘을 수는 있다 (잠그지 않는다. 시험 금액이 작아 문제가 되지 않는다)."""
    settings = get_settings()
    tz = ZoneInfo(settings.settlement_timezone)
    midnight = datetime.combine(_now().astimezone(tz).date(), time(0), tzinfo=tz).astimezone(UTC)
    count = await db.scalar(select(func.count()).select_from(Transfer).where(
        Transfer.from_bank == settings.sim_bank_code, Transfer.to_bank == bank_code,
        Transfer.from_account == SELF_TEST_ACCOUNT, Transfer.requested_at >= midnight))
    if count >= settings.self_test_daily_limit:
        raise ApiException(429, "TOO_MANY_TESTS",
                           f"셀프 시험은 하루 {settings.self_test_daily_limit}번까지입니다 (내일 00:00 KST에 다시 셀 수 있습니다)")


class _Capturing:
    """accept_and_deliver가 부른 /receive의 응답을 붙잡아 둔다 (은행이 자기 응답을 바로 보게)."""

    def __init__(self, inner: BankClient):
        self.inner = inner
        self.reply: BankReply | None = None

    async def receive(self, bank: Bank, payload: dict, request_id: str | None = None) -> BankReply:
        self.reply = await self.inner.receive(bank, payload, request_id)
        return self.reply

    def __getattr__(self, name):  # receive 말고는 그대로 넘긴다
        return getattr(self.inner, name)


def reply_view(reply: BankReply | None) -> dict:
    """은행이 실제로 돌려준 것: HTTP 상태·본문·응답 서명 검증 결과 (ping 결과와 같은 읽는 법, SPEC 3.6).
    응답 서명이 틀려 허브가 '응답 없음'으로 처리한 경우도 받은 상태·본문을 그대로 보여 준다."""
    if reply is None:
        return {"reachable": False, "status": None, "body": {}, "signature_valid": None}
    status, body = observed(reply)
    return {"reachable": status is not None, "status": status, "body": body, "signature_valid": reply.signature_valid}


def _alert_if_broken(bank_code: str, transfer_id: str, what: str, reply: BankReply) -> None:
    """은행이 5xx를 주거나 응답 서명이 틀리면 관리자에게 알린다 (그 은행으로 가는 실제 이체도 PENDING이 될 상태다)."""
    status, _ = observed(reply)
    if reply.signature_valid is False:
        problem = f"응답 서명 검증 실패 (HTTP {status})"
    elif status is not None and status >= 500:
        problem = f"HTTP {status}"
    else:
        return
    alerts.notify("self_test", f"셀프 시험: 은행 {bank_code}의 {what}가 {problem}로 답했습니다. "
                  "그 은행으로 가는 이체가 PENDING이 될 수 있습니다", bank_code=bank_code, transfer_id=transfer_id)


async def send(db: AsyncSession, bank: Bank, to_account: str, amount: int,
               client: BankClient = bank_client) -> tuple[Transfer, BankReply | None]:
    """가짜 은행 명의로 `bank`에 시험 이체를 만들고 `/receive`를 부른다. (거래, 은행 응답)을 돌려준다.

    가짜 은행이 없거나 비활성이면 503 SIM_BANK_UNAVAILABLE, 오늘 한도를 넘으면 429 TOO_MANY_TESTS.
    가짜 은행의 순채무한도가 모자라 접수 전에 거절되면(LIMIT_EXCEEDED) 은행 잘못이 아니므로 503으로 바꾼다."""
    sim = await _sim_bank(db)
    sim_code = sim.bank_code  # 아래 롤백 뒤에는 ORM 객체를 다시 읽을 수 없다 (비동기 세션)
    await _check_daily_limit(db, bank.bank_code)
    data = TransferRequest(transfer_id=str(uuid.uuid4()), to_bank=bank.bank_code, to_account=to_account,
                           from_account=SELF_TEST_ACCOUNT, amount=amount, sender_name=SELF_TEST_SENDER, memo=SELF_TEST_MEMO)
    capturing = _Capturing(client)
    try:
        transfer, _ = await service.accept_and_deliver(db, sim, data, client=capturing)
    except ApiException as exc:
        if exc.code != "LIMIT_EXCEEDED":
            raise
        await db.rollback()  # 순채무한도 검사의 예치금 행 잠금을 푼다
        logger.error("셀프 시험 불가: 가짜 은행 %s 순채무한도 초과 (%s)", sim_code, exc.message)
        alerts.notify("sim_bank", f"셀프 시험을 할 수 없습니다: 가짜 은행 {sim_code}의 순채무한도가 모자랍니다. "
                      f"{sim_code}에 예치금을 넣어 주세요 ({exc.message})", bank_code=sim_code)
        raise _unavailable(f"가짜 은행({sim_code})의 한도가 모자라 시험 이체를 보낼 수 없습니다. 허브 관리자에게 알려 주세요") from None
    logger.info("셀프 시험 이체 %s → 은행 %s: %s", transfer.transfer_id, bank.bank_code, transfer.status,
                extra={"transfer_id": transfer.transfer_id, "bank_code": bank.bank_code})
    if capturing.reply is not None:
        _alert_if_broken(bank.bank_code, transfer.transfer_id, "/receive", capturing.reply)
    return transfer, capturing.reply


async def _my_test_transfer(db: AsyncSession, bank: Bank, transfer_id: str) -> Transfer:
    """없으면 404 TRANSFER_NOT_FOUND, 내 은행으로 간 셀프 시험 거래가 아니면 403 FORBIDDEN."""
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise NotFound("TRANSFER_NOT_FOUND", "거래를 찾을 수 없습니다", transfer_id)
    if not is_self_test(transfer, bank.bank_code):
        raise Forbidden("내 은행으로 보낸 셀프 시험 거래가 아닙니다")
    return transfer


async def resend(db: AsyncSession, bank: Bank, transfer_id: str,
                 client: BankClient = bank_client) -> tuple[Transfer, BankReply]:
    """같은 transfer_id의 /receive를 한 번 더 보낸다 (허브 상태는 그대로). COMPLETED 거래만:
    은행이 거절한(FAILED) 거래를 다시 보내면 은행이 이번에는 입금할 수 있는데, 허브에는 실패로 남아 장부가 어긋난다.
    아니면 409 TRANSFER_NOT_COMPLETED. 처음 보낸 것과 같은 본문·같은 X-Request-ID로 보내고, 서명은 새로 만든다."""
    transfer = await _my_test_transfer(db, bank, transfer_id)
    if transfer.status != TransferStatus.COMPLETED:
        raise ApiException(409, "TRANSFER_NOT_COMPLETED",
                           f"COMPLETED인 셀프 시험 거래만 다시 보낼 수 있습니다 (지금 {transfer.status})", transfer_id)
    sim = await _sim_bank(db)
    reply = await client.receive(bank, service._receive_payload(transfer, sim), transfer.request_id)
    evidence.record_exchange(db, transfer.transfer_id, reply)
    await db.commit()
    logger.info("셀프 시험 재전송 %s → 은행 %s: HTTP %s", transfer_id, bank.bank_code, observed(reply)[0],
                extra={"transfer_id": transfer_id, "bank_code": bank.bank_code})
    _alert_if_broken(bank.bank_code, transfer_id, "/receive 재전송", reply)
    return transfer, reply


async def status(db: AsyncSession, bank: Bank, transfer_id: str,
                 client: BankClient = bank_client) -> tuple[Transfer, BankReply]:
    """4.3 GET /receive/{transfer_id}를 불러 결과를 돌려준다 (허브 상태는 그대로). 오간 원문은 증거로 남긴다."""
    transfer = await _my_test_transfer(db, bank, transfer_id)
    reply = await client.receive_status(bank, transfer_id, transfer.request_id)
    evidence.record_exchange(db, transfer.transfer_id, reply)
    await db.commit()
    _alert_if_broken(bank.bank_code, transfer_id, "/receive/{transfer_id}", reply)
    return transfer, reply

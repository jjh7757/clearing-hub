from datetime import UTC, date, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field


def as_utc(value: datetime | None) -> datetime | None:
    """시간대가 빠진 시각(SQLite가 돌려주는 값)은 UTC로 본다. 허브는 시각을 UTC로 저장한다."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


UtcDatetime = Annotated[datetime, AfterValidator(as_utc)]


class SettlementRunCreate(BaseModel):
    """POST /admin/settlements/run 본문. 본문을 생략해도 된다 (SETTLEMENT_PLAN 6.2)."""

    settle_date: date | None = Field(default=None, description="정산일 YYYY-MM-DD. 생략하면 어제, 오늘이면 지금 즉시 마감")


class SettlementConfirm(BaseModel):
    """POST /admin/settlements/{run_id}/confirm 본문. 본문을 생략해도 된다 (SETTLEMENT_PLAN 6.2)."""

    reason: str = Field(default="", max_length=200, description="확정 사유 (로그에 남긴다)")


class SettlementRunOut(BaseModel):
    run_id: int
    settle_date: date
    cutoff_at: UtcDatetime = Field(description="이 시각 전에 COMPLETED 된 미정산 거래까지 모았다")
    status: str
    tx_count: int
    total_amount: int
    created_by: str
    created_at: UtcDatetime
    settled_at: UtcDatetime | None
    settled_by: str | None


class PositionOut(BaseModel):
    bank_code: str
    sent_count: int
    sent_total: int = Field(description="보낸 금액 합 (줄 돈)")
    recv_count: int
    recv_total: int = Field(description="받은 금액 합 (받을 돈)")
    net_amount: int = Field(description="recv_total − sent_total. 양수면 받고 음수면 낸다")
    result: str | None = Field(description="예치금 반영 결과 (확정 뒤): OK / OVERDRAWN / OVERDUE")
    balance_after: int | None = Field(description="반영 뒤 예치금 잔액 (확정 뒤)")


class PairOut(BaseModel):
    bank_a: str
    bank_b: str
    a_to_b_count: int
    a_to_b_total: int
    b_to_a_count: int
    b_to_a_total: int
    net_amount: int = Field(description="두 방향 합계 차이의 절댓값")
    net_direction: str = Field(description="a_to_b / b_to_a / none")


class AckAdminOut(BaseModel):
    """관리자 화면의 은행 확인 결과 한 줄 (SETTLEMENT_PLAN 13절)."""

    bank_code: str
    result: str = Field(description="ACK / DISPUTE")
    transfer_ids: list[str] = Field(description="DISPUTE: 은행이 차이 난다고 한 transfer_id")
    message: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime = Field(description="마지막으로 받은 시각 (다시 보내면 덮어쓴다)")


class SettlementDetail(SettlementRunOut):
    positions: list[PositionOut] = Field(description="은행별 순 포지션, 은행 코드순. 합은 0")
    pairs: list[PairOut] = Field(description="은행 쌍별 표 (화면·CSV용)")
    acks: list[AckAdminOut] = Field(default=[], description="은행이 보낸 ack/dispute, 은행 코드순")
    disputed: bool = Field(default=False, description="DISPUTE가 하나라도 있으면 true. 자동 확정하지 않는다")


class SettlementTransferOut(BaseModel):
    transfer_id: str
    from_bank: str
    to_bank: str
    amount: int
    finished_at: UtcDatetime
    holder_name: str | None


# --- 은행용 대사 API (SETTLEMENT_PLAN 6.1, SPEC 10.4·10.5). 요청한 은행 자신의 몫만 담는다 -------------------------

class BankSettlementOut(BaseModel):
    """GET /v1/settlements 항목. net_amount·result는 요청한 은행의 것이다."""

    run_id: int
    settle_date: date
    cutoff_at: UtcDatetime = Field(description="이 시각 전에 COMPLETED 된 미정산 거래까지 모았다")
    status: str = Field(description="DRAFT / SETTLED (목록에는 CANCELLED가 나오지 않는다)")
    settled_at: UtcDatetime | None = Field(description="확정 시각. DRAFT면 null")
    net_amount: int = Field(description="내 포지션: 받을 돈 − 줄 돈. 그 run에 내 거래가 없으면 0")
    result: str | None = Field(description="내 예치금 반영 결과 OK / OVERDRAWN / OVERDUE. 확정 전이거나 내 거래가 없으면 null")
    my_ack: Literal["ACK", "DISPUTE"] | None = Field(description="내가 보낸 대사 결과 (SPEC 10.7). 안 보냈으면 null")


class BankSettlementList(BaseModel):
    settlements: list[BankSettlementOut] = Field(description="최근 30일, 최신순(run_id 내림차순), 최대 100개")


class BankPositionOut(BaseModel):
    """대사 파일의 내 포지션. 그 run에 내 거래가 없으면 숫자는 0, result·balance_after는 null."""

    sent_count: int = 0
    sent_total: int = Field(default=0, description="내가 보낸 금액 합 (줄 돈)")
    recv_count: int = 0
    recv_total: int = Field(default=0, description="내가 받은 금액 합 (받을 돈)")
    net_amount: int = Field(default=0, description="recv_total − sent_total. 양수면 내 예치금이 는다")
    result: str | None = Field(default=None, description="OK / OVERDRAWN / OVERDUE (확정 뒤)")
    balance_after: int | None = Field(default=None, description="반영 뒤 내 예치금 잔액 (확정 뒤)")


class ReconTransferOut(BaseModel):
    """대사 파일의 거래 한 줄. 계좌번호·이름은 넣지 않는다 (은행 자기 DB에 있다)."""

    transfer_id: str
    direction: Literal["SENT", "RECEIVED"] = Field(description="SENT: 내가 보냄, RECEIVED: 내가 받음")
    counterparty_bank: str = Field(description="상대 은행 코드")
    amount: int
    finished_at: UtcDatetime


class BankSettlementDetail(BaseModel):
    """GET /v1/settlements/{run_id}: 대사 파일."""

    run_id: int
    settle_date: date
    cutoff_at: UtcDatetime
    status: str = Field(description="DRAFT / SETTLED / CANCELLED. CANCELLED면 무시한다 (거래는 다음 run에 다시 들어간다)")
    settled_at: UtcDatetime | None
    position: BankPositionOut
    transfers: list[ReconTransferOut] = Field(description="그 run에서 내가 보내거나 받은 거래, 끝난 시각 순")
    my_ack: Literal["ACK", "DISPUTE"] | None = Field(description="내가 보낸 대사 결과 (SPEC 10.7). 안 보냈으면 null")


class DisputeIn(BaseModel):
    """POST /v1/settlements/{run_id}/dispute 본문 (SPEC 10.7)."""

    transfer_ids: list[Annotated[str, Field(min_length=1, max_length=36)]] = Field(
        max_length=1000, description="차이 나는 transfer_id (허브에만 있는 것, 금액·방향이 다른 것). 비어도 된다")
    message: str | None = Field(default=None, max_length=200, description="사람이 읽는 설명 (선택)")


class AckOut(BaseModel):
    """ack·dispute 응답: 그 run의 지금 상태와 내 확인 결과. ack로 전원 ack가 되면 status가 바로 SETTLED다."""

    run_id: int
    status: str = Field(description="DRAFT / SETTLED")
    settled_at: UtcDatetime | None
    my_ack: Literal["ACK", "DISPUTE"]

"""정산 배치 DRAFT: 수집·상계 (SETTLEMENT_PLAN 2절 2·3·4·8번, 4.4, 5.1의 8·9단계, 6.2, 7절 3단계, 8절 S1~S4·S9).

이체는 허브 API로 만든다 (`banks` 픽스처는 한도가 넉넉하다). 예치금 확정(confirm)은 4단계라, 확정이 필요한 시험은
`_settle()`이 4단계 confirm이 할 일(지시서 반영 + SETTLED)을 직접 한다.
"""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

import bank_sim.main as sim

from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.reserve import service as reserve
from clearing_hub.domains.settlement import service as settlement
from clearing_hub.domains.settlement.models import SettlementRun, SettlementStatus
from clearing_hub.domains.transfers.exposure import open_exposure
from clearing_hub.domains.transfers.models import Transfer, TransferStatus
from clearing_hub.domains.transfers.service import _set_status, confirm_pending
from tests.conftest import ADMIN, send_902_to_901, transfer_payload

pytestmark = pytest.mark.anyio
KST = ZoneInfo("Asia/Seoul")


def _today() -> date:
    return datetime.now(KST).date()


async def _run(client, settle_date: date | str | None = None, *, expect: int = 201) -> dict:
    body = {} if settle_date is None else {"settle_date": str(settle_date)}
    res = await client.post("/admin/settlements/run", json=body, headers=ADMIN)
    assert res.status_code == expect, res.text
    return res.json()


async def _send(sender, account: str, amount: int = 50_000) -> dict:
    payload = transfer_payload(f"110-123-{account}", amount=amount)
    await sender.post("/v1/transfers", payload)
    return payload


async def _transfer(transfer_id: str) -> Transfer:
    async with SessionLocal() as db:
        return await db.get(Transfer, transfer_id)


async def _settle(run_id: int) -> None:
    """4단계 confirm 대신: 지시서를 예치금에 반영하고 run을 SETTLED로 (한 트랜잭션)."""
    async with SessionLocal() as db, db.begin():
        run = await settlement.get_run(db, run_id)
        await reserve.apply_instruction(db, run_id, settlement.instruction_for(run))
        run.status = SettlementStatus.SETTLED
        run.settled_at, run.settled_by = datetime.now(UTC), "test"


async def _s1_transfers(banks, monkeypatch) -> list[str]:
    """S1의 하루치: 901→902 5만 2건, 902→901 3만 1건."""
    sender, receiver = banks
    ids = [(await _send(sender, "0001"))["transfer_id"] for _ in range(2)]
    ids.append((await send_902_to_901(sender, receiver, monkeypatch, 30_000))["transfer_id"])
    return ids


# --- 마감 시각 -------------------------------------------------------------------------------------------------------

def test_compute_cutoff_rules():
    """생략 → 어제·다음 날 00:00(KST), 오늘 → 지금, 과거 → 그 다음 날 00:00, 내일 → 422. 마감은 UTC로 돌려준다."""
    now = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)  # KST 11-06 12:00
    assert settlement.compute_cutoff(None, now) == (date(2026, 11, 5), datetime(2026, 11, 5, 15, 0, tzinfo=UTC))
    assert settlement.compute_cutoff(date(2026, 11, 6), now) == (date(2026, 11, 6), now)
    assert settlement.compute_cutoff(date(2026, 11, 1), now) == (date(2026, 11, 1), datetime(2026, 11, 1, 15, tzinfo=UTC))
    with pytest.raises(Exception) as exc:
        settlement.compute_cutoff(date(2026, 11, 7), now)
    assert exc.value.status_code == 422 and exc.value.code == "VALIDATION_ERROR"

    # 날짜 경계는 KST: UTC 14:59는 KST 23:59(11-06), UTC 15:00은 KST 11-07 00:00
    assert settlement.compute_cutoff(None, datetime(2026, 11, 6, 14, 59, tzinfo=UTC))[0] == date(2026, 11, 5)
    late = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
    assert settlement.compute_cutoff(None, late) == (date(2026, 11, 6), late)  # 어제 = 11-06, 마감 = 지금
    assert settlement.compute_cutoff(date(2026, 11, 7), late)[1] == late  # 이제 11-07은 오늘


# --- S1 상계 ---------------------------------------------------------------------------------------------------------

async def test_s1_multilateral_net(client, banks, monkeypatch):
    """S1: 901→902 5만×2, 902→901 3만 → 오늘 즉시 마감. 901 −7만, 902 +7만, 쌍 표 1행, Σ=0, 거래 3건에 run_id."""
    ids = await _s1_transfers(banks, monkeypatch)
    before = datetime.now(UTC)
    body = await _run(client, _today())

    assert body["status"] == "DRAFT" and body["settle_date"] == str(_today())
    assert (body["tx_count"], body["total_amount"]) == (3, 130_000)
    assert datetime.fromisoformat(body["cutoff_at"]) >= before  # 오늘이면 마감 = 지금
    assert body["created_by"] == "admin" and body["settled_at"] is None
    assert body["positions"] == [
        {"bank_code": "901", "sent_count": 2, "sent_total": 100_000, "recv_count": 1, "recv_total": 30_000,
         "net_amount": -70_000, "result": None, "balance_after": None},
        {"bank_code": "902", "sent_count": 1, "sent_total": 30_000, "recv_count": 2, "recv_total": 100_000,
         "net_amount": 70_000, "result": None, "balance_after": None},
    ]
    assert sum(p["net_amount"] for p in body["positions"]) == 0
    assert body["pairs"] == [{"bank_a": "901", "bank_b": "902", "a_to_b_count": 2, "a_to_b_total": 100_000,
                              "b_to_a_count": 1, "b_to_a_total": 30_000, "net_amount": 70_000,
                              "net_direction": "a_to_b"}]
    for tid in ids:
        assert (await _transfer(tid)).settlement_run_id == body["run_id"]

    res = await client.get(f"/admin/settlements/{body['run_id']}", headers=ADMIN)
    assert res.status_code == 200 and res.json() == body
    listed = (await client.get("/admin/settlements", headers=ADMIN)).json()
    assert [r["run_id"] for r in listed] == [body["run_id"]] and "positions" not in listed[0]


async def test_instruction_for(client, banks, monkeypatch):
    """포지션 → 정산 지시서, 은행 코드순 (4단계 confirm이 reserve.apply_instruction에 넘긴다)."""
    await _s1_transfers(banks, monkeypatch)
    async with SessionLocal() as db, db.begin():
        run = await settlement.create_draft(db, _today())
        assert settlement.instruction_for(run) == [("901", -70_000), ("902", 70_000)]
    async with SessionLocal() as db:
        assert settlement.instruction_for(await settlement.get_run(db, run.run_id)) == [("901", -70_000), ("902", 70_000)]


async def test_settlement_transfers_json_and_csv(client, banks, monkeypatch):
    """거래 목록 JSON과 CSV (헤더 행 + 3행, UTF-8, BOM 없음, 첨부 파일 이름)."""
    ids = await _s1_transfers(banks, monkeypatch)
    run_id = (await _run(client, _today()))["run_id"]

    rows = (await client.get(f"/admin/settlements/{run_id}/transfers", headers=ADMIN)).json()
    assert [r["transfer_id"] for r in rows] == ids  # 끝난 시각 순
    assert set(rows[0]) == {"transfer_id", "from_bank", "to_bank", "amount", "finished_at", "holder_name"}
    assert [(r["from_bank"], r["to_bank"], r["amount"]) for r in rows] == [
        ("901", "902", 50_000), ("901", "902", 50_000), ("902", "901", 30_000)]
    assert rows[0]["holder_name"] and datetime.fromisoformat(rows[0]["finished_at"]).tzinfo is not None

    res = await client.get(f"/admin/settlements/{run_id}/transfers.csv", headers=ADMIN)
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/csv; charset=utf-8"
    assert res.headers["content-disposition"] == f"attachment; filename=settlement_{run_id}.csv"
    assert not res.content.startswith(b"\xef\xbb\xbf")
    lines = list(csv.reader(io.StringIO(res.content.decode("utf-8"))))
    assert lines[0] == ["transfer_id", "from_bank", "to_bank", "amount", "finished_at", "holder_name"]
    assert len(lines) == 4 and [line[0] for line in lines[1:]] == ids
    assert lines[1][3] == "50000" and lines[1][5] == rows[0]["holder_name"]

    for path in ("", "/transfers", "/transfers.csv"):
        res = await client.get(f"/admin/settlements/999{path}", headers=ADMIN)
        assert res.status_code == 404 and res.json()["error"] == "SETTLEMENT_NOT_FOUND"
    assert (await client.get("/admin/settlements")).status_code == 401  # 관리자 토큰 필요


# --- S2·S3 대상 선정 -------------------------------------------------------------------------------------------------

async def test_s2_only_completed_is_collected(client, banks):
    """S2: FAILED(0002)·PENDING(0003)·UNKNOWN은 빠지고 COMPLETED만 들어간다."""
    sender, _ = banks
    completed = await _send(sender, "0001", 10_000)
    failed = await _send(sender, "0002")
    pending = await _send(sender, "0003")
    unknown = await _send(sender, "0003")
    async with SessionLocal() as db:
        await db.execute(update(Transfer).where(Transfer.transfer_id == unknown["transfer_id"])
                         .values(status=TransferStatus.UNKNOWN))
        await db.commit()
    expected = {completed["transfer_id"]: "COMPLETED", failed["transfer_id"]: "FAILED",
                pending["transfer_id"]: "PENDING", unknown["transfer_id"]: "UNKNOWN"}
    for tid, status in expected.items():
        assert (await _transfer(tid)).status == status

    body = await _run(client, _today())
    assert (body["tx_count"], body["total_amount"]) == (1, 10_000)
    for tid in expected:
        assert (await _transfer(tid)).settlement_run_id == (body["run_id"] if tid == completed["transfer_id"] else None)


async def test_s3_late_completed_goes_to_next_run(client, banks, monkeypatch):
    """S3: run 뒤에 COMPLETED 된 거래는 그 run에 없고 다음 run에 들어간다 (미정산 기준, 2절 3번)."""
    sender, _ = banks
    early = await _send(sender, "0001", 10_000)
    late = await _send(sender, "0003", 20_000)  # 응답 없음 → PENDING
    first = await _run(client, _today())
    assert first["tx_count"] == 1
    await _settle(first["run_id"])

    sim.received.clear()  # 지연 중이던 첫 요청은 버리고 재전달만 본다 (test_transfers의 0003 시험과 같은 방식)
    monkeypatch.setenv("SIM_DELAY_SECONDS", "0")  # 재시도 때는 은행이 정상 응답
    async with SessionLocal() as db:
        assert await confirm_pending(db) == 1
    assert (await _transfer(late["transfer_id"])).status == TransferStatus.COMPLETED

    second = await _run(client, _today())
    assert (second["tx_count"], second["total_amount"]) == (1, 20_000)
    assert (await _transfer(early["transfer_id"])).settlement_run_id == first["run_id"]
    assert (await _transfer(late["transfer_id"])).settlement_run_id == second["run_id"]


async def test_s3_yesterday_run_cuts_off_at_midnight(client, banks):
    """본문을 생략하면 어제 정산: 마감 = 오늘 00:00(KST). 오늘 끝난 거래는 빠지고 다음 run으로 간다."""
    sender, _ = banks
    yesterday_tx = await _send(sender, "0001", 10_000)
    today_tx = await _send(sender, "0001", 20_000)
    midnight = datetime.combine(_today(), datetime.min.time(), tzinfo=KST)
    async with SessionLocal() as db:  # 첫 거래는 어제 23:00(KST)에 끝난 것으로
        await db.execute(update(Transfer).where(Transfer.transfer_id == yesterday_tx["transfer_id"])
                         .values(finished_at=(midnight - timedelta(hours=1)).astimezone(UTC)))
        await db.commit()

    body = await _run(client)
    assert body["settle_date"] == str(_today() - timedelta(days=1))
    assert datetime.fromisoformat(body["cutoff_at"]) == midnight
    assert (body["tx_count"], body["total_amount"]) == (1, 10_000)
    assert (await _transfer(today_tx["transfer_id"])).settlement_run_id is None


# --- S4 DRAFT 하나·취소·날짜 검사 ------------------------------------------------------------------------------------

async def test_s4_single_draft_and_cancel(client, banks, monkeypatch):
    """S4: DRAFT가 있으면 409(run_id). 취소하면 거래가 미정산으로 돌아가 다음 run에 다시 들어간다."""
    ids = await _s1_transfers(banks, monkeypatch)
    first = await _run(client, _today())

    res = await client.post("/admin/settlements/run", json={}, headers=ADMIN)
    assert res.status_code == 409
    assert res.json()["error"] == "SETTLEMENT_DRAFT_EXISTS" and res.json()["run_id"] == first["run_id"]

    res = await client.post(f"/admin/settlements/{first['run_id']}/cancel", headers=ADMIN)
    assert res.status_code == 200 and res.json()["status"] == "CANCELLED" and res.json()["tx_count"] == 3
    for tid in ids:
        assert (await _transfer(tid)).settlement_run_id is None
    # 취소한 run의 포지션은 기록으로 남는다
    kept = (await client.get(f"/admin/settlements/{first['run_id']}", headers=ADMIN)).json()
    assert kept["status"] == "CANCELLED" and len(kept["positions"]) == 2

    res = await client.post(f"/admin/settlements/{first['run_id']}/cancel", headers=ADMIN)
    assert res.status_code == 409 and res.json()["error"] == "SETTLEMENT_NOT_DRAFT"

    second = await _run(client, _today())
    assert second["run_id"] != first["run_id"] and second["tx_count"] == 3
    assert second["positions"] == first["positions"]
    for tid in ids:
        assert (await _transfer(tid)).settlement_run_id == second["run_id"]

    await _settle(second["run_id"])  # 확정된 run은 취소할 수 없다
    res = await client.post(f"/admin/settlements/{second['run_id']}/cancel", headers=ADMIN)
    assert res.status_code == 409 and res.json()["error"] == "SETTLEMENT_NOT_DRAFT"
    assert (await _transfer(ids[0])).settlement_run_id == second["run_id"]
    res = await client.post("/admin/settlements/999/cancel", headers=ADMIN)
    assert res.status_code == 404 and res.json()["error"] == "SETTLEMENT_NOT_FOUND"


async def test_s4_dates_and_empty_run(client, banks):
    """내일 이후 날짜는 422, 형식 오류도 422. 거래가 없어도 run은 만든다(tx_count 0, 포지션 없음). 한 날짜에 여러 run."""
    for bad in (_today() + timedelta(days=1), "2026-13-01"):
        res = await client.post("/admin/settlements/run", json={"settle_date": str(bad)}, headers=ADMIN)
        assert res.status_code == 422 and res.json()["error"] == "VALIDATION_ERROR"
    assert (await client.get("/admin/settlements", headers=ADMIN)).json() == []

    empty = await _run(client, _today())
    assert (empty["tx_count"], empty["total_amount"], empty["positions"], empty["pairs"]) == (0, 0, [], [])
    await client.post(f"/admin/settlements/{empty['run_id']}/cancel", headers=ADMIN)
    again = await _run(client, _today())  # 같은 날짜 두 번째 run
    assert again["settle_date"] == empty["settle_date"]
    res = await client.post("/admin/settlements/run", headers=ADMIN)  # 본문 없이도 된다 (어제). DRAFT가 있어 409
    assert res.status_code == 409
    listed = (await client.get("/admin/settlements", params={"limit": 1}, headers=ADMIN)).json()
    assert [r["run_id"] for r in listed] == [again["run_id"]]


async def test_s4_concurrent_draft_blocked_by_index(client, banks, monkeypatch):
    """동시 실행으로 DRAFT 확인을 둘 다 지나쳐도 부분 유니크 인덱스가 두 번째 DRAFT를 막는다 (409, 롤백)."""
    first = await _run(client, _today())

    async def no_draft(db):
        return None

    monkeypatch.setattr(settlement, "_current_draft", no_draft)
    res = await client.post("/admin/settlements/run", json={}, headers=ADMIN)
    assert res.status_code == 409 and res.json()["error"] == "SETTLEMENT_DRAFT_EXISTS"
    async with SessionLocal() as db:
        assert [r.run_id for r in (await db.scalars(select(SettlementRun))).all()] == [first["run_id"]]


async def test_net_sum_check_rolls_back(client, banks, monkeypatch):
    """상계 검증(Σ net = 0 등)에 걸리면 RuntimeError, 트랜잭션이 롤백돼 run도 거래 표시도 남지 않는다."""
    ids = await _s1_transfers(banks, monkeypatch)

    def broken(flows):
        positions = original(flows)
        positions[0].net_amount += 1
        return positions

    original = settlement._positions
    monkeypatch.setattr(settlement, "_positions", broken)
    with pytest.raises(RuntimeError, match="포지션 합이 0이 아닙니다"):
        async with SessionLocal() as db, db.begin():
            await settlement.create_draft(db, _today())
    async with SessionLocal() as db:
        assert (await db.scalars(select(SettlementRun))).all() == []
    for tid in ids:
        assert (await _transfer(tid)).settlement_run_id is None


# --- S9 정산된 거래는 바꾸지 않는다 ----------------------------------------------------------------------------------

async def test_s9_settled_transfer_is_immutable(client, banks):
    """S9: settlement_run_id가 있는 거래에 _set_status → RuntimeError. 관리자 정리 API는 UNKNOWN만 받으므로 409."""
    sender, _ = banks
    payload = await _send(sender, "0001")
    await _run(client, _today())

    async with SessionLocal() as db:
        transfer = await db.get(Transfer, payload["transfer_id"])
        with pytest.raises(RuntimeError, match="정산된 거래는 상태를 바꿀 수 없습니다"):
            _set_status(db, transfer, TransferStatus.FAILED, "시험")
        assert transfer.status == TransferStatus.COMPLETED

    res = await client.post(f"/admin/transfers/{payload['transfer_id']}/resolve",
                            json={"status": "FAILED", "reason": "시험"}, headers=ADMIN)
    assert res.status_code == 409 and res.json()["error"] == "TRANSFER_NOT_UNKNOWN"


# --- 순채무 (4.4) ----------------------------------------------------------------------------------------------------

async def _reserve(bank) -> dict:
    res = await bank.get("/v1/reserve")
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.parametrize("net_debit_cap", [0])
async def test_exposure_moves_into_balance_on_settle(client, banks, monkeypatch, net_debit_cap):
    """정산 거래는 확정(SETTLED) 뒤에 순채무에서 빠지고 그 금액이 예치금으로 옮겨진다. 보낼 수 있는 금액은 그대로다.
    DRAFT만으로는 빠지지 않는다: 빠지면 확정 전까지 같은 한도로 한 번 더 보낼 수 있게 된다."""
    sender, receiver = banks
    res = await client.post("/admin/reserve/901/deposit", json={"amount": 100_000}, headers=ADMIN)
    assert res.status_code == 200
    await send_902_to_901(sender, receiver, monkeypatch, 30_000)
    for _ in range(2):
        assert (await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=50_000))).status_code == 200
    assert await _reserve(sender) == {"bank_code": "901", "balance": 100_000, "net_debit_cap": 0, "exposure": 70_000,
                                      "available": 30_000}
    blocked = transfer_payload("110-123-0001", amount=50_000)
    assert (await sender.post("/v1/transfers", blocked)).json()["error"] == "LIMIT_EXCEEDED"

    run = await _run(client, _today())  # DRAFT: 예치금도 순채무도 그대로
    assert (await _reserve(sender))["exposure"] == 70_000 and (await _reserve(sender))["available"] == 30_000
    retry = transfer_payload("110-123-0001", amount=50_000)
    assert (await sender.post("/v1/transfers", retry)).json()["error"] == "LIMIT_EXCEEDED"

    await _settle(run["run_id"])  # 확정: 901 −7만, 902 +7만이 예치금으로
    async with SessionLocal() as db:
        assert await open_exposure(db, "901") == 0 and await open_exposure(db, "902") == 0
    assert await _reserve(sender) == {"bank_code": "901", "balance": 30_000, "net_debit_cap": 0, "exposure": 0,
                                      "available": 30_000}
    mine = await _reserve(receiver)
    assert (mine["balance"], mine["exposure"]) == (70_000, 0)

    assert (await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=30_000))).status_code == 200
    assert await _reserve(sender) == {"bank_code": "901", "balance": 30_000, "net_debit_cap": 0, "exposure": 30_000,
                                      "available": 0}
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=1))
    assert res.json()["error"] == "LIMIT_EXCEEDED"

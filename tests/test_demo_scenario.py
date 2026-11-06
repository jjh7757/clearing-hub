"""시연 시나리오를 끝까지 돈다 (SETTLEMENT_PLAN 9절 1~8, docs/DEMO.md).

허브 API만 쓴다. 각 단계의 assert가 시연 화면에서 보여 줄 숫자다 — DEMO.md의 기대 응답과 같아야 한다.
은행은 예치금 0·한도 0에서 시작한다 (conftest의 넉넉한 기본 한도를 끈다). 그래서 1단계의 예치가 시연의 출발점이다.

AvengersBank 쪽 단계(5의 MATCHED, 7의 장부 정리)는 이 저장소에서 돌릴 수 없다. 같은 하루치(901→902 5만 2건,
902→901 3만 1건, 901 net −7만)를 가짜 허브 응답으로 만들어 `avengers_bank/tests/test_settlement_sync.py`의 B1이 증명한다.
"""

import csv
import io
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import bank_sim.main as sim
from clearing_hub.core.database import SessionLocal
from clearing_hub.core.signing import sign
from clearing_hub.domains.reserve.models import ReserveLedgerEntry
from tests.conftest import ADMIN, register_bank, send_902_to_901, set_net_debit_cap, transfer_payload

pytestmark = pytest.mark.anyio
KST = ZoneInfo("Asia/Seoul")


@pytest.fixture
def net_debit_cap() -> int:
    return 0


def _today() -> date:
    return datetime.now(KST).date()


async def _deposit(client, bank_code: str, amount: int) -> dict:
    res = await client.post(f"/admin/reserve/{bank_code}/deposit", json={"amount": amount, "memo": "시연 예치"},
                            headers=ADMIN)
    assert res.status_code == 200, res.text
    return res.json()


async def _admin_reserve(client) -> dict[str, tuple[int, int, int, int]]:
    """/admin/reserve → {은행: (balance, net_debit_cap, exposure, available)}"""
    res = await client.get("/admin/reserve", headers=ADMIN)
    assert res.status_code == 200, res.text
    return {r["bank_code"]: (r["balance"], r["net_debit_cap"], r["exposure"], r["available"]) for r in res.json()}


async def _my_reserve(bank) -> tuple[int, int, int]:
    """/v1/reserve → (balance, exposure, available)"""
    body = (await bank.get("/v1/reserve")).json()
    return body["balance"], body["exposure"], body["available"]


async def _send(bank, to_account: str, amount: int, to_bank: str = "902"):
    return await bank.post("/v1/transfers", {**transfer_payload(to_account, amount=amount), "to_bank": to_bank})


def _positions(body: dict) -> list[tuple]:
    return [(p["bank_code"], p["sent_count"], p["sent_total"], p["recv_count"], p["recv_total"], p["net_amount"])
            for p in body["positions"]]


async def test_demo_scenario(client, banks, monkeypatch):
    sender, receiver = banks  # 901 AvengersBank, 902 SimBank (가짜 은행)

    # 1. 예치: 901·902에 각 100만. /admin/reserve에 잔액 100만, 한도 0, 순채무 0, 보낼 수 있는 금액 100만
    for code in ("901", "902"):
        posted = await _deposit(client, code, 1_000_000)
        assert posted["entry"]["kind"] == "DEPOSIT" and posted["entry"]["balance_after"] == 1_000_000
    assert await _admin_reserve(client) == {"901": (1_000_000, 0, 0, 1_000_000), "902": (1_000_000, 0, 0, 1_000_000)}

    # 2. 한도 검사: 990(시연용 은행, 예치 0·한도 0)의 송금은 접수 전에 거절된다. 예치 5만 뒤 1만은 된다
    bank_990 = await register_bank(client, "990", "DemoBank")
    assert (await _admin_reserve(client))["990"] == (0, 0, 0, 0)  # 등록 때 예치금 계좌가 0으로 생긴다
    res = await _send(bank_990, "110-123-0001", 10_000)
    assert res.status_code == 400, res.text
    rejected = res.json()
    assert rejected["error"] == "LIMIT_EXCEEDED" and rejected["status"] == "FAILED"  # 보내는 은행은 출금을 되돌린다
    assert rejected["message"].startswith("순채무한도를 넘었습니다") and "예치금 0원" in rejected["message"]
    assert sim.received == {}  # 받는 은행까지 가지 않았다

    await _deposit(client, "990", 50_000)
    res = await _send(bank_990, "110-123-0001", 10_000)
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text
    assert await _my_reserve(bank_990) == (50_000, 10_000, 40_000)

    # 3. 하루치: 901→902 5만 2건 (모아에게 "SimBank 110-123-0001로 5만 원"), 902→901 3만 1건 (관리자가 902 키로)
    for _ in range(2):
        res = await _send(sender, "110-123-0001", 50_000)
        assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text
    await send_902_to_901(sender, receiver, monkeypatch, 30_000)
    await set_net_debit_cap(client, "902", 0)  # 헬퍼가 902에 넉넉히 준 한도를 되돌린다 (시연은 한도 0)
    assert len(sim.received) == 4  # 902가 받은 3건 + 902→901 1건 (테스트에서는 901의 base_url도 bank_sim이다)
    assert await _admin_reserve(client) == {
        "901": (1_000_000, 0, 70_000, 930_000),      # 10만 보내고 3만 받음
        "902": (1_000_000, 0, -80_000, 1_080_000),   # 받은 11만 − 보낸 3만: 받은 돈만큼 더 보낼 수 있다
        "990": (50_000, 0, 10_000, 40_000),
    }

    # 4. 마감 상계: 오늘 날짜로 run → DRAFT. 은행별 순 포지션의 합은 0, 쌍별 표, CSV
    res = await client.post("/admin/settlements/run", json={"settle_date": str(_today())}, headers=ADMIN)
    assert res.status_code == 201, res.text
    run = res.json()
    run_id = run["run_id"]
    assert (run["status"], run["tx_count"], run["total_amount"]) == ("DRAFT", 4, 140_000)
    #                            보낸 건수·합계      받은 건수·합계      net
    assert _positions(run) == [("901", 2, 100_000, 1, 30_000, -70_000),
                               ("902", 1, 30_000, 3, 110_000, 80_000),
                               ("990", 1, 10_000, 0, 0, -10_000)]
    assert sum(p["net_amount"] for p in run["positions"]) == 0
    assert [(p["bank_a"], p["bank_b"], p["a_to_b_total"], p["b_to_a_total"], p["net_amount"], p["net_direction"])
            for p in run["pairs"]] == [("901", "902", 100_000, 30_000, 70_000, "a_to_b"),
                                       ("902", "990", 0, 10_000, 10_000, "b_to_a")]
    res = await client.get(f"/admin/settlements/{run_id}/transfers.csv", headers=ADMIN)
    lines = list(csv.reader(io.StringIO(res.content.decode("utf-8"))))
    assert lines[0] == ["transfer_id", "from_bank", "to_bank", "amount", "finished_at", "holder_name"]
    assert sorted((line[1], line[2], line[3]) for line in lines[1:]) == [
        ("901", "902", "50000"), ("901", "902", "50000"), ("902", "901", "30000"), ("990", "902", "10000")]

    # 5. 대사 (허브 쪽 시각): 901이 받는 대사 파일에는 901의 몫만 있다. SENT 2건·RECEIVED 1건, net −7만.
    #    AvengersBank가 이 파일을 자기 장부와 맞춰 MATCHED를 내는 것은 avengers_bank/tests/test_settlement_sync.py B1
    listed = (await sender.get("/v1/settlements")).json()["settlements"]
    assert [(s["run_id"], s["status"], s["net_amount"], s["result"]) for s in listed] == [(run_id, "DRAFT", -70_000, None)]
    recon = (await sender.get(f"/v1/settlements/{run_id}")).json()
    assert [(t["direction"], t["counterparty_bank"], t["amount"]) for t in recon["transfers"]] == [
        ("SENT", "902", 50_000), ("SENT", "902", 50_000), ("RECEIVED", "902", 30_000)]  # 990 거래는 안 보인다
    assert (recon["position"]["net_amount"], recon["position"]["result"]) == (-70_000, None)
    assert await _my_reserve(sender) == (1_000_000, 70_000, 930_000)  # DRAFT로는 순채무가 그대로다 (4.4)

    # 6. 확정: 예치금이 포지션만큼 옮겨진다. SETTLE 분개 은행마다 1건, 순채무 0. 보낼 수 있는 금액은 확정 전후로 같다
    res = await client.post(f"/admin/settlements/{run_id}/confirm", json={"reason": "시연: 대사 완료"}, headers=ADMIN)
    assert res.status_code == 200, res.text
    settled = res.json()
    assert (settled["status"], settled["settled_by"]) == ("SETTLED", "admin")
    assert [(p["bank_code"], p["result"], p["balance_after"]) for p in settled["positions"]] == [
        ("901", "OK", 930_000), ("902", "OK", 1_080_000), ("990", "OK", 40_000)]
    assert await _admin_reserve(client) == {
        "901": (930_000, 0, 0, 930_000), "902": (1_080_000, 0, 0, 1_080_000), "990": (40_000, 0, 0, 40_000)}
    async with SessionLocal() as db:
        entries = (await db.scalars(select(ReserveLedgerEntry).where(ReserveLedgerEntry.run_id == run_id)
                                    .order_by(ReserveLedgerEntry.bank_code))).all()
    assert [(e.bank_code, e.kind, e.amount, e.balance_after) for e in entries] == [
        ("901", "SETTLE_DEBIT", -70_000, 930_000), ("902", "SETTLE_CREDIT", 80_000, 1_080_000),
        ("990", "SETTLE_DEBIT", -10_000, 40_000)]
    assert await _my_reserve(sender) == (930_000, 0, 930_000)
    detail = (await sender.get(f"/v1/settlements/{run_id}")).json()
    assert (detail["status"], detail["position"]["result"], detail["position"]["balance_after"]) == (
        "SETTLED", "OK", 930_000)

    # 7. 은행 장부 정리: AvengersBank 워커(settlement_sync_loop)가 SETTLED run을 보고 INTERBANK_SETTLEMENT 0,
    #    HUB_RESERVE −7만으로 정리한다 — avengers_bank/tests/test_settlement_sync.py B1·B4 (이 저장소에서는 확인하지 않는다)

    # 8. 속이는 은행: 902의 …0006은 입금하지 않고 200 COMPLETED라고 답한다. 허브는 COMPLETED로 기록한다
    res = await _send(sender, "110-123-0006", 20_000)
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text
    cheat_id = res.json()["transfer_id"]
    assert cheat_id not in sim.received  # 가짜 은행 장부에는 없다
    async with AsyncClient(transport=ASGITransport(app=sim.app), base_url="http://sim") as sim_http:
        path = f"/receive/{cheat_id}"  # 허브가 4.3으로 물어도 "입금한 적 없다"고 답한다
        res = await sim_http.get(f"/interbank{path}", headers={"X-Bank-Code": "000", **sign(receiver.secret, "GET", path, b"")})
    assert res.status_code == 404 and res.json()["error"] == "TRANSFER_NOT_FOUND"

    res = await client.post("/admin/settlements/run", json={"settle_date": str(_today())}, headers=ADMIN)
    assert res.status_code == 201, res.text
    run2 = res.json()
    assert (run2["tx_count"], _positions(run2)) == (1, [("901", 1, 20_000, 0, 0, -20_000), ("902", 0, 0, 1, 20_000, 20_000)])

    recon_902 = (await receiver.get(f"/v1/settlements/{run2['run_id']}")).json()
    assert [(t["transfer_id"], t["direction"], t["counterparty_bank"], t["amount"]) for t in recon_902["transfers"]] == [
        (cheat_id, "RECEIVED", "901", 20_000)]
    # 902가 자기 장부(received)와 맞춰 보면 허브에만 있는 거래가 나온다 = MISMATCH의 재료 (SPEC 10.6, AvengersBank의 hub_only)
    hub_only = {t["transfer_id"] for t in recon_902["transfers"] if t["direction"] == "RECEIVED"} - set(sim.received)
    assert hub_only == {cheat_id}

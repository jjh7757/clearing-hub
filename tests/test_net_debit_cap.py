"""순채무한도 검사 (SETTLEMENT_PLAN 2절 6·7번, 4.4, 5.2, 7절 2단계, 8절 N1~N3·S7).

이 파일은 은행을 예치금 0·한도 0으로 시작한다 (conftest의 넉넉한 기본 한도를 끈다).
"""

import pytest

import bank_sim.main as sim
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.reserve import service as reserve
from clearing_hub.domains.reserve.models import PositionStatus
from clearing_hub.domains.transfers.exposure import open_exposure
from clearing_hub.domains.transfers.models import Transfer, TransferStatus
from tests.conftest import ADMIN, GENEROUS_CAP, set_net_debit_cap, transfer_payload

pytestmark = pytest.mark.anyio


@pytest.fixture
def net_debit_cap() -> int:
    return 0


async def _deposit(client, bank_code: str, amount: int):
    res = await client.post(f"/admin/reserve/{bank_code}/deposit", json={"amount": amount}, headers=ADMIN)
    assert res.status_code == 200, res.text
    return res


async def _reserve(bank) -> dict:
    res = await bank.get("/v1/reserve")
    assert res.status_code == 200, res.text
    return res.json()


async def _admin_reserve(client) -> dict[str, dict]:
    return {r["bank_code"]: r for r in (await client.get("/admin/reserve", headers=ADMIN)).json()}


async def _exposure(bank_code: str) -> int:
    async with SessionLocal() as db:
        return await open_exposure(db, bank_code)


def _assert_cap_rejected(res) -> None:
    assert res.status_code == 400, res.text
    body = res.json()
    assert body["error"] == "LIMIT_EXCEEDED" and body["status"] == "FAILED"  # 보내는 은행은 출금을 되돌린다
    assert "순채무한도" in body["message"]


async def _send_902_to_901(sender, receiver, monkeypatch, amount: int) -> dict:
    """902 → 901 이체를 허브 API로 만든다. 901의 base_url도 bank_sim이라, 이때만 bank_sim이 901의 secret으로
    허브 서명을 검증하게 바꾼다. 902는 한도를 넉넉히 준다."""
    await set_net_debit_cap(sender.client, "902", GENEROUS_CAP)
    payload = {**transfer_payload("110-123-0001", amount=amount), "to_bank": "901"}
    monkeypatch.setenv("SIM_HUB_SECRET", sender.secret)
    try:
        res = await receiver.post("/v1/transfers", payload)
    finally:
        monkeypatch.setenv("SIM_HUB_SECRET", receiver.secret)
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text
    return payload


async def test_n1_no_reserve_no_cap_is_rejected(client, banks):
    """N1: 예치 0·한도 0 → 400 LIMIT_EXCEEDED FAILED. 허브에 거래가 없고 받는 은행에 입금도 없다."""
    sender, _ = banks
    payload = transfer_payload("110-123-0001", amount=50_000)
    res = await sender.post("/v1/transfers", payload)
    _assert_cap_rejected(res)
    assert res.json()["transfer_id"] == payload["transfer_id"]
    assert "예치금 0원" in res.json()["message"] and "요청 50,000원" in res.json()["message"]
    detail = await sender.get(f"/v1/transfers/{payload['transfer_id']}")
    assert detail.status_code == 404 and detail.json()["error"] == "TRANSFER_NOT_FOUND"
    assert sim.received == {}
    assert (await client.get("/admin/transfers", headers=ADMIN)).json() == []

    # 예치금을 넣으면 된다. 경계값(순채무 + 금액 = 예치금 + 한도)은 허용
    await _deposit(client, "901", 50_000)
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=50_000))
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED", res.text


async def test_cap_alone_allows_sending(client, banks):
    """예치금이 없어도 순채무한도만큼은 보낼 수 있다 (일중당좌대출 역할)."""
    sender, _ = banks
    await set_net_debit_cap(client, "901", 80_000)
    assert (await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=80_000))).status_code == 200
    _assert_cap_rejected(await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=1)))


async def test_n2_pending_counts_as_exposure(client, banks):
    """N2: 예치 10만, 5만 2건(하나는 0003 타임아웃 → PENDING) → 3번째 5만 거절."""
    sender, _ = banks
    await _deposit(client, "901", 100_000)
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=50_000))
    assert res.status_code == 200, res.text
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0003", amount=50_000))
    assert res.status_code == 202 and res.json()["status"] == "PENDING", res.text

    assert await _exposure("901") == 100_000  # PENDING도 나갈 수 있는 돈이다
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=50_000))
    _assert_cap_rejected(res)
    assert "미정산 순채무 100,000원" in res.json()["message"] and "예치금 100,000원" in res.json()["message"]


async def test_n3_received_completed_raises_room(client, banks, monkeypatch):
    """N3: 902가 901에게 COMPLETED로 10만 보냈으면 901은 예치금·한도 0이어도 그만큼 더 보낼 수 있다."""
    sender, receiver = banks
    await _send_902_to_901(sender, receiver, monkeypatch, 100_000)
    assert await _exposure("901") == -100_000 and await _exposure("902") == 100_000
    assert (await _reserve(sender))["available"] == 100_000

    assert (await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=100_000))).status_code == 200
    assert await _exposure("901") == 0
    _assert_cap_rejected(await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=1)))


async def test_failed_transfer_is_not_exposure(client, banks):
    """받는 은행이 거절한 거래(0002 계좌 없음, FAILED)는 순채무에 넣지 않는다."""
    sender, _ = banks
    await _deposit(client, "901", 50_000)
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0002", amount=50_000))
    assert res.status_code == 404 and res.json()["error"] == "ACCOUNT_NOT_FOUND" and res.json()["status"] == "FAILED"
    assert await _exposure("901") == 0
    assert (await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=50_000))).status_code == 200


async def test_open_exposure_by_status(banks):
    """4.4의 상태 조건: 보낸 쪽은 FAILED만 빼고, 받은 쪽은 COMPLETED만 센다."""
    amounts = {s: 10 ** i for i, s in enumerate(TransferStatus)}  # 상태마다 자릿수가 다른 금액
    async with SessionLocal() as db:
        for status, amount in amounts.items():
            for from_bank, to_bank in (("901", "902"), ("902", "901")):
                db.add(Transfer(transfer_id=f"{from_bank}-{status}", from_bank=from_bank, from_account="1",
                                to_bank=to_bank, to_account="2", amount=amount, sender_name="x", status=status))
        await db.commit()
        sent = sum(a for s, a in amounts.items() if s != TransferStatus.FAILED)
        recv = amounts[TransferStatus.COMPLETED]
        assert await open_exposure(db, "901") == sent - recv
        assert await open_exposure(db, "902") == sent - recv
        assert await open_exposure(db, "903") == 0  # 거래가 없으면 0 (NULL이 아니라)


async def test_per_transfer_limit_and_cap_differ_by_message(client, banks):
    """1회 상한(1억 초과)과 순채무한도는 둘 다 LIMIT_EXCEEDED FAILED이고, message로 구분한다."""
    sender, _ = banks
    await set_net_debit_cap(client, "901", GENEROUS_CAP)
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=100_000_001))
    assert res.status_code == 400 and res.json()["error"] == "LIMIT_EXCEEDED" and res.json()["status"] == "FAILED"
    assert "1회 이체 상한" in res.json()["message"] and "순채무한도" not in res.json()["message"]

    await set_net_debit_cap(client, "901", 0)
    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=50_000))
    _assert_cap_rejected(res)
    assert "1회 이체 상한" not in res.json()["message"]


async def test_reserve_views_show_exposure(client, banks):
    """GET /v1/reserve·/admin/reserve·입출금 응답의 exposure·available이 이체에 따라 바뀐다."""
    sender, receiver = banks
    await _deposit(client, "901", 300_000)
    assert (await _reserve(sender))["exposure"] == 0
    assert (await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=50_000))).status_code == 200

    assert await _reserve(sender) == {"bank_code": "901", "balance": 300_000, "net_debit_cap": 0, "exposure": 50_000,
                                      "available": 250_000}
    mine = await _reserve(receiver)
    assert mine["exposure"] == -50_000 and mine["available"] == 50_000  # 받은 쪽은 그만큼 더 보낼 수 있다

    listed = await _admin_reserve(client)
    assert (listed["901"]["exposure"], listed["901"]["available"]) == (50_000, 250_000)
    assert (listed["902"]["exposure"], listed["902"]["available"]) == (-50_000, 50_000)

    res = await _deposit(client, "901", 10_000)  # 입출금 응답의 요약에도 들어간다
    assert res.json()["reserve"]["exposure"] == 50_000 and res.json()["reserve"]["available"] == 260_000


async def test_s7_overdue_bank_cannot_send_but_receives(client, banks, monkeypatch):
    """S7(앞부분): 정산으로 잔액이 −한도 아래(OVERDUE)가 된 은행은 송금이 막히고, 받기는 된다."""
    sender, receiver = banks
    await _deposit(client, "901", 50_000)
    async with SessionLocal() as db, db.begin():
        results = await reserve.apply_instruction(db, 1, [("901", -70_000), ("902", 70_000)])
    assert results[0].status == PositionStatus.OVERDUE and results[0].balance_after == -20_000

    res = await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=10_000))
    _assert_cap_rejected(res)
    assert "예치금 -20,000원" in res.json()["message"]

    payload = await _send_902_to_901(sender, receiver, monkeypatch, 10_000)  # 받는 쪽으로는 입금된다
    assert payload["transfer_id"] in sim.received
    _assert_cap_rejected(await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=10_000)))

    await _deposit(client, "901", 30_000)  # 관리자가 예치금을 넣으면 풀린다 (2절 7번)
    assert (await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=10_000))).status_code == 200


async def test_resend_same_transfer_id_skips_cap_check(client, banks):
    """같은 transfer_id 재전송은 이미 접수된 거래라 한도와 상관없이 저장된 결과를 돌려준다 (SPEC 3.3 멱등)."""
    sender, _ = banks
    await _deposit(client, "901", 50_000)
    payload = transfer_payload("110-123-0001", amount=50_000)
    assert (await sender.post("/v1/transfers", payload)).status_code == 200

    res = await client.post("/admin/reserve/901/adjust", json={"amount": -100_000, "memo": "시험: 한도 밖으로"},
                            headers=ADMIN)
    assert res.status_code == 200 and res.json()["reserve"]["available"] == -100_000
    res = await sender.post("/v1/transfers", payload)
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED"
    assert len(sim.received) == 1
    _assert_cap_rejected(await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=1)))

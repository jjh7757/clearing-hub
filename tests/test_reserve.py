"""예치금 원장 (SETTLEMENT_PLAN 7절 1단계, 8절 R1~R3): 관리자 입출금·정정, 순채무한도, 정산 지시서 반영."""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from clearing_hub.core.database import SessionLocal
from clearing_hub.core.exceptions import ApiException
from clearing_hub.domains.reserve import service as reserve
from clearing_hub.domains.reserve.models import PositionStatus, ReserveAccount, ReserveLedgerEntry
from tests.conftest import ADMIN

pytestmark = pytest.mark.anyio


@pytest.fixture
def net_debit_cap() -> int:
    """이 파일은 한도 0에서 시작하는 것을 검사한다 (conftest의 넉넉한 기본 한도를 끈다)."""
    return 0


async def _deposit(client, bank_code: str, amount: int, memo: str = ""):
    return await client.post(f"/admin/reserve/{bank_code}/deposit", json={"amount": amount, "memo": memo}, headers=ADMIN)


async def _balance(bank_code: str) -> int:
    async with SessionLocal() as db:
        return (await db.get(ReserveAccount, bank_code)).balance


async def _entries(bank_code: str) -> list[ReserveLedgerEntry]:
    async with SessionLocal() as db:
        stmt = select(ReserveLedgerEntry).where(ReserveLedgerEntry.bank_code == bank_code).order_by(ReserveLedgerEntry.entry_id)
        return list((await db.scalars(stmt)).all())


async def _set_cap(client, bank_code: str, cap: int):
    res = await client.patch(f"/admin/banks/{bank_code}", json={"net_debit_cap": cap}, headers=ADMIN)
    assert res.status_code == 200, res.text
    return res


# --- 은행 등록·GET /v1/reserve ---

async def test_register_bank_creates_reserve_account(banks):
    async with SessionLocal() as db:
        for code in ("901", "902"):
            account = await db.get(ReserveAccount, code)
            assert account is not None and account.balance == 0


async def test_my_reserve(client, banks):
    sender, receiver = banks
    await _deposit(client, "901", 250_000)
    res = await sender.get("/v1/reserve")
    assert res.status_code == 200, res.text
    assert res.json() == {"bank_code": "901", "balance": 250_000, "net_debit_cap": 0, "exposure": 0, "available": 250_000}
    # 다른 은행은 자기 것만 본다
    assert (await receiver.get("/v1/reserve")).json()["bank_code"] == "902"
    assert (await receiver.get("/v1/reserve")).json()["balance"] == 0
    # 인증 없음
    res = await client.get("/v1/reserve")
    assert res.status_code == 401 and res.json()["error"] == "UNAUTHORIZED"


async def test_net_debit_cap_changes_available(client, banks):
    sender, _ = banks
    res = await _set_cap(client, "901", 500_000)
    assert res.json()["net_debit_cap"] == 500_000 and res.json()["name"] == "AvengersBank"  # 다른 필드는 그대로
    assert (await sender.get("/v1/reserve")).json()["available"] == 500_000
    await _deposit(client, "901", 100_000)
    assert (await sender.get("/v1/reserve")).json()["available"] == 600_000
    listed = {b["bank_code"]: b for b in (await client.get("/admin/banks", headers=ADMIN)).json()}
    assert listed["901"]["net_debit_cap"] == 500_000 and listed["902"]["net_debit_cap"] == 0

    res = await client.patch("/admin/banks/901", json={"net_debit_cap": -1}, headers=ADMIN)
    assert res.status_code == 422 and res.json()["error"] == "VALIDATION_ERROR"
    assert (await sender.get("/v1/reserve")).json()["net_debit_cap"] == 500_000


# --- 관리자 입출금·정정 ---

async def test_r1_deposit_then_withdraw(client, banks):
    """R1: 예치 100만 → 인출 30만 → 잔액 70만, 분개 2건."""
    res = await _deposit(client, "901", 1_000_000, "초기 예치")
    assert res.status_code == 200, res.text
    assert res.json()["entry"]["kind"] == "DEPOSIT" and res.json()["reserve"]["balance"] == 1_000_000
    res = await client.post("/admin/reserve/901/withdraw", json={"amount": 300_000, "memo": "일부 인출"}, headers=ADMIN)
    assert res.status_code == 200, res.text
    entry = res.json()["entry"]
    assert entry["kind"] == "WITHDRAW" and entry["amount"] == -300_000 and entry["balance_after"] == 700_000
    assert entry["created_by"] == "admin" and entry["run_id"] is None and entry["memo"] == "일부 인출"
    assert res.json()["reserve"]["balance"] == 700_000

    assert await _balance("901") == 700_000
    ledger = (await client.get("/admin/reserve/901/ledger", headers=ADMIN)).json()
    assert [(e["kind"], e["amount"], e["balance_after"]) for e in ledger] == [  # 최신순
        ("WITHDRAW", -300_000, 700_000), ("DEPOSIT", 1_000_000, 1_000_000)]
    assert sum(e["amount"] for e in ledger) == await _balance("901")  # 분개 합 = 잔액


async def test_r2_withdraw_over_limit(client, banks):
    """R2: 한도 0, 잔액 10만에서 20만 인출 → 거절. 한도를 주면 그만큼 마이너스까지 된다."""
    await _deposit(client, "901", 100_000)
    res = await client.post("/admin/reserve/901/withdraw", json={"amount": 200_000}, headers=ADMIN)
    assert res.status_code == 400 and res.json()["error"] == "INSUFFICIENT_RESERVE"
    assert "100000원" in res.json()["message"] and "한도 0원" in res.json()["message"]
    assert await _balance("901") == 100_000 and len(await _entries("901")) == 1

    await _set_cap(client, "901", 100_000)
    res = await client.post("/admin/reserve/901/withdraw", json={"amount": 200_000}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["reserve"]["balance"] == -100_000  # 잔액 − 금액 = −한도: 허용
    res = await client.post("/admin/reserve/901/withdraw", json={"amount": 1}, headers=ADMIN)
    assert res.status_code == 400 and res.json()["error"] == "INSUFFICIENT_RESERVE"


async def test_adjust_both_signs_without_limit(client, banks):
    res = await client.post("/admin/reserve/902/adjust", json={"amount": 5_000, "memo": "이중 정산 정정"}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["entry"]["kind"] == "ADJUST"
    res = await client.post("/admin/reserve/902/adjust", json={"amount": -20_000, "memo": "정산 오류 환수"}, headers=ADMIN)
    assert res.status_code == 200, res.text  # 한도 0이어도 정정은 한도 검사를 하지 않는다
    assert res.json()["reserve"]["balance"] == -15_000 and res.json()["entry"]["memo"] == "정산 오류 환수"


async def test_validation_errors(client, banks):
    for path, body in (
        ("deposit", {"amount": 0}), ("deposit", {"amount": -1000}), ("deposit", {}), ("deposit", {"amount": 10.5}),
        ("withdraw", {"amount": 0}), ("withdraw", {"amount": -1}),
        ("adjust", {"amount": 0, "memo": "사유"}), ("adjust", {"amount": 1000}), ("adjust", {"amount": 1000, "memo": ""}),
    ):
        res = await client.post(f"/admin/reserve/901/{path}", json=body, headers=ADMIN)
        assert res.status_code == 422 and res.json()["error"] == "VALIDATION_ERROR", (path, body, res.text)
    assert await _entries("901") == [] and await _balance("901") == 0

    res = await _deposit(client, "999", 1000)
    assert res.status_code == 404 and res.json()["error"] == "BANK_NOT_FOUND"
    assert (await client.get("/admin/reserve/999/ledger", headers=ADMIN)).status_code == 404
    assert (await client.post("/admin/reserve/901/deposit", json={"amount": 1000})).status_code == 401  # 관리 토큰 없음
    assert (await client.get("/admin/reserve")).status_code == 401


async def test_service_validates_amount_too(banks):
    """API를 거치지 않고 서비스를 직접 불러도 같은 검사를 한다."""
    async with SessionLocal() as db, db.begin():
        for call in (reserve.deposit(db, "901", 0), reserve.withdraw(db, "901", -5), reserve.adjust(db, "901", 0, "x"),
                     reserve.adjust(db, "901", 100, "  ")):
            with pytest.raises(ApiException) as exc:
                await call
            assert exc.value.status_code == 422


async def test_null_run_id_entries_do_not_collide(client, banks):
    """run_id가 NULL인 같은 종류의 분개 여러 건은 유니크 제약(run_id, bank_code, kind)에 걸리지 않는다."""
    for amount in (1_000, 2_000, 3_000):
        assert (await _deposit(client, "901", amount)).status_code == 200
    for amount in (100, 200):
        assert (await client.post("/admin/reserve/901/withdraw", json={"amount": amount}, headers=ADMIN)).status_code == 200
    assert len(await _entries("901")) == 5 and await _balance("901") == 5_700


async def test_unique_constraint_blocks_same_run(banks):
    """run_id가 있으면 같은 (run_id, bank_code, kind)는 DB가 막는다 (SQLite에서도)."""
    async with SessionLocal() as db:
        for _ in range(2):
            db.add(ReserveLedgerEntry(bank_code="901", kind="SETTLE_CREDIT", amount=1, run_id=7, balance_after=1,
                                      created_by="settlement"))
        with pytest.raises(IntegrityError):
            await db.commit()


# --- 계좌가 없는 기존 은행 (마이그레이션은 백필하지 않는다) ---

async def test_admin_list_and_get_or_create_for_old_bank(client, banks):
    sender, _ = banks
    async with SessionLocal() as db:
        await db.execute(delete(ReserveAccount).where(ReserveAccount.bank_code == "901"))  # 이 기능 전에 등록된 은행
        await db.commit()
    listed = {r["bank_code"]: r for r in (await client.get("/admin/reserve", headers=ADMIN)).json()}
    assert set(listed) == {"901", "902"}
    assert listed["901"]["balance"] == 0 and listed["901"]["updated_at"] is None and listed["901"]["available"] == 0
    assert listed["902"]["updated_at"] is not None
    async with SessionLocal() as db:
        assert await db.get(ReserveAccount, "901") is None  # 목록 조회는 계좌를 만들지 않는다

    assert (await sender.get("/v1/reserve")).json()["balance"] == 0  # 은행이 조회하면 만든다
    assert await _balance("901") == 0
    async with SessionLocal() as db:
        await db.execute(delete(ReserveAccount).where(ReserveAccount.bank_code == "901"))
        await db.commit()
    assert (await _deposit(client, "901", 7_000)).json()["reserve"]["balance"] == 7_000  # 입금도 만든다


# --- apply_instruction (정산 지시서, 5.1의 12단계) ---

@pytest.mark.parametrize(("deposit", "cap", "expected_901"), [
    (100_000, 0, PositionStatus.OK),  # 잔액 3만
    (0, 100_000, PositionStatus.OVERDRAWN),  # 잔액 −7만, 한도 10만 안
    (50_000, 0, PositionStatus.OVERDUE),  # 잔액 −2만, 한도 0 넘음
])
async def test_apply_instruction(client, banks, deposit, cap, expected_901):
    if deposit:
        await _deposit(client, "901", deposit)
    if cap:
        await _set_cap(client, "901", cap)
    async with SessionLocal() as db, db.begin():
        results = await reserve.apply_instruction(db, 1, [("902", 70_000), ("901", -70_000)])

    assert [(r.bank_code, r.net_amount, r.status, r.balance_after) for r in results] == [
        ("901", -70_000, expected_901, deposit - 70_000), ("902", 70_000, PositionStatus.OK, 70_000)]
    assert await _balance("901") == deposit - 70_000 and await _balance("902") == 70_000
    debit, credit = (await _entries("901"))[-1], (await _entries("902"))[-1]
    assert (debit.kind, debit.amount, debit.run_id, debit.balance_after, debit.created_by) == (
        "SETTLE_DEBIT", -70_000, 1, deposit - 70_000, "settlement")
    assert (credit.kind, credit.amount, credit.run_id, credit.balance_after) == ("SETTLE_CREDIT", 70_000, 1, 70_000)


async def test_apply_instruction_zero_net_has_no_entry(banks):
    async with SessionLocal() as db, db.begin():
        results = await reserve.apply_instruction(db, 3, [("901", 0), ("902", 0)])
    assert [r.status for r in results] == [PositionStatus.OK, PositionStatus.OK]
    assert await _entries("901") == [] and await _entries("902") == []


async def test_r3_apply_same_run_twice(client, banks):
    """R3: 같은 run_id 두 번 → 409, 잔액은 한 번만 바뀐다."""
    await _deposit(client, "901", 100_000)
    instruction = [("901", -70_000), ("902", 70_000)]
    async with SessionLocal() as db, db.begin():
        await reserve.apply_instruction(db, 1, instruction)

    with pytest.raises(ApiException) as exc:
        async with SessionLocal() as db, db.begin():
            await reserve.apply_instruction(db, 1, instruction)
    assert exc.value.status_code == 409 and exc.value.code == "RESERVE_ALREADY_APPLIED"
    assert await _balance("901") == 30_000 and await _balance("902") == 70_000


async def test_r3_unique_constraint_backstop(client, banks, monkeypatch):
    """미리 확인을 지나쳐도(동시에 두 번 확정된 경우) 유니크 제약이 막고, 롤백 뒤 잔액은 그대로다."""
    instruction = [("901", -70_000), ("902", 70_000)]
    async with SessionLocal() as db, db.begin():
        await reserve.apply_instruction(db, 1, instruction)

    async def not_applied(db, run_id):
        return False

    monkeypatch.setattr(reserve, "_already_applied", not_applied)
    with pytest.raises(ApiException) as exc:
        async with SessionLocal() as db, db.begin():
            await reserve.apply_instruction(db, 1, instruction)
    assert exc.value.status_code == 409 and exc.value.code == "RESERVE_ALREADY_APPLIED"
    assert isinstance(exc.value.__cause__, IntegrityError)
    assert await _balance("901") == -70_000 and await _balance("902") == 70_000
    assert len(await _entries("901")) == 1 and len(await _entries("902")) == 1


async def test_apply_instruction_rejects_bad_instruction(banks):
    async with SessionLocal() as db, db.begin():
        with pytest.raises(ValueError, match="두 번"):
            await reserve.apply_instruction(db, 1, [("901", -10), ("901", 10)])
        with pytest.raises(ValueError, match="합이 0"):
            await reserve.apply_instruction(db, 1, [("901", -10), ("902", 5)])
        with pytest.raises(ApiException) as exc:
            await reserve.apply_instruction(db, 1, [("901", -10), ("999", 10)])
        assert exc.value.code == "BANK_NOT_FOUND"
    assert await _entries("901") == [] and await _balance("901") == 0  # 없는 은행이 있으면 아무것도 바꾸지 않는다

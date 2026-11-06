"""본인 명의 계좌 조회 (SPEC 9절): 동시 조회, account_ref 이체, 참조값 검사, 횟수 제한, 기록 정리."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

import bank_sim.main as sim
from clearing_hub.core import bank_client as bank_client_module
from clearing_hub.core.bank_client import BankReply
from clearing_hub.core.ci import virtual_ci
from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.accounts.models import AccountLookup, AccountRef
from clearing_hub.domains.accounts.service import mask_account, purge_expired
from tests.conftest import ADMIN, SIM_PRIVATE_KEY, register_bank

pytestmark = pytest.mark.anyio

CI_KEY = "example-ci-key-change-me"  # bank_sim 기본값과 같다
KIM_CI = virtual_ci(CI_KEY, "김철수", "19850315", "010-9876-5432")  # bank_sim에 계좌 2개
NOBODY_CI = virtual_ci(CI_KEY, "없는사람", "20000101", "01000000000")


def lookup_body(ci: str = KIM_CI) -> dict:
    return {"ci": ci, "consent_id": "c-test-0001"}


def ref_transfer(ref: str, to_bank: str = "902") -> dict:
    return {"transfer_id": str(uuid.uuid4()), "to_bank": to_bank, "to_account_ref": ref, "from_account": "110-200-456789",
            "amount": 30000, "sender_name": "김철수", "memo": ""}


def test_virtual_ci_matches_spec_example():
    assert virtual_ci(CI_KEY, "홍길동", "19900101", "01012345678") == "aef7438cc8491d1ac5ba115604e9c5af849d677f2979be05df1c307366c64be2"
    assert virtual_ci(CI_KEY, " 홍 길동 ", "19900101", "010-1234-5678") == virtual_ci(CI_KEY, "홍길동", "19900101", "01012345678")


def test_mask_account():
    assert mask_account("110-123-0001") == "110-***-0001"
    assert mask_account("1101230001") == "110***0001"
    assert mask_account("12345") == "*2345"


async def test_lookup_then_transfer_by_ref(client, banks):
    sender, _ = banks
    # 9.4가 없는 은행: 가짜 은행의 없는 경로라 서명된 404 (응답 서명은 가짜 은행 키로, SPEC 2.5)
    await register_bank(client, "903", "NoLookupBank", "http://sim/interbank/none", private_key=SIM_PRIVATE_KEY)
    res = await sender.post("/v1/accounts/lookup", lookup_body())
    assert res.status_code == 200, res.text
    data = res.json()
    assert {b["bank_code"]: b["result"] for b in data["banks"]} == {"902": "OK", "903": "UNSUPPORTED"}
    assert [a["account_masked"] for a in data["accounts"]] == ["110-***-0001", "110-***-0001"]
    assert data["accounts"][0]["bank_name"] == "SimBank" and data["accounts"][1]["product_name"] == "급여통장"
    assert all(a["account_ref"].startswith("acr_") for a in data["accounts"])
    assert "account_num" not in data["accounts"][0]  # 전체 계좌번호는 주지 않는다

    payload = ref_transfer(data["accounts"][1]["account_ref"])
    res = await sender.post("/v1/transfers", payload)
    assert res.status_code == 200 and res.json()["status"] == "COMPLETED" and res.json()["holder_name"] == "김철수"
    assert sim.received[payload["transfer_id"]]["to_account"] == "110-456-0001"
    # 30분 안에는 같은 ref를 다시 쓸 수 있다
    res = await sender.post("/v1/transfers", ref_transfer(data["accounts"][1]["account_ref"]))
    assert res.status_code == 200


async def test_demo_customer_hong(banks):
    """AvengersBank 시연 고객 demo(홍길동)와 같은 사람: 계좌 1개, 수취 조회·입금 응답에 홍길동."""
    sender, _ = banks
    hong = virtual_ci(CI_KEY, "홍길동", "19900101", "010-0000-0000")
    data = (await sender.post("/v1/accounts/lookup", lookup_body(hong))).json()
    assert [(a["account_masked"], a["product_name"]) for a in data["accounts"]] == [("110-***-0001", "자유입출금")]
    res = await sender.post("/v1/inquiry", {"to_bank": "902", "to_account": "110-789-0001"})
    assert res.json()["holder_name"] == "홍길동"
    res = await sender.post("/v1/transfers", ref_transfer(data["accounts"][0]["account_ref"]))
    assert res.status_code == 200 and res.json()["holder_name"] == "홍길동"


async def test_lookup_unknown_person_is_empty(banks):
    sender, _ = banks
    res = await sender.post("/v1/accounts/lookup", lookup_body(NOBODY_CI))
    assert res.status_code == 200
    assert res.json()["accounts"] == [] and res.json()["banks"] == [{"bank_code": "902", "result": "OK", "count": 0}]


async def test_lookup_timeout_and_error(banks, monkeypatch):
    sender, _ = banks
    replies = iter([BankReply(None, {}), BankReply(500, {"error": "INTERNAL_ERROR"}), BankReply(200, {"accounts": "x"})])

    async def fake_accounts(self, bank, ci):
        return next(replies)

    monkeypatch.setattr(bank_client_module.BankClient, "accounts", fake_accounts)
    results = []
    for _ in range(3):
        res = await sender.post("/v1/accounts/lookup", lookup_body())
        assert res.status_code == 200
        results.append(res.json()["banks"][0]["result"])
    assert results == ["TIMEOUT", "ERROR", "ERROR"]


async def test_account_ref_rules(client, banks):
    sender, receiver = banks
    ref = (await sender.post("/v1/accounts/lookup", lookup_body())).json()["accounts"][0]["account_ref"]

    # 다른 은행은 쓸 수 없다
    res = await receiver.post("/v1/transfers", ref_transfer(ref, to_bank="901"))
    assert res.status_code == 400 and res.json()["error"] == "ACCOUNT_REF_INVALID"
    # to_bank가 ref와 다르면 거절
    await register_bank(client, "903", "OtherBank")
    res = await sender.post("/v1/transfers", ref_transfer(ref, to_bank="903"))
    assert res.json()["error"] == "ACCOUNT_REF_INVALID"
    # 없는 ref
    res = await sender.post("/v1/transfers", ref_transfer("acr_nothing"))
    assert res.json()["error"] == "ACCOUNT_REF_INVALID"
    # to_account와 to_account_ref는 하나만
    res = await sender.post("/v1/transfers", {**ref_transfer(ref), "to_account": "110-123-0001"})
    assert res.status_code == 422 and "하나만" in res.json()["message"]
    res = await sender.post("/v1/transfers", {k: v for k, v in ref_transfer(ref).items() if k != "to_account_ref"})
    assert res.status_code == 422
    # 만료
    async with SessionLocal() as db:
        await db.execute(update(AccountRef).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await db.commit()
    res = await sender.post("/v1/transfers", ref_transfer(ref))
    assert res.json()["error"] == "ACCOUNT_REF_INVALID"
    assert len(sim.received) == 0  # 어느 경우에도 입금되지 않았다


async def test_lookup_validation_and_limit(banks, monkeypatch):
    sender, receiver = banks
    res = await sender.post("/v1/accounts/lookup", {"ci": "ABC", "consent_id": "c"})
    assert res.status_code == 422
    res = await sender.post("/v1/accounts/lookup", {"ci": KIM_CI})
    assert res.status_code == 422  # consent_id 없음

    monkeypatch.setattr(get_settings(), "lookup_daily_limit", 2)
    for _ in range(2):
        assert (await sender.post("/v1/accounts/lookup", lookup_body())).status_code == 200
    res = await sender.post("/v1/accounts/lookup", lookup_body())
    assert res.status_code == 429 and res.json()["error"] == "TOO_MANY_LOOKUPS"
    # 다른 사람, 다른 은행은 따로 센다
    assert (await sender.post("/v1/accounts/lookup", lookup_body(NOBODY_CI))).status_code == 200
    assert (await receiver.post("/v1/accounts/lookup", lookup_body())).status_code == 200


async def test_lookup_log_and_purge(client, banks):
    sender, _ = banks
    await sender.post("/v1/accounts/lookup", lookup_body())
    listed = (await client.get("/admin/account-lookups", headers=ADMIN)).json()
    assert listed[0]["consent_id"] == "c-test-0001" and listed[0]["from_bank"] == "901" and listed[0]["account_count"] == 2
    assert "ci" not in listed[0] and "ci_hash" not in listed[0]

    async with SessionLocal() as db:
        await db.execute(update(AccountLookup).values(created_at=datetime.now(UTC) - timedelta(hours=25)))
        await db.execute(update(AccountRef).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await db.commit()
    async with SessionLocal() as db:
        await purge_expired(db)
    async with SessionLocal() as db:
        assert (await db.scalars(select(AccountRef))).all() == []
        assert (await db.scalar(select(AccountLookup.ci_hash))) is None
        assert (await db.scalar(select(AccountLookup.consent_id))) == "c-test-0001"  # 기록 자체는 남는다

"""은행용 대사 API: GET /v1/settlements, /v1/settlements/{run_id} (SETTLEMENT_PLAN 2절 9번, 5.1의 10단계, 6.1,
7절 5단계, 8절 S8, SPEC 10.4·10.5).

은행은 서명해서 부른다 (`SignedBank.get`이 경로를 서명에 넣는다 — SPEC 2.2). 응답에는 요청한 은행 자신의 몫만 있어야
한다: 다른 은행의 포지션·잔액, 계좌번호·이름이 없는지 키 집합으로 본다.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.settlement.models import SettlementRun
from tests.conftest import ADMIN, register_bank, send_902_to_901, transfer_payload

pytestmark = pytest.mark.anyio
KST = ZoneInfo("Asia/Seoul")

LIST_KEYS = {"run_id", "settle_date", "cutoff_at", "status", "settled_at", "net_amount", "result"}
DETAIL_KEYS = {"run_id", "settle_date", "cutoff_at", "status", "settled_at", "position", "transfers"}
POSITION_KEYS = {"sent_count", "sent_total", "recv_count", "recv_total", "net_amount", "result", "balance_after"}
TRANSFER_KEYS = {"transfer_id", "direction", "counterparty_bank", "amount", "finished_at"}
EMPTY_POSITION = {"sent_count": 0, "sent_total": 0, "recv_count": 0, "recv_total": 0, "net_amount": 0,
                  "result": None, "balance_after": None}


def _today() -> date:
    return datetime.now(KST).date()


async def _run(client) -> dict:
    res = await client.post("/admin/settlements/run", json={"settle_date": str(_today())}, headers=ADMIN)
    assert res.status_code == 201, res.text
    return res.json()


async def _confirm(client, run_id: int) -> None:
    res = await client.post(f"/admin/settlements/{run_id}/confirm", json={}, headers=ADMIN)
    assert res.status_code == 200, res.text


async def _cancel(client, run_id: int) -> None:
    res = await client.post(f"/admin/settlements/{run_id}/cancel", headers=ADMIN)
    assert res.status_code == 200, res.text


async def _s1(banks, monkeypatch) -> tuple[list[str], str]:
    """S1의 하루치: 901→902 5만 2건, 902→901 3만 1건. (901이 보낸 ID 2개, 902가 보낸 ID)"""
    sender, receiver = banks
    sent = []
    for _ in range(2):
        payload = transfer_payload("110-123-0001")
        res = await sender.post("/v1/transfers", payload)
        assert res.json()["status"] == "COMPLETED", res.text
        sent.append(payload["transfer_id"])
    return sent, (await send_902_to_901(sender, receiver, monkeypatch, 30_000))["transfer_id"]


async def _detail(bank, run_id: int) -> dict:
    res = await bank.get(f"/v1/settlements/{run_id}")
    assert res.status_code == 200, res.text
    return res.json()


async def _list(bank) -> list[dict]:
    res = await bank.get("/v1/settlements")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"settlements"}
    for item in body["settlements"]:
        assert set(item) == LIST_KEYS
    return body["settlements"]


def _assert_only_mine(body: dict) -> None:
    """대사 파일의 키 집합: 계좌번호·이름·다른 은행의 포지션(bank_code가 붙은 행, positions·pairs)이 없다."""
    assert set(body) == DETAIL_KEYS
    assert set(body["position"]) == POSITION_KEYS
    for t in body["transfers"]:
        assert set(t) == TRANSFER_KEYS


# --- S8: 자기 몫만 ---------------------------------------------------------------------------------------------------

async def test_s8_detail_shows_only_my_share(client, banks, monkeypatch):
    """S8: 901은 net −7만, SENT 2·RECEIVED 1(상대 902). 902는 net +7만, 방향이 반대(상대 901). 다른 은행 것은 없다."""
    sender, receiver = banks
    sent_901, sent_902 = await _s1(banks, monkeypatch)
    run = await _run(client)

    mine = await _detail(sender, run["run_id"])
    _assert_only_mine(mine)
    assert (mine["run_id"], mine["status"], mine["settle_date"]) == (run["run_id"], "DRAFT", str(_today()))
    assert mine["cutoff_at"] == run["cutoff_at"] and mine["settled_at"] is None
    assert mine["position"] == {"sent_count": 2, "sent_total": 100_000, "recv_count": 1, "recv_total": 30_000,
                                "net_amount": -70_000, "result": None, "balance_after": None}
    assert [(t["transfer_id"], t["direction"], t["counterparty_bank"], t["amount"]) for t in mine["transfers"]] == [
        (sent_901[0], "SENT", "902", 50_000), (sent_901[1], "SENT", "902", 50_000),
        (sent_902, "RECEIVED", "902", 30_000)]  # 끝난 시각 순
    assert all(datetime.fromisoformat(t["finished_at"]).tzinfo is not None for t in mine["transfers"])

    theirs = await _detail(receiver, run["run_id"])
    _assert_only_mine(theirs)
    assert theirs["position"] == {"sent_count": 1, "sent_total": 30_000, "recv_count": 2, "recv_total": 100_000,
                                  "net_amount": 70_000, "result": None, "balance_after": None}
    assert [(t["transfer_id"], t["direction"], t["counterparty_bank"]) for t in theirs["transfers"]] == [
        (sent_901[0], "RECEIVED", "901"), (sent_901[1], "RECEIVED", "901"), (sent_902, "SENT", "901")]

    # 본문 어디에도 계좌번호·이름이 없다 (키 집합 외에 값으로도 확인)
    for body in (mine, theirs):
        text = str(body)
        assert "110-123-0001" not in text and "110-200-456789" not in text and "홍길동" not in text


async def test_detail_after_confirm_shows_result(client, banks, monkeypatch):
    """확정 뒤 result·balance_after·settled_at이 보인다. 901은 예치 0·한도 넉넉 → −7만 OVERDRAWN, 902는 +7만 OK."""
    sender, receiver = banks
    await _s1(banks, monkeypatch)
    run_id = (await _run(client))["run_id"]
    await _confirm(client, run_id)

    mine, theirs = await _detail(sender, run_id), await _detail(receiver, run_id)
    assert mine["status"] == theirs["status"] == "SETTLED"
    assert mine["settled_at"] is not None and datetime.fromisoformat(mine["settled_at"]).tzinfo is not None
    assert (mine["position"]["result"], mine["position"]["balance_after"]) == ("OVERDRAWN", -70_000)
    assert (theirs["position"]["result"], theirs["position"]["balance_after"]) == ("OK", 70_000)
    assert len(mine["transfers"]) == len(theirs["transfers"]) == 3

    # 목록에도 같은 값
    def summary(rows: list[dict]) -> list[tuple]:
        return [(r["status"], r["net_amount"], r["result"]) for r in rows]

    assert summary(await _list(sender)) == [("SETTLED", -70_000, "OVERDRAWN")]
    assert summary(await _list(receiver)) == [("SETTLED", 70_000, "OK")]


async def test_bank_without_transfers_in_run(client, banks, monkeypatch):
    """그 run에 거래가 없는 은행(903)은 포지션 전부 0·null, 거래 빈 목록. 목록에는 run이 net 0으로 나온다."""
    await _s1(banks, monkeypatch)
    other = await register_bank(client, "903", "ThirdBank")
    run_id = (await _run(client))["run_id"]

    body = await _detail(other, run_id)
    _assert_only_mine(body)
    assert body["position"] == EMPTY_POSITION and body["transfers"] == []
    assert [(r["run_id"], r["net_amount"], r["result"]) for r in await _list(other)] == [(run_id, 0, None)]

    await _confirm(client, run_id)
    assert (await _detail(other, run_id))["position"] == EMPTY_POSITION  # 확정해도 포지션 행이 없으므로 그대로


# --- 목록 ------------------------------------------------------------------------------------------------------------

async def test_list_latest_first_without_cancelled_or_old(client, banks, monkeypatch):
    """목록: 최신순, 은행마다 다른 net_amount, CANCELLED 빠짐, settle_date가 30일보다 오래된 run 빠짐."""
    sender, receiver = banks
    assert await _list(sender) == []

    await _s1(banks, monkeypatch)
    first = (await _run(client))["run_id"]
    await _confirm(client, first)

    await sender.post("/v1/transfers", transfer_payload("110-123-0001", amount=10_000))
    cancelled = (await _run(client))["run_id"]  # 이 run을 취소하면 1만은 다음 run에 다시 들어간다
    await _cancel(client, cancelled)
    latest = (await _run(client))["run_id"]

    mine = await _list(sender)
    assert [r["run_id"] for r in mine] == [latest, first]  # CANCELLED 빠짐, 최신순
    assert [(r["status"], r["net_amount"]) for r in mine] == [("DRAFT", -10_000), ("SETTLED", -70_000)]
    assert mine[0]["result"] is None and mine[0]["settled_at"] is None
    assert [(r["run_id"], r["net_amount"]) for r in await _list(receiver)] == [(latest, 10_000), (first, 70_000)]

    # 30일 경계: 오늘 − 30일은 들어가고, 31일 전은 빠진다
    async with SessionLocal() as db, db.begin():
        await db.execute(update(SettlementRun).where(SettlementRun.run_id == first)
                         .values(settle_date=_today() - timedelta(days=30)))
    assert [r["run_id"] for r in await _list(sender)] == [latest, first]
    async with SessionLocal() as db, db.begin():
        await db.execute(update(SettlementRun).where(SettlementRun.run_id == first)
                         .values(settle_date=_today() - timedelta(days=31)))
    assert [r["run_id"] for r in await _list(sender)] == [latest]

    # 지난 run은 목록에서 빠져도 상세는 그대로 볼 수 있다
    assert (await _detail(sender, first))["status"] == "SETTLED"


async def test_cancelled_detail(client, banks, monkeypatch):
    """CANCELLED 상세: 200, status CANCELLED, transfers []. 포지션은 기록된 값 그대로 (은행은 무시한다)."""
    sender, _ = banks
    await _s1(banks, monkeypatch)
    run_id = (await _run(client))["run_id"]
    assert len((await _detail(sender, run_id))["transfers"]) == 3  # 은행이 DRAFT를 먼저 봤다
    await _cancel(client, run_id)

    body = await _detail(sender, run_id)
    _assert_only_mine(body)
    assert body["status"] == "CANCELLED" and body["transfers"] == []
    assert body["position"]["net_amount"] == -70_000 and body["position"]["result"] is None

    # 같은 거래가 다음 run에 다시 들어가면, 그 run에서 보인다 (취소한 run에서는 계속 비어 있다)
    nxt = (await _run(client))["run_id"]
    assert len((await _detail(sender, nxt))["transfers"]) == 3
    assert (await _detail(sender, run_id))["transfers"] == []


# --- 오류·인증 -------------------------------------------------------------------------------------------------------

async def test_not_found_and_auth(client, banks):
    """없는 run → 404 SETTLEMENT_NOT_FOUND, 정수가 아닌 run_id → 422. 인증 헤더 없음 → 401. 서명 경로는 `/v1/settlements/{id}`로 검증된다:
    다른 경로로 서명한 요청은 401 SIGNATURE_INVALID (정상 경로로 서명한 요청이 통과하는 것은 위 시험들이 보인다)."""
    sender, _ = banks
    run_id = (await _run(client))["run_id"]

    res = await sender.get(f"/v1/settlements/{run_id + 100}")
    assert res.status_code == 404 and res.json()["error"] == "SETTLEMENT_NOT_FOUND"
    res = await sender.get("/v1/settlements/abc")
    assert res.status_code == 422 and res.json()["error"] == "VALIDATION_ERROR"

    for path in ("/v1/settlements", f"/v1/settlements/{run_id}"):
        res = await client.get(path)
        assert res.status_code == 401 and res.json()["error"] == "UNAUTHORIZED"

    wrong = sender.headers("GET", "/v1/settlements", b"")  # 목록 경로로 서명해 상세를 부른다
    res = await client.get(f"/v1/settlements/{run_id}", headers=wrong)
    assert res.status_code == 401 and res.json()["error"] == "SIGNATURE_INVALID"
    as_post = sender.headers("POST", f"/v1/settlements/{run_id}", b"")  # 같은 경로를 POST로 서명해 GET으로 부른다
    res = await client.get(f"/v1/settlements/{run_id}", headers=as_post)
    assert res.status_code == 401 and res.json()["error"] == "SIGNATURE_INVALID"  # 메서드도 서명에 들어간다

    assert (await sender.get(f"/v1/settlements/{run_id}")).status_code == 200


async def test_admin_token_is_not_bank_auth(client, banks):
    """관리자 토큰으로는 은행용 API를 부를 수 없다 (은행 서명만)."""
    res = await client.get("/v1/settlements", headers=ADMIN)
    assert res.status_code == 401


async def test_root_spec_version(client):
    """루트 응답의 규격 버전은 docs/SPEC.md와 같다 (v0.6)."""
    assert (await client.get("/")).json() == {"service": "ClearingHub", "spec": "v0.6"}

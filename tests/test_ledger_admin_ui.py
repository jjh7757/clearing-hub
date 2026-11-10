"""관리자 실시간 장부 화면 (/admin/ui/ledger)과 화면이 읽는 API.

- `GET /admin/settlements/preview`: 본문 없는 `POST /admin/settlements/run`이 지금 만들 표와 같아야 하고, DB를 바꾸지 않는다
- `GET /admin/reserve/ledger`: 전체 은행 분개, 최신순, limit
- `GET /admin/reserve`의 `status` (OK / OVERDRAWN / OVERDUE)
- 인증(토큰 없음·틀림 401), 페이지·스크립트 서빙과 보안 헤더, 세 화면의 상단 링크

시각 조작은 test_settlement.py처럼 거래의 finished_at을 DB에서 어제로 옮긴다 (기본 정산의 마감 = 오늘 00:00 KST).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, update

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.settlement.models import SettlementRun
from clearing_hub.domains.transfers.models import Transfer, TransferStatus
from tests.conftest import ADMIN, send_902_to_901, set_net_debit_cap, transfer_payload

pytestmark = pytest.mark.anyio
KST = ZoneInfo("Asia/Seoul")
WRONG = {"X-Admin-Token": "x" * 40}


async def _send(sender, amount: int) -> str:
    payload = transfer_payload("110-123-0001", amount=amount)
    res = await sender.post("/v1/transfers", payload)
    assert res.json()["status"] == "COMPLETED", res.text
    return payload["transfer_id"]


async def _to_yesterday(*transfer_ids: str) -> None:
    """어제 23:00(KST)에 끝난 것으로 옮긴다 → 기본 정산(어제, 마감 오늘 00:00)에 들어간다."""
    midnight = datetime.combine(datetime.now(KST).date(), datetime.min.time(), tzinfo=KST)
    async with SessionLocal() as db:
        await db.execute(update(Transfer).where(Transfer.transfer_id.in_(transfer_ids))
                         .values(finished_at=(midnight - timedelta(hours=1)).astimezone(UTC)))
        await db.commit()


async def _set_in_flight(transfer_id: str, status: TransferStatus) -> None:
    async with SessionLocal() as db:
        await db.execute(update(Transfer).where(Transfer.transfer_id == transfer_id)
                         .values(status=status, finished_at=None))
        await db.commit()


async def _db_state() -> tuple[list[int | None], int]:
    async with SessionLocal() as db:
        run_ids = list((await db.scalars(select(Transfer.settlement_run_id))).all())
        runs = await db.scalar(select(func.count()).select_from(SettlementRun))
    return run_ids, runs


async def _preview(client) -> dict:
    res = await client.get("/admin/settlements/preview", headers=ADMIN)
    assert res.status_code == 200, res.text
    return res.json()


# --- 정산 미리보기 ---------------------------------------------------------------------------------------------------

async def test_preview_equals_next_draft_and_writes_nothing(client, banks, monkeypatch):
    """어제 끝난 S1 하루치(901→902 5만 2건, 902→901 3만 1건)는 미리보기에 들어가고, 오늘 끝난 2만은 after_cutoff,
    PENDING·UNKNOWN은 in_flight. 미리보기는 DB를 바꾸지 않고, 바로 뒤의 DRAFT와 표가 똑같다."""
    sender, receiver = banks
    yesterday = [await _send(sender, 50_000), await _send(sender, 50_000),
                 (await send_902_to_901(sender, receiver, monkeypatch, 30_000))["transfer_id"]]
    await _to_yesterday(*yesterday)
    await _send(sender, 20_000)  # 오늘 끝남 → 다음 정산
    await _set_in_flight(await _send(sender, 7_000), TransferStatus.PENDING)
    await _set_in_flight(await _send(sender, 3_000), TransferStatus.UNKNOWN)

    preview = await _preview(client)
    assert preview["settle_date"] == str(datetime.now(KST).date() - timedelta(days=1))
    assert (preview["tx_count"], preview["total_amount"]) == (3, 130_000)
    assert [(p["bank_code"], p["net_amount"]) for p in preview["positions"]] == [("901", -70_000), ("902", 70_000)]
    assert preview["after_cutoff"] == {"count": 1, "total": 20_000}
    assert preview["in_flight"] == {"count": 2, "total": 10_000, "by_status": {
        "ACCEPTED": 0, "DELIVERING": 0, "PENDING": 1, "UNKNOWN": 1}}
    assert preview["draft_run_id"] is None

    run_ids, runs = await _db_state()  # 읽기만 했다
    assert set(run_ids) == {None} and runs == 0
    assert await _preview(client) == preview  # 다시 불러도 같다

    res = await client.post("/admin/settlements/run", headers=ADMIN)  # 본문 없음 = 어제
    assert res.status_code == 201, res.text
    draft = res.json()
    for key in ("settle_date", "cutoff_at", "tx_count", "total_amount", "positions", "pairs"):
        assert preview[key] == draft[key], key


async def test_preview_skips_transfers_in_draft(client, banks):
    """DRAFT가 있으면 draft_run_id가 채워지고, 그 run에 들어간 거래는 미리보기에서 빠진다.
    DRAFT 뒤에 들어온(어제 끝난 것으로 옮긴) 거래만 보인다."""
    sender, _ = banks
    first = [await _send(sender, 10_000), await _send(sender, 20_000)]
    await _to_yesterday(*first)
    res = await client.post("/admin/settlements/run", headers=ADMIN)
    assert res.status_code == 201 and res.json()["tx_count"] == 2
    run_id = res.json()["run_id"]

    preview = await _preview(client)
    assert preview["draft_run_id"] == run_id
    assert (preview["tx_count"], preview["total_amount"], preview["positions"], preview["pairs"]) == (0, 0, [], [])

    late = await _send(sender, 5_000)
    await _to_yesterday(late)
    preview = await _preview(client)
    assert (preview["tx_count"], preview["total_amount"]) == (1, 5_000)
    assert preview["draft_run_id"] == run_id and preview["after_cutoff"] == {"count": 0, "total": 0}

    assert (await client.post(f"/admin/settlements/{run_id}/cancel", headers=ADMIN)).status_code == 200
    preview = await _preview(client)  # 취소하면 거래가 미정산으로 돌아와 다시 보인다
    assert preview["draft_run_id"] is None and (preview["tx_count"], preview["total_amount"]) == (3, 35_000)


async def test_preview_empty_hub(client):
    preview = await _preview(client)
    assert (preview["tx_count"], preview["total_amount"], preview["positions"], preview["pairs"]) == (0, 0, [], [])
    assert preview["after_cutoff"] == {"count": 0, "total": 0}
    assert preview["in_flight"]["count"] == 0 and preview["draft_run_id"] is None
    midnight = datetime.combine(datetime.now(KST).date(), datetime.min.time(), tzinfo=KST)
    assert datetime.fromisoformat(preview["cutoff_at"]) == midnight


# --- 예치금 분개·상태 ------------------------------------------------------------------------------------------------

async def _post(client, path: str, body: dict) -> None:
    res = await client.post(path, json=body, headers=ADMIN)
    assert res.status_code == 200, res.text


async def test_all_bank_ledger_newest_first_with_limit(client, banks):
    await _post(client, "/admin/reserve/901/deposit", {"amount": 1_000, "memo": "첫째"})
    await _post(client, "/admin/reserve/902/deposit", {"amount": 2_000, "memo": "둘째"})
    await _post(client, "/admin/reserve/901/withdraw", {"amount": 300, "memo": "셋째"})

    res = await client.get("/admin/reserve/ledger", headers=ADMIN)
    assert res.status_code == 200
    rows = res.json()
    assert [(r["bank_code"], r["kind"], r["amount"], r["balance_after"], r["memo"]) for r in rows] == [
        ("901", "WITHDRAW", -300, 700, "셋째"), ("902", "DEPOSIT", 2_000, 2_000, "둘째"),
        ("901", "DEPOSIT", 1_000, 1_000, "첫째")]
    assert [r["entry_id"] for r in rows] == sorted((r["entry_id"] for r in rows), reverse=True)
    assert set(rows[0]) == {"entry_id", "bank_code", "kind", "amount", "run_id", "balance_after", "memo",
                            "created_by", "created_at"}

    limited = (await client.get("/admin/reserve/ledger?limit=2", headers=ADMIN)).json()
    assert [r["memo"] for r in limited] == ["셋째", "둘째"]
    assert len((await client.get("/admin/reserve/ledger?limit=100000", headers=ADMIN)).json()) == 3  # 상한 500으로 자른다
    assert len((await client.get("/admin/reserve/ledger?limit=0", headers=ADMIN)).json()) == 1  # 최소 1


async def test_admin_reserve_has_status(client, banks):
    """잔액 ≥ 0 OK, −한도 ≤ 잔액 < 0 OVERDRAWN, 잔액 < −한도 OVERDUE. 기존 필드는 그대로."""
    await set_net_debit_cap(client, "901", 1_000)
    await set_net_debit_cap(client, "902", 1_000)
    await _post(client, "/admin/reserve/901/adjust", {"amount": -500, "memo": "시험"})
    await _post(client, "/admin/reserve/902/adjust", {"amount": -1_500, "memo": "시험"})

    listed = {r["bank_code"]: r for r in (await client.get("/admin/reserve", headers=ADMIN)).json()}
    assert {code: r["status"] for code, r in listed.items()} == {"901": "OVERDRAWN", "902": "OVERDUE"}
    assert set(listed["901"]) == {"bank_code", "balance", "net_debit_cap", "exposure", "available", "updated_at", "status"}

    await _post(client, "/admin/reserve/901/deposit", {"amount": 500, "memo": "시험"})
    listed = {r["bank_code"]: r for r in (await client.get("/admin/reserve", headers=ADMIN)).json()}
    assert listed["901"]["status"] == "OK" and listed["901"]["balance"] == 0

    sender, _ = banks  # 은행용 /v1/reserve 스키마는 그대로 (SPEC 영향 없음)
    assert set((await sender.get("/v1/reserve")).json()) == {"bank_code", "balance", "net_debit_cap", "exposure",
                                                              "available"}


# --- 인증·페이지 -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, WRONG])
@pytest.mark.parametrize("path", ["/admin/settlements/preview", "/admin/reserve/ledger", "/admin/reserve",
                                  "/admin/transfers?limit=20"])
async def test_screen_apis_need_token(client, path, headers):
    res = await client.get(path, headers=headers)
    assert res.status_code == 401 and res.json()["error"] == "UNAUTHORIZED"


async def test_ledger_page_is_served_with_security_headers(client):
    page = await client.get("/admin/ui/ledger")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "실시간 장부" in page.text and "/admin/ui/ledger.js" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-frame-options"] == "DENY" and page.headers["x-content-type-options"] == "nosniff"
    assert "<script>" not in page.text and "style=" not in page.text  # 인라인 스크립트·스타일 없음
    assert get_settings().admin_token.get_secret_value() not in page.text

    script = await client.get("/admin/ui/ledger.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert "innerHTML" not in script.text and "sessionStorage" in script.text
    assert "/admin/settlements/preview" in script.text and "/admin/reserve/ledger?limit=20" in script.text
    assert "visibilitychange" in script.text
    assert script.headers["content-security-policy"] == csp


async def test_three_screens_link_to_each_other(client):
    pages = {name: (await client.get(f"/admin/ui/{name}")).text for name in ("settlements", "reserve-requests", "ledger")}
    for name, html in pages.items():
        for other in pages:
            if other != name:
                assert f'href="/admin/ui/{other}"' in html, (name, other)

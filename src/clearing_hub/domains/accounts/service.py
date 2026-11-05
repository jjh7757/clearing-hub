"""본인 명의 계좌 조회 (SPEC 9절): 참가 은행들에 동시에 묻고, 결과를 모아 account_ref를 발급한다."""

import asyncio
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.bank_client import BankClient, BankReply, bank_client
from clearing_hub.core.config import get_settings
from clearing_hub.core.exceptions import ApiException, BadRequest
from clearing_hub.domains.accounts.models import AccountLookup, AccountRef
from clearing_hub.domains.accounts.schemas import BankLookupResult, FoundAccount, LookupRequest, LookupResponse
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.banks.models import Bank

CI_HASH_KEEP = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """SQLite는 시간대를 저장하지 않는다. 허브는 UTC로만 저장하므로 UTC로 본다."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def mask_account(account_num: str) -> str:
    """앞 3자와 끝 4자만 남기고 숫자·문자를 *로 바꾼다. 하이픈은 남긴다. 예: 110-123-0001 → 110-***-0001"""
    if len(account_num) < 8:
        return "*" * max(len(account_num) - 4, 0) + account_num[-4:]
    head, middle, tail = account_num[:3], account_num[3:-4], account_num[-4:]
    return head + "".join(ch if ch == "-" else "*" for ch in middle) + tail


def _classify(reply: BankReply) -> tuple[str, list[dict]]:
    """은행 응답 → (result, 정상 계좌 목록). 형식이 틀린 항목은 버린다."""
    if reply.status is None:
        return "TIMEOUT", []
    if reply.status == 404:
        return "UNSUPPORTED", []
    items = reply.body.get("accounts") if reply.ok else None
    if not isinstance(items, list):
        return "ERROR", []
    accounts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        num, product = item.get("account_num"), item.get("product_name") or ""
        if isinstance(num, str) and 0 < len(num) <= 50 and isinstance(product, str):
            accounts.append({"account_num": num, "product_name": product[:50]})
    return "OK", accounts


async def lookup(db: AsyncSession, requester: Bank, data: LookupRequest, client: BankClient = bank_client) -> LookupResponse:
    settings = get_settings()
    ci_hash = hashlib.sha256(data.ci.encode()).hexdigest()
    since = _now() - CI_HASH_KEEP
    used = await db.scalar(
        select(func.count()).select_from(AccountLookup).where(
            AccountLookup.from_bank == requester.bank_code, AccountLookup.ci_hash == ci_hash, AccountLookup.created_at >= since
        )
    )
    if used >= settings.lookup_daily_limit:
        raise ApiException(429, "TOO_MANY_LOOKUPS", f"같은 고객의 계좌 조회는 24시간에 {settings.lookup_daily_limit}회까지입니다")

    targets = [b for b in await bank_service.list_banks(db) if b.bank_code != requester.bank_code]
    replies = await asyncio.gather(*(client.accounts(bank, data.ci) for bank in targets))

    lookup_id = str(uuid.uuid4())
    expires_at = _now() + timedelta(seconds=settings.account_ref_ttl_seconds)
    found: list[FoundAccount] = []
    refs: list[AccountRef] = []
    results: list[BankLookupResult] = []
    for bank, reply in zip(targets, replies, strict=True):
        result, accounts = _classify(reply)
        results.append(BankLookupResult(bank_code=bank.bank_code, result=result, count=len(accounts) if result == "OK" else None))
        for account in accounts:
            ref = "acr_" + secrets.token_urlsafe(24)
            refs.append(AccountRef(ref=ref, lookup_id=lookup_id, owner_bank=requester.bank_code, bank_code=bank.bank_code,
                                   account_num=account["account_num"], expires_at=expires_at))
            found.append(FoundAccount(bank_code=bank.bank_code, bank_name=bank.name, account_masked=mask_account(account["account_num"]),
                                      product_name=account["product_name"], account_ref=ref))

    db.add(AccountLookup(
        lookup_id=lookup_id, from_bank=requester.bank_code, consent_id=data.consent_id, ci_hash=ci_hash,
        results=json.dumps([r.model_dump(exclude_none=True) for r in results]), account_count=len(found),
    ))
    await db.flush()  # account_refs가 account_lookups를 참조하므로 기록을 먼저 넣는다
    db.add_all(refs)
    await db.commit()
    return LookupResponse(lookup_id=lookup_id, accounts=found, banks=results)


async def resolve_ref(db: AsyncSession, requester: Bank, ref: str, to_bank: str, transfer_id: str) -> str:
    """이체의 to_account_ref → 실제 계좌번호. 다른 은행 것·만료·to_bank 불일치는 ACCOUNT_REF_INVALID (SPEC 9.3)."""
    row = await db.get(AccountRef, ref)
    if row is None or row.owner_bank != requester.bank_code or row.bank_code != to_bank or _aware(row.expires_at) <= _now():
        raise BadRequest("ACCOUNT_REF_INVALID", "계좌 참조값이 만료됐거나 맞지 않습니다. 계좌 조회를 다시 해 주세요", transfer_id)
    return row.account_num


async def purge_expired(db: AsyncSession) -> None:
    """만료된 account_ref를 지우고, 24시간 지난 조회 기록의 ci_hash를 비운다 (SPEC 9.5)."""
    now = _now()
    await db.execute(delete(AccountRef).where(AccountRef.expires_at <= now))
    await db.execute(
        update(AccountLookup).where(AccountLookup.ci_hash.is_not(None), AccountLookup.created_at < now - CI_HASH_KEEP).values(ci_hash=None)
    )
    await db.commit()

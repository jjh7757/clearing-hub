"""가짜 은행(902)의 장부: 표준 라이브러리 sqlite3.

`SIM_DB_PATH`가 있으면 그 파일, 없으면 `:memory:` (재시작하면 사라짐 = 예전 딕셔너리와 같은 동작).

표
  accounts(account_num PK, holder, product_name, balance)   고객 계좌. 시작할 때 SIM_CUSTOMERS로 시드한다
  transactions(id, account_num, direction 'IN'/'OUT', amount, counterpart_*, memo, transfer_id, status, created_at)
    IN  = 허브가 보낸 입금 (`/interbank/receive`). transfer_id가 유일하다 (멱등)
    OUT = 이 은행 고객이 다른 은행으로 보낸 것 (`/sim/api/transfers`). transfer_id가 유일하다.
          PENDING(차감됨) → COMPLETED, 또는 FAILED(같은 트랜잭션에서 환불). fail_code·fail_message는 FAILED 사유

잔액 규칙
  - 받는 계좌가 accounts에 있으면(고객 계좌) 같은 트랜잭션에서 잔액 증가 + IN 기록
  - 고객 계좌가 아니면(끝자리 시나리오·외부 은행 시험 송금) IN 기록만 남고 잔액은 없다

동시성: 연결 하나를 잠금으로 감싸 쓴다 (FastAPI 핸들러는 이벤트 루프에서 돌지만, 테스트·uvicorn 스레드에서도 안전하게).
쓰기는 `BEGIN IMMEDIATE`로 묶는다. 보내기의 차감(`start_send`)·확정/환불(`finish_send`)도 한 트랜잭션씩이다.
확정은 `status = 'PENDING'`일 때만 바꾸는 조건부 UPDATE라, 두 번 불려도 환불은 한 번이다.
"""

import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime

MEMORY = ":memory:"
# 1단계 파일에 없던 칸. 열 때 없으면 붙인다 (CREATE TABLE IF NOT EXISTS는 칸을 더하지 않는다)
ADDED_COLUMNS = {"fail_code": "TEXT", "fail_message": "TEXT", "finished_at": "TEXT"}
INITIAL_BALANCE = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_num  TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    product_name TEXT NOT NULL,
    balance      INTEGER NOT NULL CHECK (balance >= 0)
);
CREATE TABLE IF NOT EXISTS transactions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_num           TEXT NOT NULL,
    direction             TEXT NOT NULL CHECK (direction IN ('IN', 'OUT')),
    amount                INTEGER NOT NULL CHECK (amount > 0),
    counterpart_bank      TEXT NOT NULL DEFAULT '',
    counterpart_bank_name TEXT NOT NULL DEFAULT '',
    counterpart_account   TEXT NOT NULL DEFAULT '',
    counterpart_name      TEXT NOT NULL DEFAULT '',
    memo                  TEXT NOT NULL DEFAULT '',
    transfer_id           TEXT NOT NULL,
    status                TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    fail_code             TEXT,
    fail_message          TEXT,
    finished_at           TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_in_transfer_id ON transactions (transfer_id) WHERE direction = 'IN';
CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_out_transfer_id ON transactions (transfer_id) WHERE direction = 'OUT';
CREATE INDEX IF NOT EXISTS ix_transactions_account ON transactions (account_num, id);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str = MEMORY):
        self.path = path
        self._lock = threading.RLock()
        # isolation_level=None: 자동 커밋. 트랜잭션은 transaction()에서 직접 BEGIN/COMMIT 한다
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(transactions)").fetchall()}
            if have:  # 1단계에서 만든 파일: 새 칸을 먼저 붙인다 (SCHEMA의 인덱스는 칸과 무관)
                for name, kind in ADDED_COLUMNS.items():
                    if name not in have:
                        self._conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {kind}")
            self._conn.executescript(SCHEMA)
        self._seeded: list[dict] = []

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """쓰기 트랜잭션. 안에서 예외가 나면 모두 되돌린다."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # ---- 계좌 ----

    def seed_accounts(self, accounts: list[dict], balance: int = INITIAL_BALANCE) -> None:
        """`{account_num, holder, product_name}` 목록을 넣는다. 이미 있는 계좌는 건드리지 않는다 (잔액을 덮어쓰지 않음)."""
        self._seeded = [dict(a) for a in accounts]
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO accounts (account_num, holder, product_name, balance) VALUES (?, ?, ?, ?)",
                [(a["account_num"], a["holder"], a["product_name"], balance) for a in accounts])

    def account(self, account_num: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM accounts WHERE account_num = ?", (account_num,)).fetchone()
        return dict(row) if row else None

    def customer_accounts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY account_num").fetchall()
        return [dict(r) for r in rows]

    def balance(self, account_num: str) -> int | None:
        acc = self.account(account_num)
        return acc["balance"] if acc else None

    def transactions(self, account_num: str) -> list[dict]:
        """그 계좌의 거래내역, 최근 것부터."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM transactions WHERE account_num = ? ORDER BY id DESC",
                                      (account_num,)).fetchall()
        return [dict(r) for r in rows]

    # ---- 입금 (허브 → 은행) ----

    def receive(self, *, transfer_id: str, to_account: str, amount: int, from_bank: str, from_bank_name: str,
                from_account: str, sender_name: str, memo: str) -> bool:
        """입금 기록. 처음이면 True, 같은 transfer_id가 이미 있으면 아무것도 하지 않고 False (멱등).
        받는 계좌가 고객 계좌면 같은 트랜잭션에서 잔액을 늘린다."""
        with self.transaction() as conn:
            inserted = _insert_transaction(
                conn, account_num=to_account, direction="IN", amount=amount, counterpart_bank=from_bank,
                counterpart_bank_name=from_bank_name, counterpart_account=from_account, counterpart_name=sender_name,
                memo=memo, transfer_id=transfer_id, status="COMPLETED", ignore_duplicate=True)
            if inserted:
                _add_balance(conn, to_account, amount)  # 고객 계좌가 아니면 0행 갱신 = 잔액 없음
        return inserted

    def received(self, transfer_id: str) -> dict | None:
        """IN 기록을 예전 `received[tid]`와 같은 모양으로 (ReceiveRequest 필드 + received_at)."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM transactions WHERE direction = 'IN' AND transfer_id = ?",
                                     (transfer_id,)).fetchone()
        return _received_view(row) if row else None

    def received_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT transfer_id FROM transactions WHERE direction = 'IN' ORDER BY id").fetchall()
        return [r[0] for r in rows]

    def received_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM transactions WHERE direction = 'IN'").fetchone()[0]

    # ---- 보내기 (은행 → 허브) ----

    def start_send(self, *, transfer_id: str, from_account: str, amount: int, to_bank: str, to_bank_name: str,
                   to_account: str, to_name: str, memo: str) -> bool:
        """한 트랜잭션에서 잔액 차감 + OUT(PENDING). 고객 계좌가 아니거나 잔액이 모자라면 아무것도 하지 않고 False."""
        with self.transaction() as conn:
            cur = conn.execute("UPDATE accounts SET balance = balance - ? WHERE account_num = ? AND balance >= ?",
                               (amount, from_account, amount))
            if cur.rowcount != 1:
                return False
            _insert_transaction(conn, account_num=from_account, direction="OUT", amount=amount,
                                counterpart_bank=to_bank, counterpart_bank_name=to_bank_name,
                                counterpart_account=to_account, counterpart_name=to_name, memo=memo,
                                transfer_id=transfer_id, status="PENDING")
        return True

    def finish_send(self, transfer_id: str, status: str, fail_code: str | None = None,
                    fail_message: str | None = None) -> bool:
        """PENDING인 OUT을 COMPLETED 또는 FAILED로. FAILED면 같은 트랜잭션에서 환불한다.
        이미 확정된 거래면 아무것도 하지 않고 False (조건부 UPDATE: 두 번 불려도 환불은 한 번)."""
        if status not in ("COMPLETED", "FAILED"):
            raise ValueError(status)
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE transactions SET status = ?, fail_code = ?, fail_message = ?, finished_at = ?"
                " WHERE direction = 'OUT' AND transfer_id = ? AND status = 'PENDING'",
                (status, fail_code, fail_message, now_iso(), transfer_id))
            if cur.rowcount != 1:
                return False
            if status == "FAILED":
                row = conn.execute("SELECT account_num, amount FROM transactions WHERE direction = 'OUT'"
                                   " AND transfer_id = ?", (transfer_id,)).fetchone()
                _add_balance(conn, row["account_num"], row["amount"])
        return True

    def sent(self, transfer_id: str) -> dict | None:
        """OUT 거래 한 줄."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM transactions WHERE direction = 'OUT' AND transfer_id = ?",
                                     (transfer_id,)).fetchone()
        return dict(row) if row else None

    def pending_sends(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM transactions WHERE direction = 'OUT' AND status = 'PENDING'"
                                      " ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def reset(self) -> None:
        """장부를 처음 상태로: 거래 전부 지우고 고객 계좌를 시드 잔액으로 다시 넣는다. **테스트 전용** (`received.clear()`)."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM transactions")
            conn.execute("DELETE FROM accounts")
        self.seed_accounts(self._seeded)


def _insert_transaction(conn: sqlite3.Connection, *, account_num: str, direction: str, amount: int,
                        transfer_id: str, status: str, counterpart_bank: str = "", counterpart_bank_name: str = "",
                        counterpart_account: str = "", counterpart_name: str = "", memo: str = "",
                        ignore_duplicate: bool = False) -> bool:
    """거래 한 줄. `ignore_duplicate`면 IN transfer_id 중복을 조용히 넘기고 False. 트랜잭션 안에서 부른다."""
    verb = "INSERT OR IGNORE" if ignore_duplicate else "INSERT"
    cur = conn.execute(
        f"{verb} INTO transactions (account_num, direction, amount, counterpart_bank, counterpart_bank_name,"
        " counterpart_account, counterpart_name, memo, transfer_id, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_num, direction, amount, counterpart_bank, counterpart_bank_name, counterpart_account,
         counterpart_name, memo, transfer_id, status, now_iso()))
    return cur.rowcount == 1


def _add_balance(conn: sqlite3.Connection, account_num: str, delta: int) -> bool:
    """고객 계좌 잔액에 delta를 더한다 (음수면 차감, 잔액이 모자라면 CHECK 위반). 고객 계좌가 아니면 False."""
    cur = conn.execute("UPDATE accounts SET balance = balance + ? WHERE account_num = ?", (delta, account_num))
    return cur.rowcount == 1


def _received_view(row: sqlite3.Row) -> dict:
    return {"transfer_id": row["transfer_id"], "from_bank": row["counterpart_bank"],
            "from_bank_name": row["counterpart_bank_name"], "from_account": row["counterpart_account"],
            "to_account": row["account_num"], "amount": row["amount"], "sender_name": row["counterpart_name"],
            "memo": row["memo"], "received_at": row["created_at"]}


class ReceivedView(Mapping):
    """예전 `received` 딕셔너리(transfer_id → 입금 기록)의 읽기 겉면. 테스트가 `in`·`len`·`[tid]`·`== {}`·`.clear()`를 쓴다.
    `clear()`는 장부 전체를 처음 상태로 되돌린다(`Ledger.reset`). 테스트만 부른다."""

    def __init__(self, ledger: Ledger):
        self._ledger = ledger

    def __getitem__(self, transfer_id: str) -> dict:
        record = self._ledger.received(transfer_id)
        if record is None:
            raise KeyError(transfer_id)
        return record

    def __iter__(self):
        return iter(self._ledger.received_ids())

    def __len__(self) -> int:
        return self._ledger.received_count()

    def __contains__(self, transfer_id: object) -> bool:
        return isinstance(transfer_id, str) and self._ledger.received(transfer_id) is not None

    def clear(self) -> None:
        self._ledger.reset()

    def __repr__(self) -> str:
        return f"ReceivedView({dict(self)!r})"


def open_ledger(environ: Mapping[str, str]) -> Ledger:
    """`SIM_DB_PATH`가 있으면 그 파일, 없거나 비어 있으면 메모리."""
    return Ledger(environ.get("SIM_DB_PATH") or MEMORY)

# ClearingHub 정산 구현 계획서 — 상계·대사·예치금 결제

- 작성일: 2026-11-06
- 상태: **1~7단계 완료** (2026-11-06), 8단계(부인방지, 12절) 8a~8d 완료, 9단계(자동 확정·셀프 시험·알림, 13절) 완료, 10단계(연동 진단기, 14절) 완료. [PROJECT_PLAN.md](PROJECT_PLAN.md) 3.3절("정산은 표시만")과 [SPEC.md](SPEC.md) 8절 8번("정산 시각·방식, 미구현")을 이 문서로 대체한다
- 범위: 실제 금융망의 이체 13단계 가운데 허브에 아직 없는 **8~13단계**(마감 상계 → 대사 → 중앙은행 정산 → 은행 장부 정리)를 ClearingHub와 AvengersBank에 구현한다(1~7단계). 이어서 **부인방지**(공개키 요청·응답 서명, 12절)를 8단계로 더한다
- 전제: 서버는 하나(ClearingHub)지만 **장부는 둘**로 분리한다. 거래 기록(`transfers`)과 예치금 원장(`reserve_*`)은 서로 다른 도메인이 관리하고, 둘 사이는 "정산 지시서"로만 이어진다. 나중에 예치금 원장을 별도 서버(중앙은행 역할)로 떼어 내도 허브 쪽 코드는 그대로 두는 것이 목표다

---

## 1. 왜 하는가

지금 허브는 금융결제원의 **중계** 역할(전문 전달·기록·재시도)까지만 구현돼 있다. 그래서 다음 문제가 남아 있다.

| 문제 | 지금 | 이 계획 뒤 |
|---|---|---|
| 받는 은행이 준 외상이 영원히 갚아지지 않는다 | 입금은 즉시, 은행 간 돈은 움직이지 않음 | 매일 차액을 **예치금 원장**에서 옮긴다 (11~12단계) |
| 보내는 은행이 출금 없이 보내도 허브가 모른다 | 막을 방법 없음 | 보낸 만큼 그 은행 예치금이 줄어든다. 예치금+한도를 넘으면 접수 거절 (순채무한도) |
| 은행이 자기 장부를 고쳐도 드러나지 않는다 | 비교할 자료가 없음 | 허브가 은행마다 **대사 파일**을 주고, 은행이 자기 장부와 맞춰 본다 (10단계) |
| 은행이 파산하거나 정산 때 돈을 못 내면 | 정해진 것 없음 | 한도만큼은 마이너스를 허용하고, 넘으면 그 은행의 송금을 자동으로 막는다 |

한 줄로: **"줬다"는 말이 아니라 예치금 원장의 숫자가 이체의 증거가 되게 한다.**

---

## 2. 설계 결정

| # | 항목 | 결정 | 이유 |
|---|---|---|---|
| 1 | 중계와 정산의 위치 | 한 서버, 다른 도메인(`settlement`, `reserve`). `reserve`는 `transfers`를 직접 읽지 않고 정산 지시서만 받는다 | 부트캠프 규모. 분리 배포 가능성은 인터페이스로 남긴다 |
| 2 | 정산 단위 | **다자간 차액**. 은행마다 `받을 돈 − 줄 돈` 하나를 계산한다 (합은 항상 0). 은행 쌍별 표는 화면용으로만 따로 만든다 | 실제 소액결제망 방식. 예치금 이동 횟수가 은행 수만큼으로 줄어든다 |
| 3 | 정산일과 마감 | 정산일은 KST 날짜. 배치는 매일 **00:05 KST**에 어제 정산일로 돌고, 마감 시각 `cutoff_at`은 정산일 **다음 날 00:00 KST**. 관리자는 **오늘** 날짜로 돌려 지금 즉시 마감할 수 있다(`cutoff_at = now`, 시연용). 마감은 미래가 될 수 없다(`min(다음 날 00:00, now)`). 대상은 `status = COMPLETED`이고 **아직 정산되지 않은** 거래 전부 (`settlement_run_id IS NULL AND finished_at < cutoff_at`). 한 날짜에 run이 여러 개일 수 있다 | 날짜 범위가 아니라 "미정산" 기준이라, `UNKNOWN`이었다가 며칠 뒤 `COMPLETED`로 바뀐 거래도, 즉시 마감 뒤에 들어온 거래도 다음 run에 들어가고 빠지지 않는다 |
| 4 | 정산 흐름 | `DRAFT`(계산만) → `SETTLED`(예치금 반영). 관리자가 확정하거나 설정으로 자동 확정. **DRAFT는 동시에 하나만**: 남아 있으면 새 run은 409 `SETTLEMENT_DRAFT_EXISTS`. DRAFT는 취소(`CANCELLED`)할 수 있고, 그 거래는 미정산으로 돌아간다. `SETTLED`는 취소 불가 | 대사 뒤 확정이 실제 절차. 시연에서는 자동 확정으로 바로 보여 줄 수 있다. DRAFT가 둘이면 어느 쪽을 확정할지 헷갈리고 대사 파일도 겹친다 |
| 5 | 예치금(당좌예금) | 은행마다 `reserve_accounts` 한 행. 입금·출금은 관리자 API로만(실제 돈 이동은 범위 밖). 모든 변동은 `reserve_ledger`에 분개로 남긴다 | 한국은행 당좌계정 역할. 잔액 컬럼은 정산·입출금 때만 바뀌므로 행 잠금으로 충분하다 |
| 6 | 순채무한도 | `banks.net_debit_cap`(원, 기본 0). 접수 때 `미정산 순채무 + amount ≤ 예치금 + 한도`가 아니면 `LIMIT_EXCEEDED`(400, `status: FAILED`) | 담보·일중당좌대출 역할. 새 오류 코드를 만들지 않고 SPEC 5절의 `LIMIT_EXCEEDED`를 쓴다 |
| 7 | 정산 때 예치금 부족 | 한도 안이면 마이너스를 허용하고 포지션을 `OVERDRAWN`으로 표시. 넘으면 그래도 반영하되 `OVERDUE`로 표시한다. 그 은행은 6번 검사에 걸려 **송금이 자동으로 막히고**, 받기만 된다 | 손실분담·담보처분은 범위 밖. 관리자가 예치금을 넣으면 풀린다 |
| 8 | 정산된 거래는 바꾸지 않는다 | `transfers.settlement_run_id`가 채워진 거래는 상태 변경 금지. 정정은 `reserve_ledger`의 `ADJUST` 분개로만 | 결제완결성. 지금도 `UNKNOWN`만 관리자가 바꿀 수 있고 `UNKNOWN`은 정산 대상이 아니라 충돌이 없다 |
| 9 | 대사 파일 | 은행이 `GET /v1/settlements`(목록)에서 run을 찾아 `GET /v1/settlements/{run_id}`로 **자기 몫만** 받는다 (포지션 + 거래 목록). 허브가 은행을 부르지 않는다 | 은행 쪽 API를 늘리지 않는다 (SPEC 4절 변경 없음). 가짜 은행 `902`도 그대로 |
| 10 | 은행 장부 정리(13단계) | AvengersBank가 매일 대사 파일을 받아 `INTERBANK_SETTLEMENT` 계정과 비교하고, 맞으면 `HUB_RESERVE` 계정으로 옮긴다 | 가짜 은행은 장부가 없으므로 AvengersBank만 구현 |

---

## 3. 13단계 → 구현 대응표

| 단계 | 실제 금융망 | 구현 위치 | 상태 |
|---|---|---|---|
| 1~7 | 수취조회·출금·입금 의뢰·입금·기록 | `transfers`, `inquiry`, `bank_client`, `workers/retry.py` | 있음 |
| 8 | 하루치 거래 수집 | `settlement/service.py: create_draft()` — 미정산 `COMPLETED` 거래를 UPDATE 한 문장으로 모아 `settlement_run_id`를 찍는다 | 완료 (3단계) |
| 9 | 다자간 차액 계산 | `settlement/service.py: create_draft()` → `settlement_positions`, `settlement_pairs`, 검증 Σ net = 0 | 완료 (3단계) |
| 10 | 대사 파일 전달 | `settlement/service.py: list_for_bank()`·`bank_view()` → `GET /v1/settlements`, `GET /v1/settlements/{run_id}` (은행용, 자기 몫만), `GET /admin/settlements/{run_id}/transfers.csv` (관리자용) | 완료 (5단계) |
| 11 | 결제 지시 | `settlement/service.py: confirm()` → `reserve.service.apply_instruction(run_id, instruction_for(run))`, 일일 배치 `workers/settlement.py` | 완료 (4단계) |
| 12 | 중앙은행 원장 반영 | `reserve/service.py: apply_instruction()` — `reserve_accounts.balance` 갱신 + `reserve_ledger` 분개, 결과를 `settlement_positions.result`·`balance_after`에 (한 트랜잭션) | 완료 (4단계) |
| 13 | 은행 장부 정리 | AvengersBank `interbank/settlement_sync.py: sync_settlements()`·`reconcile_run()` — run 단위 대사 → `SETTLE` 분개(정산 계정 → `HUB_RESERVE`), 워커 `settlement_sync_loop()` | 완료 (6단계) |
| (사전) | 당좌예금 예치, 순채무한도 | `reserve` 도메인, `banks.net_debit_cap`, `transfers.service.accept_and_deliver()` 검사(`transfers/exposure.py`) | 완료 (1·2단계) |
| (신뢰) | 전자서명·부인방지 | 허브 `core/signing.py` Ed25519, 응답 서명(`core/response_signing.py`), `transfer_evidence` / AvengersBank `interbank/signing.py`·`response_signing.py`·`hub_evidence`·`keys.py` (12절, 8단계) | 완료 (8단계: 8a 허브·가짜 은행, 8b AvengersBank, 8c Ed25519 필수화·셀프 등록, 8d AvengersBank Ed25519 전용) |

---

## 4. 데이터 모델

### 4.1 허브 — 새 테이블

```
settlement_runs            정산 1회 (한 날짜에 여러 개 가능)
  run_id          PK (int)
  settle_date     date, INDEX               # KST 기준 날짜. 유니크가 아니다 (즉시 마감을 하루에 여러 번)
  cutoff_at       timestamptz               # 이 시각 전에 COMPLETED 된 미정산 거래까지
  status          DRAFT | SETTLED | CANCELLED
                  부분 유니크 인덱스 (status) WHERE status = 'DRAFT'   # DRAFT는 동시에 하나만
  tx_count, total_amount
  created_by ("admin" / "auto"), created_at, settled_at, settled_by (관리자 / "auto")

settlement_positions       은행별 순 포지션 (9단계 결과, 11단계 지시서)
  run_id FK, bank_code FK  → PK (run_id, bank_code)
  sent_count, sent_total   # 이 은행이 보낸 것 (줄 돈)
  recv_count, recv_total   # 이 은행이 받은 것 (받을 돈)
  net_amount               # recv_total − sent_total. 양수면 받음, 음수면 냄
  result                   NULL | OK | OVERDRAWN | OVERDUE   (12단계 뒤 채움)
  balance_after            # 반영 뒤 예치금 잔액

settlement_pairs           은행 쌍별 표 (화면·CSV용. 계산에는 안 쓴다)
  run_id FK, bank_a, bank_b (bank_a < bank_b) → PK
  a_to_b_count, a_to_b_total, b_to_a_count, b_to_a_total, net_amount, net_direction

reserve_accounts           은행별 예치금 (당좌예금)
  bank_code PK FK
  balance        bigint, 기본 0  (마이너스 가능, 2절 7번)
  updated_at

reserve_ledger             예치금 분개 (모든 변동의 근거)
  entry_id PK
  bank_code FK
  kind           DEPOSIT | WITHDRAW | SETTLE_CREDIT | SETTLE_DEBIT | ADJUST
  amount         잔액 증감 (SETTLE_DEBIT·WITHDRAW는 음수)
  run_id         FK nullable (SETTLE_* 만)
  memo, created_by, created_at
  UNIQUE (run_id, bank_code, kind)         # 같은 정산을 두 번 반영하지 못하게
```

### 4.2 허브 — 기존 테이블 변경

| 테이블 | 변경 |
|---|---|
| `transfers` | `settlement_run_id` (FK nullable). 채워지면 정산에 들어간 것 (DRAFT 취소 때 NULL로 돌아감). 인덱스 `(from_bank, settlement_run_id)`, `(to_bank, settlement_run_id)` — 순채무 계산·수집용 |
| `reserve_ledger` | `run_id`에 FK `settlement_runs.run_id` (3단계 마이그레이션) |
| `banks` | `net_debit_cap` (bigint, 기본 0) |

### 4.3 AvengersBank — 변경 (6단계 완료, 마이그레이션 `b9c0d1e2f3a4`)

한 날짜에 run이 여럿일 수 있으므로(SPEC 10.3) 처음 계획의 `settle_date` 대신 **`run_id`를 키**로 쓴다.

| 항목 | 변경 |
|---|---|
| `LedgerAccount` | `HUB_RESERVE = "HUB_RESERVE"` 추가: 우리가 허브에 맡긴 모의 예치금. 은행별이 아니라 하나이고 `bank_code`는 허브 `"000"` |
| `LedgerEvent` | `SETTLE = "SETTLE"` 추가: 정산 계정 → 예치금 계정. `transfer_id`는 `"settle:{run_id}:{은행}"` |
| `interbank_ledger` | `hub_run_id` (int nullable, index) 추가: 이 줄이 맞춰진 허브 run. 맞춘 `COMPLETE`·`RECEIVE`의 정산 계정 줄은 장부 정리 때 찍고, `SETTLE` 줄은 처음부터 찍는다. NULL인 `COMPLETE`·`RECEIVE` 줄이 다음 대사의 후보 |
| 새 테이블 `settlement_reconciliations` | `hub_run_id` PK, `settle_date`, `hub_status`(마지막으로 본 `DRAFT`/`SETTLED`), `hub_net_amount`(허브 `position.net_amount`), `our_net_amount`(일치한 거래로 계산한 같은 값), `result` (`MATCHED` / `CHECKING` / `MISMATCH`), `diff_json`(`hub_only`·`differs`·`checking`·`ours_only`·`ours_only_stale`·`net`, 빈 항목은 뺌), `checked_at`, `settled_at`(SETTLE 분개를 남긴 시각, nullable). 같은 run을 다시 보면 행을 갱신한다 |

허브 응답 없음·401은 행을 남기지 않는다 (처음 계획의 `HUB_UNAVAILABLE` 대신 로그만 남기고 다음 주기에 다시). 취소된 run은 행을 남기지 않고, DRAFT 때 남긴 행은 지운다.

### 4.4 순채무(exposure) 계산

접수 시점에 보내는 은행 `B`에 대해:

```
unsettled = settlement_run_id IS NULL
            OR settlement_run_id IN (SELECT run_id FROM settlement_runs WHERE status <> 'SETTLED')
sent_open = Σ amount  where from_bank = B and unsettled
                        and status in (ACCEPTED, DELIVERING, PENDING, UNKNOWN, COMPLETED)
recv_open = Σ amount  where to_bank = B   and unsettled and status = COMPLETED
exposure  = sent_open − recv_open
허용      = exposure + amount ≤ reserve_accounts.balance + banks.net_debit_cap
```

- 진행 중(`PENDING`·`UNKNOWN`) 거래도 "나갈 수 있는 돈"이므로 `sent_open`에 넣는다. `FAILED`는 뺀다.
- 받는 쪽은 `COMPLETED`만 센다. 아직 안 들어온 돈으로 보내게 하지 않기 위해서다.
- **DRAFT에 들어간 거래도 센다** (3단계에서 정함). `settlement_run_id IS NULL`만 보면 DRAFT를 만든 순간 순채무가 0이 되는데
  예치금은 확정 때 바뀌므로, 확정 전까지 같은 한도로 한 번 더 보낼 수 있게 된다. 확정은 run을 `SETTLED`로 바꾸고 예치금을
  같은 트랜잭션에서 옮기므로, 순채무가 빠지는 순간과 잔액이 바뀌는 순간이 같다. 정산 전후로 `available`은 변하지 않는다.
- 두 합계 쿼리는 인덱스를 타고, 접수마다 한 번 돈다. 거래가 많아지면 `reserve_accounts`에 캐시 컬럼을 두는 것으로 바꾼다 (지금은 안 한다).

---

## 5. 처리 흐름

### 5.1 정산 배치 (8~12단계)

일일 배치 `workers/settlement.py: settlement_loop()`(`SETTLEMENT_WORKER_ENABLED`)는 `next_cutoff()`까지 잠들었다가 `run_daily()`를 돈다.
시작할 때 오늘(KST) 만든 run이 없고 배치 시각이 지났으면(허브가 꺼져 있었음, 10절) 바로 한 번 돈다(`needs_catch_up()`).
확정하지 않은 DRAFT가 남아 있으면 그날은 경고만 남기고 건너뛴다(거래는 미정산으로 남아 다음 run에 들어간다). 거래 0건 run은 대사할 것이 없으므로 자동 확정 설정과 상관없이 바로 확정한다.

```
workers/settlement.py  매일 00:05 KST, 어제 정산일 (또는 POST /admin/settlements/run {settle_date?})
 │
 ├ 8.  create_draft(settle_date)   [트랜잭션 1]  (3단계 완료)
 │      DRAFT가 남아 있으면 409 SETTLEMENT_DRAFT_EXISTS (확정하거나 취소한 뒤 돌린다)
 │      settlement_runs 생성 (DRAFT, cutoff_at = min(정산일 다음 날 00:00 KST, now))
 │        - settle_date 생략 → 어제, 오늘 → now(즉시 마감), 내일 이후 → 422
 │      UPDATE transfers SET settlement_run_id = run WHERE status='COMPLETED'
 │             AND settlement_run_id IS NULL AND finished_at < cutoff_at
 │
 ├ 9.  (같은 함수, 같은 트랜잭션)
 │      그 run의 거래를 (from_bank, to_bank)로 GROUP BY 한 합계 하나에서
 │      은행별 sent/recv → settlement_positions (거래에 나타난 은행만)
 │      은행 쌍별        → settlement_pairs
 │      검증: Σ net_amount == 0, 건수·금액이 수집한 것과 같은지. 아니면 RuntimeError → 롤백 (계산 버그 방어)
 │      거래가 0건이어도 run은 만든다 (tx_count 0)
 │
 │      (취소) POST /admin/settlements/{run_id}/cancel: DRAFT만. 거래 settlement_run_id → NULL, run → CANCELLED
 │
 ├ 10. (대사 기간) 은행이 GET /v1/settlements/{run_id}로 자기 몫을 받아 맞춰 본다
 │      SETTLEMENT_AUTO_CONFIRM=true 면 이 단계를 건너뛰고 바로 11로 (워커 run_daily가 이어서 confirm)
 │
 ├ 11. settlement.service.confirm(run_id, confirmed_by)   [트랜잭션 2]  (4단계 완료)
 │      _lock_run: run 행 SELECT ... FOR UPDATE, DRAFT가 아니면 409 SETTLEMENT_NOT_DRAFT (두 번 확정·취소와 경쟁 방지)
 │      instruction_for(run) = [(bank_code, net_amount) ...]  ← positions에서, 은행 코드순
 │      reserve.service.apply_instruction(db, run_id, instruction)   ← reserve 도메인은 이것만 받는다
 │      (이미 분개가 있으면 409 RESERVE_ALREADY_APPLIED 그대로 전파 → 롤백, run은 DRAFT로 남는다)
 │
 └ 12. reserve.service.apply_instruction   [같은 트랜잭션]  (4단계 완료)
        은행마다: reserve_accounts 행 잠금 → balance += net_amount
                  reserve_ledger에 SETTLE_CREDIT / SETTLE_DEBIT 분개 (created_by "settlement")
                  PositionResult.status = OK / OVERDRAWN(balance < 0) / OVERDUE(balance < −cap)
        confirm이 결과를 positions.result·balance_after에 옮겨 적고
        run.status = SETTLED, settled_at, settled_by (관리자 "admin" / 워커 "auto"). 사유는 로그에만
        OVERDUE 은행이 있으면 logger.error (은행·잔액·한도, 관리자 알림)
        SETTLED와 잔액이 한 커밋에 바뀌므로 순채무(4.4)에서 빠지는 금액과 잔액에 들어가는 금액이 동시에 바뀐다
```

`reserve.apply_instruction()`의 입력은 `(run_id, [(bank_code, amount)])`뿐이다. 지시서는 `settlement.service.instruction_for(run)`이 포지션에서 만든다(은행 코드순). 이 함수를 HTTP 호출로 바꾸면 예치금 원장을 별도 서버로 뗄 수 있다 (2절 1번).

### 5.2 접수 때 순채무한도 검사

`transfers.service.accept_and_deliver()`에서 `LIMIT_EXCEEDED`(1회 상한) 검사 바로 뒤에 4.4의 검사를 넣는다. 거절 메시지: `"순채무한도를 넘었습니다 (예치금 X원 + 한도 Y원, 미정산 순채무 Z원)"`. 접수 전 거절이므로 SPEC 3.3의 규칙대로 `status: "FAILED"`가 붙고, 보내는 은행은 출금을 되돌린다.

### 5.3 은행 쪽 대사·장부 정리 (13단계, AvengersBank)

```
interbank/worker.py: settlement_sync_loop()   시작 60초 뒤 한 번(꺼져 있던 동안 놓친 run), 그 뒤 1시간마다
                                               (INTERBANK_SETTLEMENT_SYNC_STARTUP_DELAY_SECONDS / _INTERVAL_SECONDS)
interbank/settlement_sync.py: sync_settlements()
 1. GET /v1/settlements  (서명, SPEC 2절) → 끝나지 않은 run을 run_id 오름차순으로
      끝나지 않음 = 대사 행이 없음 / result ≠ MATCHED / MATCHED인데 settled_at 없음 (DRAFT 때 맞춰 봄)
      목록에서 빠진 끝나지 않은 run(취소됐거나 30일 지남)도 상세로 다시 본다
    - 응답 없음·401 등 → 로그만, 행 없음, 다음 주기에 다시
    - 상세 404 → 그 run만 건너뜀 / 상세 응답 없음 → 이번 주기 중단
    GET /v1/settlements/{run_id}  (한 날짜에 run이 여럿일 수 있으므로 날짜가 아니라 run 단위로 대사한다)
 2. reconcile_run(db, detail)   [한 트랜잭션, 대사 행 FOR UPDATE]
    - CANCELLED → 아무것도 안 함. DRAFT 때 남긴 행은 지운다 (그 거래는 다음 run에 다시 나온다)
    - 우리 쪽 후보: interbank_ledger에서 account = INTERBANK_SETTLEMENT, event ∈ {COMPLETE, RECEIVE},
      hub_run_id IS NULL (다시 볼 때를 위해 = 이 run_id 도). COMPLETE(+) → SENT, RECEIVE(−) → RECEIVED
      transfer_id → (방향, |amount|, 상대 은행, created_at). 날짜로 자르지 않는다
 3. 비교 (허브 transfers 각각을 transfer_id로)
    - 방향·금액·상대 은행이 같음 → 일치
    - 후보에 없는데 우리 interbank_transfers에서 PENDING(허브 SENT) → 확인 중 (checking)
    - 후보에 없음 → hub_only (다른 run에서 이미 맞춘 거래면 settled_in) / 내용이 다름 → differs
    - 우리 후보에만 있음 → 불일치가 아니다 (ours_only, 다음 run으로). 분개 created_at < cutoff_at − 1시간이면
      ours_only_stale + logger.warning (SPEC 10.6 "내 쪽에만 있는 거래")
    - our_net_amount = Σ 일치 RECEIVED − Σ 일치 SENT
    - result: hub_only·differs가 있으면 MISMATCH (logger.error, 자동 처리 중단)
              그다음 checking이 있으면 CHECKING
              그다음 our_net ≠ position.net_amount면 MISMATCH (diff의 net)
              아니면 MATCHED
    - CHECKING이면 sync_settlements가 confirm_pending()으로 허브에 결과를 물어(SPEC 3.4) 마무리한 뒤 한 번 더 비교
 4. MATCHED이고 허브 SETTLED이고 settled_at이 없으면 [같은 트랜잭션]
    맞춘 COMPLETE·RECEIVE 줄에 hub_run_id를 찍는다 (찍힌 수가 맞춘 수와 다르면 RuntimeError → 롤백)
    상대 은행마다 n = Σ SENT − Σ RECEIVED (그 은행에 줄 돈, 받을 돈이면 음수, 0이면 분개 없음):
      _post("settle:{run_id}:{bank}", SETTLE, bank, (INTERBANK_SETTLEMENT, −n), (HUB_RESERVE, −n), hub_run_id=run_id)
    → HUB_RESERVE 변화 = Σ(−n) = position.net_amount. 유니크 (transfer_id, event, account)로 두 번 기록 못 한다
    settled_at 기록. DRAFT면 행만 남기고 다음 주기에 SETTLED가 되면 4를 한다
 5. settlement_reconciliations에 결과 저장 (같은 run은 갱신)
```

`interbank/service.py: reconcile()`(30초마다 내부 계정 대사)도 SETTLE을 안다: 은행별 정산 계정 기대값 = 완료 보낸 합 − 받은 합 + 그 은행 SETTLE 정산 계정 줄 합, `HUB_RESERVE` 기대값 = `settled_at`이 있는 대사 행의 `hub_net_amount` 합, 그리고 은행마다 `hub_run_id`가 찍힌 정산 계정 줄의 합 = 0.

MISMATCH가 바로 이 계획의 목적이다. 받는 은행이 입금 없이 `COMPLETED`라고 답했거나(허브에만 있음), 보내는 은행이 출금 없이 보냈거나(우리 장부에 DEBIT 없음) 하면 여기서 드러난다. 운영자가 할 일은 AvengersBank `docs/INTERBANK.md`의 "정산 대사와 장부 정리".

---

## 6. API

### 6.1 은행용 (SPEC에 추가, v0.6)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/settlements` | `{"settlements": [...]}`. 최근 30일(`settle_date ≥ 오늘(tz) − 30일`) run, 최신순(`run_id` 내림차순), 최대 100개. 항목: `run_id`, `settle_date`, `cutoff_at`, `status`, `settled_at`, 내 `net_amount`(포지션이 없으면 0), 내 `result`(없으면 null). 한 날짜에 run이 여럿일 수 있다. **`CANCELLED`는 뺀다** (전에 본 DRAFT가 취소되면 목록에서 사라지고 상세에서 CANCELLED로 보인다) |
| GET | `/v1/settlements/{run_id}` | **대사 파일.** `run_id`, `settle_date`, `cutoff_at`, `status`, `settled_at`, `position {sent_count, sent_total, recv_count, recv_total, net_amount, result, balance_after}`(내 행이 없으면 0·null), `transfers [{transfer_id, direction: SENT/RECEIVED, counterparty_bank, amount, finished_at}]`(내가 보내거나 받은 것만, `finished_at` 순. 계좌번호·이름 없음). CANCELLED run은 200, `transfers: []`, 포지션은 기록된 값. 없으면 404 `SETTLEMENT_NOT_FOUND` |
| GET | `/v1/reserve` | 내 예치금: `bank_code`, `balance`, `net_debit_cap`, `exposure`(4.4), `available = balance + cap − exposure` |

모두 2절 인증(SPEC 2절 서명, 쿼리스트링 없음), 자기 은행 것만 본다. 다른 은행의 포지션·잔액은 보여 주지 않는다. 규격은 SPEC 10절.

### 6.2 관리자용

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/admin/settlements/run` | `{settle_date?}` 정산 DRAFT 생성 (8·9단계) → 201 run 요약 + 포지션 + 쌍별 표. 생략하면 어제, 오늘이면 즉시 마감, 내일 이후 422 `VALIDATION_ERROR`. DRAFT가 남아 있으면 409 `SETTLEMENT_DRAFT_EXISTS`(`run_id`) |
| GET | `/admin/settlements` | 목록, 최신순 (`limit`, 기본 30) |
| GET | `/admin/settlements/{run_id}` | 포지션 전체(은행 코드순) + 쌍별 표. 없으면 404 `SETTLEMENT_NOT_FOUND` |
| GET | `/admin/settlements/{run_id}/transfers` | 거래 목록 JSON (`transfer_id`, `from_bank`, `to_bank`, `amount`, `finished_at`, `holder_name`) |
| GET | `/admin/settlements/{run_id}/transfers.csv` | 같은 내용 CSV (UTF-8, BOM 없음, 헤더 행, `settlement_{run_id}.csv`) |
| POST | `/admin/settlements/{run_id}/cancel` | DRAFT 취소: 거래를 미정산으로 되돌림. DRAFT가 아니면 409 `SETTLEMENT_NOT_DRAFT` |
| POST | `/admin/settlements/{run_id}/confirm` | 확정 (11·12단계). 본문 `{reason?}`(200자까지, 로그에만) → 200 run 상세: `status: SETTLED`, `settled_at`, `settled_by: "admin"`, 포지션마다 `result`(OK / OVERDRAWN / OVERDUE)·`balance_after`. DRAFT가 아니면 409 `SETTLEMENT_NOT_DRAFT`(`run_id`), 없으면 404. 이미 분개가 있으면 409 `RESERVE_ALREADY_APPLIED`(run은 DRAFT로 남음) |
| GET | `/admin/reserve` | 은행별 예치금·한도·exposure |
| POST | `/admin/reserve/{bank_code}/deposit` | `{amount, memo}` 예치 (DEPOSIT 분개) |
| POST | `/admin/reserve/{bank_code}/withdraw` | `{amount, memo}` 인출. `balance − amount < −cap` 이면 거절 |
| POST | `/admin/reserve/{bank_code}/adjust` | `{amount, memo}` 정정 분개 (양수·음수). 사유 필수 |
| GET | `/admin/reserve/{bank_code}/ledger` | 분개 목록 |
| PATCH | `/admin/banks/{bank_code}` | 기존 API에 `net_debit_cap` 필드 추가 |

### 6.3 SPEC 변경 (v0.6) — 반영됨 (v0.6, SPEC 10절)

- 1절 상한 설명에 순채무한도 한 줄 추가 (`LIMIT_EXCEEDED`, `message`로 구분)
- 새 절 **10. 정산**: 2절의 결정 3·4·6·7, 6.1의 API, 대사 파일 형식, 은행이 할 일(매일 대사, 맞지 않으면 허브 관리자에게 알림)
- 5절 표에 `SETTLEMENT_NOT_FOUND`(404) 추가. `SETTLEMENT_DRAFT_EXISTS`·`SETTLEMENT_NOT_DRAFT`(409)는 관리자용이라 SPEC에는 넣지 않는다
- 8절 8번을 "확정"으로 바꾸고 이 문서를 가리킨다
- 4절(허브 → 은행)은 **바꾸지 않는다**. 이미 연동한 은행은 아무것도 고치지 않아도 이체가 그대로 된다. 단 예치금이 0이고 한도가 0이면 송금이 막히므로, 배포 전에 관리자가 참가 은행마다 예치금 또는 한도를 넣어 둔다 (7절 배포 순서)

---

## 7. 작업 단계와 일정

1인 기준 약 **10일**. 각 단계 끝에 테스트가 통과해야 다음으로 간다.

| 단계 | 소요 | 산출물 | 완료 기준 |
|---|---|---|---|
| 0. 규격·설정 **완료** | 0.5일 | SPEC v0.6 초안(6.3), `config.py`에 `settlement_cutoff_time`(기본 `00:05`), `settlement_timezone`(`Asia/Seoul`), `settlement_auto_confirm`(기본 `false`) | 팀 합의 |
| 1. 예치금 원장 **완료** | 2일 | `domains/reserve/` (models·schemas·service·router), Alembic `reserve_accounts`·`reserve_ledger`·`banks.net_debit_cap`, 관리자 입출금·정정 API, `GET /v1/reserve` | 입금·인출·정정이 분개와 잔액에 같이 남고, 인출 한도 검사가 된다 |
| 2. 순채무한도 검사 **완료** | 1일 | `transfers/service.py` 4.4 검사, 은행 등록 시 `reserve_accounts` 행 자동 생성 | 예치금 0·한도 0인 은행의 이체가 `LIMIT_EXCEEDED FAILED`로 거절되고, 예치금을 넣으면 된다. 기존 테스트는 conftest에서 한도를 넉넉히 줘 그대로 통과 |
| 3. 정산 배치 (DRAFT) **완료** | 2일 | `domains/settlement/` models·service(`create_draft`·`cancel_draft`·`instruction_for`), Alembic `settlement_*`·`transfers.settlement_run_id`·`reserve_ledger.run_id` FK, `POST /admin/settlements/run`, 취소, 조회 API, CSV, `exposure`의 미정산 조건, `_set_status` 방어 | 손으로 계산한 하루치 상계와 같다. `Σ net = 0`. DRAFT가 있는데 또 돌리면 409, 취소하면 다시 돌릴 수 있다. 늦게 COMPLETED된 거래가 다음 정산에 들어간다 |
| 4. 확정·예치금 반영 **완료** | 1.5일 | `confirm()` → `reserve.apply_instruction()`, `workers/settlement.py`(매일 자동 DRAFT, 설정 시 자동 확정, 시작 때 catch-up, 0건 run 자동 확정, `SETTLEMENT_WORKER_ENABLED`), `OVERDRAWN`·`OVERDUE` 처리, `POST /admin/settlements/{run_id}/confirm` | 확정 뒤 잔액이 포지션만큼 바뀌고 분개가 남는다. 두 번 확정 불가. `OVERDUE` 은행은 송금이 막히고 받기는 된다 |
| 5. 은행용 대사 API **완료** | 1일 | `GET /v1/settlements`, `/v1/settlements/{run_id}` (`service.list_for_bank`·`bank_view`), SPEC v0.6 10절 | 은행이 자기 거래만 본다. 다른 은행 포지션은 안 보인다 (S8, `tests/test_settlement_bank_api.py`) |
| 6. AvengersBank 대사·정리 **완료** | 2일 | `LedgerAccount.HUB_RESERVE`, `LedgerEvent.SETTLE`, `interbank_ledger.hub_run_id`, `settlement_reconciliations`, `settlement_sync.py`·`settlement_sync_loop()` 워커, `hub_client.settlements()`·`settlement(run_id)`, `reconcile()`의 SETTLE·예치금 기대값. 관리자 조회 API는 AvengersBank에 관리자 인증이 없어 만들지 않음 | 정상 하루치가 `MATCHED`되고 `SETTLE` 분개가 한 번만 생긴다. 허브 기록에 우리에게 없는 거래가 있으면 `MISMATCH`와 diff가 나온다 (`tests/test_settlement_sync.py`) |
| 7. 시연·문서 **완료** | 1일 | [DEMO.md](DEMO.md)(9절 대본, curl과 기대 숫자), `tests/test_demo_scenario.py`(9절 1~8을 허브 API로 끝까지), 가짜 은행 `…0006`(입금 없이 `200 COMPLETED`), DEPLOY "정산 배포 순서"(마이그레이션 → 재시작 → 은행마다 예치금·한도 → `/admin/reserve` 확인 → 개시 정산, `SETTLEMENT_*`), `902` 예치금은 시드 스크립트 대신 관리자 curl, PROJECT_PLAN·README·SPEC v0.6.1 | 9절 시나리오가 끝까지 돈다 (`uv run pytest tests/test_demo_scenario.py`) |
| 8. 부인방지 **8a~8d 완료** | 3.5일 | 12절 전체: Ed25519 요청·응답 서명, 키 등록·회전, 증거 보관, SPEC v1.0 초안. 8a(허브·가짜 은행, HMAC과 공존) 완료, 8b(AvengersBank: `INTERBANK_SIGNATURE_MODE`·`INTERBANK_SIGNING_KEY`·`HUB_PUBLIC_KEY`, 응답 서명 미들웨어, `hub_evidence`, 키 도구, 마이그레이션 `c0d1e2f3a4b5`, `tests/test_nonrepudiation.py`) 완료. **8c(허브): Ed25519 필수화·HMAC 삭제, 초대 코드 셀프 등록, `/v1/me`, 기본 한도 10억, 마이그레이션 `f3b8d2a7c4e1`, SPEC v1.0 다시 씀, ONBOARDING.md** 완료(12.11). **8d(AvengersBank): HMAC·`HUB_SECRET`·`INTERBANK_SIGNATURE_MODE` 삭제, Ed25519 전용, 셀프 등록 도구 `keys register-bank`·`ping`·`status`(`/v1/me`)** 완료(12.7) | 허브가 은행 서명을 만들 수 없고(허브 DB에 은행 비밀이 없다), 받은 응답으로 상대의 "답한 사실"을 증명할 수 있다 |
| 9. 관리자 손 줄이기 **완료** | 2일 | 13절: 정산 자동 확정(`settlement_acks`, `POST /v1/settlements/{run_id}/ack`·`/dispute`, 전원 ack 즉시 확정 `auto-ack`, 09:00 KST 마감 `auto-deadline`, dispute면 관리자), 은행 셀프 시험 `POST /v1/test-receive`·`/resend`·`/status`(허브가 902 명의로 시험 이체), 알림 웹훅 `core/alerts.py`(`ALERT_WEBHOOK_URL`), 마이그레이션 `a9d4e2c6b8f1`, SPEC v1.1(3.7·10.8), AvengersBank 대사 결과 ack/dispute | 관리자 일이 "초대 코드 발급·지문 전달 / 알림 대응 / 예치금 요청 처리"만 남는다 (docs/ONBOARDING.md) |
| 10. 연동 진단기 **완료** | 1일 | 14절: 인증 실패 기록 `auth_failures`(양방향, API 키 미저장), 규칙 서명 진단기 `core/signature_doctor.py`(실패한 서명을 흔한 실수 변형으로 다시 검증), 401 본문 `hint`, `GET /v1/diagnose/failures`(API 키만으로 식별), 관리자 `GET /admin/banks/{code}/auth-failures`, 알림에 진단 한 줄, 마이그레이션 `b7e3f1a9c2d4`, SPEC v1.2(3.8) | "왜 401이 나요?"에 은행이 401 응답의 `hint`로 원인(`verified` 가설)과 고칠 곳을 바로 본다. 관리자는 진단 목록을 그대로 보내 준다 (docs/ONBOARDING.md 7) |

8단계는 돈의 흐름(1~7단계, 실제 금융망의 13단계)이 아니라 **신뢰 장치**다. 1~7과 독립이라 2단계 뒤 언제든 할 수 있지만, 규격이 깨지는 변경이라 다른 팀과 날짜를 맞춰야 한다 (12절). 8c에서 아직 연동한 팀이 없다는 점을 보고 전환 기간 없이 Ed25519만 남겼다 (12.11).

**우선순위**: 기간이 줄면 8 → 6 → 5 → 4의 자동 워커 순으로 뺀다. 1·2·3(예치금, 한도, 상계 계산)은 유지한다. 이 셋만 있어도 "보낸 만큼 예치금이 줄고, 하루치 차액이 계산된다"를 보여 줄 수 있다.

**브랜치**: 허브는 `feature/settlement`, AvengersBank는 기존 `feature/backend-api`에서 이어서 한다 (main 병합·PR은 하지 않는다).

---

## 8. 테스트 목록

허브 `tests/test_settlement.py`, `tests/test_reserve.py`:

| 번호 | 시험 | 기대 |
|---|---|---|
| R1 | 예치 100만 → 인출 30만 → 잔액 70만, 분개 2건 | 잔액·분개 일치 |
| R2 | 한도 0, 잔액 10만에서 20만 인출 | 거절 |
| R3 | 같은 `run_id`로 `apply_instruction` 두 번 | 두 번째는 유니크 제약으로 실패, 잔액 한 번만 변함 |
| N1 | 예치 0·한도 0인 은행이 이체 | `LIMIT_EXCEEDED`, `status: FAILED`, 허브에 거래 없음 |
| N2 | 예치 10만, 5만 이체 2건 → 3번째 5만 | 3번째 거절 (PENDING도 exposure에 포함) |
| N3 | 받은 COMPLETED 10만이 있으면 그만큼 더 보낼 수 있다 | 허용 |
| S1 | 901→902 5만 2건, 902→901 3만 1건 → DRAFT | 901 net −7만, 902 net +7만, 쌍 표 1행, Σ=0 |
| S2 | `FAILED`·`PENDING`·`UNKNOWN` 거래는 빠진다 | tx_count에 없음 |
| S3 | 마감 뒤 COMPLETED 된 거래는 다음 정산에 들어간다. 어제 날짜 run은 오늘 끝난 거래를 뺀다 | 첫 run에 없고 두 번째 run에 있음 |
| S4 | DRAFT가 있는데 또 run / 취소 뒤 run / 내일 날짜 / 0건 | 409 `SETTLEMENT_DRAFT_EXISTS` / 거래가 미정산으로 돌아가 다시 들어감 / 422 / 201 tx_count 0 |
| S5 | 확정 → 잔액·분개·`result` | 901 잔액 −7만, 902 +7만, SETTLE_DEBIT/CREDIT |
| S6 | 확정 두 번 | 409, 잔액 그대로 |
| S7 | 잔액 5만·한도 0인 은행이 net −7만 | `OVERDUE`, 그다음 이체가 `LIMIT_EXCEEDED`, 받기는 됨 |
| S8 | 은행 API로 다른 은행 포지션 요청 | 자기 것만 나옴 (키 집합 검사, 계좌번호·이름 없음) |
| S9 | 정산된 거래에 `_set_status` / `resolve_unknown` | `RuntimeError` / 불가 (UNKNOWN이 아니므로 기존 409) |
| E1 | DRAFT 뒤·확정 뒤 순채무 (4.4) | DRAFT로는 순채무가 그대로, 확정 뒤 0이 되고 그만큼 예치금이 바뀐다. `available`은 그대로 |

AvengersBank `tests/test_settlement_sync.py`:

| 번호 | 시험 | 기대 |
|---|---|---|
| B1 | 가짜 허브 응답 = 우리 장부 (901 → 902 5만 2건, 902 → 901 3만 1건, SETTLED) | `MATCHED`, `SETTLE` 분개 생성, 정산 계정 잔액 0, `HUB_RESERVE` −7만 = `net_amount`, 맞춘 분개에 `hub_run_id`, `reconcile()` 정상 |
| B2 | 같은 run 두 번 돌림 | 분개 한 번만, 대사 행 갱신만 |
| B3 | 허브 목록에 우리에게 없는 `transfer_id` / 금액 다른 ID / 순액만 다름 | `MISMATCH`, diff에 `hub_only` / `differs` / `net`, 분개 없음 |
| B4 | 허브 DRAFT → 같은 run이 SETTLED | DRAFT 때는 대사 기록만, SETTLED가 되면 그때 분개 |
| B5 | 허브 응답 없음 / 401 | 행 없음, 다음 주기에 다시 (처음 계획의 `HUB_UNAVAILABLE` 행은 두지 않음) |
| B6 | 허브 COMPLETED, 우리 PENDING | `CHECKING`, 분개 없음 → 허브 결과 확인 뒤 `MATCHED` |
| B7 | 우리에게만 있는 거래 | `MATCHED`를 막지 않고 다음 run에서 맞춰짐. `cutoff_at − 1시간` 전 거래면 `ours_only_stale` 경고 |
| B8 | 끝나지 않은 run 고르기 | `run_id` 오름차순, 끝난 run은 안 봄, 취소된 DRAFT는 대사 행 삭제 |

---

## 9. 시연 시나리오

대본(명령·기대 응답)은 [DEMO.md](DEMO.md), 같은 순서를 허브 API로 돌려 숫자를 고정한 테스트는 `tests/test_demo_scenario.py`. 숫자는 모든 은행이 예치금 0에서 시작한 경우다.

1. **예치**: 관리자가 `901`(AvengersBank)·`902`(SimBank)에 각 100만 원 (`POST /admin/reserve/{code}/deposit`). `/admin/reserve`: 둘 다 잔액 100만, 순채무 0, `available` 100만
2. **한도 검사**: 시연용 `990`(DemoBank, 예치 0·한도 0)을 등록해 902로 1만 원 → `400 LIMIT_EXCEEDED` "순채무한도를 넘었습니다 (예치금 0원 …)", `status: FAILED`, 받는 은행까지 가지 않음. 5만 예치 뒤 같은 이체는 `COMPLETED` (`available` 4만). 운영에서 `903`부터는 다른 팀 몫이라 `990`을 쓴다
3. **하루치 거래**: 모아에게 "SimBank 110-123-0001로 5만 원" 2번, 관리자가 902 키로 `902 → 901` 3만 원 1번 (가짜 은행은 보내지 못하므로 허브 API로 직접). 순채무: 901 +7만(`available` 93만), 902 −8만(108만), 990 +1만
4. **마감 상계**: `POST /admin/settlements/run`(오늘) → DRAFT, 4건 14만. 포지션: 901 **−7만**, 902 **+8만**, 990 **−1만** (합 0). 쌍별 표 `901–902` 7만 `a_to_b`, `902–990` 1만 `b_to_a`. CSV 4행
5. **대사**: 901의 대사 파일(`GET /v1/settlements/{run_id}`)은 SENT 5만 2건·RECEIVED 3만 1건, net −7만 (990 거래는 안 보인다). DRAFT로는 순채무가 그대로. AvengersBank는 `settlement_reconciliations`에 `MATCHED` (`avengers_bank/tests/test_settlement_sync.py` B1이 같은 하루치로 증명)
6. **확정**: `confirm` → `/admin/reserve` 901 **93만**, 902 **108만**, 990 **4만**, 순채무 0, `available`은 확정 전과 같다. 분개 3건 (`SETTLE_DEBIT −7만`, `SETTLE_CREDIT +8만`, `SETTLE_DEBIT −1만`)
7. **은행 장부 정리**: AvengersBank 워커(`settlement_sync_loop`)가 SETTLED run을 보고 `INTERBANK_SETTLEMENT:902` +7만 → 0, `HUB_RESERVE` −7만 (B1·B4)
8. **속이는 은행**: 모아에게 "SimBank 110-123-**0006**으로 2만 원" → 가짜 은행이 입금 없이 `200 COMPLETED` → 허브 `COMPLETED`, 가짜 은행 장부(`received`)에는 없고 `GET /interbank/receive/{id}`는 404 → 새 run(901 −2만, 902 +2만)의 902 대사 파일에 그 거래가 `RECEIVED`로 있다 = 902가 대사하면 `hub_only` → `MISMATCH`. "데이터로만 주고받는데 어떻게 아느냐"에 대한 답이 이 화면이다. 가짜 은행은 장부가 없어 스스로 대사하지 않으므로, AvengersBank 화면에서 `MISMATCH`를 보이려면 반대 방향(허브에만 있는 AvengersBank 입금)을 시연 DB에서 만든다 (DEMO.md 8)

---

## 10. 리스크

| 리스크 | 대응 |
|---|---|
| 접수마다 exposure 합계 쿼리 2개가 돈다 | 인덱스 `(from_bank, settlement_run_id)`·`(to_bank, settlement_run_id)`. 미정산 거래는 하루치뿐이라 작다. 부족하면 캐시 컬럼 |
| 배치가 도는 중에 거래가 COMPLETED 됨 | `finished_at < cutoff_at`로 자르고, 늦은 것은 다음 run (2절 3번). `create_draft`의 UPDATE 한 문장으로 경쟁을 없앤다 |
| 관리자 두 명이 동시에 run | 서비스가 DRAFT를 먼저 확인하고, 둘 다 지나치면 부분 유니크 인덱스가 두 번째를 막는다 (409). 수집 UPDATE는 `settlement_run_id IS NULL` 조건이라 같은 거래가 두 run에 들어가지 않는다 |
| 허브와 은행의 날짜 경계가 다름 | 둘 다 KST, 허브 `finished_at` 기준. 은행은 허브가 준 목록을 **기준**으로 삼고 자기 장부에서 그 ID를 찾는다 (날짜로 자르지 않는다) |
| 허브 재시작 중 배치가 안 돎 | 워커 시작 때 "오늘 만든 run이 없고 배치 시각이 지났으면 지금 돌린다" (`needs_catch_up`, 4단계). 관리자 수동 실행도 있다 |
| 예치금을 실제 돈으로 오해 | 화면·문서에 "모의 예치금"이라고 적는다. 입출금은 관리자 API뿐이다 |
| 기존 참가 은행이 갑자기 막힘 | 배포 직후 관리자가 은행마다 예치금 또는 한도를 넣는다. 배포 순서를 DEPLOY.md에 적는다 |
| 정산된 거래를 고쳐야 할 때 | 거래는 안 고친다. `ADJUST` 분개 + 사유. 다음 정산에는 영향 없음 |

---

## 11. 범위 밖 (다음 단계)

- 손실분담·담보처분: `OVERDUE` 은행의 미결제 금액을 다른 은행이 나눠 지는 규칙
- 예치금 원장의 별도 서버 분리 (`apply_instruction`을 HTTP로)
- 관리 사이트(React) 화면. 이 계획은 API와 CSV까지다
- 가짜 은행 `902`의 장부·대사 (받기 전용이라 생략)

---

## 12. 8단계: 부인방지 (공개키 서명·응답 서명)

> **8c에서 결정이 바뀌었다 (12.11).** 아래 12.2~12.10의 HMAC 공존·서명 모드(`hmac`·`both`·`ed25519`)·전환 순서는 8a·8b 때의 설계 기록이다. 지금 규격과 코드는 Ed25519 하나이고, 은행은 초대 코드로 셀프 등록한다.

### 12.1 왜 하는가

지금 서명은 HMAC-SHA256 **공유 비밀**이다. 은행의 secret을 허브도 갖고 있어서, 허브는 "그 은행이 보낸" 요청을 만들어 낼 수 있다. 허브가 거래를 꾸며 정산까지 하면 은행은 대사에서 `MISMATCH`를 보지만, 제3자에게 "우리가 보낸 게 아니다"를 **증명할 수 없다**. 응답에는 서명이 아예 없어서, 받는 은행이 `200 COMPLETED`라고 답해 놓고 "그런 적 없다"고 해도 증명할 수 없다.

실제 금융망은 공개키 전자서명으로 이것을 막는다. 개인키는 그 기관만 갖고 있으니 서명이 있으면 보낸 사실을 부인할 수 없고, 서명이 없으면 꾸민 것이다.

| 위협 | 지금 | 8단계 뒤 |
|---|---|---|
| 허브가 은행 명의의 요청을 꾸밈 | 가능 (secret 공유) | 불가 (은행 개인키는 은행만) |
| 은행이 "그런 요청 안 보냈다"고 부인 | 판정 불가 | 서명으로 반박 |
| 받는 은행이 "입금 완료라고 답한 적 없다"고 부인 | 판정 불가 | 응답 서명으로 반박 |
| 허브가 "그런 응답 받은 적 없다"고 부인 | 판정 불가 | 은행이 보관한 서명 응답으로 반박 |

### 12.2 설계 결정

| # | 항목 | 결정 | 이유 |
|---|---|---|---|
| 1 | 알고리즘 | **Ed25519** (`cryptography` 패키지). 서명은 base64 (88자) | 키가 짧고 빠르며 라이브러리가 표준적. RSA보다 구현 실수가 적다 |
| 2 | 요청 서명 메시지 | 지금 2.2와 **같은 문자열** `ts\nMETHOD\nPATH\nbody`. HMAC 대신 Ed25519로 서명 | 은행이 메시지 조립 코드는 그대로 두고 서명 함수만 바꾸면 된다 |
| 3 | 응답 서명 메시지 | `response_ts\nSTATUS\nrequest_signature\nresponse_body`. `request_signature`는 그 요청의 `X-Signature` 값 | 응답을 **특정 요청에 묶는다.** 다른 요청의 응답을 가져다 붙이지 못한다 |
| 4 | 헤더 | 요청: 기존 `X-Timestamp`·`X-Signature` + `X-Signature-Alg: ed25519`. 응답: `X-Response-Timestamp`, `X-Response-Signature`, `X-Signature-Alg` | `X-Signature-Alg`가 없으면 HMAC으로 본다 (전환 기간용) |
| 5 | 은행 키 등록 | 은행이 키 쌍을 만들고 **공개키만** 허브에 등록: `PUT /v1/keys` (기존 HMAC으로 서명한 요청. 기존 인증을 부트스트랩으로 쓴다). 허브는 `banks.public_key`, `public_key_registered_at` 저장. 관리자도 `POST /admin/banks/{code}/public-key`로 넣을 수 있다 | 개인키는 허브에 절대 오지 않는다 |
| 6 | 허브 키 | 허브 키 쌍은 환경 변수(`HUB_SIGNING_KEY`, 개인키 base64). 공개키는 `GET /v1/hub-key`(인증 불필요)로 주고, 지문(SHA-256 앞 16자)을 관리자가 은행에 따로 알려 준다 | 은행이 처음 받은 공개키가 진짜인지 지문으로 확인 |
| 7 | 전환 (8a에서 고침) | `banks.signature_mode`, 관리자만 바꾼다 (`PATCH /admin/banks/{code}/signature-mode`). **공개키를 등록해도 허브 동작은 바뀌지 않는다.** `hmac`(기본): 은행 → 허브 HMAC만, 허브 → 은행 HMAC, 응답 서명 요구 없음. `both`: 은행 → 허브 HMAC·Ed25519 둘 다(Ed25519는 공개키가 있어야), 허브 → 은행은 **아직 HMAC**, 응답 서명 요구 없음. `ed25519`: 은행 → 허브 Ed25519만, 허브 → 은행 Ed25519, 은행 응답에 **응답 서명 필수**. `ed25519`로 올리려면 은행 공개키와 허브 키가 있어야 한다(없으면 400 `PUBLIC_KEY_MISSING`·`HUB_KEY_MISSING`). 내리는 것은 언제든 된다. 은행은 받는 쪽(허브 서명 Ed25519 검증, 자기 응답 서명)을 먼저 준비한 뒤 전환을 요청한다 (SPEC 2.6 순서) | 2절의 키 재발급처럼 "겹치는 기간 없음"으로 하면 모든 은행이 같은 순간에 바꿔야 한다. 은행별 전환이 현실적. 처음 계획(`both`에서 공개키가 있으면 허브 → 은행을 Ed25519로)은 은행이 키를 등록하는 순간 받는 쪽이 준비되지 않았으면 입금이 401(`FAILED`)로 깨진다. 받는 쪽이 바뀌는 시점을 관리자 전환 하나로 모았다 |
| 8 | 키 회전 | `PUT /v1/keys`를 **새 키로 서명하지 않고 현재 키로 서명**해서 다시 부른다. 이전 공개키는 `banks.previous_public_key`에 24시간 보관하고 그 동안 둘 다 받는다 | 유출 때 바로 바꿀 수 있고, 진행 중 재시도가 깨지지 않는다 |
| 9 | 증거 보관 | 허브: `transfer_evidence` 테이블에 접수 요청의 원본 바이트·헤더·서명과, 받는 은행 `/receive` 응답의 본문·헤더·서명을 저장. 고치거나 지우지 않는다 | 분쟁 때 꺼내 보일 것. 서명은 원본 바이트에 대한 것이라 바이트 그대로 둔다 |
| 10 | 상수 시간 비교 | Ed25519 검증은 라이브러리가 한다. 타임스탬프 5분 규칙은 그대로 | |

### 12.3 데이터 모델

```
banks                       (변경)
  signature_mode            hmac | both | ed25519   기본 hmac
  public_key                base64 (32바이트), nullable
  previous_public_key       nullable
  previous_key_expires_at   timestamptz, nullable
  public_key_registered_at  timestamptz, nullable

transfer_evidence           (새 테이블, 허브)
  evidence_id PK
  transfer_id FK index
  direction                 REQUEST_IN (은행 → 허브 접수) | RECEIVE_OUT (허브 → 은행 /receive·/receive/{id} 요청) | RECEIVE_REPLY (은행의 응답)
  method, path              요청만 (서명에 쓴 METHOD·규격 PATH. 다시 검증할 때 필요)
  http_status               응답만 (응답 서명 메시지의 STATUS)
  headers_json              서명 관련 헤더만 (X-Bank-Code, X-Timestamp, X-Signature, X-Signature-Alg, X-Request-ID, X-Response-*). X-Api-Key는 넣지 않는다
  body                      bytea / BLOB (원본 바이트)
  signature_alg             hmac | ed25519 | NULL (서명 없는 응답)
  signature_valid           bool, nullable (받을 때 검증 결과. 허브가 보낸 RECEIVE_OUT과, 요구하지 않는 응답 서명이 없을 때는 NULL)
  created_at
```

응답(RECEIVE_REPLY)을 다시 검증할 때의 `request_signature`는 바로 앞 RECEIVE_OUT 행의 `X-Signature`다. 4.3 입금 확인(GET)도 남긴다: 확인 응답의 `COMPLETED`가 거래를 완료로 만든 근거이기 때문이다. 응답을 못 받은 호출은 RECEIVE_OUT만 남는다. 수취 조회·본인 계좌 조회·대사 조회는 남기지 않는다. 마이그레이션 `e5a1c7f3d9b2`.

AvengersBank: `hub_evidence` 같은 표로 허브에서 받은 `/receive` 요청 원본과 허브 응답(허브 → 은행 호출의 응답은 은행이 서명하므로 자기 서명 원문도 보관)을 저장. 키 쌍은 `.env`(`HUB_SIGNING_KEY`)에.

### 12.4 처리 흐름

```
요청 (은행 → 허브, 허브 → 은행 공통)
  보내는 쪽: message = ts\nMETHOD\nPATH\nbody → Ed25519 서명 → X-Signature, X-Signature-Alg: ed25519
             허브 → 은행은 받는 은행이 ed25519 모드일 때만 Ed25519, hmac·both면 HMAC (+ X-Signature-Alg: hmac)
  받는 쪽:   X-Signature-Alg 보고 분기 (모르는 값은 401 SIGNATURE_INVALID)
             ed25519 → signature_mode가 both·ed25519일 때만. 상대 공개키(현재 또는 24시간 안의 이전 키)로 검증
             없음/hmac → signature_mode가 hmac·both일 때만 HMAC 검증
             모드가 받지 않는 방식이면 401 SIGNATURE_INVALID (message에 모드 안내)
             검증 뒤 원본 바이트·헤더를 evidence에 저장 (이체 접수, 새 거래일 때만)

응답 (2xx·4xx. 5xx는 본문 보장이 없으므로 서명도 없을 수 있다)
  허브:      /v1/* 응답에 허브 키가 있으면 은행 모드와 상관없이 서명 (core/response_signing.py)
  답하는 쪽: message = response_ts\nSTATUS\n<요청의 X-Signature>\nresponse_body → 서명 → X-Response-* 헤더
  받는 쪽:   ed25519 모드 은행의 응답은 서명이 없거나 틀리면 "응답 없음"으로 취급 (BankReply status None, signature_valid False)
               → 이체는 PENDING → 4.3 확인, 수취 조회는 502 RECEIVER_ERROR
             hmac·both 모드 은행의 응답은 서명을 요구하지 않는다. 있으면 검증해 signature_valid만 기록
             /receive·/receive/{id}의 요청·응답 바이트를 검증 결과와 함께 evidence에 저장 (처음·재시도 모두)
```

응답 서명 검증 실패를 "거절"이 아니라 "응답 없음"으로 보는 이유: 받는 은행이 입금을 끝냈는데 서명 설정만 틀린 경우, `FAILED`로 끝내면 보내는 은행이 되돌려 이중 지급이 된다. SPEC 4.2의 원칙(모르면 되돌리지 않는다)과 같다.

### 12.5 API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/hub-key` | 허브 공개키, 알고리즘, 지문. 인증 불필요 |
| PUT | `/v1/keys` | `{public_key}` 내 공개키 등록·교체. 현재 인증(HMAC 또는 현재 Ed25519 키)으로 서명. 응답에 `signature_mode`, 지문 |
| GET | `/v1/keys` | 내 등록 상태 (지문, `signature_mode`, 이전 키 만료 시각) |
| POST | `/admin/banks/{code}/public-key` | 관리자가 대신 등록 (은행이 채팅으로 공개키를 준 경우) |
| PATCH | `/admin/banks/{code}/signature-mode` | `hmac` / `both` / `ed25519` 전환 |
| GET | `/admin/transfers/{id}/evidence` | 분쟁 조회: 그 거래의 요청·응답 원문과 서명 검증 결과 |

### 12.6 SPEC v1.0 — 초안 반영됨 (8a: SPEC 2.1~2.6, 4절 머리말, 5절, 7절 4, 8절 17·19, 10.7)

- 2절을 "2. 인증: 공개키 서명(권장) / HMAC(전환 기간)"으로 다시 쓴다. 메시지 형식은 그대로, 서명 함수와 헤더 `X-Signature-Alg`만 추가. Python 참고 구현(`cryptography`의 `Ed25519PrivateKey`) 추가. 검증용 예시: 고정 테스트 키 쌍과 예시 1·2의 서명 값
- 새 절 **2.5 응답 서명**: 메시지 형식, 헤더, 검증 실패 시 "응답 없음" 취급
- 새 절 **2.6 키 등록·회전**: 12.5의 `/v1/keys`, 지문 확인, 24시간 이전 키
- 4절(은행이 만드는 API): 허브 요청의 Ed25519 검증, **자기 응답에 서명** 추가. 이것이 은행 쪽의 유일한 필수 변경이다
- 8절 17 갱신, 변경 이력 1.0

### 12.7 작업 분할 (세션 2개)

| 세션 | 내용 | 완료 기준 |
|---|---|---|
| 8a 허브·가짜 은행 (2일) **완료** | `core/signing.py`에 Ed25519 서명·검증·응답 서명, `core/auth.py` 분기, `bank_client.py` 응답 검증, `core/response_signing.py`(응답 서명 미들웨어), banks 모델·API(`/v1/hub-key`, `/v1/keys`, 관리자 `public-key`·`signature-mode`), `transfer_evidence`(`transfers/evidence.py`, 관리자 `/evidence`), `bank_sim` Ed25519 검증·응답 서명(`SIM_SIGNATURE_MODE`·`SIM_HUB_PUBLIC_KEY`·`SIM_SIGNING_KEY`), Alembic `e5a1c7f3d9b2`, SPEC v1.0 초안 | 가짜 은행 `902`를 `both`·`ed25519`로 전환해도 기존 이체 시나리오 통과, `hmac`에서 기존 123개 그대로 통과. 응답 서명이 틀리면 PENDING → 4.3으로 COMPLETED (`tests/test_nonrepudiation.py`, K1은 `tests/test_signing.py`) |
| 8b AvengersBank (1일) **완료** | `interbank/signing.py` Ed25519·응답 서명, `hub_client.py` 모드별 서명·허브 응답 서명 검증(`ed25519`에서 틀리면 "응답 없음" → PENDING), `router.py` `X-Signature-Alg` 분기·모드별 허용, `response_signing.py`(허브 것과 같은 순수 ASGI, 저장소 안에 따로), `hub_evidence`(`TRANSFER_OUT`·`TRANSFER_REPLY`·`RECEIVE_IN`·`RECEIVE_REPLY`, Alembic `c0d1e2f3a4b5`), 키 도구 `python -m avengers_bank.domains.interbank.keys generate·register·status·hub-key`, `.env` `INTERBANK_SIGNING_KEY`·`HUB_PUBLIC_KEY`·`INTERBANK_SIGNATURE_MODE`(허브가 정한 모드와 같게) | AvengersBank가 `ed25519` 모드로 가짜 허브와 왕복 이체·입금, `hmac` 기본에서 기존 테스트 그대로 (`tests/test_nonrepudiation.py`). 실제 허브와의 운영 전환은 아직 |
| 8d AvengersBank Ed25519 전용 (2026-11-06) **완료** | 12.11 표대로: 설정 `HUB_SECRET`·`INTERBANK_SIGNATURE_MODE` 삭제, 타행 이체는 `HUB_URL`·`HUB_API_KEY`·`INTERBANK_SIGNING_KEY`·`HUB_PUBLIC_KEY` 넷이 있어야 켜짐(받는 쪽 `/interbank/*`는 두 키만 있으면 열림 — 셀프 등록 직후 ping용). `signing.py` HMAC 삭제, `hub_client.py` 늘 Ed25519·허브 응답 서명 늘 필수, `router.py` Ed25519만(서명 헤더 없음 401 `UNAUTHORIZED`). 키 도구 `register-bank`(셀프 등록, 서명 없이)·`ping`(`/v1/me/ping`)·`status`(`/v1/me`+`/v1/keys`)·`register`(회전만)·`hub-key`·`generate`. 마이그레이션 없음 | 가짜 허브가 Ed25519만 받고 응답에 서명하는 상태로 전체 테스트 통과, `register-bank`·`ping`은 가짜 허브가 실제로 우리 `/interbank/inquiry`를 불러 응답 서명까지 확인. 운영 전환은 AvengersBank `docs/DEPLOY.md` "Ed25519 전용 전환" (허브 DEPLOY "Ed25519 필수화 배포 순서"와 함께) |

### 12.8 테스트

| 번호 | 시험 | 기대 |
|---|---|---|
| K1 | 예시 1·2를 테스트 키로 서명한 값이 SPEC의 값과 같다 | 일치 |
| K2 | `both` 모드: HMAC 요청·Ed25519 요청 둘 다 200 | 통과 |
| K3 | `ed25519` 모드에서 HMAC 요청 | 401 `SIGNATURE_INVALID` |
| K4 | 다른 은행 개인키로 서명 | 401 |
| K5 | 허브 secret으로 HMAC 서명을 꾸민 요청을 `ed25519` 은행에 보냄 | 401 (핵심: 허브도 못 꾸민다) |
| K6 | 응답 서명이 없거나 틀린 `/receive` 200 | 이체 PENDING, 4.3 확인 뒤 COMPLETED, 입금 1번 |
| K7 | 응답 서명의 `request_signature`가 다른 요청 것 | 검증 실패 (응답 바꿔치기 불가) |
| K8 | 키 회전 뒤 이전 키 24시간 안 요청 | 200. 24시간 뒤 401 |
| K9 | `GET /admin/transfers/{id}/evidence`에 요청·응답 원본과 `signature_valid` | 저장됨, 바이트 일치 |
| K10 | `PUT /v1/keys`를 다른 은행 인증으로 | 자기 것만 바꿀 수 있다 |

### 12.9 리스크

| 리스크 | 대응 |
|---|---|
| 다른 팀이 Ed25519를 못 붙임 | `both`로 두면 그 은행은 HMAC으로 계속 된다. 부인방지는 전환한 은행 사이에서만 성립한다고 SPEC에 적는다 |
| 응답 서명 때문에 기존 은행의 응답이 "응답 없음"으로 바뀜 | `hmac` 모드 은행의 응답은 서명을 요구하지 않는다. `ed25519` 모드에서만 요구 |
| 개인키 유출 | 회전 API + 관리자 `signature-mode`로 즉시 차단. 유출 전 서명 증거는 그대로 유효 |
| 증거 테이블 크기 | 이체 관련 요청·응답만, 본문은 수백 바이트. 보관 기간은 미정 (범위 밖) |
| 가짜 은행이 허브의 `signing`을 import함 | 그대로 둔다 (8a 결정). 같은 저장소의 시험용 서버라 따로 두면 두 구현이 어긋날 위험만 생긴다. SPEC 2.3·2.5에 저장소 없이 쓰는 참고 구현을 두고, 7절 6의 "따로 가져갈 때는 참고 구현으로 바꾼다"를 유지한다 |
| `ed25519` 은행이 응답 서명을 빠뜨림 | 이체가 모두 PENDING → UNKNOWN으로 쌓인다 (이중 지급은 없다). 전환 직후 ping의 `response_signature_valid`로 확인하고, 틀리면 관리자가 `both`로 내린다 |

### 12.10 8b(AvengersBank)가 알아야 할 것 (8a에서 확정)

규격은 SPEC v1.0 초안 2.1·2.2·2.3·2.5·2.6과 4절 머리말이 기준이다. 허브와 가짜 은행 `902`는 이미 이대로 돈다.

| 항목 | 확정된 값 |
|---|---|
| 요청 헤더 | `X-Timestamp`, `X-Signature`(Ed25519 base64 88자), `X-Signature-Alg: ed25519`. 없으면 HMAC으로 본다. 허브는 HMAC으로 보낼 때도 `X-Signature-Alg: hmac`을 넣는다. `X-Api-Key`는 모든 모드에서 그대로 |
| 요청 메시지 | 지금 HMAC과 같은 `f"{ts}\n{METHOD}\n{PATH}\n".encode() + body`. 허브 → 은행의 PATH는 `base_url` 뒤의 부분 |
| 키 형식 | Ed25519 raw 32바이트의 표준 base64(44자). 서명은 raw 64바이트 base64(88자). 지문 `sha256(raw 공개키).hexdigest()[:16]` |
| 응답 헤더 | `X-Response-Timestamp`, `X-Response-Signature`, `X-Signature-Alg: ed25519` |
| 응답 메시지 | `f"{response_ts}\n{STATUS}\n{요청의 X-Signature 그대로}\n".encode() + 응답 본문 바이트`. 요청에 서명이 없으면 `""` |
| 허브 공개키 | `GET /v1/hub-key`(인증 없음) → `{algorithm, public_key, fingerprint}`. 허브 관리자가 따로 알려 준 지문과 맞춰 `.env`에 저장 |
| 키 등록 | `PUT /v1/keys` `{"public_key": ...}`, 지금 인증(HMAC)으로 서명. 응답 `{bank_code, fingerprint, signature_mode, registered_at, previous_fingerprint, previous_key_expires_at, hub_fingerprint}`. `GET /v1/keys`도 같은 형식 |
| 회전 | 새 공개키로 `PUT /v1/keys`를 **지금 키로** 서명해 부른다. 허브는 옛 키를 24시간 받는다 (요청·응답 서명 모두) |
| 모드별 허브 동작 | `hmac`: 지금과 같다. `both`: AvengersBank → 허브 Ed25519도 받음, 허브 → AvengersBank는 HMAC. `ed25519`: 허브 → AvengersBank Ed25519, AvengersBank의 `/interbank/*` 2xx·4xx 응답에 응답 서명이 없으면 "응답 없음"(이체 PENDING, 수취 조회 502) |
| 허브 응답 | 허브는 `/v1/*` 2xx·4xx에 항상 응답 서명을 붙인다. `hub_client`에서 검증하고, 틀리면 본문을 믿지 말고 "모르는 상태"(3.4 확인)로 둔다 |
| 전환 순서 | ① 키 쌍 생성(개인키는 `.env`의 `HUB_SIGNING_KEY` 같은 변수) ② 허브 공개키 받기·지문 확인 ③ `/interbank/*`가 `X-Signature-Alg`로 분기(ed25519는 허브 공개키, 그 밖은 secret)해 검증하고 2xx·4xx 응답에 서명 ④ `PUT /v1/keys` ⑤ 관리자 `both` ⑥ `hub_client`를 Ed25519로 ⑦ 관리자 `ed25519` + ping `response_signature_valid: true` |
| 증거 | 허브는 이체 접수·`/receive`·`/receive/{id}` 원문을 보관한다 (`GET /admin/transfers/{id}/evidence`). AvengersBank는 `hub_evidence`에 허브가 보낸 `/receive` 요청 원문(헤더·바이트)과 자기 서명 응답, 자기가 보낸 `POST /v1/transfers` 요청과 허브의 서명 응답을 남긴다 |
| 시험 | SPEC 2.3 예시 1·2와 2.5 예시 3의 고정 값으로 구현을 먼저 맞춘다. 허브 쪽 헬퍼는 `tests/conftest.py`의 `SignedBank.use_ed25519()`, 전환 순서는 `tests/test_nonrepudiation.py`의 `ed25519_banks` 픽스처 |

### 12.11 8c: Ed25519 필수화·셀프 등록 (허브, 2026-11-06)

**결정 변경.** 8a는 HMAC과 Ed25519를 은행별 서명 모드로 함께 쓰게 만들었다 (기존 은행이 아무것도 바꾸지 않아도 되게). 그런데 **아직 연동을 시작한 다른 팀이 없다.** 지킬 기존 은행이 없으니 전환 기간이 비용만 남긴다: 규격에 모드·전환 순서·두 가지 검증이 들어가고, 은행은 두 방식을 다 만들어야 하며, 무엇보다 `hmac_secret`이 허브 DB에 남아 **허브가 은행 명의 서명을 만들 수 있는 상태**가 계속된다. 그래서 Ed25519를 필수로 하고 HMAC을 규격과 코드에서 뺐다. 허브는 은행의 비밀을 하나도 갖지 않는다 (공개키와 API 키 해시뿐). 같은 김에 관리자 일을 "초대 코드 보내기"로 줄이는 셀프 등록과 기본 순채무한도를 넣었다 (자동 확정·셀프 시험·알림은 7절 9단계).

| 영역 | 8c에서 바뀐 것 |
|---|---|
| 서명 | `core/signing.py`에서 HMAC `sign`·`verify`·`new_secret` 삭제. `core/auth.py`: 등록된 공개키(현재 + 24시간 이전 키)로만 검증, `X-Signature-Alg`는 `ed25519` 또는 없음, 공개키가 없으면 401 `SIGNATURE_INVALID` "등록된 공개키가 없습니다". `core/bank_client.py`: 항상 허브 키로 서명, 2xx·4xx 응답 서명 **항상 필수**(없거나 틀리면 "응답 없음"), 5xx는 그대로 |
| 설정 | `HUB_SIGNING_KEY` **필수**(없거나 틀리면 시작 실패, `GET /v1/hub-key`의 `HUB_KEY_MISSING` 없어짐). `INVITE_TTL_DAYS`(7), `BANK_CODE_START`(`903`), `DEFAULT_NET_DEBIT_CAP`(10억). 가짜 은행: `SIM_SIGNING_KEY` 필수(서버로 띄울 때 검사), `SIM_HUB_PUBLIC_KEY`(없으면 검증 생략), `SIM_HUB_SECRET`·`SIM_SIGNATURE_MODE` 삭제 |
| 데이터 | 마이그레이션 `f3b8d2a7c4e1`: `banks.hmac_secret`·`signature_mode` 삭제, `invites(code PK, note, created_at, expires_at, used_at, used_by_bank_code FK)`. 공개키가 NULL인 기존 은행(운영 901·902)은 막지 않는다 → 인증 401, 받는 은행으로는 접수 전 거절 `PUBLIC_KEY_MISSING`(응답 서명을 검증할 수 없어서). DEPLOY.md "Ed25519 필수화 배포 순서" 1이 그 둘의 공개키 등록이다 |
| 셀프 등록 | `POST /admin/invites {note}` → `{code, expires_at, message}`(코드는 이때만. `message`는 그대로 DM으로 보낼 초대 문구: 코드·만료·허브 주소(`HUB_PUBLIC_URL`, 없으면 요청 주소)·`docs/SPEC.md`·허브 공개키와 지문·등록 방법·1회용 경고), `GET /admin/invites`(앞 8자, `UNUSED`·`USED`·`EXPIRED`). 인증 없는 `POST /v1/banks/register {invite_code, name, base_url, public_key}` → 코드 확인(`INVITE_INVALID`) → 같은 `base_url` 확인(409 `BANK_EXISTS`) → `903`부터 빈 코드 배정(관리자가 넣은 `990` 등은 건너뜀) → 은행·예치금 계좌·기본 한도 → 초대 코드 사용 처리 → 커밋 뒤 ping → 201 `{bank_code, name, api_key, hub_public_key, hub_fingerprint, net_debit_cap, ping}`. ping이 실패해도 등록·`active` 유지. 초대 코드 행은 `FOR UPDATE`로 잠그고, 은행 코드 PK 경합은 새 트랜잭션으로 세 번까지 다시 한다 |
| 셀프 서비스 | `GET /v1/me`, `PATCH /v1/me {name?, base_url?}`(새 주소로 ping해 정상일 때만 반영, 아니면 400 `PING_FAILED` + `ping`), `POST /v1/me/ping`. ping 결과 `{ok, reachable, status, body, response_signature_valid}` — `ok`는 응답 서명이 맞고 200 또는 404 `ACCOUNT_NOT_FOUND`. 관리자 ping도 같은 모양 (`signature_mode` 칸 삭제) |
| 관리자 | `POST /admin/banks`에 `public_key` 필수(44자 검증, 응답에 지문), `net_debit_cap` 선택(빼면 기본값). `POST /admin/banks/{code}/keys`는 API 키만 재발급(`secret` 없음). `PATCH /admin/banks/{code}/signature-mode` 삭제. `POST /admin/banks/{code}/public-key`·`PUT`/`GET /v1/keys`·`GET /v1/hub-key`는 그대로 |
| 기본 한도 | 새로 등록하는 은행(셀프·관리자)은 순채무한도 10억이라 예치금 0으로도 바로 보낸다. 마이그레이션은 기존 은행의 한도를 바꾸지 않는다 |
| 문서 | SPEC v1.0 다시 씀(2절 Ed25519 전용, 3.5·3.6, 5절 표, 7절 셀프 등록 절차, 8절 1·3·13·17·18·19·20, 10.2, 체크리스트, 변경 이력 1.0 고쳐 씀. 판 번호는 1.0 그대로), 새 [ONBOARDING.md](ONBOARDING.md)(관리자용), DEPLOY.md(배포 순서·`sim.env`·`openssl`로 키 만들기), DEMO.md(DemoBank를 셀프 등록 예로, `hub_call.py` Ed25519), `deploy/server-setup.sh`(키를 서버 안에서 생성) |
| 테스트 | `SignedBank`는 Ed25519 개인키로만 서명, `register_bank`가 키 쌍을 만들어 공개키로 등록, 902는 가짜 은행 키(`SIM_SIGNING_KEY`). 모드 시험(K2·K3·`both`, `mode_banks`) 삭제 — 다른 파일의 이체 시나리오가 모두 Ed25519로 돈다. **K5를 "허브 DB의 모든 값(API 키 해시, 공개키, 이전 공개키)으로는 은행 명의 서명을 만들 수 없다"로 바꿈.** 새 `tests/test_onboarding.py`(초대 코드·코드 배정·동시 등록·`BANK_EXISTS`·ping 실패 유지·`/v1/me`·기본 한도) |

**8d(AvengersBank)가 걷어낼 것과 바꿀 것** (AvengersBank `feature/backend-api`. 허브는 8c부터 아래가 아니면 받지 않는다)

| AvengersBank 쪽 | 8d에서 |
|---|---|
| 설정 `HUB_SECRET`(`hub_secret`) | **삭제.** 허브는 더 이상 secret을 발급하지 않는다. "타행 이체 켜짐" 조건(`HUB_URL`·`HUB_API_KEY`·`HUB_SECRET`)을 `HUB_URL`·`HUB_API_KEY`·`INTERBANK_SIGNING_KEY`·`HUB_PUBLIC_KEY`로 바꾼다 |
| 설정 `INTERBANK_SIGNATURE_MODE`(`hmac`·`both`·`ed25519`) | **삭제.** 늘 지금의 `ed25519` 동작. 시작 검사는 "두 키가 모두 있고 형식이 맞아야 시작"(지금 `ed25519` 모드의 검사)만 남긴다 |
| `hub_client.py` 서명 | 모드 분기·HMAC 폴백(`sign(hub_secret)`, 키 없을 때 경고 후 HMAC) 삭제. 항상 `sign_ed25519` + `X-Signature-Alg: ed25519` |
| `hub_client.py` 허브 응답 서명 | 늘 필수 (지금 `ed25519` 모드 동작): 없거나 틀리면 본문을 믿지 않고 "모르는 상태"(3.4 확인) |
| `router.py` `_verified_body` (받는 쪽) | `X-Signature-Alg` 분기 삭제: `ed25519` 또는 없음 → `HUB_PUBLIC_KEY`로만 검증, 그 밖의 값(`hmac` 포함) 401 `SIGNATURE_INVALID`. 401에도 응답 서명 (지금처럼) |
| `signing.py` | HMAC `sign`·`verify`와 `ALG_HMAC` 삭제 (CI 계산의 HMAC은 그대로) |
| 키 도구 `keys register` | 처음 등록은 이제 `PUT /v1/keys`가 아니라 **셀프 등록**이다. 운영 901은 이미 등록돼 있으므로 허브 관리자가 `POST /admin/banks/901/public-key`로 공개키를 넣는다 (배포 순서 1). `register`(회전)는 HMAC 부트스트랩을 빼고 **지금 Ed25519 키로만** 서명한다(`--public-key`). 원하면 `keys self-register --invite-code`(새 은행용)를 더해도 된다 |
| 키 도구 `keys status` | 응답에 `signature_mode`가 없어졌다 (읽는 코드가 있으면 뺀다). `GET /v1/me`를 보여 주는 명령을 더해도 된다 |
| `hub_evidence` | 그대로. `signature_alg`는 이제 늘 `ed25519` (옛 행의 `hmac`은 둔다) |
| 테스트 | HMAC·`both`·모드 전환 시험 삭제, 가짜 허브가 Ed25519만 쓰게. 허브 응답의 관리자 ping에 `signature_mode`가 없다는 점 |
| 문서 | `INTERBANK.md`의 모드 표·전환 순서(1~8)·"5보다 6 먼저 금지" 삭제, `.env.example`·`DEPLOY.md`·README의 `HUB_SECRET` 삭제. 키 회전·유출 대응만 남긴다 |

**운영 전환 순서** (허브 DEPLOY.md "Ed25519 필수화 배포 순서"와 같다): ① 서버 안에서 허브 키(`HUB_SIGNING_KEY`)·가짜 은행 키(`SIM_SIGNING_KEY`, `SIM_HUB_PUBLIC_KEY`) 만들기 ② AvengersBank가 키 쌍을 만들어 공개키·지문을 허브 관리자에게, 허브 관리자가 허브 공개키 지문을 AvengersBank에 ③ 허브 배포(정산 1~7·8a·8c가 한 번에 간다, 운영 `main`은 SPEC v0.5) + 바로 901·902 공개키 등록 ④ AvengersBank 8d 배포(`HUB_SECRET` 지우고 `INTERBANK_SIGNING_KEY`·`HUB_PUBLIC_KEY`) ⑤ 901·902 ping `ok: true`, 소액 이체. ③과 ④ 사이에는 901의 요청이 401, 901로 가는 이체가 `PENDING`(응답 서명 없음) 또는 401 `FAILED`이므로 짧게 붙여서 한다. 돈은 이중으로 움직이지 않는다.

---

## 13. 9단계: 관리자 손 줄이기 (허브·AvengersBank, 2026-11-06)

**왜.** 8c까지 관리자가 손으로 하던 일이 셋 남았다: (1) 매일 DRAFT 확정(안 하면 다음 배치가 건너뜀), (2) 새 은행의 **받는 쪽** 시험을 위해 901에서 이체 보내 주기, (3) `/admin/reserve`·`UNKNOWN`·`OVERDUE`·대사 불일치를 들여다보기. 9단계는 이 셋을 자동화해 관리자 일을 "초대 코드 발급·지문 전달 / 알림 대응 / 예치금 요청 처리"로 줄인다.

### 13.1 설계 결정

| # | 항목 | 결정 | 이유 |
|---|---|---|---|
| 1 | 확정 조건 | 은행이 대사 결과를 보낸다(`ack` / `dispute`, run마다 은행당 한 행 `settlement_acks`, 덮어쓰기). **포지션이 있는 모든 은행이 ack하고 dispute가 없으면 그 ack 요청 안에서 확정**(`auto-ack`). 포지션 없는 은행의 ack는 기록만 | 돈이 움직이는 당사자가 모두 맞다고 한 순간이 확정 시점이다. 거래가 없는 은행은 확인할 것이 없다 |
| 2 | 확정 마감 | 매일 `SETTLEMENT_CONFIRM_DEADLINE`(09:00 KST)에 dispute가 없으면 ack가 모자라도 확정(`auto-deadline`) | 대사는 선택(SPEC 10절)이다. 대사를 하지 않는 은행(가짜 은행 902 포함) 때문에 확정이 멈추면 매일 관리자가 다시 손을 대야 한다. 00:05 배치 뒤 9시간이면 은행이 맞춰 볼 시간으로 충분하다 |
| 3 | dispute | 누가 보냈든 자동 확정을 막는다. 관리자가 확정(강제) 또는 취소. 같은 은행이 ack를 다시 보내면 풀린다. 취소해도 ack 기록은 남는다 | 불일치 판정은 증거를 보는 사람의 일이다. 은행이 자기 쪽 원인을 고친 경우를 위해 되돌릴 수 있게 한다 |
| 4 | DRAFT에만 | ack·dispute는 `DRAFT`에만(그 밖은 409 `SETTLEMENT_NOT_DRAFT`) | `SETTLED`는 바뀌지 않는다(2절 8번). 확정 뒤의 불일치는 관리자에게 직접 알리고 예치금 정정으로 |
| 5 | 동시성 | ack 처리는 run 행을 `FOR UPDATE`로 잠근다(`confirm`·`cancel_draft`와 같은 잠금) | 마지막 ack 두 개가 동시에 와도 확정은 한 번. 뒤의 요청은 409를 받고 10.5로 상태를 본다 |
| 6 | 셀프 시험 | 허브가 **가짜 은행(`SIM_BANK_CODE`) 명의**로 `accept_and_deliver`를 그대로 부른다(`from_account 902-TEST-0001`, `sender_name "ClearingHub 시험"`). 새 칸 없이 보낸 은행·`from_account`로 셀프 시험 거래를 가린다 | 보통 이체와 같은 경로라 상태 머신·재시도·증거·정산이 따로 생기지 않는다. 902는 스스로 보내지 않으므로 902가 보낸 거래는 셀프 시험뿐이다 |
| 7 | 셀프 시험 정산 | 셀프 시험 거래도 정산에 들어간다(902 → 시험 은행) | 모의 예치금이라 문제없고, 대사도 보통 거래처럼 시험된다. 902 한도가 모자라면 503 `SIM_BANK_UNAVAILABLE` + 알림 `sim_bank`(관리자가 902에 예치) |
| 8 | resend | 같은 `transfer_id`의 `/receive`를 한 번 더(허브 상태 그대로, 증거는 남김). **`COMPLETED` 거래만**(아니면 409 `TRANSFER_NOT_COMPLETED`) | 거절한(`FAILED`) 거래를 다시 보내면 은행이 이번에는 입금할 수 있는데 허브에는 실패로 남아 장부가 어긋난다 |
| 9 | 알림 | `core/alerts.notify(kind, text, **context)`: `ALERT_WEBHOOK_URL`이 있으면 `{"text","content"}`(Slack·Discord 겸용) POST, 5초, `asyncio.create_task`로 fire-and-forget, 실패는 `logger.warning`만. 동기 함수(`def`)이고 Task를 돌려준다 | 알림은 부가 기능이라 웹훅이 죽어도 이체·정산이 기다리거나 실패하면 안 된다. 동기 함수라 호출한 쪽이 await를 잊어 막힐 일이 없다 |

### 13.2 허브에서 바뀐 것

| 영역 | 내용 |
|---|---|
| 데이터 | 마이그레이션 `a9d4e2c6b8f1`: `settlement_acks(run_id FK, bank_code FK, result, transfer_ids_json, message, created_at, updated_at)`, PK (`run_id`, `bank_code`) |
| 정산 | `settlement/auto_confirm.py`: `record`(잠금 → DRAFT 확인 → 덮어쓰기 → dispute면 알림, ack면 `ack_state`가 전원 ack일 때 `confirm(confirmed_by="auto-ack")`), `confirm_at_deadline`(dispute 있으면 `deadline_held` 알림만, 없으면 `auto-deadline` 확정). `service.list_for_bank`·`bank_view`가 내 ack도 읽는다(`my_ack`). `get_run`이 `acks`를 함께 읽는다(관리자 상세 `acks`·`disputed`) |
| 워커 | `workers/settlement.py`: `next_deadline`, `run_deadline`(트랜잭션 1개), `needs_deadline_catch_up`(오늘 마감이 지났는데 그 전에 만든 DRAFT가 있으면). `settlement_loop`는 `next_cutoff`와 `next_deadline` 중 가까운 쪽까지 자고, 시작 때 마감 catch-up → 배치 catch-up 순으로 본다(놓친 마감의 DRAFT가 배치를 막지 않게). `run_daily`가 DRAFT 때문에 건너뛰면 알림 `batch_skipped` |
| API | 은행용 `POST /v1/settlements/{run_id}/ack`·`/dispute`(`{transfer_ids, message?}`) → `{run_id, status, settled_at, my_ack}`, `GET /v1/settlements`·`/{run_id}`에 `my_ack`. 셀프 시험 `POST /v1/test-receive`(3.3 형식 + `receiver_reply`), `POST /v1/test-receive/{id}/resend`, `GET /v1/test-receive/{id}/status`(`{transfer_id, hub_status, receiver_reply}`). 관리자 `GET /admin/settlements/{id}`에 `acks`·`disputed` |
| 알림 (`kind`) | `register`(셀프 등록, ping 결과), `ping_failed`(`PATCH /v1/me`), `dispute`, `auto_confirm`(ack·deadline, 은행 수·금액), `deadline_held`, `batch_skipped`, `overdue`(`confirm`), `unknown`(`_confirm_one`의 UNKNOWN 전이), `self_test`(은행 5xx·응답 서명 실패), `sim_bank`(902 없음·비활성·한도 부족) |
| 설정 | `SETTLEMENT_CONFIRM_DEADLINE`(`09:00`), `SIM_BANK_CODE`(`902`), `SELF_TEST_DAILY_LIMIT`(20), `ALERT_WEBHOOK_URL` |
| 그대로 | `SETTLEMENT_AUTO_CONFIRM`(생성 즉시 확정, 시연용), 거래 0건 run 즉시 확정, 관리자 `confirm`·`cancel` |
| 문서 | SPEC v1.1(3.7, 10.8, 10.1·10.3·10.4·10.5·10.6·10.7, 7절 7, 5절 표, 8절 8·20·21, 체크리스트), ONBOARDING(관리자 일 셋, 알림 대응 표), DEPLOY(새 설정, 9단계 배포), DEMO(자동 확정 한 줄), `.env.example` |
| 테스트 | `tests/test_settlement_ack.py`(전원 ack·일부 ack·포지션 없는 은행·dispute와 다시 ack·검증·DRAFT만·마감 확정/보류·관리자 강제 확정·`next_deadline`·catch-up·루프), `tests/test_self_test.py`(성공·`0002` 거절·resend 멱등·남의 거래 403·`status`·하루 한도·검증·902 없음/비활성/한도 부족 503·5xx·서명 실패 알림·정산 포함), `tests/test_alerts.py`(본문 키·URL 없음·루프 밖·실패와 지연에도 예외 없이 바로 돌아옴·길이 자르기·셀프 등록·ping 실패·`OVERDUE`·`UNKNOWN`). conftest의 `alert_sink`(httpx MockTransport) |

### 13.3 AvengersBank에서 바뀐 것 (`feature/backend-api`)

`settlement_sync.reconcile_run`이 대사 결과를 허브에 보낸다. 허브 `status`가 `DRAFT`일 때만(그 밖은 409라 보내지 않는다), run마다 한 번: `MATCHED`면 `hub_client.ack_settlement(run_id)`, `MISMATCH`면 `hub_client.dispute_settlement(run_id, hub_only + differs, message)`. `settlement_reconciliations`에 `acked_at`·`disputed_at`(보낸 시각, 마이그레이션 1개). 결과가 바뀌면(MISMATCH → MATCHED) 다시 보낸다. ack 응답의 `status`가 `SETTLED`(전원 ack로 즉시 확정)거나 409 `SETTLEMENT_NOT_DRAFT`(그사이 확정·취소됨)면 다음 주기를 기다리지 않고 상세를 다시 받아 장부 정리까지 한다. 보내지 못하면(응답 없음·5xx) 시각을 남기지 않아 다음 주기에 다시 보낸다 (대사·장부 정리는 그대로 돈다).

### 13.4 관리자에게 남는 일 (최종)

1. **초대 코드 발급·허브 공개키 지문 전달** (새 팀마다, ONBOARDING 1)
2. **알림 대응**: `dispute`·`deadline_held`·`batch_skipped`(판정: 확정 또는 취소), `overdue`(예치금·한도), `unknown`(recheck·resolve), `ping_failed`·`register` ping 실패·`self_test`(그 은행에 알림), `sim_bank`(902 예치)
3. **예치금·한도 요청 처리** (은행이 요청할 때, ONBOARDING 4)
4. 드문 일: 키 유출·분실(ONBOARDING 6), `SETTLED` 뒤에 은행이 알려 온 불일치(예치금 정정)

### 13.5 한계

- 마감 시각 확정은 ack하지 않은 은행이 있어도 된다. 대사를 늦게 하는 은행은 `SETTLED`를 맞춰 보게 되고, 그때의 불일치는 dispute로 보낼 수 없다 (관리자에게 직접)
- 관리자가 마감 직전에 만든 즉시 마감 DRAFT도 그 마감에 확정된다 (은행이 맞춰 볼 시간이 짧다). 시연용 기능이라 막지 않았다
- 알림은 트랜잭션 커밋 전에 나갈 수 있다 (예: 확정의 `overdue`). 커밋이 실패하면 알림만 남지만 같은 사건의 오류 로그가 함께 남는다
- 셀프 시험 하루 한도는 잠그지 않고 센다 (동시 요청 몇 건은 넘을 수 있다. 금액이 작아 문제없다)

---

## 14. 10단계: 연동 진단기 (허브, 2026-11-06)

**왜.** 9단계로 관리자 일은 셋으로 줄었지만, 다른 팀이 연동할 때 관리자 시간을 가장 많이 먹는 것은 "왜 401이 나요?"다. 원인은 늘 같은 몇 가지다: 서명 PATH(`/v1` 빠짐, 쿼리, 끝 `/`, 은행 쪽은 `/interbank` 포함), JSON을 다시 직렬화해 본문 바이트가 바뀜, 경로 뒤 `\n` 누락, 메서드 소문자, 밀리초 타임스탬프, hex·base64url 서명, 다른 키, 시계 차이, 응답 서명에 요청 서명을 안 넣음. 관리자는 매번 팀의 서명 코드를 같이 읽어야 했다. 10단계는 허브가 실패를 기록하고 **규칙으로 원인을 찾아** 401 응답에 바로 알려 주게 해, 은행이 관리자 없이 고치게 한다.

### 14.1 설계 결정

| # | 항목 | 결정 | 이유 |
|---|---|---|---|
| 1 | 규칙으로 진단 | 진단은 `core/signature_doctor.py`가 한다. 실패한 요청을 흔한 실수 변형(PATH × 본문 직렬화 × 구분자 × 메서드 × 서명 인코딩 × 키)으로 **다시 계산해 등록된 공개키로 실제로 검증**하고, 맞는 변형을 1순위 가설로 낸다(`verified: true`). 바꾼 것이 적은 조합부터, 3개까지 | Ed25519 검증이 맞으면 그 변형이 원인이라는 것은 증명이다. 같은 입력에 같은 답이 나오고, 비용이 없다(실패 한 건 최악 수십 ms) |
| 2 | 401 `hint` | API 키가 맞아 은행이 식별된 401이면 기록한 실패를 그 자리에서 진단해 1순위 가설 한 줄을 오류 본문 `hint`(선택 필드, SPEC 5절)에 넣는다. `SIGNATURE_EXPIRED`면 시계 차이(초). 키가 틀린 요청에는 넣지 않는다. 진단은 스레드에서 1초 안에, 실패하면 `hint`만 뺀다 | 은행이 401을 받은 그 자리에서 원인을 본다. 외부 스캐너에는 서명 규칙을 알려 주지 않는다. 1절 "모르는 필드 무시"라 호환된다 |
| 3 | 실패 기록 | `auth_failures`: `current_bank`의 모든 401(BANK_TO_HUB)과 허브 → 은행 호출에서 은행이 401·403을 주거나 응답 서명이 틀린 것(HUB_TO_BANK). 서명 헤더·X-Request-ID·본문 원문(64KB), HUB_TO_BANK는 은행 응답의 상태·서명 헤더·본문도. **`X-Api-Key` 값은 저장하지 않는다**(보냈는지만). 은행당 최근 200건, 식별 못 한 것은 1000건 | 서명을 다시 계산하려면 받은 바이트가 그대로 있어야 한다. API 키는 비밀이라 남기지 않는다 |
| 4 | 기록은 best-effort | 요청과 다른 세션으로 쓰고 예외는 로그만. BANK_TO_HUB는 3초 안에서 기다리고, HUB_TO_BANK는 `create_task`로 띄운다(호출한 쪽이 트랜잭션 안일 수 있다) | 기록 실패가 401 응답·이체를 막으면 안 된다 |
| 5 | 식별 방식 | 은행용 `GET /v1/diagnose/failures`는 **서명 없이 `X-Bank-Code`+`X-Api-Key`**(`identified_bank`). 읽기 전용이고 자기 은행으로 식별된 실패만, 횟수 제한 없음 | 서명이 안 되는 상태에서 쓰는 API다. API 키는 식별용이지만 이 API는 아무것도 바꾸지 않고 자기 실패만 보여 주므로 충분하다 |
| 6 | 남의 실패 | API 키가 틀린 요청은 `bank_code` NULL로 남아 어느 은행도 못 본다. 관리자 `GET /admin/banks/{code}/auth-failures`만 `claimed_bank_code`(헤더 값)로 묶어 본다 | 누구나 남의 은행 코드를 헤더에 넣을 수 있다. 그 실패를 그 은행에 보여 주면 남이 보낸 요청이 섞인다 |
| 7 | 알림 | `ping_failed`·`register`(ping 실패)·`self_test`에 규칙 진단 1순위 가설 한 줄(`원인 후보(확인됨/추정): …`) | 관리자가 알림만 보고 은행에 무엇을 고치라고 할지 안다 |

**LLM 설명은 넣었다가 뺐다.** 처음에는 진단 결과·SPEC 발췌·은행이 붙여 넣은 코드를 LLM에 넘겨 한국어 설명을 받는 `POST /v1/diagnose`를 두었지만(읽기 전용, 키가 없으면 규칙 결과만), 규칙 진단이 이미 원인·고칠 곳·SPEC 절을 주므로 LLM은 그것을 말만 바꿔 되풀이할 뿐이었고, 키·의존성·하루 한도·프롬프트 관리만 늘었다. 그래서 규칙 진단·실패 기록·401 `hint`만 남겼다. 실패 기록이 쌓여 `hint`만으로 부족하다는 근거(예: `verified: false`로 끝나는 실패가 많고, 같은 은행이 같은 401을 계속 내는 경우)가 생기면 그때 다시 검토한다.

### 14.2 허브에서 바뀐 것

| 영역 | 내용 |
|---|---|
| 데이터 | 마이그레이션 `b7e3f1a9c2d4`: `auth_failures`(direction, claimed_bank_code, bank_code, reason, message, method, path, query, url, headers_json, api_key_sent, body, body_truncated, http_status, response_headers_json, response_body, response_signature_valid, created_at). FK 없음(로그 표) |
| 기록 | `domains/support/failures.py`: `record_bank_to_hub`(`core/auth.current_bank`가 401마다, 만든 실패를 돌려준다), `failure_from_reply`·`record_reply`(`core/bank_client._request`가 응답마다. `Exchange.url` 추가), 오래된 것 정리, `recent`, `drain`(lifespan 종료 때) |
| 진단기 | `core/signature_doctor.py`: `diagnose(failure, DoctorContext) -> Diagnosis`(`hypotheses`: `code`·`title`·`detail`·`spec_ref`·`fix`·`verified`). BANK_TO_HUB `SIGNATURE_INVALID`(변형 검색), `SIGNATURE_EXPIRED`(차이 초, 밀리초), `UNAUTHORIZED`(빠진 헤더, API 키), `BANK_INACTIVE`, 공개키 없음. HUB_TO_BANK: 은행 오류 코드별 가설(`BANK_VERIFY_PATH`·`_HUB_KEY`·`_RAW_BODY`·`BANK_CLOCK`·`BANK_HEADER_CHECK`·`BANK_FORBIDDEN`), 응답 서명(헤더 없음·알고리즘·요청 서명 빠짐·STATUS·본문 다시 직렬화·인코딩·만료·다른 키를 다시 계산) |
| 진단 결과 | `domains/support/service.py`: `failure_findings`, `auth_hint`(401 `hint`, `core/auth._hint`가 스레드에서 1초 안에), `reply_hint`(알림 한 줄) |
| API | 은행용 `GET /v1/diagnose/failures`(20건). 관리자 `GET /admin/banks/{code}/auth-failures?limit=`(식별 못 한 X-Bank-Code 일치 건 포함). 401 본문 `hint` |
| 그 밖 | 초대 문구(`POST /admin/invites`)에 "401이 나면 hint → `GET /v1/diagnose/failures`" 한 줄. 새 설정·의존성 없음 |
| 문서 | SPEC v1.2(3.8 연동 진단, 3절 API 표, 5절 `hint`, 7절 8 "401이 나면: hint → failures → 관리자", 8절 22, 체크리스트, 루트 `/` `v1.2`), ONBOARDING 7(401 문의 → 관리자 목록을 그대로 보내기), DEPLOY(10단계 배포), README |
| 테스트 | `tests/test_signature_doctor.py`(실수마다 일부러 틀린 서명을 만들어 그 가설이 1순위·`verified`인지: PATH 5가지, 본문 6가지, 구분자 3가지, 서명 인코딩 4가지, 만료 키, 다른 키, 두 실수 동시, 2.3 예시 1, 시계·밀리초, 헤더·API 키, 응답 서명 3가지, 은행의 401·403 4가지), `tests/test_support.py`(기록에 API 키 없음, 내 실패만, 식별 못 한 실패는 관리자만, 401 종류별 기록, 기록 실패해도 401, 200건 유지·본문 자르기, 관리자 API, `POST /v1/diagnose` 없음, HUB_TO_BANK 응답 서명·은행 401·요청 서명 빠진 응답, 성공은 안 남김, 알림 한 줄, 401 `hint` 식별·만료·키 틀림·진단 실패) |

### 14.3 한계

- 은행 쪽 검증(HUB_TO_BANK 401)은 허브가 다시 계산할 수 없다 (은행이 무엇으로 검증했는지 모른다). 그래서 그 가설은 모두 `verified: false`이고 흔한 순서(PATH → 허브 공개키 → 원본 바이트)로 낸다
- 변형 검색은 한 조합에서 3개까지 바꿔 본다. 그보다 많이 틀렸거나 목록에 없는 실수는 "다른 키로 서명"·"메시지 구성이 전혀 다름"(추정)으로 나온다. 다른 키와 메시지 오류는 구분할 수 없다 (서명은 둘 다 그냥 틀린다)
- 진단은 지금 등록된 키로 한다. 실패 뒤에 키를 바꿨으면 결과가 그때와 다를 수 있다 (`NOW_VALID`)
- `hint`는 식별된 은행에게 서명 규칙의 틀린 곳을 알려 준다. API 키를 가진 쪽만 받으므로 받아들인다
- 401이 몰리면 기록용 별도 커넥션이 풀(5+5)을 압박할 수 있다

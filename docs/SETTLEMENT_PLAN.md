# ClearingHub 정산 구현 계획서 — 상계·대사·예치금 결제

- 작성일: 2026-11-06
- 상태: 초안. [PROJECT_PLAN.md](PROJECT_PLAN.md) 3.3절("정산은 표시만")과 [SPEC.md](SPEC.md) 8절 8번("정산 시각·방식, 미구현")을 이 문서로 대체한다
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
| 10 | 대사 파일 전달 | `GET /v1/settlements/{run_id}` (은행용, 5단계), `GET /admin/settlements/{run_id}/transfers.csv` (3단계 완료) | 일부 |
| 11 | 결제 지시 | `settlement/service.py: confirm()` → `reserve.service.apply_instruction(run_id, instruction_for(run))` | 이 계획 (`instruction_for`는 3단계에 있음) |
| 12 | 중앙은행 원장 반영 | `reserve/service.py` — `reserve_accounts.balance` 갱신 + `reserve_ledger` 분개 (한 트랜잭션) | 이 계획 |
| 13 | 은행 장부 정리 | AvengersBank `interbank/settlement_sync.py` — 대사 → `SETTLE` 분개 | 이 계획 |
| (사전) | 당좌예금 예치, 순채무한도 | `reserve` 도메인, `banks.net_debit_cap`, `transfers.service.accept_and_deliver()` 검사 | 이 계획 |
| (신뢰) | 전자서명·부인방지 | `core/signing.py` Ed25519, 응답 서명, `transfer_evidence` (12절, 8단계) | 이 계획 |

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

### 4.3 AvengersBank — 변경

| 항목 | 변경 |
|---|---|
| `LedgerAccount` | `HUB_RESERVE = "HUB_RESERVE"` 추가: 우리가 허브에 맡긴 예치금 |
| `LedgerEvent` | `SETTLE = "SETTLE"` 추가: 정산 계정 → 예치금 계정 |
| `interbank_ledger` | `settle_date` (date nullable, index) 추가: 어느 날 정산에서 맞춰졌는지. NULL이면 미정산 |
| 새 테이블 `settlement_reconciliations` | `settle_date` PK, `hub_run_id`, `hub_net_amount`, `our_net_amount`, `result` (`MATCHED` / `MISMATCH` / `HUB_UNAVAILABLE`), `diff_json`(안 맞는 `transfer_id` 목록), `checked_at` |

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
 │      SETTLEMENT_AUTO_CONFIRM=true 면 이 단계를 건너뛰고 바로 11로
 │
 ├ 11. confirm(run)                [트랜잭션 2]
 │      run.status가 DRAFT인지 SELECT ... FOR UPDATE 로 확인 (두 번 확정 방지)
 │      instruction = [(bank_code, net_amount) ...]  ← positions에서 만든다
 │      reserve.apply_instruction(run_id, instruction)   ← reserve 도메인은 이것만 받는다
 │
 └ 12. reserve.apply_instruction   [같은 트랜잭션]
        은행마다: reserve_accounts 행 잠금 → balance += net_amount
                  reserve_ledger에 SETTLE_CREDIT / SETTLE_DEBIT 분개
                  result = OK / OVERDRAWN(balance < 0) / OVERDUE(balance < −cap)
        run.status = SETTLED, settled_at
        OVERDUE 은행이 있으면 logger.error (관리자 알림)
```

`reserve.apply_instruction()`의 입력은 `(run_id, [(bank_code, amount)])`뿐이다. 지시서는 `settlement.service.instruction_for(run)`이 포지션에서 만든다(은행 코드순). 이 함수를 HTTP 호출로 바꾸면 예치금 원장을 별도 서버로 뗄 수 있다 (2절 1번).

### 5.2 접수 때 순채무한도 검사

`transfers.service.accept_and_deliver()`에서 `LIMIT_EXCEEDED`(1회 상한) 검사 바로 뒤에 4.4의 검사를 넣는다. 거절 메시지: `"순채무한도를 넘었습니다 (예치금 X원 + 한도 Y원, 미정산 순채무 Z원)"`. 접수 전 거절이므로 SPEC 3.3의 규칙대로 `status: "FAILED"`가 붙고, 보내는 은행은 출금을 되돌린다.

### 5.3 은행 쪽 대사·장부 정리 (13단계, AvengersBank)

```
interbank/settlement_sync.py   매일 00:30 KST (허브 배치 뒤)
 1. GET /v1/settlements  (서명, SPEC 2절) → 아직 맞춰 보지 않은 run을 run_id 순으로
    GET /v1/settlements/{run_id}  (한 날짜에 run이 여럿일 수 있으므로 날짜가 아니라 run 단위로 대사한다)
    - 새 run이 없음 → 허브가 아직 안 돌렸음. 1시간 뒤 다시 (최대 24회)
    - DRAFT → 대사만 하고 SETTLE 분개는 하지 않음. SETTLED가 될 때까지 다시 확인
 2. 우리 장부 계산: 허브가 준 transfer_id 목록을 **기준**으로 interbank_ledger에서
    아직 정산 표시가 없는 COMPLETE(+)·RECEIVE(−) 분개를 찾아 은행별로 합산
    (날짜로 자르지 않는다. 허브가 "미정산" 기준이라 며칠 전 거래가 들어올 수 있다)
    → our_net_amount. 부호는 허브 규칙(받을 돈 − 줄 돈)으로 바꾼다.
      INTERBANK_SETTLEMENT 계정은 "+ 줄 돈 / − 받을 돈"이므로 our_net = −(분개 합)
    추가로 우리 장부에 정산 표시가 없는데 허브 목록에 없는 COMPLETE·RECEIVE 분개도 찾는다
 3. 비교
    - 포지션 합계가 같고, transfer_id 집합도 같음 → MATCHED
    - 허브 목록의 ID가 우리 interbank_transfers에서 아직 PENDING이면 → 불일치가 아니라
      "확인 중". 그 거래가 확정될 때까지 1시간 뒤 다시 (허브 202 뒤 결과 조회가 늦은 경우)
    - 그 밖에 다르면 MISMATCH + diff_json(허브에만 있는 ID / 우리에게만 있는 ID / 금액 다른 ID)
      logger.error, 자동 처리 중단, 관리자 확인
 4. MATCHED이고 허브가 SETTLED이면 [트랜잭션]
    은행별 SETTLE 분개: INTERBANK_SETTLEMENT −net, HUB_RESERVE +net
    (transfer_id 대신 "settle:{date}:{bank}"를 키로 써서 두 번 기록 못 하게)
    맞춘 COMPLETE·RECEIVE 분개에 settle_date를 표시해 다음 대사에서 다시 세지 않게 한다
    (interbank_ledger에 settle_date 컬럼 추가, 4.3절)
 5. settlement_reconciliations에 결과 저장
```

MISMATCH가 바로 이 계획의 목적이다. 받는 은행이 입금 없이 `COMPLETED`라고 답했거나(허브에만 있음), 보내는 은행이 출금 없이 보냈거나(우리 장부에 DEBIT 없음) 하면 여기서 드러난다.

---

## 6. API

### 6.1 은행용 (SPEC에 추가, v0.6)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/settlements` | 최근 30일 정산 목록: `run_id`, `settle_date`, `cutoff_at`, `status`, 내 `net_amount`, `result`. 한 날짜에 run이 여럿일 수 있다(취소된 run 포함) |
| GET | `/v1/settlements/{run_id}` | **대사 파일.** 내 포지션(`sent_*`, `recv_*`, `net_amount`, `result`, `balance_after`) + 내가 보내거나 받은 거래 목록(`transfer_id`, 방향, 상대 은행, `amount`, `finished_at`). 없으면 404 `SETTLEMENT_NOT_FOUND` |
| GET | `/v1/reserve` | 내 예치금: `balance`, `net_debit_cap`, `exposure`(4.4), `available = balance + cap − exposure` |

모두 2절 인증, 자기 은행 것만 본다. 다른 은행의 포지션·잔액은 보여 주지 않는다.

### 6.2 관리자용

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/admin/settlements/run` | `{settle_date?}` 정산 DRAFT 생성 (8·9단계) → 201 run 요약 + 포지션 + 쌍별 표. 생략하면 어제, 오늘이면 즉시 마감, 내일 이후 422 `VALIDATION_ERROR`. DRAFT가 남아 있으면 409 `SETTLEMENT_DRAFT_EXISTS`(`run_id`) |
| GET | `/admin/settlements` | 목록, 최신순 (`limit`, 기본 30) |
| GET | `/admin/settlements/{run_id}` | 포지션 전체(은행 코드순) + 쌍별 표. 없으면 404 `SETTLEMENT_NOT_FOUND` |
| GET | `/admin/settlements/{run_id}/transfers` | 거래 목록 JSON (`transfer_id`, `from_bank`, `to_bank`, `amount`, `finished_at`, `holder_name`) |
| GET | `/admin/settlements/{run_id}/transfers.csv` | 같은 내용 CSV (UTF-8, BOM 없음, 헤더 행, `settlement_{run_id}.csv`) |
| POST | `/admin/settlements/{run_id}/cancel` | DRAFT 취소: 거래를 미정산으로 되돌림. DRAFT가 아니면 409 `SETTLEMENT_NOT_DRAFT` |
| POST | `/admin/settlements/{run_id}/confirm` | 확정 (11·12단계). `{"reason": ...}` |
| GET | `/admin/reserve` | 은행별 예치금·한도·exposure |
| POST | `/admin/reserve/{bank_code}/deposit` | `{amount, memo}` 예치 (DEPOSIT 분개) |
| POST | `/admin/reserve/{bank_code}/withdraw` | `{amount, memo}` 인출. `balance − amount < −cap` 이면 거절 |
| POST | `/admin/reserve/{bank_code}/adjust` | `{amount, memo}` 정정 분개 (양수·음수). 사유 필수 |
| GET | `/admin/reserve/{bank_code}/ledger` | 분개 목록 |
| PATCH | `/admin/banks/{bank_code}` | 기존 API에 `net_debit_cap` 필드 추가 |

### 6.3 SPEC 변경 (v0.6)

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
| 0. 규격·설정 | 0.5일 | SPEC v0.6 초안(6.3), `config.py`에 `settlement_cutoff_time`(기본 `00:05`), `settlement_timezone`(`Asia/Seoul`), `settlement_auto_confirm`(기본 `false`) | 팀 합의 |
| 1. 예치금 원장 | 2일 | `domains/reserve/` (models·schemas·service·router), Alembic `reserve_accounts`·`reserve_ledger`·`banks.net_debit_cap`, 관리자 입출금·정정 API, `GET /v1/reserve` | 입금·인출·정정이 분개와 잔액에 같이 남고, 인출 한도 검사가 된다 |
| 2. 순채무한도 검사 | 1일 | `transfers/service.py` 4.4 검사, 은행 등록 시 `reserve_accounts` 행 자동 생성 | 예치금 0·한도 0인 은행의 이체가 `LIMIT_EXCEEDED FAILED`로 거절되고, 예치금을 넣으면 된다. 기존 테스트는 conftest에서 한도를 넉넉히 줘 그대로 통과 |
| 3. 정산 배치 (DRAFT) **완료** | 2일 | `domains/settlement/` models·service(`create_draft`·`cancel_draft`·`instruction_for`), Alembic `settlement_*`·`transfers.settlement_run_id`·`reserve_ledger.run_id` FK, `POST /admin/settlements/run`, 취소, 조회 API, CSV, `exposure`의 미정산 조건, `_set_status` 방어 | 손으로 계산한 하루치 상계와 같다. `Σ net = 0`. DRAFT가 있는데 또 돌리면 409, 취소하면 다시 돌릴 수 있다. 늦게 COMPLETED된 거래가 다음 정산에 들어간다 |
| 4. 확정·예치금 반영 | 1.5일 | `confirm()` → `reserve.apply_instruction()`, `workers/settlement.py`(매일 자동 DRAFT, 설정 시 자동 확정), `OVERDRAWN`·`OVERDUE` 처리 | 확정 뒤 잔액이 포지션만큼 바뀌고 분개가 남는다. 두 번 확정 불가. `OVERDUE` 은행은 송금이 막히고 받기는 된다 |
| 5. 은행용 대사 API | 1일 | `GET /v1/settlements`, `/v1/settlements/{run_id}` | 은행이 자기 거래만 본다. 다른 은행 포지션은 안 보인다 |
| 6. AvengersBank 대사·정리 | 2일 | `LedgerAccount.HUB_RESERVE`, `LedgerEvent.SETTLE`, `settlement_reconciliations`, `settlement_sync.py` 워커, `hub_client.get_settlement()`, 관리자 조회 API 1개 | 정상 하루치가 `MATCHED`되고 `SETTLE` 분개가 한 번만 생긴다. 허브 기록을 일부러 하나 지우면 `MISMATCH`와 diff가 나온다 |
| 7. 시연·문서 | 1일 | README·DEPLOY 갱신, 시연 대본(8절), 가짜 은행 `902` 예치금 시드 | 8절 시나리오가 끝까지 돈다 |
| 8. 부인방지 | 3일 | 12절 전체: Ed25519 요청·응답 서명, 키 등록·회전, 전환 기간, 증거 보관, SPEC v1.0 | 허브가 은행 서명을 만들 수 없고, 받은 응답으로 상대의 "답한 사실"을 증명할 수 있다 |

8단계는 돈의 흐름(1~7단계, 실제 금융망의 13단계)이 아니라 **신뢰 장치**다. 1~7과 독립이라 2단계 뒤 언제든 할 수 있지만, 규격이 깨지는 변경이라 다른 팀과 날짜를 맞춰야 한다 (12절).

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
| S8 | 은행 API로 다른 은행 포지션 요청 | 자기 것만 나옴 |
| S9 | 정산된 거래에 `_set_status` / `resolve_unknown` | `RuntimeError` / 불가 (UNKNOWN이 아니므로 기존 409) |
| E1 | DRAFT 뒤·확정 뒤 순채무 (4.4) | DRAFT로는 순채무가 그대로, 확정 뒤 0이 되고 그만큼 예치금이 바뀐다. `available`은 그대로 |

AvengersBank `tests/test_settlement_sync.py`:

| 번호 | 시험 | 기대 |
|---|---|---|
| B1 | 가짜 허브 응답 = 우리 장부 | `MATCHED`, `SETTLE` 분개 생성, 정산 계정 잔액 0 |
| B2 | 같은 날짜 두 번 돌림 | 분개 한 번만 |
| B3 | 허브 목록에 우리에게 없는 `transfer_id` | `MISMATCH`, diff에 그 ID, 분개 없음 |
| B4 | 허브 DRAFT | 대사 기록만, 분개 없음 |
| B5 | 허브 404 | `HUB_UNAVAILABLE`, 다음 주기에 다시 |

---

## 9. 시연 시나리오

1. **예치**: 관리자가 `901`(AvengersBank)에 100만 원, `902`(SimBank)에 100만 원 예치. `/admin/reserve`에서 잔액 확인
2. **한도 검사**: `903`(예치 0)을 등록해 이체 → `LIMIT_EXCEEDED` "순채무한도를 넘었습니다". 예치 뒤 성공
3. **하루치 거래**: 모아에게 "SimBank 110-123-0001로 5만 원" 2번, 관리자가 `902`에서 `901`로 3만 원 1번 (가짜 은행은 보내지 못하므로 허브 API로 직접)
4. **마감 상계**: `POST /admin/settlements/run` → 포지션 표: 901 −7만, 902 +7만. 쌍별 표. CSV
5. **대사**: AvengersBank 관리 API에서 `settlement_reconciliations` 조회 → `MATCHED`
6. **확정**: `confirm` → `/admin/reserve`에서 901 93만, 902 107만. 분개 2건
7. **은행 장부 정리**: AvengersBank 워커 실행 → `INTERBANK_SETTLEMENT` 0, `HUB_RESERVE` −7만 (허브 예치금이 그만큼 줄었음)
8. **속이는 은행**: 허브 DB에서 거래 하나를 지우고(또는 가짜 은행이 입금 없이 200을 주는 계좌 `0006`을 추가해) 다시 대사 → `MISMATCH`와 문제 `transfer_id`가 나온다. "데이터로만 주고받는데 어떻게 아느냐"에 대한 답이 이 화면이다

---

## 10. 리스크

| 리스크 | 대응 |
|---|---|
| 접수마다 exposure 합계 쿼리 2개가 돈다 | 인덱스 `(from_bank, settlement_run_id)`·`(to_bank, settlement_run_id)`. 미정산 거래는 하루치뿐이라 작다. 부족하면 캐시 컬럼 |
| 배치가 도는 중에 거래가 COMPLETED 됨 | `finished_at < cutoff_at`로 자르고, 늦은 것은 다음 run (2절 3번). `create_draft`의 UPDATE 한 문장으로 경쟁을 없앤다 |
| 관리자 두 명이 동시에 run | 서비스가 DRAFT를 먼저 확인하고, 둘 다 지나치면 부분 유니크 인덱스가 두 번째를 막는다 (409). 수집 UPDATE는 `settlement_run_id IS NULL` 조건이라 같은 거래가 두 run에 들어가지 않는다 |
| 허브와 은행의 날짜 경계가 다름 | 둘 다 KST, 허브 `finished_at` 기준. 은행은 허브가 준 목록을 **기준**으로 삼고 자기 장부에서 그 ID를 찾는다 (날짜로 자르지 않는다) |
| 허브 재시작 중 배치가 안 돎 | 워커 시작 때 "어제 run이 없으면 지금 만든다". 관리자 수동 실행도 있다 |
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
| 7 | 전환 | `banks.signature_mode`: `hmac` → `both` → `ed25519`. `both`에서는 둘 다 받고, 허브 → 은행 호출은 공개키가 등록돼 있으면 Ed25519로 서명한다. 은행이 준비되면 관리자가 `ed25519`로 바꾸고 HMAC을 끈다 | 2절의 키 재발급처럼 "겹치는 기간 없음"으로 하면 모든 은행이 같은 순간에 바꿔야 한다. 은행별 전환이 현실적 |
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
  direction                 REQUEST_IN (은행 → 허브 접수) | RECEIVE_OUT (허브 → 은행 /receive 요청) | RECEIVE_REPLY (은행의 응답)
  headers_json              서명 관련 헤더만 (X-Bank-Code, X-Timestamp, X-Signature, X-Signature-Alg, X-Response-*)
  body                      bytea / BLOB (원본 바이트)
  signature_valid           bool  (받을 때 검증 결과)
  created_at
```

AvengersBank: `hub_evidence` 같은 표로 허브에서 받은 `/receive` 요청 원본과 허브 응답(허브 → 은행 호출의 응답은 은행이 서명하므로 자기 서명 원문도 보관)을 저장. 키 쌍은 `.env`(`HUB_SIGNING_KEY`)에.

### 12.4 처리 흐름

```
요청 (은행 → 허브, 허브 → 은행 공통)
  보내는 쪽: message = ts\nMETHOD\nPATH\nbody → Ed25519 서명 → X-Signature, X-Signature-Alg: ed25519
  받는 쪽:   X-Signature-Alg 보고 분기
             ed25519 → 상대 공개키(현재 또는 24시간 안의 이전 키)로 검증
             없음/hmac → signature_mode가 hmac·both일 때만 HMAC 검증, ed25519면 401 SIGNATURE_INVALID
             검증 뒤 원본 바이트·헤더를 evidence에 저장 (이체 관련 요청만)

응답 (모든 2xx·4xx. 5xx는 본문 보장이 없으므로 서명도 없을 수 있다)
  답하는 쪽: message = response_ts\nSTATUS\n<요청의 X-Signature>\nresponse_body → 서명 → X-Response-* 헤더
  받는 쪽:   서명 검증 실패면 "응답 없음"으로 취급 (이체는 PENDING → 4.3 확인, 수취 조회는 RECEIVER_ERROR)
             검증 성공한 응답 바이트를 evidence에 저장
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

### 12.6 SPEC v1.0

- 2절을 "2. 인증: 공개키 서명(권장) / HMAC(전환 기간)"으로 다시 쓴다. 메시지 형식은 그대로, 서명 함수와 헤더 `X-Signature-Alg`만 추가. Python 참고 구현(`cryptography`의 `Ed25519PrivateKey`) 추가. 검증용 예시: 고정 테스트 키 쌍과 예시 1·2의 서명 값
- 새 절 **2.5 응답 서명**: 메시지 형식, 헤더, 검증 실패 시 "응답 없음" 취급
- 새 절 **2.6 키 등록·회전**: 12.5의 `/v1/keys`, 지문 확인, 24시간 이전 키
- 4절(은행이 만드는 API): 허브 요청의 Ed25519 검증, **자기 응답에 서명** 추가. 이것이 은행 쪽의 유일한 필수 변경이다
- 8절 17 갱신, 변경 이력 1.0

### 12.7 작업 분할 (세션 2개)

| 세션 | 내용 | 완료 기준 |
|---|---|---|
| 8a 허브·가짜 은행 (2일) | `core/signing.py`에 Ed25519 서명·검증·응답 서명, `core/auth.py` 분기, `bank_client.py` 응답 검증·evidence 저장, banks 모델·API, `transfer_evidence`, `bank_sim` Ed25519 검증·응답 서명, SPEC v1.0 초안 | 가짜 은행 `902`를 `ed25519`로 전환해도 기존 이체 테스트 전부 통과. `both`·`hmac`도 통과. 응답 서명 틀리면 PENDING → 4.3으로 COMPLETED |
| 8b AvengersBank (1일) | `interbank/signing.py` Ed25519, 키 등록 호출, 허브 공개키 검증, `/interbank/*` 응답 서명, evidence 보관, `.env` 키 | AvengersBank가 `ed25519` 모드로 허브와 왕복 이체 |

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
| 가짜 은행이 허브의 `signing`을 import함 | 8a에서 `bank_sim`에 참고 구현을 따로 둔다 (SPEC 7절 6과 일치) |

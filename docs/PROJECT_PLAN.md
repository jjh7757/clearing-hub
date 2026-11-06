# ClearingHub 프로젝트 계획서 — 은행 간 이체 중계 기관

- 작성일: 2026-11-05
- 인원: 미정
- 기간: 미정 (4절의 단계별 소요 일수를 기준으로 시작일이 정해지면 날짜를 채운다)
- 관련 프로젝트: [AvengersBank](../../avengers_bank/README.md) (첫 참가 은행)

---

## 1. 프로젝트 개요

### 1.1 한 줄 소개

**여러 팀이 만든 은행 서버 사이의 이체를 중계하고, 거래를 기록·추적·정산하는 중계 기관 서버와 관리 사이트.** 실제 금융결제원(금융공동망)의 역할을 부트캠프 규모로 만든 것이다.

### 1.2 배경과 문제 정의

- 각 팀의 은행 서버는 자기 DB 안에서만 이체가 된다. 다른 팀 은행으로 보내려면 은행끼리 서로 API를 맞춰야 하는데, 은행이 N개면 연동이 N×(N−1)개가 된다.
- 은행 간 이체는 "보내는 은행 출금"과 "받는 은행 입금"이 서로 다른 DB에서 일어나므로 한 트랜잭션으로 묶을 수 없다. 중간에 실패하면 돈이 사라지거나 두 번 들어갈 수 있다.
- 그래서 **모든 은행이 중계 기관 하나만 부르게** 하고, 중계 기관이 거래를 기록하고 결과를 추적하며 하루 단위로 정산한다. 은행은 규격 하나만 지키면 다른 은행 전부와 거래할 수 있다.

### 1.3 목표

| 구분 | 목표 |
|---|---|
| 필수 | 참가 은행이 지켜야 할 **연동 규격 문서** (API, 오류 코드, 서명 방식) |
| 필수 | 중계 서버: 은행 등록, 수취 조회 중계, 이체 중계, 상태 추적, 멱등 처리 |
| 필수 | 응답 없는 거래의 재시도·결과 확인 (돈이 사라지지도, 두 번 들어가지도 않음) |
| 필수 | 관리 사이트: 참가 은행, 실시간 거래 현황, 실패 거래, 정산표 |
| 필수 | 하루 단위 정산: 은행마다 받을 돈 − 줄 돈을 상계한 차액(쌍별 표는 화면용), 모의 예치금으로 결제, 은행 대사 ([SETTLEMENT_PLAN.md](SETTLEMENT_PLAN.md)) |
| 필수 | AvengersBank를 첫 참가 은행으로 연결 (타행 이체 화면·에이전트 대화) |
| 선택 | 사기 신고 계좌 공유 (은행이 신고한 계좌를 다른 은행의 FDS가 조회) |
| 선택 | 가짜 은행 서버(시뮬레이터)로 다른 팀 없이도 시연 |

### 1.4 범위에서 제외하는 것

- 실제 은행·금융망 연동, 실명 확인, 실제 돈의 이동 (정산은 허브 DB의 **모의 예치금** 숫자를 옮기는 데서 끝난다. 예치·인출은 관리자 API로만 한다)
- 손실분담·담보처분 (예치금이 모자란 은행은 송금만 막는다, SETTLEMENT_PLAN 11절)
- 카드 결제 승인 중계, 자동이체(CMS), 외화
- 은행 서버 자체 (각 팀이 만든다. 이 프로젝트는 규격과 시뮬레이터만 제공)

---

## 2. 시스템 구성

### 2.1 전체 아키텍처

```
AvengersBank ─┐                                      ┌─▶ B은행 /interbank/inquiry · /interbank/receive
B은행 ────────┼─▶  ClearingHub 서버 (FastAPI)  ───────┼─▶ C은행 /interbank/...
C은행 ────────┘   ├ banks      참가 은행 등록·키 관리   └─▶ 가짜 은행(시뮬레이터)
                  ├ inquiry    수취 조회 중계
                  ├ transfers  이체 접수 → 전달 → 결과 (상태 머신, 멱등)
                  ├ retry      미확인 거래 재시도·결과 조회 (백그라운드)
                  ├ reserve    모의 예치금 원장 (당좌예금 역할), 순채무한도
                  ├ settlement 일 단위 정산: 마감 상계 → 대사 파일 → 확정(예치금 이동)
                  └ admin      관리자 로그인·관리 API
                        │
                   PostgreSQL
                        │
                  관리 사이트 (React): 은행 목록 · 거래 현황 · 실패 거래 · 정산표
```

- 은행은 **허브만** 부른다. 은행끼리는 서로의 주소를 모른다.
- 허브는 고객의 돈을 갖지 않는다. 출금은 보내는 은행 DB, 입금은 받는 은행 DB에서 일어나고, 허브는 그 사이의 **기록·전달·확인·정산**을 맡는다. 은행 사이의 차액은 허브가 관리하는 은행별 모의 예치금에서 옮긴다.
- 은행별 호출 방향: 은행 → 허브(요청), 허브 → 은행(입금·조회 전달). 양쪽 모두 서명으로 상대를 확인한다.

### 2.2 기술 스택

| 영역 | 기술 | 비고 |
|---|---|---|
| 서버 | Python 3.13, FastAPI, SQLAlchemy(async), Alembic, PostgreSQL | AvengersBank와 같은 스택. 익숙하고 코드를 가져올 수 있음 |
| 은행 호출 | httpx (AsyncClient), 타임아웃·재시도 | 은행 서버가 느리거나 죽어도 허브는 멈추지 않음 |
| 인증 | 은행: API 키(식별) + Ed25519 요청·응답 서명 (SPEC v1.0, 허브는 은행 비밀을 갖지 않는다) / 관리자: JWT | 2.5절 |
| 배치 | APScheduler 또는 lifespan 안의 asyncio 태스크 | 재시도 1분, 정산 하루 1회 |
| 관리 사이트 | React (Vite), TypeScript | AvengersBank 프론트와 같은 구성 |
| 시뮬레이터 | 같은 저장소의 작은 FastAPI 앱 (`bank_sim/`) | 성공·계좌 없음·지연·오류를 계좌번호로 골라 재현 |
| 테스트 | pytest, httpx, aiosqlite | PostgreSQL 없이 실행 |
| 배포 | Docker, 기존 배포 환경 (GitHub Actions) | AvengersBank와 같은 방식 |

### 2.3 용어

| 용어 | 뜻 |
|---|---|
| 은행 코드 | 허브가 은행에 주는 3자리 코드. 예: `901` AvengersBank |
| 거래 ID (`transfer_id`) | 보내는 은행이 만드는 고유 ID (UUID). **멱등성 키**. 같은 ID는 한 번만 처리 |
| 전문 | 은행과 허브가 주고받는 JSON 요청·응답 |
| 미확인 거래 | 받는 은행에 전달했지만 응답을 못 받은 거래. 재시도·결과 조회 대상 |
| 정산 | 하루치 거래를 은행마다 `받을 돈 − 줄 돈` 하나로 상계하고(합은 0), 확정하면 그만큼 예치금을 옮기는 것 |
| 예치금·순채무한도 | 은행이 허브에 맡긴 모의 예치금, 예치금을 넘어 외상으로 보낼 수 있는 금액. 미정산 순채무 + 금액이 둘의 합을 넘으면 접수 거절 (`LIMIT_EXCEEDED`) |
| 대사 | 은행이 허브가 준 자기 몫의 거래 목록(대사 파일)을 자기 장부와 `transfer_id`로 맞춰 보는 것 |

### 2.4 데이터 모델

| 테이블 | 용도 | 주요 컬럼 |
|---|---|---|
| `banks` | 참가 은행 | bank_code PK, name, base_url(은행의 `/interbank` 주소), api_key_hash, public_key(Ed25519, SPEC v1.0. 처음 계획의 hmac_secret은 8c에서 지움), active, net_debit_cap(순채무한도), created_at |
| `transfers` | 중계한 이체 | transfer_id PK(은행이 준 UUID), from_bank FK, from_account, to_bank FK, to_account, amount, sender_name, memo, status, fail_code, fail_message, requested_at, delivered_at, finished_at, retry_count, settlement_run_id(정산에 들어갔으면 그 run, 그 뒤로 상태 변경 금지) |
| `transfer_events` | 상태 변경 이력 (감사용) | event_id PK, transfer_id FK, from_status, to_status, detail, created_at |
| `inquiries` | 수취 조회 기록 (선택, 통계용) | inquiry_id PK, from_bank, to_bank, to_account, result, created_at |
| `settlement_runs` | 정산 1회 (한 날짜에 여러 개 가능, DRAFT는 하나만) | run_id PK, settle_date, cutoff_at, status(DRAFT/SETTLED/CANCELLED), tx_count, total_amount, created_by, settled_at, settled_by |
| `settlement_positions` | 은행별 순 포지션 (= 결제 지시서) | (run_id, bank_code) PK, sent_count·sent_total, recv_count·recv_total, net_amount, result(OK/OVERDRAWN/OVERDUE), balance_after |
| `settlement_pairs` | 은행 쌍별 표 (화면·CSV용) | (run_id, bank_a, bank_b) PK, a_to_b_*, b_to_a_*, net_amount, net_direction |
| `reserve_accounts`, `reserve_ledger` | 모의 예치금 잔액과 모든 변동의 분개 | bank_code PK·balance / entry_id, kind(DEPOSIT/WITHDRAW/SETTLE_*/ADJUST), amount, run_id, UNIQUE(run_id, bank_code, kind) |
| `admins` | 관리자 | admin_id PK, login_id, password_hash |
| `reported_accounts` (선택) | 사기 신고 계좌 | bank_code, account_num, reported_by, reason, created_at |

계좌번호는 중계에 꼭 필요하므로 그대로 저장하되, 관리 사이트에는 뒤 4자리만 보여 준다. 예금주 이름은 조회 결과로 은행에 전달만 하고 저장하지 않는다.

### 2.5 보안

| 장치 | 내용 |
|---|---|
| 은행별 API 키 | 허브가 발급. 요청 헤더 `X-Bank-Code`, `X-Api-Key`. 해시로 저장 |
| 요청 서명 | 헤더 `X-Timestamp`, `X-Signature = base64(Ed25519(보내는 쪽 개인키, timestamp + "\n" + METHOD + "\n" + PATH + "\n" + body))` (PATH는 SPEC 2.2의 규격 경로. 처음 계획의 HMAC은 SPEC v1.0에서 Ed25519로 바꿈). 시각 차이 5분 초과면 거절 (재전송 공격 방지). 허브 → 은행 호출은 허브 키로 서명하고, 응답에도 서명한다 (SPEC 2.5) |
| 멱등성 | 같은 `transfer_id`가 다시 오면 처리하지 않고 저장된 결과를 돌려준다. 은행도 `/interbank/receive`에서 같은 규칙 |
| 사칭 방지 | `from_bank`는 요청 본문이 아니라 인증된 `X-Bank-Code`로 정한다 |
| 관리자 | JWT 로그인. 관리 API는 `/admin/*`로 분리 |
| 개인정보 최소화 | 예금주 이름 미저장, 화면에는 계좌 뒤 4자리 |
| 금액 | 정수(원). 음수·0 거절. 1회 상한(예: 1억 원)은 허브에서도 검사 |

---

## 3. 기능 상세

### 3.1 연동 규격 (참가 은행이 지킬 것)

이 절이 다른 팀에게 전달할 **규격 문서**의 뼈대다. 확정되면 `docs/SPEC.md`로 분리한다.

**은행 → 허브**

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/v1/inquiry` | 수취 조회. `{to_bank, to_account}` → `{holder_name}` 또는 오류 |
| POST | `/v1/transfers` | 이체 요청. `{transfer_id, to_bank, to_account, from_account, amount, sender_name, memo}` → `{transfer_id, status, holder_name}` |
| GET | `/v1/transfers/{transfer_id}` | 처리 결과 조회. 타임아웃 뒤 은행이 결과를 확인할 때 |
| GET | `/v1/banks` | 참가 은행 목록 (코드·이름). 화면의 은행 선택용 |

**허브 → 은행** (각 은행이 만들어야 하는 것)

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/interbank/inquiry` | `{account_num}` → `{holder_name}` / `ACCOUNT_NOT_FOUND` |
| POST | `/interbank/receive` | `{transfer_id, from_bank, from_account, to_account, amount, sender_name, memo}` → `{status: "COMPLETED"}`. **같은 transfer_id는 한 번만 입금** |
| GET | `/interbank/receive/{transfer_id}` | 입금 처리 여부. 허브가 미확인 거래를 확인할 때 |

**오류 코드 (공통)**

| 코드 | 뜻 | 은행이 할 일 |
|---|---|---|
| `BANK_NOT_FOUND` | 없는 은행 코드 | 사용자에게 안내 |
| `ACCOUNT_NOT_FOUND` | 받는 계좌 없음 | 출금 되돌림 |
| `ACCOUNT_INACTIVE` | 해지·정지 계좌 | 출금 되돌림 |
| `DUPLICATE_TRANSFER` | 같은 transfer_id, 결과 동봉 | 동봉된 결과로 마무리 |
| `AMOUNT_INVALID` | 금액 오류 | 요청 수정 |
| `SAME_BANK` | 받는 은행 = 보내는 은행 | 자체 이체로 처리 |
| `RECEIVER_TIMEOUT` | 받는 은행 응답 없음, 상태 `PENDING` | 결과 조회로 확인 (3.2) |
| `SIGNATURE_INVALID` / `UNAUTHORIZED` | 서명·키 오류 | 설정 확인 |

### 3.2 이체 중계 흐름과 상태

```
은행 A                        ClearingHub                         은행 B
 ① 사용자 승인·FDS·출금
    (중간 계정, 상태 PENDING)
 ② POST /v1/transfers ───▶ ACCEPTED (저장, 멱등 검사)
                              │ POST /interbank/receive ─────▶ ③ 입금 (멱등)
                              │◀──────── COMPLETED ────────────┘
 ④ 응답 COMPLETED ◀────── COMPLETED
    상태 완료
```

| 허브 상태 | 뜻 | 다음 |
|---|---|---|
| `ACCEPTED` | 접수·저장됨 | 받는 은행 호출 |
| `DELIVERING` | 받는 은행 호출 중 | 응답에 따라 |
| `COMPLETED` | 받는 은행이 입금 확인 | 끝 (정산 대상) |
| `FAILED` | 받는 은행이 거절 (계좌 없음 등) | 끝. 보내는 은행이 출금 되돌림 |
| `PENDING` | 응답 없음(타임아웃) | 재시도 태스크가 `GET /interbank/receive/{id}`로 확인 → COMPLETED/FAILED. N회(예: 10회) 뒤에도 모르면 `UNKNOWN`으로 관리자 확인 |

- 받는 은행 호출 타임아웃: 5초. 재시도 간격: 1분, 최대 10회.
- 은행 A는 허브 응답이 `PENDING`이거나 통신이 끊기면 `GET /v1/transfers/{id}`로 결과를 확인한다. 허브도 `PENDING`이면 은행 A는 출금을 되돌리지 않고 기다린다 (되돌렸는데 나중에 입금이 확인되면 돈이 두 배가 된다).
- 모든 상태 변경은 `transfer_events`에 남긴다. 관리 사이트의 "실패 거래" 화면이 이 기록을 보여 준다.

### 3.3 정산

구현과 자세한 설계는 [SETTLEMENT_PLAN.md](SETTLEMENT_PLAN.md) (처음 계획한 "정산표만 보여 주고 확정은 상태만 바꾼다"를 대체했다).

- **예치금과 순채무한도**: 은행마다 모의 예치금(`reserve_accounts`)과 한도(`banks.net_debit_cap`)를 관리자가 넣는다. 접수 때 `미정산 순채무 + 금액 > 예치금 + 한도`면 `LIMIT_EXCEEDED`로 거절한다
- **마감 상계(DRAFT)**: 매일 00:05(KST) 워커가, 또는 관리자가 즉시, 아직 정산되지 않은 `COMPLETED` 거래를 모아 은행별 `받을 돈 − 줄 돈`(합 0)과 쌍별 표를 만든다. 예: 901→902 5만 2건, 902→901 3만 1건 → 901 −7만, 902 +7만. CSV로 내려받는다
- **대사**: 은행은 `GET /v1/settlements/{run_id}`로 자기 몫의 거래 목록을 받아 자기 장부와 맞춰 본다 (SPEC 10절). AvengersBank는 `MATCHED`면 내부 정산 계정을 예치금 계정으로 정리한다
- **확정(SETTLED)**: 관리자가(또는 자동 확정 설정으로) 확정하면 차액만큼 예치금을 옮기고 분개를 남긴다. 확정된 거래는 바꾸지 않고, 정정은 예치금 정정 분개로만 한다

### 3.4 관리 사이트 (React)

| 화면 | 내용 |
|---|---|
| 로그인 | 관리자 JWT |
| 대시보드 | 오늘 거래 건수·금액, 성공률, 은행별 송·수신, 최근 실패 |
| 참가 은행 | 목록, 등록(코드·이름·주소), API 키 발급·재발급, 활성/비활성, 연결 상태 확인 버튼(`/interbank/inquiry` 핑) |
| 거래 조회 | 기간·은행·상태로 검색, 상세에서 `transfer_events` 타임라인 |
| 미확인·실패 거래 | `PENDING`·`UNKNOWN`·`FAILED` 목록, "다시 확인" 버튼 |
| 정산 | 정산 run 목록·포지션·쌍별 표, 실행·확정·취소, CSV, 은행별 예치금 (API는 있음, 화면은 아직) |
| (선택) 신고 계좌 | 은행이 신고한 계좌 목록 |

### 3.5 가짜 은행 서버 (시뮬레이터)

`bank_sim/`에 `/interbank/*`만 구현한 작은 앱. 계좌번호 끝자리로 동작을 고른다.

| 계좌번호 끝 | 동작 |
|---|---|
| `…0001` | 정상 입금 |
| `…0002` | `ACCOUNT_NOT_FOUND` |
| `…0003` | 8초 지연 (허브 타임아웃 → PENDING → 재시도로 COMPLETED) |
| `…0004` | 500 오류 2번 뒤 성공 |
| `…0005` | 입금은 됐는데 응답 안 함 (결과 조회로만 확인 가능. 가장 중요한 경우) |
| `…0006` | 입금하지 않고 `200 COMPLETED` (속이는 은행. 허브 기록에는 있고 가짜 은행 장부에는 없어 대사에서 드러난다) |

다른 팀 서버가 준비되기 전에도 전체 흐름을 테스트·시연할 수 있고, 다른 팀에게 "이 시뮬레이터처럼 만들면 된다"는 참고 구현이 된다.

### 3.6 AvengersBank 쪽 변경 (참가 은행 연동)

| 항목 | 변경 |
|---|---|
| 은행 코드 | 수취인·이체 요청에 `bank_code` 추가 (기본 `901` = 자행). `recipients`에도 저장 |
| 이체 분기 | `transfer_service.transfer()`에서 `to_bank != 자행`이면 타행 흐름: 출금 → 중간 계정(`INTERBANK_SUSPENSE`) → 허브 호출 → 결과에 따라 완료/되돌림. `transfers` 상태 컬럼 추가 |
| 수취 조회 | `find_account_holder`에 `bank_code` → 타행이면 허브 `/v1/inquiry` |
| 입금 받기 | `POST /interbank/inquiry`, `/interbank/receive`, `GET /interbank/receive/{id}` 추가. 허브 서명 검증. `transfer_id` 멱등 |
| FDS | 보내는 쪽 검사는 그대로. "타행 이체" 피처 추가 가능 (처음 보내는 타행 계좌) |
| 에이전트 | "B은행 123-456으로 5만 원 보내줘" → 슬롯에 은행 추가. 확인 카드에 은행 이름 표시. 허브 호출은 이체 서비스 안에서만 (LLM 도구 아님) |
| 화면 | 수취인 등록·이체 확인 카드에 은행 선택·표시. 타행 이체는 "처리 중" 상태 표시 |

---

## 4. 일정

단계별 소요 일수만 적는다 (1인 기준, 인원이 늘면 3·4·5는 나눠 맡을 수 있다). 총 **약 4주**.

| 단계 | 소요 | 산출물 | 완료 기준 |
|---|---|---|---|
| 0. 계획·규격 | 2일 | 이 문서, `docs/SPEC.md`(연동 규격), 환경·Alembic | 다른 팀과 규격 합의 |
| 1. 허브 핵심 | 4일 | banks, 서명 인증, `/v1/inquiry`, `/v1/transfers`, 상태·이벤트 저장, 멱등 | 시뮬레이터 `0001`·`0002`로 성공·실패 |
| 2. 시뮬레이터·재시도 | 3일 | `bank_sim/`, 타임아웃·재시도 태스크, 결과 조회 | `0003`·`0004`·`0005` 모두 돈이 한 번만 들어감 (테스트로 증명) |
| 3. 관리 사이트 | 5일 | 로그인, 대시보드, 은행 관리, 거래·실패 조회 | 시연 중 거래가 실시간으로 보임 |
| 4. 정산 | 약 10일 (별도 계획) | 예치금 원장, 순채무한도, 일일 run·확정, 은행 대사 API, AvengersBank 대사·장부 정리, 시연 ([SETTLEMENT_PLAN.md](SETTLEMENT_PLAN.md) 7절 1~7단계, 완료) | 하루치 상계가 손계산과 같고, 확정 뒤 예치금이 그만큼 바뀌며, 은행 대사가 `MATCHED` (`tests/test_demo_scenario.py`) |
| 5. AvengersBank 연동 | 5일 | 3.6절 전부 | 모아에게 "B은행으로 보내줘" → 확인 카드 → 승인 → 허브 → 시뮬레이터 입금 → 완료 |
| 6. 다른 팀 연동·시연 | 3일 | 규격 전달, 연결 상태 확인, 시연 대본 | 다른 팀 은행과 왕복 이체 1건 이상 |
| 7. 선택 | 남는 기간 | 신고 계좌 공유, 은행별 통계 | |

**우선순위 원칙:** 기간이 짧아지면 7 → 4(정산) → 3의 대시보드 순으로 뺀다. 1·2(멱등·재시도)와 5는 끝까지 유지한다. 중계 기관의 핵심은 화면이 아니라 "돈이 사라지지도 두 번 들어가지도 않는 것"이기 때문이다.

---

## 5. 폴더 구조

```
clearing_hub/
├── docs/
│   ├── PROJECT_PLAN.md        # 이 문서
│   ├── SPEC.md                # 참가 은행 연동 규격 (다른 팀에 전달)
│   ├── SETTLEMENT_PLAN.md     # 정산 계획서
│   ├── DEMO.md                # 정산 시연 대본
│   └── DEPLOY.md
├── src/clearing_hub/
│   ├── main.py, config.py, database.py
│   ├── core/                  # 서명 검증, 예외, httpx 클라이언트
│   ├── domains/
│   │   ├── banks/             # models, schemas, router, service
│   │   ├── transfers/         # 접수·전달·상태 머신·결과 조회
│   │   ├── inquiry/
│   │   ├── reserve/           # 모의 예치금 원장
│   │   ├── settlement/
│   │   └── admin/
│   └── workers/               # 재시도 태스크, 정산 배치
├── bank_sim/                  # 가짜 은행 서버
├── frontend/                  # 관리 사이트 (React)
├── alembic/
└── tests/
```

AvengersBank처럼 router → service → repository 계층과 `domains/*` 5개 파일 구조를 그대로 쓴다.

---

## 6. 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| 다른 팀 서버가 규격을 안 지키거나 늦음 | 연동 시연 불가 | 규격을 먼저 확정해 전달, 시뮬레이터로 시연 가능하게 유지, "연결 상태 확인" 버튼으로 어느 쪽 문제인지 바로 확인 |
| 받는 은행 응답 없음 | 돈 유실·이중 입금 | `transfer_id` 멱등 + PENDING 상태 + 결과 조회 재시도. 테스트로 `0005` 경우를 증명 |
| 허브가 죽음 | 모든 타행 이체 중단 | 은행은 PENDING으로 두고 되돌리지 않음. 허브 재시작 시 `DELIVERING`·`PENDING` 거래를 이어서 확인 |
| 은행 키 유출 | 사칭 이체 요청 | Ed25519 서명 + 시각 검사, 키 회전(`PUT /v1/keys`), 은행 비활성화 |
| 서명 방식 구현 차이 (팀마다 바이트 순서·인코딩) | 연동 실패 | SPEC에 서명 예시(입력·결과)를 넣고, 시뮬레이터 코드를 참고 구현으로 제공 |
| 시각 동기화 | 서명 시각 검사 실패 | 허용 오차 5분, 오류 메시지에 서버 시각 포함 |
| 인원 미정 | 일정 불확실 | 4절 우선순위대로 핵심(1·2·5)부터 |
| 실제 기관 사칭으로 보임 | 발표 평가 | 독자적인 이름·화면 사용. 실제 기관 이름·로고 쓰지 않음 |

---

## 7. 시연 시나리오

1. **타행 이체 성공:** AvengersBank 모아에게 "B은행 110-123-0001로 5만 원 보내줘" → 확인 카드(은행 이름 표시) → 승인 → 허브 대시보드에 거래가 뜨고 `COMPLETED` → B은행(시뮬레이터 또는 다른 팀) 잔액 반영
2. **계좌 없음:** `…0002`로 이체 → 허브 `FAILED` → AvengersBank가 출금 되돌림, 사용자에게 안내
3. **응답 없음 → 재시도:** `…0005`로 이체 → `PENDING` → 1분 뒤 재시도 태스크가 결과 조회로 `COMPLETED` 확인 → 돈은 한 번만 들어감. 관리 사이트 타임라인으로 과정을 보여 줌
4. **다른 팀 은행과 왕복:** 다른 팀 은행 → AvengersBank 입금, AvengersBank → 다른 팀 은행
5. **정산:** 예치 → 한도 초과 거절 → 오늘 거래를 즉시 마감 → 은행별 차액·쌍별 표·CSV → 대사 `MATCHED` → 확정으로 예치금 이동 → 입금 없이 완료라고 답하는 은행(`…0006`)이 대사 파일에서 드러남. 대본은 [DEMO.md](DEMO.md)
6. **FDS 유지:** 타행 이체도 보이스피싱 대화가 있으면 AvengersBank가 허브 호출 전에 차단

---

## 8. 정해야 할 것

**다른 팀과 합의할 것**
- 은행 코드 배정 방식 (허브가 배정 / 팀이 신청)
- 계좌번호 형식: 팀마다 다르므로 허브는 문자열 그대로 전달하고 검사하지 않는다 (확인 필요)
- 금액 단위 정수(원) 고정, 1회 상한
- 서명 방식과 예시, 시각 허용 오차
- `/interbank/*` 경로 이름 (팀마다 바꿀 수 있게 `base_url`만 등록할지, 경로까지 고정할지)
- 입금 받는 쪽의 수수료·한도 검사 여부 (받는 은행이 거절할 수 있는 조건 목록)

**이 프로젝트 안에서 정할 것**
- 프로젝트 이름 (예: ClearingHub, 아벤저스 금융망). 실제 기관 이름은 쓰지 않는다
- ~~배포 위치, 도메인~~ → AvengersBank와 같은 공용 서버, `clearing-hub.ai-agent-develop.cloud` ([DEPLOY.md](DEPLOY.md))
- 저장소: 별도 저장소로 만든다 (중계 기관은 특정 은행 안에 있으면 안 된다). AvengersBank에는 허브 클라이언트와 `/interbank/*`만 추가
- 시작일·인원 → 4절의 날짜와 역할 분담
- 관리자 계정 발급 방법 (시드 스크립트)
- 재시도 횟수·간격 (위 값은 초안). 정산 시각은 00:05 KST, 확정은 관리자 (설정 `SETTLEMENT_*`, [DEPLOY.md](DEPLOY.md))

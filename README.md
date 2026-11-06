# ClearingHub

은행 간 이체 중계 서버: 참가 은행들이 허브 하나만 부르면 서로 이체할 수 있게 하고, 거래를 기록·추적·정산한다. FastAPI + SQLAlchemy(async) + PostgreSQL.

| 문서 | 내용 |
|---|---|
| [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | 계획서: 배경, 구조, 데이터 모델, 일정, 리스크 |
| [docs/SPEC.md](docs/SPEC.md) | **참가 은행 연동 규격** (다른 팀에 전달하는 문서) |
| [docs/DEPLOY.md](docs/DEPLOY.md) | AWS 배포: 처음 한 번 할 일, 가짜 은행 연결, 서버 `.env`, 정산 배포 순서(예치금) |
| [docs/SETTLEMENT_PLAN.md](docs/SETTLEMENT_PLAN.md) | 정산 계획서: 예치금 원장, 순채무한도, 마감 상계, 대사, 확정, 부인방지(12절) |
| [docs/DEMO.md](docs/DEMO.md) | 시연 대본: 예치 → 한도 → 하루치 → 상계 → 대사 → 확정 → 속이는 은행 (curl과 기대 숫자) |

## 구조

```
src/clearing_hub/
├── main.py                 앱, 재시도·정산 태스크 시작
├── core/
│   ├── signing.py          HMAC·Ed25519 요청 서명, 응답 서명, 키 쌍·지문 (SPEC 2.2·2.3·2.5)
│   ├── auth.py             은행 인증(X-Signature-Alg·서명 모드별 분기), 관리자 토큰
│   ├── bank_client.py      허브 → 은행 호출 (모드별 서명, ed25519 모드 응답 서명 검증, 타임아웃)
│   ├── response_signing.py /v1 응답에 허브 응답 서명 (SPEC 2.5)
│   └── config.py, database.py, exceptions.py, exception_handlers.py, request_id.py
├── domains/
│   ├── banks/              참가 은행 등록·키 발급·서명 모드·공개키 (/admin/banks), 목록 (/v1/banks),
│   │                       허브 공개키 (/v1/hub-key), 내 공개키 등록·회전 (/v1/keys)
│   ├── accounts/           본인 명의 계좌 조회 (/v1/accounts/lookup), account_ref, 조회 기록 (/admin/account-lookups)
│   ├── inquiry/            수취 조회 중계 (/v1/inquiry)
│   ├── reserve/            예치금 원장(모의 당좌예금): 내 예치금 (/v1/reserve), 입출금·정정·분개 (/admin/reserve)
│   ├── settlement/         정산 배치: 미정산 거래 수집·다자간 차액·쌍별 표·취소·CSV, 확정(예치금 반영) (/admin/settlements), 은행용 대사 파일 (/v1/settlements)
│   └── transfers/          이체 접수(순채무한도 검사)·전달·상태 머신·결과 조회 (/v1/transfers), 관리 조회 (/admin/transfers),
│                           증거 원문 보관 (evidence.py, /admin/transfers/{id}/evidence)
└── workers/
    ├── retry.py            미확인(PENDING) 거래 확인 태스크
    └── settlement.py       일일 정산 배치 (매일 00:05 KST 어제 정산 DRAFT, 설정 시 자동 확정, 시작 때 놓친 배치 catch-up)
src/bank_sim/main.py        가짜 은행 (SPEC 4·9.4절 참고 구현, 계좌번호 끝자리로 성공·실패·지연·속임 재현, HMAC·Ed25519 검증, 응답 서명)
tests/                      서명, 인증, 이체 성공·실패·멱등·타임아웃·재시도, 예치금·순채무한도·정산·대사 API,
                            시연 시나리오 전체 (test_demo_scenario.py = docs/DEMO.md), 부인방지 K1~K10 (test_nonrepudiation.py)
```

이체 상태: `ACCEPTED → DELIVERING → COMPLETED / FAILED / PENDING → (재시도) → COMPLETED / FAILED / UNKNOWN`

## 시작하기

[uv](https://docs.astral.sh/uv/)와 PostgreSQL이 필요하다.

```bash
uv sync
cp .env.example .env
```

`.env`에 `DATABASE_URL`과 `ADMIN_TOKEN`(32자 이상)을 채운다. 공개키 서명(SPEC 2.6)까지 쓰려면 허브 키를 만들어 `HUB_SIGNING_KEY`에 개인키를 넣는다 (없어도 HMAC 은행은 그대로 된다):

```bash
uv run python -c "from clearing_hub.core.signing import generate_keypair; print(generate_keypair())"
```

```bash
uv run alembic revision --autogenerate -m "init"
```
```bash
uv run alembic upgrade head
```
```bash
uv run fastapi dev
```

API 문서: http://127.0.0.1:8000/docs

> Windows에서는 `uv run fastapi run`이 psycopg 비동기 모드와 맞지 않는다. 개발 중에는 `fastapi dev`를 쓴다.

## 가짜 은행과 함께 돌려 보기

1. 가짜 은행을 등록한다 (관리 API, `X-Admin-Token` 헤더):
   ```bash
   curl -s -X POST http://127.0.0.1:8000/admin/banks -H "X-Admin-Token: <ADMIN_TOKEN>" -H "Content-Type: application/json" -d "{\"bank_code\":\"902\",\"name\":\"SimBank\",\"base_url\":\"http://127.0.0.1:8100/interbank\"}"
   ```
   응답의 `api_key`, `secret`은 이때만 보여 준다.
2. 가짜 은행을 켠다 (허브가 준 secret으로 허브 서명을 검증한다):
   ```bash
   SIM_HUB_SECRET=<secret> uv run uvicorn bank_sim.main:app --port 8100
   ```
   Ed25519로 받게 하려면(SPEC 2.6) 가짜 은행 키 쌍을 만들어 `SIM_SIGNING_KEY=<개인키>`, `SIM_HUB_PUBLIC_KEY=<GET /v1/hub-key의 public_key>`, `SIM_SIGNATURE_MODE=both`로 켜고, 공개키를 등록한 뒤(`POST /admin/banks/902/public-key {"public_key": …}`) `PATCH /admin/banks/902/signature-mode {"signature_mode":"ed25519"}` → 가짜 은행을 `SIM_SIGNATURE_MODE=ed25519`로 다시 켠다. ping의 `response_signature_valid`가 `true`면 된다.
3. 보내는 은행(예: AvengersBank, `901`)도 같은 방법으로 등록하고, 그 키로 서명해서 `/v1/transfers`를 부른다. 서명 방법과 예시는 [SPEC.md](docs/SPEC.md) 2절.
   등록 직후에는 예치금·순채무한도가 0이라 송금이 `LIMIT_EXCEEDED`로 거절된다. 먼저 예치금을 넣는다 (`POST /admin/reserve/901/deposit {"amount":1000000}`, [docs/DEMO.md](docs/DEMO.md) 1).
4. 정산까지 이어서 보려면 [docs/DEMO.md](docs/DEMO.md)의 순서대로 한다.

가짜 은행의 계좌번호 끝 4자리: `0001` 정상 · `0002` 계좌 없음 · `0003` 지연(타임아웃) · `0004` 500 두 번 뒤 성공 · `0005` 입금됐는데 응답 없음 · `0006` 입금하지 않고 `200 COMPLETED` (대사 시연용. 허브는 이 거래를 `COMPLETED`로 기록하지만 가짜 은행 장부에는 없다).

본인 계좌 조회(SPEC 9절): 가짜 은행은 `김철수 / 19850315 / 01098765432`의 가상 CI(`SIM_CI_KEY`, 기본 `example-ci-key-change-me`)에 계좌 2개, `홍길동 / 19900101 / 01000000000`(AvengersBank 시연 고객)에 1개를 돌려준다.

## 테스트

```bash
uv run pytest
```

SQLite로 돌아가 PostgreSQL 없이 실행된다. 허브 → 은행 호출은 같은 프로세스의 가짜 은행 앱으로 보낸다.

## 배포

`main`에 push하면 GitHub Actions가 테스트 → arm64 이미지 → ECR → 공용 서버 배포까지 한다. 허브와 가짜 은행이 같은 이미지로 컨테이너 두 개로 뜬다. 처음 한 번 할 일은 [docs/DEPLOY.md](docs/DEPLOY.md).

## 로그

허브와 가짜 은행 모두 한 줄에 JSON 하나로 남기고, 모든 줄에 추적 ID `request_id`가 붙는다 ([SPEC 2.4](docs/SPEC.md)).
요청마다 접근 로그 한 줄(`clearing_hub.access`: 메서드, 경로, 상태, 걸린 시간, 요청한 은행 코드)이 남고,
재시도 태스크의 로그에는 그 거래를 처음 접수할 때의 ID가 붙는다. 보내는 은행이 준 ID로 세 서버를 이어 찾는다.

```bash
docker logs clearing-hub 2>&1 | grep <request_id>
```

`LOG_LEVEL`(기본 `INFO`), `LOG_FORMAT`(`json` 기본 / `text`: 로컬에서 읽기 편한 형식)으로 바꾼다.

## 다음 단계

- 관리자 JWT 로그인 (지금은 `ADMIN_TOKEN` 헤더), `hmac_secret` 암호화 저장
- 정산(마감 상계 → 대사 → 예치금 결제): [docs/SETTLEMENT_PLAN.md](docs/SETTLEMENT_PLAN.md) 1~7단계 완료 — 예치금 원장(`reserve/`), 순채무한도(접수 때 `LIMIT_EXCEEDED`, `transfers/exposure.py`), 정산 DRAFT·확정·일일 워커(`settlement/`, `workers/settlement.py`), 은행용 대사 API(SPEC 10절), AvengersBank 대사·장부 정리, 시연([docs/DEMO.md](docs/DEMO.md))
- 부인방지(계획서 12절, SPEC v1.0 초안): **8a 허브·가짜 은행 완료** — Ed25519 요청·응답 서명, 은행별 서명 모드(`hmac`·`both`·`ed25519`), 키 등록·24시간 회전, 증거 원문 보관(`transfer_evidence`). 다음은 **8b AvengersBank** (계획서 12.10)와 다른 팀 전환 일정
- 관리 사이트(React)

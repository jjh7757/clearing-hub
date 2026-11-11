# SimBank(902) 시연용 최소 확장 계획

작성 2026-11-11. 대상: `src/bank_sim/` (허브 저장소 안, 허브와 같은 이미지·같은 배포).

## 0. 목표와 범위

시연에서 "다른 은행에도 돈이 실제로 들어오고, 거기서도 보낼 수 있다"를 화면으로 보여 준다.

| 넣는 것 | 빼는 것 |
|---|---|
| 계좌 잔액·거래내역을 파일(SQLite)에 유지 | 회원가입·로그인(고객은 화면에서 고른다) |
| 화면 한 장(잔액, 거래내역, 보내기) | 대사 API 소비(`/v1/settlements` 읽기) |
| 902 → 다른 은행 보내기 (허브 `POST /v1/transfers`) | LLM, FDS, 이체 한도 외 보안 기능 |
| 화면·보내기 API에만 시연 비밀번호 | 허브 코드 변경 (필요 없음, 아래 1) |

## 1. 확인한 사실 (계획의 전제)

- **허브는 바꿀 필요가 없다.** 허브에 902의 송금을 막는 코드가 없다 (`sim_bank_code`는 `self_test.py`에서만 쓴다).
- **셀프 시험 판별은 깨지지 않는다.** `is_self_test`와 하루 한도는 `from_bank == 902` **그리고** `from_account == "902-TEST-0001"`을 함께 본다. SimBank 고객 계좌(`110-…`)로 보낸 거래는 셀프 시험으로 잡히지 않는다. 다만 SimBank 보내기에서 `from_account`가 `902-TEST-0001`이 되지 않도록 막는다.
- **배포는 두 컨테이너를 함께 다시 만든다.** `deploy-app`이 compose `pull`·`up -d`를 두 서비스(app, sim)에 모두 적용한다. 허브 main push 한 번이면 sim도 새 이미지로 바뀐다. 마이그레이션은 없다.
- **이미지는 root로 돈다** (Dockerfile에 `USER` 없음). 볼륨에 SQLite를 쓰는 데 권한 문제는 없다.
- **테스트는 `bank_sim`을 ASGI로 직접 부르고 `sim.received`·`server_errors_left`·`seen_request_ids`를 비운다** (`tests/conftest.py:82`). 이 세 이름과 동작은 남겨야 기존 테스트 310개가 그대로 돈다.
- **헬스체크는 `GET /`의 200만 본다.** `/`를 HTML로 바꿔도 200이면 된다.

## 2. 지켜야 할 것

1. 계좌번호 끝자리 시나리오(0002 계좌 없음 · 0003 지연 · 0004 500 두 번 · 0005 응답 안 함 · 0006 입금 없이 COMPLETED)는 지금 동작 그대로. DEMO.md·셀프 시험·SPEC 빠른 시작 5가 여기에 기댄다.
2. 고객 계좌가 아닌 번호로 들어온 입금(외부 은행의 시험 송금 등)은 지금처럼 받는다. 거래 기록만 남기고 잔액에는 넣지 않는다.
3. `SIM_DB_PATH`가 없으면 지금처럼 메모리로 돈다. 테스트·`clearing_hub.devserver`는 바뀌지 않는다.
4. `/interbank/*`는 공개 도메인으로 내보내지 않는다 (Caddy에서 막는다). 허브는 지금처럼 내부 주소 `http://clearing-hub-sim:8100/interbank`로 부른다.
5. 보내기는 시연 비밀번호(`SIM_UI_PASSWORD`)가 있어야 한다. 비어 있으면 보내기·화면 API를 끈다(403). 공개 인터넷에서 아무나 AvengersBank 운영 계좌로 돈을 넣지 못하게 한다.

## 3. 단계

단계마다 한 세션이 하고, diff·테스트를 검토한 뒤 다음 단계로 간다. 브랜치: `feature/simbank`(`feature/settlement` = main `aec0d71`에서 분기). push·main 병합은 사용자 결정.

### 1단계: 장부 (SQLite)

- `src/bank_sim/store.py`: 표 `accounts(account_num, holder, product_name, balance)`, `transactions(id, account_num, direction IN/OUT, amount, counterpart_bank, counterpart_bank_name, counterpart_account, counterpart_name, memo, transfer_id, status, created_at)`.
- 시작할 때 `SIM_CUSTOMERS` 계좌를 시드 (각 잔액 1,000,000원, 이미 있으면 건드리지 않음).
- `receive`: 고객 계좌면 같은 트랜잭션에서 잔액 + 거래내역 IN. 멱등은 `transfer_id` 유일 제약으로 (지금 `received` 딕셔너리와 같은 응답).
- `received`·`server_errors_left`·`seen_request_ids`는 이름을 남긴다 (메모리 모드에서는 지금과 같은 딕셔너리, 파일 모드에서는 store를 읽는 얇은 겉면이어도 된다. 테스트가 `.clear()`를 부른다).
- 테스트: 새 `tests/test_bank_sim_store.py` (잔액 증가, 멱등, 0006은 잔액·기록 없음, 고객 아닌 계좌는 기록만, 재시작 뒤 유지 = 같은 파일로 다시 열기). 기존 전체 테스트 통과.

### 2단계: 보내기

- `POST /sim/api/transfers` `{from_account, to_bank, to_account, amount, memo}`, 헤더 `X-Sim-Password`.
  1. 내 고객 계좌인지, 잔액이 되는지, `amount` 1~1,000,000인지 확인. `from_account`는 `902-TEST-0001`이 될 수 없다 (고객 계좌만 허용하므로 자연히 막힌다).
  2. 허브 `POST /v1/inquiry`로 받는 분 이름 확인 → 실패면 그대로 돌려준다.
  3. 잔액을 먼저 빼고 거래내역 OUT(`PENDING`)을 남긴 뒤 `POST /v1/transfers` (새 UUID v4).
  4. `COMPLETED` → 거래 `COMPLETED`. `FAILED` → 환불, 거래 `FAILED`(사유). `202`·`UNKNOWN` → `PENDING` 유지.
- `GET /sim/api/transfers/{transfer_id}/refresh`: `PENDING`이면 허브 `GET /v1/transfers/{id}`로 갱신 (같은 규칙으로 완료·환불). 화면 폴링이 부른다.
- `POST /sim/api/inquiry`: 화면의 "받는 분 확인" 버튼용 (허브 3.2 그대로 전달).
- `GET /sim/api/banks`: 허브 `GET /v1/banks`에서 902를 뺀 목록.
- 허브 호출 서명은 `clearing_hub.core.signing.sign_ed25519(SIM_SIGNING_KEY, …)`, `X-Bank-Code: 902`. 허브 응답 서명은 `SIM_HUB_PUBLIC_KEY`로 검증한다 (SPEC 2.5. 검증 실패는 `PENDING`으로 두고 로그).
- 새 환경 변수: `SIM_HUB_URL`(운영 `http://clearing-hub:8000`), `SIM_API_KEY`(허브가 902에 준 API 키, 은행 → 허브 요청의 `X-Api-Key`, SPEC 2.1), `SIM_UI_PASSWORD`, `SIM_DB_PATH`, `SIM_BANK_CODE`(기본 902).
- 테스트: 허브 앱을 ASGI로 붙여 902 → 901 송금이 끝까지 가는지 (conftest의 901·902 키 쌍 재사용), 잔액 부족·비밀번호 틀림·허브 FAILED 환불·202 뒤 refresh.

**2단계 결과 (구현됨, `src/bank_sim/hub_client.py`, `main.py`의 `/sim/api/*`, `tests/test_bank_sim_send.py`)**

- refresh는 계획의 `GET`이 아니라 **`POST /sim/api/transfers/{transfer_id}/refresh`** 다 (상태를 바꾸고 허브에 3.3을 다시 보낼 수 있는 호출이라).
- 읽기 API `GET /sim/api/customers`(고객·계좌·잔액 한 번에), `GET /sim/api/accounts/{account_num}/transactions`를 이 단계에서 넣었다 (3단계의 `/customers/{index}/accounts` 대신).
- 환불은 허브 본문 `status: "FAILED"`일 때만 (SPEC 3.3). `status` 없는 4xx·5xx·202·타임아웃·연결 오류·응답 서명 틀림은 `PENDING` 유지. 3.4가 `404 TRANSFER_NOT_FOUND`면 같은 `transfer_id`로 3.3을 다시 보낸다 (SPEC 3.4·6절 4-1).
- 장부 `transactions`에 `fail_code`·`fail_message`·`finished_at` 칸과 OUT `transfer_id` 유일 인덱스를 더했다. 1단계 파일을 열면 칸을 붙인다 (`ALTER TABLE ADD COLUMN`).
- 서버 재시작 때 `PENDING`을 자동으로 이어 확인하는 일(SPEC 6절 5)은 하지 않는다. 화면 폴링의 refresh가 그 역할을 한다.

### 3단계: 화면

- `GET /` → 정적 HTML 한 장 (`src/bank_sim/static/index.html`, 허브 관리자 화면과 같은 방식: 바닐라 JS, 비밀번호는 탭 `sessionStorage`).
- 고객 고르기(김철수·홍길동) → 계좌 카드(잔액) → 거래내역(받은 것은 보낸 은행·보낸 사람 이름, 보낸 것은 상태 배지) → 보내기 폼(받는 은행 선택, 계좌번호 3-3-6 자동 하이픈, "받는 분 확인", 금액, 보내기).
- 5초 폴링으로 잔액·거래내역 갱신, `PENDING` 거래는 refresh 호출. 시연에서 AvengersBank 모아로 보낸 돈이 몇 초 뒤 여기 뜨는 장면이 핵심.
- 읽기 API (2단계에서 만듦): `GET /sim/api/customers`, `GET /sim/api/accounts/{account_num}/transactions` (둘 다 비밀번호 필요).
- 0002~0006 시나리오 계좌는 화면에 보이지 않는다 (고객 계좌가 아니다).
- 확인: launch.json에 `simbank-ui-8110` 항목(허브 devserver와 같이 띄워 실제 송금), 브라우저로 송금·수신 스크린샷.

### 4단계: 배포 (서버 작업, 사용자 허용 뒤 SSM)

1. **사용자**: 가비아 DNS A `simbank` → `13.209.128.48`, `nslookup simbank.ai-agent-develop.cloud 8.8.8.8`로 전파 확인.
2. `deploy/server-setup.sh`의 compose `sim`에 `volumes: ["./sim-data:/data"]`, `sim.env`에 `SIM_DB_PATH=/data/simbank.db`, `SIM_HUB_URL=http://clearing-hub:8000`, `SIM_UI_PASSWORD=<서버에서 생성>`, `SIM_API_KEY=<아래 2-1>` 추가. 서버의 compose만 이 부분을 고친다 (`.env`·키는 건드리지 않는다).
   - 2-1. **902 API 키**: 운영 902의 API 키는 지금 어디에도 없다(등록 때 한 번만 보여 주고 허브는 해시만 둔다). 서버 안에서 관리자 API `POST /admin/banks/902/keys`(`rotate_keys`)로 새로 받아 `sim.env`의 `SIM_API_KEY`에 바로 쓴다 (값은 화면·로그에 출력하지 않는다). 이 재발급은 **API 키 해시만** 바꾼다: 902 공개키·`SIM_SIGNING_KEY`·허브 키는 그대로라 셀프 시험·`/interbank` 서명에는 영향이 없다. 902 API 키를 쓰던 다른 곳은 없다 (지금까지 902는 허브를 부른 적이 없다).
   - 2-2. 확인: sim 컨테이너 재시작 뒤 `GET /sim/api/banks`(헤더 `X-Sim-Password`)가 200이고 목록에 902가 없다. 401 `UNAUTHORIZED`가 `error`로 오면 API 키, `SIGNATURE_INVALID`면 902 공개키 등록을 본다 (허브 401·403은 화면에 502로 온다, 본문 `hub_status`).
3. `/srv/caddy/Caddyfile`:
   ```
   simbank.ai-agent-develop.cloud {
       respond /interbank/* 404
       reverse_proxy clearing-hub-sim:8100
   }
   ```
   reload 뒤 `docker logs caddy`에서 `certificate obtained successfully` 확인 (DEPLOY.md 5의 주의).
4. 허브 main push → Actions → `deploy-app`이 두 컨테이너를 새 이미지로. 마이그레이션 없음.
5. 902 예치금 확인 (`GET /admin/reserve`). 보내기를 하면 902 순채무가 늘어 셀프 시험(`SIM_BANK_UNAVAILABLE`)까지 막힐 수 있으니 여유 있게 넣는다.
6. 확인: `https://simbank.ai-agent-develop.cloud/` 200, `/interbank/inquiry` 404(공개 쪽), AvengersBank 모아 "SimBank 110-123-0001로 1만 원" → SimBank 화면에 입금, SimBank에서 901 시연 계좌로 1만 원 → AvengersBank 거래내역에 "SimBank 김철수".
7. 문서: DEPLOY.md에 "SimBank 화면 배포 (날짜)", DEMO.md에 화면 장면, SPEC 3.1·부록의 "가짜 은행 902" 설명에 "시연 화면이 있다" 한 줄 (허브 동작 변경 없음이라 버전은 패치).

## 4. 위험과 대응

| 위험 | 대응 |
|---|---|
| 902 순채무가 커져 셀프 시험이 503 | 4단계 5에서 예치. 보내기 1회 100만 원 상한 |
| 화면 비밀번호 유출 → 운영 AvengersBank 계좌에 가짜 입금 | 비밀번호는 서버에서 생성해 sim.env에만. 시연 뒤 바꿀 수 있게 DEPLOY.md에 명령 |
| 볼륨 없이 배포되어 재시작마다 잔액 초기화 | 4단계 순서: compose 수정을 push보다 먼저. 확인 항목에 "재시작 뒤 잔액 유지" |
| 운영 정산에 시연 송금이 섞임 | 의도된 동작 (예치금이 모의). 관리자 정산 화면에서도 그대로 보인다 |

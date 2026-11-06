# 실행 결과 (로컬 허브, 2026-11-06)

- 환경: Windows 11, Node.js v24.20.0, npm 11.19.0, Express 4. 허브는 `uv run python -m clearing_hub.devserver` (허브 `127.0.0.1:8000`, 가짜 은행 902 `127.0.0.1:8100`)
- 내 은행: `NodeBank`, `base_url` `http://127.0.0.1:3000/interbank`, 배정된 코드 `903`
- 가린 값: 개인키·`api_key`·관리자 토큰. 공개키·서명·`transfer_id`는 비밀이 아니라 그대로 둔다
- 출력은 이 폴더의 스크립트가 콘솔에 낸 그대로다 (`→ 상태`와 `허브 응답 서명 맞음/틀림`은 `src/hub.js`가 찍는다)

| # | 항목 | 결과 |
|---|---|---|
| 1 | `node --test` 서명 벡터 (2.3 예시 1·2, 2.5 예시 3) | **통과** 6/6, 첫 실행 |
| 2 | 셀프 등록 201, `ping.ok: true`, 지문 일치 | **통과**, 첫 실행 |
| 3 | `send.js`: 902 목록·김철수·`200 COMPLETED`·서명 맞음, `0002` → `404 FAILED`, 재전송 같은 결과 | **통과** |
| 4 | `selftest.js`: COMPLETED·`signature_valid: true`, resend 뒤 Map 1건, `status` 200, 없는 계좌 FAILED | **통과** (+ 지연 시험 202 → 1분 뒤 COMPLETED) |
| 5 | 일부러 틀리기: PATH에 `/v1` 빼기 → `hint`, 진단 | **`hint`가 정확히 짚음** (`PATH_WITHOUT_V1`, 확인됨). 받는 쪽 실수 2개도 진단에 나옴 |
| 6 | 결과 기록 | 이 파일 |

## 1. 서명 벡터 — `node --test`

```
✔ 예시 1: 은행 → 허브 POST /v1/transfers (JSON.stringify가 SPEC의 190바이트와 같다)
✔ 예시 2: 허브 → 은행 GET /receive/{id} (본문 없음)
✔ 예시 3: 예시 2에 대한 은행의 200 응답 서명
✔ 지문 (SPEC 2.3의 d61a340fe933017d, 239a9acfc7ecb8de)
✔ 지금 시각 서명은 검증을 통과하고, 틀린 값은 예외 없이 false
✔ 생성한 키 쌍: 44자 base64, 서명·검증 왕복
ℹ tests 6
ℹ pass 6
ℹ fail 0
```

## 2. 셀프 등록 — `node src/register.js`

devserver 콘솔의 초대 문구에서 초대 코드·허브 공개키(`f62OB4pm…FTSs=`)·지문(`6984bc2bb5cc6185`)을 `.env`에 넣고 실행.

```
POST /v1/banks/register → 201  허브 응답 서명 맞음
- bank_code: 903  name: NodeBank  net_debit_cap: 1000000000
- api_key: chk_…(가림)(.env에만 저장, 이 응답에만 있다)
- hub_fingerprint: 6984bc2bb5cc6185  초대 문구 지문과 같음
- ping.ok: true {"ok":true,"reachable":true,"status":404,"body":{"error":"ACCOUNT_NOT_FOUND","message":"받는 계좌를 찾을 수 없습니다"},"response_signature_valid":true}
BANK_CODE·BANK_API_KEY를 .env에 저장했습니다.
```

내 서버 로그 (허브의 ping이 없는 계좌 `000-000-000000`으로 `/inquiry`를 부름. 첫 줄은 등록 전에 손으로 보낸 서명 없는 요청):

```
[bank] POST /interbank/inquiry rid=- 401 UNAUTHORIZED
[bank] POST /interbank/inquiry rid=2b0a6dbde8ef46b0bad150c97a55d473 inquiry 000-000-000000 → 404
```

서명 없는 요청에 대한 내 401에도 응답 서명이 붙는다 (2.5):

```
HTTP/1.1 401 Unauthorized
Content-Type: application/json; charset=utf-8
X-Response-Timestamp: 1793949937
X-Response-Signature: dDvZ9jh5uThTjtOqqohKeMApmIhs+ilQW6s3DTKnTEoCUHfvy0YNhxKd9c4CZIltrxS+ERYI4tZKL5cOg6mbDA==
X-Signature-Alg: ed25519

{"error":"UNAUTHORIZED","message":"허브 요청이 아니거나 서명 헤더가 없습니다"}
```

## 3. 보내는 쪽 — `node src/send.js`

```
GET /v1/banks → 200  허브 응답 서명 맞음
  받는 은행 선택지: 902 SimBank
POST /v1/inquiry → 200  허브 응답 서명 맞음
  예금주: 김철수
POST /v1/transfers → 200  허브 응답 서명 맞음
   {"transfer_id":"922c34e5-0caf-460a-bbf4-02b837dd8907","status":"COMPLETED","holder_name":"김철수","finished_at":"2026-11-06T07:26:22Z"}
  [장부] 922c34e5-0caf-460a-bbf4-02b837dd8907: 완료: 중간 계정 차감, 내 거래 COMPLETED
POST /v1/transfers → 404  허브 응답 서명 맞음
   {"error":"ACCOUNT_NOT_FOUND","message":"받는 계좌를 찾을 수 없습니다","transfer_id":"e91defd1-35a4-4633-ae05-9af8ec931216","status":"FAILED"}
  [장부] e91defd1-35a4-4633-ae05-9af8ec931216: 되돌림: 중간 계정 → 고객 계좌, 내 거래 FAILED (사유 ACCOUNT_NOT_FOUND)
POST /v1/transfers → 200  허브 응답 서명 맞음
  재전송: {"transfer_id":"922c34e5-0caf-460a-bbf4-02b837dd8907","status":"COMPLETED","holder_name":"김철수","finished_at":"2026-11-06T07:26:22Z"} → 처음과 같은 결과
GET /v1/transfers/922c34e5-0caf-460a-bbf4-02b837dd8907 → 200  허브 응답 서명 맞음
  결과 조회: {"transfer_id":"922c34e5-0caf-460a-bbf4-02b837dd8907","status":"COMPLETED","holder_name":"김철수","finished_at":"2026-11-06T07:26:22Z","to_bank":"902","amount":1000,"requested_at":"2026-11-06T07:26:22Z","fail_code":null,"fail_message":null}
```

- 은행 목록에는 902와 내 은행(903)이 있었고, 3.1대로 자기 은행을 빼고 보여 준다.
- 허브 응답 서명은 6건 모두 "맞음" (요구한 3건 + 없는 계좌·재전송·결과 조회).
- 되돌림은 본문 `status: "FAILED"`인 두 번째 이체에서만 일어났다 (3.3).

## 4. 받는 쪽 셀프 시험 — `node src/selftest.js`

```
POST /v1/test-receive → 200  허브 응답 서명 맞음
   hub: {"transfer_id":"c9dd91b8-09f2-45c9-a5af-1ef7cae83bd3","status":"COMPLETED","holder_name":"김민수","finished_at":"2026-11-06T07:26:28Z"}
   receiver_reply: {"reachable":true,"status":200,"body":{"transfer_id":"c9dd91b8-09f2-45c9-a5af-1ef7cae83bd3","status":"COMPLETED","holder_name":"김민수"},"signature_valid":true}
   내 Map: 이 거래 1건, 거래내역 "SimBank ClearingHub 시험"
POST /v1/test-receive/c9dd91b8-09f2-45c9-a5af-1ef7cae83bd3/resend → 200  허브 응답 서명 맞음
   hub: {"transfer_id":"c9dd91b8-09f2-45c9-a5af-1ef7cae83bd3","hub_status":"COMPLETED"}
   receiver_reply: {"reachable":true,"status":200,"body":{"transfer_id":"c9dd91b8-09f2-45c9-a5af-1ef7cae83bd3","status":"COMPLETED","holder_name":"김민수"},"signature_valid":true}
   재전송 뒤 내 Map: 이 거래 1건 (1이어야 함), 계좌 잔액 1000원
GET /v1/test-receive/c9dd91b8-09f2-45c9-a5af-1ef7cae83bd3/status → 200  허브 응답 서명 맞음
   hub: {"transfer_id":"c9dd91b8-09f2-45c9-a5af-1ef7cae83bd3","hub_status":"COMPLETED"}
   receiver_reply: {"reachable":true,"status":200,"body":{"transfer_id":"c9dd91b8-09f2-45c9-a5af-1ef7cae83bd3","status":"COMPLETED","holder_name":"김민수","received_at":"2026-11-06T07:26:28Z"},"signature_valid":true}
POST /v1/test-receive → 404  허브 응답 서명 맞음
   hub: {"error":"ACCOUNT_NOT_FOUND","message":"받는 계좌를 찾을 수 없습니다","transfer_id":"8105248e-7287-4320-8b51-a21552b14fb4","status":"FAILED"}
   receiver_reply: {"reachable":true,"status":404,"body":{"error":"ACCOUNT_NOT_FOUND","message":"받는 계좌를 찾을 수 없습니다","transfer_id":"8105248e-7287-4320-8b51-a21552b14fb4"},"signature_valid":true}
GET /v1/test-receive/8105248e-7287-4320-8b51-a21552b14fb4/status → 200  허브 응답 서명 맞음
   hub: {"transfer_id":"8105248e-7287-4320-8b51-a21552b14fb4","hub_status":"FAILED"}
   receiver_reply: {"reachable":true,"status":404,"body":{"error":"TRANSFER_NOT_FOUND","message":"그 거래 ID로 입금한 적이 없습니다","transfer_id":"8105248e-7287-4320-8b51-a21552b14fb4"},"signature_valid":true}
```

- 거절한 거래의 `/status`는 허브 응답이 **200**(`hub_status: FAILED`)이고 404는 `receiver_reply.status`에 있다. 빠른 시작 6단계의 문장과 층이 다르다 → [SPEC_FEEDBACK.md](SPEC_FEEDBACK.md) C1.
- 내 서버 로그: 같은 `X-Request-ID`가 첫 입금·재전송·입금 확인에 그대로 왔다 (2.4).

```
[bank] POST /interbank/receive rid=383b092e-… receive c9dd91b8-… → 200 입금 1000원 (SimBank ClearingHub 시험)
[bank] POST /interbank/receive rid=383b092e-… receive c9dd91b8-… 재전송 → 200 (입금 안 함)
[bank] GET /interbank/receive/c9dd91b8-… rid=383b092e-… receive-status c9dd91b8-… → 200
[bank] POST /interbank/receive rid=9a76c96a-… receive 8105248e-… → 404 ACCOUNT_NOT_FOUND (기록 안 함)
[bank] GET /interbank/receive/8105248e-… rid=9a76c96a-… receive-status 8105248e-… → 404
```

### 4-1. 지연 시험 (선택, 7절 7 마지막 줄) — `node src/selftest.js --slow`

계좌 `110-123-000009`는 입금을 기록한 뒤 6초 늦게 답한다 (허브 타임아웃 5초).

```
POST /v1/test-receive → 202  허브 응답 서명 맞음
   hub: {"transfer_id":"0201fe54-d4ab-49cb-95a1-e94b38685232","status":"PENDING","holder_name":null,"finished_at":null}
   receiver_reply: {"reachable":false,"status":null,"body":{},"signature_valid":null}
   70초 기다렸다가 hub_status를 다시 본다…
GET /v1/test-receive/0201fe54-d4ab-49cb-95a1-e94b38685232/status → 200  허브 응답 서명 맞음
   hub: {"transfer_id":"0201fe54-d4ab-49cb-95a1-e94b38685232","hub_status":"COMPLETED"}
   receiver_reply: {"reachable":true,"status":200,"body":{"transfer_id":"0201fe54-d4ab-49cb-95a1-e94b38685232","status":"COMPLETED","holder_name":"박지연","received_at":"2026-11-06T07:27:21Z"},"signature_valid":true}
```

허브가 1분 뒤 4.3으로 확인해 `COMPLETED`로 맞췄고, 내 서버의 입금은 1번이다 (`/receive` 로그 1줄).

## 5. 일부러 틀리기

### 5-1. 보내는 쪽: 서명 PATH에서 `/v1` 빼기 — `node src/send.js --wrong-path`

실제 주소는 `/v1/banks`, 서명은 `/banks`로.

```
GET /v1/banks → 401  허브 응답 서명 맞음
  401 본문: {
  "error": "SIGNATURE_INVALID",
  "message": "서명이 맞지 않습니다",
  "hint": "원인 후보(확인됨): 서명 PATH에 `/v1`이 빠짐. 허브는 `/v1/banks`로 검증했고, 서명은 `/banks`로 만들었다 → 서명 PATH는 `/v1`부터 넣는다(`/v1/transfers`). 2.3의 `sign_request`에 넘기는 path를 확인 (SPEC 2.2) 자세히: GET /v1/diagnose/failures"
}
GET /v1/diagnose/failures → 200  허브 응답 서명 맞음
  진단 1건째: {
  "failure": {
    "failure_id": 1, "direction": "BANK_TO_HUB", "reason": "SIGNATURE_INVALID", "message": "서명이 맞지 않습니다",
    "method": "GET", "path": "/v1/banks",
    "headers": { "X-Bank-Code": "903", "X-Timestamp": "1793949999", "X-Signature": "<가림>", "X-Signature-Alg": "ed25519",
                 "X-Request-ID": "81bae535-bd23-468f-a372-ef7ba93799b5" },
    "body_preview": "", "body_length": 0, "http_status": null, "response_headers": null, "response_body_preview": null,
    "created_at": "2026-11-06T07:26:39Z"
  },
  "hypotheses": [
    { "code": "PATH_WITHOUT_V1", "title": "서명 PATH에 `/v1`이 빠짐",
      "detail": "허브는 `/v1/banks`로 검증했고, 서명은 `/banks`로 만들었다", "spec_ref": "2.2",
      "fix": "서명 PATH는 `/v1`부터 넣는다(`/v1/transfers`). 2.3의 `sign_request`에 넘기는 path를 확인", "verified": true }
  ]
}
```

판정: **`hint` 한 줄로 원인·고칠 곳을 정확히 짚었다** (`원인 후보(확인됨)`). 401 응답에도 허브 서명이 붙어 있었다. `fix`의 예가 실제 경로가 아닌 `/v1/transfers`이고 `sign_request`가 Python 이름인 점은 사소한 개선거리 (SPEC_FEEDBACK N9).

### 5-2. 받는 쪽 (추가): 버그를 넣은 임시 사본으로 `POST /v1/me/ping`

`bank.js`를 임시로 복사해 ① 서명 PATH를 `req.originalUrl`(`/interbank/inquiry`)로 검증, ② 응답 서명을 빼고 띄운 뒤 ping. 확인 뒤 사본은 지우고 원래 `bank.js`로 ping `ok: true`를 다시 확인했다.

```
① POST /v1/me/ping → 200  {"ok":false,"reachable":true,"status":401,"body":{"error":"SIGNATURE_INVALID","message":"서명이 맞지 않습니다"},"response_signature_valid":true}
② POST /v1/me/ping → 200  {"ok":false,"reachable":true,"status":404,"body":{"error":"ACCOUNT_NOT_FOUND",…},"response_signature_valid":false}
```

`GET /v1/diagnose/failures`의 `HUB_TO_BANK` 항목 (요약):

```
{"direction":"HUB_TO_BANK","reason":"RESPONSE_SIGNATURE_INVALID","path":"/inquiry","http_status":404,
 "hypotheses":[["RESPONSE_SIGNATURE_MISSING",true,"은행 응답에 `X-Response-Signature`가 없음"]]}
{"direction":"HUB_TO_BANK","reason":"SIGNATURE_INVALID","path":"/inquiry","http_status":401,
 "hypotheses":[["BANK_VERIFY_PATH",false,"은행이 서명 PATH를 base_url 뒤 부분이 아닌 경로로 검증함"],
               ["BANK_VERIFY_HUB_KEY",false,…],["BANK_VERIFY_RAW_BODY",false,…]]}
```

판정: 응답 서명 누락은 **확인된 원인**으로, 경로 실수는 **1순위 가설**로 나왔다. 다만 ping 결과 자체에는 `hint`가 없어서, 빠른 시작 4단계의 "`hint`도 본다"를 따라가면 찾을 곳이 없다 (SPEC_FEEDBACK C3). `reason: RESPONSE_SIGNATURE_INVALID`는 SPEC에 없는 값이다.

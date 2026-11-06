# SPEC.md v1.2.1 빈틈 보고 — "SPEC만 받은 Node.js 팀" 역할로

- 날짜: 2026-11-06
- 한 일: `docs/SPEC.md`의 "빠른 시작"부터 읽고 Node.js(Express + 내장 `crypto`) 은행을 만들어, 로컬 허브(`clearing_hub.devserver`)에 셀프 등록 → 902로 이체 → 셀프 시험 → 일부러 틀리기까지 했다. 결과는 [RESULTS.md](RESULTS.md).
- **허브 소스(`src/`·`tests/`·`alembic/`)와 `avengers_bank/`는 열지 않았다.** 읽은 것은 `docs/SPEC.md`, `README.md`의 "로컬에서 한 명령으로" 절, `examples/README.md`, 그리고 허브가 준 응답(401 `hint`, `GET /v1/diagnose/failures`)뿐이다.
- 분류: **막힘**(진행 불가) / **헷갈림**(두 번 읽음·추측) / **개선**(있으면 좋을 것) / **오류**(문서가 틀림)

## 요약

| 분류 | 개수 |
|---|---|
| 막힘 | 0 |
| 헷갈림 | 3 |
| 개선 | 10 |
| 오류 | 1 |

막힌 곳은 없었다. 서명 벡터 3개가 첫 실행에 맞았고, 등록·이체·셀프 시험도 모두 첫 실행에 통과했다. 빠른 시작의 "이 절만 따라 하면"은 사실이었다. 빠른 시작 밖에서 찾아본 곳은 2.1(헤더별 오류 코드), 2.5 "누가 붙이는가", 3.7 응답 형식, 3.8, 4.2 "은행이 지켜야 할 것", 5절 오류 표뿐이다.

다만 **빠른 시작의 코드는 모두 Python(FastAPI)**이라, Node 팀은 받는 쪽(원본 바이트 받기·응답 서명 미들웨어)을 문서 도움 없이 Express 내부 동작을 알고 풀어야 한다. 아래 개선 항목 대부분이 여기서 나왔다.

### 가장 중요한 3개

1. **[개선 N1] Node(비Python) 팀용 받는 쪽 지침이 없다**: 서명 함수는 DER 접두사 한 줄 덕에 쉬웠지만, Express에서 ① 원본 바이트 받기(`express.raw`, GET이면 `req.body`가 `{}`), ② 응답 본문 바이트를 잡아 서명하기(`res.end` 가로채기), ③ PATH(`req.path`는 라우터 마운트 뒤 부분)는 문서에 없다. 응답 서명을 빠뜨리면 그 은행으로 가는 이체가 **모두 `PENDING`**이 되는 큰 실수인데(2.5), 가장 틀리기 쉬운 곳에 가장 안내가 적다. 빠른 시작 2·3단계에 "Node.js(Express)" 상자를 10줄쯤 넣기를 제안한다 (아래 N1에 문안).
2. **[헷갈림 C1] 셀프 시험 `/status`의 "404"가 두 가지 뜻이다**: 빠른 시작 6단계는 "없는 계좌로 거절한 거래는 `/status`가 `404 TRANSFER_NOT_FOUND`"라고 하지만, 실제 허브 응답은 **`200`** + `hub_status: "FAILED"` + `receiver_reply.status: 404`다. 한편 3.7 오류 표의 `404 TRANSFER_NOT_FOUND`는 허브 자체가 그 ID를 모를 때다. 시험 스크립트에서 HTTP 상태로 판정하면 틀린다.
3. **[헷갈림 C3] ping이 실패했을 때 볼 곳이 문서와 다르다**: 빠른 시작 4단계는 "`ping.ok`가 `false`면 … 7단계의 `hint`도 본다"인데, ping 결과(`POST /v1/me/ping` 200)에는 `hint`가 없다. 받는 쪽 실수는 `GET /v1/diagnose/failures`의 `HUB_TO_BANK` 항목에서만 보인다. 그 항목의 `reason` 값(`RESPONSE_SIGNATURE_INVALID` 등)도 문서에 목록이 없다.

---

## 헷갈림

### C1. 셀프 시험 `/status`의 404 (빠른 시작 6단계, 3.7, 7절 7)

- **무엇이**: 빠른 시작 6단계 "확인"의 "없는 계좌로 거절한 거래는 `/status`가 `404 TRANSFER_NOT_FOUND`다", 7절 7 표의 "없는 계좌로 거절한 거래는 `404 TRANSFER_NOT_FOUND`". 처음엔 허브가 HTTP 404를 주는 줄 알았다.
- **실제**: `GET /v1/test-receive/{id}/status` → **HTTP 200** `{"transfer_id":…,"hub_status":"FAILED","receiver_reply":{"status":404,"body":{"error":"TRANSFER_NOT_FOUND",…},"signature_valid":true}}`. 3.7 본문의 "두 API의 응답 `200`"을 다시 읽고서야 404가 `receiver_reply` 안의 값이라는 것을 알았다. 그런데 3.7 오류 표에는 허브 자체의 `404 TRANSFER_NOT_FOUND`(없는 `transfer_id`)도 있어서, 같은 코드가 두 층에서 나온다.
- **해결**: 실제로 불러 보고 3.7 "두 API의 응답 200"으로 확인 (두 번 읽음).
- **제안**: 빠른 시작 6단계 → "없는 계좌로 거절한 거래는 `/status`의 **`receiver_reply.status`가 `404`**(`TRANSFER_NOT_FOUND`)이고 `hub_status`는 `FAILED`다 (허브 응답 자체는 200)". 7절 7 표도 "`receiver_reply`가 `404 TRANSFER_NOT_FOUND`"로. 3.7 오류 표의 404 줄에 "(허브가 그 `transfer_id`를 모를 때. 내 서버의 404는 `receiver_reply.status`에 나온다)".

### C2. 받는 쪽 401의 오류 코드 나누기 (빠른 시작 3단계 vs 2.1·2.3·5절)

- **무엇이**: 빠른 시작 3단계 코드는 `X-Bank-Code`가 `"000"`이 아니어도, 헤더가 없어도, 시각이 지나도 모두 `SIGNATURE_INVALID`다. 5절 표는 "`X-Bank-Code`가 `"000"`이 아니거나 서명 헤더 없음" → `UNAUTHORIZED`, 시각 초과 → `SIGNATURE_EXPIRED`다. 어느 쪽을 따라야 하는지 잠깐 멈췄다.
- **해결**: 2.3 끝의 "오류 코드를 나눠야 하면 …"을 찾아 "둘 다 허용, 나누면 더 좋다"로 이해하고, Node 예제는 나눴다 (`UNAUTHORIZED`/`SIGNATURE_EXPIRED`/`SIGNATURE_INVALID`).
- **제안**: 빠른 시작 3단계 `BadSignature` 근처에 한 줄 주석 "모두 401이면 충분하다. 코드를 나누려면 2.3 끝 (헤더 없음·`X-Bank-Code`≠`000` → `UNAUTHORIZED`, 시각 → `SIGNATURE_EXPIRED`)".

### C3. ping 실패 때 `hint`가 없다 (빠른 시작 4단계, 3.6, 3.8)

- **무엇이**: 빠른 시작 4단계 "`ping.ok`가 `false`여도 … 3.6의 ping 결과 표로 고친 뒤 … (7단계의 `hint`도 본다)". 일부러 받는 쪽을 틀려 봤더니(경로 전체로 검증, 응답 서명 빼기) ping 결과는 `200` `{"ok":false,"status":401,…}`·`{"ok":false,…,"response_signature_valid":false}`이고 **`hint`는 어디에도 없다**. `hint`는 허브가 *내 요청*을 401로 거절할 때만 붙는다(3.8 1번).
- **실제로 도움이 된 곳**: `GET /v1/diagnose/failures`의 `HUB_TO_BANK` 항목. 응답 서명 누락은 `RESPONSE_SIGNATURE_MISSING`(`verified: true`)으로 정확히 짚었고, 경로 실수는 `BANK_VERIFY_PATH`가 1순위(`verified: false`)였다. 충분히 쓸모 있다.
- **헷갈린 점 하나 더**: `failure.reason`에 `RESPONSE_SIGNATURE_INVALID`가 나왔는데, 이 값은 5절 오류 표에도 3.8에도 없다 (3.8의 `direction` 표는 `BANK_TO_HUB`의 reason만 나열한다).
- **제안**: 빠른 시작 4단계 → "`ping.ok`가 `false`면 3.6의 ping 결과 표를 보고, 원인이 안 보이면 `GET /v1/diagnose/failures`의 `HUB_TO_BANK` 항목을 본다 (ping 결과에는 `hint`가 없다)". 3.8 `direction` 표의 `HUB_TO_BANK` 줄에 reason 값 목록(`SIGNATURE_INVALID`(내 서버가 401·403), `RESPONSE_SIGNATURE_INVALID` …)을 더한다.

---

## 개선

### N1. 빠른 시작에 Node.js(Express) 상자 [Node 특유, 가장 중요]

빠른 시작 2단계의 "다른 언어" 한 줄(DER 접두사, `crypto.sign(null, …)`)은 정확했고 그것만으로 서명 함수는 첫 실행에 맞았다. 그러나 받는 쪽(3단계)은 FastAPI 전용이라 아래 4가지를 스스로 알아내야 했다. 하나라도 틀리면 ping이 `ok: false`가 되거나, 더 나쁘게는 응답 서명이 조용히 틀려 이체가 모두 `PENDING`이 된다.

| 무엇 | Node/Express에서 틀리기 쉬운 것 | 이 예제의 풀이 |
|---|---|---|
| 원본 바이트 받기 (2.2) | 흔히 쓰는 `express.json()`은 본문을 풀어 버려 원본 바이트가 없다. `express.raw()`의 기본 `type`은 `application/octet-stream`이라 JSON 요청에 안 붙는다 | `app.use(express.raw({ type: () => true, limit: '64kb' }))` |
| 본문 없는 GET (4.3) | `express.raw`는 본문이 없으면 `req.body`를 Buffer가 아니라 **`{}`**로 둔다 (Express 5는 `undefined`). 그대로 `Buffer.concat`하면 예외, `String({})`이면 `"[object Object]"`로 서명 검증 | `Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0)` |
| 응답 서명 (2.5) | `res.json()`은 `res.send()` → `res.end(바이트)` 순서라, 미들웨어에서 "보낼 본문 바이트"를 잡으려면 `res.end`를 가로채야 한다. `res.json`을 가로채 `JSON.stringify`를 다시 하면 바이트가 같다는 보장이 없다(`RESPONSE_BODY_REDUMPED`). ETag가 켜져 있으면 조건부 요청에 304로 바뀔 수 있다 | 가장 먼저 등록하는 미들웨어에서 `res.end`를 감싸 `chunk`(Buffer/문자열)로 서명하고 헤더를 붙인 뒤 원래 `end` 호출. `app.set('etag', false)` |
| 서명 PATH (2.2) | `app.post('/interbank/receive')`로 만들면 `req.path`가 `/interbank/receive`라 떼야 한다 | `express.Router()`를 `app.use('/interbank', router)`로 붙이면 라우터 안의 `req.path`가 이미 `/receive`다 (쿼리스트링도 빠진다) |

- **제안 문안** (빠른 시작 3단계 끝, "다른 언어" 줄로):
  > Node.js(Express): ① `express.raw({ type: () => true })`로 원본 바이트를 받는다 (본문 없는 GET은 `req.body`가 Buffer가 아니므로 빈 Buffer로 바꾼다). ② 응답 서명은 맨 앞 미들웨어에서 `res.end`를 감싸 넘어오는 본문 바이트로 서명한다 (`res.json`을 감싸 다시 직렬화하지 않는다, ETag 끄기). ③ 라우터를 `app.use('/interbank', router)`로 붙이면 `req.path`가 서명 PATH다. 참고 구현: `examples/node-bank/src/bank.js`.

### N2. Node 키 만들기·읽기 한 줄씩 (빠른 시작 1·2단계) [Node 특유]

- 1단계는 Python 코드뿐이다. Node에서는 `crypto.generateKeyPairSync('ed25519')`로 만든 뒤 `export({format:'der', type:'pkcs8'})`·`export({format:'der', type:'spki'})`의 **끝 32바이트**가 raw 키다. 접두사를 붙여 읽는 것의 반대 방향인데 문서에 없다.
- 2단계 "접두사를 붙여 DER로 읽는다"에 실제 호출이 있으면 더 빠르다: `crypto.createPrivateKey({ key: Buffer.concat([접두사, raw]), format: 'der', type: 'pkcs8' })` (공개키는 `createPublicKey`, `type: 'spki'`). 대안으로 JWK(`{kty:'OKP', crv:'Ed25519', x, d}`, base64url)도 되지만 base64 ↔ base64url 변환이 또 하나의 실수 지점이라 DER 접두사 쪽을 권하는 게 맞다.

### N3. Node `Buffer.from(x, 'base64')`는 틀린 문자를 조용히 버린다 (2.2, 빠른 시작 2단계 `_verify`) [Node 특유]

- Python 참고 구현은 `b64decode(..., validate=True)`로 형식 오류를 거르는데, Node에는 그런 옵션이 없다. URL-safe(`-`·`_`)나 패딩 없는 서명도 그럭저럭 디코드되어, 길이 검사를 안 하면 엉뚱한 바이트로 검증하게 된다 (결과는 false라 안전 쪽이지만, 진단이 어려워진다).
- **제안**: 2단계 "틀리기 쉬운 곳"에 "Node는 base64를 느슨하게 읽으므로 `/^[A-Za-z0-9+/]+={0,2}$/`와 길이(키 32·서명 64바이트)를 먼저 검사한다".

### N4. JS는 `JSON.stringify` 그대로가 예시 1의 바이트다 (2.3) [Node 특유, 쉬웠던 이유]

- 문제 없었음. `JSON.stringify`는 공백이 없고 한글을 `\uXXXX`로 바꾸지 않아서, 예시 1의 본문(190바이트)·예시 3(138바이트)과 키 순서만 맞추면 그대로 같았다. Python처럼 `separators`·`ensure_ascii`를 신경 쓸 필요가 없다.
- **제안**: 2.3 예시 1 아래에 "JavaScript는 `JSON.stringify(객체)` 결과가 이 바이트와 같다 (키 순서만 같게)" 한 줄. 다른 언어 팀이 "우리 직렬화가 맞나"를 바로 판단할 수 있다.

### N5. JS 시각은 밀리초 (2.1 `X-Timestamp`) [Node 특유]

- `Date.now()`는 밀리초다. 3.8에 `TIMESTAMP_MILLIS` 가설이 있을 만큼 흔한 실수다.
- **제안**: 2.1 표의 `X-Timestamp` 줄에 "(JavaScript는 `Math.floor(Date.now() / 1000)`)".

### N6. 보내는 쪽 `fetch`의 두 가지 (빠른 시작 5단계) [Node 특유]

- 허브 응답 서명 검증에는 받은 바이트가 필요하므로 `res.json()`이 아니라 `Buffer.from(await res.arrayBuffer())`로 받고 검증한 뒤 `JSON.parse`한다.
- `fetch`에 Buffer 본문을 주면 `Content-Type`을 붙이지 않는다 (문자열이면 `text/plain`). 직접 `application/json`을 넣는다.
- **제안**: 5단계 "확인" 아래 "다른 언어" 한 줄: "응답 서명은 본문을 풀기 전의 바이트로 검증한다 (Node `fetch`면 `arrayBuffer()`)".

### N7. 허브 지문 확인은 "응답의 `hub_fingerprint` 문자열 비교"보다 "받은 키로 직접 계산"이 맞다 (3.5, 빠른 시작 4단계)

- 빠른 시작 4단계·3.5·7절 4는 "`hub_fingerprint`가 초대 문구의 지문과 같은지" 확인하라고 한다. 그런데 응답의 `hub_fingerprint`는 문자열일 뿐이라, 중간에서 키를 바꿔치기한 쪽은 진짜 지문 문자열을 그대로 넣어 둘 수 있다. 실제로 의미 있는 확인은 ① **내가 설정에 저장할 허브 공개키로 2.6의 식(sha256 앞 16자)을 직접 계산**해 초대 문구의 지문과 맞추고, ② 등록 응답의 서명을 그 키로 검증하는 것이다. 이 예제의 `register.js`는 셋 다 한다.
- **제안**: 3.5 `hub_public_key, hub_fingerprint` 줄 → "`hub_public_key`로 지문을 **직접 계산해**(2.6) 초대 문구의 지문과 맞춘다. 응답의 `hub_fingerprint`는 참고용이다. 이 응답의 서명도 그 키로 검증한다".

### N8. 4.2 규칙 2를 DB 코드 한 조각으로 (4.2) [Node에서 특히]

- Node는 메모리 `Map`이면 한 스레드라 "확인 → 기록" 사이에 끼어들 요청이 없지만, DB로 옮기면 `await db.find()`와 `await db.insert()` 사이에 같은 ID의 다른 요청이 끼어든다. "유니크 제약에 걸린 쪽은 4xx 금지"가 무슨 코드인지 그림이 바로 안 그려진다.
- **제안**: 4.2 규칙 2 아래 의사 코드: `BEGIN; INSERT INTO received(transfer_id, …) VALUES (…) ON CONFLICT (transfer_id) DO NOTHING RETURNING id;` → 행이 없으면 `ROLLBACK` 후 저장된 행을 읽어 200 (못 읽으면 503), 있으면 잔액 `UPDATE` 후 `COMMIT`·200. 이 예제 README에도 같은 내용을 적었다.

### N9. 401 `hint`의 `fix` 문구가 Python 함수 이름과 고정 예시를 쓴다 (5절 예시, 3.8)

- 실제 `hint`: "… 허브는 `/v1/banks`로 검증했고, 서명은 `/banks`로 만들었다 → 서명 PATH는 `/v1`부터 넣는다(`/v1/transfers`). 2.3의 `sign_request`에 넘기는 path를 확인". `detail`은 실제 경로(`/v1/banks`)인데 `fix`의 예는 늘 `/v1/transfers`이고, `sign_request`는 Python 참고 구현의 이름이다. 원인은 정확했고 바로 이해했으므로 사소하다.
- **제안**: `fix`의 예를 실패한 실제 경로로 채우고, "2.3의 `sign_request`" 대신 "서명 함수에 넘기는 PATH"로.

### N10. 3.7 "시험용 계좌"와 `/_local` 같은 확인 창구 (3.7, 빠른 시작 6단계)

- 빠른 시작 6단계 "확인: … 3단계 서버의 `received`에 거래가 1건(재전송해도 1건)"은 같은 프로세스 안의 Python 변수를 보는 것을 전제한다. 별도 스크립트로 시험하는 팀은 그 수를 어떻게 볼지 정해야 한다 (이 예제는 127.0.0.1에서만 열리는 `GET /_local/received`를 두었다).
- **제안**: "내 서버 로그나 DB에서 그 `transfer_id`가 1건인지 본다"로 일반화.

---

## 오류

### E1. 7절 9의 "Node.js는 `examples/node-bank`(준비 중)" (7절 9, `examples/README.md`)

- 이 예제가 들어오면 틀린 문장이 된다. `examples/README.md`는 이번 커밋에서 실제 경로로 고쳤고, **SPEC 7절 9는 지시대로 고치지 않았다** (다음 세션에서 "Node.js는 [`examples/node-bank`](../examples/node-bank/README.md)(Express, SPEC만 보고 만든 받는 쪽·보내는 쪽)"로).

---

## 문제 없었음 (그리고 왜 쉬웠나)

| 절 | 왜 쉬웠나 |
|---|---|
| 빠른 시작 2단계·2.3·2.5 (서명) | 예시 1·2·3의 **고정 값**이 있어 구현을 바로 판정할 수 있었다. DER 접두사가 16진수 그대로 있어서 Node 키 읽기를 검색하지 않았다. 예시 2의 "본문이 비어도 경로 뒤 `\n`" 문장이 결정적이었다 |
| 2.2 PATH 표 | 방향별 표 + "검증하는 쪽은 자기 `base_url` 경로 부분을 떼고 비교" 문장으로 헷갈림 없음 |
| 3.5 셀프 등록 | 필드 표·응답 예·"`request_signature`는 `""`" 명시. 첫 요청에 201 |
| 3.3 되돌림 규칙 | 박스 문장("본문의 `status`로만")과 6절 순서도가 같은 말을 해서 `settle()`을 그대로 옮겼다 |
| 3.6 ping 결과 표 | 일부러 틀렸을 때 `status 401`·`response_signature_valid: false`가 표의 줄과 정확히 맞았다 |
| 3.8 진단 | `PATH_WITHOUT_V1`이 `verified: true`로 바로 나왔다. 받는 쪽 응답 서명 누락도 `RESPONSE_SIGNATURE_MISSING`(확인됨)으로 잡았다 |
| 4.2·4.3 | "응답은 200·404 둘뿐", "4xx 준 거래는 저장하지 않는다" 같은 단정 문장이라 분기 코드가 짧았다 |
| `README.md` "로컬에서 한 명령으로" | 한 명령으로 허브·902가 뜨고 초대 문구에 키·지문·초대 코드가 다 있어서 관리자 없이 끝까지 갔다 |

## 시간 감각

- SPEC 읽기(빠른 시작 + 참조 2~6절)가 가장 길었다. 1,856줄이지만 빠른 시작(약 390줄)만으로 끝까지 갈 수 있었고, 나머지는 "오류 코드를 어떻게 나누나"·"응답 형식이 정확히 뭔가"를 찾을 때만 봤다.
- 구현에서는 **받는 쪽 응답 서명 미들웨어(`res.end` 가로채기)**에 가장 오래 생각했다. 문서 도움이 없는 유일한 부분이었다 (N1).
- 서명 벡터·등록·이체·셀프 시험은 모두 첫 실행에 통과해 디버깅 시간이 0이었다.

# node-bank — ClearingHub 참가 은행 Node.js 참고 구현

[SPEC.md](../../docs/SPEC.md)(v1.2.1)**만** 보고 만든 Node.js(Express) 은행이다. 받는 쪽(허브가 부르는 4절 API 3개 + 응답 서명)과 보내는 쪽(3절 호출 + 허브 응답 서명 검증)이 다 있다. 규격의 기준은 언제나 SPEC이고, 이 코드는 읽고 베끼기 위한 것이다.

- 의존성: `express` 하나. 서명은 Node 내장 `crypto`(Ed25519), HTTP 호출은 내장 `fetch`
- Node.js **20.12 이상** (`process.loadEnvFile`, `fetch`). v24에서 확인
- **저장은 메모리**(`Map`)라 연습용이다. 실제 서버에서 지킬 것은 아래 "실제 서버로 옮길 때"
- 로컬 허브에서 실제로 돌린 결과: [RESULTS.md](RESULTS.md). 만들면서 찾은 SPEC의 빈틈: [SPEC_FEEDBACK.md](SPEC_FEEDBACK.md)

| 파일 | SPEC | 하는 일 |
|---|---|---|
| `src/signing.js` | 2.2, 2.3, 2.5, 2.6 | 키 만들기, 지문, 요청 서명·검증, 응답 서명·검증. raw 32바이트 키를 DER 접두사로 읽는다 |
| `test/signing.test.js` | 2.3 예시 1·2, 2.5 예시 3 | 고정 값이 그대로 나오는지 (`node --test`) |
| `src/bank.js` | 4.1~4.3, 2.5, 5절 | 받는 쪽 서버 (포트 3000, `/interbank`). 허브 서명 검증, 멱등, 오류 형식, 응답 서명 미들웨어 |
| `src/register.js` | 3.5 | 셀프 등록, `bank_code`·`api_key`를 `.env`에 저장, 허브 지문 확인 |
| `src/hub.js` | 3절, 2.5 | 허브 호출 공통 (서명, 응답 서명 검증) |
| `src/send.js` | 3.1~3.4, 6절 | 902로 이체, 되돌림 규칙(`status: "FAILED"`일 때만). `--wrong-path`는 일부러 틀리기 (3.8) |
| `src/selftest.js` | 3.7 | 셀프 시험 `test-receive` → `resend` → `status`, 없는 계좌. `--slow`는 지연 시험 |

## 순서 (복붙 명령)

명령은 모두 이 폴더(`examples/node-bank`)에서 실행한다. 로컬 연습은 허브 저장소 루트에서 `uv run python -m clearing_hub.devserver`로 허브와 가짜 은행 902를 먼저 띄운다 (저장소 `README.md`의 "로컬에서 한 명령으로").

### 1. 설치·서명 확인

```bash
npm install
```
```bash
node --test
```

`pass 6`이면 SPEC 2.3·2.5의 예시 값과 같다.

### 2. 키 쌍 만들기 (한 번만)

```bash
node src/signing.js keygen > .env
```

`.env`에 `BANK_PRIVATE_KEY`·`BANK_PUBLIC_KEY`가 들어간다 (화면에는 공개키 지문이 나온다). `.env`는 `.gitignore`에 있다. 개인키는 Git·채팅·허브 누구에게도 주지 않는다.

### 3. 초대 문구의 값 넣기

허브 관리자에게 받은 초대 문구(로컬이면 devserver 콘솔)의 값을 `.env` 끝에 더한다.

```bash
cat >> .env <<'EOF'
HUB=http://127.0.0.1:8000
HUB_PUBLIC_KEY=<초대 문구의 허브 공개키>
HUB_FINGERPRINT=<초대 문구의 허브 공개키 지문>
INVITE_CODE=<초대 코드 inv_…>
BANK_NAME=NodeBank
BASE_URL=http://127.0.0.1:3000/interbank
EOF
```

운영 허브는 `HUB=https://clearing-hub.ai-agent-develop.cloud`, `BASE_URL`은 공인 인증서의 공개 HTTPS 주소다 (SPEC 3.5).

### 4. 받는 쪽 서버 띄우기 (다른 터미널, 계속 켜 둔다)

```bash
node src/bank.js
```

계좌: `110-123-000001` 김민수(셀프 시험용), `110-123-000002` 이영희, `110-123-000009` 박지연(입금 후 6초 늦게 답함, 지연 시험용).

### 5. 셀프 등록

```bash
node src/register.js
```

`201`, `ping.ok: true`, `hub_fingerprint ... 같음`이면 된 것이다. `BANK_CODE`·`BANK_API_KEY`가 `.env`에 저장된다 (**`api_key`는 이 응답에만 있다**). `register.js`는 응답의 `hub_fingerprint` 문자열만 보지 않고, `HUB_PUBLIC_KEY`로 지문을 직접 계산해 맞추고 등록 응답의 서명도 그 키로 검증한다.

### 6. 보내는 쪽 시험 (902로)

```bash
node src/send.js
```

모든 줄이 `허브 응답 서명 맞음`, 예금주 `김철수`, 이체 `200 COMPLETED`, 계좌 `110-123-0002`는 `404`·`status: FAILED`(되돌림), 같은 `transfer_id` 재전송은 처음과 같은 결과.

### 7. 받는 쪽 시험 (셀프 시험)

```bash
node src/selftest.js
```

`receiver_reply.signature_valid: true`, 재전송 뒤에도 "이 거래 1건", 없는 계좌는 `FAILED`. 하루 20번까지다. 지연 시험까지 하려면 `node src/selftest.js --slow` (70초 걸림).

### 8. 401이 나면

```bash
node src/send.js --wrong-path
```

일부러 서명 PATH에서 `/v1`을 빼고 보내, 401 본문의 `hint`와 `GET /v1/diagnose/failures`를 보여 준다. 진짜로 401이 날 때도 같은 두 곳을 본다 (SPEC 3.8). 등록 뒤 받는 쪽을 고쳤으면 `POST /v1/me/ping`으로 다시 확인한다: `node -e "require('./src/config'); require('./src/hub').call('POST','/v1/me/ping').then(r=>console.log(r.body))"`. ping 결과에는 `hint`가 없으니, ping이 `ok: false`면 `diagnose/failures`의 `HUB_TO_BANK` 항목을 본다.

## Node.js에서 틀리기 쉬운 곳 (이 예제의 풀이)

| 무엇 | 풀이 | 코드 |
|---|---|---|
| raw 32바이트 키 | Node `crypto`는 DER만 받으므로 개인키 앞에 `302e020100300506032b657004220420`(PKCS#8), 공개키 앞에 `302a300506032b6570032100`(SPKI)을 붙여 `createPrivateKey`/`createPublicKey`로 읽는다. 만들 때는 반대로 DER의 끝 32바이트 | `signing.js` |
| base64 | `Buffer.from(x, 'base64')`는 틀린 문자를 조용히 버린다. 정규식과 길이(키 32, 서명 64바이트)를 먼저 본다 | `strictB64` |
| 본문 직렬화 | `JSON.stringify`가 SPEC 예시 1의 바이트와 같다 (공백 없음, 한글 그대로). 서명한 Buffer를 그대로 보낸다 | `hub.js` |
| 시각 | `Date.now()`는 밀리초. `Math.floor(Date.now() / 1000)` | `signing.js` |
| 원본 바이트 받기 | `express.json()`이 아니라 `express.raw({ type: () => true })`. 본문 없는 GET은 `req.body`가 `{}`(Express 5는 `undefined`)라 빈 Buffer로 바꾼다 | `bank.js` ② |
| 응답 서명 | 가장 먼저 등록한 미들웨어에서 `res.end`를 감싸, 실제로 나가는 본문 바이트로 서명해 헤더를 붙인다. 401·404·422에도 붙는다. `res.json`을 감싸 다시 `JSON.stringify`하지 않는다. ETag는 끈다 | `bank.js` ① |
| 서명 PATH (받는 쪽) | 라우터를 `app.use('/interbank', router)`로 붙이면 라우터 안의 `req.path`가 이미 `base_url` 뒤 부분(`/receive`)이다 | `bank.js` ③ |
| 허브 응답 검증 | `fetch` 응답은 `res.json()` 전에 `arrayBuffer()`로 바이트를 받아 검증한다. Buffer 본문이면 `Content-Type`을 직접 넣는다 | `hub.js` |

## 실제 서버로 옮길 때 지킬 것 (SPEC 4.2, 6절)

이 예제는 메모리 `Map`이고 Node가 한 스레드라 "있는지 확인 → 입금 → 기록" 사이에 다른 요청이 끼어들지 않는다. **DB로 옮기면 `await` 사이에 같은 `transfer_id`의 재전송이 끼어든다.** 그래서:

1. **`transfer_id`에 유니크 제약**을 건 입금 기록 테이블을 둔다 (4.2 규칙 2).
2. **입금(잔액 변경)과 `transfer_id` 기록을 한 트랜잭션**으로 한다. 입금만 되고 기록이 안 되면 다음 재전송에서 두 번 들어간다.
3. 유니크 제약에 걸린 쪽은 **절대 4xx(`409` 포함)로 답하지 않는다**. 롤백하고 저장된 결과를 읽어 `200 COMPLETED`로, 아직 못 읽으면 `5xx`로 답한다 (4xx면 허브가 `FAILED`로 끝내 보내는 은행이 되돌린다 → 이중 지급). PostgreSQL 예:
   ```sql
   BEGIN;
   INSERT INTO received_transfers (transfer_id, to_account, amount, ...) VALUES ($1, $2, $3, ...)
     ON CONFLICT (transfer_id) DO NOTHING RETURNING transfer_id;
   -- 행이 안 나오면: ROLLBACK, 저장된 행을 읽어 200 (못 읽으면 503)
   UPDATE accounts SET balance = balance + $3 WHERE account_num = $2;
   COMMIT;  -- 그다음에 200 COMPLETED
   ```
4. **4xx로 거절한 거래는 기록하지 않는다** (그래야 4.3이 404). 입금을 끝내지 않았으면 2xx로 답하지 않는다 (일시 장애는 5xx).
5. 보내는 쪽은 **출금(중간 계정으로)을 한 트랜잭션으로 끝낸 뒤** `POST /v1/transfers`를 부르고, 되돌림은 본문 `status: "FAILED"`일 때만. 202·`status` 없는 4xx·5xx·타임아웃은 같은 `transfer_id`로 재전송하거나 `GET /v1/transfers/{id}`로 확인한다. 서버가 재시작되면 `PENDING` 거래를 다시 확인한다 (6절).
6. `bank.js`의 `/_local/received`는 연습용 확인 창구다. 실제 서버에는 두지 않는다. 서버는 공개 HTTPS(공인 인증서) 뒤에 두고, 서명된 요청·응답 원문을 이체 관련 호출만이라도 보관한다 (2.5).
7. 같은 `transfer_id`가 **동시에** 두 번 오는 경우는 허브로 일으킬 수 없다. 시험 환경에서 `HUB_PUBLIC_KEY`를 SPEC 2.3의 예시 허브 공개키로 바꾸고, 예시 허브 개인키로 서명한 `/receive`를 동시에 두 번 보내 입금 1번·4xx 없음을 확인한다 (7절 7). 운영 설정에는 예시 키를 넣지 않는다.

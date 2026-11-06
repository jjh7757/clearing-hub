# ClearingHub 참가 은행 연동 규격 v1.0 (초안)

- 작성일: 2026-11-05 (최종 수정 2026-11-06)
- 대상: ClearingHub(은행 간 이체 중계 서버)에 참가하려는 은행 서버 개발팀
- 상태: **v1.0 초안**. 공개키 서명·응답 서명(부인방지, 2.3·2.5·2.6)을 더한 판이다. HMAC으로 연동한 은행은 **아무것도 바꾸지 않아도 그대로 동작한다** (모드 `hmac`). 8절 19와 다른 팀의 전환 일정을 정한 뒤 확정한다
- 배경·전체 설계: [PROJECT_PLAN.md](PROJECT_PLAN.md) (허브 저장소 안의 문서. 이 문서만 받았다면 읽지 않아도 연동에 지장이 없다)

이 문서는 두 가지를 정한다.

1. 은행이 허브에 **보내는** 요청 (3절)
2. 은행이 허브로부터 **받기 위해 만들어야 하는** API (4절)

둘 다 같은 인증 방식(2절)과 오류 형식(5절)을 쓴다. 9절의 본인 명의 계좌 조회는 참여 여부를 은행이 고르는 선택 기능이다. 10절의 정산은 허브가 매일 은행 사이의 차액을 모의 예치금으로 옮기는 것이다. 대사(자기 장부와 맞춰 보기)는 선택이지만, 예치금·순채무한도가 모자라면 송금이 거절된다는 점(10.2)은 모든 은행에 적용된다.

### 구현 체크리스트

연동을 마쳤다고 보기 전에 아래를 모두 확인한다. 괄호는 근거가 되는 절이다.

**받는 쪽 (허브가 부르는 API)**
- [ ] `{base_url}/inquiry`, `/receive`, `/receive/{transfer_id}` 3개를 만들었다. 9절에 참여하면 `/accounts`도 만들었다 (4절, 9.4)
- [ ] 서명은 JSON으로 풀기 **전의 원본 바이트**로 검증하고, `X-Bank-Code`가 `"000"`인지 본다. 검증을 입금 처리보다 먼저 한다 (2.2, 4.2)
- [ ] 서명 경로는 서버가 받은 전체 경로가 아니라 **`base_url` 뒤의 부분**(`/receive` 등)으로 검증한다. 프록시가 앞부분을 바꿔도 같다 (2.2)
- [ ] `/inquiry`의 200 응답에 `holder_name`이 항상 들어 있다 (4.1)
- [ ] `/receive`는 `transfer_id`에 유니크 제약을 걸고, 입금과 ID 저장을 한 트랜잭션으로 한다. 같은 ID가 다시 오면 입금하지 않고 200을 준다 (4.2)
- [ ] 같은 ID가 동시에 와서 유니크 제약에 걸린 쪽은 4xx(`409` 포함)로 답하지 않고 200 또는 5xx로 답한다 (4.2 규칙 2. 4xx면 이중 지급)
- [ ] 입금을 끝내지 않았으면 2xx로 답하지 않는다. 거절은 4xx + 5절 형식, 일시 장애는 5xx (4.2)

**보내는 쪽 (허브를 부르는 코드)**
- [ ] 출금(중간 계정으로 옮기기)을 끝낸 **뒤에** `POST /v1/transfers`를 부른다 (3.3, 6절)
- [ ] 출금을 되돌릴지는 **본문의 `status`가 `"FAILED"`인지로만** 정한다. 202, `status` 없는 4xx, 5xx, 타임아웃이면 되돌리지 않는다 (3.3)
- [ ] 서명에 메서드와 `/v1`부터의 경로를 넣는다. 2.3의 예시 두 개와 값이 같은지 확인했다 (2.2, 2.3)
- [ ] 재전송할 때는 같은 `transfer_id`를 쓰고 서명은 새로 만든다. 새 이체에는 새 `transfer_id`를 쓴다 (2.2, 3.3)
- [ ] 결과를 모르는 거래는 `GET /v1/transfers/{id}`로 확인하고, 서버가 재시작되면 `PENDING` 거래를 다시 확인한다 (3.4, 6절)
- [ ] 결과 조회가 `404 TRANSFER_NOT_FOUND`면 바로 되돌리지 않고 같은 `transfer_id`로 POST를 다시 보낸다 (3.4)
- [ ] 받는 은행 선택 목록에서 자기 은행을 뺀다 (3.1)
- [ ] 예치금·순채무한도가 모자라면 접수 전에 `LIMIT_EXCEEDED`(`status: "FAILED"`)로 거절된다는 것을 안다. 시험 전에 허브 관리자에게 예치금을 요청했고, `GET /v1/reserve`로 `available`을 볼 수 있다 (10.2)

**정산 (선택)**
- [ ] 매일 `GET /v1/settlements`로 run을 받아, `GET /v1/settlements/{run_id}`의 `transfers`를 자기 장부와 `transfer_id`·금액으로 맞춰 본다. `DRAFT`는 비교만, `SETTLED`면 내부 계정 정리, `CANCELLED`는 무시한다 (10.5, 10.6)
- [ ] 안 맞으면 자동 처리를 멈추고 허브 관리자에게 `run_id`와 차이 나는 `transfer_id`를 알린다 (10.6)

**부인방지 (선택, `ed25519` 모드로 전환할 때)**
- [ ] Ed25519 키 쌍을 만들어 개인키는 서버 환경 변수에만 두었다. 2.3의 예시 1·2와 2.5의 예시 3 값이 내 구현과 같다 (2.3, 2.5)
- [ ] `GET /v1/hub-key`의 공개키를 허브 관리자가 따로 알려 준 지문과 맞춰 보고 설정에 저장했다 (2.6)
- [ ] 받는 쪽이 `X-Signature-Alg: ed25519` 요청을 허브 공개키로, 그 밖의 요청은 secret으로 검증한다. 4절 API의 2xx·4xx 응답에 응답 서명을 붙인다. `request_signature`는 그 요청의 `X-Signature` 그대로다 (2.5)
- [ ] `PUT /v1/keys`로 공개키를 등록했고 `GET /v1/keys`의 `fingerprint`가 내 키와 같다 (2.6)
- [ ] 관리자가 `both`로 바꾼 뒤 보내는 쪽을 Ed25519로 바꿨고, 그다음 `ed25519` 전환을 요청했다. ping의 `response_signature_valid`가 `true`다 (2.6, 7절 4)
- [ ] 서명된 요청·응답 원문(바이트·헤더)을 이체 관련 호출만이라도 보관한다 (2.5)

**운영**
- [ ] 서버 시계를 NTP로 맞췄다 (2.2)
- [ ] `base_url`이 공인 인증서를 쓰는 공개 HTTPS 주소이고, 허브 관리자의 ping이 정상으로 나온다 (7절 4)
- [ ] 7절 5의 시험 순서를 가짜 은행 `902`로 모두 해 봤다 (보내는 쪽)
- [ ] 허브 관리자에게 요청해 `901`에서 이체를 받아 7절 5의 받는 쪽 시험을 해 봤다

---

## 1. 기본 규칙

| 항목 | 규칙 |
|---|---|
| 프로토콜 | HTTPS, JSON (`Content-Type: application/json; charset=utf-8`) |
| 문자 | UTF-8 |
| 금액 | **정수, 원 단위** (예: 50000). 소수·문자열·음수·0은 `VALIDATION_ERROR`(422)로 거절. 허브 1회 상한(1억 원) 초과는 `LIMIT_EXCEEDED`(400). 보내는 은행의 미정산 순채무 + 금액이 허브 예치금 + 순채무한도를 넘어도 `LIMIT_EXCEEDED`(400, `status: "FAILED"`)로 거절되며, `message`로 1회 상한과 구분한다 (10절) |
| 은행 코드 | 허브가 배정하는 숫자 3자리 문자열 (예: `"901"`). 앞자리 0이 있을 수 있으므로 문자열로 다룬다 |
| 계좌번호 | 문자열. 허브는 형식을 검사하지 않고 받는 은행에 그대로 전달한다. 하이픈 포함 여부는 각 은행이 정한다 (예: `"110-123-456789"`) |
| 거래 ID (`transfer_id`) | 보내는 은행이 만드는 UUID v4 문자열(소문자). **같은 ID는 어디서든 한 번만 처리된다** (멱등성 키) |
| 시각 | ISO 8601, UTC, 초 단위 (`"2026-11-05T01:20:00Z"`). 응답에만 쓰인다 |
| 이름 | `sender_name`, `holder_name`은 최대 50자 |
| 메모 | `memo`는 최대 100자. 선택 항목이며, 빼고 보내면 `""`로 본다. 허브가 받는 은행에 보낼 때(4.2)는 항상 넣는다 |
| 타임아웃 | 허브 → 은행 호출은 **5초**. 은행 → 허브 호출은 10초를 권장 |
| 모르는 필드 | 요청·응답에 이 문서에 없는 필드가 있으면 **무시한다** (거절하지 않는다). 허브도 그렇게 하며, 은행도 그렇게 만든다. 나중에 필드를 더해도 기존 서버가 깨지지 않게 하기 위해서다 |

각 API의 필드 규칙은 해당 절의 **필드 표**에 있다. 표에 "필수"인 필드가 없거나 타입·길이가 틀리면 `VALIDATION_ERROR`(422)다.

---

## 2. 인증: 공개키 서명(권장) / HMAC(전환 기간)

은행 → 허브, 허브 → 은행 요청은 **모두** 서명한다. 받는 쪽은 서명이 맞을 때만 처리한다. 서명 방식은 둘이다.

| 방식 | 서명하는 키 | 검증하는 키 | 부인방지 |
|---|---|---|---|
| **Ed25519 공개키 서명** (권장, 2.3·2.5·2.6) | 보내는 쪽의 **개인키** (그 기관만 갖는다) | 보내는 쪽의 공개키 | 된다. 서명이 있으면 보낸 사실을 부인할 수 없고, 상대는 그 서명을 만들 수 없다 |
| **HMAC-SHA256** (전환 기간) | 은행과 허브가 나눠 가진 secret | 같은 secret | 안 된다. 허브도 secret을 갖고 있어 은행 명의의 요청을 만들 수 있고, 은행도 허브 명의의 요청을 만들 수 있다 |

두 방식의 서명 메시지(2.2)는 **같은 문자열**이다. 바뀌는 것은 서명 함수와 헤더 `X-Signature-Alg` 하나다. 허브 관리자가 은행마다 **서명 모드**를 정하며, 처음에는 모두 `hmac`이다. HMAC으로 연동한 은행은 **아무것도 바꾸지 않아도 지금처럼 동작한다.**

| 모드 | 은행 → 허브 요청 | 허브 → 은행 요청 | 은행의 응답 서명 (2.5) |
|---|---|---|---|
| `hmac` (기본) | HMAC만 받는다 | HMAC | 요구하지 않는다 |
| `both` | HMAC·Ed25519 둘 다 받는다 (Ed25519는 공개키 등록 뒤, 2.6) | HMAC | 요구하지 않는다 |
| `ed25519` | Ed25519만 받는다. HMAC은 401 `SIGNATURE_INVALID` | Ed25519 (허브 개인키) | **필수.** 없거나 틀리면 허브는 그 응답을 "응답 없음"으로 본다 |

모드는 허브 관리자만 바꾼다. 공개키를 등록해도 모드는 바뀌지 않는다. 바꾸는 순서는 2.6이다. 허브의 응답 서명은 모드와 상관없이 모든 `/v1` 응답에 붙는다 (2.5).

**부인방지는 `ed25519`로 전환한 은행에서만 성립한다.** `hmac`·`both` 은행과의 거래는 지금처럼 secret을 나눠 가진 상태라 "누가 보냈는지"를 제3자에게 증명할 수 없다.

**HMAC secret (`hmac`·`both` 모드)**

허브는 은행마다 **API 키**와 **서명 비밀키(secret)**를 발급한다. API 키(`X-Api-Key`)는 모드와 상관없이 은행 → 허브 요청에 항상 넣는다.

**secret은 은행 하나에 하나이고 양방향에 같이 쓴다.** 은행은 허브에 보낼 때 이 secret으로 서명하고, 허브는 그 은행을 부를 때(4절·9.4) **같은 secret**으로 서명한다. 그래서 은행은 4절 API에서 받은 서명도 이 secret 하나로 검증하면 된다. 허브 관리자가 키를 다시 발급하면(`POST /admin/banks/{bank_code}/keys`) API 키와 secret이 함께 바뀌고, 그 순간부터 허브도 새 secret으로 서명한다. 따라서 은행은 새 값을 받는 즉시 보내는 쪽과 검증하는 쪽 설정을 같이 바꿔야 한다. 재발급은 Ed25519 공개키와 서명 모드를 바꾸지 않는다.

**예전 secret을 함께 받아 주는 기간은 없다** (Ed25519 공개키는 24시간 있다, 2.6). 허브가 바꾼 뒤 은행이 설정을 바꾸기 전까지는 다음 일이 생긴다.

- 그 은행이 허브에 보내는 요청: `SIGNATURE_INVALID`(401, `status` 없음). 이체 결과가 아니므로 되돌리지 않고, 설정을 바꾼 뒤 같은 `transfer_id`로 재전송하면 된다 (6절)
- 허브가 그 은행에 보내는 입금(4.2): 은행이 401로 거절하므로 이체가 `FAILED`(`RECEIVER_ERROR`)로 끝나고, 보내는 은행이 출금을 되돌린다. 돈은 안전하지만 그 사이의 이체는 실패한다

그래서 재발급은 비상시(유출)가 아니면 허브 관리자와 은행 팀이 **시각을 미리 맞춰** 한다.

### 2.1 헤더

| 헤더 | 값 |
|---|---|
| `X-Bank-Code` | 요청을 보내는 쪽의 은행 코드. 허브가 보낼 때는 `"000"` |
| `X-Api-Key` | 발급받은 API 키. 은행 → 허브에만 쓴다 (모든 모드). **허브 → 은행 요청에는 넣지 않는다** (8절 3) |
| `X-Timestamp` | Unix 시각(초), 10진 문자열. 예: `"1762300800"` |
| `X-Signature` | 아래 방법으로 만든 서명. HMAC은 소문자 16진수 64자, Ed25519는 base64 88자 |
| `X-Signature-Alg` | `ed25519` 또는 `hmac`. **없으면 `hmac`으로 본다** (지금 은행은 넣지 않아도 된다). 그 밖의 값은 `SIGNATURE_INVALID`. 서명 대상이 아니다. 허브는 HMAC으로 보낼 때도 `hmac`을 넣는다 (모르는 헤더는 무시, 1절) |
| `X-Request-ID` | (선택) 로그 추적 ID. 서명 대상 아님 (2.4) |

### 2.2 서명 만들기

```
message   = X-Timestamp + "\n" + METHOD + "\n" + PATH + "\n" + body

HMAC:     X-Signature = hex( HMAC-SHA256( key = secret, message = UTF-8 bytes of message ) )
Ed25519:  X-Signature = base64( Ed25519-Sign( 보내는 쪽 개인키, UTF-8 bytes of message ) ),  X-Signature-Alg: ed25519
```

- `METHOD`는 HTTP 메서드 대문자다 (`GET`, `POST`, `PUT`).
- `PATH`는 **이 문서에 적힌 경로**다. 서버가 실제로 받은 경로가 아니다.

  | 방향 | `PATH` | 예 |
  |---|---|---|
  | 은행 → 허브 (3절·9.2·10절·2.6) | `/v1`부터의 경로 | `/v1/transfers`, `/v1/transfers/7c9e6679-…`, `/v1/settlements/12`, `/v1/keys` |
  | 허브 → 은행 (4절·9.4) | `base_url` **뒤의 부분** | `/inquiry`, `/receive`, `/receive/7c9e6679-…`, `/accounts` |

  `base_url`이 `https://b-bank.example.com/interbank`이면 허브가 부르는 주소는 `…/interbank/receive`지만 서명의 `PATH`는 `/receive`다. 은행 앞단의 프록시가 `/interbank`를 떼거나 `/api`를 붙여도 서명은 그대로 맞는다. 검증하는 쪽은 받은 경로에서 자기 `base_url` 경로 부분을 떼고 비교한다.
- 쿼리스트링은 쓰지 않는다. 이 문서의 API는 모두 쿼리 없이 부른다. 끝의 `/`도 붙이지 않는다.
- `body`는 **실제로 전송하는 바이트 그대로**다. 서명한 뒤에 JSON을 다시 직렬화하면 안 된다 (키 순서·공백이 바뀌면 서명이 틀린다). 본문이 없는 GET은 `body = ""`.
- 받는 쪽은 `X-Timestamp`가 현재 시각과 **5분(300초)** 넘게 차이 나면 `SIGNATURE_EXPIRED`로 거절한다 (재전송 공격 방지). 서버 시계는 NTP로 맞춰 둔다. 시계가 틀어지면 모든 요청이 `SIGNATURE_EXPIRED`로 거절된다.
- 재전송할 때는 `X-Timestamp`와 `X-Signature`를 **새로 만든다**. 본문은 처음과 같아도 된다.
- HMAC 비교는 상수 시간 비교(`hmac.compare_digest`)를 쓴다. Ed25519는 라이브러리의 `verify`를 쓴다.
- Ed25519의 키와 서명은 **raw 바이트의 표준 base64**(`+`·`/`, 패딩 `=` 포함)다. 개인키·공개키는 32바이트(44자), 서명은 64바이트(88자). URL-safe base64나 PEM·DER 형식이 아니다.
- 받는 쪽은 `X-Signature-Alg`를 보고 분기한다: `ed25519`면 보낸 쪽 공개키(은행 → 허브는 등록된 공개키, 허브 → 은행은 허브 공개키 2.6)로, 없거나 `hmac`이면 secret으로. 상대의 모드가 받지 않는 방식이면 `SIGNATURE_INVALID`.

### 2.3 예시 (검증용)

**HMAC**

예시 1: 은행 → 허브 이체 요청

```
secret      = example-secret-change-me
X-Timestamp = 1762300800
METHOD      = POST
PATH        = /v1/transfers
body (190 bytes, 공백 없음) =
{"transfer_id":"7c9e6679-7425-40de-944b-e07fc1f90ae7","to_bank":"902","to_account":"110-123-0001","from_account":"110-200-456789","amount":50000,"sender_name":"홍길동","memo":"생활비"}

X-Signature = 1afb099015e80126587211e3a2ef82bfbb9c0a4af2d9065519029985e7797f65
```

예시 2: 허브 → 은행 입금 확인 (본문 없는 GET)

```
secret      = example-secret-change-me
X-Timestamp = 1762300800
METHOD      = GET
PATH        = /receive/7c9e6679-7425-40de-944b-e07fc1f90ae7
body        = (빈 문자열)

X-Signature = d1c18e703107fcf4132c301e468014f1487099ec302b0571806694972ea5bc89
```

예시 2의 `message`는 `"1762300800\nGET\n/receive/7c9e6679-7425-40de-944b-e07fc1f90ae7\n"`이다. 본문이 비어도 경로 뒤의 `\n`은 넣는다.

Python 참고 구현:

```python
import hmac, hashlib, time

def _digest(secret: str, ts: str, method: str, path: str, body: bytes) -> str:
    msg = f"{ts}\n{method.upper()}\n{path}\n".encode() + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()

def sign(secret: str, method: str, path: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    return {"X-Timestamp": ts, "X-Signature": _digest(secret, ts, method, path, body)}

def verify(secret: str, ts: str, method: str, path: str, body: bytes, signature: str, skew: int = 300) -> bool:
    # 헤더 값이 숫자가 아니거나 ASCII가 아니면 예외(500) 대신 False(401)로 끝낸다
    try:
        if abs(int(time.time()) - int(ts)) > skew:
            return False
        return hmac.compare_digest(_digest(secret, ts, method, path, body).encode(), signature.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False
```

위 `verify`는 맞음·틀림만 돌려준다. 오류 코드를 나눠야 하면(5절) 헤더 없음은 `UNAUTHORIZED`, 시각 차이 초과는 `SIGNATURE_EXPIRED`, 그 밖의 실패는 `SIGNATURE_INVALID`로 답한다. 어느 경우든 401이다.

FastAPI에서 본문 바이트는 `await request.body()`로 받아 그대로 검증한 뒤 JSON으로 푼다. 은행이 4절 API를 검증할 때 `PATH`는 받은 경로에서 `base_url` 경로 부분 뒤만 쓴다. 예를 들어 라우터 접두사가 `/interbank`이면 `request.url.path.partition("/interbank")[2]`다.

**Ed25519**

예시용 키 쌍 (**검증 전용**. 모두에게 공개된 값이므로 운영에서는 쓰지 않는다):

```
은행 개인키 = D7frsUqSl9ZuC+7EpHfY1+w6+dAlWqg8vuHVtZfdpg4=
은행 공개키 = gxCDPKbN/rIXKDJgn4GZh5JY7I65Cwbk3A/mJjTtPzY=     지문 d61a340fe933017d
허브 개인키 = cycR/h4ydSTPkBGuRFO340cDtdAdwrXQjKC4N1GPgtQ=
허브 공개키 = yZdBWoR7swp/3CQvh4c58oahhbjxVeaxWWtqmqwDXdo=     지문 239a9acfc7ecb8de
```

Ed25519는 같은 키·같은 메시지면 서명이 **항상 같다**. 그래서 아래 값으로 자기 구현을 확인할 수 있다. 메시지는 HMAC 예시와 같다.

```
예시 1 (은행 → 허브, 은행 개인키):  X-Timestamp 1762300800, POST /v1/transfers, 위 190바이트 body
X-Signature     = SY/3NCjTxAR1dC5UXHM/PkQZR9/SMa+wcDBSIrAit0jeHW5JKKgw4vZWKiN2Z7o5V+ywCY7EyWsTEfLf/F3mBw==
X-Signature-Alg = ed25519

예시 2 (허브 → 은행, 허브 개인키):  X-Timestamp 1762300800, GET /receive/7c9e6679-7425-40de-944b-e07fc1f90ae7, body ""
X-Signature     = IMn6yCKkn/eZGPNY74b3+cNvA/4ax9knE97hh4wCQnPEHe6IEdKREh/Pit3+7bJJjx801OiRbJIvJD0HbKzPAA==
X-Signature-Alg = ed25519
```

Python 참고 구현 (`pip install cryptography`):

```python
import base64, time
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

def generate_keypair() -> tuple[str, str]:
    """(개인키, 공개키) base64. 한 번 만들어 개인키는 서버 환경 변수에만 둔다."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()

def _sign(private_key_b64: str, message: bytes) -> str:
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(key.sign(message)).decode()

def _verify(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    # 키·서명이 base64가 아니거나 길이가 틀려도 예외(500) 대신 False(401)
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature_b64, validate=True), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False

def sign_ed25519(private_key_b64: str, method: str, path: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    msg = f"{ts}\n{method.upper()}\n{path}\n".encode() + body
    return {"X-Timestamp": ts, "X-Signature": _sign(private_key_b64, msg), "X-Signature-Alg": "ed25519"}

def verify_ed25519(public_key_b64: str, ts: str, method: str, path: str, body: bytes, signature: str,
                   skew: int = 300) -> bool:
    try:
        if abs(int(time.time()) - int(ts)) > skew:
            return False
    except (ValueError, TypeError):
        return False
    return _verify(public_key_b64, f"{ts}\n{method.upper()}\n{path}\n".encode() + body, signature)
```

허브의 구현은 [`src/clearing_hub/core/signing.py`](../src/clearing_hub/core/signing.py)다 (저장소를 받았을 때).

### 2.4 추적 ID `X-Request-ID` (선택)

이체 하나를 보내는 은행 → 허브 → 받는 은행 세 서버의 로그에서 같은 ID로 찾기 위한 헤더다. **선택**이며, 없어도 모든 API가 똑같이 동작한다.

| 항목 | 규칙 |
|---|---|
| 형식 | 영문·숫자·`.`·`_`·`:`·`-` 1~128자 (정규식 `^[A-Za-z0-9._:-]{1,128}$`). UUID를 권장 |
| 서명 | **서명 대상이 아니다** (2.2의 `message`에 넣지 않는다). 로그 추적용이라 바뀌어도 돈의 흐름에는 영향이 없다 |
| 보내는 은행 → 허브 | 은행이 자기 요청 ID를 넣어 보낸다. 없거나 형식이 틀리면 허브가 새로 만든다 (**거절하지 않는다**) |
| 허브 응답 | 모든 응답에 그 요청의 `X-Request-ID`를 붙인다 (은행이 보낸 값 또는 허브가 만든 값) |
| 허브 → 받는 은행 | 그 요청으로 부르는 4.1·4.2·9.4에 같은 값을 넣는다. 이체는 접수 때 값을 거래에 저장해, 나중의 재시도(4.2 재전송, 4.3 입금 확인)에도 **처음 값**을 보낸다 |
| 받는 쪽이 할 일 | 로그에 남긴다. 값으로 분기하거나 거절하지 않는다 |

허브의 재시도 태스크가 부르는 호출에도 같은 값이 가므로, 받는 은행은 "처음 요청과 재시도"를 한 ID로 묶어 볼 수 있다. 3.4 결과 조회처럼 보내는 은행이 나중에 따로 부르는 요청은 새 ID여도 된다 (거래는 `transfer_id`로 찾는다).

### 2.5 응답 서명

요청 서명은 "누가 보냈는지"를, 응답 서명은 "**그 요청에 무엇이라고 답했는지**"를 증명한다. 받는 은행이 `200 COMPLETED`라고 답해 놓고 나중에 "그런 적 없다"고 하거나, 허브가 "그런 응답 받은 적 없다"고 할 때 서명된 응답으로 반박한다.

```
message   = X-Response-Timestamp + "\n" + STATUS + "\n" + request_signature + "\n" + response_body
signature = base64( Ed25519-Sign( 답하는 쪽 개인키, UTF-8 bytes of message ) )
```

| 헤더 | 값 |
|---|---|
| `X-Response-Timestamp` | 응답을 만든 Unix 시각(초), 10진 문자열 |
| `X-Response-Signature` | 위 서명, base64 88자 |
| `X-Signature-Alg` | `ed25519` |

- `STATUS`는 HTTP 상태 코드 10진수다 (`200`, `404`).
- `request_signature`는 **그 요청의 `X-Signature` 헤더 값 그대로**다 (HMAC 16진수든 Ed25519 base64든). 요청에 서명이 없었으면 빈 문자열. 응답을 그 요청에 묶어서, 다른 요청에 대한 진짜 응답을 가져다 붙이면 검증이 틀린다.
- `response_body`는 실제로 보내는 본문 바이트 그대로다. 본문이 없으면 `""`.
- 검증은 2.2와 같다: 답한 쪽의 공개키, 시각 차이 5분.

**누가 붙이는가**

| 응답 | 서명 |
|---|---|
| 허브의 `/v1/*` 응답 (2xx·4xx) | 은행의 모드와 상관없이 **항상** 붙는다 (허브 키가 설정된 뒤). 검증할지는 은행이 정한다 |
| `ed25519` 모드 은행의 4절·9.4 응답 (2xx·4xx) | **필수** |
| `hmac`·`both` 모드 은행의 응답 | 선택. 붙어 있으면 허브가 검증해 기록만 하고, 결과로 동작을 바꾸지 않는다 |
| 5xx | 붙이지 않아도 된다 (5절: 5xx는 본문 보장이 없다) |

**검증에 실패하면 "응답 없음"으로 본다.** 허브는 `ed25519` 모드 은행의 2xx·4xx 응답에 서명이 없거나, 틀리거나, 다른 요청에 묶여 있거나, 시각이 5분 넘게 차이 나면 그 응답을 받지 못한 것으로 처리한다.

| 호출 | 서명이 틀린 응답의 처리 |
|---|---|
| 4.2 입금 | 이체 `PENDING` → 1분 뒤 4.3으로 확인 (응답 타임아웃과 같다). 2xx여도 입금 완료로 보지 않고, 4xx여도 `FAILED`로 끝내지 않는다 |
| 4.3 입금 확인 | "확인 못 함" → 1분 뒤 다시 묻는다 |
| 4.1 수취 조회 | 보내는 은행에 `502 RECEIVER_ERROR` |

거절(`FAILED`)로 보지 않는 이유: 받는 은행이 입금을 끝냈는데 서명 설정만 틀렸을 때 `FAILED`로 끝내면 보내는 은행이 출금을 되돌려 **이중 지급**이 된다 (4.2와 같은 원칙: 모르면 되돌리지 않는다). 대신 응답 서명을 빠뜨린 `ed25519` 은행으로 가는 이체는 모두 `PENDING`을 거쳐 `UNKNOWN`으로 쌓이므로, 전환 전에 ping(7절 4)으로 확인한다.

보내는 은행이 허브 응답을 검증할 때도 같다. 3.3 응답의 서명이 틀리면 **본문을 믿지 않고** "모르는 상태"로 둔다 (되돌리지 않고 3.4로 확인, 6절).

예시 3: 예시 2(Ed25519 GET)에 대한 은행의 200 응답, 은행 개인키로 서명

```
X-Response-Timestamp = 1762300801
STATUS               = 200
request_signature    = IMn6yCKkn/eZGPNY74b3+cNvA/4ax9knE97hh4wCQnPEHe6IEdKREh/Pit3+7bJJjx801OiRbJIvJD0HbKzPAA==   (예시 2의 X-Signature)
body (138 bytes, 공백 없음) =
{"transfer_id":"7c9e6679-7425-40de-944b-e07fc1f90ae7","status":"COMPLETED","holder_name":"김철수","received_at":"2026-11-05T01:20:02Z"}

X-Response-Signature = D84tooOZEJ6jehp/BN6dVPw1KZCB+eejxpZOqgggeTW7iJznoyNjQGEFwShbhnX7prHkg9uG9e7rP9IxZS91Dg==
X-Signature-Alg      = ed25519
```

Python 참고 구현 (2.3의 `_sign`·`_verify`를 쓴다):

```python
def sign_response(private_key_b64: str, status: int, request_signature: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    msg = f"{ts}\n{status}\n{request_signature}\n".encode() + body
    return {"X-Response-Timestamp": ts, "X-Response-Signature": _sign(private_key_b64, msg), "X-Signature-Alg": "ed25519"}

def verify_response(public_key_b64: str, ts: str, status: int, request_signature: str, body: bytes,
                    signature: str, skew: int = 300) -> bool:
    try:
        if abs(int(time.time()) - int(ts)) > skew:
            return False
    except (ValueError, TypeError):
        return False
    return _verify(public_key_b64, f"{ts}\n{status}\n{request_signature}\n".encode() + body, signature)
```

본문 바이트가 정해진 **뒤에** 서명해야 한다. FastAPI는 엔드포인트에서 `JSONResponse`를 만든 다음 `response.body`로 서명해 헤더를 붙이거나, 순수 ASGI 미들웨어에서 `http.response.body`를 받아 서명한다 (허브는 [`core/response_signing.py`](../src/clearing_hub/core/response_signing.py)). 서명한 뒤에 본문을 바꾸는 미들웨어(압축 등)를 그 안쪽에 두지 않는다.

**보관 (권장)**: 분쟁 때 꺼낼 수 있게, 서명된 요청·응답의 **원본 바이트와 헤더**를 이체 관련 호출(3.3, 4.2, 4.3)만이라도 남겨 둔다. 허브는 이체마다 접수 요청, 4.2·4.3 요청과 응답 원문을 고치지 않고 보관한다.

### 2.6 키 등록·회전·전환 모드

**허브 공개키** `GET /v1/hub-key` (인증 없음)

```json
{ "algorithm": "ed25519", "public_key": "yZdBWoR7swp/3CQvh4c58oahhbjxVeaxWWtqmqwDXdo=", "fingerprint": "239a9acfc7ecb8de" }
```

- `fingerprint`(지문) = `sha256(raw 공개키 32바이트)`의 16진수 앞 16자.
- 처음 받을 때 **허브 관리자가 secret처럼 개별 경로로 알려 준 지문**과 맞춰 본다. 이 API의 응답만 믿으면 중간에서 바꿔치기한 키를 받을 수 있다. 맞으면 공개키를 설정에 저장해 두고 매번 받지 않는다.
- 허브가 아직 키를 설정하지 않았으면 `404 HUB_KEY_MISSING`.
- 위 예시 값은 2.3의 예시용 키다. 운영 허브의 키와 다르다.

**내 공개키 등록·교체** `PUT /v1/keys`

은행이 키 쌍을 만들고(2.3 `generate_keypair`) **공개키만** 등록한다. 개인키는 허브를 포함해 누구에게도 주지 않는다. 요청은 **지금 인증으로 서명**한다: `hmac`·`both` 모드면 HMAC으로(기존 인증을 처음 등록에 쓴다), `ed25519` 모드면 지금 등록된 Ed25519 키로. 등록할 수 있는 것은 `X-Bank-Code`의 자기 은행 키뿐이다.

요청
```json
{ "public_key": "gxCDPKbN/rIXKDJgn4GZh5JY7I65Cwbk3A/mJjTtPzY=" }
```

응답 `200` (`GET /v1/keys`도 같은 형식, 내 등록 상태)
```json
{
  "bank_code": "901",
  "fingerprint": "d61a340fe933017d",
  "signature_mode": "hmac",
  "registered_at": "2026-11-06T03:00:00Z",
  "previous_fingerprint": null,
  "previous_key_expires_at": null,
  "hub_fingerprint": "…"
}
```

| 필드 | 설명 |
|---|---|
| `fingerprint` | 등록된 공개키의 지문. 자기가 만든 키의 지문과 같은지 확인한다. 등록 전이면 `null` |
| `signature_mode` | 지금 모드 (`hmac`·`both`·`ed25519`). **등록해도 바뀌지 않는다** |
| `previous_fingerprint`, `previous_key_expires_at` | 교체 전 키와, 허브가 그 키를 받아 주는 마지막 시각 |
| `hub_fingerprint` | 허브 공개키의 지문 (따로 받은 값과 맞춰 보는 용도) |

`public_key`가 raw 32바이트의 base64(44자)가 아니면 `422 VALIDATION_ERROR`. 같은 키를 다시 등록하면 아무것도 바뀌지 않는다. API로 등록하기 어려우면 공개키를 허브 관리자에게 보내 대신 등록해 달라고 해도 된다 (공개키는 비밀이 아니다. 지문을 같이 알린다).

**키 회전**: 새 키 쌍을 만들고, `PUT /v1/keys`를 **지금(옛) 키로 서명해** 새 공개키로 부른다. 허브는 옛 공개키를 **24시간** 더 받는다 (요청 서명·응답 서명 검증 모두). 그 사이에 보내는 쪽과 응답 서명을 새 키로 바꾼다. 개인키가 유출됐으면 바로 회전하고 허브 관리자에게 알린다 (관리자는 모드를 내려 그 은행의 Ed25519 요청을 즉시 막을 수도 있다). 유출 전에 만든 서명 증거는 그대로 유효하다. HMAC secret 재발급(2절)과 공개키 회전은 서로 영향이 없다.

**전환 순서 (은행이 할 일)**

허브는 `ed25519`로 바꾸는 순간부터 그 은행에 Ed25519로 서명해 보내고 응답 서명을 요구한다. 그래서 **받는 쪽을 먼저** 준비한다.

| 단계 | 은행 | 허브 관리자 | 확인 |
|---|---|---|---|
| 1 | 키 쌍 생성. 개인키는 서버 환경 변수에만 | | |
| 2 | `GET /v1/hub-key`로 허브 공개키를 받아 지문 확인, 설정에 저장 | 지문을 개별 경로로 알려 줌 | |
| 3 | **받는 쪽**: 4절(·9.4) API가 `X-Signature-Alg: ed25519` 요청을 허브 공개키로 검증하게 한다 (없거나 `hmac`이면 지금처럼 secret으로, **둘 다** 받게). 자기 응답(2xx·4xx)에 응답 서명(2.5)을 붙인다 | | 허브가 아직 HMAC으로 보내므로 이 단계에서 깨지는 것이 없다 |
| 4 | `PUT /v1/keys`로 공개키 등록 (HMAC 서명) | | `GET /v1/keys`의 `fingerprint` |
| 5 | 관리자에게 `both` 요청 | `both`로 바꿈 | |
| 6 | **보내는 쪽**을 Ed25519로 (3절·9.2·10절 호출에 `X-Signature-Alg: ed25519`) | | 이체·조회가 200 |
| 7 | 관리자에게 `ed25519` 요청 | `ed25519`로 바꾸고 ping(7절 4) | ping `status` 200·404, `response_signature_valid: true` |
| 8 | (선택) 받는 쪽의 HMAC 검증을 끈다 | | |

8은 은행이 정한다. 끄면 secret으로 만든 요청(secret을 가진 은행 자신도 만들 수 있다)을 받지 않으므로 받은 요청마다 허브가 보냈다는 증거가 남고, 남겨 두면 관리자가 비상시 모드를 내렸을 때(허브가 다시 HMAC으로 보낸다) 끊기지 않는다. 모드는 언제든 내릴 수 있다 (`ed25519` → `both`·`hmac`). 내리면 그다음 호출부터 허브가 HMAC으로 보내고 응답 서명을 요구하지 않는다.

---

## 3. 은행 → 허브 API

기본 주소: `https://clearing-hub.ai-agent-develop.cloud/v1` (API 문서: `https://clearing-hub.ai-agent-develop.cloud/docs`)

예치금 조회 `GET /v1/reserve`와 정산 조회 `GET /v1/settlements`, `GET /v1/settlements/{run_id}`도 은행 → 허브 API지만 10절에 있다.

### 3.1 참가 은행 목록 `GET /v1/banks`

화면의 "받는 은행" 선택에 쓴다. 인증 필요.

```json
{ "banks": [ { "bank_code": "901", "name": "AvengersBank" }, { "bank_code": "902", "name": "SimBank" } ] }
```

- 활성 은행만 나온다. **요청한 은행 자신도 목록에 들어 있다.** 받는 은행으로 자기 은행을 고르면 이체가 `SAME_BANK`로 거절되므로, 화면에 보여 줄 때 자기 은행을 빼거나 자체 이체로 돌린다.

### 3.2 수취 조회 `POST /v1/inquiry`

받는 사람 이름을 확인 화면에 보여 주기 위한 것. 돈은 움직이지 않는다.

요청
```json
{ "to_bank": "902", "to_account": "110-123-0001" }
```

| 필드 | 타입 | 필수 | 규칙 |
|---|---|---|---|
| `to_bank` | 문자열 | 필수 | 숫자 3자리 |
| `to_account` | 문자열 | 필수 | 1~50자 |

응답 `200`
```json
{ "to_bank": "902", "to_account": "110-123-0001", "holder_name": "김철수" }
```

오류

| 코드 | HTTP | 경우 |
|---|---|---|
| `UNAUTHORIZED`·`SIGNATURE_INVALID`·`SIGNATURE_EXPIRED` | 401 | 요청한 은행의 인증 실패 (2절) |
| `BANK_INACTIVE` | 401 / 400 | 요청한 은행 자신이 비활성이면 401, 받는 은행이 비활성이면 400 |
| `VALIDATION_ERROR` | 422 | 위 필드 표 위반 |
| `BANK_NOT_FOUND` | 404 | 없는 `to_bank` |
| `ACCOUNT_NOT_FOUND` | 404 | 받는 은행에 계좌 없음 |
| `ACCOUNT_INACTIVE` | 400 | 해지·정지 계좌 |
| `RECEIVER_TIMEOUT` | 504 | 받는 은행이 5초 안에 응답 없음 |
| `RECEIVER_ERROR` | 502 | 받는 은행 5xx, 또는 받는 은행이 허브 인증을 거절 (5절) |

- 수취 조회는 허브가 재시도하지 않으므로 504·502면 사용자에게 잠시 뒤 다시 시도하라고 안내한다.
- 이체와 달리 `to_bank`가 자기 은행이어도 `SAME_BANK`로 거절하지 않고 그대로 중계한다. 다만 자기 은행 계좌는 자기 DB에서 조회하는 편이 빠르다.

`holder_name`은 받는 은행이 준 값을 **가리지 않고** 그대로 전달한다 (9.6). 화면에 가려서 보여 줄지(`김*수`)는 보내는 은행이 정한다.

### 3.3 이체 요청 `POST /v1/transfers`

**보내는 은행은 이 요청 전에 자기 DB에서 출금을 끝내고**(중간 계정으로 옮기고) 이체 상태를 "처리 중"으로 저장해 둔다. 허브 응답에 따라 완료 또는 되돌림한다 (6절).

요청
```json
{
  "transfer_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "to_bank": "902",
  "to_account": "110-123-0001",
  "from_account": "110-200-456789",
  "amount": 50000,
  "sender_name": "홍길동",
  "memo": "생활비"
}
```

| 필드 | 타입 | 필수 | 규칙 |
|---|---|---|---|
| `transfer_id` | 문자열 | 필수 | 소문자 UUID v4 (하이픈 포함 36자) |
| `to_bank` | 문자열 | 필수 | 숫자 3자리. 자기 은행 코드면 `SAME_BANK` |
| `to_account` | 문자열 | 둘 중 하나 | 1~50자 |
| `to_account_ref` | 문자열 | 둘 중 하나 | 1~64자. 9.3의 본인 계좌 조회를 쓸 때만 |
| `from_account` | 문자열 | 필수 | 1~50자 |
| `amount` | 정수 | 필수 | 1 이상. 1억 원 초과는 `LIMIT_EXCEEDED`. `50000.0`·`"50000"`은 422 |
| `sender_name` | 문자열 | 필수 | 1~50자 |
| `memo` | 문자열 | 선택 | 0~100자. 빼면 `""` |

`from_bank`는 본문에 없다. 허브가 인증된 `X-Bank-Code`로 정한다 (사칭 방지).

> **되돌릴지는 HTTP 상태나 `error` 코드가 아니라 본문의 `status`로만 정한다.** `status: "FAILED"`면 되돌리고, 그 밖의 모든 응답(202, `status` 없는 4xx, 5xx, 타임아웃)은 되돌리지 않는다. 같은 `VALIDATION_ERROR`(422)라도 허브가 요청 형식을 거절한 것(`status` 없음 → 되돌리지 않음)과 받는 은행이 거절한 것(`status: "FAILED"` → 되돌림)이 있다.

응답 `200` — 받는 은행이 입금을 확인한 경우
```json
{
  "transfer_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "status": "COMPLETED",
  "holder_name": "김철수",
  "finished_at": "2026-11-05T01:20:03Z"
}
```
`holder_name`은 받는 은행이 4.2·4.3 응답에 이름을 넣지 않았으면 `null`일 수 있다.

응답 `202` — 받는 은행 응답을 아직 못 받은 경우 (`RECEIVER_TIMEOUT`)
```json
{ "transfer_id": "7c9e6679-…", "status": "PENDING" }
```
보내는 은행은 **출금을 되돌리지 말고** 3.4로 결과를 확인한다.

응답 `4xx` + `status: "FAILED"` — 이체가 확정 실패한 경우 (5절 형식). 보내는 은행은 출금을 되돌린다.
```json
{ "error": "ACCOUNT_NOT_FOUND", "message": "받는 계좌를 찾을 수 없습니다", "transfer_id": "7c9e6679-…", "status": "FAILED" }
```
`FAILED`가 붙는 경우는 두 가지다.

- 받는 은행이 거절함: `ACCOUNT_NOT_FOUND`(404), `ACCOUNT_INACTIVE`·`AMOUNT_INVALID`·`LIMIT_EXCEEDED`·`RECEIVER_ERROR`(400), `VALIDATION_ERROR`(422)
- 허브가 접수 전에 거절함: `SAME_BANK`·`LIMIT_EXCEEDED`(1회 상한, 순채무한도 10.2)·`BANK_INACTIVE`·`ACCOUNT_REF_INVALID`(400), `BANK_NOT_FOUND`(404). 허브에 거래가 남지 않으므로 고친 뒤에는 **새 `transfer_id`**로 보낸다

**`status: "FAILED"`가 없는 4xx는 이체 결과가 아니다.** 이 오류는 허브가 거래를 찾아보기 전, 요청 자체를 거절한 것이다. `UNAUTHORIZED`·`SIGNATURE_INVALID`·`SIGNATURE_EXPIRED`·`BANK_INACTIVE`(401), 본문 형식 오류 `VALIDATION_ERROR`(422), `FORBIDDEN`(403, 다른 은행이 쓴 `transfer_id`)가 여기에 해당한다. 같은 `transfer_id`로 앞서 보낸 요청이 접수됐을 수 있으므로 **출금을 되돌리지 않는다**. 6절의 순서를 따른다.

응답 `5xx`(본문 형식 보장 없음, 5절)도 결과를 모르는 것이다. 되돌리지 말고 같은 `transfer_id`로 재전송한다.

같은 `transfer_id`가 다시 오면 허브는 새로 처리하지 않고 저장된 결과를 같은 형식으로 돌려준다 (`DUPLICATE_TRANSFER`가 아니라 **원래 결과**를 준다. 재전송한 쪽이 그대로 마무리할 수 있게 하기 위해서다). 허브는 **본문을 비교하지 않는다**. 금액·계좌가 달라도 처음 거래의 결과를 준다. 그러니 새 이체에는 반드시 새 `transfer_id`를 만든다.

이 API의 성공·결과 응답은 위 세 가지(200 `COMPLETED`, 202 `PENDING`, 4xx `FAILED`)뿐이다. 재전송했을 때 허브가 아직 받는 은행을 부르는 중이거나 결과를 끝내 모르는 거래(3.4의 `ACCEPTED`·`DELIVERING`·`UNKNOWN`)도 202 `PENDING`으로 답한다. 할 일이 같기 때문이다: 되돌리지 말고 3.4로 확인한다.

### 3.4 결과 조회 `GET /v1/transfers/{transfer_id}`

자기 은행이 보낸 거래만 조회할 수 있다.

```json
{
  "transfer_id": "7c9e6679-…",
  "status": "COMPLETED",
  "holder_name": "김철수",
  "finished_at": "2026-11-05T01:21:10Z",
  "to_bank": "902",
  "amount": 50000,
  "requested_at": "2026-11-05T01:20:00Z",
  "fail_code": null,
  "fail_message": null
}
```

- `holder_name`: 받는 은행이 준 예금주 이름. 아직 모르면 `null`
- `finished_at`: `COMPLETED`·`FAILED`가 된 시각. 그 전에는 `null`
- `fail_code`·`fail_message`: `FAILED`일 때 5절의 오류 코드와 설명. 그 밖에는 `null`

`status` 값: `ACCEPTED`, `DELIVERING`, `PENDING`, `COMPLETED`, `FAILED`, `UNKNOWN`

| 값 | 보내는 은행이 할 일 |
|---|---|
| `ACCEPTED` / `DELIVERING` / `PENDING` | 기다렸다가 다시 조회 (권장: 30초 간격) |
| `COMPLETED` | 이체 완료 처리 |
| `FAILED` | 출금 되돌림 |
| `UNKNOWN` | 허브가 재시도를 다 했는데도 결과를 모름 (접수 후 약 10분, 8절 7). 허브는 이 거래를 **더 이상 자동으로 확인하지 않고** 관리자가 받는 은행과 직접 맞춰 본다. 되돌리지 말고 6절의 "30분 뒤" 규칙을 따른다. 관리자가 정리하면 아래 중 하나로 바뀐다 |

관리자가 `UNKNOWN`을 정리하는 방법은 두 가지다. 보내는 은행은 어느 쪽이든 계속 조회하다가 바뀐 상태대로 마무리하면 된다.

- **다시 자동 확인**: 받는 은행 장애가 풀렸으면 관리자가 거래를 `PENDING`으로 되돌린다. `UNKNOWN` 다음에 `PENDING`이 보일 수 있다는 뜻이다. 허브가 다시 4.3으로 확인하고, 이번에도 1분 × 10회 안에 모르면 다시 `UNKNOWN`이 된다
- **직접 확정**: 관리자가 받는 은행과 확인한 결과로 `COMPLETED` 또는 `FAILED`로 정한다. `FAILED`면 `fail_code`는 `RECEIVER_ERROR`이고, 보내는 은행은 다른 `FAILED`와 똑같이 출금을 되돌린다. 같은 `transfer_id`로 3.3을 다시 보내도 같은 결과가 온다

조회에 `404 TRANSFER_NOT_FOUND`가 오면 **조회한 그 순간까지는** 허브에 접수 기록이 없다는 뜻이다 (허브는 받는 은행을 부르기 **전에** 접수 기록을 저장한다). 하지만 타임아웃 난 POST를 허브가 아직 처리하고 있다면 조회 직후에 접수될 수 있다. 그래서 404만 보고 **바로 되돌리지 않는다.** 같은 `transfer_id`로 3.3 POST를 다시 보내 그 응답대로 마무리한다 (멱등이라 안전하고, 사용자가 원한 이체가 그대로 진행된다).

꼭 되돌려야 한다면(예: 고객이 취소를 요청함) **마지막 POST를 보낸 지 1분 넘게 지난 뒤** 받은 404일 때만 되돌리고, 그 `transfer_id`는 **다시는 보내지 않는다** (재전송 루프·재시작 복구 대상에서 빼고 `FAILED`로 저장한다).

다른 은행이 보낸 거래의 `transfer_id`로 조회하면 `403 FORBIDDEN`이다. 인증이 틀리면 401이다 (2절).

---

## 4. 허브 → 은행 API (은행이 구현할 것)

은행은 허브에 **`base_url`** 하나를 등록한다 (예: `https://b-bank.example.com/interbank`). 허브는 그 아래 경로를 부른다. 요청에는 2절 서명이 붙어 있으니 검증해야 한다 (`X-Bank-Code: "000"`).

**`ed25519` 모드 은행** (2.6): 허브 요청을 `X-Signature-Alg`에 따라 허브 공개키(Ed25519)로 검증하고, **자기 응답(2xx·4xx)에 응답 서명(2.5)을 붙인다.** 응답 서명이 없거나 틀리면 허브는 그 응답을 "응답 없음"으로 본다 (2xx도 입금 완료로 보지 않는다). 은행 쪽에서 바뀌는 것은 이 둘뿐이고 API의 경로·본문은 그대로다. `hmac`·`both` 모드 은행은 지금처럼 HMAC만 검증하면 된다.

### 4.1 수취 조회 `POST {base_url}/inquiry`

요청
```json
{ "account_num": "110-123-0001" }
```

응답 `200`
```json
{ "holder_name": "김철수" }
```

- `holder_name`은 **필수**다. 200인데 `holder_name`이 없거나 비어 있으면, 허브는 보내는 은행에 `502 RECEIVER_ERROR`로 답한다.
- 오류: `404` `ACCOUNT_NOT_FOUND`, `400` `ACCOUNT_INACTIVE`. 이 밖의 4xx는 허브가 `RECEIVER_ERROR`로 바꾼다 (5절).

### 4.2 입금 `POST {base_url}/receive`

요청
```json
{
  "transfer_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "from_bank": "901",
  "from_bank_name": "AvengersBank",
  "from_account": "110-200-456789",
  "to_account": "110-123-0001",
  "amount": 50000,
  "sender_name": "홍길동",
  "memo": "생활비"
}
```

허브는 위 8개 필드를 항상 넣는다 (`memo`는 없으면 `""`). 각 필드의 형식은 3.3 필드 표와 같고, `from_bank`는 숫자 3자리, `from_bank_name`은 허브에 등록된 보낸 은행 이름이다. 3.3의 "`to_account`·`to_account_ref` 둘 중 하나" 규칙은 여기에 해당하지 않는다. 보내는 은행이 `to_account_ref`(9.3)로 보냈어도 허브가 실제 계좌번호로 바꿔 **`to_account`를 항상** 넣고, `to_account_ref`는 보내지 않는다.

응답 `200`
```json
{ "transfer_id": "7c9e6679-…", "status": "COMPLETED", "holder_name": "김철수" }
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `transfer_id` | 권장 | 요청의 값 그대로 |
| `status` | 권장 | `"COMPLETED"` |
| `holder_name` | **권장** | 받는 계좌의 예금주 이름. 빠지면 보내는 은행의 3.3 응답에서 `holder_name`이 `null`이 된다 |

**허브는 2xx면 본문과 상관없이 "입금 완료"로 본다** (`ed25519` 모드에서는 응답 서명이 맞는 2xx만, 2.5). 본문이 비어 있거나 `status`가 다른 값이어도 마찬가지다. 그러니 입금을 끝내지 않았으면 절대 2xx로 답하지 않는다 (거절은 4xx, 일시 장애는 5xx).

**은행이 지켜야 할 것**

1. **멱등성**: `transfer_id`를 저장해 두고, 같은 ID가 다시 오면 입금하지 않고 `200 COMPLETED`를 다시 돌려준다. 허브는 타임아웃·5xx 뒤 먼저 4.3으로 입금 여부를 묻고, 4.3이 `404`일 때 같은 요청을 다시 보낸다 (그래서 첫 요청이 아직 처리 중일 때 재전송이 겹칠 수 있다, 규칙 2). 허브의 재전송은 본문이 처음과 항상 같으므로, 본문을 비교하지 않고 저장된 결과를 주면 된다.
2. 입금과 `transfer_id` 저장은 **한 트랜잭션**으로 한다. 입금만 되고 ID 저장이 안 되면 다음 재시도에서 두 번 들어간다. `transfer_id` 칸에는 **유니크 제약**을 건다. 같은 ID가 동시에 두 번 오면(첫 요청 처리 중 타임아웃 → 재전송) 하나는 제약에 걸려 롤백되고 입금은 한 번만 된다. **롤백된 쪽은 절대 4xx(`409` 포함)로 답하지 않는다.** 허브는 4xx를 "입금하지 않음"으로 보고 이체를 `FAILED`로 끝내므로, 다른 요청에서 이미 입금됐는데 보내는 은행이 출금을 되돌려 **이중 지급**이 된다. 롤백된 쪽은 저장된 결과를 다시 읽어 `200 COMPLETED`로 답하거나, 아직 읽을 수 없으면 `5xx`로 답한다 (허브가 4.3으로 다시 확인한다).
3. **서명 검증은 입금 처리보다 먼저** 한다. 허브는 은행의 401·403을 "입금 전 거절"로 보고 이체를 `FAILED`로 끝낸다 (5절 `RECEIVER_ERROR`).
4. 거절할 때는 `4xx` + 5절 형식. `ACCOUNT_NOT_FOUND`, `ACCOUNT_INACTIVE`, `AMOUNT_INVALID`, `LIMIT_EXCEEDED`(받는 쪽 한도가 있을 때), `VALIDATION_ERROR`(본문 형식 오류). 이 밖의 코드는 허브가 `RECEIVER_ERROR`로 바꾼다 (5절)
5. 일시 장애는 `5xx`. 허브는 5xx와 타임아웃만 재시도하고 4xx는 재시도하지 않는다. **4xx를 준 거래는 저장하지 않는다.** 그래야 4.3이 404를 준다
6. 거래내역에는 보낸 사람 이름과 보낸 은행 이름을 남긴다 (`"AvengersBank 홍길동"`).

### 4.3 입금 확인 `GET {base_url}/receive/{transfer_id}`

허브가 응답을 못 받은 거래를 확인할 때 부른다.

응답 `200`
```json
{ "transfer_id": "7c9e6679-…", "status": "COMPLETED", "holder_name": "김철수", "received_at": "2026-11-05T01:20:02Z" }
```

`holder_name`은 권장 항목이다. 빠져 있으면 허브가 4.1 수취 조회로 이름을 채운다 (실패해도 이체 결과에는 영향 없음). 보내는 은행의 결과 조회(3.4)에서 받는 사람 이름이 비지 않게 하기 위해서다.

응답 `404`: 그 ID로 입금한 적 없음 (`TRANSFER_NOT_FOUND`). 허브는 4.2를 다시 보낸다.

**이 API의 응답은 위 둘(200 `COMPLETED`, 404 `TRANSFER_NOT_FOUND`)뿐이다.** 입금하지 않은 거래(4.2에서 4xx로 거절했거나 아직 처리 중)는 모두 404로 답한다. 허브는 200이어도 `status`가 `COMPLETED`가 아니면, 그리고 5xx·타임아웃이면 "확인 못 함"으로 보고 1분 뒤 다시 묻는다 (8절 7).

---

## 5. 오류 형식

모든 오류는 HTTP 4xx/5xx + 다음 본문.

```json
{ "error": "ACCOUNT_NOT_FOUND", "message": "받는 계좌를 찾을 수 없습니다", "transfer_id": "7c9e6679-…" }
```

`transfer_id`는 이체 관련 오류에만 붙는다. `message`는 사람이 읽는 설명이며 프로그램은 `error` 코드로 분기한다.

허브는 아래 표에 있는 코드만 보낸다. 받는 은행이 표에 없는 코드로 거절하면 `RECEIVER_ERROR`로 바꾸고(설명은 그대로), 받는 은행이 준 HTTP 상태가 아니라 **그 코드의 표 상태**로 답한다 (예: `ACCOUNT_NOT_FOUND` 404, `VALIDATION_ERROR` 422, 그 밖의 거절 400). 없는 경로와 허용하지 않는 메서드는 모두 404 `NOT_FOUND`다.

**5xx는 본문 형식을 보장하지 않는다.** 서버가 죽거나 앞단 프록시(로드 밸런서·리버스 프록시)가 대신 답하면 HTML이거나 비어 있을 수 있다. 받는 쪽은 5xx를 **상태 코드로만** 판단한다 (허브는 받는 은행의 5xx를 본문과 상관없이 `RECEIVER_ERROR`로 보고 재시도한다). 응답하는 쪽(허브·은행)은 처리하지 못한 오류도 가능하면 `500 {"error": "INTERNAL_ERROR", "message": …}`로 답하고, 내부 오류 내용은 본문에 넣지 않는다. 4xx는 반드시 이 형식을 지킨다 (허브가 `error` 코드를 거래의 `fail_code`로 저장한다).

| 코드 | HTTP | 뜻 | 누가 내는가 |
|---|---|---|---|
| `UNAUTHORIZED` | 401 | 허브: 은행 코드·API 키 불일치, 인증 헤더 없음. 은행: `X-Bank-Code`가 `"000"`이 아니거나 서명 헤더 없음 | 허브·은행 |
| `SIGNATURE_INVALID` | 401 | 서명 불일치, 모르는 `X-Signature-Alg`, 상대의 서명 모드가 받지 않는 방식 (예: `ed25519` 모드 은행의 HMAC 요청, 2절), 공개키 미등록 은행의 Ed25519 요청 | 허브·은행 |
| `SIGNATURE_EXPIRED` | 401 | 시각 차이 5분 초과 | 허브·은행 |
| `BANK_NOT_FOUND` | 404 | 없는 은행 코드 | 허브 |
| `BANK_INACTIVE` | 400 / 401 | 받는 은행이 비활성이면 400, 요청한 은행 자신이 비활성이면 401 | 허브 |
| `ACCOUNT_NOT_FOUND` | 404 | 받는 계좌 없음 | 은행 (허브가 전달) |
| `ACCOUNT_INACTIVE` | 400 | 해지·정지 계좌 | 은행 |
| `AMOUNT_INVALID` | 400 | 받는 은행 기준으로 받을 수 없는 금액 (허브는 금액 형식 오류에 `VALIDATION_ERROR`를 낸다) | 은행 |
| `LIMIT_EXCEEDED` | 400 | 허브 1회 상한, 보내는 은행의 순채무한도(10.2) 또는 받는 은행 한도 초과 (`message`로 구분) | 허브·은행 |
| `SAME_BANK` | 400 | 받는 은행이 보내는 은행과 같음 (같은 은행 안의 이체는 자체 처리) | 허브 |
| `VALIDATION_ERROR` | 422 | 필드 누락·형식 오류, 금액이 0·음수·소수·문자열 (`message`에 필드 이름) | 허브·은행 |
| `TRANSFER_NOT_FOUND` | 404 | 모르는 `transfer_id` | 허브·은행 |
| `FORBIDDEN` | 403 | 남의 은행 거래 조회 | 허브 |
| `RECEIVER_TIMEOUT` | 202 / 504 | 받는 은행 응답 없음. 이체는 202(본문 `status: PENDING`), 수취 조회는 504 | 허브 |
| `RECEIVER_ERROR` | 502 / 400 | 받는 은행 5xx: 이체는 `PENDING`으로 재시도, 수취 조회는 즉시 502. 받는 은행이 **허브의 서명·인증을 거절**(401·403, 허브 ↔ 받는 은행 설정 문제)한 경우도 이 코드다: 수취 조회는 502, 이체는 입금 전 거절이라 `FAILED`(400)로 끝나고 보내는 은행은 출금을 되돌린다. 보내는 은행 자신의 서명 오류(`SIGNATURE_INVALID`)와 헷갈리지 않게 받는 은행의 인증 오류 코드는 그대로 넘기지 않는다 | 허브 |
| `ACCOUNT_REF_INVALID` | 400 | `to_account_ref`가 만료됐거나 다른 은행 것, `to_bank`와 안 맞음 (9.3) | 허브 |
| `TOO_MANY_LOOKUPS` | 429 | 본인 계좌 조회 횟수 초과 (9.2) | 허브 |
| `SETTLEMENT_NOT_FOUND` | 404 | 없는 정산 `run_id` (10.5) | 허브 |
| `HUB_KEY_MISSING` | 404 / 400 | 허브 서명 키가 아직 설정되지 않음. `GET /v1/hub-key`는 404, 관리자가 `ed25519` 모드로 바꾸려 할 때 400 (2.6) | 허브 |
| `NOT_FOUND` | 404 | 없는 경로 (주소 오타) | 허브 |
| `INTERNAL_ERROR` | 500 | 그 밖의 오류 | 허브·은행 |

---

## 6. 보내는 은행의 처리 순서 (권장)

```
1. 사용자 확인·승인, 자체 FDS 검사
2. 허브 POST /v1/inquiry 로 예금주 확인 (확인 화면용, 생략 가능)
3. [한 트랜잭션] 사용자 계좌 출금 → 타행 송금 중간 계정 입금, transfers.status = PENDING, transfer_id 생성
4. 허브 POST /v1/transfers
   ├ 200 COMPLETED                → [트랜잭션] 중간 계정 차감, status = COMPLETED
   ├ 4xx + 본문 status "FAILED"   → [트랜잭션] 중간 계정 → 사용자 계좌 되돌림, status = FAILED, 사용자에게 사유 안내
   ├ 202 PENDING                  → 4-1
   ├ 4xx (본문에 status 없음)      → 되돌리지 않는다. 원인(서명·시계·API 키·본문)을 고치고 같은 transfer_id로 재전송.
   │                                 못 고치면 4-1
   └ 5xx·통신 오류·타임아웃         → 되돌리지 않는다. 같은 transfer_id로 POST 재전송 (멱등이라 안전).
                                     계속 실패하면 4-1
4-1. 30초마다 GET /v1/transfers/{id} (최대 30분)
   ├ COMPLETED / FAILED   → 위와 같이 마무리
   ├ 404 TRANSFER_NOT_FOUND → 아직 접수 기록 없음. 되돌리지 말고 같은 transfer_id로 POST 재전송 (4번으로).
   │                          꼭 되돌려야 하면 마지막 POST 뒤 1분이 지난 404일 때만, 그 ID는 다시 보내지 않는다 (3.4)
   └ 30분이 지나도 ACCEPTED·DELIVERING·PENDING·UNKNOWN, 또는 조회 자체가 계속 실패
                          → status = PENDING 그대로 두고 "확인 필요"로 표시, 허브 관리자에게 transfer_id를 알린다.
                            자동 조회는 10분 간격으로 늦춰 계속한다 (관리자가 정리하면 COMPLETED·FAILED로 바뀐다)
5. 서버 재시작 시 status = PENDING인 거래를 찾아 4-1부터 이어서 한다
```

**하지 말아야 할 것**
- 허브 응답을 모르는 상태에서 출금을 되돌리기 (입금이 됐을 수 있다 → 이중 지급). `status: "FAILED"` 없는 4xx·5xx·타임아웃도 "모르는 상태"다
- 재전송할 때 `transfer_id`를 새로 만들기 (두 번 입금된다)
- 타임아웃 직후 결과 조회의 404만 보고 되돌리기 (허브가 그 POST를 아직 처리 중일 수 있다 → 이중 지급, 3.4)
- 허브 호출을 LLM 에이전트의 도구로 주기 (돈이 움직이는 호출은 이체 서비스 안에서만)

---

## 7. 연동 절차

1. 은행이 허브 관리자에게 **은행 이름, `base_url`**을 알린다.
2. 허브 관리자가 은행을 등록하고(관리자 API `POST /admin/banks`) **은행 코드, API 키, secret**을 발급한다. secret은 등록할 때 한 번만 나오며, GitHub·채팅방이 아닌 개별 경로로 그 은행 팀에만 전달한다. 등록 직후에는 예치금과 순채무한도가 0이라 **송금이 `LIMIT_EXCEEDED`로 거절된다** (받기는 된다, 10.2). 보내는 쪽 시험 전에 허브 관리자에게 모의 예치금이나 한도를 넣어 달라고 요청한다.
3. 은행은 4절 API 3개를 만들고, 허브의 서명 검증을 붙인다.
4. 허브 관리자가 `POST /admin/banks/{bank_code}/ping`으로 `{base_url}/inquiry` 호출이 되는지 확인한다. 허브는 없는 계좌 `"000-000-000000"`으로 4.1을 부르고, 받은 HTTP 상태를 그대로 보여 준다. `base_url`은 허브 서버에서 접근할 수 있는 공개 HTTPS 주소여야 하고, 인증서는 공인 인증서여야 한다 (자체 서명 인증서 불가).

   | ping 결과 `status` | 뜻 |
   |---|---|
   | `404` (`ACCOUNT_NOT_FOUND`) 또는 `200` | 정상: 연결·서명 검증 모두 됨 |
   | `401`·`403` | 연결은 됐지만 서명 검증 실패. 은행이 가진 secret이 허브와 같은지, 검증에 원본 본문 바이트를 쓰는지, `PATH`를 `base_url` 뒤의 부분(`/inquiry`)으로 쓰는지 확인 (2.2). `ed25519` 모드면 허브 공개키(2.6)와 `X-Signature-Alg` 분기도 확인 |
   | `response_signature_valid: false` | `ed25519` 모드에서 은행 응답의 서명이 없거나 틀림. 이 상태면 이체가 모두 `PENDING`이 된다 (2.5). 응답 서명의 `request_signature`·`STATUS`·본문 바이트와 등록한 공개키 지문(`GET /v1/keys`) 확인. `hmac`·`both` 모드에서는 참고용 (`null`은 서명 없음) |
   | `404` (`error`가 `ACCOUNT_NOT_FOUND`가 아님) | `base_url` 경로가 틀림 |
   | `5xx` | 은행 서버 오류 |
   | `null` (`reachable: false`) | 연결 안 됨: 주소·방화벽·인증서·5초 타임아웃 확인 |
   등록한 뒤 `base_url`이나 은행 이름이 바뀌면 허브 관리자에게 알린다. 관리자가 바꾸면(`PATCH /admin/banks/{bank_code}`) 그다음 호출부터 새 주소로 가고, 처리 중인 거래의 재시도도 새 주소로 간다. 은행 코드·API 키·secret은 그대로다. 관리자는 바꾼 뒤 ping(4)으로 다시 확인한다.
5. 테스트 순서: 수취 조회 → 소액 이체 → 없는 계좌 → 같은 `transfer_id` 재전송(입금 1번만인지 확인). 받는 상대가 아직 없으면 허브의 가짜 은행 `902`로 보낸다. 받는 계좌번호 끝 4자리로 동작이 바뀐다: `0001` 정상, `0002` 계좌 없음, `0003` 지연(타임아웃), `0004` 5xx 두 번 뒤 성공, `0005` 입금은 됐는데 응답 없음, `0006` 입금하지 않고 `200 COMPLETED`(대사 시연용. 허브는 이 거래를 `COMPLETED`로 기록하지만 가짜 은행 장부에는 없다. 4.3 확인도 404이고, 10.5의 대사 파일에만 나온다).

   위 순서는 **보내는 쪽**만 시험한다. 가짜 은행 `902`는 받기만 하고 보내지 않으며, ping(4)은 `/inquiry`만 부른다. **받는 쪽**(4.2·4.3)은 허브 관리자에게 요청해 AvengersBank(`901`)에서 자기 은행으로 소액 이체를 받아 시험한다. 요청할 때 시험용 계좌번호를 알려 주고, 아래를 확인한다.

   | 시험 | 확인할 것 |
   |---|---|
   | 정상 계좌로 소액 이체 | 입금 1번, 거래내역에 보낸 은행 이름과 보낸 사람 이름(`"AvengersBank 홍길동"` 형식), 응답에 `holder_name` |
   | 없는 계좌로 이체 | `404 ACCOUNT_NOT_FOUND`, 거래가 저장되지 않음 (`GET /receive/{transfer_id}`가 404) |
   | 입금 후 응답 지연 흉내 (입금을 커밋한 뒤 일부러 5초 넘게 늦게 답함) | 허브가 1분 뒤 4.3으로 확인해 `COMPLETED`로 맞춤. 입금은 1번 |

   **멱등성(같은 `transfer_id` 재전송)은 허브로 일으키기 어렵다.** 허브는 4.3이 404일 때만 4.2를 다시 보내기 때문이다. 은행이 직접 시험한다: 자기 secret으로 `X-Bank-Code: "000"` 요청에 서명해(2.3) 같은 본문을 `/receive`에 두 번 보내 입금이 1번이고 두 번째 응답도 `200 COMPLETED`인지 확인한다. 이어서 새 `transfer_id`로 두 번을 **동시에** 보내 입금이 1번이고, 두 응답이 `200` 또는 `5xx`이며 **4xx는 하나도 없는지** 확인한다.

6. 참고 구현: 이 문서의 2.3·2.5(서명)와 9.1(가상 CI) 코드만으로 충분하다. 허브 저장소를 받았다면 [`src/bank_sim/main.py`](../src/bank_sim/main.py)(4절·9.4 API를 구현한 가짜 은행)와 [`src/clearing_hub/core/signing.py`](../src/clearing_hub/core/signing.py)도 볼 수 있다. `bank_sim`은 허브 패키지의 `signing`을 import하므로, 따로 가져갈 때는 2.3의 참고 구현으로 바꾼다.

---

## 8. 합의가 필요한 것 (v1.0 확정 전에 정한다)

| # | 항목 | 초안 | 결정 |
|---|---|---|---|
| 1 | 은행 코드 배정 | 허브가 `901`부터 순서대로 배정 | 확정 (v0.4): 허브 관리자가 등록할 때 코드를 정해 넣는다(`POST /admin/banks`의 `bank_code`, 자동 배정 아님). 지금 `901` AvengersBank, `902` 가짜 은행 SimBank |
| 2 | 계좌번호 형식 | 은행별 자유, 허브는 검사 안 함 | 확정 (v0.4): 허브는 1~50자 문자열인지만 본다 |
| 3 | 허브 → 은행 호출의 `X-Api-Key` | 생략 (서명만으로 확인) / 은행이 허브에 키 발급 | 확정 (v0.4): 생략. 허브는 `X-Bank-Code: 000` + 서명만 보낸다 |
| 4 | `base_url` 아래 경로 이름 (`/inquiry`, `/receive`) | 고정 | 확정 (v0.4): `/inquiry`, `/receive`, `/receive/{transfer_id}`, `/accounts`(9.4) |
| 5 | 1회 이체 상한 | 1억 원 (허브), 받는 은행 한도는 은행 자율 | 확정 (v0.4): 허브 1억 원(`MAX_TRANSFER_AMOUNT`), 받는 은행은 자율(`LIMIT_EXCEEDED`) |
| 6 | 받는 은행이 거절할 수 있는 조건 | 계좌 없음·정지·한도 초과만 | 허브는 받는 은행의 4xx를 코드와 상관없이 거절(`FAILED`)로 처리한다. 조건 목록은 미정 |
| 7 | 재시도 간격·횟수 | 1분 × 10회, 이후 `UNKNOWN` | 확정 (v0.4): 1분 × 10회(`RETRY_INTERVAL_SECONDS`·`RETRY_MAX_COUNT`), 이후 `UNKNOWN` |
| 8 | 정산 시각·방식 | 매일 00:05 KST, 전날 `COMPLETED` 상계, 표시만 | 확정 (v0.6): 10절. 매일 00:05 KST 자동 DRAFT, 관리자 확정, 모의 예치금 원장. 대사(10.6)는 은행 선택. 확정 시각은 운영 규칙으로 정한다 |
| 9 | 허브 도메인, 테스트 환경 주소 | 허브: `https://clearing-hub.ai-agent-develop.cloud`. 별도 테스트 환경 없이 가짜 은행 `902`로 시험 (7절 5) | 확정 (2026-11-05 배포) |
| 10 | 사기 신고 계좌 공유 (선택) | v1.1 | |
| 11 | 본인 계좌 조회(9절) 참여 | 선택. 안 만든 은행은 결과에서 `UNSUPPORTED`로 빠진다 | 확정 (v0.4): 허브는 9.4의 404를 `UNSUPPORTED`로 처리. AvengersBank·가짜 은행은 참여 |
| 12 | 가상 CI 재료 | 이름 + 생년월일 + 휴대폰번호 (9.1) | 확정 (v0.4): 9.1 정규화 규칙대로 허브·AvengersBank·가짜 은행이 같은 값을 만든다 |
| 13 | `CI_KEY` 관리 | 허브 관리자가 하나 만들어 secret과 같은 경로로 전달. 유출 시 전 은행이 CI를 다시 계산 | 키 생성 완료 (2026-11-05, 허브 서버 보관, 운영 가짜 은행 `902`에 적용). 은행 등록 때 secret과 함께 받는다. 9.1의 예시 키 `example-ci-key-change-me`는 검증용이며 운영에서는 쓰지 않는다 |
| 14 | 다른 은행 계좌를 보내는 은행에 주는 방식 | `account_ref`(허브 보관, 30분) / 전체 계좌번호 그대로 (9.3) | 확정 (v0.4): `account_ref`, 30분(`ACCOUNT_REF_TTL_SECONDS`) |
| 15 | 본인 계좌 조회 횟수 제한 | 은행별 같은 CI 하루 10회 | 확정 (v0.4): 요청 은행별 같은 CI 24시간 10회(`LOOKUP_DAILY_LIMIT`), 넘으면 `TOO_MANY_LOOKUPS` |
| 16 | 추적 ID `X-Request-ID` (2.4) | 선택, 서명 제외, 허브가 받는 은행까지 전달 | 확정 (v0.4): 허브·AvengersBank·가짜 은행 구현 |
| 17 | 서명 대상 | 본문만 / 메서드·경로 포함 | 확정 (v0.5): 메서드와 규격 경로를 포함 (2.2). 허브 → 은행은 `base_url` 뒤의 경로. 예전 형식을 받아 주는 기간은 없다. v1.0: Ed25519도 **같은 메시지**에 서명하고 `X-Signature-Alg`로 구분한다(없으면 HMAC). 응답 서명은 `request_signature`로 요청에 묶는다 (2.5) |
| 18 | 예치금·순채무한도 (10.2) | 관리자가 은행별로 넣음 | 확정 (v0.6): 관리자가 은행별로 넣음, 기본 0 (넣기 전에는 송금 불가) |
| 19 | 공개키 서명 전환 (2.6) | 은행별 `hmac` → `both` → `ed25519`, 관리자가 바꿈 | 허브·가짜 은행 구현 (v1.0 초안). 모든 은행이 같은 날 바꿀 필요가 없다. 은행별 전환 날짜와, 언젠가 HMAC을 끌지(`hmac` 모드 폐지)는 미정 |

---

## 9. 본인 명의 계좌 조회 (선택 기능)

고객 동의를 받아, 같은 사람 명의로 **다른 참가 은행에 있는 계좌 목록**을 가져온다. 쓰임새는 두 가지다.

- "내 다른 은행 계좌로 보내기": 계좌번호를 직접 입력하지 않고 목록에서 고른다
- 보내는 은행 FDS: 받는 계좌가 본인 명의면 위험도를 낮춘다

이체 연동(3·4절)과 독립된 **선택 기능**이다. 허브와 가짜 은행 `902`에는 구현돼 있다 (v0.3). 9.4를 만들지 않은 은행도 이체는 그대로 된다. 잔액·거래내역은 다루지 않는다.

### 9.1 가상 CI (사람 식별자)

실제 금융권은 본인인증 기관이 주는 CI(연계정보)로 같은 사람을 찾는다. 이 프로젝트에서는 각 은행이 같은 규칙으로 계산하는 **가상 CI**를 쓴다.

```
material = 이름 + "|" + 생년월일 + "|" + 휴대폰번호
ci       = hex( HMAC-SHA256( key = CI_KEY, message = UTF-8 bytes of material ) )   // 소문자 16진수 64자
```

| 재료 | 정규화 규칙 | 예 |
|---|---|---|
| 이름 | 앞뒤 공백 제거, 가운데 공백도 제거, 유니코드 NFC | `홍길동` |
| 생년월일 | `YYYYMMDD` 8자리 | `19900101` |
| 휴대폰번호 | 숫자만 (하이픈·`+82` 없이 `010`으로 시작) | `01012345678` |

- `CI_KEY`는 허브 관리자가 하나 만들어 모든 참가 은행에 secret과 같은 방법으로 전달한다 (8절 13). 은행마다 다르면 같은 사람을 찾지 못한다.
- 은행은 회원가입·정보 변경 때 `ci`를 계산해 **고객 테이블에 저장하고 인덱스**를 건다. 원본 재료(생년월일·휴대폰번호)는 은행 밖으로 보내지 않는다. 허브도 원본을 받지 않는다.
- 휴대폰번호가 바뀌면 `ci`도 바뀐다. 은행은 고객 정보가 바뀔 때 다시 계산한다.

검증용 예시:

```
CI_KEY   = example-ci-key-change-me
material = 홍길동|19900101|01012345678   (30 bytes)
ci       = aef7438cc8491d1ac5ba115604e9c5af849d677f2979be05df1c307366c64be2
```

```python
import hmac, hashlib, unicodedata

def virtual_ci(ci_key: str, name: str, birth: str, phone: str) -> str:
    name = unicodedata.normalize("NFC", "".join(name.split()))
    phone = "".join(ch for ch in phone if ch.isdigit())
    material = f"{name}|{birth}|{phone}"
    return hmac.new(ci_key.encode(), material.encode(), hashlib.sha256).hexdigest()
```

### 9.2 본인 계좌 조회 `POST /v1/accounts/lookup` (은행 → 허브)

보내는 은행이 **고객 동의를 받은 뒤** 부른다. 허브는 요청한 은행을 뺀 활성 참가 은행 전체에 9.4를 **동시에** 보내고(은행별 5초), 결과를 모아 돌려준다.

요청
```json
{
  "ci": "aef7438cc8491d1ac5ba115604e9c5af849d677f2979be05df1c307366c64be2",
  "consent_id": "c-20261105-000123"
}
```

| 필드 | 타입 | 필수 | 규칙 |
|---|---|---|---|
| `ci` | 문자열 | 필수 | 소문자 16진수 64자 (9.1) |
| `consent_id` | 문자열 | 필수 | 1~64자 |

- `consent_id`: 보내는 은행이 저장한 동의 기록의 ID (형식 자유, 최대 64자). 허브는 내용을 확인하지 못하지만 조회 기록에 남겨, 나중에 "누구의 동의로 조회했는지"를 은행 쪽 기록과 맞춰 볼 수 있게 한다.

응답 `200` (일부 은행이 실패해도 200이다)
```json
{
  "lookup_id": "3f2b8c1e-…",
  "accounts": [
    {
      "bank_code": "902",
      "bank_name": "SimBank",
      "account_masked": "110-***-0001",
      "product_name": "자유입출금",
      "account_ref": "acr_Xk2…"
    }
  ],
  "banks": [
    { "bank_code": "902", "result": "OK", "count": 1 },
    { "bank_code": "903", "result": "UNSUPPORTED" },
    { "bank_code": "904", "result": "TIMEOUT" }
  ]
}
```

| `banks[].result` | 뜻 |
|---|---|
| `OK` | 응답함 (`count`가 0이면 그 은행에 이 사람 계좌 없음) |
| `UNSUPPORTED` | 9.4가 없음 (404) |
| `TIMEOUT` | 5초 안에 응답 없음 |
| `ERROR` | 그 밖의 모든 경우: 5xx, 404를 뺀 4xx(서명 거절 401·403 포함), 2xx인데 본문에 `accounts` 배열이 없음 |

`account_masked`는 허브가 실제 계좌번호에서 앞 3자와 끝 4자만 남기고 가운데의 하이픈이 아닌 글자를 `*`로 바꾼 값이다 (`110-123-0001` → `110-***-0001`). 계좌번호가 8자보다 짧으면 끝 4자만 남긴다.

- 허브는 이 조회를 재시도하지 않는다. 화면에는 "일부 은행은 불러오지 못했습니다"를 보여 주고 다시 시도하게 한다.
- 오류: `VALIDATION_ERROR`(422, `ci`가 64자 16진수가 아님·`consent_id` 없음), `TOO_MANY_LOOKUPS`(429, 8절 15의 횟수 초과)

### 9.3 `account_ref`로 이체하기

허브는 다른 은행의 **전체 계좌번호를 보내는 은행에 주지 않는다** (8절 14). 대신 `account_ref`를 준다.

- `account_ref`는 허브가 만든 불투명한 문자열이다. 허브만 실제 계좌번호를 알고 있다
- **조회한 은행만**, 발급 후 **30분** 동안 쓸 수 있다. 여러 번 써도 된다 (같은 계좌로 두 번 보내기)
- 3.3 이체 요청에서 `to_account` 대신 `to_account_ref`를 넣는다. `to_bank`는 그대로 넣는다 (허브가 ref와 맞는지 검사)

```json
{ "transfer_id": "…", "to_bank": "902", "to_account_ref": "acr_Xk2…", "from_account": "110-200-456789", "amount": 50000, "sender_name": "홍길동", "memo": "" }
```

- `to_account`와 `to_account_ref`는 **둘 중 하나만** 넣는다 (둘 다·둘 다 없음 → `VALIDATION_ERROR`)
- 만료됐거나 다른 은행이 받은 ref, `to_bank`가 다른 ref → `ACCOUNT_REF_INVALID`(400). 보내는 은행은 9.2를 다시 부른다
- 3.4 결과 조회에는 원래부터 받는 계좌번호가 나오지 않는다

### 9.4 계좌 목록 `POST {base_url}/accounts` (허브 → 은행, 은행이 구현할 것)

4절처럼 은행이 만든다. 요청에는 2절 서명이 붙는다 (`X-Bank-Code: "000"`).

요청
```json
{ "ci": "aef7438cc8491d1ac5ba115604e9c5af849d677f2979be05df1c307366c64be2" }
```

응답 `200`
```json
{
  "accounts": [
    { "account_num": "110-123-0001", "product_name": "자유입출금" }
  ]
}
```

**은행이 지켜야 할 것**

1. **입출금 가능한 정상 계좌만** 준다. 해지·정지 계좌, 대출·적금처럼 이체를 받을 수 없는 계좌는 뺀다
2. 고객이 없거나 계좌가 없으면 `200 {"accounts": []}`. 404로 구분하지 않는다 (고객 존재 여부가 드러나지 않게)
3. 이름·잔액·생년월일 등 다른 정보는 넣지 않는다
4. 이 API를 만들지 않으면 아무것도 하지 않아도 된다. 허브는 404를 `UNSUPPORTED`로 처리한다
5. 조회 기록(시각, `ci`, 돌려준 계좌 수)을 남긴다

| 응답 필드 | 필수 | 규칙 |
|---|---|---|
| `accounts` | 필수 | 배열. 없거나 배열이 아니면 허브는 그 은행을 `ERROR`로 본다 |
| `accounts[].account_num` | 필수 | 문자열 1~50자. 이 조건에 안 맞는 항목은 허브가 **오류 없이 버린다** (결과는 `OK`, `count`에서도 빠진다) |
| `accounts[].product_name` | 선택 | 문자열. 빠지면 `""`, 50자가 넘으면 잘린다 |

서명 검증에 실패해 401·403을 주면 허브는 `UNSUPPORTED`가 아니라 `ERROR`로 본다. 9.4를 만들었는데 결과가 계속 `ERROR`면 secret 설정부터 확인한다.

**가짜 은행 `902`로 시험하기**: 허브 관리자에게 받은 **운영 `CI_KEY`**로 아래 사람의 `ci`를 만들어 9.2를 부른다. 운영 허브의 `902`는 운영 `CI_KEY`로 계산하므로, 9.1의 예시 키로 만든 `ci`로는 빈 목록만 나온다 (예시 키는 2.3·9.1 계산이 맞는지 확인하는 용도다).

| 시험 고객 (이름 / 생년월일 / 휴대폰) | `902`가 돌려주는 계좌 |
|---|---|
| `김철수 / 19850315 / 01098765432` | `110-123-0001` 자유입출금, `110-456-0001` 급여통장 |
| `홍길동 / 19900101 / 01000000000` | `110-789-0001` 자유입출금 |
| 그 밖의 사람 | 빈 목록 |

자기 은행에 같은 이름·생년월일·휴대폰번호의 고객을 만들어 두면, 그 고객으로 "내 다른 은행 계좌로 보내기"까지 끝까지 시험할 수 있다.

### 9.5 허브가 남기는 것과 남기지 않는 것

| 남긴다 | 남기지 않는다 |
|---|---|
| `lookup_id`, 요청 은행, `consent_id`, 시각, 은행별 결과·계좌 수 | `ci` 원문 (조회 횟수 제한에는 `SHA-256(ci)`만 하루 동안 쓴다) |
| `account_ref` ↔ 계좌번호 (30분 뒤 삭제) | 생년월일·휴대폰번호 (애초에 받지 않는다) |

### 9.6 한계 (이 프로젝트에서 받아들이는 것)

- `CI_KEY`를 가진 은행은 누군가의 이름·생년월일·휴대폰번호만 알면 그 사람의 `ci`를 만들어 조회할 수 있다. 실제 CI도 성격이 같아서 동의 기록(`consent_id`), 조회 기록, 횟수 제한으로 막는다. 허브 관리자는 조회 기록을 보고 은행에 동의 기록을 요구할 수 있다
- 이름 표기가 은행마다 다르면(한글·영문, 개명) 같은 사람을 찾지 못한다
- 가상 CI는 본인인증이 아니다. 은행은 9.2를 부르기 전에 자기 로그인·추가 인증으로 고객 본인임을 확인해야 한다
- 수취 조회(3.2)는 예금주 이름 전체를 돌려준다. 실제 금융권처럼 이름을 가리지 않으므로, 참가 은행은 아무 계좌번호나 넣어 이름을 알아낼 수 있다. 이 프로젝트에서는 참가 은행끼리 서로 믿는 것으로 보고 받아들인다. 화면에 가려서 보여 줄지는 보내는 은행이 정한다

---

## 10. 정산 (선택 참여, 이체에는 영향 없음)

은행이 이 절의 API를 하나도 부르지 않아도 이체(3·4절)는 그대로 된다. 허브가 은행을 새로 부르는 일도 없다 (4절은 바뀌지 않는다). 다만 **10.2의 순채무한도 검사는 모든 은행에 적용된다**: 예치금과 한도가 모자라면 송금이 `LIMIT_EXCEEDED`로 거절된다.

### 10.1 왜 있는가

이체에서 받는 은행은 고객 계좌에 **즉시** 입금한다(4.2). 그런데 은행 사이의 돈은 그 순간 움직이지 않는다. 받는 은행이 보내는 은행에게 외상을 준 셈이다. 실제 금융망에서는 금융결제원이 하루치 거래를 은행별로 상계(받을 돈 − 줄 돈)하고, 한국은행이 각 은행의 당좌예금 사이에서 그 차액만 옮겨 외상을 갚는다.

허브는 이것을 **모의 예치금** 원장으로 흉내 낸다.

1. 허브 관리자가 은행마다 예치금(당좌예금 역할)을 넣어 둔다
2. 접수 때 보내는 은행이 갚을 수 있는 만큼만 보내게 한다 (10.2)
3. 매일 하루치 거래를 은행별 차액 하나로 상계한다 (10.3)
4. 은행이 자기 몫의 거래 목록(대사 파일)을 받아 자기 장부와 맞춰 본다 (10.4~10.6)
5. 허브 관리자가 확정하면 그 차액만큼 예치금이 옮겨진다

**실제 돈은 움직이지 않는다.** 예치금은 허브 DB의 숫자이고, 넣고 빼는 것은 허브 관리자만 한다. 이 절의 목적은 "받았다", "보냈다"는 말이 아니라 양쪽 장부의 숫자로 이체를 확인하는 것이다. 받는 은행이 입금 없이 200을 주었거나 보내는 은행이 출금 없이 보냈으면 10.6의 대사에서 드러난다.

### 10.2 예치금과 순채무한도

허브 관리자는 은행마다 두 값을 넣는다.

| 값 | 뜻 | 기본 |
|---|---|---|
| 예치금 (`balance`) | 그 은행이 허브에 맡긴 모의 예치금. 정산 확정 때 차액만큼 늘거나 준다. 마이너스가 될 수 있다 | 0 |
| 순채무한도 (`net_debit_cap`) | 예치금을 넘어 외상으로 더 보낼 수 있는 금액 (실제 금융망의 담보·일중 대출 역할) | 0 |

**기본값이 둘 다 0이라, 넣기 전에는 송금이 모두 거절된다** (받기는 된다). 연동 시험 전에 허브 관리자에게 예치금이나 한도를 넣어 달라고 요청한다 (7절 2).

허브는 이체를 접수할 때(3.3) 보내는 은행에 대해 다음을 검사한다.

```
미정산 순채무 + amount ≤ 예치금 + 순채무한도
```

넘으면 접수 전에 `LIMIT_EXCEEDED`(400, `status: "FAILED"`)로 거절한다. 허브에 거래가 남지 않으므로 보내는 은행은 출금을 되돌린다 (3.3). 1회 상한(1억 원) 초과와 코드가 같으므로 `message`로 구분한다. 순채무한도 때문이면 `message`에 "순채무한도"가 들어 있다.

```json
{ "error": "LIMIT_EXCEEDED", "message": "순채무한도를 넘었습니다 (예치금 0원 + 한도 0원, 미정산 순채무 0원, 요청 50,000원)", "transfer_id": "7c9e6679-…", "status": "FAILED" }
```

**미정산 순채무**는 아직 확정된 정산(10.3의 `SETTLED`)에 들어가지 않은 거래로 계산한다.

```
미정산 순채무 = (내가 보낸 거래 중 FAILED가 아닌 것의 합) − (내가 받은 거래 중 COMPLETED인 것의 합)
```

- 보낸 쪽은 `PENDING`·`UNKNOWN`처럼 결과를 모르는 거래도 센다. 나중에 `COMPLETED`가 될 수 있기 때문이다
- 받은 쪽은 `COMPLETED`만 센다. 아직 들어오지 않은 돈으로 보내게 하지 않기 위해서다
- 받은 돈이 더 많으면 음수다. 그만큼 예치금 + 한도보다 더 보낼 수 있다
- 확정되지 않은 정산(`DRAFT`)에 들어간 거래도 아직 센다. 확정되는 순간 순채무에서 빠지고 같은 금액이 예치금에 반영되므로, 확정 전후로 보낼 수 있는 금액(`available`)은 변하지 않는다

#### 내 예치금 조회 `GET /v1/reserve`

인증 필요(2절). 본문 없는 GET이고 쿼리는 없다. 요청한 은행 자신의 것만 나온다.

응답 `200`
```json
{ "bank_code": "901", "balance": 1000000, "net_debit_cap": 500000, "exposure": 70000, "available": 1430000 }
```

| 필드 | 설명 |
|---|---|
| `bank_code` | 요청한 은행 |
| `balance` | 예치금 잔액(원). 마이너스일 수 있다 |
| `net_debit_cap` | 순채무한도(원) |
| `exposure` | 미정산 순채무(원). 위 식이며 음수일 수 있다 |
| `available` | 지금 더 보낼 수 있는 금액 = `balance + net_debit_cap − exposure`. 음수일 수 있다 (10.3의 `OVERDUE`) |

`available`이 보내려는 금액보다 작으면 그 이체는 `LIMIT_EXCEEDED`로 거절된다. `available`이 0 이하면 받기만 되고 보내기는 모두 막힌 상태다. 허브 관리자에게 예치금을 넣어 달라고 요청한다.

### 10.3 정산 회차 (run)

허브는 하루치 거래를 **정산 회차**(run) 하나로 묶어 은행별 차액을 계산한다.

```
매일 00:05 KST    허브가 어제(KST) 정산일로 run을 만든다 → DRAFT
                    대상: 마감(오늘 00:00 KST) 전에 COMPLETED 된 거래 중 아직 어느 run에도 들어가지 않은 것 전부
(대사 기간)        은행이 10.5로 자기 몫을 받아 맞춰 본다 (10.6)
관리자 확정        → SETTLED: 은행별 차액이 예치금에 반영된다
또는 관리자 취소   → CANCELLED: 그 run의 거래는 미정산으로 돌아가 다음 run에 다시 들어간다
```

| `status` | 뜻 | 은행이 할 일 |
|---|---|---|
| `DRAFT` | 차액만 계산했다. 예치금은 아직 그대로 | 맞춰 보기만 한다. 확정될 때까지 다시 확인 |
| `SETTLED` | 예치금에 반영했다. 더 바뀌지 않는다 | 맞춰 보고, 맞으면 자기 내부 계정을 정리한다 (10.6) |
| `CANCELLED` | 확정 전에 취소했다 | 무시한다. 그 거래는 다음 run에 다시 나온다 |

- **한 날짜에 run이 여럿일 수 있다.** 허브 관리자가 시연·점검을 위해 "지금 즉시 마감"으로 run을 만들 수 있기 때문이다 (그 run의 `cutoff_at`은 만든 순간). 그래서 은행은 날짜가 아니라 **`run_id` 단위**로 맞춰 본다.
- 대상은 날짜 범위가 아니라 **"확정 정산에 아직 안 들어간 `COMPLETED` 거래"**다. 며칠 전에 접수됐다가 늦게 `COMPLETED`가 된 거래(3.4의 `UNKNOWN`을 관리자가 정리한 것 등)도 그다음 run에 들어간다. 반대로 마감 전에 접수됐어도 마감 뒤에 `COMPLETED`가 된 거래는 다음 run으로 간다. 기준 시각은 허브가 기록한 `finished_at`(3.3·3.4 응답의 값)이다.
- `COMPLETED`가 아닌 거래(`FAILED`·`PENDING`·`UNKNOWN` 등)는 어느 run에도 들어가지 않는다.
- `DRAFT`는 동시에 하나만 있다. 확정하지 않은 `DRAFT`가 남아 있으면 그날 배치는 run을 만들지 않고 건너뛴다 (거래는 미정산으로 남아 다음 run에 들어간다). 그래서 `DRAFT`는 항상 가장 최근 run이고, 그보다 앞선 run은 모두 `SETTLED`나 `CANCELLED`다.
- 거래가 하나도 없는 날에도 매일 배치는 run을 만들고, 대사할 것이 없으므로 바로 `SETTLED`로 확정한다.
- 확정은 허브 관리자가 은행들의 대사가 끝난 것을 보고 한다. 정해진 확정 시각은 없다 (8절 8).

**부호와 결과**

| 필드 | 뜻 |
|---|---|
| `net_amount` | 그 run에서 내 차액 = **받을 돈 − 줄 돈** (`recv_total − sent_total`). 양수면 확정 때 내 예치금이 늘고, 음수면 준다. 모든 은행의 `net_amount`를 더하면 0이다 |
| `result` | 확정 뒤 내 예치금의 상태 (아래 표). 확정 전에는 `null` |
| `balance_after` | 확정으로 반영한 직후의 내 예치금 잔액. 확정 전에는 `null` |

| `result` | 반영 뒤 잔액 | 뜻 |
|---|---|---|
| `OK` | 0 이상 | 정상 |
| `OVERDRAWN` | 음수지만 −순채무한도 이상 | 한도 안의 마이너스. 송금은 남은 `available` 안에서 계속 된다 |
| `OVERDUE` | −순채무한도 미만 | 한도를 넘었다. 그래도 반영은 하고, 그 은행의 **송금은 막히고 받기만 된다** (10.2의 검사에 걸린다). 허브 관리자가 예치금을 넣으면 풀린다 |

### 10.4 정산 목록 `GET /v1/settlements`

인증 필요(2절). 본문 없는 GET이고 쿼리는 없다 (서명의 `PATH`는 `/v1/settlements`).

최근 30일(`settle_date`가 오늘(KST) − 30일 이후) run을 **최신순**(`run_id` 큰 것부터)으로 최대 100개 준다. `CANCELLED`는 목록에서 빠진다. 전에 `DRAFT`로 본 run이 목록에서 사라졌으면 취소된 것이고, 10.5로 부르면 `CANCELLED`로 나온다.

응답 `200`
```json
{
  "settlements": [
    { "run_id": 12, "settle_date": "2026-11-06", "cutoff_at": "2026-11-06T15:00:00Z", "status": "DRAFT",
      "settled_at": null, "net_amount": -70000, "result": null },
    { "run_id": 11, "settle_date": "2026-11-05", "cutoff_at": "2026-11-05T15:00:00Z", "status": "SETTLED",
      "settled_at": "2026-11-06T01:10:00Z", "net_amount": 20000, "result": "OK" }
  ]
}
```

| 필드 | 설명 |
|---|---|
| `run_id` | 정수. 정산 회차 ID. 클수록 나중에 만든 run이다 |
| `settle_date` | 정산일 `YYYY-MM-DD` (KST) |
| `cutoff_at` | 마감 시각(UTC, 1절). 이 시각 전에 `COMPLETED` 된 미정산 거래까지 모았다. 매일 배치의 run은 정산일 다음 날 00:00 KST(= 정산일 15:00 UTC) |
| `status` | `DRAFT` / `SETTLED` (10.3) |
| `settled_at` | 확정 시각. `DRAFT`면 `null` |
| `net_amount` | 그 run에서 **내** 차액 (10.3). 그 run에 내 거래가 없으면 0 |
| `result` | **내** 결과 `OK` / `OVERDRAWN` / `OVERDUE`. 확정 전이거나 그 run에 내 거래가 없으면 `null` |

다른 은행의 차액·결과는 나오지 않는다. 내 거래가 없는 run도 목록에 나온다 (`net_amount` 0).

오류

| 코드 | HTTP | 경우 |
|---|---|---|
| `UNAUTHORIZED`·`SIGNATURE_INVALID`·`SIGNATURE_EXPIRED`·`BANK_INACTIVE` | 401 | 요청한 은행의 인증 실패 (2절) |

### 10.5 대사 파일 `GET /v1/settlements/{run_id}`

인증 필요(2절). 본문 없는 GET이고 쿼리는 없다 (서명의 `PATH`는 `/v1/settlements/12`처럼 `run_id`까지 넣은 경로).

그 run에서 **내 포지션과, 내가 보내거나 받은 거래 목록**을 준다. 30일이 지나 목록(10.4)에서 빠진 run도 `run_id`를 알면 계속 볼 수 있다.

응답 `200` (확정 뒤 `901`이 부른 경우. 901 → 902 5만 원 2건, 902 → 901 3만 원 1건)
```json
{
  "run_id": 12,
  "settle_date": "2026-11-06",
  "cutoff_at": "2026-11-06T15:00:00Z",
  "status": "SETTLED",
  "settled_at": "2026-11-07T00:40:00Z",
  "position": {
    "sent_count": 2, "sent_total": 100000,
    "recv_count": 1, "recv_total": 30000,
    "net_amount": -70000, "result": "OK", "balance_after": 930000
  },
  "transfers": [
    { "transfer_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "direction": "SENT", "counterparty_bank": "902",
      "amount": 50000, "finished_at": "2026-11-06T01:20:03Z" },
    { "transfer_id": "16fd2706-8baf-433b-82eb-8c7fada847da", "direction": "SENT", "counterparty_bank": "902",
      "amount": 50000, "finished_at": "2026-11-06T03:02:11Z" },
    { "transfer_id": "886313e1-3b8a-4372-9b90-0c9aee199e5d", "direction": "RECEIVED", "counterparty_bank": "902",
      "amount": 30000, "finished_at": "2026-11-06T05:45:40Z" }
  ]
}
```

같은 run을 `902`가 부르면 `position`은 `sent_count 1, sent_total 30000, recv_count 2, recv_total 100000, net_amount 70000`이고, 같은 세 거래가 방향만 반대로(`RECEIVED` 2건·`SENT` 1건, `counterparty_bank: "901"`) 나온다.

`run_id`·`settle_date`·`cutoff_at`·`status`·`settled_at`은 10.4와 같다 (`status`에 `CANCELLED`가 나올 수 있다는 점만 다르다). 이 밖의 필드:

| 필드 | 설명 |
|---|---|
| `position.sent_count`, `position.sent_total` | 그 run에서 내가 보낸 거래의 건수·금액 합 (줄 돈) |
| `position.recv_count`, `position.recv_total` | 그 run에서 내가 받은 거래의 건수·금액 합 (받을 돈) |
| `position.net_amount` | `recv_total − sent_total` (10.3) |
| `position.result`, `position.balance_after` | 확정 뒤의 내 결과와 잔액 (10.3). 확정 전에는 `null` |
| `transfers` | 그 run에서 내가 보내거나 받은 거래, `finished_at` 순. 모두 `COMPLETED`다 |
| `transfers[].transfer_id` | 보내는 은행이 만든 거래 ID (3.3) |
| `transfers[].direction` | `SENT`: 내가 보냄 (3.3으로 보낸 거래) / `RECEIVED`: 내가 받음 (4.2로 입금한 거래) |
| `transfers[].counterparty_bank` | 상대 은행 코드 |
| `transfers[].amount` | 금액(원) |
| `transfers[].finished_at` | 허브가 `COMPLETED`로 기록한 시각 (3.3·3.4의 `finished_at`과 같은 값) |

- `transfers`는 `position`과 맞는다. `SENT`의 건수·합이 `sent_count`·`sent_total`, `RECEIVED`의 건수·합이 `recv_count`·`recv_total`이다.
- 계좌번호·이름·메모는 넣지 않는다. 대사에는 `transfer_id`·방향·금액이면 충분하고, 나머지는 은행 자기 DB에 있다. **다른 은행의 포지션·잔액도 나오지 않는다.**
- 그 run에 내 거래가 없으면 404가 아니라 `200`이다. `position`의 숫자는 모두 0, `result`·`balance_after`는 `null`, `transfers`는 `[]`.
- `CANCELLED` run도 `200`이다. `status: "CANCELLED"`, `transfers: []`이고, `position`에는 취소 전에 계산했던 값이 기록으로 남아 있다. **은행은 `CANCELLED`면 내용을 보지 말고 무시한다** (그 거래는 다음 run에 다시 나온다).

오류

| 코드 | HTTP | 경우 |
|---|---|---|
| `UNAUTHORIZED`·`SIGNATURE_INVALID`·`SIGNATURE_EXPIRED`·`BANK_INACTIVE` | 401 | 요청한 은행의 인증 실패 (2절). 서명의 `PATH`에 `run_id`를 빠뜨리면 `SIGNATURE_INVALID` |
| `SETTLEMENT_NOT_FOUND` | 404 | 없는 `run_id` |
| `VALIDATION_ERROR` | 422 | `run_id`가 정수가 아님 |

### 10.6 은행이 할 일 (대사 절차, 권장)

대사는 "허브가 기록한 내 거래"와 "내 장부의 거래"를 **`transfer_id` 하나하나와 금액으로** 맞춰 보는 일이다. 합계만 비교하면 금액이 같은 서로 다른 거래가 바뀌어 있어도 놓친다.

```
매일 00:30 KST 이후 (허브 배치는 00:05. 그 뒤라면 몇 시든 된다)
1. GET /v1/settlements
   → 아직 맞춰 보지 않은 run, 또는 DRAFT 때 맞춰 봤지만 아직 SETTLED 처리를 하지 않은 run을 run_id 작은 것부터
     새 run이 없으면 허브가 아직 배치를 안 돌렸거나 DRAFT가 확정되지 않은 것이다. 1시간 뒤 다시
2. run마다 GET /v1/settlements/{run_id}
   ├ CANCELLED → 무시하고 다음 run으로 (그 거래는 다음 run에 다시 나온다)
   ├ DRAFT     → 3의 비교만 한다. 내부 계정은 정리하지 않고, SETTLED가 될 때까지 다음 주기에 다시 본다
   └ SETTLED   → 3의 비교, 일치하면 4
3. 비교: transfers와 내 장부를 transfer_id 집합과 금액·방향으로 맞춘다
   내 장부 쪽 후보: 아직 어느 run에서도 맞춰지지 않은 거래 전부 (날짜로 자르지 않는다. 10.3)
     SENT     ↔ 내가 보내서 COMPLETED로 마무리한 거래 (6절)
     RECEIVED ↔ 내가 4.2로 입금한 거래
   ├ 모두 같다 (position.net_amount도 내 계산과 같다)   → 일치
   ├ 허브 목록의 ID가 내 쪽에서 아직 PENDING이다         → 불일치가 아니라 "확인 중".
   │   (허브는 COMPLETED인데 내가 결과를 아직 못 받음)      3.4로 결과를 확인해 마무리한 뒤 다시 비교한다
   ├ 허브에만 있는 ID, 금액·방향이 다른 ID              → 불일치
   └ 내 쪽에만 있는 ID                                  → 아래 "내 쪽에만 있는 거래"
   불일치면 그 run의 자동 처리를 멈추고 허브 관리자에게 run_id와 차이 나는 transfer_id를 알린다
4. SETTLED이고 일치하면 [한 트랜잭션] 내부 계정을 정리한다
   타행 정산 계정(이체 때 쌓인 줄 돈·받을 돈) → 허브 예치금 계정으로 position.net_amount만큼 옮긴다
   run_id를 키로 저장해 같은 run을 두 번 정리하지 않는다. 맞춘 거래에도 run_id를 표시해 다음 대사에서 다시 세지 않는다
5. 서버가 재시작되면 맞춰 보지 않은 run부터 1의 순서대로 이어서 한다
```

**내 쪽에만 있는 거래.** 내 장부에는 끝난 거래로 있는데 허브의 그 run에 없는 거래는 바로 불일치로 보지 않는다. 다음은 정상이다.

- 마감 직전에 끝난 거래: 허브의 `finished_at`이 `cutoff_at` 뒤면 다음 run에 들어간다. 특히 받은 거래는 허브가 4.2 응답을 받아 기록하므로, 허브의 시각이 내가 입금한 시각보다 조금 늦다
- 허브가 아직 결과를 모르는 거래: 입금은 했는데 허브 쪽이 `PENDING`·`UNKNOWN`이면(4.2 응답이 늦거나 끊긴 경우) `COMPLETED`가 된 뒤의 run에 들어간다

그래서 다음 run으로 넘겨 두고, 끝난 시각이 `cutoff_at`보다 충분히(예: 1시간) 앞서는데도 그 뒤 run에 계속 없으면 허브 관리자에게 알린다. 관리자가 허브 쪽 거래를 확인한다 (`UNKNOWN`이면 정리한다, 3.4).

**불일치가 났을 때.** 허브는 거래 기록과 확정된 정산을 고치지 않는다. 허브 관리자가 원인을 확인하고, 돈을 바로잡아야 하면 예치금 정정(허브 관리자 기능)으로 처리한다. 은행은 관리자와 맞춘 뒤 그 run의 내부 계정을 직접 정리한다. 한 run이 불일치여도 이체와 다른 run의 대사는 계속된다.

### 10.7 한계 (이 프로젝트에서 받아들이는 것)

- **모의 예치금**이다. 예치금 숫자는 실제 돈과 무관하고, 넣고 빼는 것은 허브 관리자가 한다. 고객 계좌의 돈은 지금처럼 각 은행 DB에서만 움직인다
- **손실분담이 없다.** `OVERDUE` 은행이 예치금을 끝내 채우지 않을 때 그 금액을 다른 은행이 나눠 지는 규칙, 담보 처분은 범위 밖이다. 허브는 그 은행의 송금을 막을 뿐이다
- **허브 운영자를 믿는 구조다** (9.6과 같은 성격). 거래 기록과 예치금 원장이 모두 허브에 있어서, 대사로 은행 쪽의 실수나 속임은 찾을 수 있지만 허브가 기록을 꾸미는 것까지 막지는 못한다. 또 HMAC 서명(2절)은 secret을 허브와 나눠 갖기 때문에, 분쟁 때 "그 요청을 누가 보냈는지"를 제3자에게 증명할 수 없다. `ed25519` 모드(2.6)로 전환한 은행은 요청 서명과 응답 서명(2.5)으로 "보낸 사실"과 "답한 사실"을 증명할 수 있다. 다만 허브가 거래를 빠뜨리거나 기록을 지우는 것까지 막지는 못한다 (은행이 서명된 원문을 보관해 대사 때 맞춰 본다)
- 확정 시각은 정해져 있지 않다 (10.3). 확정이 늦어지면 그동안 `DRAFT`의 거래는 미정산 순채무(10.2)에 계속 들어 있다

---

## 변경 이력

| 버전 | 날짜 | 내용 |
|---|---|---|
| 0.1 | 2026-11-05 | 초안 |
| 0.2 | 2026-11-05 | 허브 주소 기입, 4.3 응답에 `holder_name`(권장) 추가, `SAME_BANK` 추가, 금액·비활성 은행·수취 조회 오류의 HTTP 코드를 구현에 맞춤, 연동 절차를 관리자 API 기준으로 정리, 가짜 은행 시험 계좌 안내 |
| 0.3 | 2026-11-05 | 9절 본인 명의 계좌 조회(선택 기능) 초안: 가상 CI, `/v1/accounts/lookup`, `{base_url}/accounts`, `account_ref` 이체. 오류 `ACCOUNT_REF_INVALID`·`TOO_MANY_LOOKUPS`, 8절 11~15 추가 |
| 0.3.1 | 2026-11-05 | 9절 허브·가짜 은행 구현. 가짜 은행 시험 고객 안내, 3.4에 계좌번호가 나오지 않는다는 점 바로잡음 |
| 0.4 | 2026-11-05 | 2.4 추적 ID `X-Request-ID`(선택, 서명 제외, 허브가 받는 은행까지·재시도에도 같은 값으로 전달). 5절에 "5xx 본문은 형식을 보장하지 않는다, 상태 코드로 판단" 추가. 8절 결정 칸을 구현에 맞춰 채움(1·2·3·4·5·7·11·12·14·15 확정, 1번은 자동 배정이 아니라 관리자 지정으로 바로잡음, 6·8은 미정·미구현), 16 추가 |
| 0.4.1 | 2026-11-05 | 허브를 규격에 맞춤: 받는 은행의 401·403을 `RECEIVER_ERROR`로 바꿔 전달(5절 표 설명 보강), 처리하지 못한 500에도 `X-Request-ID`(2.4), 응답 시각을 초 단위 UTC로(1절, 마이크로초 제거), `transfer_id`의 UUID v4 검사(1절) |
| 0.4.2 | 2026-11-05 | 경미한 차이도 규격대로: 3.3 응답을 200 `COMPLETED`·202 `PENDING`·4xx `FAILED` 셋으로 한정(재전송 때 `ACCEPTED`·`DELIVERING`·`UNKNOWN`도 202 `PENDING`), 5절 표 밖 코드를 쓰지 않음(받는 은행의 표 밖 거절 코드 → `RECEIVER_ERROR`, HTTP 상태는 표대로, 허용하지 않는 메서드도 404 `NOT_FOUND`) |
| 0.4.3 | 2026-11-05 | 다른 팀 배포 전 보강. 2절: secret 하나를 양방향에 쓴다는 점, 허브 → 은행에는 `X-Api-Key`를 넣지 않는다는 점, NTP, 재전송 때 서명 새로 만들기. 3.3: 4xx 본문 예시, `status: "FAILED"`가 붙는 경우와 붙지 않는 경우(401·403·422 → 되돌리지 않음), 5xx 처리, 같은 ID에 다른 본문. 3.4: `UNKNOWN`은 자동 확인 중단, `TRANSFER_NOT_FOUND`면 되돌려도 됨. 4.2: 유니크 제약·동시 요청, 서명 검증 먼저, 4xx 거래는 저장하지 않음. 4.3 응답을 200 `COMPLETED`·404 둘로 한정. 6절 분기를 다시 씀(30분 뒤 처리 포함). 7절 ping 결과 보는 법. 1절 `memo` 선택. 9.6에 이름을 가리지 않는다는 점. 허브: 접수 전 거절(`SAME_BANK`·`LIMIT_EXCEEDED`·`BANK_NOT_FOUND`·`BANK_INACTIVE`·`ACCOUNT_REF_INVALID`)에도 `status: "FAILED"`를 붙임 |
| 0.4.4 | 2026-11-05 | 다른 팀 배포 전 검토 반영 (허브 동작 변경 없음, 문서를 구현에 맞춤). 1절: 모르는 필드는 무시. 2절: 키 재발급 때 겹치는 기간이 없다는 점과 그 영향. 3.2·3.3·9.2에 필드 표, 3.2 오류 표(인증·422 포함, 같은 은행 조회는 거절하지 않음). 3.3: "되돌릴지는 본문 `status`로만" 원칙, `holder_name`이 `null`일 수 있음. 3.4 예시에 `holder_name` 추가와 필드 설명. 4.2: 응답 필드 표, 허브는 2xx를 본문과 상관없이 입금 완료로 본다는 점. 7절 6: 저장소 없이도 연동 가능하게 참고 구현 안내 정리. 9.4·8절 13: 운영 `902`는 운영 `CI_KEY`로 시험해야 한다고 바로잡음 |
| 0.4.5 | 2026-11-05 | 2차 검토 반영 (허브 동작 변경 없음). 맨 앞에 구현 체크리스트. 3.1: 예시의 `902` 이름을 `SimBank`로 바로잡음, 목록에 자기 은행도 들어 있다는 점. 3.4: `403 FORBIDDEN`. 4.1: `holder_name` 필수(없으면 허브가 502 `RECEIVER_ERROR`). 9.2: `ERROR`의 범위, `account_masked` 규칙. 9.4: 응답 필드 표, 형식이 틀린 항목은 버림, 401·403은 `ERROR` |
| 0.4.6 | 2026-11-06 | 3차 검토 반영 (허브 동작 변경 없음). 4.2 규칙 2: 동시 중복 요청에서 유니크 제약에 걸린 쪽은 4xx로 답하면 안 된다(이중 지급)고 강제, 체크리스트 추가. 7절 5: `902`로는 보내는 쪽만 시험된다는 점과 `901`에서 이체를 받아 하는 받는 쪽 시험 절차, 멱등성은 은행이 직접 서명해 시험 |
| 0.4.7 | 2026-11-06 | 4차 검토 반영 (허브 동작 변경 없음). 3.4·6절: 결과 조회의 `404 TRANSFER_NOT_FOUND`만 보고 바로 되돌리지 않고 같은 `transfer_id`로 POST 재전송(타임아웃 난 POST를 허브가 아직 처리 중이면 이중 지급). 꼭 되돌릴 때는 마지막 POST 뒤 1분이 지난 404만, 그 ID는 다시 보내지 않음. 체크리스트·"하지 말아야 할 것" 추가. 2.3: `verify`가 잘못된 헤더 값에 예외 대신 `False`, 401 코드 나누는 법. 4.2: `to_account`는 항상 들어오고 `to_account_ref`는 오지 않음, 허브 재시도는 4.3 확인 뒤 404일 때만 4.2 재전송 |
| 0.5 | 2026-11-06 | **서명 형식 변경 (이전 버전과 호환되지 않음).** 2.2: `message`에 `METHOD`(대문자)와 `PATH`를 넣는다. 그전에는 본문이 없는 GET의 서명이 같은 초 안에서 모두 같아 다른 경로에 다시 쓸 수 있었다. `PATH`는 은행 → 허브는 `/v1`부터, 허브 → 은행은 `base_url` 뒤의 부분이다 (은행 앞단 프록시가 경로를 바꿔도 맞게). 쿼리스트링은 쓰지 않는다. 2.3 예시 값을 새로 계산하고 GET 예시 추가, 참고 구현 갱신. 7절 4 ping 401 안내, 8절 17, 체크리스트 추가. 허브·가짜 은행·AvengersBank 동시 변경 |
| 0.5.1 | 2026-11-06 | 관리자 정리 기능 (은행 연동 방식은 그대로). 3.4: `UNKNOWN`을 관리자가 다시 자동 확인(`PENDING`으로 되돌림)하거나 `COMPLETED`·`FAILED`(`fail_code` `RECEIVER_ERROR`)로 직접 확정한다. 7절 4: 등록 뒤 `base_url`·이름 변경은 관리자에게 요청 |
| 0.5.2 | 2026-11-06 | 순채무한도 검사 (정산 2단계, 은행 연동 방식은 그대로). 1절: 보내는 은행의 미정산 순채무 + 금액이 예치금 + 순채무한도를 넘으면 접수 전 거절 `LIMIT_EXCEEDED`(400, `status: "FAILED"`). 1회 상한과는 `message`로 구분. 예치금·한도는 허브 관리자가 넣는다 |
| 0.6 | 2026-11-06 | **10절 정산** (4절 허브 → 은행 API는 그대로, 대사는 선택). 10.1 모의 예치금으로 하는 하루 단위 상계. 10.2 예치금·순채무한도와 `LIMIT_EXCEEDED`, 미정산 순채무 계산, `GET /v1/reserve`. 10.3 정산 회차(run) `DRAFT`·`SETTLED`·`CANCELLED`, 한 날짜에 여러 run, "미정산 `COMPLETED`" 기준, `net_amount` 부호와 `result`(`OK`·`OVERDRAWN`·`OVERDUE`). 10.4 `GET /v1/settlements`(최근 30일, `CANCELLED` 제외), 10.5 대사 파일 `GET /v1/settlements/{run_id}`(내 포지션과 내 거래만, 계좌번호·이름 없음). 10.6 은행의 대사 절차, 10.7 한계. 5절 `SETTLEMENT_NOT_FOUND`, `LIMIT_EXCEEDED` 설명 보강. 7절 2: 등록 직후 예치금 0이면 송금 불가. 8절 8 확정, 18 추가. 체크리스트에 예치금·정산 |
| 0.6.1 | 2026-11-06 | 문서·가짜 은행만 (허브 동작과 은행 연동 방식은 그대로). 7절 5: 가짜 은행 `902`에 대사 시연용 계좌 `…0006`(입금하지 않고 `200 COMPLETED`로 답함) 추가. 허브 루트 `/` 응답의 `spec`을 `v0.6.1`로 |
| 1.0 (초안) | 2026-11-06 | **부인방지** (기존 HMAC 은행은 그대로 동작, 모드 `hmac`). 2절 제목을 "공개키 서명(권장) / HMAC(전환 기간)"으로, 은행별 서명 모드 `hmac`·`both`·`ed25519`. 2.1 `X-Signature-Alg`(없으면 HMAC). 2.2 Ed25519 서명(메시지는 그대로). 2.3 예시용 Ed25519 키 쌍과 예시 1·2의 서명 값, `cryptography` 참고 구현. 새 2.5 응답 서명(`X-Response-Timestamp`·`X-Response-Signature`, 요청의 `X-Signature`에 묶음, `ed25519` 모드에서 검증 실패는 "응답 없음"), 예시 3. 새 2.6 `GET /v1/hub-key`, `PUT`·`GET /v1/keys`, 지문, 24시간 이전 키, 전환 순서. 4절: `ed25519` 모드 은행의 검증·응답 서명. 5절 `HUB_KEY_MISSING`, `SIGNATURE_INVALID` 설명. 7절 4 ping의 `response_signature_valid`. 8절 17 갱신, 19 추가. 10.7. 체크리스트 "부인방지(선택)". 허브는 모든 `/v1` 응답에 서명하고 HMAC 요청에도 `X-Signature-Alg: hmac`을 넣는다. 루트 `/` 응답 `v1.0` |

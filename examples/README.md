# 참고 구현

참가 은행이 [docs/SPEC.md](../docs/SPEC.md)대로 만든 서버의 예다. 규격의 기준은 언제나 SPEC이고, 여기 코드는 읽고 베끼기 위한 것이다.

| 언어 | 위치 | 내용 |
|---|---|---|
| Python (FastAPI) | [`src/bank_sim/main.py`](../src/bank_sim/main.py) | 가짜 은행 `902`. 받는 쪽 API(SPEC 4절·9.4), 허브 서명 검증, 응답 서명. 계좌번호 끝 4자리로 성공·실패·지연·속임을 재현한다. 허브 패키지의 `signing`·`response_signing`을 import한다 |
| Python (최소) | [SPEC "빠른 시작"](../docs/SPEC.md) 2·3·5단계 | `signing.py`(서명·검증 4개), `bank.py`(받는 쪽 최소 구현, 메모리 저장), `send.py`(보내는 쪽). 허브 테스트 `tests/test_quickstart.py`가 실제로 실행한다 |
| Node.js | `examples/node-bank` (준비 중) | SPEC만 보고 만든 받는 쪽·보내는 쪽 은행 (다음 단계에서 추가) |

## 로컬에서 붙여 보기

허브 저장소 루트에서 허브와 가짜 은행을 한 번에 띄운다 (PostgreSQL 없이 SQLite):

```bash
uv run python -m clearing_hub.devserver
```

콘솔에 나오는 초대 문구(초대 코드·허브 공개키·지문)로 내 은행을 셀프 등록한다. 로컬에서는 `base_url`을 `http://127.0.0.1:3000/interbank`처럼 http로 등록해도 된다. 자세히는 [README.md](../README.md)의 "로컬에서 한 명령으로".

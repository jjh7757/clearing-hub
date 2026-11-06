"""관리자 알림 웹훅 (SETTLEMENT_PLAN 13절). `ALERT_WEBHOOK_URL`이 있으면 사건마다 한국어 한두 줄을 POST한다.

본문은 `{"text": …, "content": …}`다. Slack incoming webhook은 `text`, Discord webhook은 `content`를 읽으므로 둘 다 같은
글을 넣는다. 사건 종류(`kind`)는 글 맨 앞 `[ClearingHub·kind]`에, 핵심 ID(`context`)는 둘째 줄에 붙인다.

**알림은 부가 기능이다.** 웹훅이 느리거나 죽어도 이체·정산이 기다리거나 실패하면 안 된다. 그래서
- `notify`는 보낼 일을 `asyncio.create_task`로 띄우고 바로 돌아간다 (fire-and-forget). 호출한 쪽은 await하지 않는다
- 타임아웃 5초, 실패는 `logger.warning`만 남긴다. 예외를 올리지 않는다
- 돌려주는 Task는 테스트가 기다리는 용도다 (`drain`도 같다). 앱 종료 때 lifespan이 남은 알림을 잠깐 기다린다

트랜잭션 안에서 부르는 곳(정산 확정의 OVERDUE 등)은 커밋 전에 알림이 나갈 수 있다. 커밋이 실패하면 알림만 남는데,
그때는 같은 사건의 로그(logger.error)와 실패 로그가 함께 남으므로 관리자가 알 수 있다.
"""

import asyncio
import logging

import httpx

from clearing_hub.core.config import get_settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5
MAX_TEXT = 1900  # Discord content 한도(2000자) 안쪽

_client: httpx.AsyncClient | None = None  # 테스트는 MockTransport 클라이언트로 바꾼다
_pending: set[asyncio.Task] = set()  # 끝나기 전에 Task가 가비지 컬렉션되지 않게 잡아 둔다


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def format_text(kind: str, text: str, context: dict) -> str:
    """`[ClearingHub·kind] text` + (있으면) 둘째 줄에 `key=value` 목록."""
    line = f"[ClearingHub·{kind}] {text}"
    details = " ".join(f"{key}={value}" for key, value in context.items() if value is not None)
    return (f"{line}\n{details}" if details else line)[:MAX_TEXT]


async def _post(url: str, kind: str, message: str) -> None:
    try:
        response = await asyncio.wait_for(
            _http().post(url, json={"text": message, "content": message}, timeout=TIMEOUT_SECONDS), TIMEOUT_SECONDS)
        if response.status_code >= 400:
            logger.warning("알림 웹훅 실패 (%s): HTTP %s", kind, response.status_code)
    except Exception as exc:  # 알림 실패가 호출한 쪽으로 번지지 않게 모두 삼킨다
        logger.warning("알림 웹훅 실패 (%s): %s", kind, type(exc).__name__)


def notify(kind: str, text: str, **context) -> asyncio.Task | None:
    """알림 하나를 띄우고 바로 돌아간다. 웹훅이 없거나 이벤트 루프 밖이면 None (아무것도 안 한다).

    kind: 사건 종류 (`dispute`, `auto_confirm`, `overdue`, `unknown`, `batch_skipped`, `register`, `ping_failed`,
    `self_test`, `deadline_held`, `sim_bank`). text: 한국어 한두 줄. context: 핵심 ID (`run_id=12` 등)."""
    url = get_settings().alert_webhook_url
    if not url:
        return None
    message = format_text(kind, text, context)
    try:
        task = asyncio.get_running_loop().create_task(_post(url, kind, message))
    except RuntimeError:  # 루프 밖(동기 코드)에서 불렀다. 알림은 버리고 로그만
        logger.warning("알림을 보내지 못했습니다 (이벤트 루프 없음): %s", message)
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def drain(timeout: float = TIMEOUT_SECONDS) -> None:
    """아직 보내는 중인 알림을 기다린다 (테스트, 앱 종료). timeout이 지나면 그냥 돌아간다."""
    if _pending:
        await asyncio.wait(list(_pending), timeout=timeout)

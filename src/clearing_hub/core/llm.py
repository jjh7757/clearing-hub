"""LLM(Gemini) 호출을 한곳에 모은다 (SETTLEMENT_PLAN 14절). 쓰는 곳은 연동 지원 설명(domains/support/service.py)뿐이다.

**읽기 전용 설명에만 쓴다.** 원인은 규칙 진단기(core/signature_doctor.py)가 찾고, LLM은 은행이 붙여 넣은 코드의 고칠 줄을
짚고 질문을 실패 기록과 이어 답하는 일만 한다. 돈을 움직이는 기능(이체·정산·예치금)과 등록 정보에는 쓰지 않는다 (SPEC 6절).

- AvengersBank와 같은 Gemini를 쓴다 (`GEMINI_API_KEY`). SDK는 `google-genai`(AvengersBank의 langchain 래퍼보다 가볍다:
  LangChain 없이 호출 한 번이면 된다)
- `GEMINI_API_KEY`가 없으면 `enabled()`가 False이고 호출하는 쪽은 규칙 결과만 돌려준다
- 도구 없이 한 번 호출한다. 타임아웃 `DIAGNOSE_TIMEOUT_SECONDS`(30초), temperature 0, 생각(thinking) 끔(짧은 설명)
- 테스트는 `complete`를 monkeypatch해 고정 답을 돌려준다. 실제 API는 부르지 않는다
"""

import asyncio
import logging

from google import genai
from google.genai import errors, types

from clearing_hub.core.config import get_settings

logger = logging.getLogger(__name__)


class LlmUnavailable(Exception):
    """키 없음·타임아웃·API 오류·빈 답. 호출한 쪽은 규칙 결과만 돌려준다."""


def enabled() -> bool:
    key = get_settings().gemini_api_key
    return key is not None and bool(key.get_secret_value().strip())


_client: genai.Client | None = None


def _gemini() -> genai.Client:
    global _client
    if _client is None:
        settings = get_settings()
        _client = genai.Client(api_key=settings.gemini_api_key.get_secret_value().strip(),
                               http_options=types.HttpOptions(timeout=int(settings.diagnose_timeout_seconds * 1000)))
    return _client


async def complete(system: str, user: str) -> str:
    """시스템 지시 + 사용자 메시지 한 번 → 답 글. 실패하면 LlmUnavailable."""
    if not enabled():
        raise LlmUnavailable("GEMINI_API_KEY가 없습니다")
    settings = get_settings()
    config = types.GenerateContentConfig(
        system_instruction=system, max_output_tokens=settings.diagnose_max_tokens, temperature=0,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    try:
        response = await asyncio.wait_for(
            _gemini().aio.models.generate_content(model=settings.diagnose_model, contents=user, config=config),
            settings.diagnose_timeout_seconds,
        )
    except TimeoutError as exc:
        raise LlmUnavailable("시간 초과") from exc
    except errors.APIError as exc:
        logger.warning("LLM 호출 실패: %s %s", exc.code, exc.status)
        raise LlmUnavailable(f"HTTP {exc.code}") from exc
    except Exception as exc:  # 연결 오류 등 (httpx 예외 종류가 SDK 버전마다 다르다)
        logger.warning("LLM 연결 실패: %s", type(exc).__name__)
        raise LlmUnavailable("연결 실패") from exc
    text = (response.text or "").strip()
    if not text:
        raise LlmUnavailable("빈 답 (차단됐거나 출력 한도)")
    return text

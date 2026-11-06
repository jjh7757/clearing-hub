"""LLM(Claude) 호출을 한곳에 모은다 (SETTLEMENT_PLAN 14절). 쓰는 곳은 연동 지원 설명(domains/support/service.py)뿐이다.

**읽기 전용 설명에만 쓴다.** 원인은 규칙 진단기(core/signature_doctor.py)가 찾고, LLM은 그 결과·SPEC 발췌·팀이 붙여 넣은
코드를 이어 한국어로 설명한다. 돈을 움직이는 기능(이체·정산·예치금)과 등록 정보에는 쓰지 않는다 (SPEC 6절).

- `ANTHROPIC_API_KEY`가 없으면 `enabled()`가 False이고 호출하는 쪽은 규칙 결과만 돌려준다
- 도구 없이 한 번 호출한다 (진단과 SPEC을 미리 넣어 주므로 충분하다). 타임아웃 `DIAGNOSE_TIMEOUT_SECONDS`(30초)
- 모델 `DIAGNOSE_MODEL`(기본 `claude-sonnet-5-5`), effort `low`(짧은 설명), 서버 쪽 fallback `"default"`(안전 분류기가 거절하면
  서버가 정한 다른 모델로 다시 시도. 베타 `server-side-fallback-2026-07-01`)
- 테스트는 `complete`를 monkeypatch해 고정 답을 돌려준다. 실제 API는 부르지 않는다
"""

import asyncio
import logging

from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic

from clearing_hub.core.config import get_settings

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LlmUnavailable(Exception):
    """키 없음·타임아웃·API 오류·거절. 호출한 쪽은 규칙 결과만 돌려준다."""


def enabled() -> bool:
    key = get_settings().anthropic_api_key
    return key is not None and bool(key.get_secret_value().strip())


_client: AsyncAnthropic | None = None


def _anthropic() -> AsyncAnthropic:
    """API 키를 명시해 만든다 (SDK가 환경의 다른 자격 증명을 집지 않게). 재시도는 1번 (30초 안에 끝나야 한다)."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value().strip(),
                                 timeout=settings.diagnose_timeout_seconds, max_retries=1)
    return _client


async def complete(system: str, user: str) -> str:
    """시스템 프롬프트 + 사용자 메시지 한 번 → 답 글. 실패하면 LlmUnavailable."""
    if not enabled():
        raise LlmUnavailable("ANTHROPIC_API_KEY가 없습니다")
    settings = get_settings()
    try:
        response = await asyncio.wait_for(
            _anthropic().beta.messages.create(
                model=settings.diagnose_model, max_tokens=settings.diagnose_max_tokens, system=system,
                messages=[{"role": "user", "content": user}], output_config={"effort": "low"},
                betas=[FALLBACK_BETA], fallbacks="default",
            ),
            settings.diagnose_timeout_seconds,
        )
    except TimeoutError as exc:
        raise LlmUnavailable("시간 초과") from exc
    except APIStatusError as exc:
        logger.warning("LLM 호출 실패: HTTP %s", exc.status_code)
        raise LlmUnavailable(f"HTTP {exc.status_code}") from exc
    except APIConnectionError as exc:
        logger.warning("LLM 연결 실패: %s", type(exc).__name__)
        raise LlmUnavailable("연결 실패") from exc
    if response.stop_reason == "refusal":
        raise LlmUnavailable("거절")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise LlmUnavailable("빈 답")
    return text

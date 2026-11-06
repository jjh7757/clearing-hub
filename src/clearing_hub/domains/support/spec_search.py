"""SPEC 발췌 (연동 지원 LLM 설명의 근거, SETTLEMENT_PLAN 14절). `docs/SPEC.md`를 `##`·`###` 제목 단위로 잘라 두고,
가설의 `spec_ref`(절 번호)와 질문의 낱말로 관련 절 2~4개를 고른다. 임베딩 없이 규칙으로만 고른다 (절이 60여 개뿐이다).

운영 이미지에는 `docs/SPEC.md`를 넣는다 (Dockerfile). 파일이 없으면 빈 목록 (설명은 진단 결과만으로 한다).
"""

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from clearing_hub.core.config import get_settings

logger = logging.getLogger(__name__)

MAX_SECTIONS = 4
MIN_SECTIONS = 2
MAX_SECTION_CHARS = 3000  # 절 하나를 이보다 길게 넣지 않는다 (2.3은 참고 구현까지 길다)
DEFAULT_REFS = ("2.2", "2.3")  # 고를 근거가 없을 때: 서명 만들기·예시
_HEADING = re.compile(r"^(#{2,3})\s+(.*)$")
_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\.?\s")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_\-/.]{2,}|[가-힣]{2,}")


@dataclass(frozen=True)
class Section:
    ref: str  # 절 번호 ("2.2", "5"). 번호 없는 제목(구현 체크리스트 등)은 제목 그대로
    title: str
    text: str  # 제목 줄을 포함한 본문


def _spec_file() -> Path | None:
    for path in (Path(get_settings().spec_path), Path(__file__).resolve().parents[4] / "docs" / "SPEC.md"):
        if path.is_file():
            return path
    return None


def parse(markdown: str) -> list[Section]:
    sections: list[Section] = []
    ref, title, lines = None, None, []

    def flush():
        if title is not None:
            sections.append(Section(ref or title, title, "\n".join(lines).strip()))

    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            title = match.group(2).strip()
            number = _NUMBER.match(title)
            ref, lines = (number.group(1) if number else None), [line]
        elif title is not None:
            lines.append(line)
    flush()
    return sections


@lru_cache
def sections() -> tuple[Section, ...]:
    path = _spec_file()
    if path is None:
        logger.warning("docs/SPEC.md를 찾지 못했습니다 (연동 지원 설명에 SPEC 발췌가 빠집니다)")
        return ()
    return tuple(parse(path.read_text(encoding="utf-8")))


def _refs(spec_refs: list[str]) -> list[str]:
    """"2.2·2.3" 같은 값을 절 번호들로."""
    out = []
    for value in spec_refs:
        for part in re.split(r"[·,\s]+", value or ""):
            if part and part not in out:
                out.append(part)
    return out


def select(spec_refs: list[str], question: str | None) -> list[Section]:
    """가설의 절 번호를 먼저(앞이 1순위 가설), 남는 자리는 질문 낱말이 많이 나오는 절로 채운다. 2~4개."""
    all_sections = sections()
    if not all_sections:
        return []
    by_ref = {s.ref: s for s in all_sections}
    chosen: list[Section] = []
    for ref in _refs(spec_refs):
        if ref in by_ref and by_ref[ref] not in chosen:
            chosen.append(by_ref[ref])
        if len(chosen) >= MAX_SECTIONS - 1:  # 질문 낱말로 하나는 고를 자리를 남긴다
            break
    words = {w.lower() for w in _WORD.findall(question or "")}
    if words:
        scored = sorted(((sum(s.text.lower().count(w) for w in words), i, s) for i, s in enumerate(all_sections)),
                        key=lambda t: (-t[0], t[1]))
        for score, _, section in scored:
            if len(chosen) >= MAX_SECTIONS or score == 0:
                break
            if section not in chosen:
                chosen.append(section)
    for ref in DEFAULT_REFS:
        if len(chosen) >= MIN_SECTIONS:
            break
        if ref in by_ref and by_ref[ref] not in chosen:
            chosen.append(by_ref[ref])
    return chosen[:MAX_SECTIONS]


def excerpt(section: Section) -> str:
    text = section.text
    return text if len(text) <= MAX_SECTION_CHARS else text[:MAX_SECTION_CHARS] + "\n… (이하 생략)"

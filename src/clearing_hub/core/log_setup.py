"""로그 설정: 한 줄에 JSON 하나, 모든 줄에 추적 ID(request_id, SPEC 2.4).

- 그 요청 안에서 남긴 로그(은행 응답 없음 경고 등)에는 request_id가 저절로 붙는다.
- 재시도 태스크처럼 요청 밖에서 거래를 다룰 때는 거래에 저장한 처음 ID를 `extra={"request_id": ...}`로 넘긴다.
- 서버 로그에서 `grep <request_id>`로 보내는 은행 → 허브 → 받는 은행의 같은 이체를 이어 볼 수 있다.

환경변수 LOG_LEVEL(기본 INFO), LOG_FORMAT(json 기본 / text: 로컬에서 읽기 편한 형식).
"""

import json
import logging
import os
from datetime import UTC, datetime

from clearing_hub.core.request_id import get_request_id

_EXTRA_FIELDS = ("method", "path", "status", "duration_ms", "client", "transfer_id", "bank_code")
_HANDLER_NAME = "clearing_hub"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or get_request_id()
        if request_id:
            entry["request_id"] = request_id
        for field in _EXTRA_FIELDS:
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = getattr(record, "request_id", None) or get_request_id() or "-"
        return super().format(record)


def configure_logging() -> None:
    """루트 로거에 핸들러를 한 번만 단다 (여러 번 불러도 같다). uvicorn 자체 로그 설정은 건드리지 않는다."""
    root = logging.getLogger()
    if any(h.get_name() == _HANDLER_NAME for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(TextFormatter() if os.environ.get("LOG_FORMAT", "json").lower() == "text" else JsonFormatter())
    root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    # 은행 호출마다 httpx가 INFO로 한 줄씩 남기는 것은 접근 로그·경고와 겹치므로 끈다
    for noisy in ("httpx", "httpx2", "httpcore"):  # 판에 따라 로거 이름이 다르다
        logging.getLogger(noisy).setLevel(logging.WARNING)

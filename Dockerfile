# 운영 이미지: 허브(clearing_hub)와 가짜 은행(bank_sim)이 같은 이미지를 쓴다. 명령만 다르다 (deploy/server-setup.sh의 compose).
# GitHub Actions의 arm64 러너에서 빌드한다 (.github/workflows/deploy.yml). 서버에서는 빌드하지 않는다.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

# 의존성만 먼저 (코드가 바뀌어도 이 층은 캐시된다)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
# 연동 지원 LLM 설명의 SPEC 발췌 (domains/support/spec_search.py, SPEC_PATH 기본값 docs/SPEC.md)
COPY docs/SPEC.md ./docs/SPEC.md
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
# 시작할 때마다 마이그레이션을 적용한다. 워커는 1개: 재시도 태스크가 프로세스 안에서 돌기 때문에 여러 개면 같은 거래를 겹쳐 확인한다
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn clearing_hub.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]

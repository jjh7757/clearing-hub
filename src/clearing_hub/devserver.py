"""로컬 개발용: 허브(8000)와 가짜 은행 902(8100)를 한 명령으로 띄운다. **운영에서 쓰지 않는다.**

    uv run python -m clearing_hub.devserver            (종료: Ctrl+C)
    uv run python -m clearing_hub.devserver --hub-port 8001 --sim-port 8101

PostgreSQL·Alembic 없이 돈다. 처음 실행하면 아래를 만들어 `.env.dev`(현재 폴더)에 저장하고, 다음 실행 때 다시 쓴다.
  ADMIN_TOKEN       관리 API 토큰 (X-Admin-Token)
  HUB_SIGNING_KEY   허브 Ed25519 개인키. 공개키·지문은 시작할 때 초대 문구로 출력한다
  SIM_SIGNING_KEY   가짜 은행 902의 Ed25519 개인키 (공개키로 902를 허브에 등록한다)
환경 변수에 이미 있는 값이 `.env.dev`보다 먼저다. `DATABASE_URL`이 없으면 `sqlite+aiosqlite:///./dev_hub.db`.
처음부터 다시 하려면 `dev_hub.db`를 지운다 (`.env.dev`까지 지우면 허브 키가 바뀌어 등록한 은행의 허브 공개키 설정도 바꿔야 한다).

운영과 다른 점 (시작할 때 로그로도 알린다):
- 테이블은 Alembic이 아니라 `Base.metadata.create_all`로 만든다. Alembic 마이그레이션의 FK 추가(ALTER TABLE … ADD
  CONSTRAINT)가 SQLite에서 되지 않기 때문이다 (tests/conftest.py와 같은 방식)
- 정산 일일 배치(SETTLEMENT_WORKER_ENABLED=false)와 알림 웹훅(ALERT_WEBHOOK_URL 비움)은 끈다
- 은행 `base_url`은 `http://127.0.0.1:3000/interbank`처럼 http여도 된다. 허브는 원래 `base_url`의 https를 강제하지
  않는다(형식만 본다). 운영 허브에 등록할 때는 SPEC 3.5·7절대로 공개 HTTPS 주소를 쓴다
- 시작할 때마다 902가 없으면 등록하고 예치금 1,000만 원을 넣은 뒤, 새 초대 코드를 하나 만들어 초대 문구를 출력한다

가짜 은행의 계좌번호 끝 4자리: 0001 정상, 0002 계좌 없음, 0003 지연, 0004 5xx 두 번 뒤 성공, 0005 입금 후 응답 없음,
0006 입금하지 않고 200 (README "가짜 은행과 함께 돌려 보기").
"""

import argparse
import asyncio
import logging
import os
import secrets
import signal
import sys
from collections.abc import MutableMapping
from contextlib import contextmanager
from pathlib import Path

from clearing_hub.core.signing import generate_keypair, public_key_of  # signing은 설정을 읽지 않는다 (순환·이른 설정 없음)

ENV_FILE = Path(".env.dev")
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./dev_hub.db"
HOST = "127.0.0.1"
HUB_PORT, SIM_PORT = 8000, 8100
SIM_BANK_CODE, SIM_BANK_NAME = "902", "SimBank"
SIM_DEPOSIT = 10_000_000  # 902 예치금 1,000만 원: 셀프 시험(902 명의로 보내는 이체)이 한도에 걸리지 않게
GENERATED_KEYS = ("ADMIN_TOKEN", "HUB_SIGNING_KEY", "SIM_SIGNING_KEY")

logger = logging.getLogger("clearing_hub.devserver")


# ---- 설정 (.env.dev) ----

def load_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` 줄만 읽는다 (빈 줄·`#` 주석은 건너뛴다). 파일이 없으면 빈 dict."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def ensure_dev_env(path: Path = ENV_FILE) -> dict[str, str]:
    """`.env.dev`를 읽고, 없는 ADMIN_TOKEN·HUB_SIGNING_KEY·SIM_SIGNING_KEY를 만들어 저장한다. 있는 값은 그대로 쓴다."""
    values = load_env_file(path)
    missing = [key for key in GENERATED_KEYS if not values.get(key)]
    if not missing:
        return values
    for key in missing:
        values[key] = secrets.token_urlsafe(32) if key == "ADMIN_TOKEN" else generate_keypair()[0]  # 토큰 43자(≥32)
    lines = ["# clearing_hub.devserver가 만든 로컬 전용 설정. 커밋하지 않는다 (.gitignore). 운영 키로 쓰지 않는다"]
    lines += [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return values


def apply_env(values: dict[str, str], environ: MutableMapping[str, str] | None = None,
              hub_port: int = HUB_PORT) -> dict[str, str]:
    """devserver가 쓸 환경 변수를 채운다. 이미 있는 환경 변수가 먼저다 (`SIM_HUB_PUBLIC_KEY`만 허브 키에서 다시 맞춘다).

    허브 설정(core/config.py)은 처음 import될 때 읽히므로, 이 함수는 clearing_hub의 다른 모듈을 import하기 **전에** 부른다."""
    environ = os.environ if environ is None else environ
    defaults = {
        "DATABASE_URL": DEFAULT_DATABASE_URL,
        **{key: values[key] for key in GENERATED_KEYS},
        "HUB_PUBLIC_URL": f"http://{HOST}:{hub_port}",  # 초대 문구의 허브 주소
        "SETTLEMENT_WORKER_ENABLED": "false",
        "ALERT_WEBHOOK_URL": "",
        "LOG_FORMAT": "text",
    }
    for key, value in defaults.items():
        environ.setdefault(key, value)
    # 가짜 은행은 허브 공개키로 허브 요청을 검증한다. 실제로 쓰는 허브 키에서 만든다 (환경 변수의 옛 값은 덮어쓴다)
    environ["SIM_HUB_PUBLIC_KEY"] = public_key_of(environ["HUB_SIGNING_KEY"])
    return dict(environ)


# ---- DB 준비: 테이블, 902, 초대 코드 ----

async def create_tables() -> None:
    """Alembic 대신 create_all (모듈 docstring: SQLite에서 FK ALTER가 안 된다). 이미 있는 테이블은 그대로 둔다."""
    import clearing_hub.main  # noqa: F401  모든 도메인 모델을 Base.metadata에 올린다
    from clearing_hub.core.database import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ensure_sim_bank(sim_public_key: str, sim_base_url: str) -> bool:
    """902가 없으면 등록하고 예치금을 넣는다 (True). 있으면 base_url·공개키만 지금 값으로 맞춘다 (False)."""
    from clearing_hub.core.database import SessionLocal
    from clearing_hub.domains.banks import service as bank_service
    from clearing_hub.domains.banks.schemas import BankCreate
    from clearing_hub.domains.reserve import service as reserve_service

    async with SessionLocal() as db, db.begin():
        bank = await bank_service.get_bank(db, SIM_BANK_CODE)
        if bank is None:
            await bank_service.register_bank(db, BankCreate(bank_code=SIM_BANK_CODE, name=SIM_BANK_NAME,
                                                            base_url=sim_base_url, public_key=sim_public_key))
            await reserve_service.deposit(db, SIM_BANK_CODE, SIM_DEPOSIT, memo="devserver 초기 예치금")
            return True
        if bank.base_url != sim_base_url:  # 포트를 바꿔 띄운 경우
            bank.base_url = sim_base_url
        if bank.public_key != sim_public_key:  # .env.dev를 새로 만든 경우
            await bank_service.admin_register_public_key(db, SIM_BANK_CODE, sim_public_key)
        if not bank.active:
            bank.active = True
        return False


async def new_invite_message(hub_url: str, note: str = "devserver") -> str:
    """초대 코드를 하나 만들고 관리자 API(POST /admin/invites)와 같은 초대 문구를 돌려준다."""
    from clearing_hub.core.database import SessionLocal
    from clearing_hub.domains.banks import service as bank_service
    from clearing_hub.domains.banks.router import invite_message

    async with SessionLocal() as db, db.begin():
        invite = await bank_service.create_invite(db, note)
    return invite_message(invite.code, invite.expires_at, hub_url)


def banner(environ: MutableMapping[str, str], invite: str, hub_port: int, sim_port: int) -> str:
    """시작이 끝나면 콘솔에 출력할 안내. 로컬 전용이라 관리자 토큰도 보여 준다."""
    rule = "=" * 72
    return "\n".join([
        rule,
        "ClearingHub 로컬 개발 서버 (운영과 다름: SQLite·create_all, 정산 배치·알림 꺼짐, http base_url 허용)",
        f"- 허브:       http://{HOST}:{hub_port}   (API 문서 http://{HOST}:{hub_port}/docs)",
        f"- 가짜 은행:  http://{HOST}:{sim_port}/interbank  (902 SimBank, 받을 계좌 110-123-0001 = 김철수)",
        f"- DB:         {environ['DATABASE_URL']}",
        f"- 관리자 토큰 (X-Admin-Token): {environ['ADMIN_TOKEN']}",
        "- 내 은행 base_url 예: http://127.0.0.1:3000/interbank  (로컬만. 운영은 공개 HTTPS, SPEC 3.5)",
        "- 새 초대 코드: curl -X POST http://{0}:{1}/admin/invites -H \"X-Admin-Token: <토큰>\"".format(HOST, hub_port),
        rule,
        invite,
        rule,
        "종료: Ctrl+C",
    ])


# ---- 서버 두 개 ----

def _server(app, port: int):
    import uvicorn

    class Server(uvicorn.Server):
        @contextmanager
        def capture_signals(self):  # 두 서버가 시그널 처리기를 서로 덮어쓰지 않게, Ctrl+C는 serve()에서 한 번에 받는다
            yield

    return Server(uvicorn.Config(app, host=HOST, port=port, log_config=None, lifespan="on"))


async def serve(hub_port: int = HUB_PORT, sim_port: int = SIM_PORT) -> None:
    from bank_sim.main import app as sim_app
    from clearing_hub.main import app as hub_app

    hub_url, sim_base_url = f"http://{HOST}:{hub_port}", f"http://{HOST}:{sim_port}/interbank"
    logger.warning("로컬 개발 서버: 테이블을 Alembic이 아니라 create_all로 만든다 (운영과 다름). 정산 배치·알림은 꺼져 있다")
    await create_tables()
    if await ensure_sim_bank(public_key_of(os.environ["SIM_SIGNING_KEY"]), sim_base_url):
        logger.info("가짜 은행 902를 등록하고 예치금 %s원을 넣었다", f"{SIM_DEPOSIT:,}")
    invite = await new_invite_message(hub_url)

    servers = [_server(hub_app, hub_port), _server(sim_app, sim_port)]

    def stop(*_):
        for server in servers:
            server.should_exit = True

    previous = signal.signal(signal.SIGINT, stop)
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        while not all(server.started for server in servers):
            if any(task.done() for task in tasks):  # 포트가 이미 쓰이는 등으로 시작 실패
                stop()
                break
            await asyncio.sleep(0.1)
        else:
            print(banner(os.environ, invite, hub_port, sim_port), flush=True)
        await asyncio.gather(*tasks)
    finally:
        stop()  # 출력 중 오류 등으로 빠져나와도 두 서버를 정상 종료(lifespan shutdown)한다
        await asyncio.gather(*tasks, return_exceptions=True)
        signal.signal(signal.SIGINT, previous)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="허브와 가짜 은행 902를 로컬에서 함께 띄운다 (개발 전용)")
    parser.add_argument("--hub-port", type=int, default=HUB_PORT)
    parser.add_argument("--sim-port", type=int, default=SIM_PORT)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows에서 파일로 돌리면 cp949라 한글·기호 출력이 깨지거나 죽는다
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    apply_env(ensure_dev_env(), hub_port=args.hub_port)  # clearing_hub 설정을 읽기 전에
    from clearing_hub.core.log_setup import configure_logging

    configure_logging()
    asyncio.run(serve(args.hub_port, args.sim_port))


if __name__ == "__main__":
    main()

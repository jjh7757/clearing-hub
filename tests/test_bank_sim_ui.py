"""SimBank 시연 화면 (SIMBANK_PLAN 3단계): `GET /` HTML, `/sim/static/*`, 상태 JSON `GET /health`.

- 운영 헬스체크(compose)는 `GET /`의 200만 본다 → HTML이어도 200
- 화면은 허브 관리자 화면과 같은 보안 헤더 (CSP self, 인라인 스크립트 없음)
- `/interbank/*` 응답 서명 미들웨어는 화면 경로에 붙지 않는다
"""

import pytest
from httpx import ASGITransport, AsyncClient

import bank_sim.main as sim
from clearing_hub.admin_ui.router import SECURITY_HEADERS

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sim_client():
    sim.received.clear()
    async with AsyncClient(transport=ASGITransport(app=sim.app), base_url="http://simbank") as ac:
        yield ac


def _assert_security_headers(res) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert res.headers[name] == value, name
    assert "'unsafe-inline'" not in res.headers["Content-Security-Policy"]


def _assert_unsigned(res) -> None:
    for name in ("X-Response-Signature", "X-Response-Timestamp"):
        assert name not in res.headers, name


async def test_root_serves_page(sim_client):
    res = await sim_client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    _assert_security_headers(res)
    _assert_unsigned(res)
    html = res.text
    assert "SimBank" in html
    assert '<script src="/sim/static/app.js" defer></script>' in html
    assert 'href="/sim/static/app.css"' in html
    assert "<script>" not in html  # 인라인 스크립트 없음 (CSP script-src 'self')
    for element_id in ("login", "disabled", "customer-tabs", "accounts", "transactions", "send-form", "to-bank",
                       "to-account", "inquiry", "amount", "memo", "send"):
        assert f'id="{element_id}"' in html, element_id
    assert (await sim_client.head("/")).status_code == 200


@pytest.mark.parametrize(("name", "media"), [("app.js", "text/javascript"), ("app.css", "text/css")])
async def test_static_files(sim_client, name, media):
    res = await sim_client.get(f"/sim/static/{name}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith(media)
    _assert_security_headers(res)
    _assert_unsigned(res)
    assert res.content == (sim.STATIC / name).read_bytes()


@pytest.mark.parametrize("name", ["index.html", "nope.js", "..%2Fmain.py", "%2E%2E%2Fstore.py"])
async def test_static_only_known_files(sim_client, name):
    res = await sim_client.get(f"/sim/static/{name}")
    assert res.status_code == 404


async def test_script_uses_api_and_text_only(sim_client):
    """화면 스크립트는 /sim/api만 부르고, 바깥 문자열을 innerHTML로 그리지 않는다 (CSP connect-src 'self')."""
    script = (await sim_client.get("/sim/static/app.js")).text
    for path in ("/sim/api/customers", "/sim/api/accounts/", "/sim/api/banks", "/sim/api/inquiry",
                 "/sim/api/transfers", "/refresh", "X-Sim-Password", "sessionStorage"):
        assert path in script, path
    assert "innerHTML" not in script
    assert "http://" not in script and "https://" not in script  # 외부 연결 없음


async def test_health_json(sim_client):
    res = await sim_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"service": "BankSim", "received": 0}
    _assert_unsigned(res)


async def test_interbank_still_signed(sim_client):
    """화면을 붙여도 `/interbank/*`의 응답 서명(SPEC 2.5)은 그대로다 (대조군)."""
    res = await sim_client.post("/interbank/inquiry", json={"account_num": "110-123-0001"})
    assert res.status_code in (200, 401)  # 허브 서명이 없으면 401이어도 서명은 붙는다 (4xx도 서명)
    assert "X-Response-Signature" in res.headers


async def test_page_needs_no_password(sim_client, monkeypatch):
    """페이지에는 데이터가 없어 비밀번호 없이 준다. 데이터 API는 그대로 비밀번호가 필요하다."""
    monkeypatch.setenv("SIM_UI_PASSWORD", "pw-1234")
    assert (await sim_client.get("/")).status_code == 200
    assert (await sim_client.get("/sim/api/customers")).status_code == 401
    monkeypatch.setenv("SIM_UI_PASSWORD", "")
    assert (await sim_client.get("/")).status_code == 200
    res = await sim_client.get("/sim/api/customers", headers={"X-Sim-Password": "x"})
    assert (res.status_code, res.json()["error"]) == (403, "FORBIDDEN")


async def test_devserver_wires_simbank_and_peer(client):
    """devserver가 화면용으로 하는 일: 903 PeerBank 등록(902와 같은 함수)과 API 키 재발급 → 그 키로 허브를 부를 수 있다."""
    from clearing_hub import devserver
    from clearing_hub.core.signing import generate_keypair, public_key_of, sign_ed25519
    from tests.conftest import ADMIN

    peer_key = generate_keypair()[0]
    assert await devserver.ensure_sim_bank(public_key_of(peer_key), "http://127.0.0.1:8111/interbank",
                                           devserver.PEER_BANK_CODE, devserver.PEER_BANK_NAME) is True
    banks = {b["bank_code"]: b for b in (await client.get("/admin/banks", headers=ADMIN)).json()}
    assert banks["903"]["name"] == "PeerBank"

    api_key = await devserver.new_api_key("903")
    headers = {"X-Bank-Code": "903", "X-Api-Key": api_key, **sign_ed25519(peer_key, "GET", "/v1/banks", b"")}
    res = await client.get("/v1/banks", headers=headers)
    assert res.status_code == 200, res.text
    assert "903" in [b["bank_code"] for b in res.json()["banks"]]

"""관리자 정산 화면: `GET /admin/ui/settlements` (HTML)와 그 스크립트·스타일.

페이지 자체에는 데이터·토큰이 없어서 인증 없이 준다. 관리자가 페이지에 토큰을 넣으면 브라우저가 그 토큰을
`X-Admin-Token`으로 붙여 기존 관리자 API(`/admin/settlements`, `/admin/banks`)를 부른다. 토큰은 그 탭의
sessionStorage에만 둔다 (탭을 닫으면 지워진다). 서버는 토큰을 페이지에 넣지 않는다.

은행이 보낸 dispute 메시지 같은 바깥 문자열을 화면에 그리므로, 스크립트는 textContent로만 그리고(innerHTML 안 씀)
CSP로 인라인 스크립트·외부 연결을 막는다.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC = Path(__file__).parent / "static"

router = APIRouter(prefix="/admin/ui", tags=["admin-ui"], include_in_schema=False)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-cache",  # 배포하면 바로 새 스크립트를 받게
}


def _file(name: str, media_type: str) -> FileResponse:
    return FileResponse(STATIC / name, media_type=media_type, headers=SECURITY_HEADERS)


@router.get("/settlements")
async def settlements_page():
    return _file("settlements.html", "text/html; charset=utf-8")


@router.get("/settlements.js")
async def settlements_script():
    return _file("settlements.js", "text/javascript; charset=utf-8")


@router.get("/settlements.css")
async def settlements_style():
    return _file("settlements.css", "text/css; charset=utf-8")

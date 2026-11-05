"""SPEC 9.1 가상 CI 참고 구현. 허브는 CI를 계산하지 않는다 (은행이 계산해서 보낸다). 가짜 은행과 테스트가 쓴다.

material = 이름 + "|" + 생년월일(YYYYMMDD) + "|" + 휴대폰번호(숫자만)
ci       = hex(HMAC-SHA256(CI_KEY, material))
"""

import hashlib
import hmac
import unicodedata


def virtual_ci(ci_key: str, name: str, birth: str, phone: str) -> str:
    name = unicodedata.normalize("NFC", "".join(name.split()))
    phone = "".join(ch for ch in phone if ch.isdigit())
    material = f"{name}|{birth}|{phone}"
    return hmac.new(ci_key.encode(), material.encode(), hashlib.sha256).hexdigest()

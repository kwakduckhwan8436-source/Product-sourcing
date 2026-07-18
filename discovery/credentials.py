"""
discovery.credentials
=====================
네이버 키 자동 로그인 (주식 API config 방식).

[설계] 주식 자동매매에서 키를 config 에 한 번 등록해두고 앱 켜면 자동
로그인하던 방식을 네이버에 적용. 단, 네이버 검색 API 는 OAuth 토큰이
없고 매 요청에 ID/Secret 을 헤더로 싣는 방식이라 '토큰 발급'은 없다.
대신 키를 안전한 위치에 저장해두고 앱 시작 시 자동으로 불러온다.

[저장 위치] 우선순위:
  1. naver_credentials.bat (기존 사용자 파일 — set NAVER_CLIENT_ID=...)
  2. ~/.sourcing_tool/credentials.json (앱 전용 저장소)
환경변수(NAVER_CLIENT_ID/SECRET)가 있으면 그것도 인식.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_APP_DIR = Path.home() / ".sourcing_tool"
_CRED_FILE = _APP_DIR / "credentials.json"
_BAT_FILE = Path("naver_credentials.bat")


def _from_bat(path: Path) -> tuple[str, str] | None:
    """naver_credentials.bat 에서 키 추출 (기존 사용자 파일 호환)."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    cid = re.search(r"set\s+NAVER_CLIENT_ID=(.*)", text, re.IGNORECASE)
    sec = re.search(r"set\s+NAVER_CLIENT_SECRET=(.*)", text, re.IGNORECASE)
    if cid and sec:
        return cid.group(1).strip(), sec.group(1).strip()
    return None


def _from_json(path: Path) -> tuple[str, str] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cid = (data.get("client_id") or "").strip()
        sec = (data.get("client_secret") or "").strip()
        if cid and sec:
            return cid, sec
    except Exception:  # noqa: BLE001
        pass
    return None


def load_credentials() -> tuple[str, str] | None:
    """
    저장된 키를 자동으로 불러온다 (화면 노출 없이).
    우선순위: 환경변수 -> bat 파일 -> 앱 전용 json.
    없으면 None (사용자가 직접 입력해야 함).
    """
    env_id = os.environ.get("NAVER_CLIENT_ID", "").strip()
    env_sec = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if env_id and env_sec:
        return env_id, env_sec

    for loader, path in ((_from_bat, _BAT_FILE), (_from_json, _CRED_FILE)):
        result = loader(path)
        if result:
            return result
    return None


def save_credentials(client_id: str, client_secret: str) -> None:
    """키를 앱 전용 저장소에 저장 (다음 실행 때 자동 로그인)."""
    _APP_DIR.mkdir(parents=True, exist_ok=True)
    _CRED_FILE.write_text(
        json.dumps({"client_id": client_id.strip(),
                    "client_secret": client_secret.strip()},
                   ensure_ascii=False),
        encoding="utf-8")
    # 파일 권한 제한 (소유자만 읽기/쓰기) — 키 보호
    try:
        os.chmod(_CRED_FILE, 0o600)
    except Exception:  # noqa: BLE001
        pass


def has_saved() -> bool:
    return load_credentials() is not None

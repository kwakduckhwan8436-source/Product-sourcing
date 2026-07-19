"""
discovery.providers.naver_client
=================================
네이버 오픈 API 공통 클라이언트.

인증: HTTP 헤더 X-Naver-Client-Id / X-Naver-Client-Secret
- 검색(쇼핑): GET  https://openapi.naver.com/v1/search/shop.json
- 데이터랩 쇼핑인사이트: POST https://openapi.naver.com/v1/datalab/shopping/...
일일 한도: 25,000회. 단, 초당 요청 수 제한이 있어 속도 조절 필수.

401/429 처리:
- 401: 키 오타/공백 또는 '검색' API 미설정. 응답 본문의 errorCode 를 읽어 원인 표시.
- 429: 초당 한도 초과. 지수 백오프로 자동 재시도.
- 키는 생성 시 strip() 으로 앞뒤 공백/줄바꿈 제거.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SEARCH_SHOP = "https://openapi.naver.com/v1/search/shop.json"
_DATALAB_KEYWORDS = "https://openapi.naver.com/v1/datalab/shopping/category/keywords"


# ── 프로세스 전역 방어 (여러 사용자 → 하나의 서버 IP) ────────────────
# 사용자마다 NaverClient 를 새로 만들어도, 서버 IP 하나로 나가는 '총량' 은
# 아래 전역 락/세마포어로 묶는다. 인스턴스별 throttle 만으로는 여러 명이
# 동시에 몰릴 때 IP 가 폭주해 네이버에 차단당한다 — 그걸 막는 핵심 장치.
_GLOBAL_MIN_INTERVAL = 0.10       # 서버 전체 아웃바운드 최소 간격(초)
_GLOBAL_MAX_CONCURRENT = 6        # 동시 아웃바운드 요청 상한
_global_lock = asyncio.Lock()
_global_last = 0.0
_global_sem = asyncio.Semaphore(_GLOBAL_MAX_CONCURRENT)


async def _global_throttle() -> None:
    """서버 전체에서 호출 사이 최소 간격을 보장 (인스턴스 수와 무관).
    약간의 지터를 섞어 기계적인 등간격 요청으로 보이지 않게 한다."""
    global _global_last
    import random as _rnd
    loop = asyncio.get_event_loop()
    async with _global_lock:
        now = loop.time()
        gap = _GLOBAL_MIN_INTERVAL + _rnd.uniform(0.0, 0.05)
        wait = gap - (now - _global_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _global_last = loop.time()


class NaverAuthError(Exception):
    """401 — 키 문제 또는 검색 API 미설정. 발굴 전체를 중단시키는 치명 오류."""


class NaverRateLimited(Exception):
    """429 — 재시도 후에도 계속 막힘."""


class NaverClient:
    """검색 + 데이터랩 공용. async with 권장.

    min_interval: 호출 사이 최소 간격(초). 초당 한도 회피용.
    max_retries : 429 발생 시 재시도 횟수.
    """

    def __init__(self, client_id: str, client_secret: str, timeout: float = 15.0,
                 min_interval: float = 0.12, max_retries: int = 4,
                 datalab_interval: float = 0.8, datalab_max_retries: int = 7):
        # 앞뒤 공백/줄바꿈 제거 — 401 의 흔한 원인 차단
        self._headers = {
            "X-Naver-Client-Id": (client_id or "").strip(),
            "X-Naver-Client-Secret": (client_secret or "").strip(),
        }
        self._client = httpx.AsyncClient(timeout=timeout)
        self.call_count = 0
        self._min_interval = min_interval
        self._max_retries = max_retries
        # 데이터랩 전용 정책: 한도가 검색보다 훨씬 좁아 더 느리고 끈질기게
        self._datalab_interval = datalab_interval
        self._datalab_max_retries = datalab_max_retries
        # 호출 직렬화용 락 + 마지막 호출 시각 (초당 한도 회피)
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def __aenter__(self) -> "NaverClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self, interval: float | None = None) -> None:
        """직전 호출과 최소 간격을 보장 (초당 한도 회피)."""
        gap = self._min_interval if interval is None else interval
        loop = asyncio.get_event_loop()
        async with self._lock:
            now = loop.time()
            wait = gap - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = loop.time()

    @staticmethod
    def _explain_401(resp: httpx.Response) -> str:
        """401 응답 본문에서 네이버 errorCode 를 읽어 사람이 읽을 원인으로."""
        try:
            body = resp.json()
            code = body.get("errorCode") or body.get("errorMessage", "")
            msg = body.get("errorMessage", "")
        except Exception:  # noqa: BLE001
            code, msg = "", resp.text[:200]
        hint = ""
        c = str(code)
        if c in ("024", "028") or "Authentication failed" in msg:
            hint = " → Client ID/Secret 오타 또는 공백 의심"
        elif c in ("101", "012") or "Not Exist" in msg or "허용되지" in msg:
            hint = " → 이 앱에 '검색' API 가 설정되지 않았을 가능성 (개발자센터 > 내 애플리케이션 > API 설정에서 '검색' 체크)"
        return f"errorCode={code}, msg={msg}{hint}"

    async def _request(self, method: str, url: str, *, params=None,
                       json=None, interval: float | None = None,
                       max_retries: int | None = None,
                       backoff_base: float = 0.5) -> dict[str, Any]:
        """공통 요청: throttle -> 호출 -> 401치명/429재시도 처리.
        interval/max_retries 로 호출 종류별 정책 주입(데이터랩은 더 느리고 끈질기게)."""
        headers = dict(self._headers)
        if json is not None:
            headers["Content-Type"] = "application/json"
        retries = self._max_retries if max_retries is None else max_retries

        attempt = 0
        while True:
            await self._throttle(interval)      # 이 사용자 호출 간격
            await _global_throttle()            # 서버 전체 속도 (여러 명 합산)
            self.call_count += 1
            async with _global_sem:             # 서버 전체 동시 요청 상한
                resp = await self._client.request(method, url, params=params,
                                                  json=json, headers=headers)
            if resp.status_code == 401:
                # 키/권한 문제 — 재시도 무의미, 즉시 치명 오류로 중단
                raise NaverAuthError(self._explain_401(resp))
            if resp.status_code == 429:
                attempt += 1
                if attempt > retries:
                    raise NaverRateLimited(
                        f"429 재시도 {retries}회 초과 (속도를 더 낮추세요)")
                # 지수 백오프 (상한 30초). 데이터랩은 base 를 크게 줘서 더 길게 쉼.
                backoff = min(backoff_base * (2 ** (attempt - 1)), 30.0)
                logger.warning("429 — %.1f초 후 재시도 (%d/%d)",
                               backoff, attempt, retries)
                await asyncio.sleep(backoff)
                continue
            resp.raise_for_status()
            return resp.json()

    async def search_shop(self, query: str, display: int = 100,
                          start: int = 1, sort: str = "sim") -> dict[str, Any]:
        """쇼핑 검색. sort: sim/date/asc/dsc. display<=100, start<=1000."""
        params = {"query": query, "display": min(display, 100),
                  "start": start, "sort": sort}
        return await self._request("GET", _SEARCH_SHOP, params=params)

    async def datalab_shopping_keywords(self, body: dict[str, Any]) -> dict[str, Any]:
        """쇼핑인사이트 키워드 트렌드 (POST, JSON body).
        데이터랩은 한도가 검색보다 훨씬 좁아 더 느리고 끈질긴 정책 적용."""
        return await self._request(
            "POST", _DATALAB_KEYWORDS, json=body,
            interval=self._datalab_interval,
            max_retries=self._datalab_max_retries,
            backoff_base=1.0)

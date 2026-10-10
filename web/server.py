"""
web.server — 위탁판매 소싱 도우미 (FastAPI)

[무기]
  ① 경쟁 실체   : 숫자가 아니라 '누가' 파는지 (카탈로그/대형몰/개인) — 추가 호출 0
  ② 시즌 선행   : 12개월 시계열로 '몇 주 뒤 오르는지' → 위탁의 구조적 우위
  ③ 키워드 선점 : 축(용도·대상·속성·상황) 조합으로 빈 진입로 발굴
  ④ 마진 역산   : "얼마 이하로 떼야 남는가" → 소싱 협상 기준

[원칙] 네이버 열쇠는 서버가 보관하지 않음. 크롤링 없음. 공식 API만.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from discovery.beginner import friendly_error  # noqa: E402
from discovery.competition import analyze_competition  # noqa: E402
from urllib.parse import quote  # noqa: E402

from discovery.keyword_entry import (coupang_link, grade_entry,  # noqa: E402
                                     naver_link)
from discovery.keyword_forge import ForgedKeyword, forge, summarize  # noqa: E402
from discovery.season import analyze_season  # noqa: E402
from discovery.calendar import build_calendar  # noqa: E402
from discovery.listing import build_listing  # noqa: E402
from discovery.segments import build_report  # noqa: E402
from discovery.title_mining import mine_titles  # noqa: E402
from discovery.tracker import (api_bump, api_usage,  # noqa: E402
                               backtest_log, backtest_pending,
                               calib_get, calib_set_from_hitrate,
                               feedback_map, feedback_set,
                               grade_verdicts, hit_rate, movement_of,
                               pool_stats, record, say_verdict,
                               tracked_keywords, watch_add,
                               watch_list, watch_remove)

_HERE = Path(__file__).resolve().parent
_SCAN_TIMEOUT = 70.0   # 한 요청이 이보다 오래 붙들면 브라우저가 끊는다
APP_VERSION = "v193"   # 화면에 찍어서 '예전 서버가 도는지' 눈으로 알게 한다

# ── 실시간 접속자 (인메모리) ──────────────────────────────────
# 무료 플랜은 재시작/슬립 때 이 값이 초기화됩니다(누적=오늘 기준으로 취급).
import time as _time  # noqa: E402
_PRESENCE: dict[str, float] = {}      # visitor_id -> last_seen(ts)
_PRESENCE_WINDOW = 75.0                # 이 초 안에 하트비트가 있으면 '접속 중'
_SEEN_TODAY: dict[str, float] = {}     # visitor_id -> 첫 방문(ts), 누적 고유수 산정
_DAY_ANCHOR = {"day": _time.gmtime().tm_yday}
_PRESENCE_MAX = 20000                  # 메모리 폭주 방지 상한

# ── 발굴 결과 캐시 (여러 사용자 → 네이버 호출 절감) ─────────────
_AUTO_CACHE: dict = {}                 # key -> (ts, response_dict)
_AUTO_CACHE_TTL = 1800.0               # 30분 — 같은 분야 재스캔은 캐시로
_AUTO_CACHE_MAX = 400                  # 캐시 항목 상한

# ── 이용코드 게이트 (카페 등급 회원 전용) ───────────────────────
# Render 환경변수 SOURCING_CODES 에 쉼표로 코드들을 넣으면 그 코드가 있어야
# '팔 만한 자리 찾기' 를 쓸 수 있다. 비워두면 게이트 꺼짐(누구나 사용).
# 등급 제한 게시판에만 코드를 올리면 = 그 등급만 코드를 얻는다.
def _load_codes() -> set:
    raw = os.environ.get("SOURCING_CODES", "")
    return {c.strip() for c in raw.split(",") if c.strip()}

_ACCESS_CODES = _load_codes()

# ── 남용 방어: IP별 요청 제한 + 429 쿨다운 ─────────────────────
_IP_HITS: dict = {}                    # ip -> [최근 스캔 타임스탬프]
_IP_WINDOW = 300.0                     # 5분 창
_IP_MAX = 12                           # 5분에 스캔 12회까지 (한 명이 도배 못 함)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _ip_ok(ip: str) -> bool:
    now = _time.time()
    hits = [t for t in _IP_HITS.get(ip, []) if now - t < _IP_WINDOW]
    if len(hits) >= _IP_MAX:
        _IP_HITS[ip] = hits
        return False
    hits.append(now)
    _IP_HITS[ip] = hits
    if len(_IP_HITS) > 5000:           # 메모리 방어
        for k in list(_IP_HITS)[:1000]:
            _IP_HITS.pop(k, None)
    return True

app = FastAPI(title="위탁판매 소싱 작업대")


# ── API 실보호 게이트 ────────────────────────────────────────────────
# 화면만 가리면 누가 /api 를 직접 부를 수 있으니, 코드가 설정돼 있으면
# 모든 /api 요청에 유효한 이용코드 헤더(x-access-code)를 요구한다.
# (게이트 확인·정적·루트는 예외 — 로그인 화면과 코드 확인은 열려 있어야 함)
@app.middleware("http")
async def _api_access_gate(request: Request, call_next):
    path = request.url.path
    if _ACCESS_CODES and path.startswith("/api"):
        _open = path.startswith("/api/gate") or path in ("/api/version", "/api/presence")
        if not _open:
            code = (request.headers.get("x-access-code", "") or "").strip()
            if code not in _ACCESS_CODES:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    {"ok": False, "need_auth": True,
                     "error": "이용코드 인증이 필요해요(카페 등급 게시판에서 확인)."},
                    status_code=401)
    return await call_next(request)
# ─────────────────────────────────────────────────────────────────────

_FEE_PCT = 0.25
_MAX_FORGE = 10


def _as_list(v) -> list:
    """
    화면에서 이상한 게 와도 422 로 죽지 않게.
    (버튼 리스너 실수로 마우스 이벤트가 넘어와 서버가 422 를 뱉은 적이 있다.
     사용자 잘못이 아닌데 화면이 먹통이 되는 건 나쁘다 — 조용히 무시한다.)
    """
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int)) and str(x).strip()]
    return []


def _cat_map() -> dict:
    try:
        p = _HERE.parent / "discovery" / "data" / "category_seeds.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        return {c["name"]: c["cat_id"] for c in d.get("categories", [])}
    except Exception:  # noqa: BLE001
        return {}


_CATS = _cat_map()


class CheckReq(BaseModel):
    client_id: str
    client_secret: str
    keyword: str
    cost: int = 0
    target_margin: int = 0      # 0 = 상품값에 비례 (자동)


def _titles_for(keyword: str, base: str) -> list:
    parts, seen = [], set()
    for tok in f"{keyword} {base}".split():
        if tok and tok not in seen:
            seen.add(tok)
            parts.append(tok)
    out = [" ".join(parts)[:50]]
    if len(parts) > 2:
        out.append(" ".join([parts[0]] + parts[1:3])[:50])
    return [t for t in dict.fromkeys(out) if len(t) >= 4]


def _reverse_cost(low: int, target: int) -> int:
    """최저가에서 target 원 남기려면 얼마 이하로 떼야 하나."""
    if low <= 0:
        return 0
    return max(0, int(low - low * _FEE_PCT - target))


def _verdict(total: int, low: int, cost: int, kw: str, comp) -> dict:
    margin = int(low - cost - low * _FEE_PCT) if (cost > 0 and low > 0) else None
    if margin is None:
        profit = "떼오는 값을 넣으면 남는 돈을 계산해드려요"
    else:
        profit = (f"최저가에 맞춰 팔면 한 개당 약 {abs(margin):,}원 "
                  f"{'남아요' if margin > 0 else '손해예요'}")

    if total <= 0:
        return {"light": "🟡", "headline": "아직 판단하기 일러요", "profit": profit,
                "advice": "파는 사람이 없어요 — 기회일 수도, 안 팔리는 물건일 수도",
                "margin": margin}
    if margin is not None and margin <= 0:
        return {"light": "🔴", "headline": "이건 팔면 손해예요", "profit": profit,
                "advice": "떼오는 값을 더 낮추거나 다른 상품을 보세요", "margin": margin}
    if comp is not None and not comp.can_enter:
        return {"light": "🔴", "headline": "숫자는 적지만 못 뚫어요", "profit": profit,
                "advice": comp.note, "margin": margin}
    if total > 30000:
        return {"light": "🔴", "headline": "경쟁이 너무 세요", "profit": profit,
                "advice": f"이미 {total:,}개 — 광고 없이는 안 보여요", "margin": margin}
    if margin is not None and margin < 1500:
        return {"light": "🟡", "headline": "남는 게 너무 적어요", "profit": profit,
                "advice": f"{margin:,}원 벌자고 하기엔 손이 많이 가요", "margin": margin}
    if total > 5000 or (comp is not None and comp.grade == "좁음"):
        return {"light": "🟡", "headline": "팔리긴 하는데 자리가 좁아요", "profit": profit,
                "advice": (comp.note if comp else f"'{kw}' 처럼 구체적이어야 보여요"),
                "margin": margin}
    return {"light": "🟢", "headline": "팔아도 됩니다", "profit": profit,
            "advice": f"'{kw}' 로 올리면 비집고 들어갈 수 있어요", "margin": margin}


@app.post("/api/check")
async def check(req: CheckReq):
    from discovery.providers.naver_client import NaverClient
    from discovery.providers.naver_demand import NaverDemandProvider
    from discovery.providers.naver_shop import NaverShopProvider

    base = re.sub(r"\s+", " ", req.keyword).strip()
    if not base:
        return {"ok": False, "error": "팔고 싶은 물건 이름을 넣어주세요."}

    combos = forge(base)[:_MAX_FORGE]
    forged = []
    markets = {}
    cat_name = ""
    season = None

    try:
        async with NaverClient(req.client_id, req.client_secret) as client:
            shop = NaverShopProvider(client)
            fails = 0
            for kw, axis in combos:
                try:
                    m = await shop.market_of(kw)
                    fails = 0
                except Exception as exc:  # noqa: BLE001
                    if "RateLimited" in type(exc).__name__ or "429" in str(exc):
                        fails += 1
                        if fails >= 3:
                            raise
                    continue
                lp = sorted(m.lprices) if m.lprices else []
                grade, _note = grade_entry(m.total)
                f = ForgedKeyword(keyword=kw, axis=axis, total=m.total,
                                  price_min=lp[0] if lp else 0, grade=grade,
                                  is_ghost=(m.total <= 0),
                                  is_open=grade in ("진입쉬움", "보통"))
                forged.append(f)
                markets[kw] = m
                if not cat_name and m.category_path:
                    cat_name = m.category_path[0]

            if not forged:
                return {"ok": False,
                        "error": "네이버에서 아무것도 찾지 못했어요. 다른 말로 해보세요."}

            res = summarize(base, forged)
            alive = [f for f in forged if not f.is_ghost]
            pick = res.open_ones[0] if res.open_ones else (
                sorted(alive, key=lambda x: x.total)[0] if alive else forged[0])

            # 시장 최저가 — 네이버가 계산해 둔 카탈로그 최저가 (추가 호출 0)
            from discovery.market_price import market_price_of
            pm = markets.get(pick.keyword)
            mprice = market_price_of(pm, core=base) if pm else None
            if mprice and mprice.lowest > 0:
                pick.price_min = mprice.lowest

            cat_id = _CATS.get(cat_name, "")
            if cat_id:
                try:
                    demand = NaverDemandProvider(client)
                    trend = await demand.trend_of(pick.keyword, cat_id)
                    if trend and trend.points:
                        # 데이터랩이 준 실제 날짜를 넘김 — 월 추측 제거
                        season = analyze_season(
                            trend.points, periods=getattr(trend, "periods", None))
                except Exception:  # noqa: BLE001
                    season = None
            calls = client.call_count
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(f"{type(exc).__name__}: {exc}")}

    # A/C — 상위 노출 제목을 채굴해 상품명·상세·세트 뼈대 생성
    pm = markets.get(pick.keyword)
    mined = mine_titles(getattr(pm, "sample_titles", []) or [], base=base)
    listing = build_listing(pick.keyword, base, mined, pick.price_min)

    comp = analyze_competition(markets.get(pick.keyword)) if markets else None
    v = _verdict(pick.total, pick.price_min, req.cost, pick.keyword, comp)
    need = _reverse_cost(pick.price_min, req.target_margin)

    # ②③ 방향 추적 — 이번 상태를 남기고, 지난번과 비교
    try:
        demand_level = 0.0
        if season is not None and getattr(season, "peak_ratio", 0):
            demand_level = float(season.peak_ratio)
        record(pick.keyword, pick.total, pick.price_min, demand_level)
        mv = movement_of(pick.keyword)
    except Exception:  # noqa: BLE001
        mv = None

    return {
        "ok": True, "calls": calls, "category": cat_name,
        "best": {"keyword": pick.keyword, "axis": pick.axis, "total": pick.total,
                 "price_min": pick.price_min,
                 "price_basis": (mprice.basis if mprice else ""),
                 "price_note": (mprice.note if mprice else ""),
                 "price_trusted": (mprice.trusted if mprice else False),
                 "naver": naver_link(pick.keyword),
                 "coupang": coupang_link(pick.keyword)},
        "verdict": v,
        "competition": (None if comp is None else {
            "grade": comp.grade, "note": comp.note, "can_enter": comp.can_enter,
            "catalog_pct": comp.catalog_pct, "bigmall_pct": comp.bigmall_pct,
            "indie_pct": comp.indie_pct, "top_mall": comp.top_mall,
            "top_share_pct": comp.top_share_pct, "warnings": comp.warnings,
            "basis": comp.basis, "agree_pct": comp.agree_pct}),
        "season": (None if season is None else {
            "has_season": season.has_season, "stage": season.stage,
            "note": season.note, "action": season.action,
            "peak_month": season.peak_month,
            "weeks_until_rise": season.weeks_until_rise}),
        "reverse": {"target": req.target_margin, "need_cost": need},
        "movement": (None if mv is None else {
            "stage": mv.stage, "note": mv.note, "action": mv.action,
            "points": mv.points, "days": mv.days, "golden": mv.golden,
            "demand_change": mv.demand_change, "total_change": mv.total_change,
            "price_change": mv.price_change}),
        "forged": [{"keyword": f.keyword, "axis": f.axis, "total": f.total,
                    "grade": f.grade, "is_ghost": f.is_ghost,
                    "is_open": f.is_open} for f in forged],
        "forge_note": res.note, "ghosts": res.ghosts,
        "titles": listing.titles or _titles_for(pick.keyword, base),
        "listing": {"why": listing.why, "must": listing.must_words,
                    "gaps": listing.gap_words,
                    "detail": listing.detail_lines,
                    "bundles": listing.bundle_ideas,
                    "sample": getattr(mined, "sample", 0)},
    }


class AutoReq(BaseModel):
    client_id: str
    client_secret: str
    category: str = ""
    budget: int = 320
    exclude: list = []          # 더 찾기 — 이미 본 것
    mode: str = "consign"       # consign(위탁) / wholesale(도매)
    fee_pct: float = 25.0        # 수수료+광고+세금 (본인 실제값)
    target_margin: int = 0      # 0 = 상품값에 비례해서 자동 (권장)
    # ↑ 3000 으로 두면 비례 계산을 덮어써 4,500원짜리에도 3,000원을 요구한다
    max_total: int = 50000       # 이 이상은 못 뚫는다 (기준선)
    open_total: int = 1500       # 이하면 '빈자리' (기준선)
    min_price: int = 8000        # 최저가 이하면 볼 것 없음 (기준선)
    access_code: str = ""        # 카페 등급 회원 이용코드 (게이트 켜졌을 때)


@app.get("/api/modes")
async def modes():
    """위탁/도매 — 기준이 다르므로 화면에서 고르게 한다."""
    from discovery.mode import RULES
    return {"modes": [{"key": r.key, "label": r.label, "note": r.note,
                       "cost_label": r.cost_label, "watch": list(r.watch),
                       "target_margin": r.target_margin,
                       "fee_pct": r.fee_pct,
                       "min_margin_rate": r.min_margin_rate, "moq": r.moq}
                      for r in RULES.values()]}


@app.get("/api/version")
async def version():
    """어느 버전이 도는지 — 포트 충돌로 예전 서버가 살아있으면 여기서 드러난다."""
    return {"version": APP_VERSION}


class KeyTestReq(BaseModel):
    client_id: str
    client_secret: str


@app.post("/api/hubtest")
async def hubtest(req: KeyTestReq):
    """새 NAVER API HUB 연결 점검 — 공식 명세로 확인된 검색 API(뉴스)를
    허브 주소·인증으로 호출해, 네 새 허브 키가 실제로 되는지 원클릭 확인.
      GET https://naverapihub.apigw.ntruss.com/search/v1/news
      헤더 X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY"""
    import httpx
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "msg": "Client ID / Secret 을 둘 다 넣어주세요."}
    url = "https://naverapihub.apigw.ntruss.com/search/v1/news"
    headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec}
    params = {"query": "테스트", "display": 1, "start": 1, "sort": "date", "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = await c.get(url, headers=headers, params=params)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": "network",
                "msg": f"허브에 못 닿았어요 — {type(e).__name__}: {str(e)[:140]}"}
    body = (r.text or "")[:240]
    if r.status_code == 200:
        return {"ok": True, "kind": "ok",
                "msg": "✅ 새 허브 연결 성공! 이 키로 검색 API가 됩니다 "
                       "(뉴스 검색 테스트 통과). 이제 이 방식으로 소싱기를 재편할 수 있어요."}
    if r.status_code == 401:
        return {"ok": False, "kind": "auth",
                "msg": "401 인증 실패 — 허브 키가 맞는지, 콘솔에서 이 앱에 '검색 API'가 "
                       f"선택됐는지 확인해주세요. (응답: {body})"}
    if r.status_code == 429:
        return {"ok": False, "kind": "quota",
                "msg": "429 한도/미선택 — 콘솔에서 이 앱에 검색 API가 선택돼 있는지, "
                       f"하루 한도를 넘지 않았는지 확인해주세요. (응답: {body})"}
    return {"ok": False, "kind": "http",
            "msg": f"{r.status_code} 응답 — {body}"}


_HUB_BASE = "https://naverapihub.apigw.ntruss.com"


async def _hub_search(cid: str, csec: str, kind: str, query: str,
                      display: int = 20, sort: str = "sim") -> dict:
    """NAVER API HUB 검색 API 호출 (공식 명세: /search/v1/{kind}).
    kind: kin(지식iN)·blog·cafearticle·news·webkr 등."""
    import httpx
    url = f"{_HUB_BASE}/search/v1/{kind}"
    headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec}
    params = {"query": query, "display": min(max(display, 1), 100),
              "start": 1, "sort": sort, "format": "json"}
    async with httpx.AsyncClient(timeout=12.0) as c:
        r = await c.get(url, headers=headers, params=params)
    try:
        api_bump(1)
    except Exception:  # noqa: BLE001
        pass
    if r.status_code == 401:
        raise NaverHubAuth("401 인증 실패 — 허브 키가 맞는지, 앱에 '검색 API'가 선택됐는지 확인해주세요.")
    if r.status_code == 429:
        raise NaverHubAuth("429 한도/미선택 — 검색 API 선택 여부와 하루 한도를 확인해주세요.")
    r.raise_for_status()
    return r.json()


class NaverHubAuth(Exception):
    pass


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


# 연관어 채굴에서 걸러낼 흔한 비(非)상품 단어
_STOP_WORDS = {
    "추천", "후기", "사용", "정리", "방법", "가격", "구매", "진짜", "정말", "하나",
    "그리고", "너무", "완전", "정도", "생각", "경우", "이번", "요즘", "오늘", "우리",
    "가능", "관련", "각종", "다양", "여기", "저기", "이것", "그것", "무엇", "때문",
    "네이버", "블로그", "지식", "질문", "답변", "제품", "상품", "브랜드", "쇼핑",
    "비교", "차이", "장점", "단점", "문의", "판매", "최고", "인기", "베스트",
}


_INTENT_WORDS = ("추천", "어디서", "어디", "가성비", "싸게", "구매", "살까", "사려",
                 "고르", "골라", "비교", "후기", "괜찮", "어떤", "뭐가", "vs", "차이")


async def _kin_signal(cid: str, csec: str, kw: str) -> tuple:
    """지식iN 질문 수(수요 폭) + 제목의 구매의도 밀도(살 마음)를 한 번에 잰다."""
    try:
        data = await _hub_search(cid, csec, "kin", kw, display=10, sort="sim")
    except Exception:  # noqa: BLE001
        return (-1, 0.0)
    total = int(data.get("total") or 0)
    blob = " ".join(_strip_tags(it.get("title", "")) for it in (data.get("items") or []))
    hits = sum(1 for w in _INTENT_WORDS if w in blob)
    intent = round(min(1.0, hits / 6.0), 2)
    return (total, intent)


async def _search_total(cid: str, csec: str, kind: str, query: str) -> int:
    """검색 API 로 그 키워드의 총 문서 수만 가볍게 얻는다(display=1).
    지식iN=질문 수(수요 폭), 블로그=글 수(화제성)."""
    try:
        data = await _hub_search(cid, csec, kind, query, display=1, sort="sim")
        return int(data.get("total") or 0)
    except Exception:  # noqa: BLE001
        return -1


def _market_of(rise: float, blog_total: int) -> list:
    """블로그 글 수(화제성)와 상승률로 시장 상태를 근사한다.
    글은 적은데 뜨는 = 블루오션(초기), 글 많은데 정체 = 레드오션(포화)."""
    if not blog_total or blog_total <= 0:
        return ["", ""]
    if rise >= 10 and blog_total < 30000:
        return ["blue", "블루오션 — 뜨는데 아직 덜 붐빔"]
    if rise < 5 and blog_total >= 120000:
        return ["red", "레드오션 — 포화·성장 정체(주의)"]
    return ["", ""]


def _grade_of(confidence: int, sustained: float, spike: bool, market: str,
              rollover: bool = False) -> str:
    """선별 등급 A/B/C — 신뢰도·지속성·시장상태·되돌림을 합쳐 '살 만한가'를 한 글자로.
    A급 신뢰도 하한은 백테스트 적중률로 자동 보정된다(calib)."""
    try:
        a_min = calib_get()
    except Exception:  # noqa: BLE001
        a_min = 75
    if market == "red" or spike or rollover:
        if confidence >= 60 and sustained >= 0.67 and not spike and not rollover:
            return "B"
        return "C"
    if confidence >= a_min and sustained >= 0.67:
        return "A"
    if confidence >= 50 and sustained >= 0.33:
        return "B"
    return "C"


def _confidence(it: dict, x_state: str, kin_total: int, blog_total: int,
                intent: float = 0.0) -> int:
    """신뢰도 0~100 — '실제로 갈리는' 신호 중심으로 변별력 있게 계산한다.
    (예전엔 데이터충분도·로그압축 항이 상수처럼 붙어 전부 비슷했다)"""
    rise = it.get("rise", 0.0)
    sus = it.get("sustained", 0.0)
    st = it.get("stability", 0.0)
    c = 0.0
    c += {"verified": 34.0, "both": 26.0, "search": 16.0, "click": 9.0}.get(x_state, 0.0)  # 교차검증 0~34
    c += sus * 20.0                                       # 지속성 0~20
    c += max(0.0, min(rise, 40.0)) / 40.0 * 22.0          # 상승 강도 0~22 (상승률 클수록)
    c += st * 15.0                                        # 안정성 0~15
    c += min(1.0, max(0.0, intent)) * 15.0               # 구매의도 0~15
    # 수요 규모는 소폭 가점(로그, 최대 6) — 지배적이지 않게
    import math
    if kin_total and kin_total > 0:
        c += min(6.0, math.log10(kin_total + 1) / 5.0 * 6.0)
    # 위험 신호 감점
    if it.get("spike"):
        c -= 12.0
    if it.get("rollover"):
        c -= 18.0
    if it.get("n", 12) < 8:                               # 데이터 부족하면만 감점(충분하면 상수 아님)
        c -= 10.0
    return round(max(0.0, min(100.0, c)))


async def _related_terms(cid: str, csec: str, base: str, limit: int = 8) -> list:
    """블로그·지식iN 검색 결과 제목/설명에서 base 와 함께 자주 나오는 한글 단어를
    빈도순으로 뽑아 '실제 연관어'를 만든다(형태소기 없이 가벼운 명사 근사)."""
    from collections import Counter
    cnt: Counter = Counter()
    for kind in ("blog", "kin"):
        try:
            data = await _hub_search(cid, csec, kind, base, display=30, sort="sim")
        except Exception:  # noqa: BLE001
            continue
        for it in (data.get("items") or []):
            text = _strip_tags(it.get("title", "")) + " " + _strip_tags(it.get("description", ""))
            for w in re.findall(r"[가-힣]{2,10}", text):
                if w == base or base in w or w in base:
                    continue
                if w in _STOP_WORDS or w.endswith(("합니다", "했어요", "하는", "해서", "니다")):
                    continue
                cnt[w] += 1
    return [w for w, c in cnt.most_common(limit) if c >= 3]


async def _hub_datalab(cid: str, csec: str, path: str, body: dict) -> dict:
    """NAVER API HUB Data Lab(쇼핑 인사이트·검색어 트렌드) POST 호출.
    공식 명세: POST /datalab/v1/... + JSON 바디, X-NCP-APIGW 헤더."""
    import httpx
    url = f"{_HUB_BASE}{path}"
    headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec,
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(url, headers=headers, json=body)
    try:
        api_bump(1)
    except Exception:  # noqa: BLE001
        pass
    if r.status_code == 401:
        raise NaverHubAuth("401 인증 실패 — 허브 키가 맞는지, 앱에 '쇼핑 인사이트' API가 "
                           "선택됐는지 확인해주세요.")
    if r.status_code == 429:
        raise NaverHubAuth("429 한도/미선택 — 쇼핑 인사이트 API 선택 여부와 월 한도를 확인해주세요.")
    if r.status_code >= 400:
        raise NaverHubAuth(f"{r.status_code}: {(r.text or '')[:220]}")
    return r.json()


async def _hub_datalab_probe(cid: str, csec: str, paths: list, body: dict) -> tuple:
    """경로가 공식문서로 확정 안 돼, 유력 후보들을 실키로 눌러 '되는 경로'를 찾는다.
    401/429 는 경로 문제가 아니므로 즉시 중단. 404/300(URL없음)이면 다음 후보로.
    성공 시 (응답, 맞은 경로) 반환."""
    import httpx
    headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec,
               "Content-Type": "application/json"}
    last = ""
    async with httpx.AsyncClient(timeout=15.0) as c:
        for p in paths:
            try:
                r = await c.post(f"{_HUB_BASE}{p}", headers=headers, json=body)
            except Exception as e:  # noqa: BLE001
                last = f"{p} → {type(e).__name__}"
                continue
            if r.status_code == 200:
                return r.json(), p
            if r.status_code in (401, 429):
                raise NaverHubAuth(
                    ("401 인증 실패 — 허브 키/‘쇼핑 인사이트’ API 선택 확인"
                     if r.status_code == 401
                     else "429 한도/미선택 — 쇼핑 인사이트 API 선택·월 한도 확인"))
            last = f"{p} → {r.status_code}: {(r.text or '')[:100]}"
    raise NaverHubAuth("쇼핑 인사이트 경로를 못 찾았어요(후보 모두 실패). 마지막 응답: " + last)


# 쇼핑 인사이트 후보 경로 — 되는 것 하나를 실키로 자동 탐지한다
_SHOP_KW_PATHS = [
    "/shopping/v1/category/keywords",
    "/shopping/v1/category/keyword",
]


def _trend_of(points: list) -> dict:
    """데이터랩 시계열(data:[{period,ratio}])에서 신호를 뽑는다.
    rise(상승률)·level(평균 수준)·stability(안정성)·momentum(최근 방향)까지 계산해
    위탁(급상승)·사입(꾸준+높음) 구분과 정밀도의 근거로 쓴다."""
    import statistics
    vals = []
    for p in (points or []):
        try:
            vals.append(float(p.get("ratio")))
        except (TypeError, ValueError):
            pass
    if len(vals) < 4:
        return {"latest": round(vals[-1], 1) if vals else 0.0, "rise": 0.0,
                "stage": "데이터부족", "series": [round(v, 1) for v in vals],
                "level": round(sum(vals) / len(vals), 1) if vals else 0.0,
                "stability": 0.0, "momentum": 0.0, "n": len(vals),
                "sustained": 0.0, "spike": False}
    recent = sum(vals[-3:]) / 3.0
    earlier = sum(vals[:-3]) / max(1, len(vals) - 3)
    rise = round((recent - earlier) / max(1.0, earlier) * 100, 1)
    peak = max(vals) or 1.0
    level = sum(vals) / len(vals)                       # 평균 수준(관심 크기 대용)
    mean_v = level or 1.0
    sd = statistics.pstdev(vals)
    cv = sd / mean_v if mean_v else 1.0                 # 변동계수
    stability = round(max(0.0, min(1.0, 1.0 - cv)), 2)  # 낮게 출렁일수록 1에 가까움
    momentum = round((vals[-1] - vals[-2]) / max(1.0, vals[-2]) * 100, 1)
    # 지속성: 최근 3개월 중 '이전 평균'을 넘는 달의 비율(0~1) — 꾸준한 상승일수록 1
    earlier_mean = sum(vals[:-3]) / max(1, len(vals) - 3)
    above = sum(1 for v in vals[-3:] if v > earlier_mean)
    sustained = round(above / 3.0, 2)
    # 반짝 급등: 마지막 달을 빼면 잠잠했는데(직전까지 상승률<10%) 마지막만 1.5배+ 튄 경우
    pre = vals[:-1]
    if len(pre) >= 4:
        pre_recent = sum(pre[-3:]) / 3.0
        pre_earlier = sum(pre[:-3]) / max(1, len(pre) - 3)
        pre_rise = (pre_recent - pre_earlier) / max(1.0, pre_earlier) * 100
        spike = bool(vals[-1] >= 1.5 * pre_recent and pre_rise < 10 and rise > 0)
    else:
        spike = False
    if rise >= 20:
        stage = "급상승"
    elif rise >= 5:
        stage = "상승"
    elif rise <= -20:
        stage = "급하락"
    elif rise <= -5:
        stage = "하락"
    else:
        stage = "보합"
    # 되돌림: 고점이 2개월+ 전이고 최근 3개월 연속 하락하며 고점 대비 뚜렷이 내려온 경우
    peak_i2 = vals.index(peak)
    rollover = bool(peak_i2 <= len(vals) - 3
                    and vals[-1] < vals[-2] < vals[-3]
                    and peak / max(vals[-1], 1.0) >= 1.2)
    # 최근 가속(정밀): 같은 크기 창끼리 성장률 비교 — 최근이 이전보다 빠르면 '가속'
    rise_recent = 0.0
    accel = "flat"
    if len(vals) >= 6:
        last2 = sum(vals[-2:]) / 2.0
        mid2 = sum(vals[-4:-2]) / 2.0
        early2 = sum(vals[-6:-4]) / 2.0
        g_recent = (last2 - mid2) / max(1.0, mid2) * 100      # 최근 성장률
        g_earlier = (mid2 - early2) / max(1.0, early2) * 100  # 이전 성장률
        rise_recent = round(g_recent, 1)
        if g_recent >= g_earlier + 10 and g_recent > 0:
            accel = "up"          # 점점 더 빨라지는 중
        elif g_recent <= g_earlier - 10:
            accel = "down"        # 식는 중
    elif len(vals) >= 4:
        last2 = sum(vals[-2:]) / 2.0
        prev2 = sum(vals[-4:-2]) / 2.0
        rise_recent = round((last2 - prev2) / max(1.0, prev2) * 100, 1)
    return {"latest": round(vals[-1], 1), "rise": rise, "stage": stage,
            "peak_pct": round(vals[-1] / peak * 100),
            "series": [round(v, 1) for v in vals],
            "level": round(level, 1), "stability": stability,
            "momentum": momentum, "n": len(vals),
            "sustained": sustained, "spike": spike, "rollover": rollover,
            "rise_recent": rise_recent, "accel": accel}


def _season_of(series: list) -> dict:
    """12개월 시계열에서 '예상 성수기'와 남은 주(D-day)를 추정한다.
    고점이 평균보다 뚜렷이 높을 때만(계절성 있음) 표시한다."""
    if not series or len(series) < 8:
        return None
    avg = sum(series) / len(series)
    if avg <= 0:
        return None
    peak = max(series)
    if peak / avg < 1.35:                        # 고점이 평균의 1.35배 미만 = 계절성 약함
        return None
    peak_i = series.index(peak)
    months_ago = (len(series) - 1) - peak_i       # 고점이 몇 달 전이었나
    if months_ago == 0:
        return {"label": "지금이 예상 성수기 🔥", "weeks": 0, "hot": True}
    months_until = (12 - months_ago) % 12         # 연 주기 가정, 다음 성수기까지
    weeks = round(months_until * 4.33)
    if weeks <= 8:
        return {"label": f"예상 성수기 D-{weeks}주 — 지금 준비할 때", "weeks": weeks, "hot": True}
    return {"label": f"예상 성수기까지 D-{weeks}주", "weeks": weeks, "hot": False}


def _date_range(months: int = 12) -> tuple:
    """오늘 기준 최근 N개월 [시작, 끝] (YYYY-MM-DD)."""
    from datetime import date
    end = date.today()
    y, m = end.year, end.month - months
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}-01", end.isoformat()


class InsightCatReq(BaseModel):
    client_id: str
    client_secret: str
    categories: list = []       # 분야 이름들 (비면 10개 전체)


@app.post("/api/insight/category")
async def insight_category(req: InsightCatReq):
    """① 분야별 트렌드 — 대분류들의 클릭 추이를 비교해 '뜨는 분야'를 찾는다.
    쇼핑 인사이트는 한 번에 분야 3개까지라, 3개씩 나눠 호출해 합친다.
    (각 분야의 '시간 추이 기울기'는 요청 내부 정규화와 무관해 배치 합산이 안전.)"""
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "허브 열쇠(Client ID/Secret)를 먼저 넣어주세요."}
    names = [n for n in (req.categories or []) if n in _CATS] or list(_CATS.keys())
    start, end = _date_range(12)
    path = "/shopping/v1/categories"
    out = []
    for i in range(0, len(names), 3):                 # 최대 3개씩
        batch = names[i:i + 3]
        cat_param = [{"name": n, "param": [str(_CATS[n])]} for n in batch]
        body = {"startDate": start, "endDate": end, "timeUnit": "month",
                "category": cat_param}
        try:
            data = await _hub_datalab(cid, csec, path, body)
        except NaverHubAuth as e:
            if not out:
                return {"ok": False, "error": str(e)}
            break                                     # 일부라도 받았으면 그걸로
        except Exception as e:  # noqa: BLE001
            if not out:
                return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            break
        for r in (data.get("results") or []):
            t = _trend_of(r.get("data") or [])
            out.append({"name": r.get("title") or "", **t})
    if not out:
        return {"ok": False, "error": "결과가 비어 있어요."}
    out.sort(key=lambda x: -x["rise"])
    return {"ok": True, "items": out, "range": f"{start} ~ {end}", "path": path}


class InsightKwReq(BaseModel):
    client_id: str
    client_secret: str
    category: str = ""          # 분야 이름
    keywords: list = []


@app.post("/api/insight/keyword")
async def insight_keyword(req: InsightKwReq):
    """② 키워드별 트렌드 — 한 분야 안에서 검색어들의 클릭 추이를 비교."""
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "허브 열쇠(Client ID/Secret)를 먼저 넣어주세요."}
    code = _CATS.get(req.category or "")
    if not code:
        return {"ok": False, "error": "분야를 먼저 선택해주세요."}
    kws, seen = [], set()
    for k in (req.keywords or []):
        k = (k or "").strip()
        if k and k not in seen:
            seen.add(k)
            kws.append(k)
    kws = kws[:5]               # 데이터랩 키워드 최대 5개
    if not kws:
        return {"ok": False, "error": "이 분야에서 비교할 검색어를 1개 이상 넣어주세요."}
    kw_param = [{"name": k, "param": [k]} for k in kws]
    start, end = _date_range(12)
    body = {"startDate": start, "endDate": end, "timeUnit": "month",
            "category": str(code), "keyword": kw_param}
    try:
        data, used_path = await _hub_datalab_probe(cid, csec, _SHOP_KW_PATHS, body)
    except NaverHubAuth as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    out = []
    for r in (data.get("results") or []):
        t = _trend_of(r.get("data") or [])
        out.append({"name": r.get("title") or "", **t})
    out.sort(key=lambda x: -x["rise"])
    resp = {"ok": True, "items": out, "category": req.category,
            "range": f"{start} ~ {end}", "path": used_path}
    if not any(x.get("series") for x in out):
        import json as _json
        resp["debug"] = _json.dumps(data, ensure_ascii=False)[:500]
    return resp


_AGE_LABEL = {"10": "10대", "20": "20대", "30": "30대",
              "40": "40대", "50": "50대", "60": "60대+"}


def _segments_from(data: dict) -> dict:
    """성별/연령 응답에서 {구간라벨: 평균지수}를 뽑는다.
    응답 형식이 (a)구간별 result 분리 또는 (b)data 안 group 필드, 둘 다 대응."""
    segs: dict = {}
    for r in (data.get("results") or []):
        pts = r.get("data") or []
        grouped: dict = {}
        for p in pts:
            g = p.get("group")
            try:
                v = float(p.get("ratio"))
            except (TypeError, ValueError):
                continue
            if g is not None:
                grouped.setdefault(str(g), []).append(v)
        if grouped:                                   # (b) group 필드형
            for g, vs in grouped.items():
                segs.setdefault(g, []).extend(vs)
        else:                                         # (a) result 분리형
            label = str(r.get("title") or r.get("group") or "")
            vs = []
            for p in pts:
                try:
                    vs.append(float(p.get("ratio")))
                except (TypeError, ValueError):
                    pass
            if label and vs:
                segs.setdefault(label, []).extend(vs)
    return {k: sum(v) / len(v) for k, v in segs.items() if v}


def _pct(segs: dict) -> list:
    """평균지수를 합=100% 비율로 바꿔 [{label, pct}] 내림차순."""
    tot = sum(segs.values()) or 1.0
    out = [{"label": k, "pct": round(v / tot * 100, 1)} for k, v in segs.items()]
    out.sort(key=lambda x: -x["pct"])
    return out


class InsightTargetReq(BaseModel):
    client_id: str
    client_secret: str
    category: str = ""
    keyword: str = ""


@app.post("/api/insight/target")
async def insight_target(req: InsightTargetReq):
    """③ 타깃 분석 — 한 키워드를 '누가(성별·연령)' 많이 클릭하는지.
    성별·연령 전용 엔드포인트는 한 응답에 전 구간이 같이 정규화돼 비교 가능하다."""
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "허브 열쇠(Client ID/Secret)를 먼저 넣어주세요."}
    code = _CATS.get(req.category or "")
    if not code:
        return {"ok": False, "error": "분야를 먼저 선택해주세요."}
    kw = (req.keyword or "").strip()
    if not kw:
        return {"ok": False, "error": "타깃을 볼 검색어를 1개 넣어주세요."}
    start, end = _date_range(12)
    base = {"startDate": start, "endDate": end, "timeUnit": "month",
            "category": str(code), "keyword": kw}
    res = {"ok": True, "keyword": kw, "category": req.category}
    # 성별
    try:
        g = await _hub_datalab(cid, csec, "/shopping/v1/category/keyword/gender", dict(base))
        gs = _segments_from(g)
        gs = {("여성" if k in ("f", "female", "F") else
               "남성" if k in ("m", "male", "M") else k): v for k, v in gs.items()}
        res["gender"] = _pct(gs)
    except NaverHubAuth as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        res["gender_err"] = f"{type(e).__name__}: {str(e)[:150]}"
    # 연령
    try:
        a = await _hub_datalab(cid, csec, "/shopping/v1/category/keyword/age", dict(base))
        ags = _segments_from(a)
        ags = {_AGE_LABEL.get(k, k): v for k, v in ags.items()}
        res["age"] = _pct(ags)
    except NaverHubAuth as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        res["age_err"] = f"{type(e).__name__}: {str(e)[:150]}"
    # 한 줄 요약
    bits = []
    if res.get("gender"):
        bits.append(res["gender"][0]["label"])
    if res.get("age"):
        bits.append(res["age"][0]["label"])
    res["summary"] = (" · ".join(bits) + " 중심") if bits else ""
    if not res.get("gender") and not res.get("age"):
        return {"ok": False,
                "error": "타깃 데이터를 못 받았어요 — "
                         + (res.get("gender_err") or res.get("age_err") or "")}
    return res


# 검색어 트렌드 후보 경로 (미확인 → 실키로 자동 탐지)
_TREND_PATHS = [
    "/datalab/v1/search",
    "/search-trend/v1/search",
    "/searchtrend/v1/search",
    "/trend/v1/search",
]


class TrendReq(BaseModel):
    client_id: str
    client_secret: str
    keywords: list = []


@app.post("/api/trend/search")
async def trend_search(req: TrendReq):
    """④ 검색어 트렌드 — 키워드들의 '검색량' 추이(오르는지)를 본다.
    쇼핑 인사이트(클릭)와 교차하면: 검색↑ + 클릭↑ = 진짜 뜨는 자리."""
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "허브 열쇠(Client ID/Secret)를 먼저 넣어주세요."}
    kws, seen = [], set()
    for k in (req.keywords or []):
        k = (k or "").strip()
        if k and k not in seen:
            seen.add(k)
            kws.append(k)
    kws = kws[:5]
    if not kws:
        return {"ok": False, "error": "검색량을 볼 키워드를 1개 이상 넣어주세요."}
    start, end = _date_range(12)
    body = {"startDate": start, "endDate": end, "timeUnit": "month",
            "keywordGroups": [{"groupName": k, "keywords": [k]} for k in kws]}
    try:
        data, used_path = await _hub_datalab_probe(cid, csec, _TREND_PATHS, body)
    except NaverHubAuth as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    out = []
    for r in (data.get("results") or []):
        t = _trend_of(r.get("data") or [])
        out.append({"name": r.get("title") or "", **t})
    out.sort(key=lambda x: -x["rise"])
    return {"ok": True, "items": out, "range": f"{start} ~ {end}", "path": used_path}


# 분야별 기준점(앵커) — 항상 꾸준히 팔리는 에버그린 키워드라야 배치 정규화가 안정적이다
_ANCHORS = {
    "패션의류": "티셔츠", "패션잡화": "지갑", "화장품/미용": "선크림",
    "디지털/가전": "이어폰", "가구/인테리어": "책상", "출산/육아": "기저귀",
    "식품": "커피", "스포츠/레저": "운동화", "생활/건강": "칫솔",
    "여가/생활편의": "노트",
}


def _seed_bank() -> dict:
    """분야별 씨앗 키워드 은행 {분야명: (코드, [키워드...])} — 자동 발굴 후보 풀."""
    try:
        p = _HERE.parent / "discovery" / "data" / "category_seeds.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        return {c["name"]: (str(c["cat_id"]), list(c.get("keywords", [])))
                for c in d.get("categories", [])}
    except Exception:  # noqa: BLE001
        return {}


def _grade_explain(it: dict, x_state: str, market: str, intent: float) -> dict:
    """이 등급이 나온 이유를 '올린 근거'와 '깎은 근거'로 나눠 한눈에 설명한다."""
    plus, minus = [], []
    conf = it.get("confidence", 0)
    if conf >= 75:
        plus.append(f"신뢰도 {conf}%")
    elif conf < 50:
        minus.append(f"신뢰도 낮음({conf}%)")
    if it.get("sustained", 0.0) >= 0.67:
        plus.append("꾸준한 상승")
    elif it.get("sustained", 0.0) < 0.34:
        minus.append("상승 지속성 약함")
    if x_state == "both":
        plus.append("검색·클릭 동반 상승")
    if market == "blue":
        plus.append("블루오션(덜 붐빔)")
    elif market == "red":
        minus.append("레드오션(포화)")
    if intent >= 0.5:
        plus.append("구매의도 뚜렷")
    if it.get("spike"):
        minus.append("반짝 급등 의심")
    if it.get("rollover"):
        minus.append("고점 지나 꺾임")
    return {"plus": plus, "minus": minus}


async def _enrich_top(cid: str, csec: str, items: list, start: str, end: str) -> str:
    """상위 후보에 3중 교차검증(검색량·지식iN·블로그)·신뢰도·시장·등급을 붙인다.
    자동발굴과 니치가 공용으로 써서 선별 품질을 동일하게 유지한다. trend_path 반환."""
    import asyncio as _aio
    trend_path = None
    try:
        top = items[:20]
        srise = {}
        for i in range(0, len(top), 5):
            b = [x["keyword"] for x in top[i:i + 5]]
            body = {"startDate": start, "endDate": end, "timeUnit": "month",
                    "keywordGroups": [{"groupName": k, "keywords": [k]} for k in b]}
            data, trend_path = await _hub_datalab_probe(cid, csec, _TREND_PATHS, body)
            for r in (data.get("results") or []):
                t = _trend_of(r.get("data") or [])
                if t.get("series"):
                    srise[r.get("title") or ""] = t["rise"]
        deep = top[:10]
        kin_sig = await _aio.gather(*(_kin_signal(cid, csec, x["keyword"]) for x in deep))
        blog_t = await _aio.gather(*(_search_total(cid, csec, "blog", x["keyword"]) for x in deep))
        totals = {deep[i]["keyword"]: (kin_sig[i][0], kin_sig[i][1], blog_t[i])
                  for i in range(len(deep))}
        for it in top:
            kw = it["keyword"]
            sr = srise.get(kw)
            cr = it.get("rise", 0.0)
            kin, intent, blog = totals.get(kw, (None, 0.0, None))
            has_chatter = bool((kin and kin > 0) or (blog and blog > 0))
            is_spike = bool(it.get("spike"))
            is_roll = bool(it.get("rollover"))
            if sr is not None and sr >= 5 and cr >= 5:
                if is_spike or is_roll:
                    # 반짝 급등/되돌림이면 '둘 다 올라도' 신뢰를 낮춘다
                    x_state = "search"
                    xnote = (f"검색량 {sr:.0f}%↑·클릭↑ 이나 "
                             + ("반짝 급등 주의" if is_spike else "고점 지나 꺾임 주의"))
                elif has_chatter:
                    # 검색·클릭 + 실제 언급(지식iN/블로그)까지 = 교차검증 통과
                    x_state = "verified"
                    xnote = f"검색 {sr:.0f}%↑·클릭↑·실수요 언급까지 (교차검증 ✓)"
                else:
                    # 검색·클릭은 오르나 언급이 전혀 없음 = 노이즈 가능성
                    x_state = "both"
                    xnote = f"검색량 {sr:.0f}%↑ · 클릭도 ↑ (단, 언급 적음)"
            elif sr is not None and sr >= 5:
                x_state, xnote = "search", f"검색량 {sr:.0f}%↑ (클릭은 완만)"
            elif cr >= 5:
                x_state, xnote = "click", "클릭은 오르나 검색량은 완만"
            else:
                x_state, xnote = "none", "뚜렷한 상승 신호 약함"
            bits = [xnote]
            if kin and kin > 0:
                bits.append(f"지식iN 질문 {kin:,}건")
            if intent >= 0.5:
                bits.append(f"구매의도 높음({int(intent*100)}%)")
            if blog and blog > 0:
                bits.append(f"블로그 {blog:,}건")
            it["xcheck"] = {"level": ("go" if x_state in ("verified", "both") else
                                      "wait" if x_state in ("search", "click") else "neutral"),
                            "verified": x_state == "verified",
                            "note": " · ".join(bits)}
            conf = _confidence(it, x_state,
                               kin if kin and kin > 0 else 0,
                               blog if blog and blog > 0 else 0,
                               intent)
            it["confidence"] = conf
            if intent >= 0.5:
                it.setdefault("reasons", []).append(
                    f"지식iN에서 '추천·어디서·가성비' 등 살 마음이 뚜렷(구매의도 {int(intent*100)}%)")
            mk = _market_of(cr, blog if blog and blog > 0 else 0)
            if mk[0]:
                it["market"] = {"level": mk[0], "note": mk[1]}
            it["grade"] = _grade_of(it["confidence"], it.get("sustained", 0.0),
                                    it.get("spike", False), mk[0],
                                    it.get("rollover", False))
            it["grade_why"] = _grade_explain(it, x_state, mk[0], intent)
            if it.get("spike"):
                it.setdefault("reasons", []).insert(
                    0, "⚠️ 최근 한 달만 튄 반짝 급등일 수 있음 — 지속성 확인 필요")
    except Exception:  # noqa: BLE001
        pass
    return trend_path


class DiscoverReq(BaseModel):
    client_id: str
    client_secret: str
    category: str = ""          # 분야명 ("" = 전체 분야 스캔)
    mode: str = "consign"       # consign(위탁) | wholesale(사입/도매)
    wide: bool = False          # 넓게 찾기 — 실시간 연관어를 씨앗에 섞어 새 후보 발굴
    exclude: list = []          # 이미 본 키워드 — 빼고 다음 후보를 보여준다(새로고침)


def _discover_reasons(it: dict) -> list:
    """숫자를 '사도 되는 근거' 문장으로 — 왜 뽑혔는지 명확히 보여준다."""
    rs = it.get("rise", 0.0)
    mo = it.get("momentum", 0.0)
    st = it.get("stability", 0.0)
    lv = it.get("level", 0.0)
    n = it.get("n", 0)
    out = []
    if rs >= 20:
        out.append(f"검색 클릭이 최근 {rs:.0f}% 급상승 — 수요가 빠르게 커지는 중")
    elif rs >= 5:
        out.append(f"검색 클릭이 최근 {rs:.0f}% 상승 — 수요가 붙는 중")
    elif rs <= -5:
        out.append(f"검색 클릭이 {abs(rs):.0f}% 하락 — 수요가 식는 중(주의)")
    else:
        out.append("검색 클릭이 큰 변동 없이 유지되는 중")
    if mo >= 15:
        out.append(f"직전 달 대비 +{mo:.0f}% — 지금도 오르는 중(모멘텀 살아있음)")
    elif mo <= -15:
        out.append(f"직전 달 대비 {mo:.0f}% — 최근 꺾임(주의)")
    if st >= 0.7:
        out.append("12개월 내내 안정적(변동 적음) — 반짝 유행이 아님")
    elif st < 0.4:
        out.append("월별 변동이 큼 — 계절/이벤트 영향일 수 있음")
    if lv >= 50:
        out.append("이 분야에서 관심이 높은 편(수요 규모 큼)")
    out.append(f"최근 {n}개월 데이터로 검증됨")
    return out


def _discover_verdict(it: dict, mode: str) -> list:
    """종합 판단 — 이 모드(위탁/사입)에서 사도 되는지 한 줄로."""
    rs = it.get("rise", 0.0)
    mo = it.get("momentum", 0.0)
    st = it.get("stability", 0.0)
    lv = it.get("level", 0.0)
    if mode == "consign":
        if rs >= 20 and mo >= 0:
            return ["go", "지금 위탁으로 들어갈 타이밍 — 수요가 빠르게 오르는 중"]
        if rs >= 5:
            return ["go", "위탁 후보로 괜찮음 — 수요가 붙는 중"]
        if rs <= -5:
            return ["stop", "지금은 피하기 — 수요가 식는 중"]
        return ["wait", "관망 — 뚜렷한 상승 근거가 약함"]
    if lv >= 40 and st >= 0.6:
        return ["go", "사입해도 안전 — 수요 높고 12개월 안정적"]
    if st >= 0.6:
        return ["wait", "안정적이나 수요 규모는 보통 — 소량부터 시작"]
    return ["stop", "대량 사입은 위험 — 월별 변동이 큼"]


@app.post("/api/discover")
async def discover(req: DiscoverReq):
    """🎯 자동 발굴 — 사용자가 키워드를 안 넣어도, 내장 씨앗(분야별 수십 개)을
    쇼핑 인사이트 트렌드에 돌려 '지금 뜨는 위탁 후보'를 상승률 순으로 찾아준다."""
    import asyncio
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "허브 열쇠(Client ID/Secret)를 먼저 넣어주세요."}
    bank = _seed_bank()
    if not bank:
        return {"ok": False, "error": "씨앗 데이터를 불러오지 못했어요(category_seeds.json)."}
    targets = [req.category] if req.category in bank else list(bank.keys())
    # 🔀 넓게 찾기 — 실시간 연관어를 씨앗에 섞어 '같은 것만 나오는' 문제를 푼다.
    # (단일 분야 선택 시에만. 랜덤 씨앗에서 캐서 같은 달에도 후보가 달라진다)
    expanded_set = set()
    if req.wide and req.category in bank:
        import random
        code_w, kws_w = bank[req.category]
        anchor_w = _ANCHORS.get(req.category) or (kws_w[0] if kws_w else "")
        bases = [anchor_w] + random.sample(kws_w, min(3, len(kws_w)))
        seen_w = set(kws_w)
        for b in bases:
            if not b:
                continue
            try:
                rel = await _related_terms(cid, csec, b, limit=6)
            except Exception:  # noqa: BLE001
                rel = []
            for w in rel:
                if w not in seen_w and 2 <= len(w) <= 12:
                    seen_w.add(w)
                    expanded_set.add(w)
        if expanded_set:
            bank[req.category] = (code_w, kws_w + list(expanded_set)[:16])
    start, end = _date_range(12)
    # (분야, 코드, 앵커, 4개 배치) — 앵커를 모든 배치에 넣어 배치 간 수준 비교를 가능케 한다
    jobs = []
    anchor_of = {}
    for cat in targets:
        code, kws = bank[cat]
        anchor = _ANCHORS.get(cat) or (kws[0] if kws else "")   # 에버그린 앵커, 없으면 첫 씨앗
        anchor_of[cat] = anchor
        others = [k for k in kws if k != anchor]
        for i in range(0, len(others), 4):
            jobs.append((cat, code, anchor, others[i:i + 4]))
    sem = asyncio.Semaphore(4)
    first_err = {"msg": ""}
    anchor_info = {}                                  # {분야: {keyword, stability, n, ok}}

    async def run(cat, code, anchor, batch):
        async with sem:
            kws = ([anchor] + batch) if anchor else batch
            body = {"startDate": start, "endDate": end, "timeUnit": "month",
                    "category": code,
                    "keyword": [{"name": k, "param": [k]} for k in kws]}
            try:
                data = await _hub_datalab(cid, csec,
                                          "/shopping/v1/category/keywords", body)
            except NaverHubAuth as e:
                if not first_err["msg"]:
                    first_err["msg"] = str(e)
                return []
            except Exception as e:  # noqa: BLE001
                if not first_err["msg"]:
                    first_err["msg"] = f"{type(e).__name__}: {str(e)[:120]}"
                return []
            results = data.get("results") or []
            # 앵커의 수준·안정성을 찾아 배치 내 상대수준 환산의 기준으로 삼는다
            anchor_lv = 0.0
            trends = {}
            for r in results:
                t = _trend_of(r.get("data") or [])
                trends[r.get("title") or ""] = t
                if anchor and (r.get("title") or "") == anchor:
                    anchor_lv = t.get("level", 0.0)
                    if cat not in anchor_info:
                        st = t.get("stability", 0.0)
                        nn = t.get("n", 0)
                        anchor_info[cat] = {"keyword": anchor, "stability": st,
                                            "n": nn, "ok": bool(st >= 0.5 and nn >= 8)}
            picked = []
            for name, t in trends.items():
                if not t.get("series"):
                    continue
                if anchor and name == anchor:
                    continue                       # 앵커 자신은 후보에서 제외
                # 앵커 대비 상대수준(배치 간 비교 가능). 앵커가 약하면 원수준 유지
                nlevel = round(t["level"] / anchor_lv * 100, 1) if anchor_lv > 0 else t["level"]
                picked.append({"keyword": name, "category": cat,
                               "nlevel": nlevel, **t})
            return picked

    batches = await asyncio.gather(*(run(c, code, anc, b) for c, code, anc, b in jobs))
    items = [x for sub in batches for x in sub]
    if not items:
        return {"ok": False,
                "error": "발굴 결과가 비었어요 — " + (first_err["msg"] or
                         "트렌드 데이터를 못 받았어요.")}
    mode = "wholesale" if req.mode == "wholesale" else "consign"
    kept = []
    for it in items:
        lv = it.get("level", 0.0)
        st = it.get("stability", 0.0)
        rs = it.get("rise", 0.0)
        mo = it.get("momentum", 0.0)
        n = it.get("n", 0)
        # 정밀도 1) 데이터가 부족하면(6개월 미만) 추세를 못 믿으니 제외
        if n < 6:
            continue
        # 정밀도 2) 관심이 미미하고(수준 낮음) 오르지도 않으면 제외
        if lv < 6 and rs < 5:
            continue
        sus = it.get("sustained", 0.0)
        nlv = it.get("nlevel", lv)          # 앵커 대비 상대수준(배치 간 비교 가능)
        # 위탁 적합 = 급상승·모멘텀 × 지속성(반짝 급등은 감점)
        consign = round((max(0.0, rs) * 0.7 + max(0.0, mo) * 0.3)
                        * (0.5 + 0.5 * sus), 1)
        # 사입 적합 = 상대수준 × 안정성 (앵커 정규화로 배치 간 공정 비교)
        wholesale = round(nlv * (0.45 + 0.55 * st), 1)
        it["consign_score"] = consign
        it["wholesale_score"] = wholesale
        fit = []
        if rs >= 10 or mo >= 15:
            fit.append("위탁형")
        if nlv >= 60 and st >= 0.6:          # 앵커의 60%+ 수준이며 안정적이면 사입형
            fit.append("사입형")
        it["fit"] = fit or ["관망"]
        kept.append(it)
    if not kept:
        return {"ok": False, "error": "조건을 넘는 뜨는 후보가 없어요 — 다른 분야로 바꿔보세요."}
    # 정밀도 3) 같은 키워드가 여러 분야 씨앗에 겹치면 점수 높은 것만 남긴다
    key = "wholesale_score" if mode == "wholesale" else "consign_score"
    best = {}
    for it in kept:
        kw = it.get("keyword", "")
        if kw not in best or it.get(key, 0) > best[kw].get(key, 0):
            best[kw] = it
    kept = list(best.values())
    # 정밀도) 사용자 피드백 반영 — 👍는 위로, 👎는 아래로 (쓸수록 정확)
    try:
        fbmap = feedback_map()
    except Exception:  # noqa: BLE001
        fbmap = {}
    for it in kept:
        v = fbmap.get(it.get("keyword", ""), 0)
        it["feedback"] = v
        if v:
            for kk in ("consign_score", "wholesale_score"):
                it[kk] = max(0, min(100, it.get(kk, 0) + (12 if v > 0 else -25)))
    # 점수 → 모멘텀(동점 시 최근 방향) 순
    kept.sort(key=lambda x: (-x.get(key, 0), -x.get("momentum", 0)))
    for it in kept:
        it["hot"] = ("사입형" in it["fit"]) if mode == "wholesale" else ("위탁형" in it["fit"])
        it["score"] = it["wholesale_score"] if mode == "wholesale" else it["consign_score"]
        it["reasons"] = _discover_reasons(it)
        it["verdict"] = _discover_verdict(it, mode)      # [level, text]
        it["season"] = _season_of(it.get("series") or [])
        if it.get("rollover"):
            it["reasons"].insert(0, "⚠️ 고점을 지나 최근 꺾이는 중 — 진입 시점 주의")
        if it.get("keyword") in expanded_set:
            it["from_related"] = True            # 연관어 확장으로 새로 들어온 후보
        # 사입 모드에서 기준점(앵커)이 불안정하면 '수준 비교'가 덜 정확함을 표시
        ai = anchor_info.get(it.get("category", ""))
        if mode == "wholesale" and ai and not ai["ok"]:
            it["anchor_weak"] = True
    # 🔄 이미 본 키워드는 빼고 다음 후보를 노출(몇 주째 같은 것만 나오는 문제 해결)
    _excl = set((req.exclude or []))
    _before = len(kept)
    if _excl:
        kept = [it for it in kept if it.get("keyword") not in _excl]
    _excluded_count = _before - len(kept)
    result = kept[:40]
    # 🔗 3중 교차검증·신뢰도·등급 (자동발굴/니치 공용)
    trend_path = await _enrich_top(cid, csec, result, start, end)
    # 🧪 백테스트: 이번 A·B급 판정을 기록(하루 1회) → 등급별 적중률 검증
    try:
        rows_log = [{"keyword": it["keyword"], "category": it.get("category", ""),
                     "grade": it.get("grade", ""), "rise": it.get("rise", 0)}
                    for it in result if it.get("grade") in ("A", "B")]
        if rows_log:
            backtest_log(rows_log)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "items": result, "range": f"{start} ~ {end}",
            "scanned": len(items), "mode": mode, "targets": targets,
            "trend_path": trend_path, "expanded": len(expanded_set),
            "excluded_count": _excluded_count,
            "anchors": [{"category": c, "keyword": v["keyword"],
                         "stability": v["stability"], "ok": v["ok"]}
                        for c, v in anchor_info.items()]}


_NICHE_MODS = ["무선", "미니", "대용량", "저소음", "접이식", "휴대용", "방수",
               "소형", "대형", "프리미엄", "1인용", "정리", "세트", "실내", "캐릭터",
               "가성비", "고급"]


class NicheReq(BaseModel):
    client_id: str
    client_secret: str
    category: str = ""
    keyword: str = ""


@app.post("/api/niche")
async def niche(req: NicheReq):
    """🧭 니치 조합 발굴 — 기준 키워드에 수식어를 곱해 세부 조합의 트렌드를 재고,
    '아직 안 붐비지만 수요가 오르는' 롱테일 니치를 상승률 순으로 찾는다."""
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "허브 열쇠(Client ID/Secret)를 먼저 넣어주세요."}
    code = _CATS.get(req.category or "")
    if not code:
        return {"ok": False, "error": "분야를 먼저 선택해주세요."}
    base = (req.keyword or "").strip()
    if not base:
        return {"ok": False, "error": "기준 키워드를 넣어주세요 (예: 청소기)."}
    combos, seen = [base], {base}
    for m in _NICHE_MODS:
        c = f"{m} {base}"
        if c not in seen:
            seen.add(c)
            combos.append(c)
    # 🔎 연관검색어 자동 확장 — 블로그·지식iN에서 실제로 함께 쓰이는 단어로 조합
    related = await _related_terms(cid, csec, base, limit=8)
    rel_set = set()
    for w in related:
        c = f"{w} {base}"
        if c not in seen:
            seen.add(c)
            rel_set.add(c)
            combos.append(c)
    combos = combos[:30]
    start, end = _date_range(12)
    out = []
    first_err = ""
    for i in range(0, len(combos), 5):
        b = combos[i:i + 5]
        body = {"startDate": start, "endDate": end, "timeUnit": "month",
                "category": str(code),
                "keyword": [{"name": c, "param": [c]} for c in b]}
        try:
            data = await _hub_datalab(cid, csec,
                                      "/shopping/v1/category/keywords", body)
        except NaverHubAuth as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            if not first_err:
                first_err = f"{type(e).__name__}: {str(e)[:120]}"
            continue
        for r in (data.get("results") or []):
            t = _trend_of(r.get("data") or [])
            if t.get("n", 0) >= 6 and t.get("series"):
                it = {"keyword": r.get("title") or "", "category": req.category, **t}
                it["consign_score"] = round(max(0.0, it["rise"]) * 0.7
                                             + max(0.0, it["momentum"]) * 0.3, 1)
                it["reasons"] = _discover_reasons(it)
                it["verdict"] = _discover_verdict(it, "consign")
                it["season"] = _season_of(it.get("series") or [])
                it["fit"] = (["위탁형"] if (it["rise"] >= 10 or it["momentum"] >= 15)
                             else ["관망"])
                if it["keyword"] in rel_set:
                    it["from_related"] = True   # 실제 연관어에서 나온 조합 표시
                out.append(it)
    if not out:
        return {"ok": False, "error": "조합 데이터를 못 받았어요 — " + (first_err or "다른 키워드로.")}
    out.sort(key=lambda x: -x["rise"])
    # 니치도 자동발굴과 동일한 3중 교차검증·신뢰도·등급을 붙인다
    trend_path = await _enrich_top(cid, csec, out[:20], start, end)
    return {"ok": True, "items": out, "range": f"{start} ~ {end}",
            "base": base, "mode": "consign", "related": related,
            "trend_path": trend_path}


class BacktestReq(BaseModel):
    client_id: str
    client_secret: str


@app.post("/api/backtest")
async def backtest_api(req: BacktestReq):
    """🧪 백테스트 — 7일+ 전에 A급이라 한 종목이 이후에도 상승을 유지했는지
    다시 재서 적중률을 낸다(등급 공식이 실제로 맞는지 자기검증)."""
    import asyncio
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "허브 열쇠(Client ID/Secret)를 먼저 넣어주세요."}
    try:
        pend = backtest_pending(days=7)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "items": []}
    if not pend:
        return {"ok": True, "items": [], "note": "아직 검증할 기록이 없어요(A급을 발굴한 지 7일 지나면 쌓여요)."}
    start, end = _date_range(12)
    sem = asyncio.Semaphore(4)

    async def one(row):
        kw = row.get("keyword")
        code = _CATS.get(row.get("category", ""))
        if not kw or not code:
            return None
        body = {"startDate": start, "endDate": end, "timeUnit": "month",
                "category": str(code), "keyword": [{"name": kw, "param": [kw]}]}
        async with sem:
            try:
                data = await _hub_datalab(cid, csec,
                                          "/shopping/v1/category/keywords", body)
            except Exception:  # noqa: BLE001
                return None
        res = data.get("results") or []
        if not res:
            return None
        t = _trend_of(res[0].get("data") or [])
        now = t.get("rise", 0.0)
        # 판정 당시 상승세가 이후에도 유지(하락 전환 안 함)됐으면 적중
        hit = bool(now >= 0 and not t.get("rollover"))
        return {"keyword": kw, "category": row.get("category", ""),
                "grade": row.get("grade", ""),
                "logged_at": row.get("logged_at", "")[:10],
                "then_rise": round(float(row.get("rise") or 0), 1),
                "now_rise": now, "hit": hit}

    got = await asyncio.gather(*(one(r) for r in pend))
    out = [x for x in got if x]
    hits = sum(1 for x in out if x["hit"])
    rate = round(hits / len(out) * 100) if out else 0

    def _rate(grade):
        sub = [x for x in out if x.get("grade") == grade]
        avg_now = round(sum(x["now_rise"] for x in sub) / len(sub), 1) if sub else None
        avg_then = round(sum(x["then_rise"] for x in sub) / len(sub), 1) if sub else None
        return {"grade": grade, "total": len(sub),
                "hits": sum(1 for x in sub if x["hit"]),
                "rate": round(sum(1 for x in sub if x["hit"]) / len(sub) * 100) if sub else None,
                "avg_now": avg_now, "avg_then": avg_then}
    by_grade = [_rate("A"), _rate("B")]
    # A급 적중률로만 기준 보정(A의 표본으로)
    a_stat = by_grade[0]
    try:
        a_min = calib_set_from_hitrate(a_stat["rate"] if a_stat["rate"] is not None else rate,
                                       a_stat["total"])
    except Exception:  # noqa: BLE001
        a_min = 75
    # 등급 변별력: A가 B보다 (1)잘 맞고 (2)실제로 더 크게 올랐으면 유효
    valid = None
    mag = None                                    # 상승 유지폭 우위(A평균 - B평균)
    if a_stat["rate"] is not None and by_grade[1]["rate"] is not None:
        valid = a_stat["rate"] >= by_grade[1]["rate"]
    if a_stat["avg_now"] is not None and by_grade[1]["avg_now"] is not None:
        mag = round(a_stat["avg_now"] - by_grade[1]["avg_now"], 1)
    return {"ok": True, "items": out, "hit_rate": rate,
            "hits": hits, "total": len(out), "a_min_conf": a_min,
            "by_grade": by_grade, "grade_valid": valid, "mag_gap": mag}


@app.get("/api/usage")
async def usage_api():
    """📊 이번 달 API 호출량 — 네이버 허브 월 한도에 가까워지면 경고."""
    try:
        return {"ok": True, **api_usage()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


class OwnerClanReq(BaseModel):
    username: str
    password: str
    keyword: str = ""
    sort: str = "default"       # default | priceAsc | priceDesc
    min_price: int = 0
    max_price: int = 0
    first: int = 30
    sandbox: bool = False


# 오너클랜 JWT 토큰 임시 캐시(메모리) — username→(token, 만료ts). 비번은 저장 안 함.
_OC_TOKENS: dict = {}


def _ownerclan_item_url(key: str) -> str:
    """오너클랜 상품 상세페이지 URL — 실제 형식은 selfcode 파라미터."""
    from urllib.parse import quote
    k = (key or "").strip()
    if not k:
        return ""
    return f"https://ownerclan.com/V2/product/view.php?selfcode={quote(k)}"


async def _ownerclan_token(username: str, password: str, sandbox: bool) -> str:
    """오너클랜 공식 인증 — 아이디/비번을 POST 해서 JWT 토큰을 받는다(매뉴얼 명세).
    토큰은 메모리에 잠깐 캐시하고 비밀번호는 저장하지 않는다."""
    import time
    import httpx
    ck = f"{'sb' if sandbox else 'pr'}:{username}"
    cached = _OC_TOKENS.get(ck)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    url = ("https://auth-sandbox.ownerclan.com/auth" if sandbox
           else "https://auth.ownerclan.com/auth")
    body = {"service": "ownerclan", "userType": "seller",
            "username": username, "password": password}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=8.0, read=15.0, write=10.0, pool=8.0)) as c:
            r = await c.post(url, json=body)
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise NaverHubAuth("오너클랜 인증 서버 응답이 느려요(타임아웃). 20~30초 뒤 다시 시도해주세요.")
    if r.status_code >= 400:
        # 오너클랜이 보낸 실제 사유를 그대로 노출(디버깅에 결정적)
        detail = (r.text or "").strip().replace("\n", " ")[:200]
        env = "샌드박스" if sandbox else "운영(production)"
        raise NaverHubAuth(
            f"오너클랜 인증 거부({r.status_code}) · 요청환경: {env}. "
            f"오너클랜 응답: {detail or '(내용 없음)'} — "
            f"판매사 ID/비번이 맞는지, 그리고 이 환경({env})으로 API 사용이 "
            f"승인됐는지 확인해주세요. (샌드박스/운영은 각각 따로 신청·승인돼요)")
    # 토큰이 raw 문자열 또는 {\"token\":..} JSON 두 형태 모두 대응
    raw = (r.text or "").strip()
    token = raw.strip('"')
    if raw.startswith("{"):
        try:
            j = r.json()
            token = (j.get("token") or j.get("accessToken")
                     or j.get("jwt") or j.get("data") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    if not token or token.count(".") < 2:      # JWT는 점 2개(3구분)
        raise NaverHubAuth(f"오너클랜 인증 응답에서 토큰을 찾지 못했어요. 응답: {raw[:160]}")
    _OC_TOKENS[ck] = (token, time.time() + 3000)   # 약 50분 캐시
    return token


async def _ownerclan_search(token: str, req: OwnerClanReq) -> list:
    """오너클랜 상품 검색(allItems) — 공식 GraphQL READ(GET) 호출.
    검색+가격정렬은 오너클랜에서 무거운(Quota 강화) 쿼리라, 가격정렬은
    서버에서 직접 처리해 타임아웃을 피한다."""
    import asyncio
    from urllib.parse import quote
    import httpx
    args = [f"first: {max(1, min(req.first, 100))}"]
    kw = (req.keyword or "").replace('"', '\\"').strip()
    if kw:
        args.append(f'search: "{kw}"')
    if req.min_price and req.min_price > 0:
        args.append(f'minPrice: {int(req.min_price)}')
    if req.max_price and req.max_price > 0:
        args.append(f'maxPrice: {int(req.max_price)}')
    # 날짜 정렬만 API에 위임(가벼움). 가격 정렬은 아래에서 서버가 처리.
    if req.sort in ("dateDesc", "dateAsc"):
        args.append(f'sortBy: {req.sort}')
    query = ("query { allItems(" + ", ".join(args) + ") { pageInfo { hasNextPage } "
             "edges { node { "
             "key name price fixedPrice shippingFee images(size: medium) status model origin "
             "category { name fullName } } } } }")
    base = ("https://api-sandbox.ownerclan.com/v1/graphql" if req.sandbox
            else "https://api.ownerclan.com/v1/graphql")
    url = base + "?query=" + quote(query)
    headers = {"Authorization": f"Bearer {token}"}
    tmo = httpx.Timeout(connect=8.0, read=22.0, write=10.0, pool=8.0)
    data = None
    last_exc = None
    for attempt in range(2):                      # 타임아웃/과요청 시 1회 재시도(예산 ~45초 상한)
        try:
            async with httpx.AsyncClient(timeout=tmo) as c:
                r = await c.get(url, headers=headers)
            if r.status_code == 429:
                if attempt < 1:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                raise NaverHubAuth("오너클랜 요청이 잠깐 몰렸어요(Too many requests). "
                                   "20~30초 뒤에 다시 시도해주세요.")
            if r.status_code >= 400:
                raise NaverHubAuth(f"오너클랜 조회 실패({r.status_code}): {(r.text or '')[:160]}")
            data = r.json()
            # GraphQL 본문에 과요청 에러가 담겨오는 경우도 처리
            errs = data.get("errors") or []
            if errs and "too many" in (errs[0].get("message", "").lower()):
                if attempt < 1:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    data = None
                    continue
                raise NaverHubAuth("오너클랜 요청이 잠깐 몰렸어요(Too many requests). "
                                   "20~30초 뒤에 다시 시도해주세요.")
            break
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            last_exc = e
            if attempt < 1:
                await asyncio.sleep(1.0)
                continue
            raise NaverHubAuth("오너클랜 서버 응답이 느려요(타임아웃). 잠시 후 다시 시도하거나, "
                               "검색어를 더 구체적으로 좁혀보세요.")
    if data is None:
        raise NaverHubAuth("오너클랜 응답을 받지 못했어요(재시도 초과).")
    if data.get("errors"):
        msg = (data["errors"][0].get("message") if data["errors"] else "조회 오류")
        raise NaverHubAuth(f"오너클랜 API 오류: {msg}")
    ai = (data.get("data") or {}).get("allItems")
    if ai is None:
        raise NaverHubAuth("오너클랜 응답에 상품 데이터(allItems)가 없어요 — 쿼리/권한 확인 필요.")
    edges = ai.get("edges") or []
    out = []
    seen_keys = set()
    for e in edges:
        n = e.get("node") or {}
        key = n.get("key")
        nm = (n.get("name") or "").strip()
        imgs = n.get("images") or []
        price = n.get("price")
        # 빈/불완전 상품만 제외(진짜 쓸모없는 것): 이름 없음·이미지 없음·가격 0
        if not nm or len(nm) < 2:
            continue
        if not imgs:
            continue
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        # 중복은 '같은 상품코드'만 제거(색상·옵션 다른 건 살려서 다양성 유지)
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        out.append({
            "key": key, "name": nm,
            "price": price, "fixedPrice": n.get("fixedPrice"),
            "shippingFee": n.get("shippingFee"),
            "status": n.get("status"),
            "model": n.get("model"), "origin": n.get("origin"),
            "image": imgs[0] if imgs else "",
            "url": _ownerclan_item_url(key),
            "category": (n.get("category") or {}).get("fullName")
            or (n.get("category") or {}).get("name") or "",
        })
    # 가격 정렬은 서버에서(무거운 API 정렬 회피). 가격 없는 항목은 뒤로.
    if req.sort == "priceAsc":
        out.sort(key=lambda x: (x["price"] is None, x["price"] or 0))
    elif req.sort == "priceDesc":
        out.sort(key=lambda x: (x["price"] is None, -(x["price"] or 0)))
    return out


class DomeggookReq(BaseModel):
    api_key: str
    keyword: str = ""
    market: str = "dome"      # dome(도매꾹) | supply(도매매)
    sort: str = "rd"          # rd랭킹·ha인기·aa낮은가격·ad높은가격·da최근
    min_price: int = 0
    max_price: int = 0
    page: int = 1
    size: int = 40
    free_ship: bool = False   # 무료배송만
    kr_only: bool = False     # 국산만


@app.post("/api/domeggook/search")
async def domeggook_search_api(req: DomeggookReq):
    """도매꾹·도매매 공식 오픈API 상품리스트 조회(getItemList). 크롤링 아님, 공식 API."""
    import httpx
    from urllib.parse import urlencode
    key = (req.api_key or "").strip()
    kw = (req.keyword or "").strip()
    if not key:
        return {"ok": False, "error": "도매꾹 API 키(aid)를 넣어주세요."}
    if not kw:
        return {"ok": False, "error": "검색어를 넣어주세요."}
    params = {
        "ver": "4.1", "mode": "getItemList", "aid": key,
        "market": "supply" if req.market == "supply" else "dome",
        "om": "json", "kw": kw,
        "sz": max(1, min(req.size, 100)), "pg": max(1, req.page),
        "so": req.sort if req.sort in ("se", "rd", "ha", "aa", "ad", "sd", "qa", "qd", "da") else "rd",
    }
    if req.min_price and req.min_price > 0:
        params["mnp"] = int(req.min_price)
    if req.max_price and req.max_price > 0:
        params["mxp"] = int(req.max_price)
    if req.free_ship:
        params["who"] = "S"
    if req.kr_only:
        params["org"] = "kr"
    url = "https://domeggook.com/ssl/api/?" + urlencode(params)
    _t0 = _time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=8, read=22, write=10, pool=8)) as c:
            r = await c.get(url)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"도매꾹 연결 오류: {type(exc).__name__}"}
    _elapsed = int((_time.time() - _t0) * 1000)
    if r.status_code >= 400:
        return {"ok": False, "error": f"도매꾹 조회 실패({r.status_code}) — API 키 확인."}
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "도매꾹 응답 해석 실패(키 오류일 수 있어요)."}
    # 에러 응답(도메꾹은 errors 노드로 줄 수 있음)
    root = data.get("domeggook") or data
    if isinstance(root, dict) and root.get("errors"):
        return {"ok": False, "error": f"도매꾹 API 오류: {root['errors']}"}
    header = root.get("header") or {}
    lst = ((root.get("list") or {}).get("item")) or []
    if isinstance(lst, dict):        # 결과 1개면 dict로 옴
        lst = [lst]
    out = []
    seen = set()
    for it in lst:
        if not isinstance(it, dict):
            continue
        no = str(it.get("no") or "").strip()
        title = (it.get("title") or "").strip()
        price = it.get("price")
        thumb = (it.get("thumb") or "").strip()
        if not no or len(title) < 2:
            continue
        try:
            price = int(price)
        except Exception:  # noqa: BLE001
            price = None
        # 정밀도: 가격 없거나 0, 이미지 없는 빈 상품 제외
        if not price or price <= 0:
            continue
        if not thumb:
            continue
        if no in seen:
            continue
        seen.add(no)
        deli = it.get("deli") or {}
        out.append({
            "no": no, "title": title, "price": price,
            "thumb": thumb,
            "url": (it.get("url") or "").strip(),
            "seller": it.get("id") or "", "unitQty": it.get("unitQty"),
            "deli_who": deli.get("who"), "deli_fee": deli.get("fee"),
            "comOnly": it.get("comOnly"),
        })
    prices = [x["price"] for x in out if isinstance(x.get("price"), int) and x["price"] > 0]
    stat = {}
    if prices:
        stat = {"min": min(prices), "max": max(prices),
                "avg": int(sum(prices) / len(prices)), "count": len(out)}
    try:
        print(f"[도매꾹검색] kw='{kw}' so={params['so']} → {len(out)}개 · {_elapsed}ms", flush=True)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "items": out, "stat": stat, "keyword": kw,
            "total": header.get("numberOfItems"), "market": params["market"],
            "elapsed_ms": _elapsed, "sort": params["so"]}


class DomeggookItemReq(BaseModel):
    api_key: str
    no: str


class DomeggookBestReq(BaseModel):
    api_key: str
    keyword: str
    market: str = "dome"
    client_id: str = ""       # 네이버 허브(수요, 선택)
    client_secret: str = ""


@app.post("/api/domeggook/best")
async def domeggook_best_api(req: DomeggookBestReq):
    """⭐ 도매꾹 최적상품 — 인기순(ha, 도매꾹 공식) + 네이버 수요 + 가격/배송으로 기회점수.
    오너클랜과 달리 '인기순'이 공식 지원돼 실제 잘나가는 상품 기반으로 고른다."""
    import httpx
    from urllib.parse import urlencode
    key = (req.api_key or "").strip()
    kw = (req.keyword or "").strip()
    if not key:
        return {"ok": False, "error": "도매꾹 API 키를 넣어주세요."}
    if not kw:
        return {"ok": False, "error": "검색어를 넣어주세요."}
    # 1) 인기순으로 상품 가져오기(도매꾹 강점)
    params = {"ver": "4.1", "mode": "getItemList", "aid": key,
              "market": "supply" if req.market == "supply" else "dome",
              "om": "json", "kw": kw, "sz": 60, "pg": 1, "so": "ha"}
    url = "https://domeggook.com/ssl/api/?" + urlencode(params)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10)) as c:
            r = await c.get(url)
        if r.status_code >= 400:
            return {"ok": False, "error": f"도매꾹 조회 실패({r.status_code})."}
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"도매꾹 연결 오류: {type(exc).__name__}"}
    root = data.get("domeggook") or data
    if isinstance(root, dict) and root.get("errors"):
        return {"ok": False, "error": f"도매꾹 API 오류: {root['errors']}"}
    lst = ((root.get("list") or {}).get("item")) or []
    if isinstance(lst, dict):
        lst = [lst]
    items = []
    seen = set()
    for rank, it in enumerate(lst):
        if not isinstance(it, dict):
            continue
        no = str(it.get("no") or "").strip()
        title = (it.get("title") or "").strip()
        thumb = (it.get("thumb") or "").strip()
        try:
            price = int(it.get("price"))
        except Exception:  # noqa: BLE001
            price = None
        if not no or len(title) < 2 or not price or price <= 0 or not thumb:
            continue
        if no in seen:
            continue
        seen.add(no)
        deli = it.get("deli") or {}
        items.append({"no": no, "title": title, "price": price, "thumb": thumb,
                      "url": (it.get("url") or "").strip(),
                      "deli_who": deli.get("who"), "deli_fee": deli.get("fee"),
                      "unitQty": it.get("unitQty"), "comOnly": it.get("comOnly"),
                      "pop_rank": rank})       # 인기순 순위(0=최상위)
    if not items:
        return {"ok": True, "items": [], "keyword": kw,
                "note": "인기 상품을 찾지 못했어요(검색어 확인)."}
    # 2) 네이버 수요(공식 검색어 트렌드 1회)
    demand = None
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if cid and csec:
        try:
            start, end = _date_range(12)
            body = {"startDate": start, "endDate": end, "timeUnit": "month",
                    "keywordGroups": [{"groupName": kw, "keywords": [kw]}]}
            ddata, _ = await _hub_datalab_probe(cid, csec, _TREND_PATHS, body)
            res = (ddata.get("results") or [])
            if res:
                t = _trend_of(res[0].get("data") or [])
                demand = {"rise": t.get("rise", 0.0), "rollover": t.get("rollover", False)}
        except Exception:  # noqa: BLE001
            demand = None
    # 3) 기회점수: 인기순위(도매꾹) + 가격 경쟁력 + 무료배송 + 수요가점(공통)
    prices = [x["price"] for x in items if x["price"]]
    med = sorted(prices)[len(prices) // 2] if prices else 0
    demand_bonus = 0.0
    if demand:
        demand_bonus = max(0.0, min(20.0, demand["rise"] / 40.0 * 20.0))
        if demand.get("rollover"):
            demand_bonus -= 10.0
    scored = []
    for it in items:
        s = 0.0
        reasons = []
        # 인기순위: 상위일수록 높은 점수(0위=40 → 하위로 갈수록 감소)
        pop = max(0.0, 40.0 - it["pop_rank"] * 0.8)
        s += pop
        if it["pop_rank"] < 10:
            reasons.append(f"인기 상위 {it['pop_rank']+1}위")
        # 가격 경쟁력
        if it["price"] and med:
            if it["price"] <= med:
                s += 20.0
                reasons.append("가격 경쟁력")
            else:
                s += 6.0
        # 무료배송
        if it.get("deli_who") == "S":
            s += 8.0
            reasons.append("무료배송")
        s += demand_bonus
        if demand and demand.get("rise", 0) >= 10:
            reasons.append(f"네이버 수요 상승({round(demand['rise'])}%)")
        it2 = dict(it)
        it2["score"] = round(max(0.0, min(100.0, s + 15.0)))
        it2["why"] = reasons
        scored.append(it2)
    scored.sort(key=lambda x: -x["score"])
    return {"ok": True, "items": scored[:40], "keyword": kw, "demand": demand,
            "market": params["market"], "scanned": len(items)}


# ===== 상세페이지 공통 (톤 4종 · 상품정보고시 · 이미지 다중배치 · 재생성) =====
_DETAIL_TONES = {
    "emotion": {   # 감성형
        "label": "감성형",
        "hint": "따뜻하고 감성적인 톤. 일상의 장면과 감정을 그리듯 부드럽게, 소비자의 마음을 어루만지는 문장으로.",
        "accent": "#e5738a", "accent2": "#fff2f5", "font": "'Noto Serif KR','나눔명조',serif",
        "h1w": "700", "radius": "16px", "hstyle": "italic",
    },
    "trust": {     # 신뢰형
        "label": "신뢰형",
        "hint": "차분하고 전문적인 톤. 사양·근거·품질을 또박또박, 과장 없이 신뢰를 주는 문장으로.",
        "accent": "#2b7fff", "accent2": "#eef4ff", "font": "'맑은 고딕','Malgun Gothic',sans-serif",
        "h1w": "800", "radius": "8px", "hstyle": "normal",
    },
    "value": {     # 가성비형
        "label": "가성비형",
        "hint": "활기차고 직접적인 톤. 가격 대비 이득·혜택을 강조하고 지금 사야 하는 이유를 명확하게.",
        "accent": "#ff7a00", "accent2": "#fff5eb", "font": "'맑은 고딕','Malgun Gothic',sans-serif",
        "h1w": "900", "radius": "10px", "hstyle": "normal",
    },
    "minimal": {   # 미니멀
        "label": "미니멀",
        "hint": "군더더기 없이 짧고 담백한 톤. 핵심만, 여백을 살린 절제된 문장으로.",
        "accent": "#222222", "accent2": "#f4f5f6", "font": "'맑은 고딕','Malgun Gothic',sans-serif",
        "h1w": "700", "radius": "4px", "hstyle": "normal",
    },
}


def _dtone(t):
    return _DETAIL_TONES.get((t or "trust"), _DETAIL_TONES["trust"])


# ✨ v188 프리미엄 디자인 팔레트 — 웹 UI 같은 원색 대신 차분한 브랜드색 + 아주 옅은 바탕
_PACC = {"trust": "#1f3b63", "emotion": "#a85d68", "value": "#d9531e", "minimal": "#111111"}
_PSOFT = {"trust": "#f3f5f8", "emotion": "#f9f4f3", "value": "#fbf4ef", "minimal": "#f5f5f5"}
_PFONT = "'Pretendard','Apple SD Gothic Neo','Noto Sans KR','Malgun Gothic','맑은 고딕',sans-serif"
_PFONT_SERIF = "'Noto Serif KR','Nanum Myeongjo','나눔명조','Apple SD Gothic Neo',serif"
# 섹션 제목 → 작은 영문 라벨(프리미엄)
_PEN = {"제품 정보": "INFORMATION", "구성·옵션": "OPTION", "고객 후기": "REVIEW", "자주 묻는 질문": "Q&A",
        "한눈에 비교": "COMPARE", "제품 상세 설명": "DETAIL", "이럴 때 좋아요": "SCENE",
        "이런 점이 좋아요": "POINT", "한눈에 체크하세요": "CHECK", "주요 특징": "FEATURE",
        "하나씩 짚어볼게요": "STORY", "핵심은 딱 세 가지": "KEY POINT"}
# 유형별로 숨길 수 있는 블록(키, 이름)
_HIDEABLE = {"facts": "숫자 강조띠", "quote": "핵심 한 줄", "fors": "추천 대상", "notfors": "비추천 대상", "note": "판매자 메모", "steps": "사용 순서", "intro": "도입 문단", "scene": "사용 상황", "tip": "활용 팁", "details": "상세 설명",
             "spec": "제품 정보표", "compare": "비교표", "reviews": "후기", "faq": "FAQ", "close": "마무리 문구"}


# 🧩 상세페이지 유형 자동 추천 — 상품명·카테고리 키워드로 어울리는 레이아웃을 고른다
# (bucket, layout, 분류 이름, 추천 이유, 키워드들)
_LAYOUT_RULES = [
    ("tech", "spec", "전자·공구",
     "전자·공구류는 구매 전에 사양을 비교하는 고객이 많아 사양표를 맨 위에 두는 스펙정보형이 잘 맞아요",
     ["전자", "디지털", "가전", "컴퓨터", "pc", "노트북", "태블릿", "모니터", "키보드", "마우스", "이어폰",
      "헤드폰", "헤드셋", "블루투스", "스피커", "충전", "케이블", "배터리", "어댑터", "usb", "hdmi", "led",
      "조명", "센서", "카메라", "스마트", "공구", "드릴", "전동", "렌치", "부품", "자동차용품", "차량용",
      "계측", "측정", "멀티탭", "선풍기", "히터", "가습기", "청소기", "전기"]),
    ("beauty", "problem", "뷰티",
     "뷰티는 '어떤 고민에 쓰는지'가 먼저 와닿아야 해서 고민→해결 흐름의 문제해결형이 잘 맞아요(효능 과장은 금지)",
     ["화장품", "뷰티", "스킨", "로션", "크림", "세럼", "에센스", "앰플", "클렌징", "선크림", "마스크팩",
      "헤어", "샴푸", "트리트먼트", "바디", "네일", "메이크업", "립"]),
    ("food", "check", "식품",
     "식품은 원재료·보관법·유통기한처럼 빠르게 확인할 항목이 많아 체크리스트형이 편해요",
     ["식품", "간식", "과자", "음료", "커피", "원두", "녹차", "홍차", "티백", "견과", "젤리", "반찬",
      "밀키트", "소스", "양념", "건강식품", "영양제", "쌀", "과일", "정육", "수산", "떡", "빵"]),
    ("fashion", "check", "패션",
     "패션은 사이즈·소재·핏을 한눈에 확인하고 결정하는 경우가 많아 체크리스트형이 잘 맞아요",
     ["의류", "패션", "티셔츠", "셔츠", "맨투맨", "후드", "니트", "원피스", "스커트", "바지", "팬츠",
      "청바지", "자켓", "재킷", "코트", "패딩", "신발", "운동화", "슬리퍼", "샌들", "가방", "백팩", "지갑",
      "모자", "양말", "속옷", "잠옷", "레깅스"]),
    ("furniture", "spec", "가구",
     "가구는 크기·소재·하중 같은 사양이 구매 결정을 좌우해서 사양표를 먼저 보여주는 스펙정보형이 잘 맞아요",
     ["가구", "의자", "책상", "테이블", "식탁", "수납장", "서랍", "선반", "책장", "소파", "침대", "스툴",
      "행거", "옷장", "협탁"]),
    ("gift", "story", "선물·감성",
     "선물·감성 소품은 분위기와 '이야기'가 구매를 이끌어서 스토리형이 잘 맞아요",
     ["선물", "기념", "커플", "감성", "인테리어", "소품", "캔들", "향초", "디퓨저", "향수", "꽃", "액자",
      "엽서", "쥬얼리", "주얼리", "반지", "목걸이", "귀걸이", "팔찌", "핸드메이드", "수제", "키링", "인형",
      "문구", "다이어리", "포토"]),
    ("living", "problem", "생활·기능성",
     "생활·기능성 용품은 '불편 → 해결'을 보여줄 때 가장 잘 팔려서 문제해결형을 골랐어요",
     ["생활", "주방", "욕실", "청소", "수납", "정리", "세탁", "빨래", "행주", "수세미", "밀폐", "냄비",
      "프라이팬", "텀블러", "물병", "반려", "강아지", "고양이", "캠핑", "등산", "운동", "홈트", "매트",
      "쿠션", "베개", "이불", "침구", "방석", "우산", "거치대", "보관", "용기"]),
]


def _recommend_layout(name="", category="", extra=""):
    """카테고리(가중치 3)·상품명(2)·기타 정보(1)에 든 키워드 점수로 유형 추천.
    반환: {layout, bucket, label, reason}. 아무것도 안 잡히면 기본형."""
    cat = (category or "").lower()
    nm = (name or "").lower()
    ex = (extra or "").lower()[:600]
    best, best_score = None, 0
    for rule in _LAYOUT_RULES:
        score = 0
        for kw in rule[4]:
            if kw in cat:
                score += 3
            if kw in nm:
                score += 2
            if kw in ex:
                score += 1
        if score > best_score:
            best, best_score = rule, score
    if not best:
        return {"layout": "basic", "bucket": "etc", "label": "기타",
                "reason": "분류를 특정하기 어려워 기본형으로 시작해요 — 위 '유형'에서 바꿔보세요"}
    return {"layout": best[1], "bucket": best[0], "label": best[2], "reason": best[3]}


def _dg_category(root, basis, _cd=None):
    """도매꾹 getItemView의 카테고리 이름을 방어적으로 꺼낸다('대분류 > 중분류 > …').
    응답 구조가 버전마다 달라서 category/cat/categories 노드 아래의 name·title·#cdata를 모두 훑는다. 못 찾으면 ''."""
    names = []

    def _walk(v, depth=0):
        if depth > 5 or len(names) >= 6:
            return
        if isinstance(v, dict):
            for k in ("name", "title", "nm", "#cdata", "cdata"):
                x = v.get(k)
                if isinstance(x, (str, int)) and str(x).strip() and not str(x).strip().isdigit():
                    if str(x).strip() not in names:
                        names.append(str(x).strip())
                elif isinstance(x, dict):
                    _walk(x, depth + 1)
            for k in ("parents", "parent", "elem", "current", "list", "item", "depth1", "depth2", "depth3", "depth4"):
                if k in v:
                    _walk(v[k], depth + 1)
        elif isinstance(v, list):
            for it in v:
                _walk(it, depth + 1)
        elif isinstance(v, str) and v.strip() and not v.strip().isdigit():
            if v.strip() not in names:
                names.append(v.strip())
    for src in (basis or {}, root or {}):
        for key in ("category", "cat", "categories", "categoryName", "cate"):
            if isinstance(src, dict) and src.get(key):
                _walk(src.get(key))
        if names:
            break
    return " > ".join(names[:5])


def _pick_layout(req_layout, rec):
    """요청 layout이 'auto'(기본)면 추천을, 아니면 지정한 유형을 쓴다."""
    lay = (req_layout or "auto").strip()
    if lay == "auto" or lay not in ("basic", "problem", "spec", "story", "check"):
        return rec["layout"]
    return lay


# 전자상거래법 상품정보제공고시 — 카테고리별 필수 항목(프런트가 입력폼 렌더용으로 GET)
_NOTICE_TEMPLATES = {
    "wear":     {"label": "의류/패션",
                 "fields": ["제품 소재", "색상", "치수", "제조자(수입자)", "제조국",
                            "세탁방법 및 취급시 주의사항", "제조연월", "품질보증기준",
                            "A/S 책임자와 전화번호"]},
    "cosmetic": {"label": "화장품",
                 "fields": ["용량 또는 중량", "제품 주요 사양", "사용기한(개봉 후 사용기간)", "사용방법",
                            "화장품법상 기재 성분", "제조업자/책임판매업자", "제조국", "기능성 화장품 여부",
                            "사용시 주의사항", "품질보증기준", "소비자상담 전화번호"]},
    "food":     {"label": "식품",
                 "fields": ["제품명", "식품의 유형", "생산자 및 소재지", "제조연월일/유통기한",
                            "용량(중량)", "원재료명 및 함량", "영양성분", "유전자변형식품 여부",
                            "수입 여부(수입국)", "소비자상담 전화번호"]},
    "elec":     {"label": "가전/전자",
                 "fields": ["품명 및 모델명", "KC 인증정보", "정격전압/소비전력", "에너지소비효율등급",
                            "동일모델 출시년월", "제조자(수입자)", "제조국", "크기", "품질보증기준",
                            "A/S 책임자와 전화번호"]},
    "life":     {"label": "생활용품/잡화",
                 "fields": ["품명 및 모델명", "크기·용량·수량", "제조국", "제조자(수입자)",
                            "상품별 세부사양", "품질보증기준", "A/S 책임자와 전화번호"]},
    "etc":      {"label": "기타(공통)",
                 "fields": ["품명 및 모델명", "인증/허가 사항", "제조국(원산지)", "제조자/수입자",
                            "A/S 책임자와 전화번호", "품질보증기준"]},
}


def _detail_prompt(ctx, target, tone_key, only="", length="rich", counts=None):
    """톤 지시문을 포함한 Gemini 프롬프트. only=부분 재생성, length=간단/표준/풍부.
    v188: '사고 싶게, AI 티 안 나게' — 짧은 문장·사실·숫자 위주, 좋은/나쁜 예시를 직접 보여준다."""
    T = _dtone(tone_key)
    base = ("너는 스마트스토어에서 10년째 직접 물건을 파는 판매자야. 이 상품을 직접 써본 사람으로서, "
            "매장에서 손님에게 설명하듯 상세페이지 문구를 처음부터 새로 써줘(남의 글 베끼기 금지).\n\n"
            f"[상품 정보]\n{ctx}\n"
            + (f"[타깃 고객]\n{target}\n" if (target or '').strip() else "")
            + f"\n[요구 톤: {T['label']}]\n{T['hint']}\n"
            + "\n[사고 싶게 만드는 원칙]\n"
              "1) 형용사 대신 사실: '튼튼한' 말고 '두께 3mm 스테인리스'. 상품 정보에 있는 숫자·소재·구성을 최대한 끌어와.\n"
              "2) 기능 말고 장면: '360도 회전'보다 '내비는 가로로, 통화는 세로로 바로 돌려요'.\n"
              "3) 고객의 불편을 먼저 짚고, 그다음 해결. 단, 겁주거나 과장하지 마.\n"
              "4) 단점·한계도 하나쯤 솔직하게(예: '원형 송풍구에는 고정이 약해요'). 솔직함이 신뢰를 만든다.\n"
              "4-1) 판매자 본인의 말투로: '저희는 ~해서 이걸 골랐어요'처럼 사람이 고른 이유가 보이게. 단, 써봤다는 경험·후기·판매량은 지어내지 마.\n"
              "5) 짧게. 한 문장은 40자 안팎, 한 덩어리는 2~3문장. 모바일에서 한눈에 읽혀야 해.\n"
              "6) 과장·허위·근거 없는 효능(최고/1위/100%/의학적 효과/부작용 없음) 절대 금지. 없는 사실 지어내지 마.\n"
              "\n[AI가 쓴 티 나는 표현 — 절대 쓰지 마]\n"
              "선사합니다, 자랑합니다, 경험해 보세요, 만나보세요, 완벽한, 혁신적인, 특별한, 차원이 다른, 끝판왕, 필수템, "
              "일상을 바꿔줄, 당신의, 여러분, 놓치지 마세요, 지금 바로, 더 이상 고민하지 마세요, ~의 완벽한 조화, "
              "~로 거듭납니다, 감성 가득, 퀄리티, 강력 추천, 만족도 최고. 이모지·느낌표 남발 금지(전체에서 느낌표 2개 이하, 이모지 0개).\n"
              "같은 어미(~요/~습니다)를 연달아 세 번 이상 쓰지 말고, 문장 길이를 섞어. 존댓말은 유지.\n"
              "\n[나쁜 예 → 좋은 예]\n"
              "✗ 혁신적인 기술로 당신의 일상을 완벽하게 바꿔드립니다! → ○ 송풍구에 끼우고 폰만 올리면 끝이에요. 과속방지턱에서도 덜렁거리지 않아요.\n"
              "✗ 프리미엄 소재로 특별한 경험을 선사합니다. → ○ 손 닿는 곳은 전부 실리콘이라, 폰 옆면에 자국이 덜 남아요.\n"
              "✗ 지금 바로 만나보세요! → ○ 매일 쓰는 물건이라 하나 제대로 고르면 오래 편해요.\n")
    pfmt = ("짧은 소제목(6~16자 — '강력한 고정력'처럼 명사만 나열하지 말고 장면이 보이게: 예 '과속방지턱에서도 그대로') "
            "|| 설명 2문장(사실 근거 포함, 한 문장 45자 이내)")
    if only == "head":
        return base + "\nHEAD는 14~24자, 쉼표로 두 호흡, 가장 중요한 단어 하나만 [ ]로 감싸(예: '한 손으로 3초, [흔들림 없이] 고정'). [출력] 정확히 아래 한 줄만:\nHEAD: (헤드라인)"
    if only == "points":
        return base + ("\n[출력] 정확히 아래 5줄만(마크다운·기호 금지):\n"
                       + "\n".join(f"POINT: ({pfmt})" for _ in range(5)))
    if only == "scene":
        return base + "\n[출력] 정확히 아래 한 줄만:\nSCENE: (누가·언제 쓰는지 짧은 문장 3개를 한 줄로, 각 35자 이내)"
    if only == "close":
        return base + "\n[출력] 정확히 아래 한 줄만:\nCLOSE: (짧은 문장 2개로 담담하게 마무리 — 재촉하는 말 금지)"
    # 분량 프리셋 — v192: '풍부'는 블록 수와 문장 수를 함께 늘린다(한 문장은 여전히 짧게)
    if length == "simple":
        np, nd, nf, dlen = 3, 1, 2, "2~3문장"
    elif length == "standard":
        np, nd, nf, dlen = 5, 3, 4, "3~4문장"
    else:  # rich
        np, nd, nf, dlen = 7, 4, 6, "4~5문장(소재·만듦새·쓰는 느낌·관리법처럼 구체적으로)"
        pfmt = ("짧은 소제목(6~16자 — 명사 나열 말고 장면이 보이게: 예 '과속방지턱에서도 그대로') "
                "|| 설명 3문장(① 무엇이 ② 어떻게 ③ 그래서 고객에게 뭐가 좋은지 — 사실 근거 포함, 한 문장 45자 이내)")
        # v193 '풍부' 안에서 개수 미세 조절(장점 5~8 · 상세 2~5 · FAQ 3~8)
        if isinstance(counts, dict):
            def _ci(k, lo, hi, dv):
                try:
                    return max(lo, min(hi, int(counts.get(k, dv))))
                except Exception:  # noqa: BLE001
                    return dv
            np, nd, nf = _ci("points", 5, 8, np), _ci("details", 2, 5, nd), _ci("faqs", 3, 8, nf)
    lines = ["\n[출력 형식] 정확히 아래 태그로 써줘(마크다운·기호·번호 금지):",
             "TITLE: (스마트스토어 등록용 상품명 — 핵심 키워드 포함, 공백 포함 40자 내외)",
             "TITLE: (다른 각도의 등록용 상품명 한 줄)",
             "LABEL: (이 상품이 무엇인지 2~4단어 키워드 — 예: 차량용 휴대폰 거치대. 수식어·광고 문구 금지)",
             "HEAD: (헤드라인 14~24자, 쉼표로 두 호흡. 무엇이 어떻게 좋은지 구체적으로. 가장 중요한 단어 하나(2~8자)만 [ ]로 감싸)",
             "SUB: (헤드라인을 받쳐주는 한 문장, 40자 이내)"]
    if length != "simple":
        lines.append("INTRO: (고객이 겪는 상황으로 시작하는 짧은 문장 " + ("4~5개" if length == "rich" else "3~4개") + "를 한 줄로. 각 35자 이내)")
    for i in range(np):
        lines.append(f"POINT: ({pfmt})")
    lines.append(f"(POINT {np}줄은 모두 서로 다른 장점으로 — 겹치는 내용 금지)")
    for i in range(nd):
        lines.append(f"DETAIL: (이 문단의 소제목 8~14자 || 소재·제작·디테일·사용감·관리 중 하나를 골라 {dlen}. 앞 문단과 다른 주제)")
    lines.append("SPEC: (항목: 값 형태를 콤마로 구분 — 예: 재질: ABS, 크기: 약 7x6x9cm, 중량: 약 85g. 상품 정보에 있는 것만)")
    lines.append("SCENE: (누가·언제 쓰는지 짧은 문장 " + ("4개" if length == "rich" else "3개") + "를 한 줄로, 각 35자 이내)")
    if length != "simple":
        lines.append("TIP: (사용·관리·보관 요령 " + ("3~4문장" if length == "rich" else "2~3문장") + ")")
        lines.append("QUOTE: (이 상품을 한 문장으로 — 페이지 가운데 크게 들어갈 담백한 문장 20~30자. 과장·느낌표 금지)")
    for _ in range(3):
        lines.append("FACT: (숫자+단위 || 그 숫자가 뜻하는 것 8자 이내 — 예: 500ml || 넉넉한 용량. 상품 정보에 실제로 있는 숫자만, 없으면 이 줄은 쓰지 마)")
    if length != "simple":
        for _ in range(3):
            lines.append("FOR: (이런 분께 추천 — 구체적인 대상·상황 한 줄, 20자 이내)")
        lines.append("NOTFOR: (솔직하게, 이 상품이 맞지 않는 경우 한 줄 — 상품 정보에 근거, 20자 이내)")
        lines.append("NOTFOR: (맞지 않는 경우 하나 더 — 없으면 이 줄은 쓰지 마)")
        lines.append("NOTE: (판매자 메모 — 매장에서 손님에게 건네듯 '고를 때 이것만 보세요' 같은 조언 2~3문장. "
                     "직접 써봤다·후기·판매량 같은 사실을 지어내지 마)")
        lines.append("STEP: (사용 순서 1단계, 12자 이내)")
        lines.append("STEP: (2단계)")
        lines.append("STEP: (3단계 — 순서가 의미 없는 상품이면 STEP 줄은 모두 쓰지 마)")
    for i in range(nf):
        lines.append(f"FAQ: (구매 전 실제로 묻는 질문{i+1}) || (솔직하고 구체적인 답 1~2문장)")
    lines.append("CLOSE: (짧은 문장 2개로 담담하게 마무리 — '지금 바로'·'놓치지 마세요' 같은 재촉 금지)")
    lines.append("모든 항목은 상품 정보에 근거해. 모르는 건 쓰지 말고, 과장·허위(최고/1위/의학효과) 금지.")
    return base + "\n".join(lines)


# 🧹 v188 'AI 말투' 정리 — Gemini가 지시를 어겨도 결과에서 한 번 더 걸러낸다(뜻이 바뀌지 않는 것만)
_AI_PHRASES = [("경험해 보세요", "써보세요"), ("경험해보세요", "써보세요"), ("만나보세요", "확인해 보세요"),
               ("선사합니다", "줍니다"), ("선사해요", "줘요"), ("자랑합니다", "갖췄습니다"), ("자랑해요", "갖췄어요"),
               ("지금 바로 ", "")]
_AI_CTA = ("놓치지 마세요", "더 이상 고민하지 마세요", "후회하지 않으실", "망설이지 마세요")


def _humanize_text(t):
    import re as _re
    s = str(t or "")
    # 이모지·장식 기호 제거(한글·숫자·일반 문장부호는 그대로)
    s = _re.sub("[\U0001F000-\U0001FAFF☀-➿️‍]", "", s)
    s = s.replace("**", "").replace("__", "")
    s = _re.sub(r"!{2,}", "!", s)
    s = _re.sub(r"\?{2,}", "?", s)
    s = _re.sub(r"^\s*(?:[-•·*]|\d+[.)])\s+", "", s)
    for a, b in _AI_PHRASES:
        s = s.replace(a, b)
    # 재촉형 문장은 다른 문장이 있을 때만 통째로 뺀다(문장이 하나뿐이면 그대로 둠)
    ss = [x for x in _re.split(r"(?<=[.!?])\s+", s.strip()) if x.strip()]
    if len(ss) >= 2:
        keep = [x for x in ss if not any(c in x for c in _AI_CTA)]
        if keep:
            s = " ".join(keep)
    s = s.strip().strip('"“”').strip()
    return _re.sub(r"\s{2,}", " ", s)


def _humanize_sections(sec):
    for k in ("head", "sub", "intro", "spec", "scene", "tip", "close", "label", "note", "quote"):
        if sec.get(k):
            sec[k] = _humanize_text(sec[k])
    if sec.get("head"):
        h = sec["head"].rstrip(".!")
        k = h.find("[")
        if k >= 0:   # 강조 표시는 첫 번째 [ ] 하나만 남김
            j = h.find("]", k)
            h = h[:j + 1] + h[j + 1:].replace("[", "").replace("]", "") if j > k else h.replace("[", "").replace("]", "")
        sec["head"] = h
    # POINT '제목 || 설명' → '제목 — 설명'(클래식 카드·편집칸에서도 자연스럽게 보이게)
    pts = []
    for p in sec.get("points") or []:
        p = _humanize_text(p)
        if "||" in p:
            a, b = p.split("||", 1)
            p = f"{a.strip()} — {b.strip()}" if b.strip() else a.strip()
        if p:
            pts.append(p)
    sec["points"] = pts
    _dh = sec.get("detail_heads") or []
    _pairs = [(str(d), (_dh[i] if i < len(_dh) else "")) for i, d in enumerate(sec.get("details") or []) if str(d).strip()]
    sec["details"] = [_humanize_text(d) for d, _h in _pairs]
    if any(h for _d, h in _pairs):
        sec["detail_heads"] = [_humanize_text(h) for _d, h in _pairs]
    sec["fors"] = [_humanize_text(x)[:30] for x in (sec.get("fors") or []) if str(x).strip()]
    sec["steps"] = [_humanize_text(x)[:24] for x in (sec.get("steps") or []) if str(x).strip()]
    sec["notfors"] = [_humanize_text(x)[:30] for x in (sec.get("notfors") or []) if str(x).strip()]
    sec["facts"] = [{"n": _humanize_text(f.get("n", "")), "t": _humanize_text(f.get("t", ""))}
                    for f in (sec.get("facts") or []) if isinstance(f, dict) and str(f.get("n", "")).strip()]
    sec["faqs"] = [{"q": _humanize_text(f.get("q", "")), "a": _humanize_text(f.get("a", ""))}
                   for f in (sec.get("faqs") or []) if isinstance(f, dict)]
    return sec


async def _detail_gemini(gkey, prompt, temperature=0.9):
    """상세페이지 문구 Gemini 호출 공통. (text, None) 또는 (None, err_dict).
    - 500/503(구글 서버 일시 오류)·연결 오류는 짧게 자동 재시도한다.
    - 상태코드별로 정확한 안내(키 문제 vs 구글 서버 문제 vs 한도)를 돌려준다."""
    import httpx, asyncio
    gurl = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 12288,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    last_transient = ""       # 마지막 일시 오류 설명(재시도 다 실패했을 때 안내용)
    attempts = 3              # 최초 1회 + 재시도 2회
    for i in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=45, write=10, pool=10)) as c:
                gr = await c.post(gurl, headers={"x-goog-api-key": gkey, "Content-Type": "application/json"},
                                  json=body)
        except Exception as exc:  # noqa: BLE001 — 연결/타임아웃 등 일시 오류 → 재시도
            last_transient = f"연결 오류({type(exc).__name__})"
            if i < attempts - 1:
                await asyncio.sleep(1.2 * (i + 1)); continue
            return None, {"ok": False, "error": "Gemini 연결이 불안정해요 — 잠시 후 다시 시도해주세요. (키 문제 아님)"}
        sc = gr.status_code
        if sc == 200:
            break
        # 키·요청 문제(재시도 무의미) — 명확히 구분
        if sc in (400, 401):
            snippet = (gr.text or "")[:160]
            return None, {"ok": False, "error": f"Gemini 키 오류({sc}) — API 키가 올바른지 확인하세요(AIza…로 시작). {snippet}"}
        if sc == 403:
            return None, {"ok": False, "error": "Gemini 403 — 키 권한/지역 제한이거나 API가 비활성일 수 있어요. 키 설정을 확인하세요."}
        if sc == 429:
            return None, {"ok": False, "error": "Gemini 무료 한도 초과(429) — 잠시 후 다시 시도해주세요."}
        # 구글 서버 일시 오류(500·503 등) → 짧게 재시도
        if sc in (500, 502, 503, 504):
            last_transient = f"구글 서버 오류({sc})"
            if i < attempts - 1:
                await asyncio.sleep(1.2 * (i + 1)); continue
            return None, {"ok": False, "error": f"Gemini 서버 일시 오류({sc}) — 구글 쪽 문제예요(키 문제 아님). 잠시 후 다시 시도하면 대부분 됩니다."}
        # 그 밖의 4xx
        return None, {"ok": False, "error": f"Gemini 오류({sc}) — 잠시 후 다시 시도해주세요."}
    try:
        gd = gr.json()
        cand = (gd.get("candidates") or [{}])[0]
        parts = ((cand.get("content") or {}).get("parts") or [])
        text = "\n".join(pt.get("text", "") for pt in parts).strip()
        # 응답이 비었을 때 원인(안전차단·토큰초과) 안내
        if not text:
            fr = cand.get("finishReason") or ""
            if fr == "MAX_TOKENS":
                return None, {"ok": False, "error": "응답이 너무 길어 잘렸어요 — '분량'을 줄이거나 다시 시도해주세요."}
            if fr in ("SAFETY", "RECITATION", "BLOCKLIST"):
                return None, {"ok": False, "error": f"Gemini가 응답을 차단했어요({fr}) — 상품명·문구를 조금 바꿔 다시 시도해주세요."}
    except Exception:  # noqa: BLE001
        return None, {"ok": False, "error": "Gemini 응답 해석 실패 — 잠시 후 다시 시도해주세요."}
    if not text:
        return None, {"ok": False, "error": "문구를 만들지 못했어요. 다시 시도해주세요."}
    return text, None


def _parse_detail(text):
    """TITLE/HEAD/SUB/INTRO/POINT/DETAIL/SPEC/SCENE/TIP/FAQ/CLOSE 태그 파싱."""
    head = sub = scene = close = spec = tip = intro = label = ""
    points = []
    facts, fors, steps, notfors = [], [], [], []
    note = quote = ""
    dheads = []
    titles = []
    details = []
    faqs = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s.startswith("TITLE:"): titles.append(s[6:].strip())
        elif s.startswith("HEAD:"): head = s[5:].strip()
        elif s.startswith("LABEL:"): label = s[6:].strip()
        elif s.startswith("FACT:"):
            b_ = s[5:].strip()
            if "||" in b_:
                n_, t_ = b_.split("||", 1)
                if n_.strip():
                    facts.append({"n": n_.strip()[:12], "t": t_.strip()[:16]})
        elif s.startswith("NOTFOR:"):
            if s[7:].strip(): notfors.append(s[7:].strip())
        elif s.startswith("NOTE:"):
            note = s[5:].strip()
        elif s.startswith("FOR:"):
            if s[4:].strip(): fors.append(s[4:].strip())
        elif s.startswith("STEP:"):
            if s[5:].strip(): steps.append(s[5:].strip())
        elif s.startswith("SUB:"): sub = s[4:].strip()
        elif s.startswith("INTRO:"): intro = s[6:].strip()
        elif s.startswith("POINT:"): points.append(s[6:].strip())
        elif s.startswith("DETAIL:"):
            b_ = s[7:].strip()
            if "||" in b_:
                h_, t_ = b_.split("||", 1)
                if t_.strip():
                    dheads.append(h_.strip()[:20]); details.append(t_.strip())
            elif b_:
                dheads.append(""); details.append(b_)
        elif s.startswith("QUOTE:"): quote = s[6:].strip()
        elif s.startswith("SPEC:"): spec = s[5:].strip()
        elif s.startswith("SCENE:"): scene = s[6:].strip()
        elif s.startswith("TIP:"): tip = s[4:].strip()
        elif s.startswith("FAQ:"):
            body = s[4:].strip()
            if "||" in body:
                q, a = body.split("||", 1)
                faqs.append({"q": q.strip(), "a": a.strip()})
            elif body:
                faqs.append({"q": body, "a": ""})
        elif s.startswith("CLOSE:"): close = s[6:].strip()
    return _humanize_sections({"head": head, "sub": sub, "intro": intro, "points": points, "details": details,
                               "spec": spec, "scene": scene, "tip": tip, "close": close,
                               "faqs": faqs, "titles": [t for t in titles if t], "label": label[:24],
                               "facts": facts[:4], "fors": fors[:4], "steps": steps[:4],
                               "notfors": notfors[:3], "note": note, "quote": quote[:40],
                               "detail_heads": dheads if any(dheads) else []})


def _guide_html(guide, e, acc, acc2, radius):
    """구매 안내(배송·교환·환불) 블록 — 줄 단위 텍스트를 표로. '제목: 내용' 형식이면 2열."""
    if not guide or not str(guide).strip():
        return ""
    rows = ""
    for ln in str(guide).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ":" in ln:
            k, v = ln.split(":", 1)
            rows += (f'<tr><th style="text-align:left;padding:10px 12px;background:{acc2};font-weight:700;'
                     f'width:28%;border-bottom:1px solid #eee;font-size:13px;color:#333">{e(k.strip())}</th>'
                     f'<td style="padding:10px 12px;border-bottom:1px solid #eee;font-size:13px;line-height:1.6">{e(v.strip())}</td></tr>')
        else:
            rows += (f'<tr><td colspan="2" style="padding:10px 12px;border-bottom:1px solid #eee;'
                     f'font-size:13px;line-height:1.6">{e(ln)}</td></tr>')
    return (f'<div style="margin:36px 0 8px"><h2 style="font-size:16px;font-weight:800;margin:0 0 10px;'
            f'color:#333">🚚 구매 안내 (배송·교환·환불)</h2>'
            f'<table style="width:100%;border-collapse:collapse;border:1px solid #eee;border-radius:8px;'
            f'overflow:hidden">{rows}</table></div>')


def _reviews_html(reviews, e, acc, acc2, radius, sec_fn=None):
    """판매자가 직접 입력한 후기 블록 — 줄 단위 '별점|작성자|내용'. 별점 없으면 5점."""
    if not reviews or not str(reviews).strip():
        return ""
    items = []
    for ln in str(reviews).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = [p.strip() for p in ln.split("|")]
        stars, nm, txt = 5, "", ""
        if len(parts) >= 3:
            try:
                stars = max(1, min(5, int("".join(ch for ch in parts[0] if ch.isdigit()) or 5)))
            except Exception:  # noqa: BLE001
                stars = 5
            nm, txt = parts[1], parts[2]
        elif len(parts) == 2:
            nm, txt = parts[0], parts[1]
        else:
            txt = parts[0]
        if txt:
            items.append((stars, nm, txt))
    if not items:
        return ""
    cards = ""
    for stars, nm, txt in items:
        star_str = "★" * stars + "☆" * (5 - stars)
        who = e(nm) if nm else "구매고객"
        cards += (f'<table style="width:100%;border-collapse:separate;border-spacing:0;margin:0 0 12px;'
                  f'background:{acc2};border:1px solid {acc}33;border-radius:12px"><tr>'
                  f'<td style="padding:15px 18px">'
                  f'<div style="color:#f5a623;font-size:16px;letter-spacing:2px;margin-bottom:4px">{star_str}'
                  f'<span style="color:#888;font-size:13px;font-weight:700;margin-left:8px">{who}</span></div>'
                  f'<div style="font-size:15px;line-height:1.7;color:#333">{e(txt)}</div></td></tr></table>')
    sec = sec_fn("💬", "고객 후기") if sec_fn else '<h2 style="font-size:22px;font-weight:800;color:#1a1a1a">💬 고객 후기</h2>'
    # 평균 별점·개수 요약 배지
    avg = round(sum(s for s, _n, _t in items) / len(items), 1)
    avg_stars = "★" * int(round(avg)) + "☆" * (5 - int(round(avg)))
    summary = (f'<table style="width:100%;border-collapse:collapse;margin:0 0 16px"><tr>'
               f'<td style="background:{acc2};border:1px solid {acc}33;border-radius:{radius};padding:14px 20px">'
               f'<span style="color:#f5a623;font-size:20px;letter-spacing:2px;vertical-align:middle">{avg_stars}</span> '
               f'<span style="font-size:20px;font-weight:800;color:#1a1a1a;vertical-align:middle">평균 {avg}점</span> '
               f'<span style="font-size:14px;color:#888;font-weight:700;vertical-align:middle">· 후기 {len(items)}개</span>'
               f'</td></tr></table>')
    return f'<div style="margin:36px 0">{sec}{summary}{cards}</div>'


# 이미지 목록 정리 공용 — http만, 중복 제거, 뻔한 플레이스홀더/아이콘 제외, 최대 cap장
_IMG_JUNK = ("noimage", "no_image", "no-image", "blank.", "spacer.", "1x1.",
             "icon_", "/icon/", "btn_", "_s.gif", "loading.", "dummy")
def _clean_imgs(urls, cap=10):
    out, seen = [], set()
    for u in (urls or []):
        if not isinstance(u, str):
            continue
        u = u.strip()
        if not u.startswith("http"):
            continue
        low = u.lower()
        if any(j in low for j in _IMG_JUNK):        # 플레이스홀더·아이콘 걸러내기
            continue
        # 중복 판정 키: 쿼리스트링(?…) 제거한 경로 기준
        key = low.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
        if len(out) >= cap:
            break
    return out


def _head_plain(h):
    """헤드라인의 강조 표시 [단어] 를 뗀 순수 글자(클래식·텍스트용)."""
    return str(h or "").replace("[", "").replace("]", "")


def _head_mark(h):
    """'… [강조] …' → (순수 글자, 강조 단어 or ''). 첫 번째 [ ]만, 1~12자일 때만 인정."""
    import re as _re
    raw = str(h or "")
    m = _re.search(r"\[([^\[\]]{1,12})\]", raw)
    return _head_plain(raw), (m.group(1).strip() if m else "")


def _p_sents(t):
    """문장 단위로 쪼개기(숫자 속 마침표 15.6 같은 건 공백이 없어서 안 쪼개짐)."""
    import re as _re
    t = str(t or "").strip()
    if not t:
        return []
    return [x.strip() for x in _re.split(r'(?<=[.!?])\s+', t) if x.strip()]


def _p_split(p):
    """장점 한 줄 → (짧은 제목, 설명). '제목 — 설명'·'제목 || 설명'·'제목: 설명'을 먼저 보고,
    없으면 첫 문장이 짧을 때만 제목으로 쓴다(제목이 잘려 '…'로 끝나는 일 없게)."""
    s = str(p or "").strip()
    for sep in ("||", " — ", "—", " - ", ": "):
        k = s.find(sep)
        if 2 <= k <= 34:
            return s[:k].strip(), s[k + len(sep):].strip()
    ss = _p_sents(s)
    if len(ss) >= 2 and len(ss[0]) <= 34:
        return ss[0].rstrip("."), " ".join(ss[1:])
    for sep in (", ", ","):
        k = s.find(sep)
        if 4 <= k <= 26:
            return s[:k].strip(), s[k + 1:].strip()
    return "", s


def _premium_body(L):
    """✨ v188 프리미엄 디자인 조립 — 잘 팔리는 상세페이지의 '구조'만 참고해 새로 짠 것.
    큰 헤드라인 → 대표 사진 → (유형별 도입) → POINT마다 제목·짧은 설명·사진 → 상세 → 정보 → 후기·FAQ → 마무리.
    스마트스토어 호환: table·인라인 스타일만. 반환: (body, header_html) — header_html은 목차 삽입 기준."""
    e = L["e"]; acc = L["acc"]; acc2 = L["acc2"]
    head = L["head"]; sub = L["sub"]; intro = L["intro"]; points = L["points"]
    details = L["details"]; faqs = L["faqs"]; spec = L["spec"]; scene = L["scene"]
    tip = L["tip"]; close = L["close"]; origin = L["origin"]; slots = L["slots"]
    pt_imgs = L["pt_imgs"]; _imgtag = L["_imgtag"]; _sec = L["_sec"]; sections = L["sections"]
    lay = L["_lay"]; order = L["order"]; hide = L["_hide"]
    PAD = "padding:0 6%"
    used = set()

    def br(t):
        return "<br>".join(e(x) for x in _p_sents(t))

    hl_word = [""]

    def seg(t):
        """한 줄 조각을 escape — 강조 단어가 있으면 그 단어만 브랜드색(한 번만)."""
        w = hl_word[0]
        if w and w in t:
            i = t.find(w); hl_word[0] = ""
            return (e(t[:i]) + f'<span style="color:{acc}">{e(w)}</span>' + e(t[i + len(w):]))
        return e(t)

    def hbr(h):
        h, hl_word[0] = _head_mark(h)
        h = h.strip()
        k = h.find(",")
        if 4 <= k <= len(h) - 4:
            return seg(h[:k + 1]) + "<br>" + seg(h[k + 1:].strip())
        if len(h) > 17 and " " in h:
            mid = len(h) // 2
            sp = [i for i, ch in enumerate(h) if ch == " "]
            w = hl_word[0]
            wi = h.find(w) if w else -1
            if wi >= 0:   # 강조 단어 한가운데서 줄이 갈리지 않게
                sp = [i for i in sp if not (wi < i < wi + len(w))] or sp
            cut = min(sp, key=lambda i: abs(i - mid))
            return seg(h[:cut]) + "<br>" + seg(h[cut + 1:])
        return seg(h)

    def pimg(i):
        if 0 <= i < len(pt_imgs) and i not in used:
            used.add(i)
            return f'<div style="margin:40px 0 0">{_imgtag(pt_imgs[i])}</div>'
        return ""

    def sec_wrap(inner, top=96, band=False):
        bg = f"background:{acc2};padding-top:80px;padding-bottom:80px;" if band else ""
        return f'<div style="margin-top:{top}px;{bg}padding-left:6%;padding-right:6%">{inner}</div>'

    # ── 히어로: 짧은 강조선 → 큰 헤드라인(두 줄) → 서브 한 줄 → 대표 사진(가득) ──
    sub_html = (f'<p style="font-size:19px;color:#666;line-height:1.7;margin:24px 0 0;font-weight:400">{e(sub)}</p>'
                if sub else "")
    img0 = slots[0]
    # 헤드라인 위 키워드 라벨 — 사용자 입력(sections.eyebrow, '-'=숨김) > AI 키워드(sections.label) > 분류명(eyebrow 인자)
    _eb = str(sections.get("eyebrow") or "").strip()
    if _eb == "-":
        _eb = ""
    elif not _eb:
        _eb = str(sections.get("label") or "").strip() or str(L.get("eyebrow") or "").strip()
    _eb = _eb[:24]
    eb_html = (f'<div style="font-size:15px;font-weight:700;color:{acc};letter-spacing:2px;margin:0 0 18px">{e(_eb)}</div>'
               if _eb else "")
    header_html = (f'<div style="padding:84px 6% 52px;text-align:center">'
                   f'<div style="width:30px;height:3px;background:{acc};margin:0 auto 28px"></div>{eb_html}'
                   f'<h1 style="font-size:36px;font-weight:800;line-height:1.36;letter-spacing:-1px;color:#111;margin:0">{hbr(head)}</h1>'
                   f'{sub_html}</div>{img0}')

    # 도입 문단 — 한 문장씩 줄바꿈, 가운데, 넉넉한 줄간격
    intro_html = (f'<div style="margin-top:88px;{PAD};text-align:center;font-size:19px;line-height:2;color:#333">{br(intro)}</div>'
                  if intro else "")

    # POINT 쇼케이스 — 라벨, 큰 제목, 짧은 설명, 그 POINT의 사진
    def pshow(start=0, plist=None, style="center"):
        plist = points if plist is None else plist
        out = ""
        for j, p in enumerate(plist):
            i = start + j
            h, b = _p_split(p)
            if style == "story":
                lab = (f'<div style="font-size:46px;font-weight:200;color:{acc};line-height:1;font-family:Georgia,serif">{i + 1:02d}</div>')
                al = "left"
            else:
                lab = (f'<div style="display:inline-block;font-size:14px;font-weight:800;letter-spacing:3px;color:{acc};'
                       f'border-bottom:2px solid {acc};padding-bottom:5px">POINT {i + 1:02d}</div>')
                al = "center"
            ttl = (f'<div style="font-size:30px;font-weight:800;color:#111;line-height:1.42;letter-spacing:-0.6px;margin:20px 0 0">{e(h)}</div>'
                   if h else "")
            desc = (f'<div style="font-size:18px;color:#555;line-height:1.9;margin:18px 0 0">{br(b)}</div>' if b else "")
            im = pimg(i)
            if style == "center" and j % 2 == 1:
                # ✨ v190 리듬 — 짝수번째 POINT는 옅은 바탕 띠 위에(흰 바탕만 이어지는 단조로움 방지)
                out += (f'<div style="margin-top:100px;background:{acc2};padding-top:80px;padding-bottom:{0 if im else 80}px">'
                        f'<div style="{PAD};text-align:{al}">{lab}{ttl}{desc}</div>{im}</div>')
            else:
                out += (f'<div style="margin-top:{100 if j else 80}px;{PAD};text-align:{al}">{lab}{ttl}{desc}</div>{im}')
        return out

    # 스펙형 번호 목록 — 01 제목/설명 + 그 아래 사진
    def numlist():
        if not points:
            return ""
        out = ""
        for i, p in enumerate(points):
            h, b = _p_split(p)
            out += (f'<table style="width:100%;border-collapse:collapse;margin-top:{48 if i else 0}px"><tr>'
                    f'<td style="width:64px;vertical-align:top;font-size:28px;font-weight:300;color:{acc};padding:2px 0 0">{i + 1:02d}</td>'
                    f'<td style="vertical-align:top;border-top:1px solid #222;padding:14px 0 0">'
                    + (f'<div style="font-size:22px;font-weight:800;color:#111;line-height:1.45">{e(h)}</div>' if h else "")
                    + (f'<div style="font-size:17px;color:#666;line-height:1.85;margin-top:8px">{br(b)}</div>' if b else "")
                    + f'</td></tr></table>')
            im = pimg(i)
            if im:      # 사진은 여백 밖으로(가득 폭) — 패딩 div를 닫았다가 다시 연다
                out += f'</div>{im}<div style="{PAD}">'
        return f'<div style="margin-top:96px;{PAD}">{_sec("📌", "주요 특징")}{out}</div>'

    def checklist():
        items = [(_p_split(p)[0] or _p_split(p)[1][:34]) for p in points[:6] if str(p).strip()]
        if not items:
            return ""
        rows = "".join(
            f'<tr><td style="width:40px;padding:18px 0 18px 6px;border-bottom:1px solid #e8e8e8;color:{acc};'
            f'font-size:20px;font-weight:800;vertical-align:top">✓</td>'
            f'<td style="padding:18px 6px 18px 0;border-bottom:1px solid #e8e8e8;font-size:19px;font-weight:700;'
            f'color:#222;line-height:1.5">{e(t)}</td></tr>' for t in items)
        return sec_wrap(_sec("✅", "한눈에 체크하세요")
                        + f'<table style="width:100%;border-collapse:collapse;border-top:2px solid #111">{rows}</table>', top=72)

    def problem():
        q = intro or sub
        if not q:
            return ""
        return (sec_wrap(f'<div style="text-align:center">'
                         f'<div style="font-size:13px;font-weight:800;letter-spacing:3px;color:{acc}">WORRY</div>'
                         f'<div style="font-size:30px;font-weight:800;color:#111;margin-top:12px;letter-spacing:-0.5px">이런 적, 있으셨죠</div>'
                         f'<div style="font-size:19px;line-height:2;color:#444;margin-top:28px">{br(q)}</div></div>', top=40, band=True)
                + f'<div style="text-align:center;padding-top:56px">'
                  f'<div style="width:1px;height:60px;background:{acc};margin:0 auto"></div>'
                  f'<div style="font-size:26px;font-weight:800;color:#111;margin-top:22px;letter-spacing:-0.5px">그래서 이렇게 만들었어요</div></div>')

    def story():
        if not intro:
            return ""
        return (f'<div style="margin-top:88px;padding:0 9%">'
                f'<div style="font-size:13px;font-weight:800;letter-spacing:3px;color:{acc};margin-bottom:22px">STORY</div>'
                f'<div style="font-size:20px;line-height:2.05;color:#333">{br(intro)}</div></div>')

    scene_html = (sec_wrap(_sec("🎯", "이럴 때 좋아요")
                           + f'<div style="text-align:center;font-size:18px;line-height:2.1;color:#444">{br(scene)}</div>', band=True)
                  if scene else "")
    scene_block = scene_html + (f'<div style="margin-top:{0 if scene else 40}px">{slots[2]}</div>' if slots[2] else "")
    tip_html = (f'<div style="margin-top:72px;{PAD}"><table style="width:100%;border-collapse:collapse"><tr>'
                f'<td style="background:#f7f7f7;padding:30px 32px">'
                f'<div style="font-size:13px;font-weight:800;letter-spacing:3px;color:{acc};margin-bottom:12px">TIP</div>'
                f'<div style="font-size:17px;line-height:1.9;color:#555">{br(tip)}</div></td></tr></table></div>' if tip else "")
    tip_block = tip_html + (f'<div style="margin-top:40px">{slots[3]}</div>' if slots[3] else "")

    # 상세 설명 — 소제목(있으면) + 문단, 첫 문단 뒤 사진은 가득 폭
    dheads = [str(h).strip() for h in (sections.get("detail_heads") or [])]
    detail_html = ""
    if details:
        parts = []
        img4 = slots[4]
        for di, d in enumerate(details):
            dh = dheads[di] if di < len(dheads) else ""
            t = (f'<div style="font-size:22px;font-weight:800;color:#111;margin:{36 if di else 0}px 0 12px;letter-spacing:-0.4px">{e(dh)}</div>'
                 if dh else (f'<div style="height:{28 if di else 0}px"></div>'))
            parts.append(f'<div style="{PAD}">{t}<div style="font-size:17.5px;line-height:1.95;color:#444">{e(str(d))}</div></div>')
            if di == 0 and img4:
                parts.append(f'<div style="margin:44px 0 16px">{img4}</div>'); img4 = ""
        if img4:
            parts.append(f'<div style="margin-top:40px">{img4}</div>')
        detail_html = f'<div style="margin-top:110px"><div style="{PAD}">{_sec("🔎", "제품 상세 설명")}</div>{"".join(parts)}</div>'
    elif slots[4]:
        detail_html = f'<div style="margin-top:60px">{slots[4]}</div>'

    # 제품 정보표 — '항목: 값'은 2열, 굵은 윗선 + 얇은 줄
    spec_rows = ""
    if spec:
        for kv in spec.split(","):
            kv = kv.strip()
            if not kv:
                continue
            if ":" in kv:
                k, v = kv.split(":", 1)
                spec_rows += (f'<tr><th style="width:30%;text-align:left;padding:17px 6px;border-bottom:1px solid #e6e6e6;'
                              f'font-size:15.5px;color:#888;font-weight:600;vertical-align:top">{e(k.strip())}</th>'
                              f'<td style="padding:17px 6px;border-bottom:1px solid #e6e6e6;font-size:16px;color:#222;line-height:1.6">{e(v.strip())}</td></tr>')
            else:
                spec_rows += (f'<tr><td colspan="2" style="padding:17px 6px;border-bottom:1px solid #e6e6e6;font-size:16px;color:#222;line-height:1.6">{e(kv)}</td></tr>')
    if origin:
        spec_rows += (f'<tr><th style="width:30%;text-align:left;padding:17px 6px;border-bottom:1px solid #e6e6e6;'
                      f'font-size:15.5px;color:#888;font-weight:600">원산지</th>'
                      f'<td style="padding:17px 6px;border-bottom:1px solid #e6e6e6;font-size:16px;color:#222">{e(origin)}</td></tr>')
    spec_html = (sec_wrap(_sec("📋", "제품 정보") + f'<table style="width:100%;border-collapse:collapse;border-top:2px solid #111">{spec_rows}</table>')
                 if spec_rows else "")

    # 비교표
    compare_html = ""
    cmp_ = sections.get("compare") or {}
    if isinstance(cmp_, dict) and "compare" not in hide:
        ca = str(cmp_.get("a") or "").strip() or "이 제품"
        cb = str(cmp_.get("b") or "").strip() or "일반 제품"
        crows = [r for r in (cmp_.get("rows") or []) if isinstance(r, dict)
                 and any(str(r.get(x, "")).strip() for x in ("k", "a", "b"))]
        if crows:
            th = (f'<tr><td style="width:28%;padding:16px 8px;border-bottom:1px solid #222"></td>'
                  f'<td style="padding:16px 8px;background:{acc};color:#fff;font-weight:800;text-align:center;font-size:16px">{e(ca)}</td>'
                  f'<td style="padding:16px 8px;background:#f0f0f0;color:#777;font-weight:700;text-align:center;font-size:16px">{e(cb)}</td></tr>')
            trs = "".join(
                f'<tr><td style="padding:17px 8px;border-bottom:1px solid #e6e6e6;font-size:15px;color:#888;font-weight:600">{e(str(r.get("k", "")))}</td>'
                f'<td style="padding:17px 8px;border-bottom:1px solid #e6e6e6;text-align:center;font-size:16px;color:#111;font-weight:700">{e(str(r.get("a", "")))}</td>'
                f'<td style="padding:17px 8px;border-bottom:1px solid #e6e6e6;text-align:center;font-size:16px;color:#888">{e(str(r.get("b", "")))}</td></tr>'
                for r in crows[:12])
            compare_html = sec_wrap(_sec("⚖️", "한눈에 비교") + f'<table style="width:100%;border-collapse:collapse">{th}{trs}</table>')

    # 후기(판매자가 직접 받은 후기만)
    reviews_html = ""
    rv = L.get("reviews") or ""
    items = []
    for ln in str(rv).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts_ = [x.strip() for x in ln.split("|")]
        stars, nm, txt = 5, "", ""
        if len(parts_) >= 3:
            try:
                stars = max(1, min(5, int("".join(ch for ch in parts_[0] if ch.isdigit()) or 5)))
            except Exception:  # noqa: BLE001
                stars = 5
            nm, txt = parts_[1], parts_[2]
        elif len(parts_) == 2:
            nm, txt = parts_
        else:
            txt = parts_[0]
        if txt:
            items.append((stars, nm, txt))
    if items:
        avg = round(sum(x[0] for x in items) / len(items), 1)
        cards = "".join(
            f'<tr><td style="padding:24px 4px;border-bottom:1px solid #e8e8e8">'
            f'<div style="color:#f2a516;font-size:15px;letter-spacing:2px">{"★" * st_}{"☆" * (5 - st_)}'
            f'<span style="color:#999;font-size:14px;letter-spacing:0;margin-left:10px">{e(nm) if nm else "구매고객"}</span></div>'
            f'<div style="font-size:17px;line-height:1.8;color:#333;margin-top:8px">{e(tx)}</div></td></tr>'
            for st_, nm, tx in items)
        reviews_html = sec_wrap(
            _sec("💬", "고객 후기")
            + f'<div style="text-align:center;margin:-6px 0 26px"><span style="font-size:44px;font-weight:800;color:#111">{avg}</span>'
              f'<span style="font-size:16px;color:#999;margin-left:8px">/ 5 · 후기 {len(items)}개</span></div>'
            + f'<table style="width:100%;border-collapse:collapse;border-top:2px solid #111">{cards}</table>')

    # FAQ
    faq_html = ""
    if faqs:
        rows = ""
        for f in faqs[:8]:
            q = str(f.get("q", "")).strip(); a = str(f.get("a", "")).strip()
            if not q:
                continue
            rows += (f'<tr><td style="padding:24px 4px;border-bottom:1px solid #e8e8e8">'
                     f'<div style="font-size:18px;font-weight:800;color:#111;line-height:1.5">'
                     f'<span style="color:{acc};margin-right:8px">Q.</span>{e(q)}</div>'
                     + (f'<div style="font-size:16.5px;line-height:1.85;color:#666;margin-top:10px;padding-left:30px">{e(a)}</div>' if a else "")
                     + '</td></tr>')
        if rows:
            faq_html = ((f'<div style="margin-top:60px">{slots[5]}</div>' if slots[5] else "")
                        + sec_wrap(_sec("💬", "자주 묻는 질문")
                                   + f'<table style="width:100%;border-collapse:collapse;border-top:2px solid #111">{rows}</table>'))
    orphan = ""
    if slots[5] and not faq_html:
        orphan += f'<div style="margin-top:60px">{slots[5]}</div>'

    # 마무리 — 어두운 띠 위에 흰 글씨
    close_html = (f'<div style="margin-top:110px;background:#151515;padding:88px 8%;text-align:center">'
                  f'<div style="width:30px;height:2px;background:{acc if acc != "#111111" else "#ffffff"};margin:0 auto 30px"></div>'
                  f'<div style="font-size:26px;font-weight:700;color:#fff;line-height:1.7;letter-spacing:-0.4px">{br(close)}</div></div>'
                  if close else "")

    # 구매 안내
    guide_html = ""
    gd = L.get("guide") or ""
    if str(gd).strip():
        rows = ""
        for ln in str(gd).splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ":" in ln:
                k, v = ln.split(":", 1)
                rows += (f'<tr><th style="width:26%;text-align:left;padding:14px 6px;border-bottom:1px solid #e6e6e6;font-size:14.5px;color:#888;font-weight:600">{e(k.strip())}</th>'
                         f'<td style="padding:14px 6px;border-bottom:1px solid #e6e6e6;font-size:15px;color:#333;line-height:1.6">{e(v.strip())}</td></tr>')
            else:
                rows += f'<tr><td colspan="2" style="padding:14px 6px;border-bottom:1px solid #e6e6e6;font-size:15px;color:#333;line-height:1.6">{e(ln)}</td></tr>'
        guide_html = (f'<div style="margin-top:80px;{PAD}"><div style="font-size:19px;font-weight:800;color:#111;margin-bottom:14px">구매 안내</div>'
                      f'<table style="width:100%;border-collapse:collapse;border-top:2px solid #111">{rows}</table></div>')
    notice_html = L["notice_html"]
    if notice_html:
        notice_html = f'<div style="margin-top:56px;{PAD}">{notice_html}</div>'
    opt_html = L["opt_html"]
    if opt_html:
        opt_html = f'<div style="margin-top:60px;{PAD}">{opt_html}</div>'

    # 그래픽(선택) — 이모지 없이 담백하게
    chips_html = ""
    if L["_g_chips"] and points:
        ts = [(_p_split(p)[0] or "")[:18] for p in points[:4]]
        ts = [t for t in ts if t]
        if len(ts) >= 2:
            chips_html = ('<div style="text-align:center;margin-top:36px;padding:0 6%">' + "".join(
                f'<span style="display:inline-block;margin:4px;padding:9px 16px;border:1px solid #d8d8d8;border-radius:30px;'
                f'font-size:14.5px;font-weight:600;color:#333">{e(t)}</span>' for t in ts) + '</div>')
    banner_html = ""
    btxt = (sections.get("banner_text") or "").strip()
    blines = [x.strip() for x in btxt.splitlines() if x.strip()][:3]
    if L["banner"] and (blines or points):
        picks = blines or [(_p_split(p)[0] or _p_split(p)[1])[:30] for p in points[:3]]
        icons = [str(x).strip() for x in (sections.get("banner_icons") or []) if str(x).strip()]
        w = "%.2f%%" % (100.0 / max(1, len(picks)))
        cells = "".join(
            f'<td style="width:{w};vertical-align:top;text-align:center;padding:26px 8px;'
            f'border-left:{"1px solid #e6e6e6" if i else "none"}">'
            f'<div style="font-size:{"26px" if i < len(icons) else "15px"};font-weight:800;color:{acc};letter-spacing:1px">'
            f'{e(icons[i]) if i < len(icons) else "%02d" % (i + 1)}</div>'
            f'<div style="font-size:16px;font-weight:700;color:#222;line-height:1.5;margin-top:10px">{e(t)}</div></td>'
            for i, t in enumerate(picks))
        banner_html = (f'<div style="margin-top:56px;{PAD}"><table style="width:100%;border-collapse:collapse;'
                       f'border-top:1px solid #222;border-bottom:1px solid #e6e6e6"><tr>{cells}</tr></table></div>')
    badges_html = ""
    if L["_g_badges"]:
        bd = [t for c, t in ((L["guide"], "배송·교환 안내"), (L["options"], "옵션 선택 가능"), (L.get("reviews"), "구매 후기"),
                              (L["notice"], "상품정보 고시"), (origin, "원산지 표기")) if c]
        if len(bd) >= 2:
            badges_html = (f'<div style="margin-top:64px;{PAD};text-align:center;font-size:14px;color:#888;letter-spacing:0.5px">'
                           + '<span style="color:#ccc;margin:0 10px">|</span>'.join(e(t) for t in bd) + '</div>')
    divider_html = ('<div style="width:40px;height:1px;background:#cfcfcf;margin:90px auto 0"></div>' if L["_g_divider"] else "")

    # 블록 순서(기본형) — 각 블록은 자기 사진을 데리고 이동
    # ── ✨ v190 숫자 강조띠(FACT) — 상품 정보에 있는 숫자만(서버가 근거 없는 숫자는 미리 걸러냄) ──
    import re as _re2
    facts = [f for f in (sections.get("facts") or []) if isinstance(f, dict) and str(f.get("n", "")).strip()][:4]
    facts_html = ""
    if facts and "facts" not in hide:
        def _fcell(f, w):
            n = str(f.get("n", "")).strip(); t = str(f.get("t", "")).strip()
            m = _re2.match(r"^(약\s*)?([\d][\d.,~+/]*)(.*)$", n)
            if m and len(m.group(2)) <= 6:
                big = (f'<span style="font-size:42px;font-weight:800;color:{acc};letter-spacing:-1px">{e(m.group(2))}</span>'
                       f'<span style="font-size:18px;font-weight:700;color:{acc};margin-left:2px">{e(m.group(3).strip())}</span>')
                if m.group(1):
                    big = f'<span style="font-size:15px;color:#999;margin-right:3px">약</span>' + big
            else:
                big = f'<span style="font-size:24px;font-weight:800;color:{acc}">{e(n[:10])}</span>'
            return (f'<td style="width:{w};vertical-align:top;text-align:center;padding:26px 6px">'
                    f'<div style="line-height:1.15">{big}</div>'
                    f'<div style="font-size:15px;color:#666;margin-top:10px;line-height:1.45">{e(t[:16])}</div></td>')
        rows_ = [facts] if len(facts) <= 3 else [facts[:2], facts[2:4]]
        trs = ""
        for ri, row_ in enumerate(rows_):
            w = "%.2f%%" % (100.0 / len(row_))
            trs += (f'<tr>' + "".join(_fcell(f, w) for f in row_) + '</tr>')
        facts_html = (f'<div style="margin-top:56px;{PAD}"><table style="width:100%;border-collapse:collapse;'
                      f'border-top:1px solid #222;border-bottom:1px solid #e3e3e3">{trs}</table></div>')
    # ── 이런 분께 추천해요(FOR) ──
    fors = [str(x).strip() for x in (sections.get("fors") or []) if str(x).strip()][:4]
    for_html = ""
    if fors and "fors" not in hide:
        rws = "".join(
            f'<tr><td style="width:46px;padding:16px 0 16px 4px;vertical-align:top">'
            f'<div style="width:28px;height:28px;line-height:28px;border-radius:14px;background:{acc};color:#fff;'
            f'text-align:center;font-size:15px;font-weight:800">✓</div></td>'
            f'<td style="padding:17px 4px;font-size:18px;color:#222;font-weight:600;line-height:1.5;border-bottom:1px solid #e8e8e8">{e(x)}</td></tr>'
            for x in fors)
        for_html = sec_wrap(_sec("👥", "이런 분께 추천해요", "RECOMMEND")
                            + f'<table style="width:100%;border-collapse:collapse;background:#fff">{rws}</table>', band=True)
    # ── 이렇게 쓰세요(STEP) ──
    steps = [str(x).strip() for x in (sections.get("steps") or []) if str(x).strip()][:4]
    step_html = ""
    if len(steps) >= 2 and "steps" not in hide:
        w = "%.2f%%" % (100.0 / len(steps))
        cells = "".join(
            f'<td style="width:{w};vertical-align:top;text-align:center;padding:0 6px">'
            f'<div style="display:inline-block;font-size:13px;font-weight:800;letter-spacing:2px;color:#fff;background:{acc};'
            f'padding:6px 12px;border-radius:20px">STEP {i + 1}</div>'
            f'<div style="font-size:16.5px;color:#222;font-weight:600;line-height:1.55;margin-top:14px">{e(x)}</div></td>'
            for i, x in enumerate(steps))
        step_html = sec_wrap(_sec("🪜", "이렇게 쓰세요", "HOW TO USE")
                             + f'<table style="width:100%;border-collapse:collapse"><tr>{cells}</tr></table>')

    order = [b for b in (order or []) if b in ("points", "scene", "tip")]
    for b in ("points", "scene", "tip"):
        if b not in order:
            order.append(b)
    s1 = f'<div style="margin-top:40px">{slots[1]}</div>' if slots[1] else ""
    # 숫자 강조띠는 모든 유형에서 대표 사진 바로 아래(첫인상), 추천 대상은 도입 뒤, 사용 순서는 상세 앞
    if lay == "problem":
        main = (f"{header_html}{facts_html}{chips_html}{problem() or intro_html}{banner_html}{pshow()}{s1}"
                f"{for_html}{scene_block}{tip_block}{divider_html}{step_html}{detail_html}{spec_html}{compare_html}{opt_html}")
    elif lay == "spec":
        main = (f"{header_html}{facts_html}{chips_html}{spec_html}{banner_html}{intro_html}{numlist()}{s1}"
                f"{for_html}{step_html}{detail_html}{scene_block}{tip_block}{compare_html}{opt_html}{divider_html}")
    elif lay == "story":
        main = (f"{header_html}{story() or intro_html}{facts_html}{chips_html}{pshow(style='story')}{s1}{scene_block}"
                f"{for_html}{divider_html}{step_html}{detail_html}{tip_block}{compare_html}{spec_html}{opt_html}")
    elif lay == "check":
        main = (f"{header_html}{facts_html}{checklist()}{banner_html}{intro_html}{for_html}{pshow()}{s1}"
                f"{step_html}{detail_html}{spec_html}{compare_html}{opt_html}{scene_block}{tip_block}{divider_html}")
    else:
        # pshow()는 POINT 사진을 '사용함'으로 표시하므로 기본형일 때만 만든다(다른 유형에서 사진이 사라지는 버그 방지)
        points_block = pshow() + (f'<div style="margin-top:40px">{slots[1]}</div>' if slots[1] else "")
        blocks = {"points": points_block, "scene": scene_block, "tip": tip_block}
        main = (f"{header_html}{facts_html}{chips_html}{banner_html}{intro_html}{for_html}{''.join(blocks[b] for b in order)}"
                f"{divider_html}{step_html}{detail_html}{spec_html}{compare_html}{opt_html}")
    # 혹시 못 쓴 POINT 사진이 있으면 본문 끝에(사진 누락 0)
    left = "".join(f'<div style="margin-top:40px">{_imgtag(u)}</div>' for i, u in enumerate(pt_imgs) if i not in used)
    tail = f"{reviews_html}{faq_html}{orphan}{left}{badges_html}{close_html}{guide_html}{notice_html}"
    # ↕ 섹션 여백 — 큰 간격(24px 이상)만 배율 조정(글줄 사이 작은 간격은 그대로)
    k = {"tight": 0.6, "normal": 1.0, "wide": 1.35}.get(str(L.get("spacing") or "normal"), 1.0)
    if k != 1.0:
        import re as _re

        def _sp(t):
            def f(m):
                v = int(m.group(2))
                return f"{m.group(1)}{round(v * k) if v >= 24 else v}px"
            return _re.sub(r"((?:margin-top|padding-top|padding-bottom):)(\d+)px", f, t)

        def _sp_hero(t):   # 히어로 위아래 패딩(84px 6% 52px)과 마무리 띠(88px 8%)
            return (t.replace("padding:84px 6% 52px", f"padding:{round(84 * k)}px 6% {round(52 * k)}px")
                     .replace("padding:88px 8%", f"padding:{round(88 * k)}px 8%"))
        main, tail, header_html = _sp_hero(_sp(main)), _sp_hero(_sp(tail)), _sp_hero(_sp(header_html))
    return main + tail, header_html


# ✒️ v191 에디토리얼 디자인 — '사람이 편집한 잡지' 같은 상세페이지(새 기본)
# 왼쪽 정렬·명조 헤드라인·큰 숫자 챕터·판매자 메모·잘 맞는/안 맞는 분·따뜻한 종이색. 영문 라벨·가운데 정렬 템플릿 느낌을 뺐다.
_EACC = {"trust": "#24413a", "emotion": "#9a5446", "value": "#b8460f", "minimal": "#1c1917"}
_EPAPER = "#f6f2ea"          # 따뜻한 종이색 바탕
_EINK = "#1c1917"            # 본문 제목 잉크색
_EGRAY = "#57534e"           # 본문 글색
_ESERIF = "'Noto Serif KR','Nanum Myeongjo','나눔명조','AppleMyungjo','Batang','바탕',serif"
_ENUM = "Georgia,'Times New Roman',serif"
_EGOLD = "#a8875a"          # v192 고급형 금색 장식
# v193 고급형 장식색 3종 — (장식선, 숫자띠 바탕, 숫자색)
_ELUXE = {"gold": ("#a8875a", "#1f1d1b", "#d8c39a"), "rose": ("#b07a6e", "#2a1f1d", "#ecc3b8"),
          "silver": ("#8a8f98", "#1e2228", "#d5d9df")}


def _nbsp_tail(t):
    """v193 외톨이 글자 방지 — 문장 끝 어절이 짧으면(4자 이하) 앞 어절과 줄바꿈 없는 공백으로 붙인다(최대 2번)."""
    s = str(t or "").rstrip()
    for _ in range(2):
        k = s.rfind(" ")
        if k <= 0 or len(s) - k - 1 > 4:
            break
        s = s[:k] + "\u00a0" + s[k + 1:]
    return s


def _editorial_body(L):
    """✒️ 에디토리얼 조립 — _premium_body와 같은 재료(L=locals)로 다른 편집 구조를 만든다.
    v192: align='center'(기본·고급형 — 가운데 정렬, 금색 장식선, 차콜 띠, 핵심 한 줄 인용) / 'left'(v191 왼쪽 정렬 잡지형).
    반환: (body, header_html). 사진 배치·숨김·여백 규칙은 프리미엄과 동일(사진 누락 0)."""
    e = L["e"]; acc = L["acc"]
    head = L["head"]; sub = L["sub"]; intro = L["intro"]; points = L["points"]
    details = L["details"]; faqs = L["faqs"]; spec = L["spec"]; scene = L["scene"]
    tip = L["tip"]; close = L["close"]; origin = L["origin"]; slots = L["slots"]
    pt_imgs = L["pt_imgs"]; _imgtag = L["_imgtag"]; _sec = L["_sec"]; sections = L["sections"]
    lay = L["_lay"]; order = L["order"]; hide = L["_hide"]
    C = (str(L.get("align") or "center") != "left")
    AL = "center" if C else "left"
    PAD = "padding:0 7%"
    _lx = _ELUXE.get(str(L.get("luxe") or "gold"), _ELUXE["gold"])
    G = _lx[0]                       # 장식색(가운데형: 금·로즈골드·실버) — 왼쪽형은 브랜드색
    DECO = G if C else acc
    INK = _EINK; GRAY = _EGRAY
    used = set()

    def br(t):
        return "<br>".join(e(_nbsp_tail(x) if C else x) for x in _p_sents(t))

    def narrow(inner):
        """가운데형에서 긴 글은 폭을 640px로 좁혀 읽기 쉽게."""
        return (f'<div style="max-width:640px;margin:0 auto">{inner}</div>' if C else inner)

    def orn(top=0):
        """가운데형 장식: ── ◆ ──"""
        return (f'<div style="text-align:center;line-height:1;margin-top:{top}px">'
                f'<span style="display:inline-block;width:38px;border-top:1px solid {G};vertical-align:middle"></span>'
                f'<span style="display:inline-block;color:{G};font-size:9px;margin:0 10px;vertical-align:middle">◆</span>'
                f'<span style="display:inline-block;width:38px;border-top:1px solid {G};vertical-align:middle"></span></div>')

    def pimg(i, top=40):
        if 0 <= i < len(pt_imgs) and i not in used:
            used.add(i)
            return f'<div style="margin-top:{top}px">{_imgtag(pt_imgs[i])}</div>'
        return ""

    def block(inner, top=110, paper=False):
        bg = f"background:{_EPAPER};padding-top:72px;padding-bottom:72px;" if paper else ""
        # 주의: 'padding:0 7%' 같은 축약형을 뒤에 쓰면 위아래 여백이 지워진다 → 좌우만 따로 지정
        return f'<div style="margin-top:{top}px;{bg}padding-left:7%;padding-right:7%;text-align:{AL}">{inner}</div>'

    # ── 히어로 ──
    raw_head, mark = _head_mark(head)
    hl = e(raw_head)
    if mark and mark in raw_head:
        i0 = raw_head.find(mark)
        hl = (e(raw_head[:i0]) + f'<span style="color:{acc};border-bottom:3px solid {acc if not C else G}">{e(mark)}</span>'
              + e(raw_head[i0 + len(mark):]))
    k_ = raw_head.find(",")
    if 4 <= k_ <= len(raw_head) - 4 and not (mark and raw_head.find(mark) <= k_ < raw_head.find(mark) + len(mark)):
        j = hl.find(",")
        if j >= 0:
            hl = hl[:j + 1] + "<br>" + hl[j + 1:].lstrip()
    _eb = str(sections.get("eyebrow") or "").strip()
    if _eb == "-":
        _eb = ""
    elif not _eb:
        _eb = str(sections.get("label") or "").strip() or str(L.get("eyebrow") or "").strip()
    _eb = _eb[:24]
    if C:
        eb_html = (f'<div style="text-align:center;margin:0 0 26px;line-height:1">'
                   f'<span style="display:inline-block;width:28px;border-top:1px solid {G};vertical-align:middle"></span>'
                   f'<span style="display:inline-block;margin:0 12px;font-size:14px;font-weight:700;color:{G};letter-spacing:3px;vertical-align:middle">{e(_eb)}</span>'
                   f'<span style="display:inline-block;width:28px;border-top:1px solid {G};vertical-align:middle"></span></div>' if _eb else "")
    else:
        eb_html = (f'<table style="border-collapse:collapse;margin:0 0 24px"><tr>'
                   f'<td style="width:30px;border-top:2px solid {acc};padding:0"></td>'
                   f'<td style="padding:0 0 0 10px;font-size:15px;font-weight:700;color:{acc};line-height:1">{e(_eb)}</td></tr></table>'
                   if _eb else "")
    sub_html = (f'<div style="font-size:18.5px;color:{GRAY};line-height:1.8;margin-top:24px">{e(sub)}</div>' if sub else "")
    header_html = (f'<div style="padding:{92 if C else 76}px 7% {56 if C else 46}px;text-align:{AL}">{eb_html}'
                   f'<div style="font-family:{_ESERIF};font-size:{42 if C else 40}px;font-weight:700;line-height:1.38;color:{INK};letter-spacing:-1px">{hl}</div>'
                   f'{sub_html}{orn(34) if C else ""}</div>{slots[0]}')

    # ── 숫자 띠 ──
    facts = [f for f in (sections.get("facts") or []) if isinstance(f, dict) and str(f.get("n", "")).strip()][:4]
    facts_html = ""
    if facts and "facts" not in hide:
        import re as _re2
        band = _lx[1] if C else acc
        numc = _lx[2] if C else "#fff"

        def _fc(f, w, first):
            n = str(f.get("n", "")).strip(); t = str(f.get("t", "")).strip()
            m = _re2.match(r"^(약\s*)?([\d][\d.,~+/]*)(.*)$", n)
            if m and len(m.group(2)) <= 8:
                fs = 40 if len(m.group(2)) <= 4 else 32
                big = (f'<span style="font-family:{_ENUM};font-size:{fs}px;color:{numc}">{e(m.group(2))}</span>'
                       f'<span style="font-size:16px;color:{numc};margin-left:3px;font-weight:700">{e(m.group(3).strip())}</span>')
            else:
                big = f'<span style="font-size:22px;color:{numc};font-weight:800">{e(n[:10])}</span>'
            padl = 0 if (first or C) else 16
            return (f'<td style="width:{w};vertical-align:top;text-align:{AL};padding:30px 0 30px {padl}px;'
                    f'border-left:{"none" if first else "1px solid rgba(255,255,255,.18)"}">'
                    f'<div style="line-height:1.1">{big}</div>'
                    f'<div style="font-size:14px;color:rgba(255,255,255,.72);margin-top:10px;line-height:1.4;letter-spacing:.5px">{e(t[:16])}</div></td>')
        rows_ = [facts] if len(facts) <= 3 else [facts[:2], facts[2:4]]
        trs = ""
        for row_ in rows_:
            w = "%.2f%%" % (100.0 / len(row_))
            trs += "<tr>" + "".join(_fc(f, w, i == 0) for i, f in enumerate(row_)) + "</tr>"
        facts_html = (f'<div style="background:{band};padding:0 7%;margin-top:-{int(L.get("_gap") or 0)}px">'
                      f'<table style="width:100%;border-collapse:collapse">{trs}</table></div>')

    # ── 도입(첫 문장 인용) ──
    intro_html = ""
    if intro:
        ss = _p_sents(intro)
        rest = " ".join(ss[1:])
        qmark = (f'<div style="font-family:{_ENUM};font-size:64px;line-height:.6;color:{G};height:30px">“</div>' if C else "")
        q = (f'<div style="font-family:{_ESERIF};font-size:{25 if C else 26}px;font-weight:600;line-height:1.62;color:{INK}">'
             + (e(ss[0]) if C else f'“{e(ss[0])}”') + '</div>')
        intro_html = (f'<div style="margin-top:96px;{PAD};text-align:{AL}">{qmark}' + narrow(
            q + (f'<div style="font-size:17.5px;color:{GRAY};line-height:1.95;margin-top:20px">{br(rest)}</div>' if rest else ""))
            + '</div>')

    # ── 판매자 메모 ──
    note = str(sections.get("note") or "").strip()
    note_html = ""
    if note and "note" not in hide:
        if C:
            note_html = (f'<div style="margin:96px 7% 0;border-top:1px solid {G};border-bottom:1px solid {G};padding:40px 6%;text-align:center">'
                         f'<div style="font-size:13px;font-weight:800;color:{G};letter-spacing:4px">판매자 메모</div>'
                         f'<div style="font-family:{_ESERIF};font-size:19px;line-height:1.9;color:#292524;margin-top:16px">{br(note)}</div></div>')
        else:
            note_html = (f'<div style="margin:80px 7% 0;background:{_EPAPER};padding:30px 32px;border-left:4px solid {acc}">'
                         f'<div style="font-size:13px;font-weight:800;color:{acc};letter-spacing:1px">판매자 메모</div>'
                         f'<div style="font-size:18px;line-height:1.85;color:#292524;margin-top:12px">{br(note)}</div></div>')

    # ── POINT 챕터 ──
    def chapters(start=0, plist=None):
        plist = points if plist is None else plist
        out = ""
        for j, p in enumerate(plist):
            i = start + j
            h, b = _p_split(p)
            if C:
                txt = (f'<div style="font-family:{_ENUM};font-size:46px;line-height:1;color:{G}">{i + 1:02d}</div>'
                       f'<div style="width:28px;border-top:1px solid {G};margin:16px auto 0"></div>'
                       + (f'<div style="font-family:{_ESERIF};font-size:28px;font-weight:700;color:{INK};line-height:1.42;letter-spacing:-0.5px;margin-top:20px">{e(_nbsp_tail(h))}</div>' if h else "")
                       + (narrow(f'<div style="font-size:18px;color:{GRAY};line-height:1.95;margin-top:16px">{br(b)}</div>') if b else ""))
            else:
                txt = (f'<table style="width:100%;border-collapse:collapse"><tr>'
                       f'<td style="width:78px;vertical-align:top;font-family:{_ENUM};font-size:52px;line-height:.95;color:{acc};padding:0">{i + 1:02d}</td>'
                       f'<td style="vertical-align:top;border-top:1px solid #d6d3d1;padding:14px 0 0">'
                       + (f'<div style="font-size:26px;font-weight:800;color:{INK};line-height:1.4;letter-spacing:-0.5px">{e(h)}</div>' if h else "")
                       + (f'<div style="font-size:17.5px;color:{GRAY};line-height:1.9;margin-top:{12 if h else 0}px">{br(b)}</div>' if b else "")
                       + '</td></tr></table>')
            if j % 3 == 2:
                im = pimg(i, 0)
                out += (f'<div style="margin-top:110px;background:{_EPAPER};padding-bottom:64px">{im}'
                        f'<div style="{PAD};padding-top:{48 if im else 64}px;text-align:{AL}">{txt}</div></div>')
            else:
                out += f'<div style="margin-top:{110 if j else 96}px;{PAD};text-align:{AL}">{txt}</div>{pimg(i, 44)}'
        return out

    # ── 핵심 한 줄(인용 띠) ──
    quote = str(sections.get("quote") or "").strip()
    quote_html = ""
    if quote and "quote" not in hide:
        quote_html = (f'<div style="margin-top:110px;background:{_EPAPER};padding:84px 9%;text-align:center">'
                      f'{orn()}<div style="font-family:{_ESERIF};font-size:30px;font-weight:700;color:{INK};line-height:1.55;margin-top:26px">{e(_nbsp_tail(quote))}</div>'
                      f'{orn(26)}</div>')

    # ── 잘 맞는 분 / 안 맞는 분 ──
    fors = [str(x).strip() for x in (sections.get("fors") or []) if str(x).strip()][:4]
    nots = [str(x).strip() for x in (sections.get("notfors") or []) if str(x).strip()][:3]
    fit_html = ""
    if fors and "fors" not in hide:
        def _lst(items, mark_, col):
            if C:
                return "".join(f'<div style="padding:13px 4px;border-bottom:1px solid #e7e5e4;font-size:16.5px;color:#292524;line-height:1.55">'
                               f'<span style="color:{col};font-weight:800;margin-right:8px">{mark_}</span>{e(x)}</div>' for x in items)
            return ('<table style="width:100%;border-collapse:collapse">' + "".join(
                f'<tr><td style="width:24px;vertical-align:top;padding:12px 0;color:{col};font-weight:800;font-size:16px">{mark_}</td>'
                f'<td style="padding:12px 0;font-size:16.5px;color:#292524;line-height:1.55;border-bottom:1px solid #e7e5e4">{e(x)}</td></tr>'
                for x in items) + '</table>')
        hd = lambda t, c, b: (f'<div style="font-size:15px;font-weight:800;color:{c};padding-bottom:10px;border-bottom:2px solid {b};'
                              f'text-align:{AL};letter-spacing:{2 if C else 0}px">{t}</div>')
        good = hd("잘 맞는 분", acc, acc) + _lst(fors, "✓", acc)
        if nots and "notfors" not in hide:
            bad = hd("이런 분께는 비추천", "#78716c", "#a8a29e") + _lst(nots, "–", "#a8a29e")
            inner = (f'<table style="width:100%;border-collapse:collapse"><tr>'
                     f'<td style="width:50%;vertical-align:top;padding-right:14px">{good}</td>'
                     f'<td style="width:50%;vertical-align:top;padding-left:14px">{bad}</td></tr></table>')
            fit_html = block(_sec("", "잘 맞는 분, 안 맞는 분") + inner, paper=True)
        else:
            fit_html = block(_sec("", "이런 분께 잘 맞아요") + narrow(good), paper=True)

    # ── 쓰는 순서 ──
    steps = [str(x).strip() for x in (sections.get("steps") or []) if str(x).strip()][:4]
    step_html = ""
    if len(steps) >= 2 and "steps" not in hide:
        if C:
            w = "%.2f%%" % (100.0 / len(steps))
            cells = "".join(
                f'<td style="width:{w};vertical-align:top;text-align:center;padding:0 6px">'
                f'<div style="width:46px;height:46px;line-height:46px;margin:0 auto;border-radius:23px;border:1px solid {G};color:{G};'
                f'font-family:{_ENUM};font-size:20px">{i + 1}</div>'
                f'<div style="font-size:16.5px;color:#292524;font-weight:600;line-height:1.55;margin-top:14px">{e(x)}</div></td>'
                for i, x in enumerate(steps))
            step_html = block(_sec("", "쓰는 순서") + f'<table style="width:100%;border-collapse:collapse"><tr>{cells}</tr></table>')
        else:
            rows = "".join(
                f'<tr><td style="width:44px;vertical-align:top;padding:0 0 {0 if i == len(steps) - 1 else 22}px">'
                f'<div style="width:34px;height:34px;line-height:34px;border-radius:17px;border:2px solid {acc};color:{acc};'
                f'text-align:center;font-family:{_ENUM};font-size:17px;font-weight:700">{i + 1}</div></td>'
                f'<td style="vertical-align:top;padding:6px 0 0 6px;font-size:17.5px;color:#292524;font-weight:600;line-height:1.5">{e(x)}</td></tr>'
                for i, x in enumerate(steps))
            step_html = block(_sec("", "쓰는 순서") + f'<table style="border-collapse:collapse">{rows}</table>')

    # ── 이럴 때 / 알아두면 좋은 것 ──
    scene_html = ""
    if scene:
        if C:
            lines = f'<div style="color:{G};font-size:12px;margin:6px 0">·</div>'.join(
                f'<div style="font-size:18px;color:#292524;line-height:1.75">{e(x)}</div>' for x in _p_sents(scene))
            scene_html = block(_sec("", "이럴 때 꺼내 쓰게 돼요") + narrow(lines))
        else:
            lines = "".join(f'<div style="padding:10px 0 10px 18px;border-left:2px solid {acc};margin-top:{0 if i == 0 else 10}px;'
                            f'font-size:17.5px;color:#292524;line-height:1.7">{e(x)}</div>' for i, x in enumerate(_p_sents(scene)))
            scene_html = block(_sec("", "이럴 때 꺼내 쓰게 돼요") + lines)
    scene_block = scene_html + (f'<div style="margin-top:{40 if scene else 56}px">{slots[2]}</div>' if slots[2] else "")
    tip_html = (f'<div style="margin:80px 7% 0;border:1px solid {G if C else "#d6d3d1"};padding:32px 34px;text-align:{AL}">'
                f'<div style="font-size:{13 if C else 15}px;font-weight:800;color:{G if C else INK};letter-spacing:{4 if C else 0}px">알아두면 좋은 것</div>'
                f'<div style="font-size:16.5px;line-height:1.9;color:{GRAY};margin-top:12px">{br(tip)}</div></div>' if tip else "")
    tip_block = tip_html + (f'<div style="margin-top:40px">{slots[3]}</div>' if slots[3] else "")

    # ── 조금 더 자세히 ──
    dheads = [str(h).strip() for h in (sections.get("detail_heads") or [])]
    detail_html = ""
    if details:
        parts = []
        img4 = slots[4]
        for di, d in enumerate(details):
            dh = dheads[di] if di < len(dheads) else ""
            hh = ((f'<div style="font-family:{_ESERIF};font-size:21px;font-weight:700;color:{INK};margin-bottom:12px">{e(dh)}</div>' if C
                   else f'<div style="font-size:20px;font-weight:800;color:{INK};margin-bottom:10px">{e(dh)}</div>') if dh else "")
            parts.append(f'<div style="{PAD};margin-top:{40 if di else 0}px;text-align:{AL}">'
                         + narrow(hh + f'<div style="font-size:17.5px;line-height:1.95;color:{GRAY}">{e(str(d))}</div>') + '</div>')
            if di == 0 and img4:
                parts.append(f'<div style="margin:44px 0 10px">{img4}</div>'); img4 = ""
        if img4:
            parts.append(f'<div style="margin-top:40px">{img4}</div>')
        detail_html = f'<div style="margin-top:110px"><div style="{PAD};text-align:{AL}">{_sec("", "조금 더 자세히")}</div>{"".join(parts)}</div>'
    elif slots[4]:
        detail_html = f'<div style="margin-top:60px">{slots[4]}</div>'

    # ── 구매 전 확인하세요(사양) ──
    srows = ""
    tdl = f'width:32%;padding:16px 14px;background:{_EPAPER};border-bottom:1px solid #fff;font-size:15px;color:{GRAY};font-weight:700;vertical-align:top;text-align:{AL}'
    tdv = f'padding:16px 14px;border-bottom:1px solid #e7e5e4;font-size:16px;color:{INK};line-height:1.6;text-align:{AL}'
    if spec:
        for kv in spec.split(","):
            kv = kv.strip()
            if not kv:
                continue
            if ":" in kv:
                k2, v2 = kv.split(":", 1)
                srows += f'<tr><td style="{tdl}">{e(k2.strip())}</td><td style="{tdv}">{e(v2.strip())}</td></tr>'
            else:
                srows += f'<tr><td colspan="2" style="{tdv}">{e(kv)}</td></tr>'
    if origin:
        srows += f'<tr><td style="{tdl}">원산지</td><td style="{tdv}">{e(origin)}</td></tr>'
    spec_html = (block(_sec("", "구매 전 확인하세요") + f'<table style="width:100%;border-collapse:collapse;border-top:1px solid {INK}">{srows}</table>')
                 if srows else "")

    # ── 비교표 ──
    compare_html = ""
    cmp_ = sections.get("compare") or {}
    if isinstance(cmp_, dict) and "compare" not in hide:
        ca = str(cmp_.get("a") or "").strip() or "이 제품"
        cb = str(cmp_.get("b") or "").strip() or "일반 제품"
        crows = [r for r in (cmp_.get("rows") or []) if isinstance(r, dict) and any(str(r.get(x, "")).strip() for x in ("k", "a", "b"))]
        if crows:
            th = (f'<tr><td style="width:28%;padding:14px 8px;border-bottom:2px solid {INK}"></td>'
                  f'<td style="padding:14px 8px;border-bottom:2px solid {acc};color:{acc};font-weight:800;text-align:center;font-size:16px">{e(ca)}</td>'
                  f'<td style="padding:14px 8px;border-bottom:2px solid #a8a29e;color:#78716c;font-weight:700;text-align:center;font-size:16px">{e(cb)}</td></tr>')
            trs = "".join(
                f'<tr><td style="padding:15px 8px;border-bottom:1px solid #e7e5e4;font-size:15px;color:{GRAY};font-weight:700;text-align:{AL}">{e(str(r.get("k", "")))}</td>'
                f'<td style="padding:15px 8px;border-bottom:1px solid #e7e5e4;text-align:center;font-size:16px;color:{INK};font-weight:700">{e(str(r.get("a", "")))}</td>'
                f'<td style="padding:15px 8px;border-bottom:1px solid #e7e5e4;text-align:center;font-size:16px;color:#78716c">{e(str(r.get("b", "")))}</td></tr>'
                for r in crows[:12])
            compare_html = block(_sec("", "비교해 보면") + f'<table style="width:100%;border-collapse:collapse">{th}{trs}</table>')

    # ── 후기(판매자가 직접 받은 후기만) ──
    reviews_html = ""
    items = []
    for ln in str(L.get("reviews") or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        ps = [x.strip() for x in ln.split("|")]
        stars, nm, txt = 5, "", ""
        if len(ps) >= 3:
            try:
                stars = max(1, min(5, int("".join(ch for ch in ps[0] if ch.isdigit()) or 5)))
            except Exception:  # noqa: BLE001
                stars = 5
            nm, txt = ps[1], ps[2]
        elif len(ps) == 2:
            nm, txt = ps
        else:
            txt = ps[0]
        if txt:
            items.append((stars, nm, txt))
    if items:
        cards = "".join(
            f'<div style="margin-top:{0 if i == 0 else 16}px;background:#fff;padding:26px 28px;border:1px solid #e7e5e4;text-align:{AL}">'
            f'<div style="font-family:{_ESERIF};font-size:18px;line-height:1.75;color:{INK}">“{e(tx)}”</div>'
            f'<div style="margin-top:12px;font-size:14px;color:#a8a29e"><span style="color:{G if C else "#d97706"};letter-spacing:1px">{"★" * st_}{"☆" * (5 - st_)}</span>'
            f'&nbsp; {e(nm) if nm else "구매고객"}</div></div>'
            for i, (st_, nm, tx) in enumerate(items))
        reviews_html = block(_sec("", "먼저 산 분들 이야기") + cards, paper=True)

    # ── FAQ ──
    faq_html = ""
    if faqs:
        rows = ""
        for f in faqs[:8]:
            q = str(f.get("q", "")).strip(); a = str(f.get("a", "")).strip()
            if not q:
                continue
            if C:
                rows += (f'<div style="padding:26px 0;border-bottom:1px solid #e7e5e4;text-align:center">'
                         f'<div style="font-family:{_ENUM};color:{G};font-size:20px">Q.</div>'
                         f'<div style="font-size:18px;font-weight:800;color:{INK};line-height:1.55;margin-top:6px">{e(q)}</div>'
                         + (narrow(f'<div style="font-size:16.5px;line-height:1.85;color:{GRAY};margin-top:10px">{e(a)}</div>') if a else "")
                         + '</div>')
            else:
                rows += (f'<div style="padding:22px 0;border-bottom:1px solid #e7e5e4">'
                         f'<div style="font-size:18px;font-weight:800;color:{INK};line-height:1.5">'
                         f'<span style="font-family:{_ENUM};color:{acc};font-size:22px;margin-right:10px">Q</span>{e(q)}</div>'
                         + (f'<div style="font-size:16.5px;line-height:1.85;color:{GRAY};margin-top:8px;padding-left:28px">{e(a)}</div>' if a else "")
                         + '</div>')
        if rows:
            faq_html = ((f'<div style="margin-top:60px">{slots[5]}</div>' if slots[5] else "")
                        + block(_sec("", "많이 물어보시는 것") + rows))
    orphan = f'<div style="margin-top:60px">{slots[5]}</div>' if (slots[5] and not faq_html) else ""

    # ── 마무리 ──
    close_html = ""
    if close:
        close_html = (f'<div style="margin-top:120px;background:#1f1d1b;padding:96px 8%;text-align:{AL}">'
                      + (orn() if C else f'<div style="width:34px;border-top:2px solid {"#e7d8c3" if acc == "#1c1917" else acc}"></div>')
                      + f'<div style="font-family:{_ESERIF};font-size:28px;font-weight:600;color:#fafaf9;line-height:1.7;margin-top:28px">{br(close)}</div>'
                      + (f'<div style="margin-top:26px;font-size:13px;color:{G if C else "#a8a29e"};letter-spacing:{4 if C else 0}px">{e(_eb)}</div>' if _eb else "")
                      + '</div>')

    # ── 구매 안내·고시·옵션 ──
    guide_html = ""
    gd = L.get("guide") or ""
    if str(gd).strip():
        rows = ""
        for ln in str(gd).splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ":" in ln:
                k2, v2 = ln.split(":", 1)
                rows += (f'<tr><td style="width:26%;padding:13px 4px;border-bottom:1px solid #e7e5e4;font-size:14.5px;color:#78716c;font-weight:700">{e(k2.strip())}</td>'
                         f'<td style="padding:13px 4px;border-bottom:1px solid #e7e5e4;font-size:15px;color:#292524;line-height:1.6">{e(v2.strip())}</td></tr>')
            else:
                rows += f'<tr><td colspan="2" style="padding:13px 4px;border-bottom:1px solid #e7e5e4;font-size:15px;color:#292524">{e(ln)}</td></tr>'
        guide_html = (f'<div style="margin-top:80px;{PAD}"><div style="font-size:18px;font-weight:800;color:{INK};margin-bottom:12px;text-align:{AL}">구매 안내</div>'
                      f'<table style="width:100%;border-collapse:collapse;border-top:2px solid {INK}">{rows}</table></div>')
    notice_html = f'<div style="margin-top:56px;{PAD}">{L["notice_html"]}</div>' if L["notice_html"] else ""
    opt_html = f'<div style="margin-top:60px;{PAD}">{L["opt_html"]}</div>' if L["opt_html"] else ""

    # ── 선택 그래픽 ──
    chips_html = ""
    if L["_g_chips"] and points:
        ts = [t for t in [(_p_split(p)[0] or "")[:16] for p in points[:4]] if t]
        if len(ts) >= 2:
            chips_html = (f'<div style="margin-top:40px;{PAD};text-align:{AL}">' + "".join(
                f'<span style="display:inline-block;margin:0 4px 8px;padding:8px 16px;border:1px solid {DECO};'
                f'font-size:14px;color:#44403c">{e(t)}</span>' for t in ts) + '</div>')
    banner_html = ""
    btxt = (sections.get("banner_text") or "").strip()
    blines = [x.strip() for x in btxt.splitlines() if x.strip()][:3]
    if L["banner"] and (blines or points):
        picks = blines or [(_p_split(p)[0] or _p_split(p)[1])[:30] for p in points[:3]]
        w = "%.2f%%" % (100.0 / max(1, len(picks)))
        banner_html = (f'<div style="margin-top:56px;{PAD}"><table style="width:100%;border-collapse:collapse;border-top:1px solid {INK}"><tr>'
                       + "".join(f'<td style="width:{w};vertical-align:top;padding:20px 8px 0;text-align:{AL}">'
                                 f'<div style="font-family:{_ENUM};font-size:15px;color:{DECO}">{i + 1:02d}</div>'
                                 f'<div style="font-size:16px;font-weight:700;color:#292524;line-height:1.5;margin-top:6px">{e(t)}</div></td>'
                                 for i, t in enumerate(picks)) + '</tr></table></div>')
    divider_html = (orn(100) if C else f'<div style="margin:96px 7% 0;border-top:1px solid #d6d3d1"></div>') if L["_g_divider"] else ""
    badges_html = ""
    if L["_g_badges"]:
        bd = [t for c, t in ((L["guide"], "배송·교환 안내"), (L["options"], "옵션 선택 가능"), (L.get("reviews"), "구매 후기"),
                              (L["notice"], "상품정보 고시"), (origin, "원산지 표기")) if c]
        if len(bd) >= 2:
            badges_html = (f'<div style="margin-top:64px;{PAD};font-size:14px;color:#a8a29e;text-align:{AL}">'
                           + '<span style="margin:0 10px">·</span>'.join(e(t) for t in bd) + '</div>')

    def problem():
        q = intro or sub
        if not q:
            return ""
        return (block(f'<div style="font-family:{_ESERIF};font-size:28px;font-weight:700;color:{INK};line-height:1.45">이런 적, 있으셨죠.</div>'
                      + narrow(f'<div style="font-size:17.5px;line-height:1.95;color:{GRAY};margin-top:18px">{br(q)}</div>'), top=0, paper=True)
                + f'<div style="margin-top:60px;{PAD};text-align:{AL};font-size:22px;font-weight:800;color:{acc}">그래서 이렇게 만들었습니다.</div>')

    def checklist():
        its = [(_p_split(p)[0] or _p_split(p)[1][:34]) for p in points[:6] if str(p).strip()]
        if not its:
            return ""
        if C:
            rws = "".join(f'<div style="padding:15px 0;border-bottom:1px solid #e7e5e4;font-size:18px;font-weight:700;color:{INK}">'
                          f'<span style="color:{G};margin-right:10px">✓</span>{e(t)}</div>' for t in its)
            return block(_sec("", "한눈에 보기") + narrow(rws), top=88)
        rws = "".join(f'<tr><td style="width:30px;padding:14px 0;color:{acc};font-weight:800;font-size:17px;vertical-align:top">✓</td>'
                      f'<td style="padding:14px 0;border-bottom:1px solid #e7e5e4;font-size:18px;font-weight:700;color:{INK};line-height:1.5">{e(t)}</td></tr>'
                      for t in its)
        return block(_sec("", "한눈에 보기") + f'<table style="width:100%;border-collapse:collapse">{rws}</table>', top=80)

    order = [b for b in (order or []) if b in ("points", "scene", "tip")]
    for b in ("points", "scene", "tip"):
        if b not in order:
            order.append(b)
    s1 = f'<div style="margin-top:40px">{slots[1]}</div>' if slots[1] else ""
    if lay == "problem":
        main = (f"{header_html}{facts_html}{chips_html}{problem() or intro_html}{chapters()}{s1}{quote_html}{note_html}{fit_html}"
                f"{scene_block}{tip_block}{banner_html}{divider_html}{step_html}{detail_html}{spec_html}{compare_html}{opt_html}")
    elif lay == "spec":
        main = (f"{header_html}{facts_html}{chips_html}{spec_html}{intro_html}{banner_html}{chapters()}{s1}{quote_html}{note_html}"
                f"{fit_html}{step_html}{detail_html}{scene_block}{tip_block}{compare_html}{opt_html}{divider_html}")
    elif lay == "story":
        main = (f"{header_html}{intro_html}{facts_html}{chips_html}{chapters()}{s1}{quote_html}{note_html}{scene_block}"
                f"{fit_html}{divider_html}{step_html}{detail_html}{tip_block}{compare_html}{spec_html}{opt_html}")
    elif lay == "check":
        main = (f"{header_html}{facts_html}{checklist()}{banner_html}{intro_html}{note_html}{fit_html}{chapters()}{s1}{quote_html}"
                f"{step_html}{detail_html}{spec_html}{compare_html}{opt_html}{scene_block}{tip_block}{divider_html}")
    else:
        pblock = chapters() + s1 + quote_html
        blocks = {"points": pblock, "scene": scene_block, "tip": tip_block}
        main = (f"{header_html}{facts_html}{chips_html}{banner_html}{intro_html}{note_html}{''.join(blocks[b] for b in order)}"
                f"{fit_html}{divider_html}{step_html}{detail_html}{spec_html}{compare_html}{opt_html}")
    left = "".join(f'<div style="margin-top:40px">{_imgtag(u)}</div>' for i, u in enumerate(pt_imgs) if i not in used)
    tail = f"{reviews_html}{faq_html}{orphan}{left}{badges_html}{close_html}{guide_html}{notice_html}"
    k = {"tight": 0.6, "normal": 1.0, "wide": 1.35}.get(str(L.get("spacing") or "normal"), 1.0)
    if k != 1.0:
        import re as _re

        def _sp(t):
            def f(m):
                v = int(m.group(2))
                return f"{m.group(1)}{round(v * k) if v >= 24 else v}px"
            t = _re.sub(r"((?:margin-top|padding-top|padding-bottom):)(\d+)px", f, t)
            t = _re.sub(r"padding:(\d+)px 7% (\d+)px", lambda m: f"padding:{round(int(m.group(1)) * k)}px 7% {round(int(m.group(2)) * k)}px", t)
            t = _re.sub(r"padding:(\d+)px (8|9)%", lambda m: f"padding:{round(int(m.group(1)) * k)}px {m.group(2)}%", t)
            return _re.sub(r"margin:(\d+)px 7% 0", lambda m: f"margin:{round(int(m.group(1)) * k)}px 7% 0", t)
        main, tail, header_html = _sp(main), _sp(tail), _sp(header_html)
    return main + tail, header_html


def _classic_extras(sections, hide, e, acc, acc2, radius, _sec):
    """클래식(카드형) 디자인용 새 블록 4종 — 반환 {facts, note, fit, steps} HTML(없으면 빈 문자열)."""
    out = {"facts": "", "note": "", "fit": "", "steps": ""}
    facts = [f for f in (sections.get("facts") or []) if isinstance(f, dict) and str(f.get("n", "")).strip()][:4]
    if facts and "facts" not in hide:
        w = "%.2f%%" % (100.0 / len(facts))
        cells = "".join(
            f'<td style="width:{w};text-align:center;vertical-align:top;padding:16px 6px;'
            f'border-right:{"1px solid " + acc + "22" if i < len(facts) - 1 else "none"}">'
            f'<div style="font-size:24px;font-weight:900;color:{acc};line-height:1.2">{e(str(f.get("n", ""))[:10])}</div>'
            f'<div style="font-size:13px;color:#666;font-weight:700;margin-top:6px">{e(str(f.get("t", ""))[:16])}</div></td>'
            for i, f in enumerate(facts))
        out["facts"] = (f'<table style="width:100%;border-collapse:collapse;margin:14px 0 8px;background:{acc2};'
                        f'border:1px solid {acc}33;border-radius:{radius}"><tr>{cells}</tr></table>')
    note = str(sections.get("note") or "").strip()
    if note and "note" not in hide:
        out["note"] = (f'<table style="width:100%;border-collapse:collapse;margin:28px 0"><tr>'
                       f'<td style="background:#fffaf0;border:1px dashed {acc}88;border-radius:10px;padding:18px 20px;font-size:15.5px;line-height:1.75;color:#333">'
                       f'<b style="color:{acc}">📝 판매자 메모</b><br>{e(note)}</td></tr></table>')
    fors = [str(x).strip() for x in (sections.get("fors") or []) if str(x).strip()][:4]
    nots = [str(x).strip() for x in (sections.get("notfors") or []) if str(x).strip()][:3]
    if fors and "fors" not in hide:
        good = "".join(f'<div style="padding:8px 0;font-size:15px;color:#222">✅ {e(x)}</div>' for x in fors)
        bad = ("".join(f'<div style="padding:8px 0;font-size:15px;color:#666">🙅 {e(x)}</div>' for x in nots)
               if (nots and "notfors" not in hide) else "")
        inner = (f'<table style="width:100%;border-collapse:collapse"><tr>'
                 f'<td style="vertical-align:top;width:{"50%" if bad else "100%"};background:{acc2};border-radius:10px;padding:14px 16px">'
                 f'<b style="color:{acc}">이런 분께 추천해요</b>{good}</td>'
                 + (f'<td style="width:12px"></td><td style="vertical-align:top;width:50%;background:#f6f6f6;border-radius:10px;padding:14px 16px">'
                    f'<b style="color:#666">이런 분께는 비추천</b>{bad}</td>' if bad else "")
                 + '</tr></table>')
        out["fit"] = f'<div style="margin:36px 0">{_sec("👥", "잘 맞는 분")}{inner}</div>'
    steps = [str(x).strip() for x in (sections.get("steps") or []) if str(x).strip()][:4]
    if len(steps) >= 2 and "steps" not in hide:
        w = "%.2f%%" % (100.0 / len(steps))
        cells = "".join(
            f'<td style="width:{w};text-align:center;vertical-align:top;padding:6px">'
            f'<div style="width:40px;height:40px;line-height:40px;margin:0 auto 8px;background:{acc};color:#fff;border-radius:20px;font-weight:800">{i + 1}</div>'
            f'<div style="font-size:14.5px;color:#333;font-weight:700;line-height:1.5">{e(x)}</div></td>' for i, x in enumerate(steps))
        out["steps"] = f'<div style="margin:36px 0">{_sec("🪜", "사용 순서")}<table style="width:100%;border-collapse:collapse"><tr>{cells}</tr></table></div>'
    return out


def _build_detail_html(sections, imgs, name, tone_key="trust", notice=None,
                        notice_cat="", origin="", slots=None, order=None, guide="", toc=False, soldout="show", reviews="", sectheme=False, banner=False, img_place="spread", img_size="big", img_gap="normal", graphic=False, img_round="auto", img_border="none", graphic_parts="", img_shadow="none", captions=None, layout="basic", design="premium", hide="", spacing="normal", eyebrow="", align="center", luxe="gold"):
    """섹션+이미지+톤+상품정보고시로 상세페이지 HTML 조립. (모든 엔드포인트 공용)
    slots: [슬롯0,슬롯1,슬롯2,슬롯3] 각 값은 imgs 인덱스(int) 또는 None/-1(빈칸).
           None이면 앞에서부터 자동 배치. 스마트스토어 호환 위해 flex 대신 table 사용."""
    import html as _html
    e = _html.escape
    T = _dtone(tone_key)
    acc, acc2, font, radius = T["accent"], T["accent2"], T["font"], T["radius"]
    h1w, hstyle = T["h1w"], T["hstyle"]
    # ✨ v188 디자인 — premium(새 기본: 큰 헤드라인·사진 가득·짧은 문장·넓은 여백) / classic(v187까지의 카드형)
    P = (str(design or "premium").strip() != "classic")
    E = (str(design or "").strip() == "editorial")      # ✒️ v191 에디토리얼(프리미엄 계열: 사진 가득·POINT별 사진 공유)
    if E:
        acc = _EACC.get(tone_key or "trust", _EACC["trust"])
        acc2 = _EPAPER
        font = _PFONT
    elif P:
        acc = _PACC.get(tone_key or "trust", _PACC["trust"])
        acc2 = _PSOFT.get(tone_key or "trust", _PSOFT["trust"])
        font = _PFONT_SERIF if (tone_key == "emotion") else _PFONT
    # 🙈 v188 블록 숨기기 — 숨긴 블록의 글만 빼고, 그 자리 사진은 본문 끝으로 옮긴다(사진 누락 0)
    _hide = set(x.strip() for x in str(hide or "").split(",") if x.strip()) & set(_HIDEABLE)
    # 섹션 배경 테마 — 켜면 각 섹션 블록에 은은한 배경/패딩
    sw = (f'background:{acc2};border:1px solid {acc}22;border-radius:{radius};padding:20px 22px;' if sectheme else '')
    head = (sections.get("head") or name or "").strip()
    sub = (sections.get("sub") or "").strip()
    intro = (sections.get("intro") or "").strip()
    points = [p for p in (sections.get("points") or []) if p.strip()]
    details = [d for d in (sections.get("details") or []) if str(d).strip()]
    faqs = [f for f in (sections.get("faqs") or []) if (f.get("q") if isinstance(f, dict) else "")]
    spec = (sections.get("spec") or "").strip()
    scene = (sections.get("scene") or "").strip()
    tip = (sections.get("tip") or "").strip()
    close = (sections.get("close") or "").strip()
    options = [str(o).strip() for o in (sections.get("options") or []) if str(o).strip()]
    option_stock = sections.get("option_stock") or []   # options 와 같은 순서(정수 재고 또는 None)
    if "intro" in _hide: intro = ""
    if "details" in _hide: details = []
    if "faq" in _hide: faqs = []
    if "spec" in _hide: spec = ""; origin = ""
    if "scene" in _hide: scene = ""
    if "tip" in _hide: tip = ""
    if "close" in _hide: close = ""
    if "reviews" in _hide: reviews = ""
    imgs = _clean_imgs(imgs, cap=20)
    n = len(imgs)

    # 본문 사진 최대 폭 — 크게(가득)/보통/작게
    _imgmax = {"big": 860 if P else 820, "med": 600, "small": 420}.get((img_size or "big").strip(), 860 if P else 820)
    # 사진 위아래 여백(px) — 좁게/보통/넓게
    _gap = ({"tight": 0, "normal": 8, "wide": 28} if P else {"tight": 6, "normal": 14, "wide": 30}).get((img_gap or "normal").strip(), 8 if P else 14)
    # 사진 모서리 둥글기 — 각지게/살짝/많이 (auto=톤 기본값)
    _round = {"sharp": "0", "soft": "10px", "round": "22px"}.get((img_round or "auto").strip(), "0" if P else radius)
    # 사진 테두리(선) — 없음/얇은 회색/브랜드색
    _border = {"none": "", "gray": "border:1px solid #e5e5e5;", "brand": f"border:2px solid {acc};"}.get((img_border or "none").strip(), "")
    # 사진 그림자 — 없음/은은한
    _shadow = {"none": "", "soft": "box-shadow:0 6px 20px rgba(0,0,0,.12);"}.get((img_shadow or "none").strip(), "")
    # 사진별 캡션(한 줄 설명) — {이미지 URL: 캡션}
    _capmap = captions if isinstance(captions, dict) else {}

    def _imgtag(u):
        tag = (f'<img src="{e(u)}" alt="{e(name)}" style="width:100%;max-width:{_imgmax}px;display:block;'
               f'margin:{_gap}px auto;border-radius:{_round};{_border}{_shadow}">')
        cap = (_capmap.get(u) or "").strip()
        if cap:
            tag += (f'<div style="max-width:{_imgmax}px;margin:{"6px auto " + str(_gap + 14) if P else "-2px auto " + str(_gap + 4)}px;text-align:center;'
                    f'font-size:{14 if P else 13}px;color:#888;line-height:1.5;padding:0 6%">{e(cap)}</div>')
        return tag

    def _ph():
        return (f'<div style="width:100%;max-width:{_imgmax}px;margin:0 auto;padding:150px 0;background:{"#f4f4f4" if P else acc2};'
                f'border:2px dashed {"#d6d6d6" if P else acc + "55"};border-radius:{"0" if P else radius};text-align:center;'
                f'color:#98a0aa;font-size:15px">상품 사진 자리</div>')
    # 슬롯 6개: 0=헤드밑 1=장점밑 2=상황밑 3=팁밑 4=상세설명밑 5=FAQ위
    slot_html = {0: "", 1: "", 2: "", 3: "", 4: "", 5: ""}
    pt_imgs = []        # ✨ 프리미엄: POINT별 사진(POINT i 바로 아래) — classic에선 항상 빈 목록

    def _gallery(us):
        """📷 v193 남는 사진 2열 갤러리(table 2칸, 칸 사이 여백) — 1장이면 그냥 가득 폭."""
        us = list(us)
        if len(us) < 2:
            return "".join(f'<div style="margin:0">{_imgtag(u)}</div>' for u in us)
        rows = ""
        for i in range(0, len(us), 2):
            pair = us[i:i + 2]
            cells = "".join(
                f'<td style="width:50%;vertical-align:top;padding:0 {4 if j == 0 else 0}px 8px {0 if j == 0 else 4}px">'
                f'<img src="{e(u)}" alt="{e(name)}" style="width:100%;display:block;border-radius:{_round}">'
                + (f'<div style="font-size:13px;color:#888;text-align:center;margin-top:6px;line-height:1.4">{e(_capmap.get(u, ""))}</div>' if (_capmap.get(u) or "").strip() else "")
                + '</td>' for j, u in enumerate(pair))
            if len(pair) == 1:
                cells += '<td style="width:50%"></td>'
            rows += f'<tr>{cells}</tr>'
        return f'<table style="width:100%;border-collapse:collapse;margin:{_gap}px 0">{rows}</table>'
    if slots is not None:
        # 사용자가 슬롯별로 어느 사진을 넣을지 직접 지정(앞 4칸)
        used = []
        for si in range(4):
            idx = slots[si] if si < len(slots) else None
            try:
                idx = int(idx)
            except Exception:  # noqa: BLE001
                idx = -1
            if 0 <= idx < n:
                slot_html[si] = _imgtag(imgs[idx]); used.append(idx)
        # 지정에 안 쓰인 나머지 사진은 상세설명·FAQ 슬롯으로 자동 배치(사진 최대 활용)
        rest = [imgs[i] for i in range(n) if i not in used]
        if rest:
            slot_html[4] = _imgtag(rest[0])
        if len(rest) > 1:
            slot_html[5] = "".join(f'<div style="margin:0">{_imgtag(u)}</div>' for u in rest[1:])
        if not used and not rest and n == 0:
            slot_html[0] = _ph()
    else:
        # 자동: 대표이미지를 맨 위(헤드 아래)에 두고 나머지를 골고루 분산.
        ti = sections.get("thumbIdx")
        try:
            ti = int(ti)
        except Exception:  # noqa: BLE001
            ti = 0
        order_imgs = []
        if 0 <= ti < n:
            order_imgs.append(imgs[ti])                 # 대표 먼저
        order_imgs += [imgs[i] for i in range(n) if i != ti]
        _place = (img_place or "spread").strip()
        if _place == "top":
            # 상단 몰아넣기 — 헤드 아래(슬롯0)에 모든 사진을 세로로 쌓음
            slot_html[0] = "".join(f'<div style="margin:0">{_imgtag(u)}</div>' for u in order_imgs)
        elif _place == "bottom":
            # 하단 몰아넣기 — 본문 맨 아래(FAQ 위 슬롯5)에 모든 사진을 모아 배치
            slot_html[5] = "".join(f'<div style="margin:0">{_imgtag(u)}</div>' for u in order_imgs)
        elif P and points and n >= 2:
            # ✨ 프리미엄 분산 — 대표는 헤드라인 바로 아래, 그다음 사진들은 POINT마다 한 장씩(잘 팔리는 페이지 구조),
            # 남는 사진은 상세설명→사용상황→팁 자리, 그래도 남으면 FAQ 위 갤러리
            slot_html[0] = _imgtag(order_imgs[0])
            rest = order_imgs[1:]
            k = min(len(points), len(rest))
            pt_imgs[:] = rest[:k]
            rest = rest[k:]
            for si, u in zip([4, 2, 3], rest):
                slot_html[si] = _imgtag(u)
            if len(rest) > 3:
                slot_html[5] = _gallery(rest[3:])       # v193 남는 사진이 많으면 2열 갤러리
        else:
            # 분산(기본) — 대표를 맨 위에 두고 나머지를 블록마다 골고루
            seq = [0, 1, 2, 4, 3]      # 헤드→장점→사용상황→상세→팁 (자주 있는 블록부터 채움)
            for i in range(min(len(order_imgs), len(seq))):
                slot_html[seq[i]] = _imgtag(order_imgs[i])
            # 6장 이상 남으면 FAQ 위 슬롯에 갤러리로 모아 배치(사진 최대 활용)
            rest = order_imgs[len(seq):]
            if rest:
                slot_html[5] = "".join(f'<div style="margin:0">{_imgtag(u)}</div>' for u in rest)
        if n == 0:
            slot_html[0] = _ph()
    slots = slot_html

    # 공통 섹션 제목(아이콘 + 강조 바) — 스마트스토어 호환 위해 table/인라인 스타일만 사용
    _toc_items = []   # (anchor_id, icon, txt) — 목차용
    _sec_n = [0]

    def _sec(icon, txt, en=None):
        _sec_n[0] += 1
        aid = "dsec%d" % _sec_n[0]
        _toc_items.append((aid, "" if P else icon, txt))
        if E and str(align or "center") != "left":
            # 에디토리얼(가운데·고급형) 섹션 제목 — 명조 제목 + 아래 짧은 금색 선
            return (f'<table data-dtsec="1" id="{aid}" style="width:100%;border-collapse:collapse;margin:0 0 34px"><tr>'
                    f'<td style="text-align:center;padding:0"><div style="font-family:{_ESERIF};font-size:28px;font-weight:700;color:{_EINK};'
                    f'letter-spacing:-0.5px;line-height:1.4">{e(txt)}</div>'
                    f'<div style="width:36px;border-top:1px solid {_ELUXE.get(str(luxe or "gold"), _ELUXE["gold"])[0]};margin:16px auto 0"></div></td></tr></table>')
        if E:
            # 에디토리얼 섹션 제목 — 영문 라벨 없이, 왼쪽 정렬 굵은 한글 + 아래 가는 선
            return (f'<table data-dtsec="1" id="{aid}" style="width:100%;border-collapse:collapse;margin:0 0 26px"><tr>'
                    f'<td style="font-size:26px;font-weight:800;color:{_EINK};letter-spacing:-0.5px;line-height:1.4;'
                    f'padding:0 0 14px;border-bottom:1px solid {_EINK}">{e(txt)}</td></tr></table>')
        if P:
            # 프리미엄 섹션 제목 — 이모지 없이 작은 영문 라벨 + 큰 한글 제목(가운데)
            lab = en or _PEN.get(txt, "POINT")
            return (f'<table data-dtsec="1" id="{aid}" style="width:100%;border-collapse:collapse;margin:0 0 34px"><tr>'
                    f'<td style="text-align:center;padding:0">'
                    f'<div style="font-size:13px;font-weight:800;letter-spacing:3px;color:{acc}">{e(lab)}</div>'
                    f'<div style="font-size:30px;font-weight:800;color:#111;line-height:1.35;letter-spacing:-0.5px;margin-top:12px">{e(txt)}</div>'
                    f'</td></tr></table>')
        return (f'<table data-dtsec="1" id="{aid}" style="width:100%;border-collapse:collapse;margin:0 0 16px"><tr>'
                f'<td style="width:6px;background:{acc};border-radius:3px"></td>'
                f'<td style="padding-left:12px;font-size:22px;font-weight:800;color:#1a1a1a">'
                f'{icon} {e(txt)}</td></tr></table>')

    # 장점 카드 — 번호 배지 + 은은한 배경 카드(테이블 기반, 에디터 호환)
    def _pcards(plist, start=0):
        return "".join(
            f'<table style="width:100%;border-collapse:separate;border-spacing:0;margin:0 0 12px;'
            f'background:{acc2};border:1px solid {acc}33;border-radius:12px"><tr>'
            f'<td style="width:56px;vertical-align:top;padding:16px 0 16px 14px">'
            f'<div style="width:38px;height:38px;line-height:38px;text-align:center;background:{acc};'
            f'color:#fff;border-radius:19px;font-weight:800;font-size:16px">{start+i+1}</div></td>'
            f'<td style="padding:15px 16px 15px 10px;font-size:16px;line-height:1.7;color:#222;'
            f'vertical-align:middle">{e(p)}</td></tr></table>'
            for i, p in enumerate(plist))
    pts_cards = _pcards(points)
    pts_html = pts_cards or ""

    # 장점 문장을 '짧은 제목 + 나머지 설명'으로 나누기(레이아웃용)
    def _split(p, n=26):
        s = str(p).strip()
        cut = -1
        for ch in (".", "!", "?", ",", "—", " - "):
            k = s.find(ch)
            # 숫자 사이 마침표·쉼표(15.6인치, 1,000원)는 문장 경계가 아님 → 다음 후보로
            while 0 < k < len(s) - 1 and ch in (".", ",") and s[k - 1].isdigit() and s[k + 1].isdigit():
                k = s.find(ch, k + 1)
            if 0 < k and (cut < 0 or k < cut):
                cut = k
        if 0 < cut <= 40:
            h, b = s[:cut].strip(), s[cut + 1:].strip().lstrip(".,!? —-").strip()
        else:
            h, b = s, ""
        if len(h) > n:
            h = h[:n].rstrip() + "…"
            b = s if not b else b
        return h, b

    # 제품 정보 표
    spec_rows = ""
    if spec:
        for kv in spec.split(","):
            kv = kv.strip()
            if kv:
                spec_rows += (f'<tr><td style="padding:11px 14px;background:{acc2};font-weight:700;'
                              f'width:44px;border-bottom:1px solid #eee;color:{acc}">✓</td>'
                              f'<td style="padding:11px 14px;border-bottom:1px solid #eee;font-size:15px;color:#333">{e(kv)}</td></tr>')
    if origin:
        spec_rows += (f'<tr><td style="padding:11px 14px;background:{acc2};font-weight:700;'
                      f'border-bottom:1px solid #eee;color:#555">원산지</td>'
                      f'<td style="padding:11px 14px;border-bottom:1px solid #eee;font-size:15px;color:#333">{e(origin)}</td></tr>')
    spec_html = (f'<div style="margin:36px 0;{sw}">{_sec("📋", "제품 정보")}'
                 f'<table style="width:100%;border-collapse:collapse;font-size:15px;'
                 f'border:1px solid #eee;border-radius:10px;overflow:hidden">{spec_rows}</table></div>'
                 if spec_rows else "")

    # 구성/옵션 표 (옵션이 있을 때만) — "라벨|추가금액" 형식 + 2축 조합표 지원
    opt_html = ""
    if options:
        axes = [str(x).strip() for x in (sections.get("option_axes") or []) if str(x).strip()]
        parsed_opts = []
        any_add = False
        for o in options:
            lab, add = o, 0
            if "|" in o:
                lab, _a = o.rsplit("|", 1)
                digits = "".join(ch for ch in _a if ch.isdigit())
                if digits:
                    add = int(digits)
                    any_add = True
            parsed_opts.append((lab.strip(), add))
        # 옵션별 재고/품절 — option_stock(정수 재고 또는 None) 이 하나라도 있으면 열 표시
        def _stock_at(i):
            try:
                v = option_stock[i]
            except Exception:  # noqa: BLE001
                return None
            if v is None or v == "":
                return None
            try:
                return int(v)
            except Exception:  # noqa: BLE001
                return None
        any_stock = any(_stock_at(i) is not None for i in range(len(parsed_opts)))

        def _stock_cell(i):
            if not any_stock:
                return ''
            s = _stock_at(i)
            if s is None:
                txt, col = '-', '#aaa'
            elif s <= 0:
                txt, col = '품절', '#c2410c'
            else:
                txt, col = ("%s개" % format(s, ",")), '#1a7f37'
            return (f'<td style="padding:10px 14px;border-bottom:1px solid #eee;font-size:13px;'
                    f'color:{col};text-align:center;font-weight:700">{txt}</td>')
        # 2축 이상이고 모든 옵션이 축 개수만큼 " / " 로 나뉘면 → 조합표
        use_combo = bool(axes) and len(axes) >= 2 and all(
            len([c for c in lab.split(" / ")]) == len(axes) for lab, _a in parsed_opts)
        th = ('padding:10px 14px;background:' + acc2 + ';font-weight:800;'
              'border-bottom:2px solid ' + acc + "33;font-size:13px;color:" + acc + ';')
        head_add = ('<th style="' + th + 'text-align:right;width:120px">추가금액</th>') if any_add else ''
        head_stock = ('<th style="' + th + 'text-align:center;width:90px">재고</th>') if any_stock else ''

        def _addcell(add):
            if not any_add:
                return ''
            atxt = ("+%s원" % format(add, ",")) if add > 0 else "+0원"
            return (f'<td style="padding:10px 14px;border-bottom:1px solid #eee;font-size:14px;'
                    f'color:{"#c2410c" if add>0 else "#888"};text-align:right;font-weight:700">{atxt}</td>')

        def _row_open(i):
            """품절 옵션 처리: soldout = show(그대로) / dim(흐리게) / hide(건너뜀 → None)."""
            s = _stock_at(i)
            is_sold = (s is not None and s <= 0)
            if is_sold and soldout == "hide":
                return None
            if is_sold and soldout == "dim":
                return '<tr style="opacity:.45">'
            return '<tr>'
        if use_combo:
            head = ('<tr><th style="' + th + 'text-align:center;width:44px">#</th>'
                    + "".join('<th style="' + th + 'text-align:left">' + e(ax) + '</th>' for ax in axes)
                    + head_add + head_stock + '</tr>')
            orows = ""
            for i, (lab, add) in enumerate(parsed_opts):
                ro = _row_open(i)
                if ro is None:
                    continue
                cells = [c.strip() for c in lab.split(" / ")]
                tds = "".join(f'<td style="padding:10px 14px;border-bottom:1px solid #eee;font-size:15px;color:#333">{e(c)}</td>' for c in cells)
                orows += (f'{ro}<td style="padding:10px 14px;background:{acc2};font-weight:700;width:44px;'
                          f'border-bottom:1px solid #eee;color:{acc};text-align:center">{i+1}</td>{tds}{_addcell(add)}{_stock_cell(i)}</tr>')
        else:
            head = ('<tr><th style="' + th + 'text-align:center;width:44px">#</th>'
                    '<th style="' + th + 'text-align:left">옵션</th>' + head_add + head_stock + '</tr>')
            orows = ""
            for i, (lab, add) in enumerate(parsed_opts):
                ro = _row_open(i)
                if ro is None:
                    continue
                orows += (f'{ro}<td style="padding:10px 14px;background:{acc2};font-weight:700;width:44px;'
                          f'border-bottom:1px solid #eee;color:{acc};text-align:center">{i+1}</td>'
                          f'<td style="padding:10px 14px;border-bottom:1px solid #eee;font-size:15px;color:#333">{e(lab)}</td>'
                          f'{_addcell(add)}{_stock_cell(i)}</tr>')
        opt_html = (f'<div style="margin:36px 0;{sw}">{_sec("🎨", "구성·옵션")}'
                    f'<table style="width:100%;border-collapse:collapse;border:1px solid #eee;'
                    f'border-radius:10px;overflow:hidden">{head}{orows}</table></div>')

    tip_html = (f'<table style="width:100%;border-collapse:collapse;margin:28px 0"><tr>'
                f'<td style="background:{acc2};border:1px solid {acc}55;border-left:6px solid {acc};'
                f'border-radius:10px;padding:18px 20px;font-size:15px;line-height:1.7;color:#333">'
                f'<b style="color:{acc}">💡 활용 팁</b><br>{e(tip)}</td></tr></table>' if tip else "")

    sub_html = (f'<p style="font-size:18px;color:#fff;opacity:.92;text-align:center;margin:10px 0 0;'
                f'line-height:1.55;font-style:{hstyle}">{e(sub)}</p>' if sub else "")

    # 헤더 밴드 — 액센트 그라데이션 위에 제목/서브
    header_html = (
        f'<table style="width:100%;border-collapse:collapse;margin:0 0 8px"><tr><td '
        f'style="background:{acc};border-radius:{radius};padding:34px 26px;text-align:center">'
        f'<h1 style="font-size:30px;font-weight:{h1w};color:#fff;line-height:1.4;margin:0;'
        f'font-style:{hstyle}">{e(_head_plain(head))}</h1>{sub_html}</td></tr></table>')

    # 핵심 요약 배너 — 상단 3가지 핵심 포인트(아이콘). banner=True 이고 문구(직접입력 또는 장점)가 있을 때
    banner_text = (sections.get("banner_text") or "").strip()
    banner_lines = [ln.strip() for ln in banner_text.splitlines() if ln.strip()][:3]
    banner_html = ""
    if banner and (banner_lines or points):
        icons = ["✅", "⭐", "🚀", "💡", "🎯"]
        # 사용자가 아이콘을 직접 고른 경우 우선 사용
        chosen = [str(x).strip() for x in (sections.get("banner_icons") or []) if str(x).strip()]
        if chosen:
            icons = (chosen + icons)[:5]
        picks = []
        if banner_lines:
            picks = [(x[:40].rstrip() + "…") if len(x) > 40 else x for x in banner_lines]
        else:
            for p in points[:3]:
                # 첫 문장/첫 절만 짧게(배너용). 40자 넘으면 자름
                short = str(p).strip().split(".")[0].split("!")[0].split(",")[0].strip()
                if len(short) > 40:
                    short = short[:40].rstrip() + "…"
                picks.append(short)
        cells = "".join(
            f'<td style="width:33.33%;vertical-align:top;padding:16px 12px;text-align:center;'
            f'border-right:{"1px solid " + acc + "22" if i < len(picks) - 1 else "none"}">'
            f'<div style="font-size:26px;line-height:1;margin-bottom:8px">{icons[i]}</div>'
            f'<div style="font-size:14.5px;font-weight:700;color:#333;line-height:1.5">{e(tx)}</div></td>'
            for i, tx in enumerate(picks))
        banner_html = (f'<table style="width:100%;border-collapse:collapse;margin:16px 0 8px;'
                       f'background:{acc2};border:1px solid {acc}33;border-radius:{radius}"><tr>{cells}</tr></table>')

    scene_html = (f'<table style="width:100%;border-collapse:collapse;margin:36px 0"><tr>'
                  f'<td style="background:{acc2};border-radius:{radius};padding:26px 24px">'
                  f'{_sec("🎯", "이럴 때 좋아요")}'
                  f'<p style="font-size:16px;line-height:1.8;margin:0;color:#333">{e(scene)}</p></td></tr></table>'
                  if scene else "")

    close_html = (f'<table style="width:100%;border-collapse:collapse;margin:40px 0 20px"><tr>'
                  f'<td style="background:{acc2};border:2px solid {acc};border-radius:{radius};'
                  f'padding:26px;text-align:center;font-size:21px;font-weight:800;color:{acc};'
                  f'line-height:1.5">{e(close)}</td></tr></table>' if close else "")

    # 도입부(공감 문단)
    intro_html = (f'<p style="font-size:16.5px;line-height:1.9;color:#444;margin:26px 4px 6px;'
                  f'text-align:center">{e(intro)}</p>' if intro else "")

    # ── 🎨 그래픽 요소(비사진 시각 블록) — 블록별 on/off ──
    # graphic_parts(콤마 문자열)에 있는 블록만 켠다. 하위호환: graphic=True 이고 parts 비면 전부 켬.
    _gparts = set(x.strip() for x in (graphic_parts or "").split(",") if x.strip())
    if graphic and not _gparts:
        _gparts = {"chips", "badges", "divider"}
    _g_chips = "chips" in _gparts
    _g_badges = "badges" in _gparts
    _g_divider = "divider" in _gparts
    # (1) 혜택 요약 칩 줄: 장점 앞부분을 짧은 알약(칩)으로. 사진 없이도 상단이 꽉 참
    chips_html = ""
    if _g_chips and points:
        chip_txts = []
        for p in points[:4]:
            s = str(p).strip().split(".")[0].split("!")[0].split(",")[0].strip()
            if len(s) > 18:
                s = s[:18].rstrip() + "…"
            if s:
                chip_txts.append(s)
        if len(chip_txts) >= 2:
            chips = "".join(
                f'<span style="display:inline-block;margin:4px 5px;padding:9px 15px;background:{acc2};'
                f'border:1px solid {acc}44;border-radius:22px;font-size:14px;font-weight:700;color:{acc}">'
                f'✓ {e(t)}</span>' for t in chip_txts)
            chips_html = (f'<div style="text-align:center;margin:16px 0 4px">{chips}</div>')

    # (2) 정보 배지 줄: 실제로 페이지에 있는 섹션만 배지로(사실 그대로 — 과장 아님)
    badges_html = ""
    if _g_badges:
        badge_defs = []
        if guide:
            badge_defs.append(("🚚", "배송·교환 안내"))
        if options:
            badge_defs.append(("🎨", "옵션 선택 가능"))
        if reviews:
            badge_defs.append(("💬", "구매 후기 확인"))
        if notice:
            badge_defs.append(("📋", "상품정보 고시"))
        if origin:
            badge_defs.append(("🌐", "원산지 표기"))
        if len(badge_defs) >= 2:
            cells = "".join(
                f'<td style="text-align:center;padding:14px 8px;border-right:'
                f'{"1px solid " + acc + "22" if i < len(badge_defs)-1 else "none"}">'
                f'<div style="font-size:24px;line-height:1;margin-bottom:6px">{ic}</div>'
                f'<div style="font-size:13px;font-weight:700;color:#444">{e(tx)}</div></td>'
                for i, (ic, tx) in enumerate(badge_defs))
            badges_html = (f'<table style="width:100%;border-collapse:collapse;margin:30px 0 8px;'
                           f'background:#fafafa;border:1px solid #eee;border-radius:{radius}"><tr>{cells}</tr></table>')

    # (3) 섹션 구분 디바이더 — 큰 블록 사이에 은은한 장식선(사진 없어도 리듬감)
    divider_html = (f'<div style="text-align:center;margin:34px 0;color:{acc}55;'
                    f'font-size:14px;letter-spacing:6px">◆ ◆ ◆</div>' if _g_divider else "")

    # 상세 설명 문단들 — 문단 사이/끝에 이미지(slot4) 한 장 끼워넣기
    # detail_heads(문단별 소제목)가 있으면 각 문단 위에 작은 소제목을 붙여 가독성↑
    _dheads = [str(h).strip() for h in (sections.get("detail_heads") or [])]
    detail_html = ""
    if details:
        parts_d = []
        img4 = f'<div style="margin:18px 0">{slots[4]}</div>' if slots[4] else ""
        for di, d in enumerate(details):
            _dh = _dheads[di] if di < len(_dheads) else ""
            if _dh:
                parts_d.append(
                    f'<table style="border-collapse:collapse;margin:22px 4px 8px"><tr>'
                    f'<td style="width:4px;background:{acc};border-radius:2px"></td>'
                    f'<td style="padding-left:10px;font-size:17.5px;font-weight:800;color:#1a1a1a">{e(_dh)}</td></tr></table>')
            parts_d.append(
                f'<p style="font-size:16px;line-height:1.85;color:#333;margin:0 4px 16px">{e(str(d))}</p>')
            if di == 0 and img4:           # 첫 문단 뒤에 사진
                parts_d.append(img4); img4 = ""
        if img4:                            # 문단이 1개뿐이면 끝에 사진
            parts_d.append(img4)
        detail_html = (f'<div style="margin:36px 0;{sw}">{_sec("🔎", "제품 상세 설명")}'
                       f'{"".join(parts_d)}</div>')

    # FAQ — 질문/답변 아코디언 느낌(테이블)
    faq_html = ""
    if faqs:
        rows = ""
        for f in faqs[:8]:
            q = e(str(f.get("q", "")).strip()); a = e(str(f.get("a", "")).strip())
            if not q:
                continue
            rows += (f'<tr><td style="padding:14px 16px;border-bottom:1px solid #eee;vertical-align:top">'
                     f'<div style="font-size:15.5px;font-weight:700;color:#1a1a1a;margin-bottom:6px">Q. {q}</div>'
                     f'<div style="font-size:15px;line-height:1.7;color:#555">A. {a}</div></td></tr>')
        if rows:
            faq_html = (f'<div style="margin:36px 0;{sw}">{_sec("💬", "자주 묻는 질문")}{slots[5]}'
                        f'<table style="width:100%;border-collapse:collapse;border:1px solid #eee;'
                        f'border-radius:10px;overflow:hidden">{rows}</table></div>')

    notice_html = _notice_html(notice, notice_cat, e)
    guide_html = _guide_html(guide, e, acc, acc2, radius)
    reviews_html = _reviews_html(reviews, e, acc, acc2, radius, _sec)
    # 블록 순서 지정(장점/사용상황/활용팁) — 각 블록은 자기 이미지 슬롯을 데리고 이동
    points_block = (f'<div style="margin:38px 0;{sw}">{_sec("👍", "이런 점이 좋아요")}{pts_html}</div>{slots[1]}'
                    if pts_html else f'{slots[1]}')
    scene_block = f'{scene_html}{slots[2]}'
    tip_block = f'{tip_html}{slots[3]}'
    blocks = {"points": points_block, "scene": scene_block, "tip": tip_block}
    order = [b for b in (order or []) if b in blocks]
    for b in ("points", "scene", "tip"):
        if b not in order:
            order.append(b)
    group = "".join(blocks[b] for b in order)
    img0 = f'<div style="margin:16px 0">{slots[0]}</div>' if slots[0] else ""
    # 섹션이 없어 사진 슬롯이 빠지는 경우 방지(상세설명/FAQ가 없으면 그 자리 사진을 본문 끝에라도 붙임)
    orphan_imgs = ""
    if slots[4] and not details:
        orphan_imgs += f'<div style="margin:18px 0">{slots[4]}</div>'
    if slots[5] and not faq_html:
        orphan_imgs += f'<div style="margin:18px 0">{slots[5]}</div>'

    # ⚖️ 비교표 블록 — sections.compare = {a:'우리 제품', b:'일반 제품', rows:[{k,a,b}]}
    compare_html = ""
    _cmp = sections.get("compare") or {}
    if isinstance(_cmp, dict):
        _ca = str(_cmp.get("a") or "").strip() or "우리 제품"
        _cb = str(_cmp.get("b") or "").strip() or "일반 제품"
        _crows = [r for r in (_cmp.get("rows") or []) if isinstance(r, dict)
                  and any(str(r.get(x, "")).strip() for x in ("k", "a", "b"))]
        if _crows:
            _th = (f'<tr><td style="padding:12px 14px;background:#f6f6f6;font-weight:700;color:#555;width:28%;'
                   f'border-bottom:1px solid #eee;font-size:14px">비교 항목</td>'
                   f'<td style="padding:12px 14px;background:{acc};color:#fff;font-weight:800;text-align:center;'
                   f'border-bottom:1px solid #eee;font-size:15px">{e(_ca)}</td>'
                   f'<td style="padding:12px 14px;background:#eeeeee;color:#666;font-weight:700;text-align:center;'
                   f'border-bottom:1px solid #eee;font-size:15px">{e(_cb)}</td></tr>')
            _trs = "".join(
                f'<tr><td style="padding:12px 14px;background:#fafafa;font-weight:700;color:#444;'
                f'border-bottom:1px solid #eee;font-size:14px">{e(str(r.get("k", "")))}</td>'
                f'<td style="padding:12px 14px;text-align:center;border-bottom:1px solid #eee;font-size:14.5px;'
                f'color:#1a1a1a;font-weight:700;background:{acc2}">{e(str(r.get("a", "")))}</td>'
                f'<td style="padding:12px 14px;text-align:center;border-bottom:1px solid #eee;font-size:14.5px;'
                f'color:#777">{e(str(r.get("b", "")))}</td></tr>'
                for r in _crows[:12])
            compare_html = (f'<div style="margin:36px 0;{sw}">{_sec("⚖️", "한눈에 비교")}'
                            f'<table style="width:100%;border-collapse:collapse;border:1px solid #eee;'
                            f'border-radius:10px;overflow:hidden">{_th}{_trs}</table></div>')

    # ── 🧩 상세페이지 유형(레이아웃) — 상위 상세페이지들의 '구조'만 참고해 새로 짠 템플릿 ──
    _lay = (layout or "basic").strip()

    def _key3():   # 핵심 3가지 — 3칸 카드
        ks = points[:3]
        if not ks:
            return ""
        w = "%.2f%%" % (100.0 / len(ks))
        cells = ""
        for i, p in enumerate(ks):
            h, b = _split(p, 20)
            b = (b[:70].rstrip() + "…") if len(b) > 70 else b
            cells += (f'<td style="width:{w};vertical-align:top;padding:18px 10px;text-align:center;'
                      f'border-right:{"1px solid " + acc + "22" if i < len(ks) - 1 else "none"}">'
                      f'<div style="width:44px;height:44px;line-height:44px;margin:0 auto 10px;background:{acc};'
                      f'color:#fff;border-radius:22px;font-size:18px;font-weight:800">{i + 1}</div>'
                      f'<div style="font-size:15px;font-weight:800;color:#1a1a1a;line-height:1.45;margin-bottom:6px">{e(h)}</div>'
                      f'<div style="font-size:13.5px;color:#666;line-height:1.6">{e(b)}</div></td>')
        return (f'<div style="margin:30px 0">{_sec("✨", "핵심은 딱 세 가지")}'
                f'<table style="width:100%;border-collapse:collapse;background:{acc2};border:1px solid {acc}33;'
                f'border-radius:{radius}"><tr>{cells}</tr></table></div>')

    def _problem():   # 문제 공감 → 해결 연결
        q = intro or sub
        if not q:
            return ""
        # 해결로 넘어가는 화살표만(헤드·서브·장점 문구 중복 방지) — 바로 아래 '핵심 3가지'가 해결 파트
        sol_html = f'<div style="text-align:center;margin:2px 0 6px;font-size:24px;color:{acc}">▼</div>'
        return (f'<table style="width:100%;border-collapse:collapse;margin:26px 0 8px"><tr>'
                f'<td style="background:#f6f6f6;border-radius:{radius};padding:28px 24px;text-align:center">'
                f'<div style="font-size:14px;font-weight:800;color:{acc};letter-spacing:1px;margin-bottom:10px">이런 불편, 겪어보셨죠</div>'
                f'<div style="font-size:17px;line-height:1.85;color:#333">{e(q)}</div></td></tr></table>{sol_html}')

    def _story():   # 스토리형 도입
        if not intro:
            return ""
        return (f'<table style="width:100%;border-collapse:collapse;margin:30px 0"><tr>'
                f'<td style="border-left:5px solid {acc};padding:6px 0 6px 22px">'
                f'<div style="font-size:13px;font-weight:800;color:{acc};letter-spacing:2px;margin-bottom:10px">STORY</div>'
                f'<div style="font-size:18px;line-height:1.95;color:#333">{e(intro)}</div></td></tr></table>')

    def _steps():   # 장점을 POINT 01·02… 타임라인으로
        if not points:
            return ""
        rows = ""
        for i, p in enumerate(points):
            h, b = _split(p, 30)
            bdiv = (f'<div style="font-size:15px;line-height:1.75;color:#444">{e(b)}</div>' if b else "")
            rows += (f'<tr><td style="width:60px;vertical-align:top;padding:0 0 20px">'
                     f'<div style="width:52px;padding:6px 0;text-align:center;background:{acc};color:#fff;'
                     f'border-radius:8px;font-size:11px;font-weight:800;line-height:1.3">POINT<br>'
                     f'<span style="font-size:18px">{i + 1:02d}</span></div></td>'
                     f'<td style="vertical-align:top;padding:2px 0 20px 14px;border-left:2px solid {acc}33">'
                     f'<div style="font-size:16.5px;font-weight:800;color:#1a1a1a;margin-bottom:5px">{e(h)}</div>'
                     f'{bdiv}</td></tr>')
        return (f'<div style="margin:38px 0;{sw}">{_sec("📖", "하나씩 짚어볼게요")}'
                f'<table style="width:100%;border-collapse:collapse">{rows}</table></div>')

    def _numlist():   # 스펙형 — 번호 리스트(제목 굵게 + 설명)
        if not points:
            return ""
        rows = ""
        for i, p in enumerate(points):
            h, b = _split(p, 30)
            rows += (f'<tr><td style="width:46px;padding:14px 0 14px 12px;vertical-align:top;font-size:20px;'
                     f'font-weight:900;color:{acc}">{i + 1:02d}</td>'
                     f'<td style="padding:14px 16px 14px 6px;border-bottom:1px solid #eee;font-size:15px;line-height:1.7;color:#444">'
                     f'<b style="color:#1a1a1a;font-size:16px">{e(h)}</b>{("<br>" + e(b)) if b else ""}</td></tr>')
        return (f'<div style="margin:36px 0;{sw}">{_sec("📌", "주요 특징")}'
                f'<table style="width:100%;border-collapse:collapse;border-top:2px solid {acc}">{rows}</table></div>')

    def _checklist():   # 체크리스트형 — 한눈에 체크
        items = [_split(p, 34)[0] for p in points[:6] if str(p).strip()]
        if not items:
            return ""
        rows = "".join(
            f'<tr><td style="width:36px;padding:11px 0 11px 16px;vertical-align:top">'
            f'<div style="width:22px;height:22px;line-height:22px;text-align:center;background:{acc};color:#fff;'
            f'border-radius:5px;font-size:13px;font-weight:800">✓</div></td>'
            f'<td style="padding:11px 16px 11px 8px;font-size:15.5px;color:#222;font-weight:600;line-height:1.55">{e(t)}</td></tr>'
            for t in items)
        return (f'<div style="margin:26px 0">{_sec("✅", "한눈에 체크하세요")}'
                f'<table style="width:100%;border-collapse:collapse;background:{acc2};border:1px solid {acc}33;'
                f'border-radius:{radius}">{rows}</table></div>')

    if "compare" in _hide:
        compare_html = ""
    tail = f"{reviews_html}{faq_html}{orphan_imgs}{badges_html}{close_html}{guide_html}{notice_html}"
    if P:
        body, header_html = (_editorial_body(locals()) if E else _premium_body(locals()))
    elif _lay == "problem":
        # 문제해결형: 공감 → 해결 → 핵심 3가지 → 나머지 장점 → 상세 → 근거(스펙·비교·후기) → 구매 장애물 제거(FAQ) → 구매 유도
        _rest = _pcards(points[3:], 3)
        rest_block = (f'<div style="margin:30px 0;{sw}">{_sec("👍", "이런 점도 좋아요")}{_rest}</div>' if _rest else "")
        body = (f"{header_html}{chips_html}{_problem()}{banner_html}{_key3()}{img0}{rest_block}{slots[1]}"
                f"{scene_block}{tip_block}{divider_html}{detail_html}{spec_html}{compare_html}{opt_html}{tail}")
    elif _lay == "spec":
        # 스펙·정보형: 사양을 먼저 → 특징 번호 리스트 → 상세 → 사용 장면 → 비교 → FAQ
        body = (f"{header_html}{chips_html}{spec_html}{banner_html}{intro_html}{img0}{_numlist()}{slots[1]}"
                f"{detail_html}{scene_block}{tip_block}{compare_html}{opt_html}{divider_html}{tail}")
    elif _lay == "story":
        # 스토리형: 이야기로 시작 → 장점 타임라인 → 사용 장면 → 상세 → 후기 → 마무리
        body = (f"{header_html}{_story() or intro_html}{img0}{chips_html}{_steps()}{slots[1]}{scene_block}"
                f"{divider_html}{detail_html}{tip_block}{compare_html}{spec_html}{opt_html}{tail}")
    elif _lay == "check":
        # 체크리스트형: 한눈에 체크 → 대표사진 → 핵심 3가지 → 자세히 → 스펙·비교 → FAQ(구매 전 확인)
        _rest = _pcards(points[3:], 3)
        rest_block = (f'<div style="margin:30px 0;{sw}">{_sec("🔍", "자세히 보면")}{_rest}</div>' if _rest else "")
        body = (f"{header_html}{_checklist()}{img0}{banner_html}{_key3()}{intro_html}{rest_block}{slots[1]}"
                f"{detail_html}{spec_html}{compare_html}{opt_html}{scene_block}{tip_block}{divider_html}{tail}")
    else:
        body = f"""{header_html}
  {chips_html}
  {banner_html}
  {intro_html}
  {img0}
  {group}
  {divider_html}
  {detail_html}
  {spec_html}
  {compare_html}
  {opt_html}
  {reviews_html}
  {faq_html}
  {orphan_imgs}
  {badges_html}
  {close_html}
  {guide_html}
  {notice_html}"""
    # 🧩 v191 클래식에도 새 블록(숫자 강조·판매자 메모·잘 맞는/안 맞는 분·사용 순서) — 카드·이모지 톤 그대로
    if not P:
        _cx = _classic_extras(sections, _hide, e, acc, acc2, radius, _sec)
        if _cx["facts"] and body.startswith(header_html):
            body = header_html + _cx["facts"] + body[len(header_html):]
        _mid = _cx["note"] + _cx["fit"] + _cx["steps"]
        if _mid:
            if detail_html and detail_html in body:
                body = body.replace(detail_html, _mid + detail_html, 1)
            elif tail and tail in body:
                body = body.replace(tail, _mid + tail, 1)
            else:
                body = body + _mid
    # 📑 목차(앵커) — toc=True 이고 섹션이 2개 이상일 때 상단에 추가. 시각 순서대로 정렬
    # (레이아웃에 따라 안 쓰인 섹션은 목차에서 제외)
    _toc_items[:] = [it for it in _toc_items if ('id="%s"' % it[0]) in body]
    if toc and len(_toc_items) >= 2:
        ordered = sorted(_toc_items, key=lambda it: (body.find('id="%s"' % it[0]) + 1) or 10 ** 9)
        links = "".join(
            f'<li style="margin:4px 0"><a href="#{aid}" style="color:{acc};text-decoration:none;font-size:15px">'
            f'{ic} {e(tx)}</a></li>' for aid, ic, tx in ordered)
        toc_html = (f'<div style="margin:8px 0 28px;padding:16px 20px;background:{acc2};'
                    f'border:1px solid {acc}33;border-radius:{radius}">'
                    f'<div style="font-size:16px;font-weight:800;color:#1a1a1a;margin-bottom:8px">📑 목차</div>'
                    f'<ul style="margin:0;padding-left:20px;line-height:1.6">{links}</ul></div>')
        if P:
            links = "".join(
                f'<li style="margin:6px 0;list-style:none"><a href="#{aid}" style="color:#333;text-decoration:none;font-size:16px">'
                f'<span style="color:{acc};font-weight:800;margin-right:10px">{i + 1:02d}</span>{e(tx)}</a></li>'
                for i, (aid, ic, tx) in enumerate(ordered))
            toc_html = (f'<div style="margin:56px 6% 0;padding:24px 0;border-top:2px solid #111;border-bottom:1px solid #e6e6e6">'
                        f'<div style="font-size:13px;font-weight:800;letter-spacing:3px;color:{acc};margin-bottom:10px">CONTENTS</div>'
                        f'<ul style="margin:0;padding:0;line-height:1.6">{links}</ul></div>')
        body = f"{header_html}\n  {toc_html}\n  " + body[len(header_html):]
    if P:
        return (f'<div style="max-width:860px;margin:0 auto;font-family:{font};color:#222;padding:0 0 64px;'
                f'background:#fff;word-break:keep-all;overflow-wrap:break-word">\n  {body}\n</div>')
    return (f'<div style="max-width:860px;margin:0 auto;font-family:{font};color:#222;padding:8px;background:#fff">\n'
            f'  {body}\n</div>')


def _notice_html(notice, notice_cat, e):
    """상품정보제공고시 표 — notice = [{k,v}, ...] (빈 값은 '상세페이지 참조')."""
    if not notice:
        return ""
    rows = ""
    for item in notice:
        try:
            k = str(item.get("k", "")).strip()
            v = str(item.get("v", "")).strip()
        except Exception:  # noqa: BLE001
            continue
        if not k:
            continue
        vhtml = e(v) if v else '<span style="color:#aaa">상세페이지 참조</span>'
        rows += (f'<tr><th style="text-align:left;padding:10px 12px;background:#fafafa;font-weight:600;'
                 f'width:38%;border-bottom:1px solid #eee;font-size:13px;color:#555">{e(k)}</th>'
                 f'<td style="padding:10px 12px;border-bottom:1px solid #eee;font-size:13px">{vhtml}</td></tr>')
    if not rows:
        return ""
    cat = e(notice_cat) if notice_cat else "상품 정보"
    return (f'<div style="margin:44px 0 8px"><h2 style="font-size:16px;font-weight:800;margin:0 0 10px;'
            f'color:#333">상품정보제공고시 <span style="font-size:12px;color:#999;font-weight:500">'
            f'({cat}) · 전자상거래법 필수</span></h2>'
            f'<table style="width:100%;border-collapse:collapse;border:1px solid #eee;border-radius:8px;'
            f'overflow:hidden">{rows}</table></div>')


class DetailTrimReq(BaseModel):
    gemini_key: str
    texts: list = []
    limit: int = 120


@app.post("/api/detail/trim")
async def detail_trim_api(req: DetailTrimReq):
    """카피 자동 다듬기 — 너무 긴 문장들을 핵심만 남겨 limit자 이내로 축약."""
    gkey = (req.gemini_key or "").strip()
    texts = [str(t).strip() for t in (req.texts or []) if str(t).strip()]
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    if not texts:
        return {"ok": False, "error": "다듬을 문장이 없어요."}
    lim = max(30, min(300, int(req.limit or 120)))
    numbered = "\n".join(f"{i+1}) {t}" for i, t in enumerate(texts))
    prompt = ("너는 한국 스마트스토어 카피라이터야. 아래 문장들을 각각 핵심만 남겨 "
              f"{lim}자 이내로 자연스럽게 줄여줘. 의미·톤은 유지하고 과장/허위는 넣지 마.\n\n"
              f"[문장들]\n{numbered}\n\n"
              f"[출력] 입력과 같은 개수만큼, 순서대로 아래 한 줄씩만(마크다운·번호 금지):\n"
              + "\n".join("TRIM: (줄인 문장)" for _ in texts))
    text, err = await _detail_gemini(gkey, prompt, temperature=0.5)
    if err:
        return err
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.upper().startswith("TRIM:"):
            out.append(ln.split(":", 1)[1].strip())
    # 개수가 안 맞으면 원문 유지로 보정
    result = []
    for i, t in enumerate(texts):
        result.append(out[i] if i < len(out) and out[i] else t)
    return {"ok": True, "texts": result}


class DetailFaqReq(BaseModel):
    gemini_key: str
    ctx: str = ""
    name: str = ""
    existing: list = []      # 이미 있는 질문(중복 방지)
    count: int = 3


@app.post("/api/detail/faq")
async def detail_faq_api(req: DetailFaqReq):
    """상품 정보 기반 FAQ 자동 생성 — 구매 전 실제로 궁금해할 Q/A 를 count개 제안."""
    gkey = (req.gemini_key or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    n = max(1, min(6, int(req.count or 3)))
    ex = [str(q).strip() for q in (req.existing or []) if str(q).strip()][:10]
    ex_txt = ("\n[이미 있는 질문 — 겹치지 않게]\n" + "\n".join("- " + q for q in ex)) if ex else ""
    prompt = ("너는 한국 스마트스토어 상세페이지 카피라이터야. 아래 상품 정보로 "
              f"구매 전 고객이 실제로 궁금해할 FAQ {n}개를 새로 만들어줘(중복·뻔한 질문 지양, 구체적으로).\n\n"
              f"[상품 정보]\n{req.ctx or req.name}\n" + ex_txt +
              f"\n\n[출력] 정확히 {n}줄, 각 줄 아래 형식만(마크다운·번호 금지):\n"
              + "\n".join("FAQ: (질문) || (망설임을 없애주는 구체적 답변 1~2문장)" for _ in range(n))
              + "\n과장·허위·근거 없는 효능 금지.")
    text, err = await _detail_gemini(gkey, prompt, temperature=0.7)
    if err:
        return err
    faqs = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.upper().startswith("FAQ:") and "||" in ln:
            body = ln.split(":", 1)[1]
            q, a = body.split("||", 1)
            q, a = q.strip(), a.strip()
            if q:
                faqs.append({"q": q, "a": a})
    if not faqs:
        return {"ok": False, "error": "FAQ를 만들지 못했어요. 다시 시도해주세요."}
    return {"ok": True, "faqs": faqs}


class DetailSoftenReq(BaseModel):
    gemini_key: str
    texts: list = []
    banned: list = []


@app.post("/api/detail/soften")
async def detail_soften_api(req: DetailSoftenReq):
    """금지어 자동 순화 — 과장·절대·의학효과 표현을 표시광고법에 안전한 표현으로 교체(길이·톤 유지)."""
    gkey = (req.gemini_key or "").strip()
    texts = [str(t) for t in (req.texts or [])]
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    if not any((t or "").strip() for t in texts):
        return {"ok": False, "error": "순화할 문장이 없어요."}
    banned = [str(w).strip() for w in (req.banned or []) if str(w).strip()]
    numbered = "\n".join(f"{i+1}) {t}" for i, t in enumerate(texts))
    prompt = ("너는 한국 스마트스토어 카피라이터야. 아래 문장들에서 표시광고법 위반 위험 표현"
              "(최고/1위/유일/완벽/100%/완치/치료/의학적/부작용 없음/절대/영구/기적 등 과장·절대·의학적 효능)을 "
              "자연스럽고 안전한 표현으로 바꿔줘. 의미·톤·길이는 최대한 유지하고, 위험 표현만 순화해.\n"
              + (f"[특히 이 표현들을 손봐줘]\n{', '.join(banned)}\n" if banned else "")
              + f"\n[문장들]\n{numbered}\n\n"
              f"[출력] 입력과 같은 개수만큼 순서대로 아래 한 줄씩만(마크다운·번호 금지):\n"
              + "\n".join("SOFT: (순화한 문장)" for _ in texts))
    text, err = await _detail_gemini(gkey, prompt, temperature=0.4)
    if err:
        return err
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.upper().startswith("SOFT:"):
            out.append(ln.split(":", 1)[1].strip())
    result = [out[i] if i < len(out) and out[i] else texts[i] for i in range(len(texts))]
    return {"ok": True, "texts": result}


class SubheadsReq(BaseModel):
    gemini_key: str
    details: list = []


@app.post("/api/detail/subheads")
async def detail_subheads_api(req: SubheadsReq):
    """긴 상세설명 문단마다 짧은 소제목(14자 내외)을 붙여 가독성을 높인다. 1회 호출로 전체 처리."""
    gkey = (req.gemini_key or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    dets = [str(d).strip() for d in (req.details or [])][:8]
    if not any(dets):
        return {"ok": False, "error": "소제목을 붙일 상세설명이 없어요."}
    numbered = "\n".join(f"{i+1}) {d}" for i, d in enumerate(dets))
    prompt = ("아래 상품 상세설명 문단마다, 그 문단 내용을 한눈에 알려주는 짧은 소제목을 붙여줘. "
              "각 14자 이내, 명사형 또는 짧은 구로. 과장·허위(최고/1위/완벽 등) 금지, 이모지·따옴표 금지.\n\n"
              f"[문단들]\n{numbered}\n\n"
              "[출력] 입력과 같은 개수만큼 순서대로 아래 한 줄씩만(번호 금지):\n"
              + "\n".join("HEAD: (소제목)" for _ in dets))
    text, err = await _detail_gemini(gkey, prompt, temperature=0.4)
    if err:
        return err
    out = [ln.split(":", 1)[1].strip().strip('"“”') for ln in text.splitlines()
           if ln.strip().upper().startswith("HEAD:")]
    heads = [(out[i][:20] if i < len(out) else "") for i in range(len(dets))]
    return {"ok": True, "heads": heads}


_CMP_STOP = {"가능", "있음", "없음", "제품", "포함", "사용", "지원", "우수", "편리", "간편", "높음", "낮음", "좋음",
             "다양", "기본", "일반", "제공", "구성", "타입", "방식", "정도", "이상", "이하", "약", "개", "종"}


def _facts_verify(sections, ctx):
    """숫자 강조띠(FACT)의 숫자가 상품 정보(ctx)에 실제로 있는 것만 남긴다 — 지어낸 수치가 크게 박히는 사고 방지."""
    import re as _re
    src = str(ctx or "").replace(" ", "").replace(",", "")
    keep = []
    for f in (sections.get("facts") or []):
        nums = _re.findall(r"\d+(?:\.\d+)?", str(f.get("n", "")).replace(",", ""))
        if nums and all(x in src for x in nums):
            keep.append(f)
    sections["facts"] = keep[:4]
    return sections


def _cmp_evidence(val, info):
    """비교표 '이 제품' 칸 값이 상품 정보(ctx·사양·장점)에 근거가 있는지 단순 대조.
    숫자가 있으면 그 숫자가 원문에 있어야 하고, 아니면 의미 있는 단어의 절반 이상이 원문에 있어야 ✓."""
    import re as _re
    v = str(val or "").strip()
    src = re_src = str(info or "").replace(" ", "").lower()
    if not v:
        return False
    nums = _re.findall(r"\d+(?:[.,]\d+)?", v)
    if nums:
        return all(n.replace(",", "") in re_src.replace(",", "") for n in nums)
    words = [w for w in _re.findall(r"[가-힣A-Za-z]{2,}", v) if w not in _CMP_STOP]
    if not words:
        return False
    hit = sum(1 for w in words if w.lower() in src)
    return hit * 2 >= len(words)


class CompareDraftReq(BaseModel):
    gemini_key: str
    ctx: str = ""
    name: str = ""
    spec: str = ""
    points: list = []


@app.post("/api/detail/comparedraft")
async def detail_comparedraft_api(req: CompareDraftReq):
    """비교표 초안 — 상품 정보에 근거한 '이 제품 vs 일반 제품' 비교 3~5행.
    편집기에만 채우고, 사용자가 검토 후 반영해야 상세페이지에 들어간다."""
    gkey = (req.gemini_key or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    info = "\n".join(x for x in [
        (req.name or "").strip() and f"[상품명] {req.name.strip()}",
        (req.spec or "").strip() and f"[사양] {req.spec.strip()}",
        ("[장점]\n" + "\n".join("- " + str(p).strip() for p in (req.points or [])[:7] if str(p).strip())) if req.points else "",
        (req.ctx or "").strip()[:900]] if x)
    if not info.strip():
        return {"ok": False, "error": "상품 정보가 없어요 — 상세페이지를 먼저 만들어주세요."}
    prompt = ("너는 한국 스마트스토어 상세페이지 편집자야. 아래 상품 정보만 근거로 '이 제품 vs 일반 제품' 비교표 초안을 만들어.\n"
              "[반드시 지킬 것]\n"
              "1) 상품 정보에 실제로 있는 사실(소재·크기·무게·구성·기능)만 '이 제품' 칸에 써. 없는 수치·인증·성능을 지어내지 마.\n"
              "2) '일반 제품' 칸은 특정 회사·브랜드·상품명을 절대 쓰지 말고, '제품마다 다름', '별도 구매가 많음', "
              "'미포함인 경우가 많음'처럼 일반적이고 단정하지 않는 표현으로.\n"
              "3) 최고·1위·유일·완벽·100% 같은 과장, 근거 없는 우위 주장 금지. 각 칸은 15자 이내로 짧게.\n"
              "4) 비교 항목은 3~5개. 고객이 실제로 비교하는 항목(구성품, 소재, 크기/무게, 사용 편의 등) 위주.\n\n"
              f"[상품 정보]\n{info}\n\n"
              "[출력] 아래 형식만(마크다운·번호 금지):\n"
              "HEADA: (이 제품 열 이름, 8자 이내 — 모르면 '이 제품')\n"
              "HEADB: 일반 제품\n"
              "ROW: (항목) || (이 제품) || (일반 제품)\n(ROW 줄을 3~5개)")
    text, err = await _detail_gemini(gkey, prompt, temperature=0.4)
    if err:
        return err
    a, b, rows = "이 제품", "일반 제품", []
    for ln in text.splitlines():
        s = ln.strip()
        u = s.upper()
        if u.startswith("HEADA:"):
            a = s.split(":", 1)[1].strip()[:12] or a
        elif u.startswith("HEADB:"):
            b = s.split(":", 1)[1].strip()[:12] or b
        elif u.startswith("ROW:") and "||" in s:
            parts = [x.strip() for x in s.split(":", 1)[1].split("||")]
            if len(parts) >= 3 and parts[0]:
                rows.append({"k": parts[0][:20], "a": parts[1][:30], "b": parts[2][:30]})
    if not rows:
        return {"ok": False, "error": "비교표 초안을 만들지 못했어요. 다시 시도해주세요."}
    rows = rows[:5]
    for r in rows:                      # v188: '이 제품' 칸이 상품 정보에 근거가 있는지 표시(✓/⚠)
        r["ev"] = _cmp_evidence(r.get("a", ""), info)
    return {"ok": True, "compare": {"a": a, "b": b, "rows": rows},
            "unsure": sum(1 for r in rows if not r["ev"])}


class RewriteReq(BaseModel):
    gemini_key: str
    sentence: str = ""
    field_text: str = ""
    why: str = ""
    tone: str = "trust"
    ctx: str = ""


@app.post("/api/detail/rewrite")
async def detail_rewrite_api(req: RewriteReq):
    """🗣 v189 '이 문장만 다시 쓰기' — 말투 점검에 걸린 문장 하나만 사람 말투로 고친다(사실은 그대로)."""
    gkey = (req.gemini_key or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    sent = (req.sentence or "").strip()
    if not sent:
        return {"ok": False, "error": "고칠 문장이 없어요."}
    T = _dtone(req.tone)
    prompt = ("너는 스마트스토어에서 10년째 물건을 파는 판매자야. 아래 상세페이지 문장 '하나'만 사람이 쓴 것처럼 고쳐줘.\n"
              f"[톤] {T['label']} — {T['hint']}\n"
              f"[고칠 문장] {sent}\n"
              f"[걸린 이유] {req.why or 'AI가 쓴 티'}\n"
              f"[문단 전체(참고용)] {(req.field_text or '')[:600]}\n"
              + (f"[상품 정보(참고용)] {(req.ctx or '')[:900]}\n" if (req.ctx or '').strip() else "")
              + "[규칙]\n"
              "- 뜻과 사실(숫자·소재·기능)은 그대로. 새로운 사실·수치를 지어내지 마.\n"
              "- 형용사 대신 사실, 짧게: 한 문장 40자 안팎(길면 두 문장으로 나눠도 됨). 존댓말 유지.\n"
              "- 쓰지 말 것: 선사, 자랑, 경험해 보세요, 만나보세요, 완벽한, 혁신적인, 특별한, 지금 바로, 놓치지 마세요, 여러분, 당신의, 이모지, 느낌표.\n"
              "- 앞뒤 문장과 어미가 겹치지 않게 어미를 바꿔(예: ~요 → ~죠/~습니다/명사로 끝내기).\n"
              "[출력] 정확히 아래 한 줄만:\nTEXT: (고친 문장)")
    text, err = await _detail_gemini(gkey, prompt, temperature=0.7)
    if err:
        return err
    out = ""
    for ln in text.splitlines():
        if ln.strip().upper().startswith("TEXT:"):
            out = ln.strip()[5:].strip(); break
    out = _humanize_text(out or text.strip().splitlines()[0])
    if not out:
        return {"ok": False, "error": "다시 쓰지 못했어요. 다시 시도해주세요."}
    return {"ok": True, "text": out}


class RewriteManyReq(BaseModel):
    gemini_key: str
    items: list = []          # [{sentence, why}]
    tone: str = "trust"
    ctx: str = ""


@app.post("/api/detail/rewrite_many")
async def detail_rewrite_many_api(req: RewriteManyReq):
    """✏️ v190 말투 일괄 다시 쓰기 — 걸린 문장 전부를 Gemini 한 번으로 고친다(문장마다 T번호: 한 줄)."""
    gkey = (req.gemini_key or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    items = [it for it in (req.items or []) if isinstance(it, dict) and str(it.get("sentence", "")).strip()][:30]
    if not items:
        return {"ok": False, "error": "고칠 문장이 없어요."}
    T = _dtone(req.tone)
    listing = "\n".join(f"S{i + 1}: {str(it.get('sentence')).strip()}  (걸린 이유: {str(it.get('why') or 'AI 말투')[:60]})"
                         for i, it in enumerate(items))
    prompt = ("너는 스마트스토어에서 10년째 물건을 파는 판매자야. 아래 상세페이지 문장들을 하나씩, 사람이 쓴 것처럼 고쳐줘.\n"
              f"[톤] {T['label']} — {T['hint']}\n"
              + (f"[상품 정보(참고용)] {(req.ctx or '')[:900]}\n" if (req.ctx or '').strip() else "")
              + "[규칙]\n- 각 문장의 뜻과 사실(숫자·소재·기능)은 그대로, 새 사실·수치 금지.\n"
              "- 한 문장 40자 안팎(길면 두 문장 OK), 존댓말, 이모지·느낌표 금지.\n"
              "- 쓰지 말 것: 선사, 자랑, 경험해 보세요, 만나보세요, 완벽한, 혁신적인, 특별한, 지금 바로, 놓치지 마세요, 여러분, 당신의.\n"
              "- 바로 앞뒤 문장과 어미가 겹치지 않게(~요/~죠/~습니다/명사형을 섞어).\n"
              f"[고칠 문장]\n{listing}\n"
              f"[출력] 정확히 {len(items)}줄, 각 줄은 'T번호: 고친 문장' 형식만(예: T1: …).")
    text, err = await _detail_gemini(gkey, prompt, temperature=0.7)
    if err:
        return err
    out = [""] * len(items)
    import re as _re
    for ln in text.splitlines():
        m = _re.match(r"\s*T(\d+)\s*[:：]\s*(.+)$", ln.strip())
        if m:
            k = int(m.group(1)) - 1
            if 0 <= k < len(items):
                out[k] = _humanize_text(m.group(2))
    if not any(out):
        return {"ok": False, "error": "다시 쓰지 못했어요. 다시 시도해주세요."}
    return {"ok": True, "texts": out}


class LabelsReq(BaseModel):
    gemini_key: str
    name: str = ""
    ctx: str = ""
    bucket: str = ""


@app.post("/api/detail/labels")
async def detail_labels_api(req: LabelsReq):
    """🤖 v190 헤드라인 위 키워드 라벨 후보 3개 — 상품이 무엇인지 2~4단어(광고 문구 금지)."""
    gkey = (req.gemini_key or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    prompt = ("상세페이지 헤드라인 위에 작게 들어갈 '키워드 라벨' 후보 3개를 만들어줘.\n"
              f"[상품명] {(req.name or '').strip()}\n"
              + (f"[분류] {req.bucket}\n" if (req.bucket or '').strip() else "")
              + (f"[상품 정보] {(req.ctx or '')[:900]}\n" if (req.ctx or '').strip() else "")
              + "[규칙] 이 상품이 무엇인지 2~4단어(공백 포함 16자 이내). 세 후보는 각도를 다르게"
              "(예: 품목명 / 용도+품목 / 핵심 사양+품목). 광고 문구·최고·추천·이모지 금지. 상품 정보에 없는 사양 금지.\n"
              "[출력] 정확히 3줄, 'L: 라벨' 형식만.")
    text, err = await _detail_gemini(gkey, prompt, temperature=0.6)
    if err:
        return err
    labels = []
    for ln in text.splitlines():
        t = ln.strip()
        if t.upper().startswith("L:"):
            v = _humanize_text(t[2:].strip()).strip(" .")[:20]
            if v and v not in labels:
                labels.append(v)
    if not labels:
        return {"ok": False, "error": "라벨 후보를 만들지 못했어요. 다시 시도해주세요."}
    return {"ok": True, "labels": labels[:3]}


# 🎨 v193 AI 연출컷 — 사진 1장(제품)은 그대로 두고 배경·조명·장면만 바꾼 새 사진을 Gemini 이미지 모델로 만든다.
# 서버는 사진을 저장하지 않는다(받아서 Gemini에 넘기고 결과만 돌려줌). 이미지 생성은 무료 사용량이 없어 결제 등록된 키가 필요.
_AIIMG_MODELS = ["gemini-nano-banana-2.1", "gemini-3.1-flash-lite-image", "gemini-3.1-flash-image", "gemini-2.5-flash-image"]
_AIIMG_STYLES = {
    "studio": ("스튜디오", "Setting: a clean seamless studio backdrop in soft light gray, soft diffused studio lighting, "
                         "a subtle natural shadow beneath the product, premium catalog look."),
    "life": ("생활 장면", "Setting: a realistic everyday scene where this product is naturally used (infer the right place from "
                       "the product name), tasteful and uncluttered, shallow depth of field, warm natural light. "
                       "If any person appears, show only hands, never faces."),
    "table": ("원목 테이블", "Setting: on a natural light oak wooden table near a window, soft morning daylight, "
                          "one or two subtle neutral props that never cover the product."),
    "color": ("컬러 배경", "Setting: a solid soft pastel background with a simple matte geometric pedestal, "
                        "modern minimal brand-shoot style, soft shadows."),
    "outdoor": ("야외", "Setting: a bright outdoor environment that suits how this product is used, natural sunlight, "
                      "background softly blurred."),
    "dark": ("고급 어두운", "Setting: a dark luxurious background such as charcoal stone or deep fabric, "
                        "soft dramatic key light, elegant premium mood."),
}


def _aiimg_prompt(style, name, aspect):
    st = _AIIMG_STYLES.get(style) or _AIIMG_STYLES["studio"]
    return ("Using the provided product photo, create a new e-commerce product photograph. "
            "CRITICAL: the product itself must remain exactly identical to the input photo — same shape, proportions, "
            "colors, materials, logos, printed text, buttons and every detail. Do not redesign, add, remove, recolor or "
            "relabel any part of the product, and do not change its size relative to its parts. "
            "Do not add any text, captions, prices, watermarks, badges or logos to the image. "
            "Only change the background, surface, lighting and surrounding setting. "
            "Photorealistic, high quality, natural shadows and reflections, the whole product clearly in focus and fully visible. "
            + st[1] + (f" Product: {name}." if name else "")
            + (f" Output aspect ratio {aspect}." if aspect else ""))


def _find_image_b64(obj, depth=0):
    """응답 JSON 안에서 base64 이미지 데이터를 찾는다(generateContent·Interactions 응답 모양이 달라도)."""
    if depth > 8:
        return None, None
    if isinstance(obj, dict):
        mt = obj.get("mimeType") or obj.get("mime_type") or ""
        dt = obj.get("data")
        if isinstance(dt, str) and len(dt) > 200 and (str(mt).startswith("image/") or obj.get("type") == "image"):
            return dt, (mt or "image/png")
        for v in obj.values():
            d, m = _find_image_b64(v, depth + 1)
            if d:
                return d, m
    elif isinstance(obj, list):
        for v in obj:
            d, m = _find_image_b64(v, depth + 1)
            if d:
                return d, m
    return None, None


class AiImageReq(BaseModel):
    gemini_key: str
    image: str = ""            # data:image/jpeg;base64,… (브라우저가 1024px로 줄여서 보냄)
    style: str = "studio"
    name: str = ""
    aspect: str = "1:1"        # 1:1 / 4:5 / 3:4 / 16:9
    model: str = ""            # (선택) 특정 모델 강제


@app.post("/api/detail/aiimage")
async def detail_aiimage_api(req: AiImageReq):
    """🎨 AI 연출컷 1장 — 제품은 원본 그대로, 배경·장면만 바꾼 사진. 결과는 data URL로만 돌려주고 서버에 남기지 않는다."""
    import httpx, asyncio
    gkey = (req.gemini_key or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    img = (req.image or "").strip()
    if not img.startswith("data:image/") or ";base64," not in img:
        return {"ok": False, "error": "원본 사진을 읽지 못했어요."}
    mime, b64 = img[5:].split(";base64,", 1)
    if len(b64) > 6_000_000:
        return {"ok": False, "error": "원본 사진이 너무 커요(4MB 이하)."}
    aspect = req.aspect if req.aspect in ("1:1", "4:5", "3:4", "16:9") else "1:1"
    prompt = _aiimg_prompt(req.style, (req.name or "")[:60], aspect)
    models = ([req.model] if (req.model or "").strip() else []) + [m for m in _AIIMG_MODELS if m != req.model]
    last_err = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=90, write=20, pool=10)) as c:
            for m in models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
                body = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": b64}}]}],
                        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": aspect}}}
                r = await c.post(url, headers={"x-goog-api-key": gkey, "Content-Type": "application/json"}, json=body)
                if r.status_code == 200:
                    data, mt = _find_image_b64(r.json())
                    if data:
                        return {"ok": True, "image": f"data:{mt};base64,{data}", "model": m, "style": req.style}
                    last_err = "이미지가 없는 응답(안전 필터에 걸렸을 수 있어요)"
                    continue
                if r.status_code in (400, 404) and ("not found" in r.text.lower() or "not supported" in r.text.lower()
                                                    or "unknown" in r.text.lower() or r.status_code == 404):
                    last_err = f"{m} 모델 없음"; continue            # 다음 모델로
                if r.status_code in (401, 403):
                    return {"ok": False, "error": "Gemini 키 권한 오류 — 이미지 생성이 허용된 키인지 확인하세요."}
                if r.status_code == 429:
                    return {"ok": False, "error": "이미지 생성 한도(429) — 이미지 생성은 무료 사용량이 없어 결제 등록된 키가 필요해요. 결제 등록 후 다시 시도하세요."}
                if r.status_code in (500, 502, 503, 504):
                    last_err = f"구글 서버 일시 오류({r.status_code})"; await asyncio.sleep(1.2); continue
                last_err = f"{m} 오류({r.status_code}) {r.text[:120]}"
            # 마지막 수단: Interactions API(2026 문서 방식)
            try:
                r = await c.post("https://generativelanguage.googleapis.com/v1beta/interactions",
                                 headers={"x-goog-api-key": gkey, "Content-Type": "application/json"},
                                 json={"model": _AIIMG_MODELS[0],
                                       "input": [{"type": "text", "text": prompt}, {"type": "image", "mime_type": mime, "data": b64}],
                                       "response_format": {"type": "image", "aspect_ratio": aspect}})
                if r.status_code == 200:
                    data, mt = _find_image_b64(r.json())
                    if data:
                        return {"ok": True, "image": f"data:{mt};base64,{data}", "model": _AIIMG_MODELS[0] + " (interactions)", "style": req.style}
                last_err = last_err or f"interactions 오류({r.status_code})"
            except Exception as exc:  # noqa: BLE001
                last_err = last_err or f"interactions 연결 오류({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Gemini 연결 오류({type(exc).__name__}) — 잠시 후 다시 시도해주세요."}
    return {"ok": False, "error": "AI 연출컷을 만들지 못했어요 — " + (last_err or "알 수 없는 오류")}


class CompareCheckReq(BaseModel):
    ctx: str = ""
    name: str = ""
    spec: str = ""
    points: list = []
    rows: list = []


@app.post("/api/detail/comparecheck")
async def detail_comparecheck_api(req: CompareCheckReq):
    """편집 중인 비교표의 '이 제품' 칸을 상품 정보와 다시 대조(Gemini 호출 없음) — 사용자가 고친 뒤 ✓/⚠ 갱신용."""
    info = "\n".join([req.name or "", req.spec or "", "\n".join(str(p) for p in (req.points or [])), (req.ctx or "")[:2000]])
    return {"ok": True, "ev": [_cmp_evidence(str((r or {}).get("a", "")), info) for r in (req.rows or [])[:12]]}


class ImgCheckReq(BaseModel):
    urls: list = []


@app.post("/api/img/check")
async def img_check_api(req: ImgCheckReq):
    """저장본 사진 일괄 점검 — 각 URL이 실제 이미지로 받아지는지(프록시와 같은 방식) 확인.
    전체를 내려받지 않고 앞부분만(Range) 요청해 가볍게 확인한다. 최대 80개."""
    import httpx, asyncio
    from urllib.parse import urlsplit
    urls = []
    for u in (req.urls or []):
        if isinstance(u, str) and u.startswith(("http://", "https://")) and u not in urls:
            urls.append(u)
    urls = urls[:80]
    sem = asyncio.Semaphore(8)
    result = {}

    async def _one(c, u):
        async with sem:
            try:
                p = urlsplit(u)
                h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                     "Referer": f"{p.scheme}://{p.netloc}/", "Range": "bytes=0-2047",
                     "Accept": "image/*,*/*;q=0.8"}
                r = await c.get(u, headers=h)
                ct = (r.headers.get("content-type") or "").lower()
                result[u] = (r.status_code in (200, 206)) and ct.startswith("image/")
            except Exception:  # noqa: BLE001
                result[u] = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=6, read=10, write=6, pool=6),
                                     follow_redirects=True) as c:
            await asyncio.gather(*[_one(c, u) for u in urls])
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "사진 점검 중 연결 오류"}
    return {"ok": True, "result": result}


class ToneHeadsReq(BaseModel):
    gemini_key: str
    ctx: str = ""
    name: str = ""


@app.post("/api/detail/toneheads")
async def detail_toneheads_api(req: ToneHeadsReq):
    """톤 프리뷰 — 현재 상품으로 4가지 톤(신뢰/감성/가성비/미니멀) 헤드라인 한 줄씩 한 번에 생성.
    (톤별로 따로 호출하지 않고 1회 호출로 끝내 Gemini 한도 보호)"""
    gkey = (req.gemini_key or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    ctx = (req.ctx or "").strip()
    if not ctx and not (req.name or "").strip():
        return {"ok": False, "error": "상품 정보가 없어요 — 상세페이지를 먼저 만들어주세요."}
    prompt = ("너는 한국 스마트스토어 카피라이터야. 아래 상품으로 '서로 다른 톤'의 헤드라인(상단 큰 문구)을 "
              "각 한 줄씩 써줘. 각 25자 내외, 과장·허위(최고/1위/의학효과) 금지, 서로 느낌이 확실히 다르게.\n\n"
              f"[상품 정보]\n{(req.name or '').strip()}\n{ctx}\n\n"
              "[출력] 정확히 아래 4줄만(마크다운·번호 금지):\n"
              "TRUST: (신뢰형 — 꼼꼼하고 정중하게 믿음 주는 한 줄)\n"
              "EMOTION: (친근형 — 옆에서 말해주듯 따뜻한 한 줄)\n"
              "VALUE: (가성비형 — 가격·구성 가치가 와닿는 한 줄)\n"
              "MINIMAL: (담백형 — 군더더기 없이 핵심만 한 줄)")
    text, err = await _detail_gemini(gkey, prompt, temperature=0.8)
    if err:
        return err
    heads = {}
    for ln in text.splitlines():
        s = ln.strip()
        for tag, key in (("TRUST:", "trust"), ("EMOTION:", "emotion"), ("VALUE:", "value"), ("MINIMAL:", "minimal")):
            if s.upper().startswith(tag):
                heads[key] = s.split(":", 1)[1].strip()
    if not heads:
        return {"ok": False, "error": "헤드라인을 만들지 못했어요. 다시 시도해주세요."}
    return {"ok": True, "heads": heads}


@app.get("/api/detail/meta")
async def detail_meta():
    """상세페이지 UI용 메타 — 톤 목록 + 상품정보고시 카테고리별 필수항목."""
    tones = [{"key": k, "label": v["label"], "accent": v["accent"]}
             for k, v in _DETAIL_TONES.items()]
    notices = [{"key": k, "label": v["label"], "fields": v["fields"]}
               for k, v in _NOTICE_TEMPLATES.items()]
    return {"ok": True, "tones": tones, "notices": notices}


class DetailRegenReq(BaseModel):
    gemini_key: str
    ctx: str = ""             # 상품 컨텍스트(최초 생성 때 서버가 돌려준 값 — 공급사 재조회 불필요)
    name: str = ""
    imgs: list = []
    tone: str = "trust"
    target: str = ""
    notice: list = []
    notice_cat: str = ""
    origin: str = ""
    keep: dict = {}           # 유지할 기존 섹션들
    only: str = ""            # "" = 전체 재생성, "head"/"points"/"scene"/"close" = 부분
    restyle: bool = False     # True = Gemini 호출 없이 톤/고시만 다시 입힘
    use_slots: bool = False   # True면 slots(이미지 배치)를 그대로 적용, False면 자동
    slots: list = []          # [슬롯0,1,2,3] 각 imgs 인덱스 또는 -1(빈칸)
    order: list = []          # 블록 순서: ["points","scene","tip"]
    guide: str = ""           # 구매 안내(배송·교환·환불) 블록 텍스트(빈값이면 없음)
    length: str = "rich"
    toc: bool = False
    soldout: str = "show"
    reviews: str = ""
    sectheme: bool = False
    banner: bool = False
    img_place: str = "spread"   # 자동 이미지 배치: spread(분산)/top(상단몰아)/bottom(하단몰아)
    img_size: str = "big"       # 본문 사진 폭: big(크게)/med(보통)/small(작게)
    img_gap: str = "normal"     # 사진 위아래 여백: tight(좁게)/normal(보통)/wide(넓게)
    graphic: bool = False       # 그래픽 요소(혜택 칩·정보 배지·구분선) 넣기
    img_round: str = "auto"     # 사진 모서리: auto(톤기본)/sharp(각지게)/soft(살짝)/round(많이)
    img_border: str = "none"    # 사진 테두리: none/gray(얇은회색)/brand(브랜드색)
    graphic_parts: str = ""     # 그래픽 블록 개별 on/off: "chips,badges,divider" 중 켤 것만
    img_shadow: str = "none"    # 사진 그림자: none/soft(은은한)
    captions: dict = {}         # 사진별 캡션 {이미지 URL: 한 줄 설명}
    layout: str = "basic"       # 상세페이지 유형: basic/problem/spec/story/check
    design: str = "editorial"   # v191 디자인: editorial(새 기본)/premium(모던)/classic(v187까지)
    hide: str = ""              # v188 숨길 블록(콤마): intro,scene,tip,details,spec,compare,reviews,faq,close
    spacing: str = "normal"     # v189 프리미엄 섹션 여백: tight/normal/wide
    eyebrow: str = ""           # v189 헤드라인 위 키워드 라벨 자동값(분류명) — sections.eyebrow/label이 우선
    align: str = "center"       # v192 에디토리얼 글 정렬: center(기본·고급형)/left(잡지형)
    luxe: str = "gold"          # v193 고급형 장식색: gold/rose/silver
    counts: dict = {}           # v193 '풍부' 개수 조절 {points, details, faqs}


@app.post("/api/detail/regen")
async def detail_regen_api(req: DetailRegenReq):
    """상세페이지 재생성 / 부분 재생성 / 톤 리스타일 (공급사 API 재호출 없음)."""
    gkey = (req.gemini_key or "").strip()
    keep = dict(req.keep or {})
    _slots = req.slots if req.use_slots else None
    _order = req.order or None
    # 톤/고시/이미지배치/순서만 다시 입히기 — Gemini 호출 없이 즉시
    if req.restyle:
        html = _build_detail_html(keep, req.imgs, req.name, req.tone,
                                  req.notice, req.notice_cat, req.origin, slots=_slots, order=_order, guide=req.guide, toc=req.toc, soldout=getattr(req,"soldout","show"), reviews=getattr(req,"reviews",""), sectheme=getattr(req,"sectheme",False), banner=getattr(req,"banner",False), img_place=getattr(req,"img_place","spread"), img_size=getattr(req,"img_size","big"), img_gap=getattr(req,"img_gap","normal"), graphic=getattr(req,"graphic",False), img_round=getattr(req,"img_round","auto"), img_border=getattr(req,"img_border","none"), graphic_parts=getattr(req,"graphic_parts",""), img_shadow=getattr(req,"img_shadow","none"), captions=getattr(req,"captions",{}), layout=getattr(req,"layout","basic"), design=getattr(req,"design","editorial"), hide=getattr(req,"hide",""), spacing=getattr(req,"spacing","normal"), eyebrow=getattr(req,"eyebrow",""), align=getattr(req,"align","center"), luxe=getattr(req,"luxe","gold"))
        return {"ok": True, "html": html, "sections": keep, "img_count": len(req.imgs)}
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    if not (req.ctx or "").strip():
        return {"ok": False, "error": "상품 정보(ctx)가 없어요 — 상세페이지를 먼저 만들어주세요."}
    only = (req.only or "").strip()
    prompt = _detail_prompt(req.ctx, req.target, req.tone, only=only, length=req.length, counts=getattr(req, "counts", None))
    text, err = await _detail_gemini(gkey, prompt)
    if err:
        return err
    parsed = _facts_verify(_parse_detail(text), req.ctx)
    if only == "head":
        keep["head"] = parsed.get("head") or keep.get("head", "")
    elif only == "points":
        keep["points"] = parsed.get("points") or keep.get("points", [])
    elif only == "scene":
        keep["scene"] = parsed.get("scene") or keep.get("scene", "")
    elif only == "close":
        keep["close"] = parsed.get("close") or keep.get("close", "")
    else:
        _opts = keep.get("options")   # 옵션표는 공급사 API에서 온 값 — Gemini 재생성이 지우지 않게 보존
        _axes = keep.get("option_axes")
        _stock = keep.get("option_stock")
        _bantxt = keep.get("banner_text")
        _banicons = keep.get("banner_icons")
        _compare = keep.get("compare")      # 사용자가 직접 넣은 비교표는 재생성해도 유지
        _eyebrow = keep.get("eyebrow")      # 사용자가 직접 쓴 키워드 라벨도 유지
        keep = parsed  # 전체 재생성 (상세설명이 새로 바뀌므로 문단 소제목 detail_heads는 버림)
        if _compare:
            keep["compare"] = _compare
        if _eyebrow:
            keep["eyebrow"] = _eyebrow
        if _opts:
            keep["options"] = _opts
        if _axes:
            keep["option_axes"] = _axes
        if _stock:
            keep["option_stock"] = _stock
        if _bantxt:
            keep["banner_text"] = _bantxt
        if _banicons:
            keep["banner_icons"] = _banicons
    if not keep.get("head"):
        keep["head"] = req.name
    html = _build_detail_html(keep, req.imgs, req.name, req.tone,
                              req.notice, req.notice_cat, req.origin, slots=_slots, order=_order, guide=req.guide, toc=req.toc, soldout=getattr(req,"soldout","show"), reviews=getattr(req,"reviews",""), sectheme=getattr(req,"sectheme",False), banner=getattr(req,"banner",False), img_place=getattr(req,"img_place","spread"), img_size=getattr(req,"img_size","big"), img_gap=getattr(req,"img_gap","normal"), graphic=getattr(req,"graphic",False), img_round=getattr(req,"img_round","auto"), img_border=getattr(req,"img_border","none"), graphic_parts=getattr(req,"graphic_parts",""), img_shadow=getattr(req,"img_shadow","none"), captions=getattr(req,"captions",{}), layout=getattr(req,"layout","basic"), design=getattr(req,"design","editorial"), hide=getattr(req,"hide",""), spacing=getattr(req,"spacing","normal"), eyebrow=getattr(req,"eyebrow",""), align=getattr(req,"align","center"), luxe=getattr(req,"luxe","gold"))
    return {"ok": True, "html": html, "sections": keep, "img_count": len(req.imgs)}


class DetailFromDgReq(BaseModel):
    gemini_key: str
    api_key: str
    no: str
    target: str = ""
    tone: str = "trust"
    notice: list = []
    notice_cat: str = ""
    guide: str = ""
    length: str = "rich"
    toc: bool = False
    soldout: str = "show"
    reviews: str = ""
    sectheme: bool = False
    banner: bool = False
    design: str = "editorial"
    hide: str = ""
    spacing: str = "normal"
    align: str = "center"
    luxe: str = "gold"
    counts: dict = {}
    layout: str = "auto"      # 상세페이지 유형: auto(추천)/basic/problem/spec/story/check


@app.post("/api/detail/from_domeggook")
async def detail_from_domeggook_api(req: DetailFromDgReq):
    """🪄 도매꾹 상품 → 상세페이지 한 번에. 도매꾹에서 정보·이미지를 가져와 Gemini로 문구 생성."""
    import html as _html
    import httpx
    from urllib.parse import urlencode
    gkey = (req.gemini_key or "").strip()
    akey = (req.api_key or "").strip()
    no = str(req.no or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    if not akey or not no:
        return {"ok": False, "error": "도매꾹 키·상품번호가 필요해요."}
    # 1) 도매꾹 상세 조회
    params = {"ver": "4.5", "mode": "getItemView", "aid": akey, "no": no, "om": "json"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=25, write=10, pool=10)) as c:
            r = await c.get("https://domeggook.com/ssl/api/?" + urlencode(params))
        if r.status_code >= 400:
            return {"ok": False, "error": f"도매꾹 상품 조회 실패({r.status_code})."}
        dd = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"도매꾹 연결 오류: {type(exc).__name__}"}
    root = dd.get("domeggook") or dd
    basis = root.get("basis") or {}

    def _cd(v):
        if isinstance(v, dict):
            return str(v.get("#cdata") or v.get("cdata") or v.get("$") or "")
        return "" if v is None else str(v)
    name = _cd(basis.get("title")) or _cd(root.get("title"))
    if not name:
        return {"ok": False, "error": "도매꾹에서 상품을 찾지 못했어요."}
    imgs_raw = root.get("thumb") or basis.get("thumb") or []
    if isinstance(imgs_raw, str):
        imgs_raw = [imgs_raw]
    imgs = _clean_imgs([_cd(x) for x in imgs_raw], cap=10)
    kwnode = (basis.get("keywords") or {}).get("kw") if isinstance(basis.get("keywords"), dict) else None
    kws = []
    if isinstance(kwnode, list):
        kws = [_cd(k) for k in kwnode if _cd(k)]
    elif kwnode:
        kws = [_cd(kwnode)]
    # 2) Gemini 문구 생성 (공통 헬퍼)
    ctx = (f"[상품명]\n{name}\n"
           + (f"[관련 키워드]\n{', '.join(kws[:8])}\n" if kws else ""))
    prompt = _detail_prompt(ctx, req.target, req.tone, length=req.length, counts=getattr(req, "counts", None))
    text, err = await _detail_gemini(gkey, prompt)
    if err:
        return err
    sections = _facts_verify(_parse_detail(text), ctx)   # 숫자 강조띠: 근거 없는 숫자 제거
    if not sections.get("head"):
        sections["head"] = name
    # 옵션 표(있으면) — 도매꾹 getItemView 의 options 노드를 방어적으로 파싱
    try:
        onode = root.get("options") or basis.get("options") or {}
        opts_raw = []
        if isinstance(onode, dict):
            oo = onode.get("opt") or onode.get("option") or []
            opts_raw = oo if isinstance(oo, list) else [oo]
        elif isinstance(onode, list):
            opts_raw = onode
        opt_list = []
        for o in opts_raw[:12]:
            if isinstance(o, dict):
                nm = _cd(o.get("name") or o.get("title") or "")
                vraw = o.get("value") or o.get("values") or o.get("val") or ""
                if isinstance(vraw, list):
                    vv = " / ".join(_cd(x) for x in vraw if _cd(x))
                else:
                    vv = _cd(vraw)
                line = (f"{nm}: {vv}" if nm and vv else (vv or nm)).strip(": ").strip()
            else:
                line = _cd(o).strip()
            if line:
                opt_list.append(line)
        if opt_list:
            sections["options"] = opt_list
    except Exception:  # noqa: BLE001
        pass
    dg_cat = _dg_category(root, basis, _cd)       # v188: 도매꾹 카테고리도 추천에 반영(없으면 상품명·키워드만)
    rec = _recommend_layout(name, dg_cat, " ".join(kws))
    rec["category"] = dg_cat
    lay = _pick_layout(getattr(req, "layout", "auto"), rec)
    detail_html = _build_detail_html(sections, imgs, name, req.tone,
                                     req.notice, req.notice_cat, guide=req.guide, toc=req.toc, soldout=getattr(req,"soldout","show"), reviews=getattr(req,"reviews",""), sectheme=getattr(req,"sectheme",False), banner=getattr(req,"banner",False), layout=lay, design=getattr(req,"design","editorial"), hide=getattr(req,"hide",""), spacing=getattr(req,"spacing","normal"), eyebrow=(rec.get("label", "") if rec.get("bucket") != "etc" else ""), align=getattr(req,"align","center"), luxe=getattr(req,"luxe","gold"))
    return {"ok": True, "html": detail_html, "name": name, "img_count": len(imgs),
            "sections": sections, "imgs": imgs, "ctx": ctx, "tone": req.tone,
            "layout": lay, "layout_rec": rec}


@app.post("/api/domeggook/item")
async def domeggook_item_api(req: DomeggookItemReq):
    """도매꾹 상품상세정보(getItemView) — 상품번호로 옵션·배송·판매자 등 상세 조회."""
    import httpx
    from urllib.parse import urlencode
    key = (req.api_key or "").strip()
    no = str(req.no or "").strip()
    if not key or not no:
        return {"ok": False, "error": "API 키·상품번호가 필요해요."}
    params = {"ver": "4.5", "mode": "getItemView", "aid": key, "no": no, "om": "json"}
    url = "https://domeggook.com/ssl/api/?" + urlencode(params)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=25, write=10, pool=10)) as c:
            r = await c.get(url)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"연결 오류: {type(exc).__name__}"}
    if r.status_code >= 400:
        return {"ok": False, "error": f"상세 조회 실패({r.status_code})."}
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "응답 해석 실패."}
    root = data.get("domeggook") or data
    if isinstance(root, dict) and root.get("errors"):
        return {"ok": False, "error": f"도매꾹 API 오류: {root['errors']}"}
    basis = root.get("basis") or {}
    price_node = root.get("price") or {}
    deli = root.get("deli") or {}
    seller = root.get("seller") or {}

    def _cd(v):   # CDATA/그냥 문자열/dict 모두 문자열로
        if isinstance(v, dict):
            return str(v.get("#cdata") or v.get("cdata") or v.get("$") or "")
        return "" if v is None else str(v)

    title = _cd(basis.get("title")) or _cd(root.get("title"))
    status = _cd(basis.get("status"))
    tax = _cd(basis.get("tax"))
    section = _cd(basis.get("section"))
    date_reg = _cd(basis.get("dateReg"))
    # 키워드
    kws = []
    kwnode = (basis.get("keywords") or {}).get("kw") if isinstance(basis.get("keywords"), dict) else None
    if isinstance(kwnode, list):
        kws = [_cd(k) for k in kwnode if _cd(k)]
    elif kwnode:
        kws = [_cd(kwnode)]
    # 수량별 단가 파싱: "1+9850|11+9800|301+9750" → [{qty:1,price:9850},...]
    tiers = []
    raw_price = _cd(price_node.get("dome")) or _cd(price_node.get("supply")) or _cd(price_node.get("price"))
    for seg in raw_price.split("|"):
        seg = seg.strip()
        if "+" in seg:
            a, b = seg.split("+", 1)
            try:
                tiers.append({"qty": int(a), "price": int(b)})
            except Exception:  # noqa: BLE001
                pass
    base_price = tiers[0]["price"] if tiers else None
    sample_price = None
    try:
        sample_price = int(_cd(price_node.get("sample")))
    except Exception:  # noqa: BLE001
        sample_price = None
    # 이미지
    imgs = root.get("thumb") or basis.get("thumb") or []
    if isinstance(imgs, str):
        imgs = [imgs]
    imgs = [_cd(x) for x in imgs if _cd(x).startswith("http")][:5]
    detail = {
        "no": no, "title": title, "status": status, "tax": tax,
        "section": section, "date_reg": date_reg, "keywords": kws[:8],
        "price": base_price, "tiers": tiers[:8], "sample": sample_price,
        "origin": _cd(basis.get("origin")),
        "unitQty": _cd(basis.get("unitQty")) or _cd(root.get("unitQty")),
        "seller_id": _cd(seller.get("id")), "seller_nick": _cd(seller.get("nick")),
        "seller_grade": _cd(seller.get("grade")),
        "deli_who": _cd(deli.get("who")), "deli_fee": _cd(deli.get("fee")),
        "deli_merge": _cd(deli.get("merge")),
        "nego": _cd(basis.get("nego")),
        "url": _cd(basis.get("url")) or _cd(root.get("url")),
        "thumbs": imgs,
    }
    return {"ok": True, "item": detail}


@app.post("/api/ownerclan/search")
async def ownerclan_search_api(req: OwnerClanReq):
    """🛍 오너클랜 상품 검색 — 회원 각자의 판매사 계정으로 공식 API 조회.
    비밀번호는 토큰 발급에만 쓰고 서버에 저장하지 않는다."""
    u = (req.username or "").strip()
    p = req.password or ""
    if not u or not p:
        return {"ok": False, "error": "오너클랜 판매사 ID/비밀번호를 넣어주세요."}
    if not (req.keyword or "").strip():
        return {"ok": False, "error": "검색어를 넣어주세요."}
    _t0 = _time.time()
    try:
        token = await _ownerclan_token(u, p, req.sandbox)
        items = await _ownerclan_search(token, req)
    except NaverHubAuth as exc:
        return {"ok": False, "error": str(exc), "elapsed_ms": int((_time.time() - _t0) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"오너클랜 연결 오류: {type(exc).__name__}"}
    _elapsed = int((_time.time() - _t0) * 1000)
    # 서버 콘솔에도 남겨 어떤 검색어/정렬이 느린지 눈으로 파악(진단용)
    try:
        print(f"[OC검색] kw='{req.keyword}' sort={req.sort} → {len(items)}개 · {_elapsed}ms", flush=True)
    except Exception:  # noqa: BLE001
        pass
    # 결과가 없을 때 원인 안내(특히 샌드박스는 실제 상품이 거의 없음)
    if not items:
        if req.sandbox:
            return {"ok": True, "items": [], "stat": {}, "keyword": req.keyword,
                    "sandbox": True, "elapsed_ms": _elapsed, "sort": req.sort,
                    "note": ("샌드박스(테스트) 환경에는 실제 상품이 거의 없어서 검색 결과가 "
                             "비어 있을 수 있어요. 운영(production) API가 승인돼 있으면 "
                             "'샌드박스' 체크를 끄고 다시 검색해보세요.")}
        return {"ok": True, "items": [], "stat": {}, "keyword": req.keyword,
                "sandbox": False, "elapsed_ms": _elapsed, "sort": req.sort,
                "note": ("이 검색어로는 오너클랜에 상품이 없어요. 더 일반적인 단어"
                         "(예: '청소기'→'무선청소기','핸디청소기')로 바꿔보세요.")}
    # 통계
    prices = [it["price"] for it in items if isinstance(it.get("price"), (int, float)) and it["price"] > 0]
    stat = {}
    if prices:
        stat = {"min": int(min(prices)), "max": int(max(prices)),
                "avg": int(sum(prices) / len(prices)), "count": len(items)}
    return {"ok": True, "items": items, "stat": stat, "keyword": req.keyword,
            "sandbox": req.sandbox, "elapsed_ms": _elapsed, "sort": req.sort}


class OwnerClanItemReq(BaseModel):
    username: str
    password: str
    key: str
    sandbox: bool = False


@app.post("/api/ownerclan/item")
async def ownerclan_item_api(req: OwnerClanItemReq):
    """🔎 오너클랜 단일 상품 상세 — 실제 등록 상품인지 확인 + 상세정보(배송비·원산지·
    제조사·권장가·옵션/재고)를 앱 안에서 바로 보여준다."""
    import httpx
    from urllib.parse import quote
    u = (req.username or "").strip()
    p = req.password or ""
    key = (req.key or "").strip()
    if not u or not p or not key:
        return {"ok": False, "error": "상품 코드를 확인해주세요."}
    try:
        token = await _ownerclan_token(u, p, req.sandbox)
    except NaverHubAuth as exc:
        return {"ok": False, "error": str(exc)}
    kk = key.replace('"', '\\"')
    query = ('query { item(key: "' + kk + '") { key name model production origin '
             'price fixedPrice pricePolicy shippingFee shippingType status content '
             'taxFree returnable guaranteedShippingPeriod boxQuantity '
             'category { fullName } images(size: medium) '
             'options { price quantity optionAttributes { name value } } } }')
    base = ("https://api-sandbox.ownerclan.com/v1/graphql" if req.sandbox
            else "https://api.ownerclan.com/v1/graphql")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=40.0, write=10.0, pool=10.0)) as c:
            r = await c.get(base + "?query=" + quote(query),
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            return {"ok": False, "error": f"상세 조회 실패({r.status_code})"}
        data = r.json()
    except httpx.ReadTimeout:
        return {"ok": False, "error": "오너클랜 응답이 느려요(타임아웃). 잠시 후 다시 눌러주세요."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"연결 오류: {type(exc).__name__}"}
    if data.get("errors"):
        return {"ok": False, "error": f"API 오류: {data['errors'][0].get('message','')}"}
    n = (data.get("data") or {}).get("item")
    if not n:
        return {"ok": False, "error": "해당 상품을 찾지 못했어요(코드 확인)."}
    # 상세설명은 HTML/길 수 있어 태그 제거 후 앞부분만
    content = _strip_tags(n.get("content") or "")
    content = (content[:400] + "…") if len(content) > 400 else content
    opts = []
    for o in (n.get("options") or [])[:20]:
        attrs = " / ".join(f'{a.get("name")}:{a.get("value")}'
                           for a in (o.get("optionAttributes") or []))
        opts.append({"label": attrs or "기본", "price": o.get("price"),
                     "qty": o.get("quantity")})
    imgs = n.get("images") or []
    return {"ok": True, "item": {
        "key": n.get("key"), "name": n.get("name"), "model": n.get("model"),
        "production": n.get("production"), "origin": n.get("origin"),
        "price": n.get("price"), "fixedPrice": n.get("fixedPrice"),
        "shippingFee": n.get("shippingFee"), "shippingType": n.get("shippingType"),
        "status": n.get("status"), "content": content,
        "taxFree": n.get("taxFree"), "returnable": n.get("returnable"),
        "guaranteedShippingPeriod": n.get("guaranteedShippingPeriod"),
        "boxQuantity": n.get("boxQuantity"),
        "category": (n.get("category") or {}).get("fullName") or "",
        "images": imgs[:5], "options": opts}}


class OwnerClanBestReq(BaseModel):
    username: str
    password: str
    keyword: str
    client_id: str = ""         # 네이버 허브(수요 측정용, 선택)
    client_secret: str = ""
    sandbox: bool = False
    w_stock: int = 30           # 재고 가중치(조정 가능)
    w_price: int = 25           # 가격경쟁력 가중치
    w_margin: int = 30          # 마진 여력 가중치(권장가-공급가, 상품마다 다름)


@app.post("/api/ownerclan/best")
async def ownerclan_best_api(req: OwnerClanBestReq):
    """⭐ 최적 상품 골라주기 — 오너클랜 실제 원가·재고(공식 API) + 네이버 수요(공식 API)를
    합쳐 '지금 떼다 팔기 좋은' 상품을 점수순으로. 크롤링 없이 공식 API만 사용."""
    u = (req.username or "").strip()
    p = req.password or ""
    kw = (req.keyword or "").strip()
    if not u or not p:
        return {"ok": False, "error": "오너클랜 판매사 ID/비밀번호를 넣어주세요."}
    if not kw:
        return {"ok": False, "error": "검색어를 넣어주세요."}
    # 1) 오너클랜 상품 (실제 원가·재고)
    try:
        token = await _ownerclan_token(u, p, req.sandbox)
        sreq = OwnerClanReq(username=u, password=p, keyword=kw,
                            sort="default", sandbox=req.sandbox, first=100)
        items = await _ownerclan_search(token, sreq)
    except NaverHubAuth as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"오너클랜 연결 오류: {type(exc).__name__}"}
    if not items:
        return {"ok": True, "items": [], "keyword": kw, "demand": None,
                "note": "오너클랜에 이 검색어 상품이 없어요(운영 환경·검색어 확인)."}
    # 관련도 필터(정밀): 상품명에 검색어 토큰이 얼마나 들어갔는지로 관련도를 매긴다.
    kw_tokens = [t for t in re.findall(r"[가-힣A-Za-z0-9]{2,}", kw) if t not in _STOP_WORDS]
    if not kw_tokens:
        kw_tokens = [kw]
    # 붙여 쓴 검색어(예: '무선청소기') 대응 — 3글자+ 부분문자열도 관련어로 취급
    kw_subs = set(kw_tokens)
    for t in kw_tokens + [kw]:
        for L in range(3, min(len(t), 6) + 1):
            for i in range(0, len(t) - L + 1):
                kw_subs.add(t[i:i + L])

    def _relevance(it):
        """검색어의 '모든 단어가 얼마나 들어갔는지'(커버리지)로 관련도를 매긴다.
        오너클랜은 여러 단어를 OR로 검색하므로, 한 단어만 맞은 엉뚱한 상품
        (예: '화장대 쓰레기통'에 쓰레기통만 있는 것)을 커버리지로 걸러낸다."""
        nm = (it.get("name") or "")
        if not nm:
            return 0
        words = nm.split()
        nwords = len(words)
        toks = kw_tokens if kw_tokens else [kw]
        # 각 검색어 토큰이 상품명에 있는지 + 가장 앞선 매칭 위치
        present = [t for t in toks if t in nm]
        coverage = len(present) / len(toks)            # 0~1: 검색어를 얼마나 담았나
        first_pos = None
        for i, wd in enumerate(words):
            if any(t in wd for t in toks):
                first_pos = i
                break
        if first_pos is None:
            # 단어 경계로는 못 찾음 — 부분문자열만 있으면 아주 약하게
            return 1 if (coverage >= 0.99 and any(s in nm for s in kw_subs)) else 0
        front = first_pos <= 1
        first_half = first_pos < max(2, nwords // 2)
        stuffed = nwords >= 8 and not first_half        # 긴 이름 + 뒤쪽 = 끼워넣기
        # ── 판정 ──
        if coverage >= 0.999:                           # 검색어 '모든' 단어 포함
            if nwords >= 8 and not first_half:
                return 0                                # 아주 긴 이름 + 뒤쪽 = 끼워넣기
            if front:
                return 3                                # 앞쪽 = 진짜 그 상품
            if first_half:
                return 2                                # 앞 절반
            return 1                                    # 뒤쪽에만 = 끼워넣기 의심(약함)
        if coverage >= 0.5:                             # 절반만 포함(단어 일부 빠짐)
            # 단일 검색어(토큰 1개)면 0.5가 곧 100%라 이 분기 안 옴.
            # 다단어인데 일부만 맞음 = 엉뚱할 위험 → 앞쪽이어도 약하게만
            return 1 if (front and not stuffed) else 0
        return 0                                        # 대부분 안 맞음 = 다른 상품

    for it in items:
        it["_rel"] = _relevance(it)
    strong = [it for it in items if it["_rel"] >= 2]     # 검색어 전부 포함(진짜 매칭)
    mid = [it for it in items if it["_rel"] == 1]
    match_note = None
    if len(strong) >= 3:
        items = strong
    elif len(strong) >= 1:
        items = strong + mid
        match_note = "일부는 검색어 일부만 일치해요 — 상품명·이미지를 꼭 확인하세요."
    else:
        # 검색어를 다 담은 상품이 없음 = 그런 조합 상품이 오너클랜에 거의 없다는 뜻
        items = (strong + mid) or items
        match_note = ("'" + kw + "' 전체를 담은 상품이 거의 없어요. 두 단어를 한 상품에서 "
                      "찾기 어려우면 한 단어로(예: '화장대'만) 검색해보세요. 상품명·이미지도 꼭 확인하세요.")
    # 2) 네이버 수요 (공식 검색어 트렌드 1회 — 합법)
    demand = None
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if cid and csec:
        try:
            start, end = _date_range(12)
            body = {"startDate": start, "endDate": end, "timeUnit": "month",
                    "keywordGroups": [{"groupName": kw, "keywords": [kw]}]}
            data, _ = await _hub_datalab_probe(cid, csec, _TREND_PATHS, body)
            res = (data.get("results") or [])
            if res:
                t = _trend_of(res[0].get("data") or [])
                demand = {"rise": t.get("rise", 0.0), "stage": t.get("stage"),
                          "level": t.get("level"), "rollover": t.get("rollover", False)}
        except Exception:  # noqa: BLE001
            demand = None
    # 3) 상품별 '기회 점수' — 재고·마진여력·가격경쟁력 (수요는 키워드 공통)
    prices = [it["price"] for it in items
              if isinstance(it.get("price"), (int, float)) and it["price"] > 0]
    med = sorted(prices)[len(prices) // 2] if prices else 0
    w_stock = max(0, min(60, req.w_stock))
    w_price = max(0, min(60, req.w_price))
    w_margin = max(0, min(60, req.w_margin))
    # 수요는 키워드 공통이라 순위엔 영향 없음 — '전체 가점'으로만(모든 상품 동일)
    demand_bonus = 0.0
    if demand:
        demand_bonus = max(0.0, min(15.0, demand["rise"] / 40.0 * 15.0))
        if demand.get("rollover"):
            demand_bonus -= 7.5
    scored = []
    for it in items:
        price = it.get("price") if isinstance(it.get("price"), (int, float)) else None
        fixed = it.get("fixedPrice") if isinstance(it.get("fixedPrice"), (int, float)) else None
        instock = (it.get("status") == "available")
        s = 0.0
        reasons = []
        # 재고(상품마다 다를 수 있음)
        if instock:
            s += w_stock
            reasons.append("판매중")
        else:
            s -= w_stock * 0.7
            reasons.append("품절/불가")
        # 가격 경쟁력(중간값 이하일수록) — 상품마다 다름
        if price and med:
            ratio = max(0.0, min(1.0, (med * 1.5 - price) / (med * 1.5))) if med else 0
            s += w_price * ratio
            if price <= med:
                reasons.append("가격 경쟁력")
        # 마진 여력(권장소비자가 대비 공급가가 낮을수록 큼) — 상품마다 다름
        if fixed and price and fixed > price:
            room = (fixed - price) / fixed          # 0~1
            s += w_margin * room
            if room >= 0.3:
                reasons.append(f"마진 여력 큼(권장가 대비 -{round(room*100)}%)")
        s += demand_bonus
        if demand and demand.get("rise", 0) >= 10:
            reasons.append(f"네이버 수요 상승({round(demand['rise'])}%)")
        rel = it.get("_rel", 0)
        s += rel * 8                                  # 검색어 관련도 가점(정밀 매칭)
        if rel >= 2:
            reasons.append("검색어 정확 매칭")
        elif rel == 0:
            reasons.append("검색어와 관련 약함")
        it2 = dict(it)
        it2["url"] = _ownerclan_item_url(it.get("key"))
        it2["margin_room"] = round((fixed - price) / fixed * 100) if (fixed and price and fixed > price) else None
        it2["score"] = round(max(0.0, min(100.0, s + 15.0)))
        it2["why"] = reasons
        scored.append(it2)
    # 관련도 1순위, 그다음 점수 — 엉뚱한 상품이 위로 오지 않게
    scored.sort(key=lambda x: (-(x.get("_rel", 0)), -x["score"]))
    return {"ok": True, "items": scored[:40], "keyword": kw, "demand": demand,
            "sandbox": req.sandbox, "scanned": len(items), "match_note": match_note}


class RankBattleReq(BaseModel):
    client_id: str
    client_secret: str
    keywords: list = []         # 2~5개 후보


@app.post("/api/rankbattle")
async def rank_battle_api(req: RankBattleReq):
    """🥊 키워드 순위 대결 — 후보 여러 개를 검색어 트렌드 상대지수로 순위 매김.
    한 응답에 함께 정규화돼 서로 비교 가능(공식 API)."""
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "허브 열쇠(Client ID/Secret)를 먼저 넣어주세요."}
    kws = [str(k).strip() for k in (req.keywords or []) if str(k).strip()]
    kws = list(dict.fromkeys(kws))[:5]              # 중복 제거·최대 5개
    if len(kws) < 2:
        return {"ok": False, "error": "비교할 키워드를 2개 이상 넣어주세요."}
    start, end = _date_range(12)
    body = {"startDate": start, "endDate": end, "timeUnit": "month",
            "keywordGroups": [{"groupName": k, "keywords": [k]} for k in kws]}
    try:
        data, _ = await _hub_datalab_probe(cid, csec, _TREND_PATHS, body)
    except NaverHubAuth as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"조회 오류: {type(exc).__name__}"}
    results = data.get("results") or []
    rows = []
    for r in results:
        pts = r.get("data") or []
        t = _trend_of(pts)
        # 최근값(상대지수)·상승률로 순위
        rows.append({"keyword": r.get("title") or r.get("groupName"),
                     "latest": round(t.get("latest", 0.0), 1),
                     "rise": t.get("rise", 0.0),
                     "stage": t.get("stage"), "series": t.get("series", [])})
    if not rows:
        return {"ok": False, "error": "결과가 없어요(키워드를 바꿔보세요)."}
    # 최근 상대지수 기준 순위(동률이면 상승률)
    rows.sort(key=lambda x: (-x["latest"], -x["rise"]))
    top = rows[0]["latest"] or 1
    for i, rw in enumerate(rows):
        rw["rank"] = i + 1
        rw["pct_of_top"] = round(rw["latest"] / top * 100) if top else 0
    return {"ok": True, "items": rows, "range": f"{start} ~ {end}"}


class TitleOptReq(BaseModel):
    client_id: str
    client_secret: str
    title: str


@app.post("/api/title/optimize")
async def title_optimize_api(req: TitleOptReq):
    """제목 최적화 — 공식 API HUB(검색어 트렌드 + 연관어)만으로 제목을 진단하고
    검색량 많은 키워드로 재구성 제안. 상위노출 보장이 아니라 '검색 잘 걸리는' 재구성."""
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    title = (req.title or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "네이버 허브 열쇠를 먼저 넣어주세요."}
    if not title:
        return {"ok": False, "error": "제목을 넣어주세요."}

    # 1) 제목에서 한글/영숫자 토큰 추출(2~) → 불용어 제거
    raw_tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", title)
    tokens, seen = [], set()
    for t in raw_tokens:
        if t in _STOP_WORDS:
            continue
        if t not in seen:
            seen.add(t)
            tokens.append(t)
    if not tokens:
        return {"ok": False, "error": "제목에서 분석할 단어를 찾지 못했어요."}

    # 2) 핵심어 기준 연관어 채굴 — 대표어 + 두 번째로 긴 단어까지 합쳐 재료를 넉넉히
    core = max(tokens, key=len)
    related = []
    try:
        related = await _related_terms(cid, csec, core, limit=16)
    except NaverHubAuth as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:  # noqa: BLE001
        related = []
    # 두 번째 핵심어로도 연관어를 더 모아 결합(제목을 더 풍성하게)
    others = sorted([t for t in tokens if t != core], key=len, reverse=True)
    if others:
        try:
            more = await _related_terms(cid, csec, others[0], limit=10)
            for w in more:
                if w not in related:
                    related.append(w)
        except Exception:  # noqa: BLE001
            pass

    # 3) 제목 단어 + 연관어의 검색량(상대지수)을 한 번에 측정(트렌드, 최대 5개 그룹)
    cand = tokens[:5]
    demand = {}
    try:
        start, end = _date_range(12)
        groups = [{"groupName": w, "keywords": [w]} for w in cand]
        body = {"startDate": start, "endDate": end, "timeUnit": "month",
                "keywordGroups": groups}
        data, _ = await _hub_datalab_probe(cid, csec, _TREND_PATHS, body)
        for r in (data.get("results") or []):
            nm = r.get("title") or r.get("groupName")
            t = _trend_of(r.get("data") or [])
            demand[nm] = {"latest": round(t.get("latest", 0.0), 1),
                          "rise": t.get("rise", 0.0)}
    except Exception:  # noqa: BLE001
        demand = {}

    # 연관어도 검색량 측정(상위 10개, 5개씩 2묶음 — 재료 확보)
    rel_demand = {}
    if related:
        try:
            for grp_start in (0, 5):
                chunk = related[grp_start:grp_start + 5]
                if not chunk:
                    break
                rg = [{"groupName": w, "keywords": [w]} for w in chunk]
                body2 = {"startDate": start, "endDate": end, "timeUnit": "month",
                         "keywordGroups": rg}
                data2, _ = await _hub_datalab_probe(cid, csec, _TREND_PATHS, body2)
                for r in (data2.get("results") or []):
                    nm = r.get("title") or r.get("groupName")
                    t = _trend_of(r.get("data") or [])
                    rel_demand[nm] = round(t.get("latest", 0.0), 1)
        except Exception:  # noqa: BLE001
            pass

    # 4) 진단
    diag = []
    n_len = len(title)
    if n_len > 50:
        diag.append({"type": "warn", "msg": f"제목이 {n_len}자로 길어요. 네이버쇼핑은 "
                     "핵심 키워드 위주 40~50자가 무난해요."})
    elif n_len < 15:
        diag.append({"type": "warn", "msg": f"제목이 {n_len}자로 짧아요. 검색 걸릴 키워드를 "
                     "더 넣을 여지가 있어요."})
    # 중복 단어
    dup = [w for w in set(raw_tokens) if raw_tokens.count(w) > 1]
    if dup:
        diag.append({"type": "warn", "msg": f"반복된 단어: {', '.join(dup[:5])} — "
                     "네이버는 중복을 가중해주지 않아요. 빼고 다른 키워드를 넣으세요."})
    # 특수문자/브랜드성 과다
    if re.search(r"[!~★☆♥▶◀#\[\]{}]", title):
        diag.append({"type": "warn", "msg": "특수문자(★, !, [] 등)는 검색에 도움 안 되고 "
                     "일부는 어뷰징으로 볼 수 있어요. 빼는 걸 권해요."})
    # 검색량 낮은 단어 지적
    weak = [w for w in cand if demand.get(w, {}).get("latest", 0) < 5]
    if weak:
        diag.append({"type": "info", "msg": f"검색량이 약한 단어: {', '.join(weak[:5])} — "
                     "꼭 필요한 게 아니면 검색량 높은 단어로 바꿔보세요."})

    # 4-2) 스마트스토어 상품명 규칙 체커 (공식 가이드 기준)
    ss = []
    # 글자 수: 50자 내외 권장, 100자 초과 불가
    if n_len > 100:
        ss.append({"ok": False, "label": "글자 수",
                   "msg": f"{n_len}자 — 100자 초과는 등록 불가예요. 줄이세요."})
    elif n_len > 50:
        ss.append({"ok": False, "label": "글자 수",
                   "msg": f"{n_len}자 — 50자 내외 권장(최대 100자). 핵심만 남기세요."})
    else:
        ss.append({"ok": True, "label": "글자 수", "msg": f"{n_len}자 — 권장(50자 내외) 안이에요."})
    # 중복 단어 금지
    if dup:
        ss.append({"ok": False, "label": "단어 중복",
                   "msg": f"{', '.join(dup[:5])} 반복 — 중복은 가중 안 돼요. 한 번만."})
    else:
        ss.append({"ok": True, "label": "단어 중복", "msg": "중복 단어 없어요."})
    # 특수문자 금지
    sc = re.findall(r"[!~★☆♥▶◀◆■●#\[\]{}@%&*]", title)
    if sc:
        ss.append({"ok": False, "label": "특수문자",
                   "msg": f"{' '.join(sorted(set(sc)))} 사용 — 특수문자·기호는 빼세요."})
    else:
        ss.append({"ok": True, "label": "특수문자", "msg": "특수문자 없어요."})
    # 판매처/쇼핑몰명 금지(대표적 표현 감지)
    shopwords = re.findall(r"(공식|본사|정품샵|스토어|셀러|직영|무료배송|당일발송|최저가|이벤트|할인|사은품|1\+1)", title)
    if shopwords:
        ss.append({"ok": False, "label": "홍보문구·판매처",
                   "msg": f"{', '.join(sorted(set(shopwords)))} — 홍보문구·판매처명은 "
                          "상품명에 넣지 않아요(별도 노출)."})
    else:
        ss.append({"ok": True, "label": "홍보문구·판매처", "msg": "홍보문구 없어요."})
    # 조사·수식어 최소화(간단 감지)
    josa = re.findall(r"(은|는|이|가|을|를|의|와|과|으로|하는|한|해서|용의)\b", title)
    if len(josa) >= 2:
        ss.append({"ok": False, "label": "조사·수식어",
                   "msg": "조사·수식어가 많아요 — 명사 위주로 간결하게."})
    else:
        ss.append({"ok": True, "label": "조사·수식어", "msg": "명사 위주로 간결해요."})
    ss_pass = sum(1 for x in ss if x["ok"])

    # 5) 추천 키워드 — A) 엉뚱한 단어(브랜드·지역·불용어·검색량0) 강하게 걸러냄
    _BRAND_RE = re.compile(r"(나이키|아디다스|애플|아이폰|갤럭시|삼성|엘지|lg|샤넬|구찌|"
                           r"디올|스타벅스|다이슨|무신사|쿠팡|네이버|지그재그|에이블리)", re.I)
    _REGION_RE = re.compile(r"(서울|부산|대구|인천|광주|대전|울산|경기|강남|명동|홍대|"
                            r"동대문|남대문|중국|일본|미국|해외|국산|수입)")
    core_ch = set(core)
    add_kw = []
    for w in related:
        if w in title or w in _STOP_WORDS:
            continue
        if len(w) < 2 or len(w) > 10:
            continue
        if _BRAND_RE.search(w) or _REGION_RE.search(w):   # 브랜드·지역 제외
            continue
        if re.search(r"\d{2,}", w):                        # 숫자 범벅 제외
            continue
        lv = rel_demand.get(w)
        if lv is not None and lv < 1:                      # 검색량 0인 건 제외
            continue
        # 핵심어와 글자가 하나도 안 겹치고 너무 동떨어진 건 후순위(약한 관련성)
        add_kw.append({"word": w, "latest": lv,
                       "rel": 1 if (set(w) & core_ch) else 0})
    # 검색량 우선, 관련성 보조로 정렬
    add_kw.sort(key=lambda x: (x["latest"] is None, -(x["latest"] or 0), -x["rel"]))
    add_kw = [{"word": a["word"], "latest": a["latest"]} for a in add_kw[:10]]

    # 6) 새 제목 제안 — B) 스마트스토어 권장 순서로 자연스럽게 조합
    #    [속성/수식어] + [핵심 명사] + [용도/대상] 순, 중복·특수문자 없이, 25~40자 목표
    _ATTR = ("무선", "미니", "대용량", "소형", "대형", "휴대용", "접이식", "저소음",
             "고속", "다용도", "프리미엄", "슬림", "초경량", "방수", "usb", "충전식")
    _USE = ("사무실", "차량용", "가정용", "업소용", "캠핑", "주방", "욕실", "책상",
            "탁상용", "선물", "여행", "실내", "야외", "휴대")
    # 검색량순 핵심어 — 측정된 5개는 검색량순, 나머지 사용자 단어도 뒤에 다 포함
    ranked = sorted(cand, key=lambda w: -(demand.get(w, {}).get("latest", 0)))
    ranked = ranked + [t for t in tokens if t not in ranked]   # 사용자 단어 전부 살림
    add_words = [a["word"] for a in add_kw]
    pool = list(dict.fromkeys(ranked + add_words))         # 제목단어(전부) + 추천어
    attrs = [w for w in pool if w in _ATTR]
    uses = [w for w in pool if w in _USE]
    nouns = [w for w in pool if w not in _ATTR and w not in _USE]

    def _compose(words, cap=50):
        seen2, out = set(), []
        total = 0
        for w in words:
            if not w or w in seen2:
                continue
            add_len = len(w) + (1 if out else 0)
            if total + add_len > cap:
                break
            out.append(w)
            seen2.add(w)
            total += add_len
        return " ".join(out)

    # 스마트스토어 정석 구조: [속성] + 대표명사 + [세부속성] + [용도/대상]
    #  (예: '여름용 냉감 이불 쿨링소재 싱글' / '무선 로봇청소기 강력흡입 가정용')
    all_words = list(dict.fromkeys(nouns + attrs + uses + add_words))
    main = nouns[0] if nouns else (ranked[0] if ranked else pool[0])
    other_nouns = [w for w in nouns if w != main]
    add_nouns = [w for w in add_words if w not in _ATTR and w not in _USE and w != main]
    # 안1(정석): 대표속성1 + 대표명사 + 속성 + 용도 — 가장 자연스러운 기본형
    order1 = attrs[:1] + [main] + attrs[1:3] + other_nouns[:1] + uses[:2]
    # 안2(검색 강조): 대표명사 + 추천명사 + 속성 + 용도
    order2 = [main] + add_nouns[:2] + attrs[:2] + uses[:1]
    # 안3(풍성): 대표속성 + 대표명사 + 세부속성/추천 + 용도, 최대 채움
    order3 = attrs[:1] + [main] + add_nouns[:1] + attrs[1:2] + other_nouns[:1] + uses[:1] + add_nouns[1:2]
    s1 = _compose(list(dict.fromkeys(order1 + all_words)))
    s2 = _compose(list(dict.fromkeys(order2 + all_words)))
    s3 = _compose(list(dict.fromkeys(order3 + all_words)))
    suggestions = []
    for s in (s1, s2, s3):
        s = s.strip()
        # 최소 3단어 이상 + 원본과 다를 때만 채택
        if s and len(s.split()) >= 3 and s not in suggestions and s != title:
            suggestions.append(s)
    # 그래도 비면(재료 부족) 최소한 재배열이라도 제공
    if not suggestions and pool:
        fallback = _compose(list(dict.fromkeys([main] + ranked + add_words + pool)))
        if fallback and fallback != title:
            suggestions.append(fallback)

    return {"ok": True, "title": title, "core": core,
            "tokens": [{"word": w, **demand.get(w, {})} for w in cand],
            "add_keywords": add_kw, "diagnostics": diag,
            "smartstore": ss, "smartstore_pass": ss_pass, "smartstore_total": len(ss),
            "suggestions": suggestions,
            "note": "네이버 검색어 트렌드·연관어(공식 API) 기준이에요. "
                    "상위노출을 보장하진 않아요."}


class FeedbackReq(BaseModel):
    keyword: str
    vote: int = 0            # +1 좋음 / -1 별로 / 0 취소
    category: str = ""
    owner: str = "local"


@app.post("/api/feedback")
async def feedback_api(req: FeedbackReq):
    """발굴 결과에 대한 👍/👎 — 다음 발굴부터 순위에 반영(쓸수록 정확)."""
    ok = feedback_set(req.keyword, req.vote, req.category, req.owner or "local")
    return {"ok": ok}


class TitleAIReq(BaseModel):
    gemini_key: str
    title: str
    keywords: list = []       # 검색량 높은 키워드(선택) — 근거로 제공
    category: str = ""


@app.post("/api/title/ai")
async def title_ai_api(req: TitleAIReq):
    """제목 최적화(AI) — 사용자가 넣은 Gemini API 키로 자연스러운 새 제목 생성.
    키는 이 요청에만 쓰고 서버에 저장하지 않는다(무료 티어 gemini-2.5-flash)."""
    import httpx
    key = (req.gemini_key or "").strip()
    title = (req.title or "").strip()
    if not key:
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…로 시작)."}
    if not key.startswith("AIza"):
        return {"ok": False, "error": "Gemini 키 형식이 아니에요(보통 AIza로 시작해요)."}
    if not title:
        return {"ok": False, "error": "제목/키워드를 넣어주세요."}
    kw_line = ", ".join([str(k) for k in (req.keywords or []) if str(k)])[:300]
    prompt = (
        "너는 한국 이커머스(네이버 스마트스토어·쿠팡) 상품명 SEO 전문가야.\n"
        "아래 입력을 '참고'만 하고, 이 상품에 딱 맞는 '완전히 새로운' 상품명 3개를 지어줘.\n"
        "입력 단어를 그대로 재배열하지 말고, 이 상품이 무엇인지 파악해서 "
        "실제 구매자가 검색할 법한 키워드로 새로 구성해.\n\n"
        f"[상품 정보/현재 제목]\n{title}\n\n"
        + (f"[검색량 높은 참고 키워드]\n{kw_line}\n\n" if kw_line else "")
        + (f"[카테고리]\n{req.category}\n\n" if req.category else "")
        + "[네이버·쿠팡 상품명 정책 — 꼭 지켜라]\n"
        "- 길이 25~45자, 띄어쓰기로 키워드 구분\n"
        "- 구조: (대표 핵심키워드) + (세부속성: 크기·색상·소재·수량) + (용도·대상)\n"
        "- 대표 핵심키워드(무슨 상품인지)를 맨 앞에\n"
        "- 구체적 세부키워드(롱테일) 포함: 막연한 '청소기'보다 '차량용 무선 청소기'\n"
        "- 금지: 브랜드·타사명, 특수문자/이모지, 홍보문구(최저가·무료배송·정품·1+1·이벤트), "
        "같은 단어 반복, 수량단위 남발\n"
        "- 과장·허위(최고/1위/의학효과) 금지\n"
        "- 사람이 읽어도 자연스럽게(단어 나열 티 안 나게)\n\n"
        "[출력] 서로 강조점이 다른 3개:\n"
        "1번: 대표키워드+핵심속성 중심\n"
        "2번: 용도·사용상황·대상 중심\n"
        "3번: 경쟁 덜한 세부(롱테일) 키워드 중심\n\n"
        "[중요] 입력을 그대로 재배열하지 마라. 예시:\n"
        "입력: '미니 휴지통 탁상용 화장대 쓰레기통'\n"
        "나쁜 예(재배열): '미니 화장대 쓰레기통 탁상용 휴지통'\n"
        "좋은 예(새 구성): '탁상용 미니 휴지통 원룸 사무실 책상 소형 쓰레기통', "
        "'침실 화장대 휴지통 좁은공간 인테리어 미니 휴지통', "
        "'차량용 다용도 미니 휴지통 뚜껑형 탁상 정리함'\n"
        "→ 이렇게 새 세부키워드(원룸·책상·뚜껑형·인테리어 등)를 더해 다르게 만들어라.\n\n"
        "설명·번호·따옴표·기호 없이 제목 3개만 줄바꿈으로 출력."
    )
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash:generateContent")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 1.0, "maxOutputTokens": 8192,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=40, write=10, pool=10)) as c:
            r = await c.post(url, headers={"x-goog-api-key": key,
                                           "Content-Type": "application/json"},
                             json=body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Gemini 연결 오류: {type(exc).__name__}"}
    if r.status_code == 400:
        return {"ok": False, "error": "요청 오류(키가 틀렸거나 만료). 키를 확인해주세요."}
    if r.status_code == 403:
        return {"ok": False, "error": "권한 오류(403) — 키 제한 설정이나 서버 IP 문제일 수 있어요. "
                                      "로컬(내 PC)에서 실행 중이면 대부분 잘 돼요."}
    if r.status_code == 429:
        return {"ok": False, "error": "무료 한도 초과(429) — 잠시 후 다시(분당 요청 제한)."}
    if r.status_code >= 400:
        return {"ok": False, "error": f"Gemini 오류({r.status_code})."}
    try:
        data = r.json()
        parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        text = "\n".join(p.get("text", "") for p in parts).strip()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "Gemini 응답을 해석하지 못했어요."}
    # 3줄 파싱
    lines = []
    for ln in text.splitlines():
        s = ln.strip().lstrip("0123456789.-)· \t").strip().strip('"').strip("'")
        if s and len(s) >= 4:
            lines.append(s)
    lines = lines[:3]
    if not lines:
        return {"ok": False, "error": "AI가 제목을 만들지 못했어요. 다시 시도해주세요."}
    return {"ok": True, "titles": lines}


class OwnerClanRefreshReq(BaseModel):
    username: str
    password: str
    keys: list = []           # 관심목록에 담긴 오너클랜 상품코드들
    sandbox: bool = False


@app.post("/api/ownerclan/refresh")
async def ownerclan_refresh_api(req: OwnerClanRefreshReq):
    """📌 관심목록 오너클랜 상품 일괄 갱신 — itemsByKeys 로 여러 상품의
    현재 가격·재고·상태를 한 번에 조회(담을 때와 비교용)."""
    import httpx
    from urllib.parse import quote
    u = (req.username or "").strip()
    p = req.password or ""
    keys = [str(k).strip() for k in (req.keys or []) if str(k).strip()][:40]
    if not u or not p:
        return {"ok": False, "error": "오너클랜 ID/비밀번호가 필요해요."}
    if not keys:
        return {"ok": False, "error": "갱신할 상품이 없어요."}
    try:
        token = await _ownerclan_token(u, p, req.sandbox)
    except NaverHubAuth as exc:
        return {"ok": False, "error": str(exc)}
    keys_str = ", ".join('"' + k.replace('"', '') + '"' for k in keys)
    query = ("query { itemsByKeys(keys: [" + keys_str + "]) { "
             "key name price fixedPrice status } }")
    base = ("https://api-sandbox.ownerclan.com/v1/graphql" if req.sandbox
            else "https://api.ownerclan.com/v1/graphql")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=40, write=10, pool=10)) as c:
            r = await c.get(base + "?query=" + quote(query),
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 429:
            return {"ok": False, "error": "오너클랜 요청이 몰렸어요 — 20~30초 후 다시."}
        if r.status_code >= 400:
            return {"ok": False, "error": f"갱신 실패({r.status_code})."}
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"연결 오류: {type(exc).__name__}"}
    if data.get("errors"):
        return {"ok": False, "error": f"API 오류: {data['errors'][0].get('message','')}"}
    rows = (data.get("data") or {}).get("itemsByKeys") or []
    out = {}
    for n in rows:
        if not n:
            continue
        out[n.get("key")] = {"price": n.get("price"), "fixedPrice": n.get("fixedPrice"),
                             "status": n.get("status"), "name": n.get("name")}
    return {"ok": True, "items": out}


class DetailAIReq(BaseModel):
    counts: dict = {}          # v193 풍부 개수 조절
    gemini_key: str
    product: str              # 상품명/핵심 정보
    features: str = ""        # 특징·장점(선택)
    target: str = ""          # 타깃 고객(선택)
    images: list = []         # 내 상품 이미지 URL들(선택)
    tone: str = "trust"
    notice: list = []
    notice_cat: str = ""
    guide: str = ""
    length: str = "rich"
    toc: bool = False
    soldout: str = "show"
    reviews: str = ""
    sectheme: bool = False
    banner: bool = False
    design: str = "editorial"
    hide: str = ""
    spacing: str = "normal"
    align: str = "center"
    luxe: str = "gold"
    counts: dict = {}
    layout: str = "auto"      # 상세페이지 유형: auto(추천)/basic/problem/spec/story/check


@app.post("/api/detail/ai")
async def detail_ai_api(req: DetailAIReq):
    """상세페이지 생성 — 내 상품 정보로 HTML 상세페이지를 만든다(남의 것 스크래핑 아님).
    문구는 Gemini로, 이미지는 사용자가 준 URL을 배치. 결과는 붙여넣기 가능한 HTML."""
    key = (req.gemini_key or "").strip()
    product = (req.product or "").strip()
    if not key or not key.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…로 시작)."}
    if not product:
        return {"ok": False, "error": "상품명·핵심 정보를 넣어주세요."}
    imgs = _clean_imgs(req.images or [], cap=20)
    ctx = (f"[상품]\n{product}\n"
           + (f"[특징·장점]\n{req.features}\n" if req.features.strip() else ""))
    prompt = _detail_prompt(ctx, req.target, req.tone, length=req.length, counts=getattr(req, "counts", None))
    text, err = await _detail_gemini(key, prompt)
    if err:
        return err
    sections = _facts_verify(_parse_detail(text), ctx)   # 숫자 강조띠: 근거 없는 숫자 제거
    if not sections.get("head"):
        sections["head"] = product
    rec = _recommend_layout(product, "", req.features or "")
    lay = _pick_layout(getattr(req, "layout", "auto"), rec)
    detail_html = _build_detail_html(sections, imgs, product, req.tone,
                                     req.notice, req.notice_cat, guide=req.guide, toc=req.toc, soldout=getattr(req,"soldout","show"), reviews=getattr(req,"reviews",""), sectheme=getattr(req,"sectheme",False), banner=getattr(req,"banner",False), layout=lay, design=getattr(req,"design","editorial"), hide=getattr(req,"hide",""), spacing=getattr(req,"spacing","normal"), eyebrow=(rec.get("label", "") if rec.get("bucket") != "etc" else ""), align=getattr(req,"align","center"), luxe=getattr(req,"luxe","gold"))
    return {"ok": True, "html": detail_html, "sections": sections,
            "imgs": imgs, "ctx": ctx, "tone": req.tone, "img_count": len(imgs),
            "layout": lay, "layout_rec": rec}


class DetailFromOcReq(BaseModel):
    gemini_key: str
    username: str
    password: str
    key: str                 # 오너클랜 상품코드
    sandbox: bool = False
    target: str = ""         # 타깃(선택)
    tone: str = "trust"
    notice: list = []
    notice_cat: str = ""
    guide: str = ""
    length: str = "rich"
    toc: bool = False
    soldout: str = "show"
    reviews: str = ""
    sectheme: bool = False
    banner: bool = False
    design: str = "editorial"
    hide: str = ""
    spacing: str = "normal"
    align: str = "center"
    luxe: str = "gold"
    counts: dict = {}
    layout: str = "auto"      # 상세페이지 유형: auto(추천)/basic/problem/spec/story/check


@app.post("/api/detail/from_ownerclan")
async def detail_from_ownerclan_api(req: DetailFromOcReq):
    """🪄 오너클랜 상품 → 상세페이지 한 번에.
    오너클랜에서 상품 정보·이미지를 가져와서(공식 API), 문구는 Gemini로 만들고,
    이미지까지 배치한 HTML 상세페이지를 반환한다."""
    import html as _html
    import httpx
    from urllib.parse import quote
    gkey = (req.gemini_key or "").strip()
    if not gkey or not gkey.startswith("AIza"):
        return {"ok": False, "error": "Gemini API 키를 넣어주세요(AIza…)."}
    u = (req.username or "").strip()
    p = req.password or ""
    k = (req.key or "").strip()
    if not u or not p or not k:
        return {"ok": False, "error": "오너클랜 로그인/상품코드가 필요해요."}
    # 1) 오너클랜에서 상품 정보 + 이미지 가져오기(공식 API)
    try:
        token = await _ownerclan_token(u, p, req.sandbox)
    except NaverHubAuth as exc:
        return {"ok": False, "error": str(exc)}
    kk = k.replace('"', '')
    q = ('query { item(key: "' + kk + '") { key name model origin price fixedPrice '
         'shippingFee content category { fullName } images(size: large) '
         'options { price quantity optionAttributes { name value } } } }')
    base = ("https://api-sandbox.ownerclan.com/v1/graphql" if req.sandbox
            else "https://api.ownerclan.com/v1/graphql")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=40, write=10, pool=10)) as c:
            r = await c.get(base + "?query=" + quote(q),
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            return {"ok": False, "error": f"오너클랜 상품 조회 실패({r.status_code})."}
        od = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"오너클랜 연결 오류: {type(exc).__name__}"}
    n = (od.get("data") or {}).get("item")
    if not n:
        return {"ok": False, "error": "오너클랜에서 상품을 찾지 못했어요(코드 확인)."}
    name = n.get("name") or ""
    imgs = _clean_imgs(n.get("images") or [], cap=10)
    cat = (n.get("category") or {}).get("fullName") or ""
    content_txt = _strip_tags(n.get("content") or "")[:600]
    opt_raw = []
    axis_names = []   # 옵션 축 이름(예: 색상, 사이즈) — 순서 유지, 중복 제거
    for o in (n.get("options") or [])[:24]:
        attrs = [a for a in (o.get("optionAttributes") or []) if a.get("value")]
        v = " / ".join(str(a.get("value", "")).strip() for a in attrs)
        if v:
            for a in attrs:
                nm2 = str(a.get("name", "")).strip()
                if nm2 and nm2 not in axis_names:
                    axis_names.append(nm2)
            try:
                op = int(o.get("price") or 0)
            except Exception:  # noqa: BLE001
                op = 0
            qv = o.get("quantity")
            try:
                qv = int(qv) if qv is not None else None
            except Exception:  # noqa: BLE001
                qv = None
            opt_raw.append((v, op, qv))
    # 옵션별 추가금액 = 각 옵션가 - 최저 옵션가(상대 upcharge). 가격 정보 없으면 0
    opt_list = []
    opt_stock = []
    valid_prices = [p for _v, p, _q in opt_raw if p > 0]
    base_op = min(valid_prices) if valid_prices else 0
    for v, op, qv in opt_raw:
        add = (op - base_op) if (op > 0 and base_op > 0) else 0
        opt_list.append(f"{v}|{add}" if add > 0 else v)
        opt_stock.append(qv)
    opt_txt = ", ".join(v for v, _p, _q in opt_raw)
    # 2) Gemini 로 문구 생성 (공통 헬퍼)
    origin = n.get("origin") or ""
    ctx = (f"[상품명]\n{name}\n"
           + (f"[카테고리]\n{cat}\n" if cat else "")
           + (f"[원산지]\n{origin}\n" if origin else "")
           + (f"[옵션]\n{opt_txt}\n" if opt_txt else "")
           + (f"[상품설명 참고]\n{content_txt}\n" if content_txt else ""))
    prompt = _detail_prompt(ctx, req.target, req.tone, length=req.length, counts=getattr(req, "counts", None))
    text, err = await _detail_gemini(gkey, prompt)
    if err:
        return err
    sections = _facts_verify(_parse_detail(text), ctx)   # 숫자 강조띠: 근거 없는 숫자 제거
    if not sections.get("head"):
        sections["head"] = name
    if opt_list:
        sections["options"] = opt_list
        # 2축 이상(색상×사이즈 등)이면 조합표 헤더로 사용
        if len(axis_names) >= 2:
            sections["option_axes"] = axis_names[:3]
        # 재고 정보가 하나라도 있으면 재고 열용으로 저장
        if any(q is not None for q in opt_stock):
            sections["option_stock"] = opt_stock
    rec = _recommend_layout(name, cat, content_txt)
    lay = _pick_layout(getattr(req, "layout", "auto"), rec)
    detail_html = _build_detail_html(sections, imgs, name, req.tone,
                                     req.notice, req.notice_cat, origin=origin, guide=req.guide, toc=req.toc, soldout=getattr(req,"soldout","show"), reviews=getattr(req,"reviews",""), sectheme=getattr(req,"sectheme",False), banner=getattr(req,"banner",False), layout=lay, design=getattr(req,"design","editorial"), hide=getattr(req,"hide",""), spacing=getattr(req,"spacing","normal"), eyebrow=(rec.get("label", "") if rec.get("bucket") != "etc" else ""), align=getattr(req,"align","center"), luxe=getattr(req,"luxe","gold"))
    return {"ok": True, "html": detail_html, "name": name, "img_count": len(imgs),
            "sections": sections, "imgs": imgs, "ctx": ctx, "tone": req.tone,
            "origin": origin, "layout": lay, "layout_rec": rec}


@app.post("/api/keytest")
async def keytest(req: KeyTestReq):
    """열쇠 하나만 딱 점검 — 씨앗·한도와 무관하게 '이 키가 유효한지 +
    쇼핑 검색 API 가 살아있는지' 를 정확한 상태코드로 알려준다."""
    from discovery.providers.naver_client import (
        NaverClient, NaverAuthError, NaverRateLimited)
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "kind": "empty",
                "msg": "Client ID / Secret 을 둘 다 넣어주세요."}
    try:
        async with NaverClient(cid, csec) as c:
            data = await c.search_shop("테스트", display=1)
        total = data.get("total") if isinstance(data, dict) else None
        return {"ok": True, "kind": "ok",
                "msg": f"정상! 쇼핑 검색 API 가 살아있고 열쇠도 유효해요. (표본 total={total})"}
    except NaverAuthError as e:
        m = str(e)
        if "404" in m:
            return {"ok": False, "kind": "shutdown",
                    "msg": "쇼핑 검색 API 가 404 — 네이버가 이 API 를 종료한 상태예요. "
                           "열쇠가 맞아도 데이터를 받을 수 없어요."}
        if "403" in m:
            return {"ok": False, "kind": "noperm",
                    "msg": "이 열쇠에 '검색' API 권한이 없어요 — 개발자센터 → API 설정에서 '검색' 추가."}
        return {"ok": False, "kind": "auth",
                "msg": "인증 실패 — Client ID/Secret 이 틀렸거나 서로 뒤바뀌었을 수 있어요. "
                       "개발자센터에서 값을 다시 복사해주세요. (원문: " + m[:120] + ")"}
    except NaverRateLimited:
        return {"ok": False, "kind": "rate",
                "msg": "지금은 네이버 한도에 걸려 있어요 — 1~2분 뒤 다시 점검해주세요."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": "error",
                "msg": f"{type(e).__name__}: {str(e)[:160]}"}


@app.get("/api/gate/status")
async def gate_status():
    """이용코드 게이트가 켜져 있는지 + 안내 문구(등급 이름).
    SOURCING_GATE_NOTE 환경변수로 '열심멤버 이상' 같은 등급 문구를 바꿀 수 있다."""
    note = os.environ.get("SOURCING_GATE_NOTE",
                          "열심멤버 이상 등급만 이용할 수 있어요.")
    return {"enabled": bool(_ACCESS_CODES), "note": note}


@app.get("/api/gate/check")
async def gate_check(code: str = ""):
    """입력한 코드가 유효한지 — 프런트에서 잠금 해제 전에 확인."""
    if not _ACCESS_CODES:
        return {"ok": True, "enabled": False, "valid": True}
    return {"ok": True, "enabled": True, "valid": (code or "").strip() in _ACCESS_CODES}


@app.get("/api/presence")
async def presence(id: str = ""):
    """실시간 접속자 하트비트.
    - online : 최근 75초 안에 신호를 보낸 고유 방문자 수 (=지금 접속 중)
    - total  : 오늘(UTC 날짜 기준) 다녀간 고유 방문자 수
    무료 플랜 재시작/슬립 시 두 값 모두 초기화됩니다.
    """
    now = _time.time()
    # 날짜가 바뀌면 누적 리셋
    today = _time.gmtime().tm_yday
    if today != _DAY_ANCHOR["day"]:
        _DAY_ANCHOR["day"] = today
        _SEEN_TODAY.clear()

    vid = (id or "").strip()[:64]
    if not vid:
        vid = "anon"

    if vid not in _SEEN_TODAY:
        _SEEN_TODAY[vid] = now
    _PRESENCE[vid] = now

    # 오래된 접속 정리
    dead = [k for k, ts in _PRESENCE.items() if now - ts > _PRESENCE_WINDOW]
    for k in dead:
        _PRESENCE.pop(k, None)

    # 상한 방어 (혹시 모를 폭주)
    if len(_SEEN_TODAY) > _PRESENCE_MAX:
        # 가장 오래된 것부터 잘라냄
        for k in sorted(_SEEN_TODAY, key=_SEEN_TODAY.get)[:len(_SEEN_TODAY) - _PRESENCE_MAX]:
            _SEEN_TODAY.pop(k, None)

    return {"online": max(1, len(_PRESENCE)), "total": len(_SEEN_TODAY)}


@app.get("/api/categories")
async def get_categories():
    from discovery.auto_scan import categories
    return {"categories": categories()}


@app.post("/api/auto")
async def auto(req: AutoReq, request: Request):
    """자동 발굴 — 아무것도 입력하지 않아도 팔 만한 것을 찾아 대령.

    [Failed to fetch 방지] 예산 × 호출 간격에 429 재시도가 겹치면 1분을 넘겨
    브라우저가 끊는다 → 시간 상한을 두고 '그때까지 찾은 것' 이라도 돌려준다.

    [여러 사용자 방어] 같은 (분야·모드) 첫 스캔은 결과를 짧게 캐시해,
    여러 명이 같은 분야를 눌러도 네이버를 한 번만 친다 (IP 폭주 방지).
    이용코드 게이트 + IP별 횟수 제한으로 남용도 막는다."""
    # ① 이용코드 게이트 (카페 등급 회원 전용) — 켜져 있을 때만
    if _ACCESS_CODES and (req.access_code or "").strip() not in _ACCESS_CODES:
        return {"ok": False, "gated": True,
                "error": "열심멤버 이상 등급 전용이에요 — 등급 게시판의 이용코드를 넣어주세요."}
    # ② IP별 횟수 제한 (한 명이 도배해서 IP 를 태우지 못하게)
    ip = _client_ip(request)
    if not _ip_ok(ip):
        return {"ok": False, "error": (
            "잠깐요 — 짧은 시간에 너무 많이 눌렀어요. 네이버 차단을 막기 위해 "
            "잠시 쉬어주세요 (몇 분 뒤 다시 가능).")}
    from discovery.auto_scan import auto_scan
    from discovery.providers.naver_client import NaverClient
    from discovery.providers.naver_demand import NaverDemandProvider
    from discovery.providers.naver_shop import NaverShopProvider

    import asyncio as _aio
    budget = max(20, min(600, req.budget))

    # ── 결과 캐시 (첫 스캔만; '더 찾기'는 exclude 가 있어 캐시 안 함) ──
    exclude = _as_list(req.exclude)
    cache_key = None
    if not exclude:
        cache_key = "|".join(str(x) for x in (
            req.mode, req.category, budget, req.max_total, req.open_total,
            req.min_price, req.fee_pct, req.target_margin))
        hit = _AUTO_CACHE.get(cache_key)
        if hit and (_time.time() - hit[0]) < _AUTO_CACHE_TTL:
            cached = dict(hit[1])
            cached["cached"] = True
            return cached
    try:
        async with NaverClient(req.client_id, req.client_secret) as client:
            shop = NaverShopProvider(client)
            finds, rep = await _aio.wait_for(auto_scan(
                                         shop, category=req.category,
                                         budget=budget,
                                         exclude=_as_list(req.exclude),
                                         mode=(req.mode or "consign"),
                                         fee_pct=req.fee_pct / 100.0,
                                         target_margin=req.target_margin,
                                         max_total=req.max_total,
                                         open_total=req.open_total,
                                         min_price=req.min_price),
                                         timeout=_SCAN_TIMEOUT)
            calls = client.call_count
    except _aio.TimeoutError:
        return {"ok": False, "error": (
            "네이버가 오늘 느려서 시간 안에 못 끝냈어요.\n"
            "잠시 뒤 다시 누르거나, 분야를 하나만 골라서 해보세요.")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(f"{type(exc).__name__}: {exc}")}

    # 스냅샷 기록 — 다음에 또 보면 '어디로 가는지' 알려주기 위해.
    # [중요] 수요를 0 으로 박으면 demand_change 가 늘 0 이 되어 골든타임이
    # 영원히 발동하지 않는다. 상위 후보만 데이터랩으로 실제 수요를 받아 남긴다.
    top_n = 5
    demands: dict = {}
    seasons: dict = {}    # 🏷️ 계절 연동: 키워드 → 시즌 프로파일
    try:
        from discovery.season import analyze_season
        # 데이터랩은 한도가 좁다. 재시도를 2회로 줄여 '몇 분 멈춤'을 막는다.
        # (trend_of 는 429 를 안에서 삼키고 found=False 로 준다 → 예외로는
        #  못 잡는다. found 가 False 면 한도로 보고 즉시 포기한다.)
        async with NaverClient(req.client_id, req.client_secret,
                               datalab_max_retries=2) as c2:
            d2 = NaverDemandProvider(c2)
            for f in finds[:top_n]:
                cat_id = _CATS.get(f.category, "")
                if not cat_id:
                    continue
                t = await d2.trend_of(f.keyword, cat_id)
                if t and t.found:
                    demands[f.keyword] = float(t.level or 0)
                    # 12개월 시계열로 성수기 선행 판정 (이미 받은 데이터 재활용)
                    try:
                        sp = analyze_season(t.points, periods=t.periods)
                        if sp.stage and sp.stage != "판단불가":
                            seasons[f.keyword] = {
                                "has_season": sp.has_season,
                                "peak_month": sp.peak_month,
                                "rise_month": sp.rise_month,
                                "peak_ratio": sp.peak_ratio,
                                "stage": sp.stage,
                                "note": sp.note,
                                "action": sp.action,
                            }
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    break   # 한도 소진 — 더 기다려도 어차피 실패
    except Exception:  # noqa: BLE001
        pass

    for f in finds[:60]:
        try:
            # 지난번 판정을 지금 상태로 채점 → 그다음 이번 판정을 기록
            grade_verdicts(f.keyword, f.total, f.price_min)
            record(f.keyword, f.total, f.price_min, demands.get(f.keyword, 0.0))
            if f.grade == "빈자리":
                say_verdict(f.keyword, f.grade, f.total, f.price_min)
        except Exception:  # noqa: BLE001
            pass

    # 이전 스냅샷과 비교해 '어디로 가는 중'인지 붙이기
    moves: dict = {}
    for f in finds[:60]:
        try:
            mv = movement_of(f.keyword)
            if mv.stage and mv.stage != "데이터부족":
                moves[f.keyword] = {"stage": mv.stage, "note": mv.note,
                                    "action": mv.action, "golden": mv.golden}
        except Exception:  # noqa: BLE001
            pass

    out = []
    for f in finds[:60]:
        d = f.as_dict()
        d["movement"] = moves.get(f.keyword)
        d["season"] = seasons.get(f.keyword)
        # 수요 검증(상위 후보만 데이터랩 확인) — 낮으면 '안 팔리는 빈자리' 경고.
        # '빈자리인데 수요 없음' 은 초보가 가장 크게 당하는 함정이라 명시한다.
        if f.keyword in demands:
            lvl = demands[f.keyword]
            d["demand_level"] = round(lvl, 1)
            if lvl < 15.0:
                d["demand_low"] = True
                doubts = list(d.get("doubts") or [])
                doubts.append("검색 수요가 낮아요 — '빈자리'가 아니라 "
                              "'안 팔려서 빈 자리'일 수 있어요 (수요 먼저 확인)")
                d["doubts"] = doubts
        out.append(d)
    try:
        hr = hit_rate()
        hits = {"pct": hr.pct, "graded": hr.graded, "hits": hr.hits,
                "pending": hr.pending}
    except Exception:  # noqa: BLE001
        hits = None
    try:
        pool = pool_stats(req.category)
    except Exception:  # noqa: BLE001
        pool = None
    resp = {"ok": True, "calls": calls, "scanned": budget,
            "finds": out, "report": rep.as_dict(), "note": rep.summary(),
            "hitrate": hits, "pool": pool}
    # 첫 스캔 결과를 캐시에 저장 (여러 명이 같은 분야를 눌러도 네이버 재호출 없음)
    if cache_key is not None and out:
        if len(_AUTO_CACHE) > _AUTO_CACHE_MAX:
            for k in sorted(_AUTO_CACHE, key=lambda k: _AUTO_CACHE[k][0])[:100]:
                _AUTO_CACHE.pop(k, None)
        _AUTO_CACHE[cache_key] = (_time.time(), resp)
    return resp


class DiagReq(BaseModel):
    client_id: str
    client_secret: str
    keyword: str


@app.post("/api/diagnose")
async def diagnose(req: DiagReq):
    """
    판정 근거 검증용 — 실제 네이버 응답을 그대로 보여준다.

    [왜 필요한가] competition.py 는 productType 1,3=카탈로그 / 2=독립 이라는
    문서 기준 매핑을 쓴다. 이게 실제와 맞는지는 사용자가 실물로 확인해야 한다
    (추측 위에 판정을 쌓으면 안 됨). 여기서 나온 상품을 직접 눌러보고
    '가격비교 페이지로 가는지' 확인하면 매핑이 맞는지 알 수 있다.
    """
    from discovery.providers.naver_client import NaverClient

    kw = re.sub(r"\s+", " ", req.keyword).strip()
    if not kw:
        return {"ok": False, "error": "확인할 물건 이름을 넣어주세요."}
    try:
        async with NaverClient(req.client_id, req.client_secret) as client:
            sim = await client.search_shop(kw, display=20, sort="sim")
            asc = await client.search_shop(kw, display=5, sort="asc")
            calls = client.call_count
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(f"{type(exc).__name__}: {exc}")}

    def _rows(data):
        out = []
        for it in (data.get("items") or []):
            out.append({
                "title": re.sub(r"<[^>]+>", "", it.get("title", "")),
                "lprice": it.get("lprice"), "mall": it.get("mallName"),
                "ptype": it.get("productType"), "link": it.get("link"),
                "brand": it.get("brand"), "maker": it.get("maker"),
            })
        return out

    sim_rows = _rows(sim)
    types = [r["ptype"] for r in sim_rows if r["ptype"] is not None]
    dist = {}
    for t in types:
        dist[str(t)] = dist.get(str(t), 0) + 1

    sim_low = min([int(r["lprice"]) for r in sim_rows
                   if r["lprice"] and str(r["lprice"]).isdigit()] or [0])
    asc_rows = _rows(asc)
    asc_low = min([int(r["lprice"]) for r in asc_rows
                   if r["lprice"] and str(r["lprice"]).isdigit()] or [0])

    return {
        "ok": True, "calls": calls, "keyword": kw,
        "total": sim.get("total"),
        "price_check": {
            "sim_top100_low": sim_low,   # 예전에 '최저가'라고 쓰던 값
            "true_low": asc_low,         # 진짜 최저가 (가격순 1등)
            "gap": sim_low - asc_low if (sim_low and asc_low) else 0,
        },
        "ptype_dist": dist,
        "samples": sim_rows[:10],
        "asc_samples": asc_rows[:5],
    }


class SegReq(BaseModel):
    client_id: str
    client_secret: str
    keyword: str
    category: str = ""


@app.post("/api/segments")
async def segments(req: SegReq):
    """① 누가 언제 찾나 — 성별·기기별 시즌 모양 (데이터랩 4회)."""
    from discovery.providers.naver_client import NaverClient
    from discovery.providers.naver_demand import NaverDemandProvider
    from discovery.providers.naver_shop import NaverShopProvider

    kw = re.sub(r"\s+", " ", req.keyword).strip()
    if not kw:
        return {"ok": False, "error": "물건 이름을 넣어주세요."}
    try:
        # 데이터랩은 한도가 좁다. 재시도를 2회로 줄여 '몇 분 멈춤'을 막는다.
        # (자동발굴 직후엔 한도가 거의 차 있어 오래 기다려도 어차피 실패)
        async with NaverClient(req.client_id, req.client_secret,
                               datalab_max_retries=2) as client:
            cat_id = _CATS.get(req.category, "")
            if not cat_id:
                shop = NaverShopProvider(client)
                m = await shop.market_of(kw, sample=10)
                if m.category_path:
                    cat_id = _CATS.get(m.category_path[0], "")
            if not cat_id:
                return {"ok": False,
                        "error": "이 물건의 분야를 찾지 못했어요. 분야를 골라주세요."}
            d = NaverDemandProvider(client)
            pairs = []
            for label, kwargs in (("여성", {"gender": "f"}),
                                  ("남성", {"gender": "m"}),
                                  ("모바일", {"device": "mo"}),
                                  ("PC", {"device": "pc"})):
                try:
                    t = await d.segment_trend(kw, cat_id, **kwargs)
                except Exception:  # noqa: BLE001
                    t = None
                # segment_trend 는 429 를 안에서 삼키고 found=False 로 준다.
                # 첫 조회부터 실패면 한도가 찬 것 — 나머지도 마찬가지라 즉시 중단.
                if not pairs and (t is None or not t.found):
                    return {"ok": False, "error": (
                        "데이터랩 한도가 찼어요.\n"
                        "자동발굴을 막 돌린 뒤엔 잘 안 됩니다 — "
                        "5~10분 뒤에 다시 눌러주세요.")}
                pairs.append((label, t))
            calls = client.call_count
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(f"{type(exc).__name__}: {exc}")}

    rep = build_report(pairs)
    return {"ok": True, "calls": calls, "keyword": kw,
            "insight": rep.insight, "limit_note": rep.limit_note,
            "segments": [{"label": s.label, "peak_month": s.peak_month,
                          "slope": s.slope, "found": s.found, "note": s.note}
                         for s in rep.segments]}


class CalReq(BaseModel):
    client_id: str
    client_secret: str
    category: str = "생활/건강"


@app.post("/api/calendar")
async def calendar(req: CalReq):
    """B. 시즌 캘린더 — 분야의 1년 장사 지도."""
    import datetime as _d

    from discovery.auto_scan import load_seeds
    from discovery.providers.naver_client import NaverClient
    from discovery.providers.naver_demand import NaverDemandProvider

    cat_id = _CATS.get(req.category, "")
    if not cat_id:
        return {"ok": False, "error": "분야를 골라주세요."}
    seeds = [k for k, _ in load_seeds(req.category)][:10]
    if not seeds:
        return {"ok": False, "error": "이 분야의 씨앗이 없어요."}
    try:
        async with NaverClient(req.client_id, req.client_secret,
                               datalab_max_retries=2) as client:
            d = NaverDemandProvider(client)
            cal = await build_calendar(None, d, seeds, cat_id,
                                       today_month=_d.date.today().month,
                                       budget=10)
            calls = client.call_count
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(f"{type(exc).__name__}: {exc}")}
    if not cal.months and not cal.flat:
        return {"ok": False, "error": (
            "데이터랩에서 아무것도 받지 못했어요.\n"
            "한도가 찼을 수 있어요 — 5~10분 뒤에 다시 눌러주세요.")}
    return {"ok": True, "calls": calls, "category": req.category,
            **cal.as_dict()}


@app.get("/api/tracked")
async def tracked():
    """추적 중인 키워드 (방향을 보려면 여러 번 봐야 함)."""
    return {"items": tracked_keywords()}


class PriceReq(BaseModel):
    client_id: str
    client_secret: str
    keyword: str


@app.post("/api/price_debug")
async def price_debug(req: PriceReq):
    """
    가격 진단 — '왜 이 값이 시장 최저가인가' 를 한 줄씩 보여준다.

    [왜 필요한가] 가격이 틀릴 때마다 '뭐가 섞였을까' 를 추측으로 고쳐왔다.
    그건 삽질이다. 네이버가 준 것을 그대로 펼쳐놓고, 우리가 무엇을 왜 버렸는지
    한 줄씩 보여주면 — 어디서 틀렸는지 눈으로 바로 잡힌다.
    """
    import re as _re

    from discovery.providers.naver_client import NaverClient

    kw = _re.sub(r"\s+", " ", req.keyword).strip()
    if not kw:
        return {"ok": False, "error": "물건 이름을 넣어주세요."}
    try:
        async with NaverClient(req.client_id, req.client_secret) as client:
            sim = await client.search_shop(kw, display=100, sort="sim")
            asc = await client.search_shop(kw, display=40, sort="asc")
            calls = client.call_count
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(f"{type(exc).__name__}: {exc}")}

    def _clean(t):
        return _re.sub(r"<[^>]+>", "", t or "")

    def _int(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return 0

    # 기준가 = 상위 노출 상품들의 중앙값
    sim_prices = sorted(_int(i.get("lprice")) for i in (sim.get("items") or [])
                        if _int(i.get("lprice")) > 0)
    ref = sim_prices[len(sim_prices) // 2] if sim_prices else 0
    core_key = kw.replace(" ", "")
    NEW = (1, 2, 3)

    rows = []
    kept = []
    for it in (asc.get("items") or []):
        lp = _int(it.get("lprice"))
        title = _clean(it.get("title", ""))
        pt = _int(it.get("productType"))
        link = it.get("link", "")
        why = ""
        if lp <= 0:
            why = "가격 없음"
        elif pt and pt not in NEW:
            why = f"중고/단종/판매예정 (유형 {pt})"
        elif core_key and core_key not in title.replace(" ", ""):
            why = "제목에 핵심어 없음 → 부속품·다른 상품"
        elif ref and lp < ref * 0.35:
            why = f"기준가({ref:,}원)의 35% 미만 → 너무 쌈"
        else:
            kept.append(lp)
        rows.append({"title": title, "lprice": lp, "ptype": pt, "link": link,
                     "mall": it.get("mallName", ""),
                     "excluded": why, "kept": (why == "")})

    # 군집 검사 — 혼자 튀는 값은 시장가가 아니다
    kept.sort()
    chosen, cluster_note = 0, ""
    for i, p in enumerate(kept):
        near = sum(1 for q in kept[i:] if q <= p * 1.25)
        if near >= 3:
            chosen = p
            cluster_note = f"{p:,}원 근처(±25%)에 {near}개가 모여 있어 인정"
            break
    if not chosen and kept:
        chosen = kept[len(kept) // 2]
        cluster_note = "군집이 없어 중앙값으로 대체 (믿을 만한 최저가 없음)"

    return {
        "ok": True, "calls": calls, "keyword": kw,
        "total": sim.get("total"), "reference": ref,
        "asc_raw_low": min([r["lprice"] for r in rows if r["lprice"] > 0] or [0]),
        "chosen": chosen, "cluster_note": cluster_note,
        "kept_count": len(kept), "rows": rows[:25],
        "naver": f"https://search.shopping.naver.com/search/all?query={quote(kw)}&sort=price_asc",
    }


class TitleABReq(BaseModel):
    client_id: str
    client_secret: str
    titles: list = []


@app.post("/api/title_ab")
async def title_ab(req: TitleABReq):
    """
    ④ 제목 A/B — 후보 제목들을 실제로 검색해 '어느 게 제일 비어 있나' 를 본다.
    (제목을 3개 주고 마는 게 아니라, 어느 걸 써야 하는지까지 답한다)
    """
    from discovery.providers.naver_client import NaverClient
    from discovery.providers.naver_shop import NaverShopProvider

    titles = [str(t).strip() for t in (req.titles or []) if str(t).strip()][:5]
    if not titles:
        return {"ok": False, "error": "확인할 제목이 없어요."}
    try:
        async with NaverClient(req.client_id, req.client_secret) as client:
            shop = NaverShopProvider(client)
            rows = []
            for t in titles:
                try:
                    m = await shop.market_of(t, sample=10)
                    rows.append({"title": t, "total": m.total, "ok": True})
                except Exception as exc:  # noqa: BLE001
                    if "RateLimited" in type(exc).__name__ or "429" in str(exc):
                        return {"ok": False, "error": friendly_error(str(exc))}
                    rows.append({"title": t, "total": 0, "ok": False})
            calls = client.call_count
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(f"{type(exc).__name__}: {exc}")}

    live = [r for r in rows if r["ok"] and r["total"] > 0]
    live.sort(key=lambda r: r["total"])
    best = live[0]["title"] if live else ""
    for r in rows:
        r["best"] = (r["title"] == best)
    return {"ok": True, "calls": calls, "rows": rows, "best": best,
            "note": (f"'{best}' 가 가장 비어 있어요 "
                     f"({live[0]['total']:,}개)" if live
                     else "세 제목 모두 검색이 안 잡혀요")}


class OrdersReq(BaseModel):
    csv_text: str = ""


@app.post("/api/orders")
async def orders(req: OrdersReq):
    """⑤ 판매 기록 대조 — 도구 추천이 진짜 매출로 이어졌나."""
    from discovery.orders import match_with_tool, parse_orders
    from discovery.tracker import watch_keywords

    rep = parse_orders(req.csv_text or "")
    if not rep.ok:
        return {"ok": False, "error": rep.note}
    try:
        keys = list(watch_keywords())
        for it in tracked_keywords():
            keys.append(it["keyword"])
    except Exception:  # noqa: BLE001
        keys = []
    rep = match_with_tool(rep, keys)
    return {"ok": True, "note": rep.note, "rows": rep.rows,
            "name_col": rep.name_col, "qty_col": rep.qty_col,
            "amt_col": rep.amt_col, "total_qty": rep.total_qty,
            "total_amount": rep.total_amount, "hit_pct": rep.hit_pct,
            "top": [{"name": i.name, "qty": i.qty, "amount": i.amount}
                    for i in rep.items[:12]],
            "matched": rep.matched[:12], "unmatched": rep.unmatched[:8]}


class WatchReq(BaseModel):
    keyword: str
    payload: dict = {}
    owner: str = "local"     # 브라우저마다 다른 값 — 관심목록이 안 섞이게


@app.post("/api/watch/add")
async def watch_add_api(req: WatchReq):
    try:
        who = (req.owner or "local")[:64]
        watch_add(req.keyword, req.payload, owner=who)
        return {"ok": True, "count": len(watch_list(who))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.post("/api/watch/remove")
async def watch_remove_api(req: WatchReq):
    try:
        who = (req.owner or "local")[:64]
        watch_remove(req.keyword, owner=who)
        return {"ok": True, "count": len(watch_list(who))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


class WatchTrackReq(BaseModel):
    client_id: str
    client_secret: str
    owner: str = "local"


@app.post("/api/watch/track")
async def watch_track_api(req: WatchTrackReq):
    """⭐ 관심목록 주간 추적 — 담을 때 상승률 대비 '지금'을 다시 재서
    계속 오르는지/꺾였는지, 급상승으로 전환됐는지(🔔)를 알려준다."""
    import asyncio
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        return {"ok": False, "error": "허브 열쇠(Client ID/Secret)를 먼저 넣어주세요."}
    who = (req.owner or "local")[:64]
    try:
        items = watch_list(who)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "items": []}
    if not items:
        return {"ok": True, "items": []}
    start, end = _date_range(12)
    sem = asyncio.Semaphore(4)

    async def one(it):
        kw = it.get("keyword")
        catname = it.get("category", "")
        code = _CATS.get(catname)
        if not kw or not code:
            return None
        body = {"startDate": start, "endDate": end, "timeUnit": "month",
                "category": str(code), "keyword": [{"name": kw, "param": [kw]}]}
        async with sem:
            try:
                data = await _hub_datalab(cid, csec,
                                          "/shopping/v1/category/keywords", body)
            except Exception:  # noqa: BLE001
                return None
        res = data.get("results") or []
        if not res:
            return None
        t = _trend_of(res[0].get("data") or [])
        now = t.get("rise", 0.0)
        added = float(it.get("rise") or 0.0)
        diff = round(now - added, 1)
        if diff >= 5:
            d = "up"
        elif diff <= -5:
            d = "down"
        else:
            d = "flat"
        return {"keyword": kw, "category": catname,
                "added_rise": round(added, 1), "now_rise": now, "delta": diff,
                "dir": d, "turned": bool(added < 20 and now >= 20),
                "rollover": t.get("rollover", False),
                "grade": it.get("grade", ""), "added_at": it.get("added_at", "")}

    got = await asyncio.gather(*(one(it) for it in items[:15]))
    out = [x for x in got if x]
    out.sort(key=lambda x: -x["now_rise"])
    return {"ok": True, "items": out, "alerts": [x for x in out if x["turned"]]}


@app.get("/api/watch")
async def watch_list_api(owner: str = "local"):
    try:
        items = watch_list((owner or "local")[:64])
        return {"ok": True, "items": items, "count": len(items)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "items": []}


@app.get("/api/watch/moves")
async def watch_moves_api(owner: str = "local"):
    """관심 키워드별 '변동 신호' 요약 — 골든타임/치킨게임 등.
    movement_of() 가 두 시점 스냅샷을 비교해 방향을 알려준다.
    스냅샷은 찾기/확인을 돌릴 때마다 record() 로 쌓인다(무료 플랜은 재시작 시 초기화)."""
    who = (owner or "local")[:64]
    try:
        items = watch_list(who)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "items": []}

    out = []
    for it in items:
        kw = it.get("keyword")
        if not kw:
            continue
        try:
            mv = movement_of(kw)
        except Exception:  # noqa: BLE001
            continue
        out.append({
            "keyword": kw,
            "category": it.get("category", ""),
            "stage": mv.stage,
            "note": mv.note,
            "action": mv.action,
            "points": mv.points,
            "days": round(mv.days, 1),
            "total_change": mv.total_change,
            "price_change": mv.price_change,
            "demand_change": mv.demand_change,
            "golden": mv.golden,
            "season": it.get("season"),   # 🏷️ 담을 때 저장된 시즌 프로파일
        })

    # 눈에 띄어야 하는 순서: 골든타임 → 치킨게임 → 몰리는중 → 식는중 → 조용함 → 데이터부족
    order = {"골든타임": 0, "치킨게임": 1, "몰리는중": 2,
             "식는중": 3, "조용함": 4, "데이터부족": 5}
    out.sort(key=lambda x: order.get(x["stage"], 9))
    golden = sum(1 for x in out if x["stage"] == "골든타임")
    chicken = sum(1 for x in out if x["stage"] == "치킨게임")
    crowding = sum(1 for x in out if x["stage"] == "몰리는중")
    ready = sum(1 for x in out if x["points"] >= 2 and x["stage"] != "데이터부족")
    return {"ok": True, "items": out, "count": len(out),
            "golden": golden, "chicken": chicken, "crowding": crowding,
            "ready": ready}


class TitleReq(BaseModel):
    client_id: str
    client_secret: str
    titles: list = []


@app.post("/api/title_check")
async def title_check(req: TitleReq):
    """
    ④ 상품명 A/B — 제목 후보마다 '실제로 몇 개가 걸리는지' 물어본다.
    제목을 3개 주고 고르라 하면 사용자는 근거가 없다. 숫자를 붙여준다.
    """
    from discovery.providers.naver_client import NaverClient
    from discovery.providers.naver_shop import NaverShopProvider

    tits = [t.strip() for t in (req.titles or []) if t and t.strip()][:4]
    if not tits:
        return {"ok": False, "error": "확인할 제목이 없어요."}
    try:
        async with NaverClient(req.client_id, req.client_secret) as client:
            shop = NaverShopProvider(client)
            out = []
            for t in tits:
                try:
                    m = await shop.market_of(t, sample=10)
                    out.append({"title": t, "total": m.total})
                except Exception:  # noqa: BLE001
                    out.append({"title": t, "total": -1})
            calls = client.call_count
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(f"{type(exc).__name__}: {exc}")}

    valid = [o for o in out if o["total"] >= 0]
    if valid:
        best = min(valid, key=lambda o: o["total"])
        for o in out:
            o["best"] = (o is best)
        note = (f"'{best['title']}' 이 가장 비어 있어요 "
                f"({best['total']:,}개) — 이걸로 올리세요")
    else:
        note = "제목 확인에 실패했어요."
    return {"ok": True, "calls": calls, "items": out, "note": note}


class SalesReq(BaseModel):
    csv_b64: str = ""
    picks: list = []


@app.post("/api/sales")
async def sales_api(req: SalesReq):
    """⑤ 내 판매 기록 대조 — 도구 추천이 실제 매출로 이어졌나."""
    import base64

    from discovery.sales import match_picks, parse_sales

    try:
        raw = base64.b64decode(req.csv_b64 or "")
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "파일을 읽지 못했어요."}
    if not raw:
        return {"ok": False, "error": "파일이 비어 있어요."}
    rep = parse_sales(raw)
    if not rep.ok:
        return {"ok": False, "error": rep.note, "columns": rep.columns}
    # picks 는 카드 목록. 문자열로 와도 받아들인다.
    picks = []
    for p in (req.picks or []):
        if isinstance(p, dict) and p.get("keyword"):
            picks.append(p)
        elif isinstance(p, str) and p.strip():
            picks.append({"keyword": p.strip()})
    if picks:
        rep = match_picks(rep, picks)
    return {"ok": True, "rows": rep.rows, "products": len(rep.products or []),
            "name_col": rep.name_col, "qty_col": rep.qty_col,
            "total_qty": rep.total_qty, "total_amt": rep.total_amt,
            "matched": rep.matched, "unmatched": rep.unmatched_picks,
            "top": (rep.products or [])[:15], "note": rep.note}


class CustomsReq(BaseModel):
    item_price: int = 0
    qty: int = 1
    shipping: int = 0
    category: str = ""
    keyword: str = ""
    duty_rate: float = -1.0
    sell_price: int = 0
    fee_pct: float = 25.0


@app.post("/api/customs")
async def customs_api(req: CustomsReq):
    """① 관세·부가세 — 도매 수입에서 마진을 삼키는 것."""
    from discovery.customs import estimate, margin_after_tax
    c = estimate(item_price=req.item_price, qty=req.qty, shipping=req.shipping,
                 category=req.category, keyword=req.keyword,
                 duty_rate=(None if req.duty_rate < 0 else req.duty_rate),
                 for_resale=True)
    m = (margin_after_tax(req.sell_price, c.per_unit, req.fee_pct / 100.0)
         if req.sell_price else None)
    return {"ok": True, "item_total": c.item_total, "shipping": c.shipping,
            "cif": c.cif, "duty_rate": c.duty_rate, "duty": c.duty,
            "vat": c.vat, "tax_total": c.tax_total,
            "grand_total": c.grand_total, "per_unit": c.per_unit,
            "warns": c.warns, "note": c.note, "margin": m}


@app.get("/api/checklist")
async def checklist_api(mode: str = "consign", category: str = ""):
    """④ 시작 전에 갖춰야 할 것 — 통관고유부호 없으면 물건이 안 나온다."""
    from discovery.checklist import as_dicts
    return {"ok": True, "mode": mode, "steps": as_dicts(mode, category)}


@app.get("/favicon.ico")
async def favicon():
    """브라우저가 자동으로 찾는 아이콘 — 없으면 로그에 404 가 쌓인다."""
    from fastapi.responses import Response
    return Response(status_code=204)


@app.get("/sw.js")
async def sw():
    """일부 브라우저/확장이 서비스워커를 찾는다. 안 쓰므로 조용히 응답."""
    from fastapi.responses import Response
    return Response(status_code=204)


@app.get("/api/img")
async def img_proxy(u: str):
    """공급사 상품 이미지 프록시(미리보기 전용).
    일부 공급사 CDN은 외부 <img> 직접표시(핫링크)를 막아 그림이 안 뜬다.
    서버가 공식 상품 이미지 URL을 대신 받아 다시 내려주면 미리보기·이미지저장(html2canvas)이 안정적으로 된다.
    ※ 내보내기 HTML·URL 목록은 원본 주소를 그대로 쓴다(스마트스토어 업로드용). 남의 상세페이지 스크래핑이 아니라 판매할 상품의 공식 이미지다."""
    import httpx
    from fastapi.responses import Response
    if not (isinstance(u, str) and u.startswith(("http://", "https://"))):
        return Response(status_code=400, content=b"bad url")
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(u)
        referer = f"{parts.scheme}://{parts.netloc}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": referer,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=8, read=20, write=8, pool=8),
                                     follow_redirects=True) as c:
            r = await c.get(u, headers=headers)
    except Exception:  # noqa: BLE001
        return Response(status_code=502, content=b"fetch error")
    if r.status_code != 200:
        return Response(status_code=502, content=b"upstream %d" % r.status_code)
    ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        # 이미지가 아니면(핫링크 차단 페이지 등) 거절 — 프론트가 원본으로 폴백
        return Response(status_code=415, content=b"not image")
    data = r.content
    if len(data) > 12 * 1024 * 1024:            # 12MB 초과는 거절(비정상)
        return Response(status_code=413, content=b"too large")
    return Response(content=data, media_type=ct,
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/")
async def index():
    # 화면(index.html)은 캐시하지 않게 — 배포하면 회원 브라우저가 항상 최신을 받는다
    return FileResponse(_HERE / "static" / "index.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})


app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

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
from discovery.tracker import (grade_verdicts, hit_rate, movement_of,  # noqa: E402
                               pool_stats, record, say_verdict,
                               tracked_keywords, watch_add,
                               watch_list, watch_remove)

_HERE = Path(__file__).resolve().parent
_SCAN_TIMEOUT = 70.0   # 한 요청이 이보다 오래 붙들면 브라우저가 끊는다
APP_VERSION = "v61"   # 화면에 찍어서 '예전 서버가 도는지' 눈으로 알게 한다

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


async def _hub_datalab(cid: str, csec: str, path: str, body: dict) -> dict:
    """NAVER API HUB Data Lab(쇼핑 인사이트·검색어 트렌드) POST 호출.
    공식 명세: POST /datalab/v1/... + JSON 바디, X-NCP-APIGW 헤더."""
    import httpx
    url = f"{_HUB_BASE}{path}"
    headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec,
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(url, headers=headers, json=body)
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
    """데이터랩 시계열(data:[{period,ratio}])에서 상승/하락 신호를 뽑는다.
    최근 3구간 평균 vs 그 이전 평균의 변화율로 '뜨는 중'을 판정."""
    vals = []
    for p in (points or []):
        try:
            vals.append(float(p.get("ratio")))
        except (TypeError, ValueError):
            pass
    if len(vals) < 4:
        return {"latest": round(vals[-1], 1) if vals else 0.0,
                "rise": 0.0, "stage": "데이터부족", "series": [round(v, 1) for v in vals]}
    recent = sum(vals[-3:]) / 3.0
    earlier = sum(vals[:-3]) / max(1, len(vals) - 3)
    rise = round((recent - earlier) / max(1.0, earlier) * 100, 1)
    peak = max(vals) or 1.0
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
    return {"latest": round(vals[-1], 1), "rise": rise, "stage": stage,
            "peak_pct": round(vals[-1] / peak * 100),
            "series": [round(v, 1) for v in vals]}


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


def _seed_bank() -> dict:
    """분야별 씨앗 키워드 은행 {분야명: (코드, [키워드...])} — 자동 발굴 후보 풀."""
    try:
        p = _HERE.parent / "discovery" / "data" / "category_seeds.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        return {c["name"]: (str(c["cat_id"]), list(c.get("keywords", [])))
                for c in d.get("categories", [])}
    except Exception:  # noqa: BLE001
        return {}


class DiscoverReq(BaseModel):
    client_id: str
    client_secret: str
    category: str = ""          # 분야명 ("" = 전체 분야 스캔)


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
    start, end = _date_range(12)
    # (분야, 코드, 5개 배치) 작업 목록
    jobs = []
    for cat in targets:
        code, kws = bank[cat]
        for i in range(0, len(kws), 5):
            jobs.append((cat, code, kws[i:i + 5]))
    sem = asyncio.Semaphore(4)
    first_err = {"msg": ""}

    async def run(cat, code, batch):
        async with sem:
            body = {"startDate": start, "endDate": end, "timeUnit": "month",
                    "category": code,
                    "keyword": [{"name": k, "param": [k]} for k in batch]}
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
            picked = []
            for r in (data.get("results") or []):
                t = _trend_of(r.get("data") or [])
                if t.get("series"):
                    picked.append({"keyword": r.get("title") or "",
                                   "category": cat, **t})
            return picked

    batches = await asyncio.gather(*(run(c, code, b) for c, code, b in jobs))
    items = [x for sub in batches for x in sub]
    if not items:
        return {"ok": False,
                "error": "발굴 결과가 비었어요 — " + (first_err["msg"] or
                         "트렌드 데이터를 못 받았어요.")}
    # 상승률 순 → 상위. 급상승·상승만 '뜨는 후보'로 표시
    items.sort(key=lambda x: -x["rise"])
    for it in items:
        it["hot"] = it["rise"] >= 5
    return {"ok": True, "items": items[:40], "range": f"{start} ~ {end}",
            "scanned": len(items), "targets": targets}


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


@app.get("/")
async def index():
    return FileResponse(_HERE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

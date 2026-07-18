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
import re
import sys
from pathlib import Path

from fastapi import FastAPI
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
APP_VERSION = "v37"   # 화면에 찍어서 '예전 서버가 도는지' 눈으로 알게 한다

# ── 실시간 접속자 (인메모리) ──────────────────────────────────
# 무료 플랜은 재시작/슬립 때 이 값이 초기화됩니다(누적=오늘 기준으로 취급).
import time as _time  # noqa: E402
_PRESENCE: dict[str, float] = {}      # visitor_id -> last_seen(ts)
_PRESENCE_WINDOW = 75.0                # 이 초 안에 하트비트가 있으면 '접속 중'
_SEEN_TODAY: dict[str, float] = {}     # visitor_id -> 첫 방문(ts), 누적 고유수 산정
_DAY_ANCHOR = {"day": _time.gmtime().tm_yday}
_PRESENCE_MAX = 20000                  # 메모리 폭주 방지 상한

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
async def auto(req: AutoReq):
    """
    [Failed to fetch 방지]
    예산 400 × 호출 간격 0.12초 = 48초를 한 요청이 붙들고 있었다.
    거기에 429 재시도까지 겹치면 1분을 넘겨 브라우저가 연결을 끊는다
    (화면엔 'Failed to fetch' 로 보인다 — 사용자는 원인을 알 수 없다).
    → 시간 상한을 두고, 넘으면 '그때까지 찾은 것' 이라도 돌려준다.
    """
    """자동 발굴 — 아무것도 입력하지 않아도 팔 만한 것을 찾아 대령."""
    from discovery.auto_scan import auto_scan
    from discovery.providers.naver_client import NaverClient
    from discovery.providers.naver_demand import NaverDemandProvider
    from discovery.providers.naver_shop import NaverShopProvider

    import asyncio as _aio
    budget = max(20, min(600, req.budget))
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
    return {"ok": True, "calls": calls, "scanned": budget,
            "finds": out, "report": rep.as_dict(), "note": rep.summary(),
            "hitrate": hits, "pool": pool}


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


class MetaReq(BaseModel):
    url: str


_META_ALLOW = ("smartstore.naver.com", "m.smartstore.naver.com",
               "brand.naver.com", "shopping.naver.com",
               "search.shopping.naver.com", "msearch.shopping.naver.com",
               "naver.me")


def _og(html_text: str, prop: str) -> str:
    """OG/메타 태그 하나의 content 를 뽑는다 (속성 순서 무관)."""
    m = re.search(
        r'<meta\b[^>]*\b(?:property|name)\s*=\s*["\']' + re.escape(prop)
        + r'["\'][^>]*>', html_text, re.I)
    if not m:
        return ""
    cm = re.search(r'\bcontent\s*=\s*["\'](.*?)["\']', m.group(0), re.I | re.S)
    import html as _html
    return _html.unescape(cm.group(1)).strip() if cm else ""


@app.post("/api/page_meta")
async def page_meta(req: MetaReq):
    """상세페이지가 '공개한' 공유용 메타(OG)만 읽는다 — 크롤링 아님.
    스마트스토어/네이버 쇼핑 주소만 허용(SSRF 방지). 제목·대표이미지·설명·가격.
    어떤 경우에도 JSON 을 돌려준다(500 로 죽지 않게) → 프런트가 원인을 안내."""
    from urllib.parse import urlparse
    try:
        url = (req.url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        host = (urlparse(url).hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in _META_ALLOW):
            return {"ok": False,
                    "error": "스마트스토어·네이버 쇼핑 주소만 확인할 수 있어요."}
        try:
            import httpx
        except Exception:  # noqa: BLE001
            return {"ok": False,
                    "error": "서버에 httpx 가 없어요. requirements.txt 로 설치 후 재배포해주세요."}

        # 실제 브라우저처럼 보이는 헤더 — 네이버가 봇 UA 에 빈 페이지를 주는 걸 줄인다.
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        }
        text = ""
        final_url = url
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=9.0,
                                         headers=headers) as c:
                r = await c.get(url)
                final_url = str(r.url)
                final_host = (urlparse(final_url).hostname or "").lower()
                if not (final_host.endswith("naver.com")
                        or final_host.endswith("naver.me")):
                    return {"ok": False, "error": "네이버 페이지가 아니에요."}
                text = r.text[:300000]
        except Exception:  # noqa: BLE001
            return {"ok": False,
                    "error": "페이지를 불러오지 못했어요 — 네이버가 서버 접근을 막았거나 "
                             "주소가 상세페이지가 아닐 수 있어요."}

        title = _og(text, "og:title")
        image = _og(text, "og:image")
        desc = _og(text, "og:description")
        price = (_og(text, "product:price:amount") or _og(text, "og:price:amount")
                 or _og(text, "product:sale_price:amount"))
        title = re.sub(r"\s*[:\-|]\s*(네이버\s*쇼핑|스마트스토어|스토어|브랜드스토어).*$",
                       "", title).strip()
        if not (title or image):
            return {"ok": False,
                    "error": "이 페이지에서 공유용 정보를 못 찾았어요 — 네이버가 서버엔 "
                             "빈 페이지를 준 것 같아요. (로그인 전용·봇차단 페이지일 수 있음)"}
        return {"ok": True, "title": title, "image": image, "desc": desc,
                "price": price, "final_url": final_url}
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "확인 중 문제가 생겼어요. 잠시 후 다시 시도해주세요."}


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

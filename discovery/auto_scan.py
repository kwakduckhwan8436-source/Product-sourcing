"""
discovery.auto_scan
==================
자동 발굴 — 사용자가 아무것도 입력하지 않아도 '팔 만한 것'을 찾아 대령한다.

[왜 필요한가]
초보는 '뭘 팔지'를 모른다. 검색칸을 주는 것 자체가 이미 어려운 요구다.
씨앗(대분류별 대표어) → 축 조합으로 롱테일 생성 → 전부 조회 →
경쟁 실체까지 보고 '진짜 들어갈 자리'만 남긴다.

[원가 없이 돈 되는지 판단하는 법 — 마진 역산]
공급가를 모르면 마진을 못 낸다. 대신 역산한다:
  "최저가 20,000원 → 12,000원 이하로 떼오면 3,000원 남음"
사용자는 공급처에서 그 값에 되는지만 확인하면 된다. 이게 소싱 기준선.

[호출 예산]
씨앗 하나당 조합 N개 = 검색 N회. budget 으로 상한을 두고,
429 가 연속으로 나면 즉시 중단한다(무한 대기 금지).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from discovery.competition import CompetitionProfile, analyze_competition
from discovery.keyword_entry import coupang_link, grade_entry, naver_link
from discovery.compare import market_links, price_spread_of
from discovery.market_price import market_price_of
from discovery.mode import (mode_advice, reverse_cost as mode_cost,
                            rule_of, score_weights, target_of)
from discovery.quality import assess_quality, deep_quality
from discovery.suppliers import mode_tip, supplier_links
from discovery.keyword_forge import axes_for, core_of, forge

logger = logging.getLogger(__name__)

_FEE_PCT = 0.25
_SEEDS_PATH = Path(__file__).resolve().parent / "data" / "category_seeds.json"

# 자동 발굴 기준
# [왜 완화했나] 한국 네이버는 웬만한 말이 수만 개다. 5,000 컷으로는 0개가 나온다.
# 0개는 사용자에게 아무 쓸모가 없다 → 절대 안 되는 것만 자르고,
# 나머지는 등급으로 줄세워 '그중 나은 것'을 보여준다.
_HARD_MAX = 50_000       # 이 이상은 광고 없이 절대 불가 (절대 컷)
_MIN_TOTAL = 20          # 이 미만이면 수요 미검증(유령)
_MIN_PRICE = 8_000       # 최저가 이 미만이면 뭘 해도 안 남음
_TARGET_MARGIN = 3_000   # 역산 목표 마진
_MIN_RELEVANCE = 0.5     # 결과 제목의 이만큼은 핵심어를 담아야 '그 시장'이다
_MIN_MOD_USE = 0.05      # 수식어가 제목의 이만큼도 안 쓰이면 '없는 말'이다
_TOP_PRICE_CHECK = 60    # 상위 이만큼만 최저가를 실제 조회
# ↑ 이게 결과 개수의 천장이다. 30 이면 아무리 많이 훑어도 30개가 끝.
#   네이버 호출 간격(0.12초) 때문에 무한정 늘릴 순 없다 — 60이 30초선의 한계.
_PARALLEL = 8            # 동시에 몇 개를 물어볼까 (클라이언트가 초당 간격은 지킴)
_SEED_BATCH = 56         # 한 번에 파볼 씨앗 수 (넓게 훑기)

# 등급 경계 (경쟁 등록 수)
_G_OPEN = 1_500          # 이하 = 진짜 빈자리
_G_NARROW = 8_000        # 이하 = 좁지만 가능


@dataclass(slots=True)
class ScanReport:
    """왜 0개인지 정직하게 보여주기 위한 집계."""
    looked: int = 0
    red: int = 0          # 너무 붐빔
    ghost: int = 0        # 검색 자체가 거의 없음 (죽은 말)
    off: int = 0          # 엉뚱함 — 그 상품이 아닌 게 나옴
    nolow: int = 0        # 그 상품의 가격을 못 찾음
    cheap: int = 0        # 최저가가 낮아 마진 불가
    blocked: int = 0      # 카탈로그/대형몰 장악
    passed: int = 0

    def as_dict(self) -> dict:
        return {"looked": self.looked, "red": self.red, "ghost": self.ghost,
                "off": self.off, "nolow": self.nolow,
                "cheap": self.cheap, "blocked": self.blocked,
                "passed": self.passed}

    def summary(self) -> str:
        if self.passed:
            hard = []
            if self.red:
                hard.append(f"붐비는 곳 {self.red}개 포함")
            tail = f" ({', '.join(hard)})" if hard else ""
            return f"{self.looked}개를 훑어 {self.passed}개 건졌어요{tail}"
        parts = []
        if self.red:
            parts.append(f"너무 붐빔 {self.red}개")
        if self.blocked:
            parts.append(f"가격비교·대형몰 장악 {self.blocked}개")
        if self.ghost:
            parts.append(f"찾는 사람 없음 {self.ghost}개")
        if self.off:
            parts.append(f"엉뚱한 상품 {self.off}개")
        if self.nolow:
            parts.append(f"가격 못 찾음 {self.nolow}개")
        if self.cheap:
            parts.append(f"마진 불가 {self.cheap}개")
        return (f"{self.looked}개를 훑었는데 남은 게 없어요 — " + ", ".join(parts))


@dataclass(slots=True)
class Find:
    keyword: str
    axis: str = ""
    category: str = ""
    total: int = 0
    price_min: int = 0            # 대표가 — 마진 역산 기준
    naver_low: int = 0            # 네이버 검색 '낮은가격순' 1등 (사용자가 보는 값)
    naver_low_title: str = ""     # 그 값이 무슨 상품인지 (커버? 중고? 본품?)
    naver_low_skipped: int = 0    # 웹처럼 건너뛴 중고·단종 개수
    naver_low_other: int = 0      # 건너뛴 '다른 상품' 개수 (케이프 같은 것)
    naver_low_link: str = ""      # 그 가격이 나온 '바로 그 상품' 페이지
    core: str = ""                # 이 시장의 핵심 명사 (최저가 조회용)
    mode: str = ""                # 위탁 / 도매
    target_margin: int = 0        # 이 상품에서 남길 돈 (값에 비례)
    quality: dict = field(default_factory=dict)   # 떼도 되는 물건인가
    suppliers: list = field(default_factory=list)  # 떼올 곳
    mode_note: list = field(default_factory=list)  # 이 방식에서 알아야 할 것
    reasons: list = field(default_factory=list)   # 왜 이게 뽑혔나 (근거)
    doubts: list = field(default_factory=list)    # 뭘 조심해야 하나
    same_product: bool = False    # 최저가가 '가격비교로 묶인 같은 제품' 인가
    spread: dict = field(default_factory=dict)   # 판매처별 가격 폭 (네이버 가격비교)
    links: list = field(default_factory=list)    # 다른 마켓에서 열어보기
    # ↑ 검색 결과로 보내면 웹은 광고를 끼워 넣어 값이 절대 안 맞는다.
    #   그 상품 페이지로 직접 보내면 누르는 순간 같은 숫자가 보인다.
    price_basis: str = ""         # 'catalog'(네이버 계산) / 'listing'(개별 매물)
    price_note: str = ""
    price_trusted: bool = False
    need_cost: int = 0            # 이 값 이하로 떼오면 목표 마진 남음
    comp: CompetitionProfile | None = None
    score: float = 0.0
    grade: str = ""        # 빈자리 / 좁음 / 어려움
    title: str = ""
    listing: dict = field(default_factory=dict)   # 제목·상세·세트 (A/C)
    naver: str = ""
    coupang: str = ""

    def as_dict(self) -> dict:
        c = self.comp
        return {
            "keyword": self.keyword, "axis": self.axis, "category": self.category,
            "total": self.total, "price_min": self.price_min,
            "naver_low": self.naver_low, "naver_low_title": self.naver_low_title,
            "naver_low_skipped": self.naver_low_skipped,
            "naver_low_other": self.naver_low_other,
            "naver_low_link": self.naver_low_link,
            "same_product": self.same_product,
            "reasons": self.reasons, "doubts": self.doubts,
            "mode": self.mode, "mode_note": self.mode_note,
            "target_margin": self.target_margin,
            "quality": self.quality, "suppliers": self.suppliers,
            "spread": self.spread, "links": self.links,
            "price_basis": self.price_basis, "price_note": self.price_note,
            "price_trusted": self.price_trusted,
            "need_cost": self.need_cost, "score": round(self.score, 1),
            "grade": self.grade,
            "title": self.title, "naver": self.naver, "coupang": self.coupang,
            "listing": self.listing,
            "comp": (None if c is None else {
                "grade": c.grade, "note": c.note, "can_enter": c.can_enter,
                "catalog_pct": c.catalog_pct, "bigmall_pct": c.bigmall_pct,
                "indie_pct": c.indie_pct, "basis": c.basis,
                "agree_pct": c.agree_pct}),
        }


def load_seeds(category: str = "") -> list[tuple[str, str]]:
    """씨앗 목록 → [(키워드, 카테고리명)]. category 지정 시 그 대분류만."""
    try:
        d = json.loads(_SEEDS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("씨앗 로드 실패: %s", exc)
        return []
    out = []
    for c in d.get("categories", []):
        if category and c.get("name") != category:
            continue
        for kw in c.get("keywords", []):
            out.append((kw, c.get("name", "")))
    return out


def categories() -> list[str]:
    try:
        d = json.loads(_SEEDS_PATH.read_text(encoding="utf-8"))
        return [c["name"] for c in d.get("categories", [])]
    except Exception:  # noqa: BLE001
        return []


def reverse_cost(price_min: int, target: int = _TARGET_MARGIN,
                 fee_pct: float = _FEE_PCT) -> int:
    """
    최저가에서 target 원 남기려면 얼마 이하로 떼야 하나.
    fee_pct 는 사용자가 자기 실제 수수료를 넣는다 (기본 25% 는 어디까지나 통념).
    """
    if price_min <= 0:
        return 0
    return max(0, int(price_min - price_min * fee_pct - target))


def modifier_used(market, keyword: str, core: str) -> float:
    """
    수식어가 이 시장에서 실제로 쓰이는 말인가.

    [왜 필요한가] 축 조합은 아무 명사에나 수식어를 갖다 붙인다. 그래서
    "가정용 트렌치코트" 같은, 아무도 안 쓰는 말이 만들어진다. 네이버는 느슨히
    매칭해서 뭔가를 돌려주고 등록 수도 적으니 '빈자리!' 로 통과해버린다.
    → 실제로는 시장이 없는 것이다.

    판별: 그 수식어가 상위 제목에 얼마나 등장하나. 0% 면 아무도 안 쓰는 말.
    """
    mods = [t for t in keyword.split() if t and t not in core]
    if not mods:
        return 1.0
    titles = [str(t).replace(" ", "") for t in
              (getattr(market, "sample_titles", None) or [])]
    if not titles:
        return 1.0
    worst = 1.0
    for m in mods:
        hit = sum(1 for t in titles if m.replace(" ", "") in t)
        worst = min(worst, hit / len(titles))
    return worst


def relevance_of(market, core: str) -> float:
    """
    검색이 엉뚱한 데를 짚었는지 본다.

    [왜 필요한가] "아기 실리콘주방장갑" 같은 조합도 네이버는 유사 매칭으로
    뭔가를 돌려준다. 등록 수가 적으니 '빈자리'로 통과해버린다 — 실제로는
    그런 시장이 없는 것이다. 결과 제목에 핵심어가 없으면 헛짚은 것.
    """
    titles = list(getattr(market, "sample_titles", None) or [])
    if not titles or not core:
        return 1.0
    hit = sum(1 for t in titles if core in str(t).replace(" ", ""))
    return hit / len(titles)


def explain(f, rank: int, total_count: int) -> tuple:
    """
    왜 이게 뽑혔나 — 근거와 의심을 함께 밝힌다.

    [왜 필요한가] 점수만 던지면 사용자는 믿을 근거가 없다. '82점' 이 아니라
    '이번에 본 것 중 경쟁이 제일 적고, 개인 셀러가 90% 라 자리가 있고,
    9,750원 이하로 떼면 남는다' 여야 판단할 수 있다.
    의심스러운 점도 숨기지 않는다 — 숨기면 그게 손실이 된다.
    """
    r, d = [], []
    c = f.comp
    if f.grade == "이 중 가장 빈 곳":
        r.append(f"이번에 훑은 {total_count}개 중 경쟁이 적은 축이에요 "
                 f"({f.total:,}개)")
    elif f.grade == "보통":
        r.append(f"경쟁은 중간이에요 ({f.total:,}개)")
    else:
        d.append(f"경쟁이 많은 편이에요 ({f.total:,}개) — 광고 없이는 힘듭니다")

    if c is not None:
        if (c.indie_pct or 0) >= 60:
            r.append(f"개인 스토어가 {c.indie_pct:.0f}% — 내 상세페이지로 승부할 수 있어요")
        if (c.catalog_pct or 0) >= 40:
            d.append(f"가격비교에 {c.catalog_pct:.0f}% 묶여 있어요 — 최저가 싸움이 됩니다")
        if (c.bigmall_pct or 0) >= 35:
            d.append(f"대형몰이 {c.bigmall_pct:.0f}% — 광고비로 밀립니다")

    if f.need_cost > 0:
        r.append(f"{f.need_cost:,}원 이하로 떼오면 한 개당 남아요")
    sp = f.spread or {}
    if sp.get("found"):
        if (sp.get("spread_pct") or 0) >= 40:
            r.append(f"파는 곳마다 값이 {sp['spread_pct']:.0f}% 벌어져 있어요 — 파고들 틈")
        elif (sp.get("spread_pct") or 0) <= 10:
            d.append("파는 곳마다 값이 거의 같아요 — 가격으로는 못 이깁니다")
    if f.same_product:
        r.append("최저가가 네이버 가격비교로 묶인 같은 제품이에요 (믿을 만함)")
    else:
        d.append("최저가가 다른 제품일 수 있어요 — [이 가격 상품 보기] 로 확인하세요")
    mv = None
    return r, d


def grade_of(total: int, open_total: int = _G_OPEN) -> str:
    """기준선은 사용자가 정한다 — 제 감보다 30년 감각이 정확하다."""
    if total <= open_total:
        return "빈자리"
    if total <= open_total * 5:
        return "좁음"
    return "어려움"


def _score(total: int, price_min: int, comp: CompetitionProfile | None) -> float:
    """
    점수 = 비어있음 × 들어갈수있음 × 마진여지.
    설명 가능해야 하므로 단순하게 유지한다.
    """
    # 비어있음: 적을수록 높음 (절대컷 구간 안에서)
    empt = max(0.0, min(1.0, 1 - (total - _MIN_TOTAL) / (_HARD_MAX - _MIN_TOTAL)))
    # 마진 여지: 최저가가 높을수록 떼올 폭이 큼 (8천~5만 구간)
    room = max(0.0, min(1.0, (price_min - _MIN_PRICE) / (50_000 - _MIN_PRICE)))
    # 들어갈 수 있음: 독립 상품 비율이 곧 내 자리
    enter = 0.2
    if comp is not None:
        if not comp.can_enter:
            return 0.0
        enter = max(0.2, comp.indie_pct / 100)
    return round(empt * 60 + room * 20 + enter * 20, 1)


def _title_of(keyword: str, seed: str) -> str:
    parts, seen = [], set()
    for t in f"{keyword} {seed}".split():
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
    return " ".join(parts)[:50]


async def _gather(coros, limit: int = _PARALLEL):
    """
    동시에 여러 개를 물어본다.

    [왜 필요한가] 그동안 한 번 부르고 응답을 기다리고, 또 부르고 — 순차였다.
    네이버 응답이 0.3초씩만 걸려도 120번이면 40초가 그냥 날아간다.
    클라이언트가 호출 '시작 간격'(0.12초)은 지키므로 초당 한도는 안 넘고,
    기다리는 시간만 겹쳐서 3~4배 더 많이 볼 수 있다.
    """
    import asyncio
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            try:
                return await c
            except Exception as exc:  # noqa: BLE001
                return exc

    return await asyncio.gather(*(run(c) for c in coros))


def _is_rate(x) -> bool:
    return isinstance(x, Exception) and (
        "RateLimited" in type(x).__name__ or "429" in str(x))


async def auto_scan(shop, category: str = "", budget: int = 320,
                    per_seed: int = 5, progress_cb=None, exclude=None,
                    mode: str = "consign",
                    fee_pct: float = _FEE_PCT,
                    target_margin: int = 0,   # 0 = 상품값에 비례 (자동)
                    max_total: int = _HARD_MAX,
                    open_total: int = _G_OPEN,
                    min_price: int = _MIN_PRICE):
    """
    씨앗 → (제목 채굴 + 캔 말) → 조회 → 경쟁 실체 → 등급/순위.

    [3단계 병렬]
      1) 씨앗들의 시장을 한꺼번에 조회 → 제목 채굴
      2) 캔 말들을 한꺼번에 조회 → 거르기 (여기가 대부분)
      3) 살아남은 상위만 최저가 조회 (비싼 호출이라 마지막에)
    """
    import random

    from discovery.listing import build_listing
    from discovery.title_mining import mine_titles, suggest_keywords
    from discovery.tracker import add_pool, mark_scanned, pool_keywords

    rule = rule_of(mode)          # 위탁/도매 — 기준이 다르다
    base_seeds = load_seeds(category)
    try:
        pooled = pool_keywords(category, limit=40)
    except Exception:  # noqa: BLE001
        pooled = []
    seeds = [(k, category) for k in pooled] + list(base_seeds)
    rep = ScanReport()
    if not seeds:
        return [], rep
    # [더 찾기] 방금 보여준 것들은 빼고 새 땅을 판다
    skip = {str(x).strip() for x in (exclude or []) if x}
    if skip:
        seeds = [(k, c) for k, c in seeds if k not in skip]
    random.shuffle(seeds)
    seeds = seeds[:_SEED_BATCH]

    used = 0
    # ── 1단계: 씨앗 시장을 한꺼번에 ────────────────────────────────
    if progress_cb:
        progress_cb(0, budget, f"{len(seeds)}개 분야를 한꺼번에 살펴보는 중")
    m0s = await _gather([shop.market_of(k) for k, _ in seeds])
    used += len(seeds)
    if sum(1 for x in m0s if _is_rate(x)) >= 3:
        if progress_cb:
            progress_cb(used, budget, "한도소진")
        return [], rep

    # ── 2단계: 캔 말들을 모아서 한꺼번에 ──────────────────────────
    cands: list[tuple[str, str, str, str]] = []   # (kw, axis, seed, cat)
    for (seed, cat), m0 in zip(seeds, m0s):
        if isinstance(m0, Exception):
            continue
        rep.looked += 1
        brands = set()
        for it in (m0.items or []):
            for k in ("brand", "maker", "mall"):
                v = (it.get(k) or "").strip()
                if v:
                    brands.add(v)
        try:
            mined = mine_titles(m0.sample_titles, base=seed, exclude=brands)
            extra = suggest_keywords(seed, mined, limit=per_seed)
            add_pool([k for k, _ in extra], category=cat, source="제목채굴")
            mark_scanned(seed)
        except Exception:  # noqa: BLE001
            extra = []
        got = [(k, a, seed, cat) for k, a in extra
               if k and k != seed and k not in skip]
        random.shuffle(got)
        cands.extend(got[:per_seed])
        if seed not in skip:
            cands.append((seed, "대표어", seed, cat))   # 씨앗 자체도 후보

    room = max(0, budget - used - _TOP_PRICE_CHECK)
    cands = cands[:room]
    if progress_cb:
        progress_cb(used, budget, f"후보 {len(cands)}개를 한꺼번에 확인하는 중")

    pre = {k: m for (k, _, s, _c), m in zip(seeds and cands, [])}  # noqa: F841
    seed_market = {s: m for (s, _c), m in zip(seeds, m0s)
                   if not isinstance(m, Exception)}
    to_fetch = [(k, a, s, c) for (k, a, s, c) in cands if k not in seed_market]
    fetched = await _gather([shop.market_of(k) for k, _, _, _ in to_fetch])
    used += len(to_fetch)
    if sum(1 for x in fetched if _is_rate(x)) >= 3 and not any(
            not isinstance(x, Exception) for x in fetched):
        if progress_cb:
            progress_cb(used, budget, "한도소진")
        return [], rep

    markets = {k: m for (k, _, _, _), m in zip(to_fetch, fetched)}
    for s, m in seed_market.items():
        markets[s] = m

    # ── 거르기 ──────────────────────────────────────────────────
    finds: list[Find] = []
    for kw, axis, seed, cat in cands:
        m = markets.get(kw)
        if m is None or isinstance(m, Exception):
            continue
        rep.looked += 1
        core_kw = core_of(seed)
        if m.total < _MIN_TOTAL:
            rep.ghost += 1
            continue
        if relevance_of(m, core_kw.replace(" ", "")) < _MIN_RELEVANCE:
            rep.off += 1
            continue
        if modifier_used(m, kw, core_kw) < _MIN_MOD_USE:
            rep.off += 1
            continue
        if m.total > max_total:
            rep.red += 1          # 세어만 두고 버리지 않음
        comp = analyze_competition(m)
        if not comp.can_enter:
            rep.blocked += 1
            continue
        mp = market_price_of(m, core=core_kw)
        spd = price_spread_of(m, core=core_kw)
        lst = {}
        try:
            bs = set()
            for it in (m.items or []):
                for k2 in ("brand", "maker", "mall"):
                    v = (it.get(k2) or "").strip()
                    if v:
                        bs.add(v)
            mk = mine_titles(m.sample_titles or [], base=core_kw, exclude=bs)
            L = build_listing(kw, core_kw, mk, mp.lowest or 0, 0)
            # ⑤ 다듬은 제목 3벌 · ⑥ 상세 8줄 + 사진 목록
            from discovery.listing import build_detail_plan, polish_titles
            pol = polish_titles(kw, core_kw, mk)
            plan = build_detail_plan(kw, core_kw, mk, mp.lowest or 0, 0, cat)
            lst = {"why": L.why, "must": L.must_words, "gaps": L.gap_words,
                   "titles": L.titles, "detail": L.detail_lines,
                   "bundles": L.bundle_ideas, "sample": mk.sample,
                   "polished": [{"text": t.text, "kind": t.kind,
                                 "length": t.length, "warns": t.warns}
                                for t in pol],
                   "plan": [{"head": h, "body": b} for h, b in plan.lines],
                   "photos": plan.photos, "plan_note": plan.note}
        except Exception:  # noqa: BLE001
            lst = {}
        finds.append(Find(
            keyword=kw, axis=axis, category=cat, total=m.total,
            price_min=mp.lowest or 0, comp=comp,
            score=_score(m.total, mp.lowest or 0, comp),
            grade=grade_of(m.total, open_total),
            title=(lst.get("titles") or [_title_of(kw, core_kw)])[0],
            listing=lst, core=core_kw,
            spread={"found": spd.found, "low": spd.low, "high": spd.high,
                    "spread_pct": spd.spread_pct, "sellers": spd.sellers,
                    "note": spd.note, "hint": spd.hint},
            links=market_links(kw),
            naver=naver_link(kw), coupang=coupang_link(kw)))

    # ── 3단계: 상위만 최저가 (비싼 호출) ────────────────────────
    finds.sort(key=lambda f: -f.score)
    top = finds[:_TOP_PRICE_CHECK]
    if progress_cb:
        progress_cb(used, budget, f"상위 {len(top)}개 최저가 확인 중")
    # 기준가(대표 가격대)를 넘겨야 '너무 싼 건 본품이 아니다' 를 판별한다.
    # 2단계로 바꾸면서 이걸 안 넘겨 부속품이 최저가로 잡혔다.
    lows = await _gather([shop.naver_lowest(f.keyword, core=f.core,
                                            reference=f.price_min)
                          for f in top]) if hasattr(shop, "naver_lowest") else []
    used += len(top)
    keep: list[Find] = []
    for f, nl in zip(top, lows or []):
        if isinstance(nl, Exception) or not nl or nl.get("price", 0) <= 0:
            rep.nolow += 1
            continue
        f.naver_low = nl.get("price", 0)
        f.naver_low_title = nl.get("title", "")
        f.naver_low_skipped = nl.get("skipped", 0)
        f.naver_low_other = nl.get("skipped_other", 0)
        f.naver_low_link = nl.get("link", "")
        f.price_min = f.naver_low
        f.same_product = bool(nl.get("ptype") in (1, 3))
        f.need_cost = mode_cost(f.price_min, rule,
                                fee_pct=fee_pct,
                                target_margin=target_margin)
        f.mode = rule.key
        f.target_margin = (target_margin if target_margin
                           else target_of(f.price_min, rule))
        if f.need_cost <= 0:
            rep.cheap += 1
            continue
        keep.append(f)
    rep.passed = len(keep)
    finds = keep

    if finds:
        totals = sorted(f.total for f in finds)
        q1 = totals[len(totals) // 4]
        q3 = totals[len(totals) * 3 // 4]
        lo, hi = totals[0], totals[-1]
        span = max(1, hi - lo)
        for f in finds:
            if f.total <= q1:
                f.grade = "이 중 가장 빈 곳"
            elif f.total <= q3:
                f.grade = "보통"
            else:
                f.grade = "붐비는 편"
            empt = 1.0 - (f.total - lo) / span
            # 마진 여지 = 최저가 대비 얼마나 싸게 떼야 하나 (여유가 클수록 좋음)
            r2 = 0.0
            if f.price_min > 0 and f.need_cost > 0:
                r2 = max(0.0, min(1.0, (f.price_min - f.need_cost) / f.price_min))
            enter = 0.2
            if f.comp is not None:
                enter = max(0.2, (f.comp.indie_pct or 0) / 100)
            w_empt, w_margin, w_enter = score_weights(rule)
            f.score = round(empt * w_empt + r2 * w_margin + enter * w_enter, 1)
            f.mode_note = mode_advice(rule, f.price_min, f.need_cost)
        for i, f in enumerate(finds):
            f.reasons, f.doubts = explain(f, i + 1, rep.looked)
            # 떼도 되는 물건인가 — 경쟁·마진과 별개로 본다
            qa = assess_quality(f.keyword, f.category, f.price_min,
                                f.need_cost, rule.key, f.comp, f.spread)
            # 이미 받아둔 데이터에서 더 캔다 — 브랜드 장악도·제목 획일성·가격 폭
            mkt = markets.get(f.keyword)
            if mkt is not None and not qa.blocked:
                qa = deep_quality(qa, mkt, rule.key)
            f.quality = {"score": qa.score, "grade": qa.grade,
                         "good": qa.good, "risks": qa.risks,
                         "blocked": qa.blocked}
            f.suppliers = supplier_links(f.core or f.keyword, rule.key)
        # 인증 없으면 못 파는 건 아예 뺀다 (추천했다가 물리면 안 된다)
        blocked = [f for f in finds if f.quality.get("blocked")]
        if blocked:
            finds = [f for f in finds if not f.quality.get("blocked")]
            rep.passed = len(finds)
    finds.sort(key=lambda f: -f.score)
    return finds, rep

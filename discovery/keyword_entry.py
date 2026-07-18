"""
discovery.keyword_entry
======================
진입 키워드 발굴 + 제목 후보 생성 (차별화 도구).

[핵심] 같은 상품이라도 '어떤 키워드로 파느냐'에 따라 경쟁이 다르다.
오너클랜 상품의 키워드 전부를 네이버에 조회해 '경쟁 적은 진입로'를 찾고,
그 키워드로 제목 후보를 만든다.

[정직한 한계]
- 상위노출을 '보장'하지 못한다. 노출은 판매량·리뷰·클릭률·광고가 좌우.
  이 모듈이 하는 건 '상위노출이 가능한 작은 시장(경쟁 적은 키워드)'을 찾는 것.
- 네이버 인기검색어 랭킹은 공식 API가 없어 못 쓴다(크롤링 금지).
- 쿠팡은 공개 검색 API가 없어 자동 집계 불가 → 확인 링크만 제공.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote

logger = logging.getLogger(__name__)

# 제목에서 빼야 할 것들 (셀러명·과장·중복 유발)
_NOISE = re.compile(
    r"(무료배송|당일발송|정품|최저가|인기|추천|BEST|best|신상|초특가|할인|이벤트)")
_BRACKET = re.compile(r"[\[\]\(\)【】<>]")
_MULTI_SPACE = re.compile(r"\s+")


@dataclass(slots=True)
class KeywordEntry:
    """한 키워드의 진입 가능성."""
    keyword: str
    total: int = 0              # 네이버 등록 수 = 중복도(이미 몇 개 올라왔나)
    price_min: int = 0
    price_mean: int = 0
    found: bool = False
    entry_grade: str = ""       # 진입쉬움/보통/어려움/불가
    note: str = ""

    @property
    def is_entry(self) -> bool:
        return self.entry_grade in ("진입쉬움", "보통")


@dataclass(slots=True)
class TitlePlan:
    """상품 하나의 진입 계획: 최적 키워드 + 제목 후보."""
    product_name: str = ""
    entries: list[KeywordEntry] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    best: KeywordEntry | None = None


def grade_entry(total: int, easy_max: int = 1000,
                normal_max: int = 5000, hard_max: int = 30_000) -> tuple[str, str]:
    """등록 수(중복도)로 진입 난이도 판정."""
    if total <= 0:
        return "불가", "검색 결과 없음 — 수요 미확인"
    if total <= easy_max:
        return "진입쉬움", f"이미 {total:,}개만 등록 — 비어 있음"
    if total <= normal_max:
        return "보통", f"{total:,}개 등록 — 비집고 들어갈 여지"
    if total <= hard_max:
        return "어려움", f"{total:,}개 등록 — 광고 없이는 힘듦"
    return "불가", f"{total:,}개 등록 — 대형 레드오션"


def keywords_of(product) -> list[str]:
    """오너클랜 상품의 키워드 전부 (대표 + 세부). 중복 제거."""
    out: list[str] = []
    seen = set()
    raw = getattr(product, "keyword", "") or ""
    for k in raw.split(","):
        k = k.strip()
        if len(k) >= 2 and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _core_tokens(name: str, limit: int = 4) -> list[str]:
    """상품명에서 제목에 쓸 핵심 토큰 추출 (노이즈·괄호 제거)."""
    s = _BRACKET.sub(" ", name)
    s = _NOISE.sub(" ", s)
    s = _MULTI_SPACE.sub(" ", s).strip()
    toks = [t for t in s.split() if len(t) >= 2]
    out, seen = [], set()
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def build_titles(product, entry: KeywordEntry, max_len: int = 50) -> list[str]:
    """
    진입 키워드를 앞세운 제목 후보 생성.
    규칙: 진입키워드 앞 배치 → 핵심 속성 → 수량/용량. 중복 단어 제거, 50자 내외.
    """
    kw = entry.keyword
    tokens = [t for t in _core_tokens(product.name)
              if t not in kw]  # 키워드와 중복되는 토큰 제외
    origin = (getattr(product, "origin", "") or "").strip()
    titles: list[str] = []

    def _mk(parts: list[str]) -> str:
        s = _MULTI_SPACE.sub(" ", " ".join(p for p in parts if p)).strip()
        return s[:max_len]

    # 후보1: 진입키워드 + 핵심 2토큰
    titles.append(_mk([kw] + tokens[:2]))
    # 후보2: 진입키워드 + 핵심 3토큰 (더 구체)
    if len(tokens) >= 3:
        titles.append(_mk([kw] + tokens[:3]))
    # 후보3: 진입키워드 + 원산지(국내산일 때만 신뢰 요소)
    if "국내" in origin or "국산" in origin:
        titles.append(_mk([kw, "국내산"] + tokens[:2]))

    # 중복 제거 + 너무 짧은 것 제외
    out, seen = [], set()
    for t in titles:
        if len(t) >= 6 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def naver_link(keyword: str, lowest_first: bool = True) -> str:
    """네이버 쇼핑 검색 링크 (최저가순)."""
    sort = "&sort=price_asc" if lowest_first else ""
    return f"https://search.shopping.naver.com/search/all?query={quote(keyword)}{sort}"


def coupang_link(keyword: str) -> str:
    """
    쿠팡 검색 링크 — 공개 검색 API가 없어 자동 집계 불가.
    사용자가 직접 눈으로 중복/가격을 확인하는 용도 (크롤링 아님).
    """
    return f"https://www.coupang.com/np/search?q={quote(keyword)}"


async def analyze_entry_keywords(product, shop, max_keywords: int = 8,
                                 progress_cb=None) -> TitlePlan:
    """
    상품 하나의 키워드 전부를 네이버에 조회해 진입로를 찾는다.
    각 키워드의 등록 수 = 중복도(이미 몇 명이 올렸나).
    """
    plan = TitlePlan(product_name=product.name)
    kws = keywords_of(product)[:max_keywords]
    if not kws:
        # 키워드 컬럼이 비면 상품명 핵심 토큰으로 대체
        toks = _core_tokens(product.name, limit=3)
        kws = [" ".join(toks[:2])] if toks else []

    for i, kw in enumerate(kws):
        try:
            market = await shop.market_of(kw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("키워드 조회 실패 (%s): %s", kw, exc)
            if progress_cb:
                progress_cb(i + 1, len(kws), f"조회 실패: {kw}")
            continue
        lp = sorted(market.lprices) if market.lprices else []
        grade, note = grade_entry(market.total)
        plan.entries.append(KeywordEntry(
            keyword=kw, total=market.total,
            price_min=lp[0] if lp else 0,
            price_mean=int(market.price_mean or 0),
            found=market.total > 0, entry_grade=grade, note=note))
        if progress_cb:
            progress_cb(i + 1, len(kws), f"{kw}: {market.total:,}개 ({grade})")

    # 진입 가능한 것 중 경쟁 적은 순
    plan.entries.sort(key=lambda e: (e.total if e.total > 0 else 9_999_999))
    entries_ok = [e for e in plan.entries if e.is_entry]
    if entries_ok:
        plan.best = entries_ok[0]
        plan.titles = build_titles(product, plan.best)
    return plan

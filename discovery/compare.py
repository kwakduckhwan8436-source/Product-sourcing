"""
discovery.compare
================
최저가 비교 — 다른 판매처·다른 마켓과 견줘본다.

[자동으로 되는 것 — 네이버 가격비교]
가격비교 카탈로그 상품(productType 1)은 네이버가 이미 '그 상품을 파는 여러
판매처' 를 모아 최저가(lprice)와 최고가(hprice)를 계산해 뒀다.
그 폭이 곧 다중 판매처 비교다. 추가 호출 0회.
  - 폭이 좁다  = 다들 비슷하게 판다 → 가격으로 이기기 어렵다
  - 폭이 넓다  = 비싸게 파는 곳이 있다 → 내가 중간에 들어갈 자리가 있다

[자동으로 안 되는 것 — 정직하게]
쿠팡·11번가·G마켓·다나와는 공개 검색 API 가 없다. 긁으면 계정이 막힌다.
그래서 '한 번에 열어보는 링크' 만 만든다. 비교는 사용자가 눈으로 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

_CATALOG = 1


@dataclass(slots=True)
class PriceSpread:
    low: int = 0            # 판매처 중 최저
    high: int = 0           # 판매처 중 최고
    spread_pct: float = 0.0  # 최고가가 최저가보다 몇 % 비싼가
    sellers: int = 0        # 근거가 된 가격비교 상품 수
    found: bool = False
    note: str = ""
    hint: str = ""


def price_spread_of(market, core: str = "") -> PriceSpread:
    """네이버 가격비교 기준: 같은 상품을 파는 곳들의 가격 폭."""
    sp = PriceSpread()
    items = list(getattr(market, "items", None) or [])
    if not items:
        sp.note = "상품 정보가 없어요"
        return sp

    key = (core or getattr(market, "keyword", "") or "").replace(" ", "")
    rows = [it for it in items
            if it.get("ptype") == _CATALOG
            and it.get("lprice", 0) > 0
            and (not key or key in str(it.get("title", "")).replace(" ", ""))]
    if not rows:
        sp.note = ("네이버 가격비교에 안 묶인 시장이에요 — "
                   "판매처별 비교를 네이버가 해두지 않았어요")
        sp.hint = "개인 셀러 위주라 오히려 들어갈 자리는 넓을 수 있어요"
        return sp

    lows = [it["lprice"] for it in rows]
    highs = [it.get("hprice") or it["lprice"] for it in rows]
    sp.low = min(lows)
    sp.high = max(highs)
    sp.sellers = len(rows)
    sp.found = True
    if sp.low > 0:
        sp.spread_pct = round((sp.high - sp.low) / sp.low * 100, 1)

    if sp.spread_pct <= 10:
        sp.note = (f"파는 곳마다 값이 거의 같아요 "
                   f"({sp.low:,}~{sp.high:,}원, 차이 {sp.spread_pct:.0f}%)")
        sp.hint = "가격으로는 못 이겨요 — 묶음·상세페이지로 차별화해야 합니다"
    elif sp.spread_pct <= 40:
        sp.note = (f"파는 곳마다 {sp.spread_pct:.0f}% 차이가 나요 "
                   f"({sp.low:,}~{sp.high:,}원)")
        sp.hint = "중간 가격대로 들어갈 자리가 있어요"
    else:
        sp.note = (f"값이 크게 벌어져 있어요 — {sp.low:,}원부터 {sp.high:,}원까지 "
                   f"({sp.spread_pct:.0f}% 차이)")
        sp.hint = "비싸게 파는 곳이 많다는 뜻 — 파고들 여지가 큽니다"
    return sp


def market_links(keyword: str) -> list:
    """
    다른 마켓에서 같은 이름으로 검색해 보는 링크.
    (공개 API 가 없어 자동 비교는 못 한다 — 눈으로 보시라고 문만 열어준다)
    """
    q = quote(keyword)
    return [
        {"name": "네이버 가격비교",
         "url": f"https://search.shopping.naver.com/search/all?query={q}&sort=price_asc"},
        {"name": "쿠팡", "url": f"https://www.coupang.com/np/search?q={q}"},
        {"name": "11번가",
         "url": f"https://search.11st.co.kr/Search.tmall?kwd={q}"},
        {"name": "G마켓",
         "url": f"https://browse.gmarket.co.kr/search?keyword={q}"},
        {"name": "다나와",
         "url": f"https://search.danawa.com/dsearch.php?k1={q}"},
    ]

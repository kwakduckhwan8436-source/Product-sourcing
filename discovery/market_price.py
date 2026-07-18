"""
discovery.market_price
=====================
시장 최저가 — 네이버가 이미 계산해 준 값을 그대로 쓴다.

[왜 이렇게 바꿨나 — 그동안의 삽질]
가격순(asc)으로 목록을 긁어와 "이건 부속품일 것이다 / 중고일 것이다 / 품절일
것이다" 를 추측으로 걸러왔다. 추측 위에 추측을 쌓으니 매번 틀렸다.

그런데 네이버는 이미 답을 주고 있었다.
  productType 1 = 가격비교 '카탈로그' 상품.
  이 상품의 lprice 는 네이버가 그 상품을 파는 여러 판매처를 모아서
  계산해 놓은 '최저가' 다. 우리가 흉내 낼 필요가 없다.

즉 유형 1 상품들의 lprice = 네이버 공식 최저가.
부속품·중고·품절 걱정이 없다. 네이버가 이미 그 상품 단위로 정리해 뒀으니까.

[한계 — 정직하게]
- 카탈로그가 없는 시장(개인 셀러만 있는 롱테일)은 유형 1 이 안 나온다.
  그때는 유형 2(독립 상품) 중 제목이 맞는 것들의 가격 분포를 쓴다.
- 어느 쪽이든 '이 값이 어디서 왔는지'(basis)를 함께 돌려준다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_CATALOG = 1          # 가격비교 카탈로그 = 네이버가 최저가를 계산해 둔 상품
_NEW_TYPES = (1, 2, 3)


@dataclass(slots=True)
class MarketPrice:
    lowest: int = 0           # 대표 상품군의 최저가 (네이버 검색 최저가와 다름!)
    median: int = 0           # 대표 가격대 — 마진 역산의 기준
    basis: str = ""           # 'catalog'(네이버 계산) / 'listing'(개별 매물)
    sample: int = 0           # 근거가 된 상품 수
    note: str = ""
    prices: list = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return self.basis == "catalog" and self.sample >= 2


def _match(title: str, core: str) -> bool:
    if not core:
        return True
    return core.replace(" ", "") in (title or "").replace(" ", "")


def market_price_of(market, core: str = "") -> MarketPrice:
    """
    ShopMarket.items 에서 시장 최저가를 뽑는다.
    1순위: 유형 1(가격비교 카탈로그) — 네이버가 계산한 최저가. 추측 없음.
    2순위: 유형 2·3(개별 매물) 중 제목이 맞는 것.
    """
    items = list(getattr(market, "items", None) or [])
    mp = MarketPrice()
    if not items:
        mp.note = "상품 정보가 없어요"
        return mp

    core = core or getattr(market, "keyword", "") or ""

    # 1순위 — 네이버가 계산해 둔 카탈로그 최저가
    cat = [it for it in items
           if it.get("ptype") == _CATALOG and it.get("lprice", 0) > 0
           and _match(it.get("title", ""), core)]
    if cat:
        prices = sorted(it["lprice"] for it in cat)
        mp.lowest = prices[0]
        mp.median = prices[len(prices) // 2]
        mp.basis = "catalog"
        mp.sample = len(prices)
        mp.prices = prices[:10]
        mp.note = (f"네이버가 여러 판매처를 모아 계산한 최저가예요 "
                   f"(가격비교 상품 {len(prices)}개 기준)")
        return mp

    # 2순위 — 카탈로그가 없는 시장(개인 셀러 위주). 개별 매물로 본다.
    lis = [it for it in items
           if it.get("ptype") in _NEW_TYPES and it.get("lprice", 0) > 0
           and _match(it.get("title", ""), core)]
    if not lis:
        mp.note = "이 이름에 맞는 상품이 없어요 — 검색어가 헛짚었을 수 있어요"
        return mp

    prices = sorted(it["lprice"] for it in lis)
    mp.median = prices[len(prices) // 2]
    # 개별 매물은 품절·미끼가 섞인다. 혼자 뚝 떨어진 값은 안 믿는다.
    for i, p in enumerate(prices):
        near = sum(1 for q in prices[i:] if q <= p * 1.25)
        if near >= 3:
            mp.lowest = p
            break
    if not mp.lowest:
        mp.lowest = mp.median
    mp.basis = "listing"
    mp.sample = len(prices)
    mp.prices = prices[:10]
    mp.note = (f"가격비교 상품이 없어 개별 매물 {len(prices)}개로 봤어요 "
               f"(카탈로그보다 덜 정확)")
    return mp

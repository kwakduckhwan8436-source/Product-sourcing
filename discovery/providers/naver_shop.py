"""
discovery.providers.naver_shop
==============================
검색 API(shop.json) -> ShopMarket 으로 정규화.

발굴에서 검색 API가 직접 주는 핵심:
- total      = 경쟁 강도 (등록 상품 수)
- items.lprice= 가격 분포 -> 변동계수로 '가격 책정 여지' 측정
- category1~4 = 리스크 분류용 카테고리 경로
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any

from discovery.base import ShopMarket
from discovery.providers.naver_client import NaverClient

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

# 네이버 검색 API productType (문서 기준)
#   1~3  = 일반상품 (가격비교 / 비매칭 / 매칭)
#   4~6  = 중고, 7~9 = 단종, 10~12 = 판매예정
# 최저가를 뽑을 때 중고·단종·판매예정이 섞이면 '시장 최저가'가 통째로 틀어진다.
_NEW_TYPES = (1, 2, 3)

# 부속품·소모품 — 본품이 아니다.
# "빨래건조대 커버 990원" 은 제목에 '빨래건조대' 가 들어 있어 제목 검사를
# 통과한다. 그걸 최저가로 쓰면 마진 계산이 통째로 거짓이 된다.
_ACCESSORY = (
    "커버", "고리", "부품", "부속", "케이스", "파우치", "리필", "교체",
    "받침", "스티커", "거치대", "전용홀더", "패드만", "헤드만", "필터",
    "충전기만", "케이블만", "스탠드만", "다리만", "봉만", "망만",
    "여분", "추가구성", "옵션추가", "부자재", "소모품", "샘플",
)
_CLUSTER_MIN = 3       # 비슷한 가격이 이만큼은 있어야 진짜 시장가
_CLUSTER_TOL = 0.25    # ±25% 안이면 '비슷한 가격'


def _clean(text: str) -> str:
    """검색 결과 title 의 <b> 강조 태그/HTML 엔티티 제거."""
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class NaverShopProvider:
    """ShopSearchProvider 구현체."""

    def __init__(self, client: NaverClient):
        self.client = client

    async def market_of(self, keyword: str, sample: int = 100,
                        sort: str = "sim") -> ShopMarket:
        """sort="sim" = 정확도순(상위 노출 구조 파악용).
        진짜 최저가는 lowest_of() 로 따로 조회해야 한다 — sim 상위 100개
        안에 전체 최저가 상품이 없을 수 있기 때문."""
        data = await self.client.search_shop(keyword, display=min(sample, 100),
                                              sort=sort)
        total = _to_int(data.get("total")) or 0
        items = data.get("items") or []

        lprices: list[int] = []
        titles: list[str] = []
        category_path: list[str] = []
        mall_names: list[str] = []
        product_types: list[int] = []
        links: list[str] = []
        keep: list[dict] = []
        for it in items:
            lp = _to_int(it.get("lprice"))
            if lp:
                lprices.append(lp)
            titles.append(_clean(it.get("title", "")))
            # 경쟁 구조 분석용 (이미 응답에 있는 데이터, 추가 호출 0)
            mall = it.get("mallName")
            if mall:
                mall_names.append(str(mall))
            pt = _to_int(it.get("productType"))
            if pt is not None:
                product_types.append(pt)
            lk = it.get("link")
            if lk:
                links.append(str(lk))
            keep.append({"title": _clean(it.get("title", "")),
                         "lprice": lp or 0, "hprice": _to_int(it.get("hprice")) or 0,
                         "ptype": pt, "mall": it.get("mallName", ""),
                         "brand": str(it.get("brand") or ""),
                         "maker": str(it.get("maker") or ""),
                         "link": str(lk or "")})
            # 첫 유효 item 의 카테고리를 시장 대표 경로로
            if not category_path:
                path = [it.get(f"category{i}") for i in range(1, 5)]
                category_path = [c for c in path if c]

        return ShopMarket(
            keyword=keyword,
            total=total,
            lprices=lprices,
            category_path=category_path,
            sample_titles=titles[:100],   # 제목 채굴용 — 받은 건 다 보관
            mall_names=mall_names,
            product_types=product_types,
            links=links,
            items=keep,
            raw={"total": total, "count": len(items)},
        )

    async def naver_lowest(self, keyword: str, core: str | None = None,
                           reference: float | None = None,
                           sample: int = 40) -> dict:
        """
        그 상품의 시장 최저가 — 본품만.

        [실제로 있었던 사고 두 번]
          1) "가정용 트렌치코트" 최저가로 '아이엠베베 케이프 5,000원' → 제목 검사 추가
          2) 상위 30개 중 29개가 '마진 불가' → 알고 보니 '빨래건조대 커버 990원'
             같은 부속품이 최저가로 잡힘. 제목에 핵심어가 들어 있어 검사를
             통과해버린 것. 마진 계산이 통째로 거짓이 됐다.

        [네 겹으로 거른다]
          1) 중고·단종·판매예정 제외 (웹은 중고를 별도 탭으로 숨긴다)
          2) 제목에 핵심어가 있어야 함
          3) 부속품 말(커버·고리·리필…)이 있으면 본품이 아니다
          4) 기준가의 35% 미만이면 본품일 리 없다 (reference 를 주면)
        """
        data = await self.client.search_shop(keyword, display=min(sample, 100),
                                             sort="asc")
        items = data.get("items") or []
        core_key = (core or keyword).replace(" ", "")
        skipped_used = skipped_other = skipped_acc = skipped_low = 0
        for it in items:
            pt = _to_int(it.get("productType"))
            if pt is not None and pt not in _NEW_TYPES:
                skipped_used += 1
                continue
            lp = _to_int(it.get("lprice"))
            if not lp or lp <= 0:
                continue
            title = _clean(it.get("title", ""))
            flat = title.replace(" ", "")
            if core_key and core_key not in flat:
                skipped_other += 1
                continue
            if any(a in flat for a in _ACCESSORY):
                skipped_acc += 1          # 커버·고리 같은 부속품
                continue
            if reference and reference > 0 and lp < reference * 0.35:
                skipped_low += 1          # 본품이라기엔 너무 쌈
                continue
            return {"price": lp, "title": title,
                    "link": str(it.get("link") or ""), "ptype": pt or 0,
                    "skipped": skipped_used, "skipped_other": skipped_other,
                    "skipped_acc": skipped_acc, "skipped_low": skipped_low,
                    "note": ""}
        return {"price": 0, "title": "", "link": "", "ptype": 0,
                "skipped": skipped_used, "skipped_other": skipped_other,
                "skipped_acc": skipped_acc, "skipped_low": skipped_low,
                "note": f"'{core_key}' 본품을 못 찾았어요"}

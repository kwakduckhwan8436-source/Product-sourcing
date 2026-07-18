"""
discovery.blue_ocean
===================
블루오션 발굴 엔진.

[정의] 블루오션 = 수요는 있는데(사람들이 찾는데) 파는 사람은 적은(등록 상품
적은) 시장. 케이블정리함(108만 건)의 정반대.

[왜 기존 발굴은 블루오션을 못 찾나]
유명 키워드("수납박스")로 검색하니 당연히 수백만 개가 등록돼 있다.
블루오션은 정의상 '아직 유명하지 않은' 좁은 키워드에 숨어 있다.

[3단계 전략]
1. 롱테일 생성: 유명 씨앗 제목에서 '수식어가 붙어 좁아진' 키워드를 캐낸다.
   "수납박스" → "차량용 트렁크 정리함", "방수 캠핑 수납함" ...
   (수식어 = 용도/소재/장소/대상. 좁을수록 경쟁이 적다)
2. 레드오션 차단: 검색해서 등록 상품 수가 상한 이상이면 버린다.
3. 블루오션 판정: 등록 적고(블루) + 그래도 수요 신호 있는(오션) 것만.

[비용] 롱테일 검색이 늘지만 검색 API 는 하루 25,000회라 여유.
데이터랩은 최종 후보에만.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from discovery.base import ShopMarket

# 롱테일을 만드는 '수식어' — 용도/소재/장소/대상/형태.
# 이런 수식어가 붙은 키워드는 좁고 구체적이라 경쟁이 적다 = 블루오션 후보.
_MODIFIER_HINTS = (
    # 용도/장소
    "차량용", "캠핑", "주방", "욕실", "사무실", "여행용", "휴대용", "야외",
    "현관", "베란다", "냉장고", "책상", "침대", "벽걸이", "문걸이",
    # 소재/형태
    "실리콘", "스테인리스", "방수", "접이식", "투명", "대용량", "미니",
    "원목", "철제", "자석", "흡착", "걸이형", "이중", "회전",
    # 대상
    "반려동물", "강아지", "고양이", "유아", "노인", "1인", "캠퍼",
)
_TOKEN_RE = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z0-9]+")


@dataclass(slots=True)
class BlueOceanConfig:
    redocean_max_total: int = 100_000   # 등록 이 이상이면 레드오션(차단)
    blue_ideal_total: int = 20_000      # 이 이하면 경쟁 빈 정도 만점
    min_total: int = 50                 # (구) 너무 적으면 죽은 시장
    max_longtail: int = 12              # 씨앗당 생성할 롱테일 수
    # --- 새 블루오션 정의: 검색되고 + 셀러 적당하고 + 가격 버티는 ---
    demand_floor: float = 15.0          # 데이터랩 검색 수준 이 이상이어야 '수요 있음'
    healthy_min_total: int = 300        # 셀러가 이 정도는 있어야 '죽은 시장 아님'
    margin_floor_pct: float = 15.0      # 추정 마진율 이 이상이어야 통과
    cost_ratio: float = 0.35            # 원가 통념: 판매가의 35% (키 없을 때)


@dataclass(slots=True)
class BlueOceanScore:
    keyword: str
    total: int = 0
    is_blue: bool = False
    competition_openness: float = 0.0   # 0~1, 등록 적을수록 높음
    demand_ok: bool = False             # 검색 수요 있음
    healthy_competition: bool = False   # 셀러 적당(죽은 시장 아님)
    price_holding: bool = True          # 가격 안 무너짐
    est_margin_pct: float | None = None # 추정 마진율(%)
    margin_ok: bool = False
    note: str = ""


def estimate_margin(market: ShopMarket,
                    cfg: BlueOceanConfig | None = None) -> float | None:
    """네이버 판매가 + 원가 통념으로 마진율 추정 (키 없이 1차 필터)."""
    cfg = cfg or BlueOceanConfig()
    price = market.price_mean
    if not price or price <= 0:
        return None
    est_cost = price * cfg.cost_ratio + 3000 + price * 0.25
    return round((price - est_cost) / price * 100, 1)


def generate_longtails(market: ShopMarket, seed_keyword: str,
                       cfg: BlueOceanConfig | None = None) -> list[str]:
    """
    유명 씨앗의 상품 제목에서 '수식어 + 핵심어' 형태의 롱테일을 생성.
    제목에 실제로 등장한 수식어만 사용 (가공의 조합 방지 = 무가정).
    """
    cfg = cfg or BlueOceanConfig()
    titles = market.sample_titles or []
    if not titles:
        return []

    # 씨앗 자체가 셀러/브랜드명이면 롱테일 생성 안 함 (브랜드는 제품이 아님)
    seed_norm = seed_keyword.lower().replace(" ", "")
    for mall in (market.mall_names or []):
        m = mall.lower().replace(" ", "")
        if m and (m == seed_norm or m in seed_norm or seed_norm in m):
            return []   # 씨앗이 판매처명 → 제품 키워드가 아니므로 버림

    seed_core = seed_keyword.replace(" ", "")
    # 제목에서 실제 등장한 수식어 수집 (빈도순)
    found_mods = Counter()
    for title in titles:
        low = title.lower()
        for mod in _MODIFIER_HINTS:
            if mod in low:
                found_mods[mod] += 1

    # 수식어 + 씨앗 핵심어 결합 → 롱테일 (실제 제목에 근거한 것만)
    longtails = []
    for mod, _ in found_mods.most_common(cfg.max_longtail):
        # "차량용" + "수납박스" → "차량용 수납박스"
        lt = f"{mod} {seed_keyword}"
        longtails.append(lt)
    return longtails


def evaluate_blue_ocean(market: ShopMarket, demand=None,
                        price_holding: bool = True,
                        cfg: BlueOceanConfig | None = None) -> BlueOceanScore:
    """
    새 블루오션 정의: 검색되고(수요) + 셀러 적당하고(틈) + 가격 버티고(진짜)
    + 마진 나는 가격구조. 하나라도 무너지면 is_blue=False (가짜 블루오션 거름).
    """
    cfg = cfg or BlueOceanConfig()
    total = market.total
    out = BlueOceanScore(keyword=market.keyword, total=total,
                         price_holding=price_holding)

    # 1) 레드오션 차단
    if total >= cfg.redocean_max_total:
        out.note = f"레드오션 (등록 {total:,}건)"
        return out
    # 2) 죽은 시장 차단 (셀러 너무 적음 = 수요 미검증)
    if total < cfg.healthy_min_total:
        out.note = (f"죽은 시장 의심 (등록 {total:,}건 < {cfg.healthy_min_total}) "
                    f"— 셀러가 적어 수요 미검증")
        return out
    out.healthy_competition = True

    # 3) 검색 수요 (데이터랩 절대 수준)
    if demand is not None and demand.has_data:
        out.demand_ok = demand.level >= cfg.demand_floor
        if not out.demand_ok:
            out.note = (f"수요 약함 (검색수준 {demand.level:.0f} < {cfg.demand_floor:.0f}) "
                        f"— 등록 적지만 찾는 사람 적음")
            return out

    # 4) 가격 유지력 (이력 기반)
    if not price_holding:
        out.note = "가격 하락 중 — 공급이 수요 추월(헛 블루오션)"
        return out

    # 5) 마진 1차 필터
    out.est_margin_pct = estimate_margin(market, cfg)
    if out.est_margin_pct is not None:
        out.margin_ok = out.est_margin_pct >= cfg.margin_floor_pct
        if not out.margin_ok:
            out.note = (f"마진 부족 (추정 {out.est_margin_pct:.0f}% < "
                        f"{cfg.margin_floor_pct:.0f}%) — 팔려도 안 남음")
            return out

    # 경쟁 빈 정도 (표시용)
    span = max(1, cfg.redocean_max_total - cfg.blue_ideal_total)
    out.competition_openness = (1.0 if total <= cfg.blue_ideal_total
                                else max(0.0, 1.0 - (total - cfg.blue_ideal_total) / span))

    out.is_blue = out.healthy_competition and out.price_holding and (
        out.margin_ok or out.est_margin_pct is None)
    demand_txt = "수요✓" if out.demand_ok else "수요미확인"
    margin_txt = (f"마진~{out.est_margin_pct:.0f}%" if out.est_margin_pct is not None
                  else "마진미상")
    out.note = (f"블루오션 ({demand_txt}, 등록 {total:,}건, 가격유지✓, {margin_txt})")
    return out

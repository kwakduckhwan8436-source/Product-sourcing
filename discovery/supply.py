"""
discovery.supply
================
공급 안정성 점수 (네이버 proxy 버전).

[목적] "떼온 뒤 공급이 끊길 위험"을 떼기 전에 예측.
공급 끊김은 가장 늦게·가장 크게 터지는 사고. 처음부터 안 끊길 걸 고른다.

[네이버 proxy 신호 — 추가 API 호출 0, 이미 수집된 데이터 재활용]
1. 셀러 다양성 (unique_seller_ratio): 같은 상품을 파는 셀러가 많을수록
   공급이 분산됨 = 한 곳 끊겨도 대체 가능 = 안전. 가장 강한 신호.
2. 시즌성 위험 (DemandTrend.volatility): 추세 변동이 클수록 유행/시즌
   상품 = 단종 위험. 변동 작고 꾸준하면 사철 상품 = 안전.
3. 범용성 (카테고리): 생활필수품류는 단종 거의 없음. 시즌/유행/캐릭터
   카테고리는 공급 끊김 위험.
4. 시장 두께 (total): 너무 적으면(틈새지만) 공급처도 적어 끊김 위험.
   적정 이상이어야 공급 생태계가 받쳐줌.

[주의] proxy 는 알리/도매 실제 공급처가 아닌 '국내 시장의 대리 신호'.
키 확보 후 AliExpress 공급처 수로 강화 예정. 지금은 80% 방어가 목표.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from discovery.base import DemandTrend, ShopMarket


class SupplyGrade(str, Enum):
    HIGH = "안정"      # 공급 끊김 위험 낮음
    MEDIUM = "보통"
    LOW = "주의"       # 공급 끊김 위험 높음
    UNKNOWN = "미상"   # 데이터 부족


# 범용성 높은(잘 안 끊기는) 카테고리 키워드 -> 가점
_STABLE_CATEGORY = ("생활", "주방", "욕실", "수납", "생필품", "세제",
                    "위생", "정리", "청소", "건강")
# 시즌/유행성 높은(잘 끊기는) 카테고리 키워드 -> 감점
_VOLATILE_CATEGORY = ("패션", "의류", "트렌드", "시즌", "캐릭터", "한정",
                     "파티", "행사", "코스프레")


@dataclass(slots=True)
class SupplyStability:
    keyword: str
    score: float = 0.0            # 0~1, 높을수록 안정
    grade: SupplyGrade = SupplyGrade.UNKNOWN
    seller_diversity: float | None = None   # 셀러 다양성 신호
    season_risk: float | None = None        # 시즌성 위험(0 안전 ~ 1 위험)
    category_factor: float = 1.0            # 범용성 보정
    rationale: str = ""


def _category_factor(category_path: list[str]) -> float:
    """범용성: 생활필수품류 가점(>1), 시즌/유행류 감점(<1)."""
    joined = " ".join(category_path)
    factor = 1.0
    if any(t in joined for t in _STABLE_CATEGORY):
        factor *= 1.15
    if any(t in joined for t in _VOLATILE_CATEGORY):
        factor *= 0.75
    return factor


def score_supply_stability(market: ShopMarket, demand: DemandTrend | None,
                           total_floor: int = 300,
                           volatility_cap: float = 30.0) -> SupplyStability:
    """
    공급 안정성 0~1 점수 + 등급. 데이터 부족하면 UNKNOWN.
    """
    out = SupplyStability(keyword=market.keyword)

    # 1. 셀러 다양성 (가장 강한 신호). 없으면 안정성 판단 불가 -> UNKNOWN
    diversity = market.unique_seller_ratio
    if diversity is None:
        out.rationale = "셀러 데이터 없음 — 공급 안정성 판단 불가"
        return out
    out.seller_diversity = diversity

    # 2. 시즌성 위험: 추세 변동성 클수록 위험. 데이터 없으면 중립(0.3 가정).
    if demand is not None and demand.has_data:
        out.season_risk = min(1.0, demand.volatility / volatility_cap)
    else:
        out.season_risk = 0.3  # 수요 데이터 없으면 중립값

    # 3. 범용성
    out.category_factor = _category_factor(market.category_path)

    # 4. 시장 두께: 너무 얇으면 공급 생태계 부족
    thickness = 1.0 if market.total >= total_floor else market.total / total_floor

    # === 결합 ===
    # 다양성(주력) × (1 - 시즌위험) × 범용성 × 시장두께
    raw = diversity * (1.0 - 0.5 * out.season_risk) * thickness
    raw *= out.category_factor
    out.score = max(0.0, min(1.0, raw))

    # 등급
    if out.score >= 0.6:
        out.grade = SupplyGrade.HIGH
    elif out.score >= 0.35:
        out.grade = SupplyGrade.MEDIUM
    else:
        out.grade = SupplyGrade.LOW

    out.rationale = (
        f"셀러다양성 {diversity:.0%}, "
        f"시즌위험 {out.season_risk:.0%}, "
        f"범용성 {out.category_factor:.2f}배, "
        f"시장두께 {thickness:.0%} → 공급 {out.grade.value}"
    )
    return out

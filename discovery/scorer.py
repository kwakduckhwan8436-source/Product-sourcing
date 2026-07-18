"""
discovery.scorer
================
발굴 점수 엔진. 곱셈 핵심 + 리스크 게이트 + 안정형/선점형 두 점수.

공식:
  핵심틈새도 = 수요성분 × 경쟁희소성 × 가격여지
             ※ 곱셈 -> '둘 다 좋아야' 폭발. 한쪽이 0이면 전체 0.
  발굴점수   = 핵심틈새도 × 리스크게이트

  안정형 수요성분 = demand_level (꾸준한 수준) - 변동성 페널티
  선점형 수요성분 = demand_slope (상승 기울기)

발굴 단계는 컷오프 없이 랭킹만. 거르기는 다음 단계(cost_mapper 마진)에서.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from discovery.base import (DemandTrend, DiscoveryScore, ListType, ShopMarket)


# 인증/반품 리스크 키워드 -> 게이트 계수 (1.0 안전 ~ 낮을수록 진입장벽)
_RISK_RULES: list[tuple[float, tuple[str, ...]]] = [
    (0.35, ("화장품", "뷰티", "스킨", "건강식품", "영양제", "의약", "의료기기")),
    (0.45, ("식품", "음료", "다이어트", "유아", "분유", "기저귀")),
    (0.65, ("전자", "전기", "충전", "배터리", "가전")),
    (0.80, ("의류", "패션", "신발", "속옷")),  # 사이즈 반품 리스크
]


@dataclass(slots=True)
class ScorerConfig:
    # 경쟁 정규화 기준: log10(total) 가 이 범위일 때 0~1 매핑
    log_total_min: float = 2.0    # 100건 -> 매우 희소(고득점)
    log_total_max: float = 6.0    # 100만건 -> 레드오션(저득점)
    # 가격 변동계수 정규화 상한 (이상이면 1.0)
    price_cv_cap: float = 0.8
    # 수요 정규화
    level_cap: float = 60.0       # 데이터랩 level 이 이 이상이면 1.0
    slope_cap: float = 8.0        # 월간 기울기 이 이상이면 1.0
    volatility_cap: float = 30.0  # 안정형 변동성 페널티 기준
    # 곱셈 성분 가중(지수). 1.0=동일 영향. 높이면 그 축을 더 깐깐하게.
    w_demand: float = 1.0
    w_competition: float = 1.0
    w_price: float = 0.6          # 가격여지는 보조 신호라 영향 약하게
    # --- 경쟁 구조 정밀 보정 (1순위 고도화) ---
    # 셀러 독점/묶음 신호로 기존 경쟁희소성을 보정하는 강도. 0이면 보정 없음(기존과 동일).
    competition_refine_weight: float = 0.4  # 0~1, 보정 신호의 영향력
    bundle_redocean_threshold: float = 0.6  # 묶음비율 이 이상이면 레드오션 신호
    # --- 수요-경쟁 불균형 보너스 ---
    # 수요와 경쟁희소성이 둘 다 이 기준 이상이면 "진짜 틈새"로 보고 가점.
    # (찾는 사람 많은데 파는 사람 적은 황금 지점). 한쪽만 높으면 보너스 없음.
    imbalance_threshold: float = 0.45   # 둘 다 이 값 이상이어야 보너스
    imbalance_bonus: float = 0.6        # 보너스 강도 (둘 다 높을수록 최대 +이만큼)
    # --- 점수 펼치기 (0~100 변별) ---
    # 곱셈 결과(0~1)는 0.0x대로 깔려 변별이 안 됨. 제곱근으로 펼쳐 0~100 으로.
    score_scale_power: float = 0.5      # 0.5=제곱근. 작을수록 더 펼침.


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _norm(x: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return _clamp01((x - lo) / (hi - lo))


def risk_factor(category_path: list[str]) -> float:
    """카테고리 경로에서 가장 강한 리스크 게이트를 적용."""
    joined = " ".join(category_path)
    factor = 1.0
    for coef, terms in _RISK_RULES:
        if any(t in joined for t in terms):
            factor = min(factor, coef)
    return factor


def score_keyword(market: ShopMarket, demand: DemandTrend,
                  cfg: ScorerConfig | None = None) -> DiscoveryScore:
    cfg = cfg or ScorerConfig()

    # --- 경쟁 희소성: total 적을수록 1 ---
    log_total = math.log10(market.total) if market.total > 0 else cfg.log_total_min
    competition = 1.0 - _norm(log_total, cfg.log_total_min, cfg.log_total_max)

    # --- 경쟁 구조 정밀 보정 (1순위 고도화) ---
    # 셀러 데이터가 있을 때만 보정. 없으면 competition 그대로(하위 호환).
    seller_conc = market.seller_concentration
    uniq_ratio = market.unique_seller_ratio
    bundle = market.bundle_ratio
    competition_refined = competition
    if seller_conc is not None or bundle is not None:
        # 보정 계수: 분산될수록(고유셀러비↑, 독점도↓, 묶음↓) 1보다 커지고,
        # 독점/레드오션일수록 1보다 작아진다. 최종 0.5~1.3 범위로 제한.
        adj = 1.0
        if uniq_ratio is not None:
            # 고유셀러비 0.5 기준: 높으면 가점, 낮으면 감점
            adj *= (1.0 + cfg.competition_refine_weight * (uniq_ratio - 0.5))
        if bundle is not None and bundle >= cfg.bundle_redocean_threshold:
            # 묶음 과다 = 정착된 레드오션 -> 감점
            adj *= (1.0 - cfg.competition_refine_weight * (bundle - cfg.bundle_redocean_threshold))
        adj = max(0.5, min(1.3, adj))
        competition_refined = _clamp01(competition * adj)

    # --- 가격 여지: 변동계수 클수록 1 ---
    cv = market.price_cv or 0.0
    price_room = _norm(cv, 0.0, cfg.price_cv_cap)

    # --- 수요 두 성분 ---
    level_n = _norm(demand.level, 0.0, cfg.level_cap) if demand.has_data else 0.0
    slope_n = _norm(demand.slope, 0.0, cfg.slope_cap) if demand.has_data else 0.0
    vol_penalty = _norm(demand.volatility, 0.0, cfg.volatility_cap) if demand.has_data else 0.0

    # 안정형 수요 = 수준 높되 변동성 큰 건 깎음
    stable_demand = _clamp01(level_n * (1.0 - 0.4 * vol_penalty))
    emerging_demand = slope_n

    # --- 리스크 게이트 ---
    rf = risk_factor(market.category_path)

    # --- 곱셈 결합 (가중 지수) + 불균형 보너스 + 펼치기 ---
    def combine(demand_comp: float) -> float:
        # 0 방지용 epsilon 후 가중 곱. 경쟁은 보정된 값 사용.
        d = max(demand_comp, 1e-6) ** cfg.w_demand
        c = max(competition_refined, 1e-6) ** cfg.w_competition
        p = max(price_room, 1e-6) ** cfg.w_price
        base = _clamp01(d * c * p) * rf

        # 불균형 보너스: 수요와 경쟁희소성이 '둘 다' 기준 이상일 때만.
        # 찾는 사람 많은데(수요↑) 파는 사람 적은(경쟁희소성↑) 황금 지점.
        # 한쪽만 높으면(죽은 시장 or 레드오션) 보너스 0.
        if (demand_comp >= cfg.imbalance_threshold
                and competition_refined >= cfg.imbalance_threshold):
            # 두 값이 기준을 얼마나 함께 넘는지 → 보너스 크기
            over_d = demand_comp - cfg.imbalance_threshold
            over_c = competition_refined - cfg.imbalance_threshold
            # 둘의 곱(둘 다 커야 큼) × 강도. 최대 1 범위로 정규화.
            span = max(1e-6, 1.0 - cfg.imbalance_threshold)
            synergy = (over_d / span) * (over_c / span)  # 0~1
            base = _clamp01(base + synergy * cfg.imbalance_bonus * rf)

        # 펼치기: 곱셈 결과는 0.0x대로 깔려 변별 불가.
        # 제곱근으로 펼쳐 변별을 살림 (0.04→0.2, 0.09→0.3). 0~1 유지.
        spread = base ** cfg.score_scale_power
        return _clamp01(spread)

    stable = combine(stable_demand)
    emerging = combine(emerging_demand)

    demand_note = ("수요데이터없음" if not demand.has_data
                   else f"수준{demand.level:.0f}/기울기{demand.slope:+.1f}")
    # 경쟁 정밀 신호를 근거에 추가 (데이터 있을 때만)
    comp_detail = ""
    if uniq_ratio is not None:
        comp_detail += f", 고유셀러 {uniq_ratio:.0%}"
    if bundle is not None:
        comp_detail += f", 묶음 {bundle:.0%}"
    if competition_refined != competition:
        comp_detail += f" → 경쟁보정 {competition:.2f}→{competition_refined:.2f}"
    rationale = (
        f"경쟁 total={market.total:,}(희소성 {competition:.2f}), "
        f"가격여지 {price_room:.2f}, {demand_note}, "
        f"리스크게이트 {rf:.2f}{comp_detail}"
    )

    return DiscoveryScore(
        keyword=market.keyword,
        category_path=market.category_path,
        competition_scarcity=competition,
        price_room=price_room,
        risk_factor=rf,
        demand_level=level_n,
        demand_slope=slope_n,
        stable_score=round(stable, 4),
        emerging_score=round(emerging, 4),
        total=market.total,
        rationale=rationale,
        seller_concentration=seller_conc,
        unique_seller_ratio=uniq_ratio,
        bundle_ratio=bundle,
        competition_refined=competition_refined,
    )


@dataclass(slots=True)
class DiscoveryResult:
    """전체 발굴 결과: 두 랭킹."""
    scores: list[DiscoveryScore] = field(default_factory=list)

    def ranked(self, list_type: ListType, top_n: int | None = None
               ) -> list[DiscoveryScore]:
        s = sorted(self.scores, key=lambda x: x.score_for(list_type), reverse=True)
        return s[:top_n] if top_n else s

    def stable(self, top_n: int | None = None) -> list[DiscoveryScore]:
        return self.ranked(ListType.STABLE, top_n)

    def emerging(self, top_n: int | None = None) -> list[DiscoveryScore]:
        return self.ranked(ListType.EMERGING, top_n)

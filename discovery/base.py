"""
discovery.base
==============
카테고리/키워드 발굴의 핵심 데이터 구조 + 공급원 인터페이스.

발굴 철학: 그물은 넓게. 발굴 단계는 컷오프 없이 랭킹만 매긴다.
진짜 거르기는 다음 단계(cost_mapper 마진 분석)에서.

두 리스트:
- 안정형(STABLE)  : 수요 level(절대 수준) 높고 경쟁 적음. 꾸준히 팔 후보.
- 선점형(EMERGING): 수요 slope(상승 기울기) 가파르고 경쟁 안 붙음. 공격적 후보.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ListType(str, Enum):
    STABLE = "STABLE"       # 안정형
    EMERGING = "EMERGING"   # 선점형


@dataclass(slots=True)
class ShopMarket:
    """검색 API(shop.json)로 본 '한 키워드 시장'의 단면."""
    keyword: str
    total: int                              # 등록 상품 수 = 경쟁 강도 직접 측정치
    lprices: list[int] = field(default_factory=list)  # item별 최저가 분포
    category_path: list[str] = field(default_factory=list)  # category1~4
    sample_titles: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    # --- 경쟁 구조 정밀 분석용 (1순위 고도화, 추가 API 호출 0) ---
    # 모두 default 보유 -> 기존 코드는 이 필드를 몰라도 그대로 동작(하위 호환)
    mall_names: list[str] = field(default_factory=list)   # item별 판매몰명
    product_types: list[int] = field(default_factory=list)  # item별 productType
    links: list[str] = field(default_factory=list)   # item별 상품 링크
    items: list[dict] = field(default_factory=list)  # item 원본(가격·유형·제목 짝)
    # ↑ 따로 담으면 짝이 어긋나 '유형1(가격비교)의 가격'을 못 고른다.
    #   네이버가 여러 판매처를 모아 계산한 최저가가 바로 그 값인데.
    # ↑ 링크 주소가 곧 증거다: search.shopping.naver.com/catalog/... = 가격비교
    #   카탈로그, 그 외(smartstore 등) = 독립 상품. productType 추측에 안 기댄다.

    @property
    def price_mean(self) -> float | None:
        return sum(self.lprices) / len(self.lprices) if self.lprices else None

    @property
    def price_cv(self) -> float | None:
        """가격 변동계수(표준편차/평균). 클수록 가격 책정 여지 = 가점."""
        if len(self.lprices) < 2:
            return None
        mean = self.price_mean
        if not mean:
            return None
        var = sum((p - mean) ** 2 for p in self.lprices) / len(self.lprices)
        return (var ** 0.5) / mean

    @property
    def seller_concentration(self) -> float | None:
        """상위 셀러 독점도 (HHI 정규화, 0~1). 높을수록 소수 독점 = 진입 어려움.
        mall_names 없으면 None (기존 호환)."""
        if not self.mall_names:
            return None
        from collections import Counter
        counts = Counter(self.mall_names)
        n = len(self.mall_names)
        # 허핀달지수: 점유율 제곱합 (1=완전독점, 1/n=완전분산)
        hhi = sum((c / n) ** 2 for c in counts.values())
        return hhi

    @property
    def unique_seller_ratio(self) -> float | None:
        """고유 셀러 수 / 표본 수. 높을수록 다양(틈새). 낮을수록 소수 독점."""
        if not self.mall_names:
            return None
        return len(set(self.mall_names)) / len(self.mall_names)

    @property
    def bundle_ratio(self) -> float | None:
        """productType 중 가격비교 묶음(1) 비율. 높을수록 정착된 레드오션.
        네이버 productType: 1=가격비교 묶음 상품군, 2=일반 단독 상품 등."""
        if not self.product_types:
            return None
        bundles = sum(1 for t in self.product_types if t == 1)
        return bundles / len(self.product_types)


@dataclass(slots=True)
class DemandTrend:
    """데이터랩 쇼핑인사이트로 본 키워드 수요 추세 (수준/기울기 분리)."""
    keyword: str
    level: float = 0.0        # 최근 구간 평균 수요 수준 (0~100 스케일)
    slope: float = 0.0        # 최근 구간 상승 기울기 (양수=상승)
    volatility: float = 0.0   # 변동성 (안정형에서 감점 요소)
    points: list[float] = field(default_factory=list)  # 원시 시계열
    periods: list[str] = field(default_factory=list)   # 각 점의 실제 날짜(YYYY-MM-DD)
    found: bool = False       # 데이터랩에서 실제로 잡혔는지

    @property
    def has_data(self) -> bool:
        return self.found and len(self.points) >= 2


@dataclass(slots=True)
class DiscoveryScore:
    """한 키워드의 발굴 점수 (두 리스트용 점수를 동시에 보유)."""
    keyword: str
    category_path: list[str] = field(default_factory=list)

    # 공통 성분 (0~1 정규화)
    competition_scarcity: float = 0.0   # 1 - normalize(log(total))
    price_room: float = 0.0             # normalize(price_cv)
    risk_factor: float = 1.0            # 1.0(안전) ~ 0.3(인증필요 등) 게이트

    # 수요 성분
    demand_level: float = 0.0           # normalize(level)
    demand_slope: float = 0.0           # normalize(slope)

    # 최종 점수
    stable_score: float = 0.0           # 안정형 랭킹용
    emerging_score: float = 0.0         # 선점형 랭킹용

    # 근거 (셀러 신뢰 확보용)
    total: int = 0
    rationale: str = ""

    # --- 경쟁 구조 정밀 분석 결과 (1순위 고도화, 모두 default) ---
    seller_concentration: float | None = None   # 독점도 HHI (높을수록 독점)
    unique_seller_ratio: float | None = None     # 고유셀러비 (높을수록 분산=틈새)
    bundle_ratio: float | None = None            # 묶음비율 (높을수록 레드오션)
    competition_refined: float = 0.0             # 보정 후 경쟁희소성 (실제 점수에 쓰인 값)

    # --- 공급 안정성 (네이버 proxy, default) ---
    supply_score: float | None = None            # 0~1, 높을수록 안 끊김
    supply_grade: str = "미상"                    # 안정/보통/주의/미상
    supply_rationale: str = ""

    # --- 인증/법규 (신규 기능 2, default) ---
    cert_labels: list[str] = field(default_factory=list)  # 필요 인증 목록
    cert_note: str = ""                                   # 경고 문구

    # --- 시장 생애주기 (시간축 분석, default) ---
    lifecycle_stage: str = ""        # 🟢 열리는 중 / 🔴 레드오션 / 🟡 성숙 / ⚫ 저무는 중
    lifecycle_note: str = ""         # 진입속도·가격추세 요약
    entry_pct_per_day: float = 0.0   # 하루 신규 진입률(%) — 간접 수요 증거

    # --- 마진 추정 (판매가 + 원가 통념, default) ---
    est_margin_pct: float | None = None   # 추정 마진율(%) — 1차 필터

    def score_for(self, list_type: ListType) -> float:
        return (self.stable_score if list_type == ListType.STABLE
                else self.emerging_score)


@dataclass(slots=True)
class CategorySeed:
    """발굴 씨앗. 데이터랩은 카테고리 코드 단위로 동작하므로 코드 필수."""
    name: str
    cat_id: str             # 네이버 쇼핑 카테고리 코드
    parent: str | None = None
    keywords: list[str] = field(default_factory=list)  # 이 카테고리의 씨앗 키워드


@runtime_checkable
class ShopSearchProvider(Protocol):
    async def market_of(self, keyword: str, sample: int = 100) -> ShopMarket:
        """키워드 -> 시장 단면(total/가격분포/카테고리)."""
        ...


@runtime_checkable
class DemandProvider(Protocol):
    async def trend_of(self, keyword: str, cat_id: str) -> DemandTrend:
        """키워드+카테고리코드 -> 수요 추세(level/slope)."""
        ...

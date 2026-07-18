"""
discovery.competition
====================
경쟁 '실체' 분석 — 숫자가 아니라 '누가' 팔고 있는지 본다.

[왜 필요한가]
"경쟁 800개"만 보면 🟢 로 잘못 판정한다. 그 800개가 전부 브랜드 카탈로그에
묶여 있으면 개인 셀러는 카탈로그 안에서 최저가 싸움만 가능 = 진입 불가.
반대로 개인 스마트스토어 위주면 800개여도 내 자리가 있다.

[데이터 출처] 네이버 검색 응답에 이미 들어오는 값 — 추가 호출 0회.
  - mallName    : 판매처 이름  → 대형몰/오픈마켓 장악도
  - productType : 상품 유형    → 가격비교 카탈로그 묶임 정도

[productType 의미 — 네이버 검색 API 문서 기준]
  1 = 일반상품 가격비교 상품(카탈로그 자체)
  2 = 일반상품 가격비교 비매칭(독립 상품 — 내 페이지로 노출됨)
  3 = 일반상품 가격비교 매칭(카탈로그에 묶인 상품 — 최저가 싸움)
  4~6 중고 / 7~9 단종 / 10~12 판매예정
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# 개인 셀러가 광고비로 이기기 어려운 대형 판매처
_BIG_MALLS = (
    "쿠팡", "11번가", "G마켓", "지마켓", "옥션", "위메프", "티몬",
    "SSG", "이마트", "롯데", "홈플러스", "GS", "CJ", "현대", "신세계",
    "인터파크", "하이마트", "다이소", "올리브영", "무신사", "컬리",
)
_CATALOG_TYPES = (1, 3)   # 카탈로그 자체 / 카탈로그에 묶인 상품
_INDIE_TYPES = (2,)       # 독립 상품 (내 상세페이지로 노출)
# 링크가 곧 증거 — 가격비교 카탈로그는 이 주소로 간다.
# productType 매핑은 문서 기억에 기댄 '추측'이지만, 링크는 사실이다.
_CATALOG_URL = ("/catalog/",)


@dataclass(slots=True)
class CompetitionProfile:
    basis: str = "link"          # 무엇으로 판정했나 (link=사실 / type=추측)
    agree_pct: float = -1.0      # 링크와 productType 이 일치한 비율 (자가검증)
    sample: int = 0
    catalog_pct: float = 0.0     # 가격비교 카탈로그에 묶인 비율
    indie_pct: float = 0.0       # 독립 상품 비율 (개인이 비집을 자리)
    bigmall_pct: float = 0.0     # 대형몰/오픈마켓 비율
    top_share_pct: float = 0.0   # 상위 1개 판매처 점유율 (독점도)
    top_mall: str = ""
    can_enter: bool = True
    grade: str = ""              # 들어갈만함/좁음/막힘
    note: str = ""
    warnings: list[str] = field(default_factory=list)


def _is_catalog_link(url: str) -> bool:
    return any(k in (url or "") for k in _CATALOG_URL)


def analyze_competition(market) -> CompetitionProfile:
    """
    경쟁 실체 판정.

    [판정 근거의 우선순위]
      1순위 = 상품 링크. 가격비교 카탈로그는 /catalog/ 로 간다 — 이건 사실.
      2순위 = productType. 문서 기억에 기댄 매핑이라 '추측'이다.
    링크가 있으면 링크로 판정하고, productType 과 얼마나 일치하는지(agree_pct)
    를 함께 내보낸다 → 사람이 일일이 눌러보지 않아도 매핑이 맞는지 알 수 있다.
    """
    malls = [m for m in (getattr(market, "mall_names", None) or []) if m]
    types = [t for t in (getattr(market, "product_types", None) or [])
             if t is not None]
    links = [l for l in (getattr(market, "links", None) or []) if l]
    p = CompetitionProfile(sample=max(len(malls), len(types), len(links)))
    if p.sample == 0:
        p.grade, p.note = "알수없음", "표본이 없어 경쟁 구조를 볼 수 없어요"
        return p

    # 1) 카탈로그 묶임 정도 — 링크(사실) 우선, 없으면 productType(추측)
    if links:
        cat = sum(1 for l in links if _is_catalog_link(l))
        p.catalog_pct = round(cat / len(links) * 100, 1)
        p.indie_pct = round(100 - p.catalog_pct, 1)
        p.basis = "link"
        # 자가검증: productType 추측이 링크(사실)와 얼마나 맞는지
        if types and len(types) == len(links):
            same = sum(1 for t, l in zip(types, links)
                       if (t in _CATALOG_TYPES) == _is_catalog_link(l))
            p.agree_pct = round(same / len(links) * 100, 1)
    elif types:
        cat = sum(1 for t in types if t in _CATALOG_TYPES)
        ind = sum(1 for t in types if t in _INDIE_TYPES)
        p.catalog_pct = round(cat / len(types) * 100, 1)
        p.indie_pct = round(ind / len(types) * 100, 1)
        p.basis = "type"

    # 2) 대형몰 장악도 + 독점도
    #    [중요] 가격비교 카탈로그(ptype 1)는 '판매처'가 아니다. 네이버가 만든
    #    상품 페이지일 뿐이라 mallName 이 '네이버' 등으로 찍힌다. 이걸 판매처로
    #    세면 "네이버가 80% 독점" 같은 헛판정이 나와 멀쩡한 시장을 막는다.
    items = list(getattr(market, "items", None) or [])
    if items:
        malls = [it.get("mall") for it in items
                 if it.get("ptype") != 1 and it.get("mall")]
    if malls:
        big = sum(1 for m in malls
                  if any(b in m for b in _BIG_MALLS))
        p.bigmall_pct = round(big / len(malls) * 100, 1)
        top_mall, top_n = Counter(malls).most_common(1)[0]
        p.top_mall = top_mall
        p.top_share_pct = round(top_n / len(malls) * 100, 1)

    # 3) 판정 — 막힌 순서대로
    if p.catalog_pct >= 70:
        p.can_enter, p.grade = False, "막힘"
        p.note = (f"{p.catalog_pct:.0f}%가 가격비교에 묶여 있어요 — "
                  f"내 상세페이지가 안 보이고 최저가 싸움만 됩니다")
    elif p.bigmall_pct >= 60:
        p.can_enter, p.grade = False, "막힘"
        p.note = (f"{p.bigmall_pct:.0f}%가 대형몰이에요 — "
                  f"광고비로 이기기 어렵습니다")
    elif p.top_share_pct >= 40:
        p.can_enter, p.grade = False, "좁음"
        p.note = (f"'{p.top_mall}' 한 곳이 {p.top_share_pct:.0f}%를 먹고 있어요 — "
                  f"독점 시장입니다")
    elif p.catalog_pct >= 40 or p.bigmall_pct >= 35:
        p.can_enter, p.grade = True, "좁음"
        p.note = (f"카탈로그 {p.catalog_pct:.0f}% · 대형몰 {p.bigmall_pct:.0f}% — "
                  f"자리는 있지만 좁아요")
    else:
        p.can_enter, p.grade = True, "들어갈만함"
        p.note = (f"독립 상품이 {p.indie_pct:.0f}% — "
                  f"개인 스토어가 비집고 들어갈 자리가 있어요")

    # 4) 부가 경고
    if p.catalog_pct >= 40:
        p.warnings.append(
            "가격비교에 묶이면 상세페이지·사진으로 차별화가 안 돼요")
    if p.bigmall_pct >= 35:
        p.warnings.append("대형몰이 많아 최저가가 계속 내려갈 수 있어요")
    return p

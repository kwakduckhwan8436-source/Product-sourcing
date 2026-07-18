"""
discovery.compliance
====================
인증·법규 검증 강화 (신규 기능 2).

[목적] 30년 셀러의 거부권 관문 — 인증 없이 떼서 팔면 판매중지·과태료·
형사처벌. 모르고 떼는 게 가장 큰 사고. 키워드/카테고리로 인증 필요
품목을 플래그하고 카드에 경고.

[주의] 이것은 '주의 환기'용 휴리스틱이지 법률 자문이 아니다.
플래그가 떴다고 무조건 불가가 아니라 '확인 필요'라는 신호.
최종 판단은 셀러가 관계기관/관세사에 확인해야 함.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CertType(str, Enum):
    KC_ELECTRIC = "KC인증(전기/전자)"
    KC_KIDS = "KC인증(어린이제품)"
    COSMETIC = "화장품(책임판매업)"
    FOOD = "식품(수입신고)"
    MEDICAL = "의료기기/의약외품"
    BATTERY = "배터리/충전(안전확인)"
    BIOCIDE = "살생물제(살균/방역)"


# 키워드/카테고리에 이 단어가 있으면 해당 인증 의심
_RULES: dict[CertType, tuple[str, ...]] = {
    CertType.KC_ELECTRIC: (
        "충전기", "어댑터", "전기", "전자", "콘센트", "멀티탭", "led", "조명",
        "전동", "가전", "플러그", "usb", "히터", "전열", "램프", "이어폰",
        "블루투스", "스피커", "보조배터리"),
    CertType.KC_KIDS: (
        "유아", "아동", "어린이", "완구", "장난감", "키즈", "베이비", "젖병",
        "유모차", "카시트", "учебный"),
    CertType.COSMETIC: (
        "화장품", "크림", "세럼", "토너", "로션", "에센스", "마스크팩",
        "선크림", "립", "파운데이션", "쿠션", "앰플", "스킨"),
    CertType.FOOD: (
        "식품", "간식", "과자", "차", "커피", "건강식품", "영양제", "보충제",
        "젤리", "캔디", "초콜릿", "분말", "원두", "차류"),
    CertType.MEDICAL: (
        "의료", "마스크", "체온계", "혈압", "찜질", "파스", "밴드", "붕대",
        "소독", "마사지기", "온열기"),
    CertType.BATTERY: (
        "배터리", "건전지", "리튬", "충전지", "보조배터리", "파워뱅크"),
    CertType.BIOCIDE: (
        "살균", "방역", "소독제", "탈취", "방충", "살충", "항균"),
}


@dataclass(slots=True)
class ComplianceFlag:
    keyword: str
    certs: list[CertType] = field(default_factory=list)
    note: str = ""

    @property
    def has_risk(self) -> bool:
        return bool(self.certs)

    @property
    def labels(self) -> list[str]:
        return [c.value for c in self.certs]

    @property
    def summary(self) -> str:
        if not self.certs:
            return "인증 이슈 없음(추정)"
        return " · ".join(self.labels)


def check_compliance(keyword: str, category_path: list[str] | None = None
                     ) -> ComplianceFlag:
    """키워드 + 카테고리에서 인증 필요 가능성을 탐지."""
    haystack = keyword.lower()
    if category_path:
        haystack += " " + " ".join(category_path).lower()

    found: list[CertType] = []
    for cert, words in _RULES.items():
        if any(w in haystack for w in words):
            found.append(cert)

    flag = ComplianceFlag(keyword=keyword, certs=found)
    if found:
        flag.note = ("인증/신고 대상일 수 있음 — 떼기 전 관세사·관계기관 확인 필수. "
                     "미인증 판매 시 판매중지·과태료 위험.")
    return flag

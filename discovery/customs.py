"""
discovery.customs
================
① 관세·부가세 — 도매 수입에서 마진을 통째로 삼키는 것.

[초보가 제일 크게 착각하는 것]
"150달러 이하는 면세" 는 <b>개인이 자기가 쓰려고</b> 살 때 얘기다.
<b>팔려고 수입하면 금액과 상관없이 관세·부가세가 붙는다.</b>
이걸 모르고 1688에서 대량 발주했다가 통관에서 세금 폭탄을 맞는 일이 흔하다.
그래서 도매를 다루는 이상 이 계산은 선택이 아니다.

[계산 방식 — 한국 수입 기준]
  과세가격(CIF) = 물품가 + 국제운임 + 보험료
  관세          = CIF × 관세율
  부가세        = (CIF + 관세) × 10%
  총 원가       = CIF + 관세 + 부가세 + 국내 부대비용

[정직한 한계 — 관세율은 품목마다 다르다]
정확한 관세율은 HS코드로 정해지고, 같은 '가방' 이라도 소재에 따라 갈린다.
아래 값은 <b>흔한 구간의 통념</b>이지 확정이 아니다.
실제 발주 전에는 관세청(unipass) 이나 관세사에게 확인해야 한다.
도구는 '세금이 이만큼 붙을 수 있다' 를 알려주는 것이지, 세액을 확정하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VAT_RATE = 0.10          # 부가세 10%
DEFAULT_DUTY = 8.0       # 관세율을 모를 때 흔히 쓰는 값(%)

# 카테고리별 관세율 — 통념. 반드시 확인 필요.
DUTY_TABLE = (
    ("의류", 13.0), ("패션의류", 13.0), ("니트", 13.0), ("코트", 13.0),
    ("신발", 13.0), ("가방", 8.0), ("패션잡화", 8.0),
    ("화장품", 6.5), ("미용", 6.5),
    ("식품", 30.0), ("건강", 8.0),
    ("전자", 8.0), ("디지털", 8.0), ("가전", 8.0), ("배터리", 8.0),
    ("가구", 8.0), ("인테리어", 8.0),
    ("완구", 8.0), ("유아", 8.0), ("육아", 8.0),
    ("스포츠", 8.0), ("레저", 8.0),
    ("생활", 8.0), ("주방", 8.0),
)


@dataclass(slots=True)
class CustomsCalc:
    item_total: int = 0        # 물품가 합계
    shipping: int = 0          # 국제운임(배송대행비)
    cif: int = 0               # 과세가격
    duty_rate: float = 0.0     # 적용한 관세율(%)
    duty: int = 0              # 관세
    vat: int = 0               # 부가세
    tax_total: int = 0         # 세금 합계
    grand_total: int = 0       # 총 원가
    per_unit: int = 0          # 개당 진짜 원가
    taxed: bool = True         # 세금이 붙나
    warns: list = field(default_factory=list)
    note: str = ""


def duty_rate_of(category: str, keyword: str = "") -> float:
    """카테고리로 관세율 추정 — 통념이지 확정이 아니다."""
    text = f"{category} {keyword}"
    for key, rate in DUTY_TABLE:
        if key in text:
            return rate
    return DEFAULT_DUTY


def estimate(item_price: int, qty: int, shipping: int,
             category: str = "", keyword: str = "",
             duty_rate: float | None = None,
             for_resale: bool = True) -> CustomsCalc:
    """
    수입 원가 = 물품가 + 운임 + 관세 + 부가세.

    for_resale=True (팔려고 수입) → 금액 무관 과세. 이게 기본값인 이유는
      이 도구를 쓰는 사람은 팔려고 떼오기 때문이다.
    """
    c = CustomsCalc()
    qty = max(1, int(qty or 1))
    c.item_total = max(0, int(item_price or 0)) * qty
    c.shipping = max(0, int(shipping or 0))
    c.cif = c.item_total + c.shipping
    if c.cif <= 0:
        c.note = "물품가를 넣어주세요"
        return c

    c.duty_rate = (duty_rate if duty_rate is not None
                   else duty_rate_of(category, keyword))

    if not for_resale and c.cif <= 150_000:
        # 개인 자가사용 소액면세 — 판매 목적이면 해당 없음
        c.taxed = False
        c.duty = c.vat = 0
        c.warns.append(
            "개인 자가사용으로 계산했어요. 팔 거면 이 면세는 적용되지 않습니다")
    else:
        c.duty = int(c.cif * c.duty_rate / 100)
        c.vat = int((c.cif + c.duty) * VAT_RATE)

    c.tax_total = c.duty + c.vat
    c.grand_total = c.cif + c.tax_total
    c.per_unit = int(c.grand_total / qty)

    if for_resale:
        c.warns.append(
            "팔려고 수입하면 <b>금액과 상관없이</b> 관세·부가세가 붙어요 — "
            "'150달러 이하 면세'는 개인이 자기가 쓸 때만입니다")
    if c.cif >= 2_000_000:
        c.warns.append("금액이 커요 — 관세사를 끼는 게 안전합니다")
    if "식품" in f"{category}{keyword}":
        c.warns.append("식품은 관세가 높고(30% 안팎) 식약처 수입신고가 따로 필요해요")
    c.warns.append(
        f"관세율 {c.duty_rate:.1f}%는 <b>통념</b>이에요. 품목(HS코드)마다 다르니 "
        f"발주 전 관세청·관세사에 꼭 확인하세요")
    c.note = (f"물품 {c.item_total:,} + 운임 {c.shipping:,} = 과세가격 {c.cif:,}원 "
              f"→ 관세 {c.duty:,} + 부가세 {c.vat:,} = 세금 {c.tax_total:,}원")
    return c


def margin_after_tax(sell_price: int, per_unit_cost: int,
                     fee_pct: float = 0.25) -> dict:
    """세금까지 넣은 실제 마진."""
    fee = int(sell_price * fee_pct)
    margin = sell_price - per_unit_cost - fee
    rate = round(margin / sell_price * 100, 1) if sell_price else 0.0
    return {"fee": fee, "margin": margin, "rate": rate,
            "ok": margin > 0}

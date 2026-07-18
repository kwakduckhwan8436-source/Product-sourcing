"""
discovery.landed_cost
====================
착지원가 계산기 (신규 기능 1).

[목적] 30년 셀러의 가장 흔한 사고 — "겉마진에 속아 떼왔다 손해".
알리/도매 원가만 보면 안 된다. 진짜 원가 = 상품가 + 배송 + 관세 +
부가세 + 판매수수료 + 광고비 + 반품충당. 이걸 다 빼고 실마진을 본다.

[설계] 모든 비율은 LandedCostConfig 에서 조절 가능. GUI 에서 사용자가
자기 장사에 맞게 바꾼다. 키 불필요 — 사용자가 원가/판매가만 넣으면 계산.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LandedCostConfig:
    """착지원가 계산에 쓰는 비율들. 전부 조절 가능 (기본값은 알리 소싱 일반 기준)."""
    customs_rate: float = 0.08       # 관세율 (품목마다 다름, 기본 8%)
    vat_rate: float = 0.10           # 부가세 10%
    platform_fee_rate: float = 0.0585  # 네이버 스마트스토어 수수료 ~5.85%
    ad_cost_rate: float = 0.10       # 광고비 (판매가 대비 10%)
    return_reserve_rate: float = 0.03  # 반품충당 (판매가 대비 3%)
    usd_krw: float = 1380.0          # 환율 (USD 원가 입력 시)


@dataclass(slots=True)
class LandedCostResult:
    sale_price: float                # 판매가(원)
    product_cost: float              # 상품 원가(원)
    shipping: float                  # 배송비(원)
    customs: float                   # 관세(원)
    vat: float                       # 부가세(원)
    platform_fee: float              # 판매수수료(원)
    ad_cost: float                   # 광고비(원)
    return_reserve: float            # 반품충당(원)
    landed_cost: float               # 착지원가 합계(원)
    net_margin: float                # 실마진(원)
    margin_rate: float               # 실마진율 (%)
    markup: float                    # 판매가/상품원가 배수
    verdict: str                     # 판정 메시지

    @property
    def is_profitable(self) -> bool:
        return self.net_margin > 0


def compute_landed_cost(
    sale_price: float,
    product_cost: float,
    shipping: float = 0.0,
    cfg: LandedCostConfig | None = None,
    cost_in_usd: bool = False,
) -> LandedCostResult:
    """
    착지원가와 실마진 계산.
    - sale_price: 한국 판매가(원)
    - product_cost: 상품 매입 원가 (cost_in_usd=True 면 USD, 아니면 원)
    - shipping: 국제+국내 배송비(원)
    - 관세/부가세는 (상품원가+배송) 기준, 수수료/광고/반품충당은 판매가 기준
    """
    cfg = cfg or LandedCostConfig()
    if cost_in_usd:
        product_cost = product_cost * cfg.usd_krw

    # 수입 비용: 관세·부가세는 (상품원가+배송) = 과세표준 기준
    taxable = product_cost + shipping
    customs = taxable * cfg.customs_rate
    vat = (taxable + customs) * cfg.vat_rate   # 부가세는 관세 포함액 기준

    # 판매 비용: 판매가 기준
    platform_fee = sale_price * cfg.platform_fee_rate
    ad_cost = sale_price * cfg.ad_cost_rate
    return_reserve = sale_price * cfg.return_reserve_rate

    landed = (product_cost + shipping + customs + vat
              + platform_fee + ad_cost + return_reserve)
    net = sale_price - landed
    margin_rate = (net / sale_price * 100) if sale_price else 0.0
    markup = (sale_price / product_cost) if product_cost else 0.0

    # 판정 (30년 셀러 기준: 실마진율 15% 미만이면 위험)
    if net <= 0:
        verdict = "❌ 적자 — 떼면 안 됨"
    elif margin_rate < 10:
        verdict = "⚠ 박리 — 광고·반품 한 번에 적자 전환 위험"
    elif margin_rate < 20:
        verdict = "△ 보통 — 마진 얇음, 물량으로 승부"
    else:
        verdict = "✅ 양호 — 마진 여유 있음"

    return LandedCostResult(
        sale_price=round(sale_price), product_cost=round(product_cost),
        shipping=round(shipping), customs=round(customs), vat=round(vat),
        platform_fee=round(platform_fee), ad_cost=round(ad_cost),
        return_reserve=round(return_reserve), landed_cost=round(landed),
        net_margin=round(net), margin_rate=round(margin_rate, 1),
        markup=round(markup, 2), verdict=verdict)

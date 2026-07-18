"""
discovery.mode
=============
위탁(드랍십) vs 도매(사입) — 소싱 기준이 다르다.

[왜 나눠야 하나]
같은 시장이라도 어떻게 떼오느냐에 따라 '좋은 자리' 가 다르다.
한 기준으로 둘 다 보면 둘 다 틀린다.

  위탁 — 재고 0. 공급사가 배송. 공급가가 비싸 마진이 얇다.
    · 마진 3,000원이라도 한다 (돈이 안 묶이니까)
    · 남과 상품이 똑같다 → 경쟁 적은 롱테일이 생명
    · 유일한 우위: 미리 올려둬도 공짜 → 시즌 선행
    · 무서운 것: 품절(내 페널티), 배송지연 클레임, 반품비

  도매 — 대량으로 싸게 뗀다. 내가 재고를 안고 배송한다.
    · 마진율이 두꺼워야 한다 (돈이 묶이고 재고가 썩는다)
    · 수요가 큰 시장이어도 된다 (물량으로 먹으니까)
    · 우위: 묶음·자체포장으로 차별화, 단가 협상
    · 무서운 것: 안 팔리면 재고가 그대로 손실

[정직] 아래 숫자(3,000원·30%·최소수량)는 통념이다. 본인 장사에 맞게
바꿔야 한다. 다만 '위탁과 도매는 다른 기준으로 봐야 한다' 는 건 사실이다.
"""
from __future__ import annotations

from dataclasses import dataclass

CONSIGN = "consign"      # 위탁
WHOLESALE = "wholesale"  # 도매


@dataclass(slots=True)
class ModeRule:
    key: str
    label: str
    fee_pct: float          # 수수료+광고+세금
    target_rate: float      # 목표 마진율(%) — 상품값에 비례
    floor_margin: int       # 그래도 이 돈은 남아야 (저가 상품 방어)
    target_margin: int      # (구) 고정 목표 — 화면 호환용
    min_margin_rate: float  # 최소 마진율(%) — 도매는 이게 중요
    prefer_empty: float     # 경쟁이 적은 걸 얼마나 중요하게 볼까 (0~1)
    cost_label: str         # 화면에 뭐라고 부를까
    moq: int                # 최소 주문 수량(통념)
    note: str
    watch: tuple            # 이 방식에서 조심할 것


RULES = {
    CONSIGN: ModeRule(
        key=CONSIGN, label="위탁 (재고 없이)",
        fee_pct=0.25,
        target_rate=15.0,      # 상품값의 15%
        floor_margin=1000,     # 5천원짜리도 최소 1천원은
        target_margin=3000, min_margin_rate=10.0,
        prefer_empty=0.75,
        cost_label="떼오는 값 상한",
        moq=1,
        note=("재고가 0이라 돈이 안 묶여요. 마진이 얇아도 됩니다. "
              "대신 남과 상품이 똑같으니 <b>경쟁 적은 자리</b>가 생명이에요."),
        watch=("품절 — 팔렸는데 공급사에 재고가 없으면 페널티는 나에게 옵니다",
               "배송지연 — 공급사가 보내니 늦어요. 상세페이지에 미리 밝히세요",
               "반품비 — 반품비 6,000원짜리를 마진 3,000원에 팔면 "
               "한 번 반품에 두 개 판 게 날아갑니다"),
    ),
    WHOLESALE: ModeRule(
        key=WHOLESALE, label="도매 (대량 사입)",
        fee_pct=0.25,
        target_rate=30.0,      # 상품값의 30% — 돈이 묶이니 비율이 중요
        floor_margin=2000,
        target_margin=6000, min_margin_rate=30.0,
        prefer_empty=0.45,
        cost_label="사입 단가 상한",
        moq=30,
        note=("돈이 묶이고 재고가 썩을 수 있어요. <b>마진율 30% 이상</b>은 "
              "돼야 합니다. 대신 수요가 큰 시장도 물량으로 먹을 수 있어요."),
        watch=("재고 리스크 — 안 팔리면 그대로 손실입니다. 첫 발주는 적게",
               "최소수량(MOQ) — 보통 30개 이상. 그만큼 자본이 묶여요",
               "보관·배송 — 내가 다 해야 합니다. 그 품과 비용도 마진에서 나가요"),
    ),
}


def rule_of(mode: str) -> ModeRule:
    return RULES.get(mode or CONSIGN, RULES[CONSIGN])


def target_of(price_min: int, rule: ModeRule) -> int:
    """
    한 개 팔아 남길 돈 — 상품값에 비례한다.

    [왜 고정이면 안 되나] 4,500원짜리에 3,000원을 남기라는 건 말이 안 된다
    (원가가 375원이어야 한다). 5만원짜리에 3,000원만 남기는 것도 아깝다.
    → 상품값의 몇 % 로 잡되, 너무 싼 물건은 최소 금액으로 받쳐준다.
    """
    if price_min <= 0:
        return 0
    return max(rule.floor_margin, int(price_min * rule.target_rate / 100))


def reverse_cost(price_min: int, rule: ModeRule,
                 fee_pct: float | None = None,
                 target_margin: int | None = None) -> int:
    """
    이 값 이하로 떼와야 남는다.

    위탁: 목표 마진(원) 기준 — 한 개라도 남으면 되니까.
    도매: 마진율 기준도 함께 본다 — 돈이 묶이므로 비율이 중요하다.
    """
    if price_min <= 0:
        return 0
    fee = rule.fee_pct if fee_pct is None else fee_pct
    tgt = target_of(price_min, rule) if target_margin is None else target_margin
    by_amount = price_min - price_min * fee - tgt
    if rule.key == WHOLESALE:
        by_rate = price_min * (1 - fee - rule.min_margin_rate / 100)
        return max(0, int(min(by_amount, by_rate)))
    return max(0, int(by_amount))


def score_weights(rule: ModeRule) -> tuple:
    """
    (비어있음, 마진여지, 들어갈수있음) 가중치.
    위탁은 '빈 자리' 가 생명, 도매는 '마진' 이 생명.
    """
    if rule.key == WHOLESALE:
        return (45.0, 35.0, 20.0)
    return (65.0, 15.0, 20.0)


def mode_advice(rule: ModeRule, price_min: int, need_cost: int) -> list:
    """이 방식에서 이 상품을 볼 때 알아야 할 것."""
    out = []
    if rule.key == WHOLESALE:
        if need_cost > 0:
            total = need_cost * rule.moq
            out.append(f"최소 {rule.moq}개만 떼도 약 {total:,}원이 묶여요 "
                       f"— 첫 발주는 더 적게 시작하세요")
        rate = ((price_min - need_cost) / price_min * 100) if price_min else 0
        out.append(f"이 단가면 마진율 약 {rate:.0f}% — 도매는 30% 아래면 위험해요")
    else:
        out.append("재고가 없으니 미리 올려둬도 손해가 없어요 — 시즌을 노려보세요")
        if need_cost and need_cost < 5000:
            out.append("마진이 얇아요. 반품 한 번이면 여러 개 판 게 날아갑니다")
    return out

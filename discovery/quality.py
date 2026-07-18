"""
discovery.quality
================
'좋은 상품인가' — 팔리는 것과 별개로, 떼도 되는 물건인가.

[왜 필요한가]
경쟁이 적고 마진이 나도 <b>떼면 안 되는 물건</b>이 있다.
  · 의류·신발 — 사이즈 반품이 많다. 마진을 반품비가 삼킨다
  · 식품·화장품 — 유통기한·신고증. 초보가 손대면 위험
  · 의료기기·건강기능식품 — 신고증 없으면 판매 자체가 불법
  · 부피 큰 것 — 위탁은 몰라도 도매는 보관비가 마진을 먹는다
  · 값이 너무 싼 것 — 반품 한 번에 여러 개 판 게 날아간다

도구가 '경쟁 적음 + 마진 남음' 만 보고 추천하면, 이걸 모르는 사람이
그대로 떼왔다가 물린다. 그래서 따로 본다.

[정직한 한계]
카테고리 이름으로 추정한다. 네이버가 무게·부피를 안 주기 때문이다.
'이 물건은 위험할 수 있다' 는 신호지, 확정이 아니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 신고증·인증이 있어야 파는 것 — 없으면 불법
_LICENSE = ("의료기기", "건강기능식품", "의약", "렌즈", "안경",
            "전자담배", "주류", "홍삼")
# 사이즈·취향 반품이 많은 것
_RETURN_HEAVY = ("패션의류", "패션잡화", "신발", "속옷", "수영복", "코트",
                 "원피스", "바지", "니트", "가디건", "재킷")
# 유통기한·변질
_PERISH = ("식품", "화장품", "미용", "간식", "음료", "냉동", "신선")
# 부피가 커서 보관·배송이 무거운 것 (도매에서 특히)
_BULKY = ("가구", "인테리어", "매트", "러그", "행거", "책장", "테이블",
          "의자", "침대", "소파", "건조대", "선반")
# 전기·안전 인증(KC)이 걸리는 것
_KC = ("전기", "충전", "배터리", "led", "전자", "가전", "히터", "선풍기")


@dataclass(slots=True)
class Quality:
    score: int = 100          # 100에서 위험만큼 깎는다
    grade: str = ""           # 안전 / 보통 / 조심 / 하지마
    good: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    blocked: bool = False     # 인증 없으면 못 파는 것


def assess_quality(keyword: str, category: str, price_min: int,
                   need_cost: int, mode: str = "consign",
                   comp=None, spread: dict | None = None) -> Quality:
    """이 물건, 떼도 되나."""
    q = Quality()
    text = f"{category} {keyword}".lower()

    # ── 못 파는 것부터 ──
    for k in _LICENSE:
        if k in text:
            q.blocked = True
            q.score = 0
            q.grade = "하지마"
            q.risks.append(
                f"{k} — 신고증이 있어야 팔 수 있어요. 없으면 불법입니다")
            return q

    # ── 위험 ──
    if any(k in text for k in _RETURN_HEAVY):
        q.score -= 25
        q.risks.append("사이즈·취향 반품이 많은 품목이에요 "
                       "— 반품비가 마진을 먹습니다")
    if any(k in text for k in _PERISH):
        q.score -= 20
        q.risks.append("유통기한·변질 위험이 있어요 "
                       "— 재고를 안는 도매는 특히 조심")
    if any(k in text for k in _KC):
        q.score -= 15
        q.risks.append("전기·배터리 제품은 KC 인증을 확인하세요")
    if any(k in text for k in _BULKY):
        if mode == "wholesale":
            q.score -= 20
            q.risks.append("부피가 커요 — 도매는 보관·배송비가 마진을 먹습니다")
        else:
            q.score -= 5
            q.risks.append("부피가 커서 반품 배송비가 비쌉니다")

    margin = max(0, price_min - need_cost)
    if price_min and price_min < 8000:
        q.score -= 20
        q.risks.append(f"값이 싸요({price_min:,}원) — 반품 한 번에 "
                       f"여러 개 판 게 날아갑니다")
    if margin and margin < 2000:
        q.score -= 15
        q.risks.append(f"남는 게 {margin:,}원뿐 — 반품·불량 한 번이면 마이너스")

    # ── 좋은 점 ──
    if comp is not None:
        if (getattr(comp, "indie_pct", 0) or 0) >= 60:
            q.score += 8
            q.good.append("개인 스토어가 많아요 — 상세페이지로 승부할 수 있어요")
        if (getattr(comp, "catalog_pct", 0) or 0) >= 40:
            q.score -= 10
            q.risks.append("가격비교에 묶여 있어요 — 최저가 싸움만 됩니다")
    if spread and spread.get("found"):
        sp = spread.get("spread_pct") or 0
        if sp >= 40:
            q.score += 10
            q.good.append(f"파는 곳마다 값이 {sp:.0f}% 벌어져 있어요 — 파고들 틈")
        elif sp <= 10:
            q.score -= 8
            q.risks.append("파는 곳마다 값이 같아요 — 가격으로는 못 이깁니다")
    if 10000 <= price_min <= 60000:
        q.score += 8
        q.good.append("값이 팔기 좋은 대역이에요 (1~6만원)")
    if not q.risks:
        q.good.append("눈에 띄는 위험이 없어요")

    q.score = max(0, min(100, q.score))
    if q.score >= 80:
        q.grade = "안전"
    elif q.score >= 60:
        q.grade = "보통"
    else:
        q.grade = "조심"
    return q


# ─────────────────────────────────────────────────────────────
# 엔진 강화 — 이미 받아둔 데이터에서 더 캐낸다 (추가 호출 0회)
# ─────────────────────────────────────────────────────────────
import re as _re
from collections import Counter as _Counter

_TITLE_NOISE = _re.compile(
    r"(무료배송|당일발송|정품|최저가|특가|할인|이벤트|사은품|증정|\d+%|\d+원)")


def brand_grip(market) -> dict:
    """
    한 브랜드가 이 시장을 얼마나 쥐고 있나.

    [왜 중요한가] 브랜드가 상위를 다 먹고 있으면 개인 셀러는 못 뚫는다.
    소비자가 '그 브랜드' 를 찾아 들어오기 때문이다. 경쟁 수가 적어도 소용없다.
    네이버가 상품마다 brand/maker 를 주는데 그동안 안 썼다.
    """
    items = list(getattr(market, "items", None) or [])
    names = [str(it.get("brand") or it.get("maker") or "").strip()
             for it in items]
    names = [n for n in names if n and len(n) >= 2]
    if len(names) < 4:
        return {"found": False, "top": "", "pct": 0.0, "note": ""}
    top, cnt = _Counter(names).most_common(1)[0]
    pct = round(cnt / len(names) * 100, 1)
    if pct >= 60:
        note = f"'{top}' 브랜드가 {pct:.0f}% — 브랜드 시장이라 개인은 뚫기 어려워요"
    elif pct >= 35:
        note = f"'{top}' 브랜드가 {pct:.0f}% — 브랜드 힘이 센 편이에요"
    else:
        note = f"특정 브랜드가 없어요 (최다 {top} {pct:.0f}%) — 개인이 들어갈 자리"
    return {"found": True, "top": top, "pct": pct, "note": note}


def title_sameness(market) -> dict:
    """
    제목이 다들 똑같은가 = 물건이 다 똑같다 = 가격 말고 싸울 게 없다.

    [왜 중요한가] 위탁은 같은 공급사에서 떼니 제목까지 판박이인 시장이 있다.
    거긴 상세페이지를 아무리 잘 만들어도 최저가만 팔린다.
    """
    titles = [str(t) for t in (getattr(market, "sample_titles", None) or [])]
    if len(titles) < 6:
        return {"found": False, "pct": 0.0, "note": ""}
    sets = []
    for t in titles[:40]:
        toks = set(_TITLE_NOISE.sub(" ", t).split())
        if toks:
            sets.append(toks)
    if len(sets) < 6:
        return {"found": False, "pct": 0.0, "note": ""}
    # 서로 얼마나 겹치나 (평균 자카드)
    tot = n = 0
    for i in range(len(sets)):
        for j in range(i + 1, min(i + 6, len(sets))):
            a, b = sets[i], sets[j]
            u = len(a | b)
            if u:
                tot += len(a & b) / u
                n += 1
    if not n:
        return {"found": False, "pct": 0.0, "note": ""}
    pct = round(tot / n * 100, 1)
    if pct >= 55:
        note = f"제목이 다들 판박이예요 ({pct:.0f}% 겹침) — 최저가 말고 싸울 게 없어요"
    elif pct >= 35:
        note = f"제목이 비슷비슷해요 ({pct:.0f}% 겹침)"
    else:
        note = f"제목이 제각각이에요 ({pct:.0f}% 겹침) — 상세페이지로 승부 가능"
    return {"found": True, "pct": pct, "note": note}


def price_room(market) -> dict:
    """
    가격이 벌어져 있나 = 값을 매길 여지가 있나.
    ShopMarket.price_cv 를 그동안 계산만 해두고 안 썼다.
    """
    cv = getattr(market, "price_cv", None)
    if cv is None:
        return {"found": False, "cv": 0.0, "note": ""}
    cv = float(cv)
    if cv >= 0.35:
        note = "파는 값이 제각각이에요 — 중간 가격대로 들어갈 자리가 있어요"
    elif cv <= 0.12:
        note = "다들 비슷한 값에 팔아요 — 가격 싸움이 됩니다"
    else:
        note = ""
    return {"found": True, "cv": round(cv, 2), "note": note}


def deep_quality(q: Quality, market, mode: str = "consign") -> Quality:
    """
    기본 판정에 '이미 받아둔 데이터에서 더 캔 신호' 를 얹는다.
    추가 API 호출 0회 — 그동안 버리던 것들이다.
    """
    bg = brand_grip(market)
    ts = title_sameness(market)
    pr = price_room(market)

    if bg["found"]:
        if bg["pct"] >= 60:
            q.score -= 25
            q.risks.append(bg["note"])
        elif bg["pct"] >= 35:
            q.score -= 10
            q.risks.append(bg["note"])
        else:
            q.score += 8
            q.good.append(bg["note"])
    if ts["found"]:
        if ts["pct"] >= 55:
            q.score -= 18
            q.risks.append(ts["note"])
        elif ts["pct"] < 35:
            q.score += 8
            q.good.append(ts["note"])
    if pr["found"] and pr["note"]:
        if pr["cv"] >= 0.35:
            q.score += 8
            q.good.append(pr["note"])
        else:
            q.score -= 8
            q.risks.append(pr["note"])

    # 기본 판정에서 '위험 없음'을 붙였는데 정밀 단계에서 위험이 생겼으면 그 문구 제거
    if q.risks:
        q.good = [g for g in q.good if "눈에 띄는 위험이 없어요" not in g]

    q.score = max(0, min(100, q.score))
    if not q.blocked:
        q.grade = ("안전" if q.score >= 80 else
                   ("보통" if q.score >= 60 else "조심"))
    return q

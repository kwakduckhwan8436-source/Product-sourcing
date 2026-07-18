"""
discovery.beginner
=================
초보용 판정 엔진 — "이거 팔아도 돼요?" 한 질문에 답한다.

[설계 원칙]
- 결론부터: 🟢 팔아도 됨 / 🟡 애매 / 🔴 하지 마세요
- 근거는 4줄: 얼마에 떼나 / 얼마에 팔리나 / 남나 / 어떻게 올리나
- 점수·변동계수 같은 건 초보 화면에 안 보임
- 초보가 모르고 물리는 위험(착불·해외배송·인증품목)을 도구가 대신 경고

[실측 컬럼 근거] 오너클랜 xlsx에서 확인:
  12=배송유형(무료/선불/착불), 34=원산지(국내산/해외|아시아|중국),
  53=반품배송비, 4=카테고리명
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 인증·신고가 필요해 초보가 손대면 안 되는 품목
_LICENSE_NEEDED = ("의료기기", "건강기능식품", "의약", "렌즈")
# 클레임이 많아 초보에게 위험한 품목
_CLAIM_HEAVY = ("식품", "화장품", "속옷")


@dataclass(slots=True)
class BeginnerVerdict:
    light: str = "🟡"              # 🟢 / 🟡 / 🔴
    headline: str = ""             # 한 줄 결론
    buy_line: str = ""             # ① 얼마에 떼나
    sell_line: str = ""            # ② 얼마에 팔리나
    profit_line: str = ""          # ③ 남나
    title_line: str = ""           # ④ 어떻게 올리나
    warnings: list[str] = field(default_factory=list)
    margin: int = 0
    titles: list[str] = field(default_factory=list)
    best_keyword: str = ""


def check_risks(product) -> list[str]:
    """초보가 모르고 물리는 위험을 오너클랜 데이터로 자동 경고."""
    out: list[str] = []
    cat = product.category or ""
    origin = (getattr(product, "origin", "") or "")
    ship_type = (getattr(product, "shipping_type", "") or "")
    return_fee = int(getattr(product, "return_fee", 0) or 0)

    for k in _LICENSE_NEEDED:
        if k in cat:
            out.append(f"🔴 {k} 품목 — 신고증이 있어야 팔 수 있어요. 초보는 피하세요")
            break
    for k in _CLAIM_HEAVY:
        if k in cat:
            out.append(f"🟡 {k} 품목 — 반품·클레임이 많은 편이에요")
            break
    if "착불" in ship_type:
        out.append("🟡 착불 배송 — 반품될 때 배송비를 물 수 있어요")
    if "해외" in origin:
        out.append("🟡 해외 배송 상품 — 배송이 늦어 클레임이 생기기 쉬워요")
    if return_fee >= 5000:
        out.append(f"🟡 반품배송비 {return_fee:,}원 — 반품 한 번에 마진이 날아가요")
    return out


def assess(product, naver_total: int, naver_min: int, titles: list[str],
           best_keyword: str = "", fee_pct: float = 0.25) -> BeginnerVerdict:
    """
    오너클랜 상품 + 네이버 최저가로 초보용 판정.
    fee_pct: 마켓수수료+광고·세금 대략 (기본 25%)
    """
    v = BeginnerVerdict(titles=titles, best_keyword=best_keyword)
    v.warnings = check_risks(product)
    wholesale = product.wholesale
    v.buy_line = f"오너클랜에서 {wholesale:,}원에 떼요"

    # 네이버에서 못 찾음
    if naver_total <= 0 or naver_min <= 0:
        v.light = "🟡"
        v.headline = "아직 판단하기 일러요"
        v.sell_line = "네이버에 비슷한 상품이 안 보여요"
        v.profit_line = "파는 사람이 없어 가격을 가늠할 수 없어요"
        v.title_line = "아무도 안 판다면 — 기회일 수도, 안 팔리는 물건일 수도 있어요"
        return v

    # 실제로 남는 돈 (최저가에 맞춰 팔 때)
    margin = int(naver_min - wholesale - naver_min * fee_pct
                 - int(getattr(product, "shipping", 0) or 0))
    v.margin = margin
    v.sell_line = f"네이버 최저가 {naver_min:,}원 — 이미 {naver_total:,}명이 팔아요"

    if margin > 0:
        v.profit_line = f"최저가에 맞춰 팔면 한 개당 약 {margin:,}원 남아요"
    else:
        v.profit_line = f"최저가에 맞춰 팔면 한 개당 약 {abs(margin):,}원 손해예요"

    # 신호등 판정 — 손해/경쟁/인증 순으로 본다
    blocked = any(w.startswith("🔴") for w in v.warnings)
    if blocked:
        v.light, v.headline = "🔴", "이 상품은 하지 마세요"
        v.title_line = "인증이 필요한 품목이에요"
    elif margin <= 0:
        v.light, v.headline = "🔴", "이건 팔면 손해예요"
        v.title_line = "최저가 경쟁에서 남지 않아요 — 다른 상품을 보세요"
    elif naver_total > 30_000:
        v.light, v.headline = "🔴", "경쟁이 너무 세요"
        v.title_line = f"이미 {naver_total:,}명이 팔아요 — 광고 없이는 안 보여요"
    elif margin < 1500:
        v.light, v.headline = "🟡", "남는 게 너무 적어요"
        v.title_line = f"{margin:,}원 벌자고 하기엔 손이 많이 가요"
    elif naver_total > 5_000:
        v.light, v.headline = "🟡", "팔리긴 하는데 경쟁이 있어요"
        v.title_line = (f"'{best_keyword}' 로 올려보세요" if best_keyword
                        else "제목을 구체적으로 지어야 보여요")
    else:
        v.light, v.headline = "🟢", "팔아도 됩니다"
        v.title_line = (f"'{best_keyword}' 로 올리면 비집고 들어갈 수 있어요"
                        if best_keyword else "이 제목으로 올려보세요")
    return v


def friendly_error(msg: str) -> str:
    """오류를 초보 말로 바꿔준다."""
    m = str(msg)
    if "429" in m or "RateLimited" in m or "한도" in m:
        return ("네이버가 잠깐 쉬라고 하네요 (하루에 쓸 수 있는 양을 다 썼어요).\n"
                "10분쯤 뒤에 다시 해보시고, 그래도 안 되면 내일 다시 해주세요.")
    if "401" in m or "Auth" in m or "자격" in m:
        return ("네이버 열쇠(아이디·비밀번호)가 맞지 않아요.\n"
                "위쪽 칸에 네이버 개발자센터에서 받은 값을 다시 넣어주세요.")
    if "찾을 수 없" in m or "xlsx" in m.lower() or "zip" in m.lower():
        return ("오너클랜 파일을 못 찾았어요.\n"
                "오너클랜에서 받은 zip 파일을 그대로 골라주세요.")
    return f"문제가 생겼어요: {m}"

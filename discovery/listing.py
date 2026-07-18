"""
discovery.listing
================
A. 상품명·상세페이지 자동 작성 / C. 묶음(세트) 발굴

[왜 여기가 매출을 가른다]
'뭘 팔지'를 알아도 제목을 못 지으면 안 팔린다. 위탁은 상품이 남과 똑같으니
남는 무기는 제목과 상세페이지뿐이다.

[근거는 추측이 아니라 채굴한 데이터]
상위 노출 100개 제목에서 캐낸 것만 쓴다.
  - 필수어(30% 이상이 쓰는 말) → 빼면 검색에 안 걸린다
  - 희귀말(소수만 쓰는 말)     → 아직 안 붐빈 진입로
  - 바이그램(붙여 쓰는 짝)     → 같이 팔리는 조합 = 세트 힌트

[정직한 한계]
- 상위노출을 보장하지 않는다. 노출은 판매량·리뷰·클릭률·광고가 함께 정한다.
- 상세페이지는 '뼈대'다. 사진과 실제 문장은 사람이 채워야 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_MAX_TITLE = 50          # 네이버 권장 제목 길이
# 세트로 묶을 때 자연스러운 짝이 아닌 말 (부속·수식어)
_NOT_SET = {"무료", "배송", "당일", "정품", "인기", "추천", "최저"}


@dataclass(slots=True)
class Listing:
    titles: list[str] = field(default_factory=list)
    must_words: list[str] = field(default_factory=list)
    gap_words: list[str] = field(default_factory=list)
    detail_lines: list[str] = field(default_factory=list)
    bundle_ideas: list[str] = field(default_factory=list)
    why: str = ""


def build_titles(entry_keyword: str, mined, base: str = "",
                 limit: int = 3) -> list[str]:
    """
    제목 후보 = 진입 키워드(앞) + 필수어 + 핵심어.

    네이버 제목 원칙: 검색될 말을 앞에, 중복 없이, 50자 내.
    필수어를 넣는 이유 — 상위 노출 중 30%+ 가 쓰는 말은 소비자가 실제로
    같이 검색하는 말이다. 빼면 그 검색에서 아예 안 걸린다.
    """
    core = (base or "").strip()
    ek = entry_keyword.strip()

    def _useful(w: str) -> bool:
        """이미 검색어/핵심어에 들어있는 말은 제목에 또 넣지 않는다.
        ('실리콘주방장갑' 검색인데 필수어 '주방장갑'을 또 붙이면 중복)"""
        if not w or len(w) < 2:
            return False
        if w in ek or ek in w:
            return False
        if core and (w in core or core in w):
            return False
        return True

    must = [w.word for w in getattr(mined, "must", []) if _useful(w.word)][:3]
    out: list[str] = []

    def _mk(words: list[str]) -> str:
        seen, parts = set(), []
        for w in words:
            for tok in str(w).split():
                if tok and tok not in seen:
                    seen.add(tok)
                    parts.append(tok)
        return " ".join(parts)[:_MAX_TITLE]

    # 1) 진입키워드 + 필수어 1개  (가장 안전)
    if must:
        out.append(_mk([entry_keyword, must[0], core]))
    # 2) 진입키워드 + 필수어 2개  (검색 폭 넓힘)
    if len(must) >= 2:
        out.append(_mk([entry_keyword, must[0], must[1], core]))
    # 3) 필수어 앞세우기 (대표 검색 노림)
    if must:
        out.append(_mk([must[0], entry_keyword, core]))
    if not out:
        out.append(_mk([entry_keyword, core]))
    # 중복/과단문 제거
    res, seen = [], set()
    for t in out:
        if len(t) >= 6 and t not in seen:
            seen.add(t)
            res.append(t)
    return res[:limit]


def build_detail(entry_keyword: str, mined, price_min: int = 0,
                 need_cost: int = 0) -> list[str]:
    """
    상세페이지 첫 3줄 뼈대 — 소비자가 3초 안에 보는 곳.

    구성: ① 누구를 위한 물건인가(진입 키워드가 곧 타겟)
          ② 왜 이걸 사야 하나(시장이 공통으로 내세우는 값 = 필수어)
          ③ 남과 뭐가 다른가(희귀말 = 남들이 안 말하는 지점)
    """
    must = [w.word for w in getattr(mined, "must", [])][:3]
    rare = [w.word for w in getattr(mined, "rare", [])][:2]
    lines: list[str] = []

    lines.append(f"[누구에게] {entry_keyword} 찾으시는 분께 — "
                 f"딱 그 용도에 맞춘 제품입니다")
    if must:
        lines.append(f"[핵심] {' · '.join(must)} — "
                     f"이 시장에서 다들 따지는 것들, 다 갖췄습니다")
    else:
        lines.append("[핵심] 기본에 충실한 제품입니다")
    if rare:
        lines.append(f"[남과 다른 점] {' · '.join(rare)} — "
                     f"파는 곳이 드문 부분입니다. 여기를 사진으로 보여주세요")
    else:
        lines.append("[남과 다른 점] 사진·후기로 차별화하세요 "
                     "(제목만으론 부족합니다)")
    return lines


def build_bundles(base: str, mined, limit: int = 5) -> list[str]:
    """
    C. 묶음(세트) 발굴 — 최저가 비교를 탈출하는 유일한 길.

    상위 제목에서 '붙여 쓰는 짝'(바이그램)을 캐낸다. 같이 적힌다는 건
    같이 쓰인다는 뜻 → 세트로 묶으면 비교 대상이 사라진다.
    """
    out: list[str] = []
    core = (base or "").strip()
    for pair, count in getattr(mined, "pairs", [])[:12]:
        toks = [t for t in pair.split() if t not in _NOT_SET]
        if len(toks) < 2:
            continue
        if core and core in pair:
            continue
        out.append(f"{core} + {toks[-1]} 세트  (상위 {count}개가 함께 적음)")
        if len(out) >= limit:
            break
    if not out:
        rare = [w.word for w in getattr(mined, "rare", [])][:2]
        for w in rare:
            out.append(f"{core} + {w} 구성  (남들이 안 하는 조합)")
    return out[:limit]


def build_listing(entry_keyword: str, base: str, mined,
                  price_min: int = 0, need_cost: int = 0) -> Listing:
    """제목 + 상세 뼈대 + 세트 아이디어를 한 번에."""
    lst = Listing()
    lst.must_words = [w.word for w in getattr(mined, "must", [])][:5]
    lst.gap_words = [w.word for w in getattr(mined, "rare", [])][:5]
    lst.titles = build_titles(entry_keyword, mined, base)
    lst.detail_lines = build_detail(entry_keyword, mined, price_min, need_cost)
    lst.bundle_ideas = build_bundles(base, mined)
    if lst.must_words:
        lst.why = (f"상위 노출 상품 {getattr(mined, 'sample', 0)}개를 뜯어보니 "
                   f"'{lst.must_words[0]}' 를 다들 제목에 넣어요 — "
                   f"빼면 그 검색에서 안 보입니다")
    else:
        lst.why = "상위 제목에서 공통어가 안 나왔어요 — 자유롭게 지어도 됩니다"
    return lst


# ─────────────────────────────────────────────────────────────
# ⑤ 제목 자동 다듬기
# ─────────────────────────────────────────────────────────────
# [근거] 네이버 쇼핑 상품명 가이드(공개 문서) 기준.
#   권장 순서: 브랜드/제조사 → 상품명 → 속성(색/재질/수량/사이즈)
#   금지: 중복 단어, 특수문자·이모지, 홍보문구(최저가·무료배송·이벤트 등)
#   길이: 100자까지 되지만 노출은 앞쪽 위주 → 50자 안쪽을 권함
# ※ 이건 '가이드' 지 '검증된 공식' 이 아니다. 노출은 판매·리뷰·광고가 함께 정한다.

_BAN_WORDS = (
    "최저가", "무료배송", "당일발송", "당일출고", "이벤트", "특가", "핫딜",
    "세일", "할인", "쿠폰", "사은품", "증정", "무료", "정품", "인기",
    "베스트", "추천", "신상", "1위", "국내최저", "빠른배송", "묶음배송",
)
_BAN_CHARS = re.compile(r"[^\w가-힣\s\.\-]")   # 이모지·특수문자
_MULTI_SP = re.compile(r"\s+")


@dataclass(slots=True)
class PolishedTitle:
    text: str = ""
    length: int = 0
    kind: str = ""            # 짧게 / 표준 / 길게
    warns: list = field(default_factory=list)


def polish_titles(entry_keyword: str, base: str, mined,
                  attrs: str = "") -> list:
    """
    제목 3벌 — 짧게(안전) / 표준 / 길게(키워드 많이).

    attrs: 사용자가 아는 속성(예: "3단 대형 스테인리스"). 없으면 채굴한 말로.
    """
    must = [w.word for w in getattr(mined, "must", [])][:4]
    gaps = [w.word for w in getattr(mined, "rare", [])][:3]
    core = (base or "").strip()
    ek = (entry_keyword or "").strip()

    def _clean_word(w: str) -> str:
        w = _BAN_CHARS.sub(" ", w)
        return _MULTI_SP.sub(" ", w).strip()

    def _useful(w: str) -> bool:
        w = _clean_word(w)
        if not w or len(w) < 2:
            return False
        if any(b in w for b in _BAN_WORDS):
            return False
        if w in ek or ek in w:
            return False
        if core and (w in core or core in w):
            return False
        return True

    user_attrs = [a for a in _MULTI_SP.sub(" ", attrs or "").split() if _useful(a)]
    picked_must = [w for w in must if _useful(w)]
    picked_gap = [w for w in gaps if _useful(w)]

    def _build(words: list) -> PolishedTitle:
        seen, parts = set(), []
        for w in words:
            for tok in _clean_word(w).split():
                low = tok.lower()
                if tok and low not in seen:
                    seen.add(low)
                    parts.append(tok)
        text = _MULTI_SP.sub(" ", " ".join(parts)).strip()
        t = PolishedTitle(text=text[:_MAX_TITLE], kind="")
        t.length = len(t.text)
        if len(text) > _MAX_TITLE:
            t.warns.append(f"{len(text)}자라 {_MAX_TITLE}자로 잘랐어요")
        return t

    out: list = []
    # 짧게 — 진입 키워드 + 필수어 1개 (가장 안전)
    a = _build([ek] + picked_must[:1] + user_attrs[:1])
    a.kind = "짧게 (안전)"
    out.append(a)
    # 표준 — 진입 키워드 + 필수어 2 + 속성 1
    b = _build([ek] + picked_must[:2] + user_attrs[:2])
    b.kind = "표준 (권장)"
    out.append(b)
    # 길게 — 빈틈 말까지 (검색 폭 넓힘)
    c = _build([ek] + picked_must[:2] + picked_gap[:1] + user_attrs[:3])
    c.kind = "길게 (키워드 많이)"
    out.append(c)

    res, seen = [], set()
    for t in out:
        if t.text and t.text not in seen and len(t.text) >= 6:
            seen.add(t.text)
            if t.length > 45:
                t.warns.append("좀 길어요 — 앞쪽 단어가 더 중요합니다")
            res.append(t)
    return res


def title_warnings(text: str) -> list:
    """이 제목에 문제가 있나 — 네이버 가이드 기준."""
    w = []
    if not text:
        return ["제목이 비었어요"]
    if len(text) > _MAX_TITLE:
        w.append(f"{len(text)}자 — {_MAX_TITLE}자 안쪽을 권합니다")
    hit = [b for b in _BAN_WORDS if b in text]
    if hit:
        w.append(f"홍보문구는 빼세요: {', '.join(hit[:3])} (스팸으로 볼 수 있어요)")
    if _BAN_CHARS.search(text):
        w.append("특수문자·이모지는 빼세요")
    toks = [t.lower() for t in text.split()]
    dup = {t for t in toks if toks.count(t) > 1}
    if dup:
        w.append(f"같은 말이 반복돼요: {', '.join(list(dup)[:3])}")
    return w


# ─────────────────────────────────────────────────────────────
# ⑥ 상세페이지 뼈대 (6~8줄) + 사진 목록
# ─────────────────────────────────────────────────────────────
@dataclass(slots=True)
class DetailPlan:
    lines: list = field(default_factory=list)    # [(제목, 내용)]
    photos: list = field(default_factory=list)   # 찍어야 할 사진
    note: str = ""


def build_detail_plan(entry_keyword: str, base: str, mined,
                      price_min: int = 0, need_cost: int = 0,
                      category: str = "") -> DetailPlan:
    """
    상세페이지 뼈대 — 위탁판매용.

    [왜 이 순서인가] 소비자는 위에서 아래로 훑다가 3초 안에 나간다.
      1) 후킹   — 누구의 어떤 문제인가 (검색해서 들어온 그 말로 시작)
      2) 핵심   — 이 시장이 공통으로 따지는 것 (채굴한 필수어)
      3) 차별   — 남들이 말 안 하는 지점 (채굴한 빈틈)
      4) 사양   — 크기·재질 (사람이 채워야 할 자리)
      5) 대상   — 이런 분께
      6) 배송   — 위탁은 배송이 늦을 수 있어 미리 밝히는 게 클레임을 줄인다
      7) 주의   — 반품 조건. 안 밝히면 분쟁이 된다
      8) 마무리 — 지금 살 이유

    [정직] 문장은 뼈대다. 사진과 실제 문장은 사람이 채워야 한다.
    """
    must = [w.word for w in getattr(mined, "must", [])][:4]
    gaps = [w.word for w in getattr(mined, "rare", [])][:3]
    core = (base or "").strip()
    ek = (entry_keyword or "").strip()
    p = DetailPlan()

    p.lines.append(("1. 첫 줄 (3초 안에 읽히는 곳)",
                    f"{ek} 찾으시죠? 딱 그 용도로 만든 {core}입니다."))
    if must:
        p.lines.append(("2. 핵심 3가지",
                        " / ".join(f"{w}" for w in must[:3])
                        + "  ← 이 시장에서 다들 따지는 것들. 사진으로 보여주세요"))
    else:
        p.lines.append(("2. 핵심 3가지",
                        "재질 / 크기 / 사용 편의 — 세 가지만 골라 크게 쓰세요"))
    if gaps:
        p.lines.append(("3. 남과 다른 점",
                        " · ".join(gaps)
                        + "  ← 파는 곳이 드문 부분. 여기가 승부처입니다"))
    else:
        p.lines.append(("3. 남과 다른 점",
                        "사진·후기로 차별화하세요 (제목만으론 부족합니다)"))
    p.lines.append(("4. 사양 (직접 채우세요)",
                    "크기 __cm × __cm / 무게 __g / 재질 __ / 구성 __개"))
    p.lines.append(("5. 이런 분께",
                    f"{ek}이 필요한 분 / 좁은 공간에 두실 분 / 처음 사보시는 분"))
    p.lines.append(("6. 배송 안내 (위탁이라 꼭)",
                    "공급사에서 바로 보냅니다. 주문 후 __일 내 출고, "
                    "도서산간은 더 걸릴 수 있어요. — 미리 밝히면 클레임이 줄어요"))
    p.lines.append(("7. 교환·반품",
                    "단순변심 반품비 __원 / 개봉·사용 후 반품 불가 / "
                    "불량은 전액 부담 — 안 밝히면 분쟁이 됩니다"))
    if price_min:
        p.lines.append(("8. 마무리",
                        f"같은 값이면 이걸 고르실 이유를 한 줄로. "
                        f"(이 시장 최저가는 {price_min:,}원대예요)"))
    else:
        p.lines.append(("8. 마무리", "같은 값이면 이걸 고르실 이유를 한 줄로."))

    p.photos = [
        "대표컷 — 흰 배경, 정면. 썸네일이 클릭을 정합니다",
        "사용컷 — 실제로 쓰는 장면 (검색해 들어온 그 상황 그대로)",
        "크기 비교컷 — 손·A4·페트병 옆에. '생각보다 작다' 반품을 막아요",
        "디테일컷 — 위에 쓴 '핵심 3가지'를 하나씩 클로즈업",
        "구성품컷 — 받으면 뭐가 들었는지 (분쟁 예방)",
    ]
    if gaps:
        p.photos.append(f"차별점컷 — '{gaps[0]}' 를 눈에 보이게. 여기가 승부처")
    p.note = ("뼈대입니다. 사진과 실제 문장은 직접 채우셔야 해요 — "
              "위탁은 상품이 남과 같으니 남는 무기가 이것뿐입니다.")
    return p

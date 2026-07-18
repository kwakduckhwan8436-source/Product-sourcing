"""
discovery.keyword_forge
======================
키워드 선점 — 남이 안 쓴 조합을 만들어 낸다.

[원리]
같은 상품이라도 '어떤 말로 파느냐'로 경쟁이 갈린다. 대표어("우산")는
수십만 개지만, 축을 바꾼 조합("골프우산 답례품")은 몇백 개다.
소비자가 실제로 검색하는 축은 정해져 있다 — 용도/대상/상황/속성/수량.

[정직한 한계]
- 조합을 '만들' 수는 있지만 그게 실제로 검색되는지는 네이버에 물어봐야 안다.
  → 생성 후 반드시 조회해서 '검색 0'인 유령 키워드를 걸러낸다.
- 인기검색어 랭킹 API가 없으므로 '요즘 뜨는 말'은 알 수 없다.
  대신 축 조합으로 넓게 던지고 데이터로 거른다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 소비자가 실제로 쓰는 검색 축 (한국 이커머스 기준)
_AXES: dict[str, tuple[str, ...]] = {
    "대상": ("아기", "신생아", "어린이", "학생", "남자", "여자", "어르신",
             "반려견", "고양이", "커플", "1인가구"),
    "용도": ("업소용", "가정용", "차량용", "캠핑용", "사무실", "주방", "욕실",
             "현관", "여행용", "등산", "헬스", "선물용", "답례품", "판촉물"),
    "상황": ("장마", "여름", "겨울", "이사", "집들이", "명절", "생일",
             "돌잔치", "야외", "실내", "출근", "야간"),
    "속성": ("대용량", "미니", "대형", "소형", "휴대용", "무선", "접이식",
             "방수", "가벼운", "튼튼한", "고급", "저렴한", "인기"),
    "수량": ("대량", "묶음", "세트", "10개입", "낱개", "박스"),
}
_STOP = re.compile(r"[\[\]\(\)/]|\d+개입|\d+개|\d+P|세트|단품")

# 분야마다 말이 되는 축이 다르다.
# 실제 사고: '가정용 트렌치코트' 를 만들어 네이버에 물었더니 엉뚱한 케이프가
# 나왔다. 옷에 '업소용/가정용/차량용' 을 붙이면 아무도 그렇게 검색하지 않는다.
_AXES_BY_CATEGORY = {
    "패션의류": ("대상", "상황", "속성"),
    "패션잡화": ("대상", "상황", "속성"),
    "화장품/미용": ("대상", "속성"),
    "식품": ("대상", "상황", "수량"),
    "출산/육아": ("대상", "속성", "상황"),
    "디지털/가전": ("용도", "속성"),
    "가구/인테리어": ("용도", "속성"),
    "스포츠/레저": ("용도", "대상", "속성"),
    "생활/건강": ("용도", "대상", "속성"),
    "여가/생활편의": ("용도", "속성"),
}
_DEFAULT_AXES = ("용도", "대상", "속성")


def axes_for(category: str) -> tuple:
    """그 분야에서 말이 되는 축만 준다."""
    return _AXES_BY_CATEGORY.get(category, _DEFAULT_AXES)

# 2단 조합에서 한국어로 자연스러운 축 순서만 허용.
# ("아기 업소용 건조대" 처럼 대상+용도는 말이 안 됨 → 제외)
_PAIR_OK = (
    ("용도", "속성"),   # 업소용 대용량 건조대
    ("대상", "속성"),   # 아기 방수 시트
    ("상황", "속성"),   # 장마 접이식 우산
    ("용도", "수량"),   # 업소용 대량 장갑
    ("속성", "수량"),   # 대용량 묶음 물티슈
)


@dataclass(slots=True)
class ForgedKeyword:
    keyword: str
    axis: str = ""          # 어떤 축으로 만든 조합인지
    total: int = 0
    price_min: int = 0
    grade: str = ""
    is_ghost: bool = False  # 검색 자체가 없는 유령 키워드
    is_open: bool = False   # 비어 있는 진입로


@dataclass(slots=True)
class ForgeResult:
    base: str = ""
    forged: list[ForgedKeyword] = field(default_factory=list)
    open_ones: list[ForgedKeyword] = field(default_factory=list)
    ghosts: int = 0
    note: str = ""


def core_of(name: str) -> str:
    """상품명/키워드에서 핵심 명사만 남긴다."""
    s = _STOP.sub(" ", name or "")
    toks = [t for t in s.split() if len(t) >= 2]
    return toks[-1] if toks else (name or "").strip()


def forge(base: str, axes: tuple[str, ...] = ("용도", "대상", "속성", "상황"),
          per_axis: int = 4, depth: int = 1,
          extra: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """
    대표어 + 축 조합으로 진입로 후보 생성.
    depth=2 면 축×축 2단 조합까지 만든다 ("업소용 대용량 빨래건조대").
    → 1단은 대부분 이미 붐빈다. 빈자리는 2단에 있다.
    extra: 제목 채굴 등 외부에서 캐낸 후보 [(키워드, 근거)]
    반환: [(키워드, 축이름), ...] — 중복 제거, 원본 포함.
    """
    core = core_of(base)
    out: list[tuple[str, str]] = [(base.strip(), "원본")]
    seen = {base.strip()}

    for kw, why in (extra or []):
        k = kw.strip()
        if k and k not in seen:
            seen.add(k)
            out.append((k, why))

    firsts: list[tuple[str, str]] = []
    for axis in axes:
        for word in _AXES.get(axis, ())[:per_axis]:
            kw = f"{word} {core}".strip()
            if kw not in seen and len(kw) >= 4:
                seen.add(kw)
                out.append((kw, axis))
                firsts.append((word, axis))

    if depth >= 2:
        # 자연스러운 축 순서만 (앞: 용도/대상/상황, 뒤: 속성/수량)
        for w1, a1 in firsts:
            for w2, a2 in firsts:
                if (a1, a2) not in _PAIR_OK:
                    continue
                kw = f"{w1} {w2} {core}".strip()
                if kw not in seen and len(kw) <= 30:
                    seen.add(kw)
                    out.append((kw, f"{a1}+{a2}"))
    return out


def summarize(base: str, forged: list[ForgedKeyword]) -> ForgeResult:
    """조회 결과를 정리 — 유령 걸러내고 빈 진입로만 남긴다."""
    r = ForgeResult(base=base, forged=forged)
    r.ghosts = sum(1 for f in forged if f.is_ghost)
    r.open_ones = sorted(
        [f for f in forged if f.is_open and not f.is_ghost],
        key=lambda f: f.total)
    if not r.open_ones:
        r.note = ("만들어 본 조합이 전부 막혀 있거나 검색이 없어요 — "
                  "이 물건은 말을 바꿔도 자리가 안 나요")
    else:
        best = r.open_ones[0]
        r.note = (f"'{best.keyword}' 가 가장 비어 있어요 "
                  f"({best.total:,}개 · {best.axis} 축)")
    return r

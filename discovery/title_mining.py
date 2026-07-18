"""
discovery.title_mining
=====================
제목 채굴 — 상위 노출 상품들의 제목에서 '이 시장의 진짜 언어'를 캔다.

[왜 획기적인가]
네이버는 인기검색어 랭킹 API를 안 준다. 크롤링도 안 한다. 그래서 '요즘 뜨는 말'
을 알 방법이 없다고 봤다. 그런데 — 상위 노출 100개 상품의 제목이 곧 답이다.
그 셀러들은 팔리는 말을 넣어 제목을 지었다. 상위에 있다는 건 그 말이 먹힌다는
증거다. 즉 제목 = 검증된 키워드 뭉치.

[캐내는 것]
  1. 이 시장에서 다들 쓰는 말(빈출 토큰) → 내 제목에 꼭 넣어야 할 말
  2. 소수만 쓰는 말(희귀 토큰)         → 아직 안 붐빈 진입로 후보
  3. 붙여 쓰는 짝(바이그램)            → 실제 검색되는 조합

[정직한 한계]
- 검색량이 아니라 '셀러들이 쓰는 말'이다. 실제 검색되는지는 조회로 확인해야 한다.
- 형태소 분석기 없이 공백/기호 기준으로 자른다(외부 의존 없이 가볍게).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# 제목에서 걸러야 할 잡음
_NOISE = re.compile(
    r"(무료배송|당일발송|무료|정품|최저가|특가|할인|이벤트|사은품|증정|"
    r"국내생산|당일|배송|추천|인기|BEST|NEW|SALE|세일|쿠폰|카드|"
    r"\d+%|\d+원|\d+개입|\d+매|\d+세트|\d+P|\d+ml|\d+g|\d+kg|\d+cm|\d+mm)",
    re.I)
_SYM = re.compile(r"[^\w가-힣]+")
_NUM_ONLY = re.compile(r"^[\d\W_]+$")
_STOPWORDS = {
    "상품", "제품", "판매", "전용", "정품", "본품", "옵션", "선택",
    "및", "외", "형", "용", "개", "종", "세트", "묶음", "대용",
}


@dataclass(slots=True)
class MinedWord:
    word: str
    count: int = 0
    share: float = 0.0      # 상위 노출 중 몇 %가 이 말을 쓰나
    kind: str = ""          # 필수어 / 흔한말 / 희귀말


@dataclass(slots=True)
class MiningResult:
    sample: int = 0
    must: list[MinedWord] = field(default_factory=list)    # 다들 쓰는 말
    rare: list[MinedWord] = field(default_factory=list)    # 소수만 쓰는 말
    pairs: list[tuple[str, int]] = field(default_factory=list)  # 붙여 쓰는 짝
    note: str = ""


def _tokens(title: str) -> list[str]:
    t = _NOISE.sub(" ", title or "")
    t = _SYM.sub(" ", t)
    out = []
    for w in t.split():
        if len(w) < 2 or len(w) > 12:
            continue
        if _NUM_ONLY.match(w) or w in _STOPWORDS:
            continue
        out.append(w)
    return out


def mine_titles(titles: list[str], base: str = "",
                exclude: set | None = None) -> MiningResult:
    """
    상위 노출 제목들 → 이 시장의 언어.

    exclude: 브랜드·제조사·판매처 이름. 반드시 걸러야 한다.
      [왜] 이걸 안 거르면 '리빙엔 빨래건조대', '오늘의집 빨래건조대' 같은
      브랜드 키워드를 만들어낸다. 그 브랜드 상품만 나오니 검색이 20건도 안
      되고 전부 '찾는 사람 없음' 으로 죽는다 — 실제로 그렇게 되고 있었다.
      우리가 찾는 건 브랜드가 아니라 '속성·용도' 말이다.
    """
    r = MiningResult(sample=len(titles or []))
    if r.sample < 5:
        r.note = "제목 표본이 적어 캐낼 게 없어요"
        return r

    base_toks = set(_tokens(base)) if base else set()
    ban = set()
    for e in (exclude or ()):
        for t in _tokens(str(e)):
            ban.add(t)
    freq: Counter = Counter()
    pair: Counter = Counter()
    for t in titles:
        toks = _tokens(t)
        seen = set()
        for w in toks:
            if w in base_toks or w in ban:   # 검색어·브랜드는 제외
                continue
            if w not in seen:       # 한 제목에서 중복 카운트 방지
                seen.add(w)
                freq[w] += 1
        for a, b in zip(toks, toks[1:]):
            if (a not in base_toks and b not in base_toks
                    and a not in ban and b not in ban):
                pair[f"{a} {b}"] += 1

    n = r.sample
    for w, c in freq.most_common(80):
        share = round(c / n * 100, 1)
        mw = MinedWord(word=w, count=c, share=share)
        if share >= 30:
            mw.kind = "필수어"          # 다들 쓰는 말 — 빼면 안 보임
            r.must.append(mw)
        elif c >= 2 and 5.0 <= share <= 25.0:
            # 희귀말 = 소수만 쓰는 말 = 아직 안 붐빈 진입로.
            # (예전엔 '정확히 3번' 만 통과해 표본이 작으면 늘 빈손이었다 —
            #  눈덩이의 연료가 여기서 끊겼다)
            mw.kind = "희귀말"
            r.rare.append(mw)

    r.pairs = [(p, c) for p, c in pair.most_common(12) if c >= 2]
    if r.must:
        r.note = (f"이 시장은 '{r.must[0].word}' 를 "
                  f"{r.must[0].share:.0f}%가 제목에 넣어요 — 빼면 안 보입니다")
    elif r.rare:
        r.note = f"공통어가 없어요 — '{r.rare[0].word}' 같은 말로 비집어 보세요"
    else:
        r.note = "제목에서 건질 말이 없어요"
    return r


def suggest_keywords(base: str, mined: MiningResult, limit: int = 12
                     ) -> list[tuple[str, str]]:
    """
    캐낸 말로 진입 키워드 후보 생성.
    희귀말(아직 안 붐빈 말) + 대표어 조합이 핵심. [(키워드, 근거)]
    """
    out: list[tuple[str, str]] = []
    seen = set()
    core = base.strip()

    # 1순위: 희귀말 + 대표어 (남들이 아직 안 쓰는 말)
    for w in mined.rare[:limit]:
        kw = f"{w.word} {core}"
        if kw not in seen:
            seen.add(kw)
            out.append((kw, f"상위 {w.count}개만 쓰는 말"))

    # 1.5순위: 필수어 + 대표어.
    # [왜 넣었나] 희귀말만 쓰면 '3단 커플 욕실슬리퍼' 같은 초롱테일이 되어
    # 검색이 20건도 안 나온다 → 전부 '찾는 사람 없음' 으로 죽는다.
    # 필수어는 상위 30% 이상이 제목에 넣는 말이라 실제로 검색되는 말이다.
    for w in getattr(mined, "must", [])[:4]:
        kw = f"{w.word} {core}"
        if kw not in seen:
            seen.add(kw)
            out.append((kw, f"상위 {w.share:.0f}% 가 쓰는 말"))
    # 2순위: 실제 붙여 쓰는 짝 (검증된 조합)
    for p, c in mined.pairs[:6]:
        kw = f"{p} {core}" if core not in p else p
        if kw not in seen and len(kw) <= 30:
            seen.add(kw)
            out.append((kw, f"상위 {c}개가 붙여 쓰는 말"))
    return out[:limit]

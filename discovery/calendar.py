"""
discovery.calendar
=================
B. 시즌 캘린더 — 1년 장사 계획을 한 장으로.

분야의 씨앗들을 데이터랩에 물어 각각의 성수기를 뽑고, 12개월 지도로 편다.
  "5월: 제습제·선풍기 / 8월: 가습기·전기요"

[비용] 씨앗 1개당 데이터랩 1회. 데이터랩은 한도가 좁으니 씨앗 수를 제한한다.
[한계] 1년치만 본다. '작년 한 번'의 패턴이라 반복 검증은 안 된 것.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from discovery.season import analyze_season

_MONTHS = ("1월", "2월", "3월", "4월", "5월", "6월",
           "7월", "8월", "9월", "10월", "11월", "12월")


@dataclass(slots=True)
class CalItem:
    keyword: str
    peak_month: int = 0
    ratio: float = 0.0        # 성수기가 평소의 몇 배
    rise_month: int = 0       # 오르기 시작하는 달 = 준비 시작
    stage: str = ""


@dataclass(slots=True)
class SeasonCalendar:
    months: dict = field(default_factory=dict)   # {월: [CalItem]}
    flat: list = field(default_factory=list)     # 연중 고른 것
    now_prep: list = field(default_factory=list)  # 지금 준비할 것
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "months": {str(m): [{"keyword": i.keyword, "ratio": i.ratio,
                                 "rise_month": i.rise_month}
                                for i in items]
                       for m, items in sorted(self.months.items())},
            "flat": [i.keyword for i in self.flat],
            "now_prep": [{"keyword": i.keyword, "peak_month": i.peak_month,
                          "ratio": i.ratio} for i in self.now_prep],
            "note": self.note,
        }


async def build_calendar(shop, demand, seeds: list, cat_id: str,
                         today_month: int, budget: int = 12,
                         progress_cb=None) -> SeasonCalendar:
    """
    seeds: [키워드...] · cat_id: 데이터랩 카테고리 코드
    반환: 12개월 지도 + '지금 준비할 것'
    """
    cal = SeasonCalendar()
    used = 0
    for kw in seeds:
        if used >= budget:
            break
        try:
            t = await demand.trend_of(kw, cat_id)
            used += 1
        except Exception:  # noqa: BLE001
            continue
        if not t or not t.points:
            continue
        s = analyze_season(t.points, periods=getattr(t, "periods", None))
        item = CalItem(keyword=kw, peak_month=s.peak_month,
                       ratio=s.peak_ratio, rise_month=s.rise_month,
                       stage=s.stage)
        if progress_cb:
            progress_cb(used, budget, kw)
        if not s.has_season:
            cal.flat.append(item)
            continue
        cal.months.setdefault(s.peak_month, []).append(item)
        # 지금 준비해야 할 것 = 상승 시작이 1~2달 앞
        gap = s.rise_month - today_month
        if gap < -6:
            gap += 12
        if 0 <= gap <= 2:
            cal.now_prep.append(item)

    for m in cal.months:
        cal.months[m].sort(key=lambda i: -i.ratio)
    cal.now_prep.sort(key=lambda i: -i.ratio)

    if cal.now_prep:
        top = cal.now_prep[0]
        cal.note = (f"지금 준비할 것 {len(cal.now_prep)}개 — "
                    f"특히 '{top.keyword}' 는 {top.peak_month}월에 "
                    f"평소의 {top.ratio:.1f}배로 뜁니다")
    elif cal.months:
        cal.note = "지금 당장 준비할 건 없어요. 달력에서 다음 시즌을 미리 보세요"
    else:
        cal.note = "이 분야는 시즌을 타지 않아요 — 아무 때나 시작해도 됩니다"
    return cal

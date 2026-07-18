"""
discovery.season
===============
시즌 선행 알림 — 위탁판매의 유일한 구조적 우위.

[왜 강력한가]
위탁은 재고가 0이라 '미리 올려두는 비용'이 없다. 사입 셀러는 시즌 전에
재고를 안고 있어야 하므로 못 하지만, 위탁은 3주 앞서 상품을 올려두고
리뷰 몇 개만 쌓아두면 시즌이 터질 때 이미 위에 있다.

[데이터] 데이터랩 쇼핑인사이트 12개월 시계열(DemandTrend.points).
  points 는 최근 12개월(기본)의 검색 수요 지수. 여기서
  '작년 언제부터 올랐나' → '올해 언제 준비해야 하나' 를 역산한다.

[정직한 한계]
- 1년치만 보므로 '작년 한 번'의 패턴이다. 2년 이상 반복 확인은 못 한다.
- 검색량이지 판매량이 아니다.
- 연중 고른 상품은 시즌성이 없다고 나오는 게 맞다(억지로 만들지 않음).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

# 시즌으로 인정할 최소 조건
_MIN_POINTS = 8            # 이보다 짧으면 판단 불가
_PEAK_RATIO = 1.4          # 성수기 평균이 연평균의 이 배 이상이어야 시즌
_RISE_RATIO = 1.15         # 이 배를 처음 넘는 지점을 '오르기 시작'으로 본다
_PREP_WEEKS = 3            # 권장 선행 준비 기간(주)


@dataclass(slots=True)
class SeasonProfile:
    has_season: bool = False
    peak_index: int = -1          # points 상의 성수기 위치
    peak_month: int = 0           # 성수기 월(1~12)
    rise_month: int = 0           # 오르기 시작하는 월
    weeks_until_rise: int = 0     # 지금부터 상승 시작까지 남은 주(음수=이미 지남)
    peak_ratio: float = 0.0       # 성수기 / 연평균
    stage: str = ""               # 지금준비 / 이미시작 / 아직멀었음 / 연중고름
    note: str = ""
    action: str = ""


def _month_of(idx: int, total: int, today: _dt.date | None = None,
              periods: list[str] | None = None) -> int:
    """
    points 인덱스 → 실제 월.
    periods(데이터랩이 준 실제 날짜)가 있으면 그걸 쓴다 — 추측 없음.
    없을 때만 '마지막 점 = 이번 달' 로 근사한다.
    """
    if periods and 0 <= idx < len(periods):
        try:
            return int(str(periods[idx]).split("-")[1])
        except (IndexError, ValueError):
            pass
    today = today or _dt.date.today()
    months_ago = (total - 1) - idx
    m = today.month - months_ago
    while m <= 0:
        m += 12
    return m


def analyze_season(points: list[float], today: _dt.date | None = None,
                   periods: list[str] | None = None) -> SeasonProfile:
    """12개월 시계열 → 시즌 선행 판정.
    periods: 데이터랩이 준 실제 날짜(YYYY-MM-DD). 있으면 추측 없이 정확."""
    today = today or _dt.date.today()
    p = SeasonProfile()
    pts = [float(x) for x in (points or []) if x is not None]
    if len(pts) < _MIN_POINTS:
        p.note = "시계열이 짧아 시즌을 판단할 수 없어요"
        p.stage = "판단불가"
        return p

    avg = sum(pts) / len(pts)
    if avg <= 0:
        p.note = "검색이 거의 없어요"
        p.stage = "판단불가"
        return p

    peak_i = max(range(len(pts)), key=lambda i: pts[i])
    p.peak_index = peak_i
    p.peak_ratio = round(pts[peak_i] / avg, 2)
    p.peak_month = _month_of(peak_i, len(pts), today, periods)

    # 시즌성이 약하면 억지로 만들지 않는다
    if p.peak_ratio < _PEAK_RATIO:
        p.has_season = False
        p.stage = "연중고름"
        p.note = "연중 고르게 팔려요 — 시즌을 노릴 상품은 아니에요"
        p.action = "아무 때나 시작해도 됩니다"
        return p

    p.has_season = True
    # 성수기 직전에서 거슬러 올라가며 '오르기 시작한 지점' 찾기
    rise_i = peak_i
    for i in range(peak_i, 0, -1):
        if pts[i] < avg * _RISE_RATIO:
            rise_i = i + 1
            break
    else:
        rise_i = 0
    p.rise_month = _month_of(rise_i, len(pts), today, periods)

    # 올해 기준 상승 시작까지 남은 개월 → 주
    months_left = p.rise_month - today.month
    if months_left < -6:
        months_left += 12
    elif months_left > 6:
        months_left -= 12
    p.weeks_until_rise = int(months_left * 4.3)

    if -1 <= months_left <= 0:
        p.stage = "이미시작"
        p.note = (f"{p.rise_month}월부터 오르기 시작해 {p.peak_month}월이 성수기예요 "
                  f"(평소의 {p.peak_ratio:.1f}배) — 이미 오르는 중입니다")
        p.action = "지금 바로 올리세요. 늦으면 남들 뒤에 섭니다"
    elif 0 < months_left <= 2:
        p.stage = "지금준비"
        p.note = (f"{p.rise_month}월부터 올라 {p.peak_month}월이 성수기예요 "
                  f"(평소의 {p.peak_ratio:.1f}배)")
        p.action = (f"약 {p.weeks_until_rise}주 남았어요 — "
                    f"지금 올려두면 시즌 때 이미 위에 있습니다")
    elif months_left < -1:
        p.stage = "지났음"
        p.note = (f"{p.peak_month}월이 성수기였어요 "
                  f"(평소의 {p.peak_ratio:.1f}배) — 올해는 지났습니다")
        p.action = f"내년 {max(1, p.rise_month - 1)}월쯤 다시 보세요"
    else:
        p.stage = "아직멀었음"
        p.note = (f"{p.peak_month}월이 성수기예요 (평소의 {p.peak_ratio:.1f}배)")
        p.action = (f"약 {p.weeks_until_rise}주 뒤부터 오릅니다 — "
                    f"{max(1, p.rise_month - 1)}월쯤 준비하세요")
    return p

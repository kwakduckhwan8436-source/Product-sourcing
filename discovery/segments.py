"""
discovery.segments
=================
누가 언제 찾나 — 성별/기기별 '시즌 모양' 비교.

[할 수 있는 것] 세그먼트별로 '언제 검색이 오르는가'(피크 월)를 비교.
[할 수 없는 것] '누가 더 많이 검색하나'(비중). 데이터랩이 요청마다 자기
  최댓값을 100으로 정규화하기 때문 — 따로 부른 두 결과의 크기는 비교 불가.
  이 한계를 숨기지 않고 화면에도 적는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Segment:
    label: str
    peak_month: int = 0
    slope: float = 0.0
    found: bool = False
    note: str = ""


@dataclass(slots=True)
class SegmentReport:
    segments: list[Segment] = field(default_factory=list)
    insight: str = ""
    limit_note: str = ("데이터랩은 요청마다 따로 정규화합니다 — "
                       "'누가 더 많이 찾나'는 알 수 없고, "
                       "'언제 찾나'만 비교할 수 있어요.")


def _peak_month(trend) -> int:
    pts = list(getattr(trend, "points", None) or [])
    if not pts:
        return 0
    i = max(range(len(pts)), key=lambda k: pts[k])
    periods = list(getattr(trend, "periods", None) or [])
    if i < len(periods):
        try:
            return int(str(periods[i]).split("-")[1])
        except (IndexError, ValueError):
            return 0
    return 0


def build_report(named_trends: list[tuple[str, object]]) -> SegmentReport:
    """[(라벨, DemandTrend)] → 세그먼트 비교."""
    r = SegmentReport()
    for label, t in named_trends:
        if t is None or not getattr(t, "found", False):
            r.segments.append(Segment(label=label, found=False,
                                      note="데이터 없음"))
            continue
        pm = _peak_month(t)
        sg = Segment(label=label, peak_month=pm,
                     slope=round(float(getattr(t, "slope", 0)), 2), found=True)
        rise = "오르는 중" if sg.slope > 0.3 else (
            "내리는 중" if sg.slope < -0.3 else "평평")
        sg.note = (f"{pm}월에 가장 많이 찾아요 · {rise}" if pm
                   else f"{rise}")
        r.segments.append(sg)

    ok = [s for s in r.segments if s.found and s.peak_month]
    if len(ok) >= 2:
        months = {s.peak_month for s in ok}
        if len(months) > 1:
            parts = " / ".join(f"{s.label}은 {s.peak_month}월" for s in ok)
            r.insight = f"찾는 때가 갈려요 — {parts}. 시기별로 제목을 다르게 쓰세요"
        else:
            r.insight = (f"모두 {ok[0].peak_month}월에 몰려요 — "
                         f"그때를 겨냥해 미리 올려두세요")
    elif ok:
        r.insight = f"{ok[0].label}: {ok[0].note}"
    else:
        r.insight = "세그먼트 데이터를 받지 못했어요"
    return r

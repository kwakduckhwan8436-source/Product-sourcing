"""
discovery.providers.naver_demand
=================================
데이터랩 쇼핑인사이트 -> DemandTrend (level/slope 분해).

핵심: 데이터랩 시계열(상대 비율 0~100)을 두 성분으로 나눈다.
- level : 최근 구간 평균 수요 수준 -> 안정형 리스트
- slope : 최근 구간 선형회귀 기울기 -> 선점형 리스트
- volatility: 변동성 -> 안정형에서 감점

데이터랩은 카테고리 코드 단위로 동작하므로 cat_id 필수.
값은 '절대 검색량'이 아니라 기간 내 상대값(최대=100)이라, 방향/형태가 의미.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from discovery.base import DemandTrend
from discovery.providers.naver_client import NaverClient

logger = logging.getLogger(__name__)


def _linear_slope(ys: list[float]) -> float:
    """등간격 시계열의 최소제곱 기울기. x=0,1,2,... 가정."""
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def _stdev(ys: list[float]) -> float:
    n = len(ys)
    if n < 2:
        return 0.0
    m = sum(ys) / n
    return (sum((y - m) ** 2 for y in ys) / n) ** 0.5


class NaverDemandProvider:
    """DemandProvider 구현체."""

    def __init__(self, client: NaverClient, months_back: int = 12,
                 recent_window: int = 3, time_unit: str = "month"):
        self.client = client
        self.months_back = months_back
        self.recent_window = recent_window   # '최근 구간' 길이
        self.time_unit = time_unit

    def _date_range(self) -> tuple[str, str]:
        end = _dt.date.today()
        start = end - _dt.timedelta(days=30 * self.months_back)
        return start.isoformat(), end.isoformat()

    async def trend_of(self, keyword: str, cat_id: str) -> DemandTrend:
        start_date, end_date = self._date_range()
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "timeUnit": self.time_unit,
            "category": cat_id,
            "keyword": [{"name": keyword, "param": [keyword]}],
        }
        try:
            data = await self.client.datalab_shopping_keywords(body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("datalab 실패 (%s): %s", keyword, exc)
            return DemandTrend(keyword=keyword, found=False)

        points = self._extract_points(data)
        periods = self._extract_periods(data)
        if len(points) < 2:
            return DemandTrend(keyword=keyword, points=points, found=bool(points))

        recent = points[-self.recent_window:] if len(points) >= self.recent_window else points
        return DemandTrend(
            keyword=keyword,
            level=sum(recent) / len(recent),
            slope=_linear_slope(recent),
            volatility=_stdev(points),
            points=points,
            periods=periods,
            found=True,
        )

    @staticmethod
    def _extract_periods(data: dict[str, Any]) -> list[str]:
        """results[0].data[].period 실제 날짜 추출 — 시즌 계산에 필수.
        (이걸 버리면 '마지막 점 = 이번 달' 이라고 가정할 수밖에 없다.)"""
        results = data.get("results") or []
        if not results:
            return []
        out: list[str] = []
        for d in (results[0].get("data") or []):
            p = d.get("period")
            if p:
                out.append(str(p))
        return out

    @staticmethod
    def _extract_points(data: dict[str, Any]) -> list[float]:
        """results[0].data[].ratio 시계열 추출 (방어적)."""
        results = data.get("results") or []
        if not results:
            return []
        series = results[0].get("data") or []
        pts: list[float] = []
        for d in series:
            r = d.get("ratio")
            if r is not None:
                try:
                    pts.append(float(r))
                except (TypeError, ValueError):
                    continue
        return pts

    async def segment_trend(self, keyword: str, cat_id: str,
                            gender: str | None = None,
                            ages: list[str] | None = None,
                            device: str | None = None) -> DemandTrend:
        """
        성별/연령/기기로 잘라 본 수요 시계열.

        [중요 — 정직한 한계]
        데이터랩은 '요청마다' 자기 최댓값을 100으로 정규화한다. 따라서
        gender=f 와 gender=m 을 따로 불러 비교해도 '누가 더 많이 검색하나'는
        알 수 없다(둘 다 최댓값이 100). 비교할 수 있는 것은 '언제 검색하나'
        (시즌 모양)뿐이다. 이 메서드는 그 용도로만 써야 한다.
        """
        start_date, end_date = self._date_range()
        body: dict[str, Any] = {
            "startDate": start_date,
            "endDate": end_date,
            "timeUnit": self.time_unit,
            "category": cat_id,
            "keyword": [{"name": keyword, "param": [keyword]}],
        }
        if gender:
            body["gender"] = gender
        if ages:
            body["ages"] = ages
        if device:
            body["device"] = device
        try:
            data = await self.client.datalab_shopping_keywords(body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("datalab segment 실패 (%s): %s", keyword, exc)
            return DemandTrend(keyword=keyword, found=False)
        points = self._extract_points(data)
        periods = self._extract_periods(data)
        if len(points) < 2:
            return DemandTrend(keyword=keyword, found=False)
        recent = points[-self.recent_window:] or points
        return DemandTrend(
            keyword=keyword,
            level=sum(recent) / len(recent),
            slope=_linear_slope(recent),
            volatility=_stdev(points),
            points=points,
            periods=periods,
            found=True,
        )

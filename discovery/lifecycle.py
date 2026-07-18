"""
discovery.lifecycle
===================
시장 생애주기 분석 (시간축 경쟁 + 간접 수요 증거).

[핵심 발상] 한 시점의 경쟁 수(total)는 죽은 숫자. 시간축으로 보면
시장이 '뜨는 중'인지 '저무는 중'인지 보인다.

[간접 수요 증거] 신규 진입 속도 = 셀러들의 집단 투표.
등록 상품이 빠르게 느는 시장 = 셀러들이 "여기 돈 된다"고 판단해 몰리는 곳.
셀러는 안 팔리는 데 안 들어온다 → 진입 속도가 곧 실수요의 증거.
(리뷰·판매량을 크롤링 없이 간접 측정하는 합법적 우회)

[생애주기 4단계] 진입 속도 × 가격 추세로 판정:
  진입↑ + 가격유지 = 🟢 열리는 중 (골든타임, 들어갈 때)
  진입↑ + 가격하락 = 🔴 레드오션 진행 (이미 늦음, 치킨게임)
  진입정체 + 가격유지 = 🟡 성숙 안정 (틈새 굳어짐)
  진입감소 + 가격하락 = ⚫ 저무는 중 (나갈 때)

[한계] 이력이 며칠 쌓여야 작동. 데이터 1개면 판정 불가(UNKNOWN).
자동 발굴 루프와 짝을 이뤄 시간이 만들어주는 신호.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Lifecycle(str, Enum):
    OPENING = "🟢 열리는 중"     # 진입↑ 가격유지 — 골든타임
    REDOCEAN = "🔴 레드오션 진행"  # 진입↑ 가격↓ — 늦음
    MATURE = "🟡 성숙 안정"      # 진입정체 가격유지 — 틈새 굳음
    DECLINING = "⚫ 저무는 중"    # 진입↓ 가격↓ — 나갈 때
    UNKNOWN = "⏳ 데이터 부족"    # 이력 1개 이하


@dataclass(slots=True)
class LifecycleResult:
    keyword: str
    stage: Lifecycle = Lifecycle.UNKNOWN
    entry_rate_per_day: float = 0.0    # 하루 평균 신규 진입 (total 증가/일)
    entry_pct_per_day: float = 0.0     # 하루 평균 진입률 (%)
    price_trend_pct: float = 0.0       # 기간 전체 가격 변화율 (%)
    days_span: float = 0.0             # 분석 기간(일)
    samples: int = 0                   # 사용한 스냅샷 수
    bonus: float = 0.0                 # 점수에 더할 시간축 가점 (0~1)
    note: str = ""


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def analyze_lifecycle(history: list, *,
                      fast_entry_pct: float = 1.0,
                      price_drop_pct: float = -5.0) -> LifecycleResult:
    """
    이력(snapshots, 최신순 정렬된 sqlite Row 리스트)에서 생애주기 판정.

    파라미터(조절 가능):
    - fast_entry_pct: 하루 진입률 이 이상이면 '진입 빠름'으로 (기본 1%/일)
    - price_drop_pct: 기간 가격변화 이 이하면 '가격 하락'으로 (기본 -5%)
    """
    if not history or len(history) < 2:
        kw = history[0]["keyword"] if history else ""
        return LifecycleResult(keyword=kw, samples=len(history),
                               note="이력 부족 — 며칠 더 쌓이면 생애주기 판정 가능")

    kw = history[0]["keyword"]
    # 최신순으로 들어오므로 [0]=최신, [-1]=가장 오래됨
    newest, oldest = history[0], history[-1]
    t_new, t_old = _parse(newest["captured_at"]), _parse(oldest["captured_at"])
    if not t_new or not t_old:
        return LifecycleResult(keyword=kw, samples=len(history),
                               note="타임스탬프 파싱 불가")

    days = max((t_new - t_old).total_seconds() / 86400, 1e-6)
    res = LifecycleResult(keyword=kw, days_span=round(days, 1),
                          samples=len(history))

    # 진입 속도: (최신 total - 오래된 total) / 기간
    n_new = newest["total"] or 0
    n_old = oldest["total"] or 0
    res.entry_rate_per_day = (n_new - n_old) / days
    if n_old > 0:
        res.entry_pct_per_day = (n_new - n_old) / n_old / days * 100

    # 가격 추세: 기간 전체 변화율
    p_new, p_old = newest["price_median"], oldest["price_median"]
    if p_old and p_new:
        res.price_trend_pct = (p_new - p_old) / p_old * 100

    # === 4단계 판정 ===
    fast_entry = res.entry_pct_per_day >= fast_entry_pct
    price_dropping = res.price_trend_pct <= price_drop_pct

    if fast_entry and not price_dropping:
        res.stage = Lifecycle.OPENING       # 골든타임
        res.bonus = 0.5
    elif fast_entry and price_dropping:
        res.stage = Lifecycle.REDOCEAN      # 늦음
        res.bonus = 0.0
    elif not fast_entry and not price_dropping:
        res.stage = Lifecycle.MATURE        # 안정 틈새
        res.bonus = 0.2
    else:
        res.stage = Lifecycle.DECLINING     # 저무는 중
        res.bonus = 0.0

    res.note = (f"{res.days_span}일간 진입 {res.entry_pct_per_day:+.1f}%/일, "
                f"가격 {res.price_trend_pct:+.1f}% → {res.stage.value}")
    return res

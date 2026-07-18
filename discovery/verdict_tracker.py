"""
discovery.verdict_tracker
========================
판정 추적 + 자동 채점 (신빙성의 핵심).

[목적] "이 도구의 판정을 믿어도 되나?"에 답하기 위해, 도구가 내린
블루오션 판정을 기록하고 시간이 지나 채점한다. 적중률이 신빙성의 증거.

[연속 가동 불필요] 주말에 발굴하면 판정 기록 → 다음 주말에 다시 열면
일주일 지난 판정들이 자동 채점됨. 두 시점 스냅샷만 있으면 되므로
평일 내내 켜둘 필요 없음.

[채점 기준] 블루오션 판정의 본질 = "셀러 안 몰리고 가격 유지된다".
일주일 후:
  - 등록 급증(+50% 이상) = 남들도 몰려왔다 → 빗나감(블루 아니었음)
  - 가격 급락(-10% 이상) = 치킨게임 시작 → 빗나감
  - 둘 다 안정 = 판정 적중(여전히 블루오션)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


# 채점 임계값 (조절 가능)
_GRADE_AFTER_DAYS = 5          # 판정 후 이 일수 지나야 채점
_ENTRY_SURGE_PCT = 50.0        # 등록 이만큼 늘면 '남들 몰림' = 빗나감
_PRICE_DROP_PCT = -10.0        # 가격 이만큼 빠지면 '치킨게임' = 빗나감


@dataclass(slots=True)
class HitRate:
    total_graded: int = 0
    hits: int = 0
    pending: int = 0          # 아직 채점 안 된 판정 수

    @property
    def pct(self) -> float:
        return (self.hits / self.total_graded * 100) if self.total_graded else 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_between(a: str, b: str) -> float:
    try:
        ta = datetime.fromisoformat(a)
        tb = datetime.fromisoformat(b)
        return abs((tb - ta).total_seconds()) / 86400
    except (ValueError, TypeError):
        return 0.0


def record_verdict(conn: sqlite3.Connection, keyword: str, verdict: str,
                   total: int, price: float | None, score: float) -> None:
    """블루오션 판정을 기록 (나중에 채점하기 위해)."""
    # 같은 키워드의 '미채점' 판정이 최근에 있으면 중복 기록 안 함
    cur = conn.execute(
        "SELECT predicted_at FROM verdicts WHERE keyword=? AND graded_at IS NULL "
        "ORDER BY predicted_at DESC LIMIT 1", (keyword,))
    row = cur.fetchone()
    if row and _days_between(row[0], _now()) < _GRADE_AFTER_DAYS:
        return  # 최근 미채점 판정 있음 → 중복 방지
    conn.execute(
        "INSERT INTO verdicts(keyword, predicted_at, verdict, total_then, "
        "price_then, score_then) VALUES (?,?,?,?,?,?)",
        (keyword, _now(), verdict, total, price, score))
    conn.commit()


def grade_pending(conn: sqlite3.Connection, keyword: str,
                  total_now: int, price_now: float | None) -> int:
    """
    이 키워드의 미채점 판정 중 충분히 시간이 지난 것을 채점.
    반환: 이번에 채점한 개수.
    """
    cur = conn.execute(
        "SELECT id, predicted_at, total_then, price_then FROM verdicts "
        "WHERE keyword=? AND graded_at IS NULL", (keyword,))
    rows = cur.fetchall()
    graded = 0
    now = _now()
    for vid, pred_at, total_then, price_then in rows:
        if _days_between(pred_at, now) < _GRADE_AFTER_DAYS:
            continue  # 아직 이름

        hit = 1
        notes = []
        # 등록 급증 체크
        if total_then and total_then > 0:
            entry_pct = (total_now - total_then) / total_then * 100
            if entry_pct >= _ENTRY_SURGE_PCT:
                hit = 0
                notes.append(f"등록 +{entry_pct:.0f}%(남들 몰림)")
        # 가격 급락 체크
        if price_then and price_now and price_then > 0:
            price_pct = (price_now - price_then) / price_then * 100
            if price_pct <= _PRICE_DROP_PCT:
                hit = 0
                notes.append(f"가격 {price_pct:.0f}%(치킨게임)")
        if hit == 1:
            notes.append("셀러·가격 안정 — 판정 적중")

        conn.execute(
            "UPDATE verdicts SET graded_at=?, hit=?, grade_note=? WHERE id=?",
            (now, hit, " / ".join(notes), vid))
        graded += 1
    if graded:
        conn.commit()
    return graded


def hit_rate(conn: sqlite3.Connection, verdict: str = "blue") -> HitRate:
    """전체 적중률 — 도구 판정의 신빙성 지표."""
    cur = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE hit IS NOT NULL), "
        "COUNT(*) FILTER (WHERE hit=1), "
        "COUNT(*) FILTER (WHERE hit IS NULL) "
        "FROM verdicts WHERE verdict=?", (verdict,))
    total, hits, pending = cur.fetchone()
    return HitRate(total_graded=total or 0, hits=hits or 0, pending=pending or 0)


def keyword_track(conn: sqlite3.Connection, keyword: str) -> list[sqlite3.Row]:
    """한 키워드의 판정 추적 이력 (적중/빗나감)."""
    cur = conn.execute(
        "SELECT * FROM verdicts WHERE keyword=? ORDER BY predicted_at DESC",
        (keyword,))
    return cur.fetchall()

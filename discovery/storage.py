"""
discovery.storage
=================
발굴 결과 영속화 + 추세 추적 (3순위 고도화).

[목적]
발굴은 1회성이 아니라 시간에 따른 변화가 핵심.
- 매 발굴을 SQLite 에 스냅샷 저장 (키워드, 날짜, total, 점수, 가격중앙값)
- 재발굴 시 직전 기록과 비교: 경쟁 늘었나? 가격 떨어졌나? 점수 올랐나?
- "지난 발굴 대비 경쟁 급증" 같은 변화 신호 산출

[설계]
- 표준 라이브러리 sqlite3 만 사용 (의존성 추가 0)
- 기존 발굴 흐름은 건드리지 않음. 저장/조회는 선택적 호출.
- DB 파일은 기본 discovery/data/history.sqlite
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from discovery.base import DiscoveryScore, ShopMarket

_DEFAULT_DB = "discovery/data/history.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword       TEXT NOT NULL,
    captured_at   TEXT NOT NULL,        -- ISO8601 UTC
    total         INTEGER,
    stable_score  REAL,
    emerging_score REAL,
    price_median  REAL,
    seller_conc   REAL,
    bundle_ratio  REAL
);
CREATE INDEX IF NOT EXISTS idx_kw_time
    ON snapshots(keyword, captured_at);

CREATE TABLE IF NOT EXISTS watchlist (
    keyword     TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT '검토중',  -- 검토중/보류/사입결정/제외
    memo        TEXT DEFAULT '',
    added_at    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verdicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword       TEXT NOT NULL,
    predicted_at  TEXT NOT NULL,        -- 판정 시점
    verdict       TEXT NOT NULL,        -- 'blue'(블루오션이라 판정) 등
    total_then    INTEGER,              -- 판정 시 등록 상품 수
    price_then    REAL,                 -- 판정 시 가격중앙
    score_then    REAL,                 -- 판정 시 점수
    graded_at     TEXT,                 -- 채점된 시점 (NULL=미채점)
    hit           INTEGER,              -- 1=적중, 0=빗나감, NULL=미채점
    grade_note    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_verdict_kw
    ON verdicts(keyword, predicted_at);
"""


@dataclass(slots=True)
class TrendDelta:
    """직전 스냅샷 대비 변화."""
    keyword: str
    has_prev: bool = False
    prev_at: str | None = None
    total_delta: int = 0          # 경쟁(등록수) 증감
    total_pct: float = 0.0        # 증감률 %
    price_delta_pct: float = 0.0  # 가격중앙값 증감률 %
    score_delta: float = 0.0      # 안정형 점수 증감

    @property
    def signal(self) -> str:
        """사람이 읽을 변화 신호. 카드에 표시용."""
        if not self.has_prev:
            return "신규(이력없음)"
        parts = []
        if self.total_pct >= 20:
            parts.append(f"경쟁 급증 +{self.total_pct:.0f}%")
        elif self.total_pct <= -20:
            parts.append(f"경쟁 감소 {self.total_pct:.0f}%")
        if self.price_delta_pct <= -10:
            parts.append(f"가격 하락 {self.price_delta_pct:.0f}%(출혈?)")
        elif self.price_delta_pct >= 10:
            parts.append(f"가격 상승 +{self.price_delta_pct:.0f}%")
        if self.score_delta >= 0.05:
            parts.append(f"점수↑ +{self.score_delta:.2f}")
        elif self.score_delta <= -0.05:
            parts.append(f"점수↓ {self.score_delta:.2f}")
        return " · ".join(parts) if parts else "큰 변화 없음"


class DiscoveryStore:
    def __init__(self, db_path: str | Path = _DEFAULT_DB):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DiscoveryStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def _price_median(market: ShopMarket | None) -> float | None:
        if market and market.lprices:
            return float(median(market.lprices))
        return None

    def latest_before_now(self, keyword: str) -> sqlite3.Row | None:
        """이번 저장 전, 해당 키워드의 가장 최근 스냅샷."""
        cur = self._conn.execute(
            "SELECT * FROM snapshots WHERE keyword=? "
            "ORDER BY captured_at DESC LIMIT 1", (keyword,))
        return cur.fetchone()

    def compute_delta(self, score: DiscoveryScore,
                      market: ShopMarket | None) -> TrendDelta:
        """저장 '전에' 호출 — 직전 기록과 현재를 비교해 변화 산출."""
        prev = self.latest_before_now(score.keyword)
        if prev is None:
            return TrendDelta(keyword=score.keyword, has_prev=False)

        d = TrendDelta(keyword=score.keyword, has_prev=True,
                       prev_at=prev["captured_at"])
        # 경쟁 증감
        if prev["total"]:
            d.total_delta = score.total - prev["total"]
            d.total_pct = d.total_delta / prev["total"] * 100
        # 가격 증감
        cur_med = self._price_median(market)
        if prev["price_median"] and cur_med:
            d.price_delta_pct = (cur_med - prev["price_median"]) / prev["price_median"] * 100
        # 점수 증감
        if prev["stable_score"] is not None:
            d.score_delta = score.stable_score - prev["stable_score"]
        return d

    def save(self, score: DiscoveryScore, market: ShopMarket | None = None,
             captured_at: str | None = None) -> None:
        """현재 발굴 스냅샷 저장."""
        ts = captured_at or datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO snapshots(keyword, captured_at, total, stable_score, "
            "emerging_score, price_median, seller_conc, bundle_ratio) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (score.keyword, ts, score.total, score.stable_score,
             score.emerging_score, self._price_median(market),
             score.seller_concentration, score.bundle_ratio))
        self._conn.commit()

    def save_batch(self, scores: list[DiscoveryScore],
                   markets: dict[str, ShopMarket] | None = None
                   ) -> dict[str, TrendDelta]:
        """
        여러 결과를 저장하고, 각 키워드의 변화(저장 전 기준)를 반환.
        markets: 키워드->ShopMarket (가격중앙값 계산용, 선택).
        저장 후 각 score 에 시간축 생애주기를 주입(이력 기반).
        """
        markets = markets or {}
        deltas: dict[str, TrendDelta] = {}
        # 먼저 전부 delta 계산 (저장 전 직전 기록 기준)
        for s in scores:
            deltas[s.keyword] = self.compute_delta(s, markets.get(s.keyword))
        # 그다음 일괄 저장
        for s in scores:
            self.save(s, markets.get(s.keyword))
        # 저장 후 이력으로 생애주기 판정해 주입
        self._attach_lifecycle(scores)
        # 판정 추적 + 자동 채점 (신빙성) — 과거 판정 채점 후 이번 판정 기록
        try:
            self.track_verdicts(scores, markets)
        except Exception:  # noqa: BLE001
            pass  # 추적 실패가 발굴을 막지 않게
        return deltas

    def track_verdicts(self, scores: list[DiscoveryScore],
                       markets: dict | None = None,
                       blue_threshold: float = 0.5) -> dict:
        """
        판정 추적 + 자동 채점 (신빙성).
        1) 이 키워드들의 과거 미채점 판정을 현재 상태로 채점.
        2) 이번에 블루오션(점수 높음)인 것을 새 판정으로 기록.
        """
        from discovery import verdict_tracker as vt
        markets = markets or {}
        graded = recorded = 0
        for s in scores:
            mk = markets.get(s.keyword)
            price_now = mk.price_mean if mk else None
            graded += vt.grade_pending(self._conn, s.keyword, s.total, price_now)
            if s.stable_score >= blue_threshold or s.emerging_score >= blue_threshold:
                vt.record_verdict(self._conn, s.keyword, "blue",
                                  s.total, price_now, s.stable_score)
                recorded += 1
        return {"graded": graded, "recorded": recorded}

    def get_hit_rate(self):
        """도구 판정 적중률 (신빙성 지표)."""
        from discovery import verdict_tracker as vt
        return vt.hit_rate(self._conn)

    def _attach_lifecycle(self, scores: list[DiscoveryScore]) -> None:
        """저장된 이력에서 각 키워드의 시장 생애주기를 계산해 주입."""
        from discovery.lifecycle import analyze_lifecycle
        for s in scores:
            hist = self.history_of(s.keyword, limit=30)
            lc = analyze_lifecycle(hist)
            s.lifecycle_stage = lc.stage.value
            s.lifecycle_note = lc.note
            s.entry_pct_per_day = round(lc.entry_pct_per_day, 2)
            # 시간축 가점을 점수에 반영 (골든타임이면 가점)
            if lc.bonus > 0:
                # 기존 점수에 시간축 보너스를 더함 (최대 1로 클램프)
                s.stable_score = min(1.0, s.stable_score + lc.bonus * 0.3)
                s.emerging_score = min(1.0, s.emerging_score + lc.bonus * 0.3)

    def history_of(self, keyword: str, limit: int = 30) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM snapshots WHERE keyword=? "
            "ORDER BY captured_at DESC LIMIT ?", (keyword, limit))
        return cur.fetchall()

    # === 관심 키워드 관리 (신규 기능 3) ===
    def add_watch(self, keyword: str, status: str = "검토중",
                  memo: str = "") -> None:
        """관심 키워드 추가/갱신 (있으면 상태·메모 업데이트)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO watchlist(keyword, status, memo, added_at, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(keyword) DO UPDATE SET status=?, memo=?, updated_at=?",
            (keyword, status, memo, now, now, status, memo, now))
        self._conn.commit()

    def remove_watch(self, keyword: str) -> None:
        self._conn.execute("DELETE FROM watchlist WHERE keyword=?", (keyword,))
        self._conn.commit()

    def list_watch(self) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM watchlist ORDER BY updated_at DESC")
        return cur.fetchall()

    def is_watched(self, keyword: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM watchlist WHERE keyword=? LIMIT 1", (keyword,))
        return cur.fetchone() is not None

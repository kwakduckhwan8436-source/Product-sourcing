"""
discovery.tracker
================
시간이 쌓여야 나오는 무기 두 개.

  ② 골든타임 지수 — 수요는 느는데 셀러는 안 늘면 = 지금 들어가라
  ③ 최저가 붕괴 감시 — 최저가가 계속 빠지면 = 치킨게임, 들어가지 마

[왜 강력한가]
한 시점 스냅샷으로는 '지금 붐비나'만 안다. 두 시점을 비교하면 '어디로 가는
중인가'를 안다. 남들은 현재만 보고 들어오지만, 이건 방향을 보고 들어간다.

[연속 가동 불필요]
두 시점 스냅샷만 있으면 된다. 주말에만 켜도 주 단위로 쌓인다.

[정직한 한계]
- 최소 2회, 의미 있으려면 3~4회는 봐야 한다. 첫 실행엔 아무것도 안 나온다.
- 수요는 데이터랩 '검색량'이지 판매량이 아니다.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# 배포 환경에서는 디스크 경로가 다르다.
# Render 무료 플랜은 디스크가 날아가므로(재배포마다 초기화) 자료를 계속
# 쌓으려면 유료 Disk 를 붙이고 SOURCING_DB 로 그 경로를 지정해야 한다.
def _resolve_db() -> Path:
    """SOURCING_DB 가 있으면 쓰되, 그 경로를 만들 수 없으면(디스크 미장착 등)
    앱이 죽지 않게 로컬 파일로 자동 폴백한다."""
    env = os.environ.get("SOURCING_DB")
    if env:
        p = Path(env)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            t = p.parent / ".w_test"
            t.write_text("ok", encoding="utf-8")
            t.unlink(missing_ok=True)
            return p
        except Exception:  # noqa: BLE001
            pass                                   # 못 쓰면 아래 로컬로
    p = Path(__file__).resolve().parent.parent / "market_history.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_DB = _resolve_db()
_MIN_GAP_DAYS = 4.0        # 이만큼은 지나야 '변화'로 본다
_CRASH_PCT = -7.0          # 최저가가 이만큼 빠지면 붕괴 신호
_SURGE_PCT = 40.0          # 셀러가 이만큼 늘면 몰려온 것

_GRADE_AFTER_DAYS = 5.0    # 판정 후 이만큼 지나야 채점
_HIT_SURGE = 50.0          # 셀러가 이만큼 늘면 '빗나감'(남들이 몰림)
_HIT_CRASH = -10.0         # 최저가가 이만큼 빠지면 '빗나감'(치킨게임)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL,
    said_at     TEXT NOT NULL,
    verdict     TEXT NOT NULL,      -- '빈자리' 등 도구가 한 말
    total_then  INTEGER,
    price_then  INTEGER,
    graded_at   TEXT,
    hit         INTEGER,            -- 1 적중 / 0 빗나감 / NULL 미채점
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_vd ON verdicts(keyword, said_at);

CREATE TABLE IF NOT EXISTS watch (
    owner       TEXT NOT NULL DEFAULT 'local',
    keyword     TEXT NOT NULL,
    payload     TEXT NOT NULL,       -- 카드 내용 통째로 (JSON)
    memo        TEXT DEFAULT '',
    added_at    TEXT NOT NULL,
    PRIMARY KEY (owner, keyword)
);
-- owner 가 없으면 카페 회원 100명의 관심목록이 전부 뒤섞인다.
-- A 가 담은 걸 B 가 보고, B 가 지우면 A 것도 사라진다.
-- (market_snap·verdicts·pool 은 공유가 맞다 — 시장 정보는 모두의 것이고
--  여럿이 볼수록 골든타임·적중률·키워드풀이 정확해진다)

CREATE TABLE IF NOT EXISTS pool (
    keyword     TEXT PRIMARY KEY,
    category    TEXT,
    source      TEXT,               -- 어디서 캤나 (제목채굴/조합/씨앗)
    added_at    TEXT NOT NULL,
    scanned_at  TEXT                -- 마지막으로 본 때 (NULL=아직 안 봄)
);
CREATE INDEX IF NOT EXISTS idx_pool ON pool(category, scanned_at);

CREATE TABLE IF NOT EXISTS market_snap (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    total       INTEGER,
    price_min   INTEGER,
    demand      REAL
);
CREATE INDEX IF NOT EXISTS idx_snap ON market_snap(keyword, seen_at);
CREATE TABLE IF NOT EXISTS backtest (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL,
    category    TEXT,
    grade       TEXT,
    rise        REAL,
    logged_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bt ON backtest(keyword, logged_at);
CREATE TABLE IF NOT EXISTS meta (
    k    TEXT PRIMARY KEY,
    val  TEXT
);
"""


def backtest_log(rows: list, path: str | None = None) -> int:
    """A급 판정을 기록한다. rows=[{keyword,category,grade,rise}]. 같은 키워드는
    하루 1회만 남긴다(중복 방지). 기록한 건수를 반환."""
    import datetime as _dt
    if not rows:
        return 0
    today = _dt.date.today().isoformat()
    c = _conn(path)
    n = 0
    try:
        for r in rows:
            kw = (r.get("keyword") or "").strip()
            if not kw:
                continue
            dup = c.execute(
                "SELECT 1 FROM backtest WHERE keyword=? AND substr(logged_at,1,10)=?",
                (kw, today)).fetchone()
            if dup:
                continue
            c.execute(
                "INSERT INTO backtest(keyword,category,grade,rise,logged_at) "
                "VALUES(?,?,?,?,?)",
                (kw, r.get("category", ""), r.get("grade", ""),
                 float(r.get("rise") or 0), _dt.datetime.now().isoformat(timespec="seconds")))
            n += 1
        c.commit()
    finally:
        c.close()
    return n


def backtest_pending(days: int = 7, path: str | None = None) -> list:
    """기록된 지 N일 이상 지난 항목(검증 대상)을 돌려준다."""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).isoformat()
    c = _conn(path)
    try:
        rows = c.execute(
            "SELECT keyword,category,grade,rise,logged_at FROM backtest "
            "WHERE logged_at <= ? ORDER BY logged_at DESC LIMIT 40",
            (cutoff,)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def api_bump(n: int = 1, path: str | None = None) -> None:
    """이번 달 API 호출 수를 n 만큼 올린다(meta 에 'apicnt_YYYY-MM' 키로 누적).
    네이버 허브 월 한도(기본 3만)를 넘기지 않게 사용량을 추적하는 용도."""
    import datetime as _dt
    key = "apicnt_" + _dt.date.today().strftime("%Y-%m")
    try:
        c = _conn(path)
        try:
            row = c.execute("SELECT val FROM meta WHERE k=?", (key,)).fetchone()
            cur = int(row["val"]) if row else 0
            c.execute("INSERT INTO meta(k,val) VALUES(?,?) "
                      "ON CONFLICT(k) DO UPDATE SET val=excluded.val",
                      (key, str(cur + n)))
            c.commit()
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        pass


def api_usage(path: str | None = None) -> dict:
    """이번 달 호출 수/한도/남은 비율을 돌려준다."""
    import datetime as _dt
    key = "apicnt_" + _dt.date.today().strftime("%Y-%m")
    limit = 25000                       # 월 한도(안전 마진 두고 보수적으로)
    used = 0
    try:
        c = _conn(path)
        try:
            row = c.execute("SELECT val FROM meta WHERE k=?", (key,)).fetchone()
            used = int(row["val"]) if row else 0
            lr = c.execute("SELECT val FROM meta WHERE k='api_limit'").fetchone()
            if lr:
                limit = int(lr["val"])
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        pass
    pct = round(used / limit * 100) if limit else 0
    return {"month": _dt.date.today().strftime("%Y-%m"),
            "used": used, "limit": limit, "pct": pct,
            "warn": pct >= 80, "over": pct >= 100}


def calib_get(path: str | None = None) -> int:
    """A급 신뢰도 하한 보정치(기본 75). 백테스트 적중률로 자동 조정된다."""
    c = _conn(path)
    try:
        row = c.execute("SELECT val FROM meta WHERE k='a_min_conf'").fetchone()
        return int(row["val"]) if row else 75
    except Exception:  # noqa: BLE001
        return 75
    finally:
        c.close()


def calib_set_from_hitrate(hit_rate: int, total: int, path: str | None = None) -> int:
    """백테스트 적중률로 A급 신뢰도 하한을 조정한다(표본 5건+ 일 때만).
    적중률 낮으면 기준 상향(엄격), 아주 높으면 소폭 하향. 65~85 범위."""
    if total < 5:
        return calib_get(path)
    cur = calib_get(path)
    if hit_rate < 50:
        new = min(85, cur + 5)
    elif hit_rate < 65:
        new = min(85, cur + 2)
    elif hit_rate >= 85:
        new = max(70, cur - 2)
    else:
        new = cur
    if new != cur:
        c = _conn(path)
        try:
            c.execute("INSERT INTO meta(k,val) VALUES('a_min_conf',?) "
                      "ON CONFLICT(k) DO UPDATE SET val=excluded.val", (str(new),))
            c.commit()
        finally:
            c.close()
    return new


@dataclass(slots=True)
class Movement:
    keyword: str = ""
    points: int = 0            # 쌓인 스냅샷 수
    days: float = 0.0          # 처음~최근 간격
    demand_change: float = 0.0  # 수요 변화율(%)
    total_change: float = 0.0   # 셀러 변화율(%)
    price_change: float = 0.0   # 최저가 변화율(%)
    golden: float = 0.0        # 골든타임 지수 (수요증가 - 셀러증가)
    stage: str = ""            # 골든타임 / 몰리는중 / 치킨게임 / 조용함 / 데이터부족
    note: str = ""
    action: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate(c: sqlite3.Connection) -> None:
    """
    예전 DB 를 새 구조로 옮긴다.

    [왜] 이미 쓰던 사람의 watch 테이블엔 owner 칸이 없다. 그대로 두면
    '컬럼이 없다' 며 관심목록이 통째로 죽는다. 쓰던 자료를 날리지 않고 옮긴다.
    """
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(watch)").fetchall()}
    except Exception:  # noqa: BLE001
        return
    if not cols or "owner" in cols:
        return
    try:
        c.execute("ALTER TABLE watch RENAME TO watch_old")
        c.executescript(_SCHEMA)
        c.execute("INSERT OR IGNORE INTO watch(owner, keyword, payload, memo,"
                  " added_at) SELECT 'local', keyword, payload,"
                  " COALESCE(memo,''), added_at FROM watch_old")
        c.execute("DROP TABLE watch_old")
        c.commit()
    except Exception:  # noqa: BLE001
        pass


def _conn(path: str | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(path or _DB)
    c.row_factory = sqlite3.Row
    # 이사를 먼저 한다. _SCHEMA 는 'IF NOT EXISTS' 라서 예전 표가 있으면
    # 새 구조가 적용되지 않는다 — 먼저 옮기고 나서 만들어야 한다.
    _migrate(c)
    c.executescript(_SCHEMA)
    return c


def record(keyword: str, total: int, price_min: int, demand: float = 0.0,
           path: str | None = None) -> None:
    """이번에 본 시장 상태를 남긴다 (나중에 방향을 보기 위해)."""
    c = _conn(path)
    try:
        c.execute("INSERT INTO market_snap(keyword, seen_at, total, price_min,"
                  " demand) VALUES (?,?,?,?,?)",
                  (keyword, _now(), int(total or 0), int(price_min or 0),
                   float(demand or 0)))
        c.commit()
    finally:
        c.close()


def _days_between(a: str, b: str) -> float:
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a))
                   .total_seconds()) / 86400
    except (ValueError, TypeError):
        return 0.0


def movement_of(keyword: str, path: str | None = None) -> Movement:
    """처음 본 때 vs 지금 → 이 시장이 어디로 가는 중인가."""
    m = Movement(keyword=keyword)
    c = _conn(path)
    try:
        rows = c.execute(
            "SELECT * FROM market_snap WHERE keyword=? ORDER BY seen_at",
            (keyword,)).fetchall()
    finally:
        c.close()
    m.points = len(rows)
    if len(rows) < 2:
        m.stage = "데이터부족"
        m.note = "이번이 처음이에요 — 다음에 또 보면 어디로 가는지 알려드려요"
        m.action = "주말마다 한 번씩 눌러주세요. 2~3번이면 방향이 보입니다"
        return m

    first, last = rows[0], rows[-1]
    m.days = _days_between(first["seen_at"], last["seen_at"])
    if m.days < _MIN_GAP_DAYS:
        m.stage = "데이터부족"
        m.note = f"아직 {m.days:.0f}일치뿐이에요 — 변화를 보려면 며칠 더 필요해요"
        m.action = "다음 주말에 다시 확인해보세요"
        return m

    def _pct(a, b):
        a = float(a or 0)
        return round((float(b or 0) - a) / a * 100, 1) if a > 0 else 0.0

    m.total_change = _pct(first["total"], last["total"])
    m.price_change = _pct(first["price_min"], last["price_min"])
    m.demand_change = _pct(first["demand"], last["demand"])
    m.golden = round(m.demand_change - m.total_change, 1)

    # 판정 — 위험한 것부터
    if m.price_change <= _CRASH_PCT:
        m.stage = "치킨게임"
        m.note = (f"{m.days:.0f}일 만에 최저가가 {m.price_change:.0f}% 빠졌어요 "
                  f"— 서로 값 깎는 중입니다")
        m.action = "들어가지 마세요. 지금 들어가면 바닥까지 같이 내려갑니다"
    elif m.total_change >= _SURGE_PCT:
        m.stage = "몰리는중"
        m.note = (f"{m.days:.0f}일 만에 셀러가 {m.total_change:.0f}% 늘었어요 "
                  f"— 남들도 알아챘습니다")
        m.action = "늦었어요. 지금 들어가면 뒤에 서게 됩니다"
    elif m.golden >= 20 and m.demand_change > 0:
        m.stage = "골든타임"
        m.note = (f"수요는 {m.demand_change:.0f}% 느는데 셀러는 "
                  f"{m.total_change:.0f}%밖에 안 늘었어요")
        m.action = "지금이 자리예요. 남들이 알아채기 전입니다"
    elif m.demand_change <= -20:
        m.stage = "식는중"
        m.note = f"수요가 {m.demand_change:.0f}% 줄었어요 — 시들해지는 중입니다"
        m.action = "다른 걸 보세요"
    else:
        m.stage = "조용함"
        m.note = (f"{m.days:.0f}일 동안 큰 변화가 없어요 "
                  f"(수요 {m.demand_change:+.0f}% · 셀러 {m.total_change:+.0f}%)")
        m.action = "서두를 것도, 피할 것도 없어요"
    return m


def tracked_keywords(path: str | None = None) -> list[dict]:
    """추적 중인 키워드 목록 (몇 번 봤는지)."""
    c = _conn(path)
    try:
        rows = c.execute(
            "SELECT keyword, COUNT(*) n, MIN(seen_at) a, MAX(seen_at) b "
            "FROM market_snap GROUP BY keyword ORDER BY b DESC LIMIT 50"
        ).fetchall()
    finally:
        c.close()
    return [{"keyword": r["keyword"], "seen": r["n"],
             "days": round(_days_between(r["a"], r["b"]), 1)} for r in rows]


# ---------------------------------------------------------------- 판정 적중률
@dataclass(slots=True)
class HitRate:
    graded: int = 0
    hits: int = 0
    pending: int = 0

    @property
    def pct(self) -> float:
        return round(self.hits / self.graded * 100, 1) if self.graded else 0.0


def say_verdict(keyword: str, verdict: str, total: int, price_min: int,
                path: str | None = None) -> None:
    """도구가 '빈자리다' 라고 말한 순간을 기록해 둔다 (나중에 채점하려고)."""
    c = _conn(path)
    try:
        row = c.execute(
            "SELECT said_at FROM verdicts WHERE keyword=? AND graded_at IS NULL"
            " ORDER BY said_at DESC LIMIT 1", (keyword,)).fetchone()
        if row and _days_between(row["said_at"], _now()) < _GRADE_AFTER_DAYS:
            return                      # 최근에 이미 말했으면 중복 기록 안 함
        c.execute("INSERT INTO verdicts(keyword, said_at, verdict, total_then,"
                  " price_then) VALUES (?,?,?,?,?)",
                  (keyword, _now(), verdict, int(total or 0), int(price_min or 0)))
        c.commit()
    finally:
        c.close()


def grade_verdicts(keyword: str, total_now: int, price_now: int,
                   path: str | None = None) -> int:
    """
    시간이 지난 판정을 채점한다.
      셀러 급증(+50%) 또는 최저가 급락(-10%) → 빗나감
      둘 다 안정 → 적중
    """
    c = _conn(path)
    graded = 0
    try:
        rows = c.execute(
            "SELECT * FROM verdicts WHERE keyword=? AND graded_at IS NULL",
            (keyword,)).fetchall()
        now = _now()
        for r in rows:
            if _days_between(r["said_at"], now) < _GRADE_AFTER_DAYS:
                continue
            hit, notes = 1, []
            t0 = float(r["total_then"] or 0)
            p0 = float(r["price_then"] or 0)
            if t0 > 0:
                ch = (total_now - t0) / t0 * 100
                if ch >= _HIT_SURGE:
                    hit = 0
                    notes.append(f"셀러 +{ch:.0f}%")
            if p0 > 0 and price_now > 0:
                ch = (price_now - p0) / p0 * 100
                if ch <= _HIT_CRASH:
                    hit = 0
                    notes.append(f"최저가 {ch:.0f}%")
            if hit:
                notes.append("셀러·가격 안정")
            c.execute("UPDATE verdicts SET graded_at=?, hit=?, note=? WHERE id=?",
                      (now, hit, " / ".join(notes), r["id"]))
            graded += 1
        if graded:
            c.commit()
    finally:
        c.close()
    return graded


def hit_rate(path: str | None = None) -> HitRate:
    """이 도구가 '빈자리다' 라고 한 것 중 몇 %가 맞았나 — 신뢰의 유일한 증거."""
    c = _conn(path)
    try:
        r = c.execute(
            "SELECT COUNT(*) FILTER (WHERE hit IS NOT NULL) g,"
            " COUNT(*) FILTER (WHERE hit=1) h,"
            " COUNT(*) FILTER (WHERE hit IS NULL) p FROM verdicts").fetchone()
    finally:
        c.close()
    return HitRate(graded=r["g"] or 0, hits=r["h"] or 0, pending=r["p"] or 0)


# ------------------------------------------------- 키워드 풀 (눈덩이)
def add_pool(keywords, category: str = "", source: str = "제목채굴",
             path: str | None = None) -> int:
    """
    새로 캔 키워드를 풀에 쌓는다.

    [왜 필요한가] 씨앗이 6개로 고정이면 매번 같은 결과가 나온다. 그건 발굴이
    아니라 같은 목록을 다시 읽는 것이다. 상위 제목에서 캔 말을 풀에 넣어두면
    다음 회차는 새 땅을 판다 — 눈덩이처럼 넓어진다.
    """
    c = _conn(path)
    n = 0
    try:
        for kw in keywords:
            kw = (kw or "").strip()
            if len(kw) < 2:
                continue
            try:
                cur = c.execute(
                    "INSERT OR IGNORE INTO pool(keyword, category, source,"
                    " added_at) VALUES (?,?,?,?)",
                    (kw, category, source, _now()))
                n += cur.rowcount if cur.rowcount > 0 else 0
            except Exception:  # noqa: BLE001
                pass
        c.commit()
    finally:
        c.close()
    return n


def pool_keywords(category: str = "", limit: int = 40, fresh_days: float = 10.0,
                  path: str | None = None) -> list:
    """
    이번에 볼 키워드 — 아직 안 본 것 먼저, 그다음 오래된 것.
    최근에 본 건 건너뛴다 → 매번 다른 땅을 판다.
    """
    c = _conn(path)
    try:
        rows = c.execute(
            "SELECT keyword, scanned_at FROM pool"
            " WHERE (?='' OR category=?)"
            " ORDER BY (scanned_at IS NOT NULL), scanned_at ASC LIMIT ?",
            (category, category, limit * 3)).fetchall()
    finally:
        c.close()
    out = []
    now = _now()
    for r in rows:
        if r["scanned_at"] and _days_between(r["scanned_at"], now) < fresh_days:
            continue                      # 최근에 본 것은 건너뜀
        out.append(r["keyword"])
        if len(out) >= limit:
            break
    return out


def mark_scanned(keyword: str, path: str | None = None) -> None:
    c = _conn(path)
    try:
        c.execute("UPDATE pool SET scanned_at=? WHERE keyword=?", (_now(), keyword))
        c.commit()
    finally:
        c.close()


def pool_stats(category: str = "", path: str | None = None) -> dict:
    c = _conn(path)
    try:
        r = c.execute(
            "SELECT COUNT(*) t, COUNT(*) FILTER (WHERE scanned_at IS NULL) n"
            " FROM pool WHERE (?='' OR category=?)", (category, category)).fetchone()
    finally:
        c.close()
    return {"total": r["t"] or 0, "unseen": r["n"] or 0}


# ------------------------------------------------- 관심 목록
def watch_add(keyword: str, payload: dict, owner: str = "local",
              path: str | None = None) -> bool:
    """
    마음에 든 카드를 저장한다.

    [왜] 300개를 훑어 30개를 찾아놓고 새로고침 한 번에 다 날아갔다.
    가장 아까운 손해였다. 카드 내용을 통째로 남겨서 나중에 다시 본다.
    """
    import json as _j
    c = _conn(path)
    try:
        c.execute(
            "INSERT OR REPLACE INTO watch(owner, keyword, payload, memo, added_at)"
            " VALUES (?,?,?, COALESCE((SELECT memo FROM watch WHERE owner=?"
            " AND keyword=?),''), ?)",
            (owner, keyword, _j.dumps(payload, ensure_ascii=False),
             owner, keyword, _now()))
        c.commit()
        return True
    finally:
        c.close()


def watch_remove(keyword: str, owner: str = "local",
                 path: str | None = None) -> bool:
    c = _conn(path)
    try:
        c.execute("DELETE FROM watch WHERE owner=? AND keyword=?",
                  (owner, keyword))
        c.commit()
        return True
    finally:
        c.close()


def watch_list(owner: str = "local", path: str | None = None) -> list:
    import json as _j
    c = _conn(path)
    try:
        rows = c.execute(
            "SELECT * FROM watch WHERE owner=? ORDER BY added_at DESC",
            (owner,)).fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        try:
            d = _j.loads(r["payload"])
        except Exception:  # noqa: BLE001
            d = {"keyword": r["keyword"]}
        d["memo"] = r["memo"] or ""
        d["added_at"] = r["added_at"]
        out.append(d)
    return out


def watch_keywords(owner: str = "local", path: str | None = None) -> set:
    c = _conn(path)
    try:
        rows = c.execute("SELECT keyword FROM watch WHERE owner=?",
                         (owner,)).fetchall()
    finally:
        c.close()
    return {r["keyword"] for r in rows}

"""
discovery.orders
===============
⑤ 내 판매 기록 연동 — 도구 추천이 진짜 매출로 이어졌나.

[왜 이게 궁극인가]
적중률·골든타임 다 좋지만, 결국 물어야 할 건 하나다.
"이 도구가 찍어준 걸로 내가 돈을 벌었나?"
스마트스토어 주문 내역을 올리면, 도구가 추천했던 키워드와 대조한다.

[무가정 — 컬럼을 추측하지 않는다]
스마트스토어 CSV 의 정확한 컬럼명을 본 적이 없다. 그래서 이름을 고정하지
않고, 헤더에서 '상품명 같은 칸' 과 '수량/금액 같은 칸' 을 찾아낸다.
못 찾으면 억지로 맞추지 않고 "이 칸이 뭔지 알려달라" 고 정직하게 말한다.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

# 헤더에서 찾을 후보 (스마트스토어/쿠팡/엑셀 어느 것이든)
_NAME_HINTS = ("상품명", "상품 이름", "품명", "옵션명", "상품", "product")
_QTY_HINTS = ("수량", "판매수량", "주문수량", "구매수량", "qty", "quantity")
_AMT_HINTS = ("금액", "결제금액", "상품금액", "판매금액", "정산", "price", "amount")
_DATE_HINTS = ("일시", "날짜", "주문일", "결제일", "date")
_NUM = re.compile(r"[^0-9.-]")


@dataclass(slots=True)
class SoldItem:
    name: str
    qty: int = 0
    amount: int = 0


@dataclass(slots=True)
class OrderReport:
    ok: bool = False
    rows: int = 0
    name_col: str = ""
    qty_col: str = ""
    amt_col: str = ""
    items: list = field(default_factory=list)      # 많이 판 순
    total_qty: int = 0
    total_amount: int = 0
    matched: list = field(default_factory=list)    # 도구가 추천했던 것 중 판 것
    unmatched: list = field(default_factory=list)  # 도구가 못 찾아준 것
    hit_pct: float = 0.0
    note: str = ""


def _find_col(header: list, hints: tuple) -> str:
    for h in header:
        hl = str(h or "").strip().lower()
        for k in hints:
            if k.lower() in hl:
                return h
    return ""


def _to_int(v) -> int:
    try:
        return int(float(_NUM.sub("", str(v or "0")) or 0))
    except (ValueError, TypeError):
        return 0


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(s or "").lower())


def parse_orders(text: str) -> OrderReport:
    """주문 CSV 텍스트 → 무엇을 얼마나 팔았나."""
    r = OrderReport()
    if not text or not text.strip():
        r.note = "파일이 비어 있어요"
        return r
    # 구분자 추측 (콤마/탭)
    sample = text[:2000]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    try:
        rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
    except Exception as exc:  # noqa: BLE001
        r.note = f"CSV 를 읽지 못했어요: {exc}"
        return r
    if not rows:
        r.note = "내용이 없어요"
        return r

    header = list(rows[0].keys())
    r.name_col = _find_col(header, _NAME_HINTS)
    r.qty_col = _find_col(header, _QTY_HINTS)
    r.amt_col = _find_col(header, _AMT_HINTS)
    if not r.name_col:
        r.note = ("상품명 칸을 못 찾았어요. 이 파일의 칸 이름들: "
                  + ", ".join(str(h) for h in header[:12]))
        return r

    agg: dict = {}
    for row in rows:
        name = str(row.get(r.name_col) or "").strip()
        if not name:
            continue
        q = _to_int(row.get(r.qty_col)) if r.qty_col else 1
        a = _to_int(row.get(r.amt_col)) if r.amt_col else 0
        it = agg.setdefault(name, SoldItem(name=name))
        it.qty += max(1, q)
        it.amount += a
    r.rows = len(rows)
    r.items = sorted(agg.values(), key=lambda x: -x.qty)
    r.total_qty = sum(i.qty for i in r.items)
    r.total_amount = sum(i.amount for i in r.items)
    r.ok = True
    r.note = (f"{r.rows}줄에서 상품 {len(r.items)}종 · "
              f"총 {r.total_qty}개 판매를 읽었어요")
    return r


def match_with_tool(report: OrderReport, tool_keywords) -> OrderReport:
    """
    도구가 추천했던 키워드와 실제 판 상품을 대조.
    상품명에 그 키워드(공백 제거)가 들어 있으면 '도구가 찍어준 것' 으로 본다.
    """
    if not report.ok:
        return report
    keys = [(k, _norm(k)) for k in (tool_keywords or []) if k]
    for it in report.items:
        n = _norm(it.name)
        hit = None
        for orig, k in keys:
            if k and k in n:
                hit = orig
                break
        if hit:
            report.matched.append({"sold": it.name, "keyword": hit,
                                   "qty": it.qty, "amount": it.amount})
        else:
            report.unmatched.append({"sold": it.name, "qty": it.qty,
                                     "amount": it.amount})
    sold_qty = report.total_qty or 1
    hit_qty = sum(m["qty"] for m in report.matched)
    report.hit_pct = round(hit_qty / sold_qty * 100, 1)
    if not keys:
        report.note += " · 아직 도구가 추천한 기록이 없어 대조는 못 했어요"
    elif report.matched:
        report.note += (f" · 그중 {len(report.matched)}종은 도구가 찍어줬던 "
                        f"것이에요 (판매 수량의 {report.hit_pct:.0f}%)")
    else:
        report.note += " · 도구가 찍어준 것과 겹치는 게 없어요"
    return report

"""
discovery.sales
==============
⑤ 내 판매 기록 연동 — 도구 추천이 실제 매출로 이어졌나.

[왜 궁극인가]
지금까지 '적중률' 은 '셀러가 안 몰렸나' 로만 채점했다. 그건 대리 지표다.
진짜 답은 '내가 그걸 팔아서 돈을 벌었나' 뿐이다. 주문 기록을 대조하면
도구의 추천이 매출로 이어졌는지 처음으로 확인된다.

[무가정 — 스마트스토어 CSV 형식을 추측하지 않는다]
마켓마다, 시기마다 컬럼 이름이 다르다. 그래서 '상품명 비슷한 컬럼' 을
찾아내는 방식으로 유연하게 읽고, **무엇을 찾았는지 사용자에게 그대로 보고**
한다. 못 찾으면 억지로 짐작하지 않고 컬럼 목록을 보여주며 물어본다.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field

# 컬럼 이름 후보 (마켓마다 다르므로 넓게)
_NAME_HINTS = ("상품명", "상품 이름", "제품명", "옵션정보", "상품번호명",
               "product", "item", "name", "title")
_QTY_HINTS = ("수량", "주문수량", "판매수량", "qty", "quantity", "count")
_AMT_HINTS = ("금액", "결제금액", "판매금액", "상품금액", "정산", "총주문금액",
              "price", "amount", "total")
_DATE_HINTS = ("일시", "날짜", "주문일", "결제일", "date")

_SPLIT = re.compile(r"[^\w가-힣]+")


@dataclass(slots=True)
class SalesReport:
    ok: bool = False
    rows: int = 0
    name_col: str = ""
    qty_col: str = ""
    amt_col: str = ""
    columns: list = field(default_factory=list)
    products: list = field(default_factory=list)   # [{name, qty, amount}]
    matched: list = field(default_factory=list)    # 도구 추천과 겹친 것
    unmatched_picks: list = field(default_factory=list)
    total_qty: int = 0
    total_amt: int = 0
    note: str = ""


def _find_col(headers: list, hints: tuple) -> str:
    low = [(h or "").strip() for h in headers]
    for h in low:
        for k in hints:
            if k in h.lower() or k in h:
                return h
    return ""


def _to_int(v) -> int:
    try:
        return int(float(re.sub(r"[^\d.\-]", "", str(v)) or 0))
    except (ValueError, TypeError):
        return 0


def _read_rows(data: bytes) -> tuple:
    """엑셀에서 저장한 CSV 는 보통 cp949, 요즘은 utf-8-sig. 둘 다 시도."""
    for enc in ("utf-8-sig", "cp949", "utf-8", "euc-kr"):
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 구분자 자동 판별
        sample = text[:4000]
        delim = "\t" if sample.count("\t") > sample.count(",") else ","
        try:
            rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
        except Exception:  # noqa: BLE001
            continue
        if rows and rows[0]:
            return rows, enc
    return [], ""


def parse_sales(data: bytes) -> SalesReport:
    """주문 CSV → 상품별 판매 수량·금액."""
    r = SalesReport()
    rows, enc = _read_rows(data)
    if not rows:
        r.note = ("파일을 못 읽었어요. 스마트스토어 주문내역을 "
                  "CSV(또는 엑셀→CSV 저장) 로 올려주세요.")
        return r
    r.columns = [h for h in (rows[0].keys() or []) if h]
    r.name_col = _find_col(r.columns, _NAME_HINTS)
    r.qty_col = _find_col(r.columns, _QTY_HINTS)
    r.amt_col = _find_col(r.columns, _AMT_HINTS)
    if not r.name_col:
        r.note = ("상품명 컬럼을 못 찾았어요. 아래 컬럼 중 어느 것이 상품명인지 "
                  "알려주시면 맞추겠습니다.")
        return r

    agg: dict = {}
    for row in rows:
        name = (row.get(r.name_col) or "").strip()
        if not name:
            continue
        qty = _to_int(row.get(r.qty_col)) if r.qty_col else 1
        amt = _to_int(row.get(r.amt_col)) if r.amt_col else 0
        d = agg.setdefault(name, {"name": name, "qty": 0, "amount": 0})
        d["qty"] += max(1, qty)
        d["amount"] += amt
        r.rows += 1
    r.products = sorted(agg.values(), key=lambda x: -x["qty"])
    r.total_qty = sum(p["qty"] for p in r.products)
    r.total_amt = sum(p["amount"] for p in r.products)
    r.ok = True
    r.note = (f"{r.rows}건 · 상품 {len(r.products)}종 · "
              f"{r.total_qty}개 판매" +
              (f" · {r.total_amt:,}원" if r.total_amt else ""))
    return r


def _tokens(s: str) -> set:
    return {t for t in _SPLIT.split(s or "") if len(t) >= 2}


def match_picks(report: SalesReport, picks: list) -> SalesReport:
    """
    도구가 추천한 키워드와 실제 판매 상품을 대조.
    (제목이 완전히 같을 리 없으니 '핵심 단어가 겹치는가' 로 본다)
    """
    if not report.ok or not picks:
        return report
    for pick in picks:
        kw = pick.get("keyword") or ""
        ktok = _tokens(kw)
        if not ktok:
            continue
        best, best_hit = None, 0
        for p in report.products:
            hit = len(ktok & _tokens(p["name"]))
            if hit > best_hit:
                best, best_hit = p, hit
        # 핵심 단어가 2개 이상 겹쳐야 같은 물건으로 본다
        if best and best_hit >= 2:
            report.matched.append({
                "keyword": kw, "sold_name": best["name"],
                "qty": best["qty"], "amount": best["amount"],
                "hit": best_hit})
        else:
            report.unmatched_picks.append(kw)
    return report

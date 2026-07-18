"""
discovery.suppliers
==================
물건을 떼올 곳 — 위탁(드랍십)과 도매(사입)를 나눠서 연다.

[무가정 — 검색 주소를 지어내지 않는다]
사이트마다 검색 주소 규칙이 다르고, 개편되면 바뀐다. 내가 확실히 아는 곳만
직접 검색 주소를 쓰고, 확실하지 않은 곳은 '네이버로 그 사이트 찾기' 로 연다.
지어낸 주소를 넣으면 눌렀을 때 엉뚱한 데가 뜨고, 그건 도구를 못 믿게 만든다.

[왜 나눠야 하나]
  위탁 — 재고 없이 파는 곳. 공급가가 비싸지만 돈이 안 묶인다.
  도매 — 대량으로 싸게 떼는 곳. 마진이 크지만 재고를 떠안는다.
같은 물건이라도 어디서 떼느냐로 마진이 갈린다.
"""
from __future__ import annotations

from urllib.parse import quote

# kind: "direct" = 검색 주소가 확실한 곳 / "search" = 네이버로 찾아 들어가는 곳
_CONSIGN = [
    {"name": "오너클랜", "kind": "site",
     "url": "https://ownerclan.com/",
     "desc": "국내 최대급 위탁 도매. 상품 DB를 엑셀로 내려받을 수 있어요"},
    {"name": "도매매", "kind": "site",
     "url": "https://domemedb.domeggook.com/",
     "desc": "위탁 전용. 배송대행까지 해줍니다"},
    {"name": "온채널", "kind": "site",
     "url": "https://www.onch3.co.kr/",
     "desc": "위탁 상품 많고 마켓 연동이 편해요"},
    {"name": "셀러허브", "kind": "site",
     "url": "https://www.sellerhub.co.kr/",
     "desc": "여러 공급사를 한 곳에서"},
]

_WHOLESALE = [
    {"name": "도매꾹", "kind": "site",
     "url": "https://domeggook.com/",
     "desc": "국내 대량 도매. 최소수량이 있지만 단가가 쌉니다"},
    {"name": "1688 (알리바바 중국내수)", "kind": "direct",
     "url": "https://s.1688.com/selloffer/offer_search.htm?keywords={q}",
     "desc": "가장 싸지만 중국어·배송대행이 필요해요"},
    {"name": "알리익스프레스", "kind": "direct",
     "url": "https://ko.aliexpress.com/w/wholesale-{q}.html",
     "desc": "소량도 되고 한국어. 배송이 느립니다"},
    {"name": "신상마켓 (동대문)", "kind": "site",
     "url": "https://sinsangmarket.kr/",
     "desc": "의류 도매. 사업자 인증이 필요해요"},
    {"name": "타오바오", "kind": "site",
     "url": "https://world.taobao.com/",
     "desc": "종류가 가장 많음. 구매대행 필요"},
]

_NAVER_FIND = "https://search.naver.com/search.naver?query={q}"


def supplier_links(keyword: str, mode: str = "consign") -> list:
    """
    이 키워드를 떼올 만한 곳들.

    kind:
      direct — 그 사이트에서 바로 검색됨 (주소 규칙을 확실히 아는 곳)
      site   — 사이트만 열림. 안에서 직접 검색하세요
      find   — 네이버로 '사이트명 + 키워드' 를 찾아 들어감
    """
    q = quote((keyword or "").strip())
    src = _WHOLESALE if mode == "wholesale" else _CONSIGN
    out = []
    for s in src:
        if s["kind"] == "direct":
            url = s["url"].replace("{q}", q)
            hint = "바로 검색됩니다"
        else:
            url = s["url"]
            hint = "사이트가 열려요 — 안에서 검색하세요"
        out.append({"name": s["name"], "url": url, "desc": s["desc"],
                    "kind": s["kind"], "hint": hint,
                    "find": _NAVER_FIND.replace(
                        "{q}", quote(f"{s['name']} {keyword or ''}".strip()))})
    return out


def mode_tip(mode: str) -> str:
    if mode == "wholesale":
        return ("도매는 <b>최소수량(MOQ)</b>과 <b>배송대행비</b>를 꼭 물어보세요. "
                "단가만 싸고 부대비용에서 마진이 날아가는 일이 흔합니다.")
    return ("위탁은 <b>품절 여부</b>와 <b>출고일</b>을 꼭 확인하세요. "
            "팔렸는데 공급사에 재고가 없으면 페널티는 나에게 옵니다.")

"""
discovery.checklist
==================
④ 시작 전에 갖춰야 할 것 — 없으면 물건이 안 나오거나 불법이 된다.

[왜 이게 기능이어야 하나]
도구가 '이거 팔면 돼요' 라고 알려줘도, 통관고유부호가 없으면 물건이
세관에서 안 나온다. 통신판매업 신고 없이 팔면 과태료를 맞는다.
초보가 제일 많이 막히는 게 여기인데, 아무도 미리 안 알려준다.

[정직한 한계]
법과 절차는 바뀐다. 아래는 2026년 초 기준으로 알려진 내용이고,
반드시 해당 기관에서 최신 내용을 확인해야 한다. 도구는 '이런 게 필요하다' 를
알려주는 것이지 법률 자문이 아니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Step:
    title: str
    why: str
    how: str
    where: str = ""
    cost: str = ""
    when: str = ""      # 언제 필요한가


COMMON = [
    Step(title="사업자등록",
         why="이게 없으면 통신판매업 신고도, 세금계산서도, 도매 거래도 안 됩니다",
         how="홈택스에서 온라인으로. 처음엔 보통 간이과세자로 시작해요",
         where="홈택스 hometax.go.kr", cost="무료", when="맨 처음"),
    Step(title="통신판매업 신고",
         why="온라인으로 물건을 팔려면 법으로 필요해요. 없이 팔면 과태료입니다",
         how="정부24에서 신청. 구매안전서비스 확인증(스마트스토어에서 발급)이 필요해요",
         where="정부24 gov.kr", cost="등록면허세 연 4만원대(지역별 상이)",
         when="팔기 전"),
    Step(title="판매 채널 입점",
         why="스마트스토어는 개인도 바로 되고 수수료가 낮은 편이에요",
         how="네이버 커머스ID 가입 → 사업자 인증 → 상품 등록",
         where="sell.smartstore.naver.com", cost="무료", when="팔기 전"),
]

CONSIGN_ONLY = [
    Step(title="위탁 공급사 가입",
         why="공급사마다 상품·단가·배송조건이 달라요. 두세 곳은 비교하세요",
         how="사업자등록증으로 가입 → 상품 DB 받기 → 마켓에 올리기",
         where="오너클랜 · 도매매 · 온채널", cost="무료~월 이용료",
         when="상품 올리기 전"),
    Step(title="품절·출고일 확인 습관",
         why="팔렸는데 공급사에 재고가 없으면 <b>페널티는 나에게</b> 옵니다",
         how="올리기 전 재고 확인, 주기적으로 재확인. 출고일은 상세페이지에 명시",
         where="", cost="", when="계속"),
]

WHOLESALE_ONLY = [
    Step(title="통관고유부호 발급",
         why="<b>이게 없으면 수입 물건이 세관에서 안 나옵니다.</b> "
             "도매·구매대행에서 제일 많이 막히는 지점이에요",
         how="관세청 사이트에서 사업자번호로 즉시 발급 (개인은 개인통관고유부호)",
         where="관세청 unipass.customs.go.kr", cost="무료",
         when="수입 발주 전 — 반드시 먼저"),
    Step(title="관세율 확인 (HS코드)",
         why="품목마다 관세율이 다릅니다. 의류 13%, 식품 30%대까지 가요. "
             "모르고 발주하면 마진이 통째로 날아갑니다",
         how="관세청에서 품목 검색. 금액이 크면 관세사에 물어보세요",
         where="관세청 unipass", cost="무료(관세사는 유료)",
         when="발주 전"),
    Step(title="배송대행지(배대지) 정하기",
         why="1688·타오바오는 한국으로 직배송이 안 되는 경우가 많아요",
         how="중국 현지 배대지 신청 → 그 주소로 받고 → 묶어서 한국으로",
         where="배대지 업체", cost="kg당 과금", when="발주 전"),
    Step(title="첫 발주는 적게",
         why="안 팔리면 재고가 그대로 손실입니다. 샘플부터 받아보세요",
         how="MOQ가 30개여도 샘플 1~2개는 대부분 보내줍니다",
         where="", cost="", when="항상"),
]

CATEGORY_EXTRA = {
    "식품": Step(title="식품 수입신고",
                 why="식약처 신고 없이 팔면 불법입니다",
                 how="식품위생법상 영업신고 + 수입식품 신고",
                 where="식약처", cost="", when="식품을 다룰 때"),
    "화장품": Step(title="화장품 책임판매업 등록",
                   why="화장품을 수입해 팔려면 책임판매업 등록이 필요해요",
                   how="식약처에 등록. 책임판매관리자를 둬야 해요",
                   where="식약처", cost="", when="화장품을 다룰 때"),
    "전기": Step(title="KC 인증 확인",
                 why="전기·배터리 제품은 KC 인증이 없으면 통관·판매가 막혀요",
                 how="공급사에 인증서 요청. 없으면 직접 받아야 하는데 비쌉니다",
                 where="국가기술표준원", cost="수십만원~", when="전기 제품일 때"),
}


def steps_for(mode: str = "consign", category: str = "") -> list:
    """이 방식에서 갖춰야 할 것들 — 순서대로."""
    out = list(COMMON)
    out += (WHOLESALE_ONLY if mode == "wholesale" else CONSIGN_ONLY)
    for key, st in CATEGORY_EXTRA.items():
        if key in (category or ""):
            out.append(st)
    return out


def as_dicts(mode: str = "consign", category: str = "") -> list:
    return [{"title": s.title, "why": s.why, "how": s.how,
             "where": s.where, "cost": s.cost, "when": s.when}
            for s in steps_for(mode, category)]

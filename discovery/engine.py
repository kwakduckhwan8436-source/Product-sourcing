"""
discovery.engine
================
발굴 전체 오케스트레이터.

흐름: 시드 키워드 -> (검색 API: 시장단면) + (데이터랩: 수요추세)
      -> 스코어링 -> 안정형/선점형 두 랭킹.

비용 통제:
- 검색 API + 데이터랩은 키워드당 각 1회 (하루 25,000 한도 안에서 수천개 OK)
- asyncio 세마포어로 동시 호출 제한 (네이버 측 부하/차단 방지)
- 발굴은 컷오프 없이 랭킹만. 거르기는 다음 단계(cost_mapper)에서.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from discovery.base import CategorySeed, DemandTrend, ShopMarket
from discovery.providers.naver_client import NaverAuthError
from discovery.providers.naver_demand import NaverDemandProvider
from discovery.providers.naver_shop import NaverShopProvider
from discovery.scorer import DiscoveryResult, ScorerConfig, score_keyword

logger = logging.getLogger(__name__)


def load_seeds(path: str | Path) -> list[CategorySeed]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    seeds: list[CategorySeed] = []
    for c in data.get("categories", []):
        seeds.append(CategorySeed(
            name=c["name"], cat_id=c["cat_id"],
            keywords=list(c.get("keywords", [])),
        ))
    return seeds


class DiscoveryEngine:
    def __init__(self, shop: NaverShopProvider, demand: NaverDemandProvider,
                 scorer_cfg: ScorerConfig | None = None, concurrency: int = 1,
                 datalab_top_n: int = 15, blue_ocean: bool = False,
                 blue_cfg=None):
        self.shop = shop
        self.demand = demand
        self.scorer_cfg = scorer_cfg or ScorerConfig()
        self._sem = asyncio.Semaphore(concurrency)
        # 데이터랩 전용 세마포어: 항상 직렬(1). 한도가 좁아 동시 호출 금지.
        self._datalab_sem = asyncio.Semaphore(1)
        # 데이터랩은 한도가 좁아 전체가 아닌 '검색 점수 상위 N개'만 호출.
        # 0 이면 데이터랩 생략(검색만), 음수면 전체(기존 동작).
        self.datalab_top_n = datalab_top_n
        # 블루오션 모드: 씨앗에서 롱테일을 캐 레드오션을 거르고 블루오션만 발굴.
        self.blue_ocean = blue_ocean
        from discovery.blue_ocean import BlueOceanConfig
        self.blue_cfg = blue_cfg or BlueOceanConfig()

    def _attach_supply(self, score, market, demand=None):
        """공급 안정성 + 인증 검증을 계산해 score 에 주입 (네이버 proxy)."""
        from discovery.supply import score_supply_stability
        ss = score_supply_stability(market, demand)
        score.supply_score = ss.score
        score.supply_grade = ss.grade.value
        score.supply_rationale = ss.rationale
        # 인증/법규 플래그 (신규 기능 2)
        from discovery.compliance import check_compliance
        cf = check_compliance(score.keyword, market.category_path)
        score.cert_labels = cf.labels
        score.cert_note = cf.note
        return score

    async def _search_one(self, keyword: str, cat_id: str):
        """1단계: 검색만으로 점수화 (데이터랩 없이, 빠름). market 도 함께 보존."""
        async with self._sem:
            try:
                market: ShopMarket = await self.shop.market_of(keyword)
            except NaverAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("검색 실패 (%s): %s", keyword, exc)
                return None
            # 데이터랩 없이 우선 점수화 (수요 성분은 0 → 안정형 위주)
            trend = DemandTrend(keyword=keyword, found=False)
            score = score_keyword(market, trend, self.scorer_cfg)
            # 공급 안정성 (검색 데이터만으로도 셀러 다양성/범용성 측정 가능)
            self._attach_supply(score, market, None)
            return (score, market, cat_id)

    async def _enrich_demand(self, score, market, cat_id):
        """2단계: 상위 키워드만 데이터랩으로 수요추세 채워 재점수화.
        데이터랩 전용 세마포어로 '한 번에 하나씩'만 — 동시 폭주로 인한 429 방지."""
        async with self._datalab_sem:
            try:
                trend = await self.demand.trend_of(score.keyword, cat_id)
            except NaverAuthError:
                logger.warning("데이터랩 권한 없음 (%s)", score.keyword)
                return score  # 기존 검색 점수 유지
            except Exception as exc:  # noqa: BLE001
                logger.warning("데이터랩 실패 (%s): %s", score.keyword, exc)
                return score
            # 수요까지 반영해 재점수화 + 공급 안정성도 시즌위험 반영해 갱신
            new_score = score_keyword(market, trend, self.scorer_cfg)
            self._attach_supply(new_score, market, trend)
            # 블루오션 모드: 데이터랩 수요까지 반영해 진짜 블루오션 재확정
            if self.blue_ocean:
                from discovery.blue_ocean import evaluate_blue_ocean
                bo = evaluate_blue_ocean(market, demand=trend, cfg=self.blue_cfg)
                new_score.rationale = (new_score.rationale + " | " + bo.note).strip(" |")
                new_score.est_margin_pct = bo.est_margin_pct
                # 수요 약함/마진 부족이면 점수를 깎아 가짜 블루오션을 아래로
                if not bo.is_blue:
                    new_score.stable_score *= 0.3
                    new_score.emerging_score *= 0.3
            return new_score

    # 기존 단건 메서드 보존 (하위 호환)
    async def _score_one(self, keyword: str, cat_id: str):
        async with self._sem:
            try:
                market: ShopMarket = await self.shop.market_of(keyword)
            except NaverAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("검색 실패 (%s): %s", keyword, exc)
                return None
            try:
                trend = await self.demand.trend_of(keyword, cat_id)
            except NaverAuthError:
                logger.warning("데이터랩 권한 없음 (%s)", keyword)
                trend = DemandTrend(keyword=keyword, found=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("데이터랩 실패 (%s): %s", keyword, exc)
                trend = DemandTrend(keyword=keyword, found=False)
            return score_keyword(market, trend, self.scorer_cfg)

    async def _blue_ocean_expand(self, triples):
        """
        씨앗 시장에서 롱테일을 생성→검색→레드오션 차단→블루오션 후보만 반환.
        씨앗 자체(유명 키워드)는 레드오션이므로 결과에서 제외, 롱테일만 남김.
        """
        from discovery.blue_ocean import (generate_longtails,
                                          evaluate_blue_ocean)
        # 1) 각 씨앗 시장에서 롱테일 키워드 생성
        longtail_set = {}  # 키워드 -> cat_id (중복 제거)
        for score, market, cat_id in triples:
            lts = generate_longtails(market, score.keyword, self.blue_cfg)
            for lt in lts:
                longtail_set.setdefault(lt, cat_id)

        if not longtail_set:
            # 롱테일을 못 만들면 원본에서 레드오션만 거르고 반환
            return [(s, m, c) for (s, m, c) in triples
                    if evaluate_blue_ocean(m, cfg=self.blue_cfg).is_blue]

        # 2) 롱테일 검색 (검색 API — 한도 여유)
        lt_tasks = [self._search_one(kw, cat) for kw, cat in longtail_set.items()]
        lt_results = await asyncio.gather(*lt_tasks)

        # 3) 레드오션 차단 + 블루오션만 남김
        blue = []
        for t in lt_results:
            if t is None:
                continue
            score, market, cat_id = t
            bo = evaluate_blue_ocean(market, cfg=self.blue_cfg)
            if bo.is_blue:
                # 블루오션 신호를 score 에 기록
                score.rationale = (score.rationale + " | " + bo.note).strip(" |")
                blue.append((score, market, cat_id))
        return blue

    async def discover(self, seeds: list[CategorySeed]) -> DiscoveryResult:
        """
        2단계 발굴:
          1) 모든 키워드를 검색만으로 점수화 (데이터랩 호출 0, 429 안 만남)
          2) 검색 점수 상위 datalab_top_n 개만 데이터랩으로 수요추세 보강
        -> 데이터랩 호출이 전체가 아닌 상위 N개로 줄어 한도 초과 방지.
        """
        # === 1단계: 검색 전체 ===
        search_tasks = []
        for seed in seeds:
            for kw in seed.keywords:
                search_tasks.append(self._search_one(kw, seed.cat_id))
        searched = await asyncio.gather(*search_tasks)
        triples = [t for t in searched if t is not None]  # (score, market, cat_id)
        logger.info("1단계 검색 완료: %d개 키워드", len(triples))

        # === 블루오션 모드: 씨앗에서 롱테일을 캐 레드오션 거르고 블루만 ===
        if self.blue_ocean:
            triples = await self._blue_ocean_expand(triples)
            logger.info("블루오션 발굴 후: %d개 후보", len(triples))

        if self.datalab_top_n == 0 or not triples:
            # 데이터랩 생략 (검색만)
            return DiscoveryResult(scores=[t[0] for t in triples])

        # === 2단계: 검색 점수 상위 N개만 데이터랩 ===
        # 안정형/선점형 어느 쪽이든 상위에 들 만한 키워드를 데이터랩 대상으로.
        # (검색 점수 = 현재 stable_score, 수요 0 상태 기준)
        ranked = sorted(triples, key=lambda t: t[0].stable_score, reverse=True)
        if self.datalab_top_n > 0:
            enrich_targets = ranked[:self.datalab_top_n]
            keep_as_is = ranked[self.datalab_top_n:]
        else:  # 음수 = 전체 (기존 동작 호환)
            enrich_targets = ranked
            keep_as_is = []

        logger.info("2단계 데이터랩 대상: 상위 %d개", len(enrich_targets))
        enrich_tasks = [self._enrich_demand(s, m, c) for s, m, c in enrich_targets]
        enriched = await asyncio.gather(*enrich_tasks)

        final = list(enriched) + [t[0] for t in keep_as_is]
        result = DiscoveryResult(scores=final)
        logger.info("발굴 완료: 총 %d개 (데이터랩 보강 %d개)",
                    len(final), len(enriched))
        return result

    async def discover_from_file(self, seeds_path: str | Path) -> DiscoveryResult:
        return await self.discover(load_seeds(seeds_path))

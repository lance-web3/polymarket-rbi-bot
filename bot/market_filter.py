from __future__ import annotations

import json
import math
from dataclasses import dataclass
from statistics import pstdev
from typing import Any

from bot.market_classifier import JsonFileMarketClassifier, MarketClassifier, NullMarketClassifier
from data.polymarket_client import PolymarketHistoryClient
from polymarket_rbi_bot.microstructure import extract_maturity_datetimes, compute_time_to_resolution_hours


@dataclass(slots=True)
class MarketEligibilityResult:
    eligible: bool
    reason: str
    metrics: dict


@dataclass(slots=True)
class MarketFilter:
    min_liquidity: float = 1000.0
    min_history_points: int = 100
    min_price: float = 0.1
    max_price: float = 0.9
    min_abs_return_bps_24h: float = 50.0
    excluded_keywords: tuple[str, ...] = ()
    strict_mode: bool = False
    strict_min_price: float | None = None
    strict_max_price: float | None = None
    strict_excluded_keywords: tuple[str, ...] = ()
    market_family_mode: str = 'balanced'
    allowed_market_families: tuple[str, ...] = ()
    blocked_market_families: tuple[str, ...] = ()
    family_allow_keywords: tuple[str, ...] = ()
    family_block_keywords: tuple[str, ...] = ()
    classifier: MarketClassifier | None = None
    llm_market_classifier_path: str | None = None
    history_client: PolymarketHistoryClient | None = None
    # maturity + microstructure gating (eligibility layer)
    enable_maturity_gating: bool = True
    enable_microstructure_gating: bool = True
    strict_min_time_to_resolution_hours: float | None = None
    strict_max_time_to_resolution_hours: float | None = None
    strict_min_time_since_open_hours: float | None = None
    strict_max_current_spread_bps: float | None = None

    def evaluate(self, market: dict, token_id: str) -> MarketEligibilityResult:
        question = str(market.get('question') or '')
        lowered = question.lower()
        for keyword in self.excluded_keywords:
            if keyword and keyword in lowered:
                metrics = self._build_metrics(market=market, question=question, current_price=self._extract_current_price(market), history=[], quote_metrics=self._extract_quote_metrics(market), history_error=None)
                metrics['question'] = question
                return MarketEligibilityResult(False, f'Excluded keyword matched: {keyword}', metrics)

        if self.strict_mode:
            for keyword in self.strict_excluded_keywords:
                if keyword and keyword in lowered:
                    metrics = self._build_metrics(market=market, question=question, current_price=self._extract_current_price(market), history=[], quote_metrics=self._extract_quote_metrics(market), history_error=None)
                    metrics['question'] = question
                    return MarketEligibilityResult(False, f'Strict-mode keyword matched: {keyword}', metrics)

        family_info = self._classify_family(market)
        family_block_reason = self._family_block_reason(question=question, family_info=family_info)
        if family_block_reason is not None:
            metrics = self._build_metrics(market=market, question=question, current_price=self._extract_current_price(market), history=[], quote_metrics=self._extract_quote_metrics(market), history_error=None)
            metrics['question'] = question
            metrics['market_family'] = family_info
            return MarketEligibilityResult(False, family_block_reason, metrics)

        try:
            liquidity = float(market.get('liquidity') or 0.0)
        except (TypeError, ValueError):
            liquidity = 0.0

        current_price = self._extract_current_price(market)
        quote_metrics = self._extract_quote_metrics(market)
        # Maturity awareness from market metadata
        maturity_times = extract_maturity_datetimes(market)

        client = self.history_client or PolymarketHistoryClient()
        history: list[dict[str, Any]] = []
        history_error: str | None = None
        try:
            history = client.fetch_price_history(token_id=token_id, interval='max', fidelity=60)
        except Exception as exc:
            history_error = str(exc)

        metrics = self._build_metrics(market=market, question=question, current_price=current_price, history=history, quote_metrics=quote_metrics, history_error=history_error)
        metrics['liquidity'] = liquidity
        metrics['current_price'] = current_price
        metrics['question'] = question
        metrics['strict_mode'] = self.strict_mode
        metrics['market_family'] = family_info
        # Add maturity metrics
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)
        ttr = compute_time_to_resolution_hours(now=now, market_like=market)
        metrics['time_to_resolution_hours'] = ttr.get('time_to_resolution_hours')
        metrics['time_since_open_hours'] = ttr.get('time_since_open_hours')
        if family_info.get('score_adjustment'):
            metrics['quality_score'] = round(float(metrics.get('quality_score') or 0.0) + float(family_info.get('score_adjustment') or 0.0), 1)

        active_min_price = self.strict_min_price if self.strict_mode and self.strict_min_price is not None else self.min_price
        active_max_price = self.strict_max_price if self.strict_mode and self.strict_max_price is not None else self.max_price

        if liquidity < self.min_liquidity:
            return MarketEligibilityResult(False, 'Liquidity below threshold', metrics)
        if current_price is not None and not (active_min_price <= current_price <= active_max_price):
            return MarketEligibilityResult(False, 'Current price outside configured range', metrics)
        if history_error is not None:
            return MarketEligibilityResult(False, f'Could not fetch history: {history_error}', metrics)
        if len(history) < self.min_history_points:
            return MarketEligibilityResult(False, 'Insufficient history length', metrics)
        if float(metrics.get('abs_return_bps_24h') or 0.0) < self.min_abs_return_bps_24h:
            return MarketEligibilityResult(False, 'Recent movement below threshold', metrics)

        # Strict-mode extra gating: maturity + microstructure (fast checks only in eligibility)
        if self.strict_mode and self.enable_maturity_gating:
            ttr_hours = metrics.get('time_to_resolution_hours')
            since_open = metrics.get('time_since_open_hours')
            if ttr_hours is not None:
                if self.strict_min_time_to_resolution_hours is not None and ttr_hours < self.strict_min_time_to_resolution_hours:
                    return MarketEligibilityResult(False, 'Maturity too soon (time-to-resolution)', metrics)
                if self.strict_max_time_to_resolution_hours is not None and ttr_hours > self.strict_max_time_to_resolution_hours:
                    return MarketEligibilityResult(False, 'Maturity too far (time-to-resolution)', metrics)
            if since_open is not None and self.strict_min_time_since_open_hours is not None and since_open < self.strict_min_time_since_open_hours:
                return MarketEligibilityResult(False, 'Market too new (since open)', metrics)

        if self.strict_mode and self.enable_microstructure_gating and self.strict_max_current_spread_bps is not None:
            spread_bps = quote_metrics.get('spread_bps')
            if spread_bps is not None and spread_bps > self.strict_max_current_spread_bps:
                return MarketEligibilityResult(False, 'Spread too wide for strict mode', metrics)

        return MarketEligibilityResult(True, 'Eligible', metrics)

    def evaluate_family_only(self, market: dict) -> MarketEligibilityResult:
        """Lightweight check: keyword + family gates only, no history fetch.

        Intended for backtest replay where history is already loaded from CSV.
        """
        question = str(market.get('question') or '')
        lowered = question.lower()
        for keyword in self.excluded_keywords:
            if keyword and keyword in lowered:
                return MarketEligibilityResult(
                    False, f'Excluded keyword matched: {keyword}', {'question': question}
                )
        if self.strict_mode:
            for keyword in self.strict_excluded_keywords:
                if keyword and keyword in lowered:
                    return MarketEligibilityResult(
                        False, f'Strict-mode keyword matched: {keyword}', {'question': question}
                    )
        family_info = self._classify_family(market)
        family_block_reason = self._family_block_reason(question=question, family_info=family_info)
        if family_block_reason is not None:
            return MarketEligibilityResult(
                False,
                family_block_reason,
                {'question': question, 'market_family': family_info},
            )
        return MarketEligibilityResult(
            True, 'Eligible', {'question': question, 'market_family': family_info}
        )

    def _classify_family(self, market: dict[str, Any]) -> dict[str, Any]:
        heuristic_family, heuristic_reason = self._heuristic_family(market)
        classifier = self.classifier
        if classifier is None and self.llm_market_classifier_path:
            classifier = JsonFileMarketClassifier(self.llm_market_classifier_path, fallback=NullMarketClassifier())
        classification = classifier.classify(market) if classifier is not None else NullMarketClassifier(default_family=heuristic_family).classify(market)
        family = classification.family if classification.family != 'unknown' else heuristic_family
        reason_parts = [part for part in [heuristic_reason, classification.reason] if part]
        classifier_metadata = classification.metadata or {}
        return {
            'family': family,
            'heuristic_family': heuristic_family,
            'tradable': classification.tradable,
            'decision': classifier_metadata.get('decision') or ('allow' if classification.tradable else 'avoid'),
            'score_adjustment': round(float(classification.score_adjustment or 0.0), 2),
            'reason': ' | '.join(reason_parts) if reason_parts else 'No family reason',
            'classifier_metadata': classifier_metadata,
        }

    def _heuristic_family(self, market: dict[str, Any]) -> tuple[str, str]:
        fields = [
            str(market.get('question') or ''),
            str(market.get('description') or ''),
            str(market.get('category') or ''),
            str(market.get('subcategory') or ''),
            str(market.get('slug') or ''),
            json.dumps(market.get('tags') or []),
        ]
        lowered = ' '.join(fields).lower()

        outright_keywords = ('qualify', 'win the', 'champion', 'advance to', 'reach the playoffs', 'top 4', 'top four', 'gold medal', 'mvp', 'oscar')
        legal_keywords = ('sentenced', 'indicted', 'convicted', 'appeal', 'supreme court', 'sec', 'lawsuit', 'trial', 'prison')
        news_keywords = ('ceasefire', 'attack', 'assassination', 'earthquake', 'hurricane', 'killed', 'resigns', 'tariff', 'sanction', 'fed rate', 'cpi')
        sports_keywords = ('fifa', 'nba', 'nfl', 'mlb', 'nhl', 'qualify', 'playoffs', 'champion', 'grand slam', 'formula 1', 'uefa')
        crypto_keywords = ('bitcoin', 'ethereum', 'solana', 'airdrop', 'etf', 'price above', 'price below')

        if any(keyword in lowered for keyword in legal_keywords):
            return 'legal_regulatory', 'Heuristic family: legal/regulatory discrete-jump market'
        if any(keyword in lowered for keyword in news_keywords):
            return 'news_breaking', 'Heuristic family: breaking-news / event-jump market'
        if any(keyword in lowered for keyword in sports_keywords) and any(keyword in lowered for keyword in outright_keywords):
            return 'sports_outright', 'Heuristic family: sports outright / repricing-friendly market'
        if any(keyword in lowered for keyword in crypto_keywords) and any(keyword in lowered for keyword in outright_keywords):
            return 'crypto_outright', 'Heuristic family: crypto outright / repricing-friendly market'
        if any(keyword in lowered for keyword in outright_keywords):
            return 'scheduled_event', 'Heuristic family: scheduled-event outright market'
        return 'event_resolution', 'Heuristic family: unresolved general event market'

    def _family_block_reason(self, *, question: str, family_info: dict[str, Any]) -> str | None:
        lowered = question.lower()
        family = str(family_info.get('family') or 'unknown').lower()
        tradable = bool(family_info.get('tradable', True))

        if not tradable:
            return f'Classifier rejected market family: {family}'
        if any(keyword in lowered for keyword in self.family_block_keywords):
            matched = next(keyword for keyword in self.family_block_keywords if keyword in lowered)
            return f'Market-family block keyword matched: {matched}'
        if self.strict_mode and self.market_family_mode in {'strict', 'outright_only'}:
            if self.allowed_market_families and family not in self.allowed_market_families:
                return f'Strict market-family filter rejected: {family}'
        if self.blocked_market_families and family in self.blocked_market_families:
            return f'Blocked market family: {family}'
        if self.family_allow_keywords and any(keyword in lowered for keyword in self.family_allow_keywords):
            return None
        if self.strict_mode and self.market_family_mode == 'balanced' and family in {'news_breaking', 'legal_regulatory', 'war_geopolitics', 'disaster', 'assassination', 'discrete_binary', 'event_resolution'}:
            return f'Strict market-family filter rejected: {family}'
        return None

    def _build_metrics(self, *, market: dict[str, Any], question: str, current_price: float | None, history: list[dict[str, Any]], quote_metrics: dict[str, float | None], history_error: str | None) -> dict[str, Any]:
        try:
            liquidity = float(market.get('liquidity') or 0.0)
        except (TypeError, ValueError):
            liquidity = 0.0

        history_metrics = self._compute_history_metrics(history)
        score_breakdown = self._score_market(liquidity=liquidity, current_price=current_price, history_points=history_metrics['history_points'], abs_return_bps_24h=history_metrics['abs_return_bps_24h'], realized_volatility_bps=history_metrics['realized_volatility_bps'], movement_consistency=history_metrics['movement_consistency'], spread_bps=quote_metrics.get('spread_bps'))
        quality_score = round(sum(component['score'] for component in score_breakdown.values()), 1)
        metrics: dict[str, Any] = {**history_metrics, **quote_metrics, 'history_error': history_error, 'quality_score': quality_score, 'quality_tier': self._quality_tier(quality_score), 'score_breakdown': score_breakdown, 'ranking_summary': {'score': quality_score, 'tier': self._quality_tier(quality_score), 'best_feature': max(score_breakdown.items(), key=lambda item: item[1]['score'])[0] if score_breakdown else None, 'weakest_feature': min(score_breakdown.items(), key=lambda item: item[1]['score'])[0] if score_breakdown else None}}
        if question:
            metrics['question'] = question
        return metrics

    def _extract_current_price(self, market: dict[str, Any]) -> float | None:
        outcome_prices = market.get('outcomePrices')
        if isinstance(outcome_prices, str):
            try:
                prices = json.loads(outcome_prices)
                if prices:
                    return float(prices[0])
            except Exception:
                return None
        elif isinstance(outcome_prices, list) and outcome_prices:
            try:
                return float(outcome_prices[0])
            except (TypeError, ValueError):
                return None
        return None

    def _extract_quote_metrics(self, market: dict[str, Any]) -> dict[str, float | None]:
        bid = self._first_float(market.get('bestBid'), market.get('best_bid'), market.get('bid'), market.get('yesBid'), market.get('yes_bid'))
        ask = self._first_float(market.get('bestAsk'), market.get('best_ask'), market.get('ask'), market.get('yesAsk'), market.get('yes_ask'))
        mid = None
        spread_bps = None
        if bid is not None and ask is not None and bid > 0 and ask >= bid:
            mid = (bid + ask) / 2
            if mid > 0:
                spread_bps = ((ask - bid) / mid) * 10_000
        return {'best_bid': bid, 'best_ask': ask, 'spread_bps': round(spread_bps, 1) if spread_bps is not None else None}

    def _compute_history_metrics(self, history: list[dict[str, Any]]) -> dict[str, float | int | None]:
        closes = [self._coerce_float(row.get('close')) for row in history]
        closes = [value for value in closes if value is not None and value > 0]
        history_points = len(closes)
        recent = closes[-24:] if len(closes) >= 24 else closes
        abs_return_bps = 0.0
        if len(recent) >= 2 and recent[0] > 0:
            abs_return_bps = abs((recent[-1] - recent[0]) / recent[0]) * 10_000

        returns_bps: list[float] = []
        direction_changes = 0
        last_sign = 0
        for previous, current in zip(closes, closes[1:]):
            if previous <= 0:
                continue
            move_bps = ((current - previous) / previous) * 10_000
            returns_bps.append(move_bps)
            sign = 1 if move_bps > 0 else -1 if move_bps < 0 else 0
            if sign != 0 and last_sign != 0 and sign != last_sign:
                direction_changes += 1
            if sign != 0:
                last_sign = sign

        realized_volatility_bps = pstdev(returns_bps) if len(returns_bps) >= 2 else 0.0
        movement_consistency = direction_changes / max(len(returns_bps), 1) if returns_bps else 0.0
        return {'history_points': history_points, 'abs_return_bps_24h': round(abs_return_bps, 1), 'realized_volatility_bps': round(realized_volatility_bps, 1), 'movement_consistency': round(movement_consistency, 3)}

    def _score_market(self, *, liquidity: float, current_price: float | None, history_points: int, abs_return_bps_24h: float, realized_volatility_bps: float, movement_consistency: float, spread_bps: float | None) -> dict[str, dict[str, float | str | None]]:
        price_centrality = 0.0
        if current_price is not None:
            distance_from_center = abs(current_price - 0.5)
            price_centrality = max(0.0, 1.0 - (distance_from_center / 0.5))

        liquidity_score = 25.0 * self._clamp(math.log10(max(liquidity, 1.0)) / 4.0)
        history_score = 20.0 * self._clamp(history_points / max(self.min_history_points * 3, 1))
        movement_score = 20.0 * self._clamp(abs_return_bps_24h / max(self.min_abs_return_bps_24h * 6, 1.0))
        volatility_score = 15.0 * self._clamp(realized_volatility_bps / 250.0)
        quote_score = 0.0 if spread_bps is None else 10.0 * self._clamp(1.0 - (spread_bps / 500.0))
        price_score = 5.0 * price_centrality
        consistency_score = 5.0 * self._clamp(movement_consistency / 0.35)
        return {
            'liquidity': {'score': round(liquidity_score, 1), 'max_score': 25.0, 'value': round(liquidity, 2), 'explanation': 'More liquidity usually means easier entry/exit and less slippage.'},
            'history': {'score': round(history_score, 1), 'max_score': 20.0, 'value': history_points, 'explanation': 'Longer history makes the strategy test less fragile.'},
            'recent_move': {'score': round(movement_score, 1), 'max_score': 20.0, 'value': round(abs_return_bps_24h, 1), 'explanation': 'Some recent movement is useful; dead markets are hard to trade.'},
            'volatility': {'score': round(volatility_score, 1), 'max_score': 15.0, 'value': round(realized_volatility_bps, 1), 'explanation': 'Intraday volatility can create signals, up to a capped score.'},
            'quote_quality': {'score': round(quote_score, 1), 'max_score': 10.0, 'value': spread_bps, 'explanation': 'Tighter spread is better when quote data is available.'},
            'price_zone': {'score': round(price_score, 1), 'max_score': 5.0, 'value': current_price, 'explanation': 'Prices away from 0/1 are often more tradable than near-certain outcomes.'},
            'movement_consistency': {'score': round(consistency_score, 1), 'max_score': 5.0, 'value': round(movement_consistency, 3), 'explanation': 'A bit of back-and-forth suggests the market is actually moving, not frozen.'},
        }

    def _quality_tier(self, score: float) -> str:
        if score >= 75:
            return 'A'
        if score >= 60:
            return 'B'
        if score >= 45:
            return 'C'
        return 'D'

    def _first_float(self, *values: Any) -> float | None:
        for value in values:
            parsed = self._coerce_float(value)
            if parsed is not None:
                return parsed
        return None

    def _coerce_float(self, value: Any) -> float | None:
        try:
            if value in {None, ''}:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _clamp(self, value: float, low: float = 0.0, high: float = 1.0) -> float:
        return max(low, min(high, value))

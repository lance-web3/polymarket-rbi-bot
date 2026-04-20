from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class MarketFamilyClassification:
    family: str
    tradable: bool
    score_adjustment: float = 0.0
    reason: str = ""
    metadata: dict[str, Any] | None = None


class MarketClassifier:
    """Lightweight market-family classifier.

    The live/backtest path only consumes a local JSON artifact.
    External tooling can precompute that file however it wants (OpenAI,
    heuristics, manual review, etc.) without introducing synchronous LLM
    calls into trading.
    """

    def classify(self, market: dict[str, Any]) -> MarketFamilyClassification:
        raise NotImplementedError


@dataclass(slots=True)
class NullMarketClassifier(MarketClassifier):
    default_family: str = 'unknown'

    def classify(self, market: dict[str, Any]) -> MarketFamilyClassification:
        return MarketFamilyClassification(
            family=self.default_family,
            tradable=True,
            score_adjustment=0.0,
            reason='No external classifier configured',
            metadata={'source': 'null'},
        )


@dataclass(slots=True)
class JsonFileMarketClassifier(MarketClassifier):
    path: str
    fallback: MarketClassifier | None = None

    def classify(self, market: dict[str, Any]) -> MarketFamilyClassification:
        file_path = Path(self.path)
        if not file_path.exists():
            if self.fallback is not None:
                return self.fallback.classify(market)
            return MarketFamilyClassification(
                family='unknown', tradable=True, reason=f'Classifier file not found: {self.path}', metadata={'source': 'json_file_missing'}
            )

        payload = json.loads(file_path.read_text())
        record = self._match_record(payload, market)

        if record is None:
            if self.fallback is not None:
                return self.fallback.classify(market)
            return MarketFamilyClassification(
                family='unknown', tradable=True, reason='No classifier record matched market', metadata={'source': 'json_file'}
            )

        raw_decision = str(record.get('decision') or '').strip().lower()
        explicit_tradable = record.get('tradable')
        if explicit_tradable is None:
            tradable = raw_decision not in {'avoid', 'review'}
        else:
            tradable = bool(explicit_tradable)

        confidence = self._coerce_float(record.get('confidence'))
        score_adjustment = record.get('score_adjustment')
        if score_adjustment is None:
            score_adjustment = self._default_score_adjustment(raw_decision, confidence)

        family = str(
            record.get('family')
            or record.get('primary_family')
            or record.get('regime')
            or record.get('heuristic_family')
            or 'unknown'
        )
        rationale = str(record.get('rationale') or record.get('reason') or 'Classifier record matched')
        risk_flags = record.get('risk_flags') or []
        regimes = record.get('regime_labels') or record.get('families') or []
        metadata = {
            'source': 'json_file',
            'decision': raw_decision or ('allow' if tradable else 'avoid'),
            'confidence': confidence,
            'risk_flags': risk_flags if isinstance(risk_flags, list) else [str(risk_flags)],
            'regime_labels': regimes if isinstance(regimes, list) else [str(regimes)],
            'raw': record,
        }
        return MarketFamilyClassification(
            family=family,
            tradable=tradable,
            score_adjustment=float(score_adjustment or 0.0),
            reason=rationale,
            metadata=metadata,
        )

    def _match_record(self, payload: Any, market: dict[str, Any]) -> dict[str, Any] | None:
        key_candidates = [
            str(market.get('conditionId') or market.get('condition_id') or ''),
            str(market.get('tokenId') or market.get('token_id') or ''),
            str(market.get('slug') or ''),
            str(market.get('question') or ''),
        ]
        records = self._flatten_payload(payload)
        for key in key_candidates:
            if not key:
                continue
            for record in records:
                if self._record_matches(record, key):
                    return record
        return None

    def _flatten_payload(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [record for record in payload if isinstance(record, dict)]
        if isinstance(payload, dict):
            if isinstance(payload.get('records'), list):
                return [record for record in payload['records'] if isinstance(record, dict)]
            if isinstance(payload.get('markets'), list):
                return [record for record in payload['markets'] if isinstance(record, dict)]
            flattened: list[dict[str, Any]] = []
            for key, value in payload.items():
                if not isinstance(value, dict):
                    continue
                record = dict(value)
                record.setdefault('lookup_key', key)
                flattened.append(record)
            return flattened
        return []

    def _record_matches(self, record: dict[str, Any], candidate: str) -> bool:
        values = {
            str(record.get('lookup_key') or ''),
            str(record.get('condition_id') or ''),
            str(record.get('conditionId') or ''),
            str(record.get('token_id') or ''),
            str(record.get('tokenId') or ''),
            str(record.get('slug') or ''),
            str(record.get('question') or ''),
        }
        return candidate in values

    def _default_score_adjustment(self, decision: str, confidence: float | None) -> float:
        confidence_value = confidence if confidence is not None else 0.5
        if decision == 'allow':
            return round(min(10.0, 2.0 + confidence_value * 6.0), 2)
        if decision == 'avoid':
            return round(max(-10.0, -(2.0 + confidence_value * 6.0)), 2)
        return 0.0

    def _coerce_float(self, value: Any) -> float | None:
        try:
            if value in {None, ''}:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

"""Strategy that reads pre-computed LLM probability estimates from a JSONL file
and emits a StrategySignal. No LLM calls happen inside generate_signal — per
PLAN, predictions are cached offline via `deploy.llm_predict_markets`.

The strategy is side-effect-free. It needs the token_id at construction time
(passed via the run_live CLI), looks up the most recent prediction for that
token, and returns BUY if model_p > market_mid (model thinks the market is
underpriced) or SELL if model_p < market_mid (overpriced).

Confidence is `confidence_llm * |edge|`, capped at 1.0. The trader's risk
gates handle the final go/no-go via MIN_ENTRY_CONFIDENCE + edge thresholds.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from polymarket_rbi_bot.models import MarketSnapshot, SignalSide, StrategySignal
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


@dataclass
class LLMProbabilityStrategy(BaseStrategy):
    """Look up the most recent prediction for a token_id and convert to a signal.

    Required at construction time: `predictions_path` and `token_id`. The
    `max_age_hours` parameter rejects predictions older than the threshold
    (default 48h to match the prediction cache TTL).
    """
    name: str = "LLMProbability"
    predictions_path: Path = field(default_factory=lambda: Path("data/llm_predictions.jsonl"))
    token_id: str | None = None
    max_age_hours: float = 48.0

    def generate_signal(self, history: list[MarketSnapshot]) -> StrategySignal:
        if not self.token_id:
            return self._hold("token_id not configured")
        pred, matched_side = self._load_latest_prediction(self.token_id)
        if pred is None:
            return self._hold(f"no fresh prediction for token_id={self.token_id}")

        # Use mid from history if available, else from the prediction record.
        # If matched on the NO token, history.mid is the NO mid (correct directly);
        # the prediction's recorded mid is the YES mid, so flip if we matched NO.
        mid = None
        if history:
            mid = history[-1].mid_price
        if mid is None or mid <= 0:
            recorded_mid = pred.get("mid")
            if recorded_mid is not None and recorded_mid > 0:
                mid = (1.0 - float(recorded_mid)) if matched_side == "no" else float(recorded_mid)
        if mid is None or mid <= 0:
            return self._hold("no mid price available")

        raw_p_llm = float(pred["p_llm"])
        # When matched on NO token, the LLM probability for THIS token is 1 - p_yes.
        p_llm = (1.0 - raw_p_llm) if matched_side == "no" else raw_p_llm
        confidence_llm = float(pred.get("confidence_llm") or 0.0)
        edge = p_llm - float(mid)  # positive → buy, negative → sell

        if abs(edge) < 0.001:  # < 10 bps — too small
            return self._hold(f"edge too small: {edge:+.4f}")

        # Scale confidence by edge magnitude. A 5-point edge at 0.5 confidence
        # yields strategy confidence ≈ 0.025 → small; a 30-point edge at 0.7
        # confidence yields ≈ 0.21 → respectable.
        signal_confidence = min(1.0, confidence_llm * abs(edge) * 5.0)

        if edge > 0:
            side = SignalSide.BUY
            # Reference price for the limit order: market mid (trader adds price_edge_bps).
            target_price = float(mid)
        else:
            side = SignalSide.SELL
            target_price = float(mid)

        reason = (
            f"LLM p_yes={p_llm:.3f} vs mid={mid:.3f} (edge {edge*10000:+.0f}bps); "
            f"conf_llm={confidence_llm:.2f}; "
            f"reason={(pred.get('reasoning') or '')[:200]}"
        )

        return StrategySignal(
            strategy_name=self.name,
            side=side,
            confidence=signal_confidence,
            price=target_price,
            reason=reason,
            metadata={
                "llm_p_yes": raw_p_llm,
                "llm_p_for_traded_token": p_llm,
                "llm_confidence": confidence_llm,
                "llm_edge": edge,
                "llm_edge_bps": round(edge * 10000, 2),
                "llm_matched_side": matched_side,
                "llm_prompt_version": pred.get("prompt_version"),
                "llm_niche": pred.get("niche_llm"),
                "llm_ts": pred.get("ts"),
                "llm_evidence": pred.get("evidence"),
                "llm_reasoning_excerpt": (pred.get("reasoning") or "")[:300],
                "market_mid_at_prediction": pred.get("mid"),
            },
        )

    def _hold(self, reason: str) -> StrategySignal:
        return StrategySignal(
            strategy_name=self.name,
            side=SignalSide.HOLD,
            confidence=0.0,
            reason=reason,
        )

    def _load_latest_prediction(
        self, token_id: str
    ) -> tuple[dict[str, Any] | None, str]:
        """Find the most-recent prediction whose `token_id` (YES) or `no_token_id`
        equals the requested token_id. Returns (row, matched_side) where
        matched_side ∈ {"yes", "no", "none"}. If matched_side=="no", caller must
        flip p_yes → 1 − p_yes when using this prediction.
        """
        path = self.predictions_path
        if not path.exists():
            return None, "none"
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=self.max_age_hours)
        best: dict[str, Any] | None = None
        best_ts: datetime | None = None
        best_side: str = "none"
        needle = str(token_id)
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("token_id") or "") == needle:
                    side = "yes"
                elif str(row.get("no_token_id") or "") == needle:
                    side = "no"
                else:
                    continue
                try:
                    ts = datetime.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00"))
                except Exception:  # noqa: BLE001
                    continue
                if ts < cutoff:
                    continue
                if best_ts is None or ts > best_ts:
                    best_ts = ts
                    best = row
                    best_side = side
        return best, best_side

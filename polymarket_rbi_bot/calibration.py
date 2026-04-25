"""Brier-score and calibration helpers.

A signal stack can be right on direction more often than not yet still lose
money if its probabilities are poorly calibrated (overconfident on losers,
underconfident on winners). PLAN's Phase 2 honest-test gate uses Brier score
and reliability bins to catch that.

Stdlib only.
"""

from __future__ import annotations

from typing import Iterable, Sequence


def brier_score(predictions: Sequence[float], outcomes: Sequence[int]) -> float:
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"predictions ({len(predictions)}) and outcomes ({len(outcomes)}) length mismatch"
        )
    if not predictions:
        raise ValueError("brier_score requires at least one observation")
    total = 0.0
    for p, y in zip(predictions, outcomes):
        if y not in (0, 1):
            raise ValueError(f"outcomes must be 0 or 1, got {y!r}")
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"predictions must be in [0, 1], got {p!r}")
        total += (p - y) ** 2
    return total / len(predictions)


def calibration_curve(
    predictions: Sequence[float],
    outcomes: Sequence[int],
    bins: int = 10,
) -> list[dict[str, float | int]]:
    if bins < 2:
        raise ValueError("bins must be >= 2")
    if len(predictions) != len(outcomes):
        raise ValueError("predictions and outcomes length mismatch")
    edges = [i / bins for i in range(bins + 1)]
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in zip(predictions, outcomes):
        idx = min(int(p * bins), bins - 1)
        buckets[idx].append((p, y))
    rows: list[dict[str, float | int]] = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            rows.append(
                {
                    "bin_lo": edges[i],
                    "bin_hi": edges[i + 1],
                    "count": 0,
                    "mean_pred": None,
                    "empirical_freq": None,
                }
            )
            continue
        mean_pred = sum(p for p, _ in bucket) / len(bucket)
        empirical = sum(y for _, y in bucket) / len(bucket)
        rows.append(
            {
                "bin_lo": edges[i],
                "bin_hi": edges[i + 1],
                "count": len(bucket),
                "mean_pred": mean_pred,
                "empirical_freq": empirical,
            }
        )
    return rows


def reference_brier_baselines(outcomes: Iterable[int]) -> dict[str, float]:
    outs = list(outcomes)
    if not outs:
        return {"always_p_zero_dot_five": float("nan"), "always_base_rate": float("nan")}
    base_rate = sum(outs) / len(outs)
    half = brier_score([0.5] * len(outs), outs)
    base = brier_score([base_rate] * len(outs), outs)
    return {"always_p_zero_dot_five": half, "always_base_rate": base, "base_rate": base_rate}

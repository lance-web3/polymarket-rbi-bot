"""Provider-agnostic LLM probability-estimation wrapper.

Anthropic Claude (Messages API + tool_use forcing) is primary.
OpenAI Chat Completions (response_format: json_schema) is fallback.

Uses stdlib + `requests` only — matches the existing `deploy/classify_markets_openai.py`
pattern (no extra SDK dep). Caches the system prompt via Anthropic prompt-caching
to amortize the per-call cost across batches.

Single-market interface:
    client = LLMProbabilityClient(config)
    result = client.predict(question, description, niche_hint=None, condition_id=None)
    # result -> {"p_yes": 0.32, "confidence": 0.65, "top_evidence": [...],
    #            "what_would_flip": "...", "reasoning": "...", "niche_classification": "...",
    #            "_meta": {"provider": "anthropic", "model": "...", "latency_s": 1.2,
    #                      "input_tokens": ..., "output_tokens": ..., "cached_tokens": ...}}
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.config import BotConfig
from polymarket_rbi_bot.llm_prompts import (
    ANTHROPIC_TOOL,
    OPENAI_BATCH_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt_batch,
    build_user_prompt_single,
)

logger = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMProbabilityClient:
    config: BotConfig
    timeout: int = 90

    def predict(
        self,
        question: str,
        description: str,
        *,
        niche_hint: str | None = None,
        condition_id: str | None = None,
        extra_context: str | None = None,
    ) -> dict[str, Any]:
        provider = (self.config.llm_provider or "anthropic").lower()
        attempts: list[tuple[str, Exception]] = []
        order = [provider]
        if provider == "anthropic":
            order.append("openai")
        elif provider == "openai":
            order.append("anthropic")
        seen: set[str] = set()
        for prov in order:
            if prov in seen:
                continue
            seen.add(prov)
            try:
                if prov == "anthropic":
                    if not self.config.anthropic_api_key:
                        raise LLMError("ANTHROPIC_API_KEY not set")
                    return self._predict_anthropic(
                        question, description, niche_hint, condition_id, extra_context
                    )
                if prov == "openai":
                    if not self.config.openai_api_key:
                        raise LLMError("OPENAI_API_KEY not set")
                    return self._predict_openai_single(
                        question, description, niche_hint, condition_id, extra_context
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("provider=%s failed: %s", prov, e)
                attempts.append((prov, e))
        raise LLMError(f"All providers failed: {attempts!r}")

    # ------------------------------------------------------------------ Anthropic

    def _predict_anthropic(
        self,
        question: str,
        description: str,
        niche_hint: str | None,
        condition_id: str | None,
        extra_context: str | None,
    ) -> dict[str, Any]:
        user_prompt = build_user_prompt_single(question, description, niche_hint, extra_context)
        body = {
            "model": self.config.anthropic_model,
            "max_tokens": 1024,
            # `temperature` is deprecated on Opus 4.7+; the model produces deterministic-enough
            # outputs without it. Other models in the Claude family accept it but ignore it.
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": [ANTHROPIC_TOOL],
            "tool_choice": {"type": "tool", "name": ANTHROPIC_TOOL["name"]},
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "x-api-key": self.config.anthropic_api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": "prompt-caching-2024-07-31",
            "content-type": "application/json",
        }
        t0 = time.time()
        resp = self._post_with_retry(ANTHROPIC_URL, headers, body)
        latency = time.time() - t0
        payload = resp.json()
        content = payload.get("content") or []
        tool_use = next((c for c in content if c.get("type") == "tool_use"), None)
        if not tool_use:
            raise LLMError(f"Anthropic response missing tool_use: {payload}")
        result = dict(tool_use.get("input") or {})
        usage = payload.get("usage") or {}
        result["_meta"] = {
            "provider": "anthropic",
            "model": payload.get("model") or self.config.anthropic_model,
            "latency_s": round(latency, 3),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "prompt_version": PROMPT_VERSION,
            "condition_id": condition_id,
        }
        self._validate_prediction(result)
        return result

    # ------------------------------------------------------------------ OpenAI

    def _predict_openai_single(
        self,
        question: str,
        description: str,
        niche_hint: str | None,
        condition_id: str | None,
        extra_context: str | None,
    ) -> dict[str, Any]:
        # OpenAI wrapper: use the batch schema with a single-element results array
        market = {
            "condition_id": condition_id or "single",
            "question": question,
            "description": description,
            "niche_hint": niche_hint,
        }
        if extra_context:
            market["description"] = (market.get("description") or "") + "\n\nADDITIONAL CONTEXT:\n" + extra_context
        body = {
            "model": self.config.llm_predict_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt_batch([market])},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": OPENAI_BATCH_SCHEMA,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.config.openai_api_key}",
            "Content-Type": "application/json",
        }
        t0 = time.time()
        resp = self._post_with_retry(OPENAI_URL, headers, body)
        latency = time.time() - t0
        payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        results = parsed.get("results") or []
        if not results:
            raise LLMError(f"OpenAI response had empty results: {payload}")
        result = dict(results[0])
        result.pop("condition_id", None)
        usage = payload.get("usage") or {}
        result["_meta"] = {
            "provider": "openai",
            "model": payload.get("model") or self.config.llm_predict_model,
            "latency_s": round(latency, 3),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "prompt_version": PROMPT_VERSION,
            "condition_id": condition_id,
        }
        self._validate_prediction(result)
        return result

    # ------------------------------------------------------------------ shared

    def _post_with_retry(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> requests.Response:
        max_retries = max(1, int(self.config.llm_predict_max_retries))
        backoff = 1.5
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp
                # Retry on 429 + 5xx only. 4xx (auth/bad request/not found) is terminal.
                if resp.status_code in (429, 500, 502, 503, 504):
                    sleep_s = backoff ** attempt
                    logger.info(
                        "http %s on %s, sleeping %.1fs (attempt %d/%d)",
                        resp.status_code, url, sleep_s, attempt + 1, max_retries,
                    )
                    time.sleep(sleep_s)
                    last_err = LLMError(f"http {resp.status_code}: {resp.text[:300]}")
                    continue
                # Terminal 4xx — raise immediately with body so caller can diagnose
                raise LLMError(f"http {resp.status_code}: {resp.text[:300]}")
            except requests.exceptions.Timeout as e:
                last_err = e
                sleep_s = backoff ** attempt
                logger.info("timeout, sleeping %.1fs (attempt %d/%d)", sleep_s, attempt + 1, max_retries)
                time.sleep(sleep_s)
                continue
            except requests.exceptions.ConnectionError as e:
                last_err = e
                sleep_s = backoff ** attempt
                logger.info("connection error %s, sleeping %.1fs (attempt %d/%d)", e, sleep_s, attempt + 1, max_retries)
                time.sleep(sleep_s)
                continue
        raise LLMError(f"max retries exceeded: {last_err}")

    @staticmethod
    def _validate_prediction(result: dict[str, Any]) -> None:
        for field in ("p_yes", "confidence", "top_evidence", "what_would_flip", "reasoning", "niche_classification"):
            if field not in result:
                raise LLMError(f"prediction missing field {field!r}: {result!r}")
        p = result["p_yes"]
        c = result["confidence"]
        if not (0.0 <= float(p) <= 1.0):
            raise LLMError(f"p_yes out of range: {p!r}")
        if not (0.0 <= float(c) <= 1.0):
            raise LLMError(f"confidence out of range: {c!r}")
        # Clip strictly inside (0,1) for Brier-score safety
        result["p_yes"] = min(0.999, max(0.001, float(p)))
        result["confidence"] = min(1.0, max(0.0, float(c)))


# --- CLI: sanity test on hand-picked markets --------------------------------------

_SANITY_MARKETS = [
    {
        "question": "Will the Boston Celtics win the 2026 NBA Finals?",
        "description": "This market will resolve YES if the Boston Celtics win the 2026 NBA Finals championship.",
        "niche_hint": "sports_outright",
    },
    {
        "question": "Will the US Federal Reserve cut rates at its June 2026 FOMC meeting?",
        "description": "Resolves YES if the FOMC reduces the federal funds rate target at the June 2026 meeting.",
        "niche_hint": "regulatory_policy",
    },
    {
        "question": "Will Bitcoin close above $200,000 on June 30, 2026?",
        "description": "Resolves YES if the BTC-USD close price on 2026-06-30 is strictly greater than $200,000 per Coinbase.",
        "niche_hint": "crypto",
    },
]


def _cli_sanity(args: argparse.Namespace) -> None:
    config = BotConfig.from_env()
    if args.provider:
        config.llm_provider = args.provider
    client = LLMProbabilityClient(config)
    print(f"# Sanity test: provider={config.llm_provider} model_a={config.anthropic_model} model_o={config.llm_predict_model}")
    for m in _SANITY_MARKETS:
        print(f"\n--- {m['question']}")
        try:
            r = client.predict(
                m["question"],
                m["description"],
                niche_hint=m["niche_hint"],
            )
            print(json.dumps(
                {
                    "p_yes": r["p_yes"],
                    "confidence": r["confidence"],
                    "niche": r["niche_classification"],
                    "evidence": r["top_evidence"],
                    "flip": r["what_would_flip"],
                    "reasoning": (r["reasoning"] or "")[:240],
                    "meta": r["_meta"],
                },
                indent=2,
            ))
        except Exception as e:  # noqa: BLE001
            print(f"FAILED: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM probability-engine client (sanity-test runner).")
    sub = parser.add_subparsers(dest="cmd")
    p_san = sub.add_parser("sanity", help="Call the wrapper on 3 hand-picked markets and print results.")
    p_san.add_argument("--provider", choices=["anthropic", "openai"], default=None)
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if args.cmd == "sanity" or args.cmd is None:
        _cli_sanity(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

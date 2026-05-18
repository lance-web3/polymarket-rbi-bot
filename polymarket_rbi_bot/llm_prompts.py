"""Prompt + JSON-schema for the LLM probability engine.

Versioned so we can A/B prompts later. The schema is shared between Anthropic
(consumed via tool_use forcing) and OpenAI (consumed via response_format).
"""

from __future__ import annotations

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = (
    "You are a calibrated probability estimator for Polymarket prediction markets. "
    "Given a market question and short description, output your best estimate of the probability "
    "that the YES outcome resolves true.\n\n"
    "Discipline:\n"
    "- Base your estimate ONLY on information available up to your knowledge cutoff. "
    "If you do not know whether an event after your cutoff has occurred, do NOT guess — say so in `what_would_flip` and keep p_yes near the prior implied by the question class.\n"
    "- Calibration is more important than confidence. A wrong-direction prediction at high confidence costs more than a wide one at low confidence.\n"
    "- For winner-take-all markets with N obvious contenders, the uninformative prior is roughly 1/N. State this prior explicitly in `reasoning` if you have no domain edge.\n"
    "- If the question is unanswerable from public information at any time (e.g. private deal terms), output p_yes ≈ 0.5 and confidence ≤ 0.3.\n"
    "- Output ONLY the structured tool call. No prose outside the tool call.\n"
)

# JSON Schema used for both providers. Anthropic consumes it as a tool input_schema;
# OpenAI consumes it as a response_format json_schema (wrapped in {results: [...]}).
PREDICTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "p_yes": {
            "type": "number",
            "minimum": 0.001,
            "maximum": 0.999,
            "description": "Probability that the YES outcome resolves true. Strictly inside (0,1).",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "How much you trust your own p_yes estimate. 0 = uninformed prior; 1 = near-certain.",
        },
        "top_evidence": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "minItems": 1,
            "maxItems": 3,
            "description": "1-3 short bullet points supporting your estimate. Empty/null is not allowed; if you have no real evidence, say so explicitly.",
        },
        "what_would_flip": {
            "type": "string",
            "maxLength": 200,
            "description": "Short description of what new information would meaningfully change p_yes.",
        },
        "reasoning": {
            "type": "string",
            "maxLength": 800,
            "description": "Short paragraph (3-6 sentences) explaining your estimate.",
        },
        "niche_classification": {
            "type": "string",
            "enum": [
                "politics_election",
                "regulatory_policy",
                "sports_outright",
                "crypto",
                "awards_entertainment",
                "corporate_event",
                "breaking_news",
                "scheduled_event",
                "other",
            ],
            "description": "Which niche this market belongs to.",
        },
    },
    "required": [
        "p_yes",
        "confidence",
        "top_evidence",
        "what_would_flip",
        "reasoning",
        "niche_classification",
    ],
}

# For OpenAI's response_format, we wrap the single-market schema in a `results` array
# to match the existing classify_markets_openai.py shape (batched response).
OPENAI_BATCH_SCHEMA = {
    "name": "predict_probabilities_batch",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "condition_id": {"type": "string"},
                        **PREDICTION_SCHEMA["properties"],
                    },
                    "required": ["condition_id", *PREDICTION_SCHEMA["required"]],
                },
            }
        },
        "required": ["results"],
    },
}

# Anthropic uses tool definitions; one tool with the per-market schema. We call the tool
# once per market (no batching on Anthropic side) so prompt caching of the system prompt
# does the amortization instead.
ANTHROPIC_TOOL = {
    "name": "predict_probability",
    "description": "Submit your probability estimate for one Polymarket binary market.",
    "input_schema": PREDICTION_SCHEMA,
}


def build_user_prompt_single(
    question: str,
    description: str,
    niche_hint: str | None = None,
    extra_context: str | None = None,
) -> str:
    """User prompt for a single market (Anthropic path)."""
    parts = [
        "Estimate p_yes for this Polymarket market.\n",
        f"QUESTION:\n{question}\n",
    ]
    if description:
        parts.append(f"\nDESCRIPTION:\n{description.strip()[:2000]}\n")
    if niche_hint:
        parts.append(f"\nMARKET NICHE (heuristic, may be wrong): {niche_hint}\n")
    if extra_context:
        parts.append(f"\nADDITIONAL CONTEXT:\n{extra_context.strip()[:2000]}\n")
    parts.append(
        "\nReturn the predict_probability tool call. Do not include any text outside the tool call."
    )
    return "".join(parts)


def build_user_prompt_batch(
    markets: list[dict],
) -> str:
    """User prompt for a batch of markets (OpenAI path)."""
    compact = []
    for m in markets:
        compact.append(
            {
                "condition_id": m.get("condition_id"),
                "question": m.get("question"),
                "description": (m.get("description") or "")[:1500],
                "niche_hint": m.get("niche_hint") or m.get("heuristic_family"),
            }
        )
    import json as _json
    return (
        "Estimate p_yes for each Polymarket market below.\n"
        "Return the structured results JSON. Do not include any other text.\n\n"
        f"MARKETS:\n{_json.dumps(compact, indent=2)}"
    )

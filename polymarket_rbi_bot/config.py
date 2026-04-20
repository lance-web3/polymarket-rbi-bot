from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _env_tuple(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in os.getenv(name, default).split(',') if item.strip())


@dataclass(slots=True)
class BotConfig:
    host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    private_key: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    signature_type: int = 0
    funder_address: str | None = None
    token_id: str | None = None
    market_slug: str | None = None
    condition_id: str | None = None
    max_position_size: float = 100.0
    max_notional_per_order: float = 25.0
    daily_loss_limit: float = 100.0
    default_order_size: float = 10.0
    price_edge_bps: float = 20.0
    min_market_liquidity: float = 1000.0
    min_market_history_points: int = 100
    min_price: float = 0.1
    max_price: float = 0.9
    min_abs_return_bps_24h: float = 50.0
    buy_only_mode: bool = True
    long_entry_weight: float = 1.5
    macd_weight: float = 0.75
    rsi_weight: float = 0.35
    cvd_weight: float = 0.25
    buy_bias_multiplier: float = 1.15
    excluded_keywords: tuple[str, ...] = ()
    state_path: str = "data/live_state.json"
    require_live_decision_ts: bool = True
    max_decision_age_seconds: float = 30.0
    max_spread_bps: float = 250.0
    max_price_deviation_bps_from_mid: float = 150.0
    max_price_deviation_bps_from_quote: float = 100.0
    max_open_orders_total: int = 20
    max_open_orders_per_token: int = 1
    block_duplicate_token_orders: bool = True
    submission_cooldown_seconds: float = 30.0
    fill_cooldown_seconds: float = 60.0
    strict_strategy_mode: bool = False
    min_entry_confidence: float = 0.50
    min_buy_score: float = 1.3
    min_buy_sell_score_gap: float = 0.35
    min_buy_signal_count: int = 1
    strict_require_confirmers: bool = False
    strict_min_confirmers: int = 0
    strict_confirmer_buy_bonus: float = 0.08
    strict_confirmer_sell_penalty: float = 0.12
    strict_min_entry_score: float = 0.55
    min_hold_bars: int = 3
    cooldown_bars_after_exit: int = 2
    strict_max_hold_bars: int = 12
    strict_fail_exit_bars: int = 6
    strict_fail_exit_pnl_bps: float = -35.0
    strict_take_profit_bars: int = 4
    strict_take_profit_pnl_bps: float = 80.0
    strict_profit_giveback_bps: float = 45.0
    strict_extended_hold_bars: int = 8
    strict_extended_hold_exit_gap: float = 0.15
    estimated_round_trip_cost_bps: float = 80.0
    min_expected_edge_bps: float = 120.0
    edge_cost_buffer_bps: float = 30.0
    strict_min_price: float = 0.18
    strict_max_price: float = 0.68
    strict_excluded_keywords: tuple[str, ...] = ()
    market_family_mode: str = "balanced"
    allowed_market_families: tuple[str, ...] = ()
    blocked_market_families: tuple[str, ...] = ()
    family_allow_keywords: tuple[str, ...] = ()
    family_block_keywords: tuple[str, ...] = ()
    llm_market_classifier_path: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.4"
    openai_classifier_input_path: str = "data/market_classifier_input.json"
    openai_classifier_output_path: str = "data/market_classifier_output.json"
    openai_classifier_batch_size: int = 8
    openai_classifier_pause_seconds: float = 1.5
    # Maturity and microstructure gating
    enable_maturity_gating: bool = True
    enable_microstructure_gating: bool = True
    strict_min_time_to_resolution_hours: float | None = None
    strict_max_time_to_resolution_hours: float | None = None
    strict_min_time_since_open_hours: float | None = None
    strict_quote_lookback_bars: int = 24
    strict_min_quote_observations: int = 3
    strict_min_quote_availability_ratio: float = 0.25
    strict_max_avg_spread_bps: float = 450.0
    strict_max_current_spread_bps: float = 450.0
    strict_max_wide_spread_rate: float = 0.65
    strict_wide_spread_bps: float = 700.0

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "BotConfig":
        load_dotenv(env_file)
        excluded_keywords = _env_tuple("EXCLUDED_KEYWORDS", "gta vi,jesus christ")
        strict_excluded_keywords = _env_tuple("STRICT_EXCLUDED_KEYWORDS", "election,war,ceasefire,attack,assassination,indictment,sentenced,convicted,supreme court,sec,etf approval,fed rate,tariff,sanction")
        allowed_market_families = _env_tuple("ALLOWED_MARKET_FAMILIES", "sports_outright,crypto_outright,award_outright,entertainment_outright,scheduled_event")
        blocked_market_families = _env_tuple("BLOCKED_MARKET_FAMILIES", "news_breaking,legal_regulatory,war_geopolitics,disaster,assassination,discrete_binary,event_resolution")
        family_allow_keywords = _env_tuple("FAMILY_ALLOW_KEYWORDS", "qualify,win the,advance to,reach the playoffs,champion,top 4,top four,make the playoffs")
        family_block_keywords = _env_tuple("FAMILY_BLOCK_KEYWORDS", "sentenced,indicted,convicted,arrested,supreme court,appeal,ceasefire,attack,killed,resigns,resign,etf approval,fed rate,cpi,tariff,sanction,earthquake,hurricane")
        return cls(
            host=os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com"),
            gamma_host=os.getenv("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com"),
            chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
            private_key=os.getenv("PRIVATE_KEY"),
            api_key=os.getenv("API_KEY"),
            api_secret=os.getenv("API_SECRET"),
            api_passphrase=os.getenv("API_PASSPHRASE"),
            signature_type=int(os.getenv("SIGNATURE_TYPE", "0")),
            funder_address=os.getenv("FUNDER_ADDRESS"),
            token_id=os.getenv("TOKEN_ID"),
            market_slug=os.getenv("MARKET_SLUG"),
            condition_id=os.getenv("CONDITION_ID"),
            max_position_size=float(os.getenv("MAX_POSITION_SIZE", "100")),
            max_notional_per_order=float(os.getenv("MAX_NOTIONAL_PER_ORDER", "25")),
            daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT", "100")),
            default_order_size=float(os.getenv("DEFAULT_ORDER_SIZE", "10")),
            price_edge_bps=float(os.getenv("PRICE_EDGE_BPS", "20")),
            min_market_liquidity=float(os.getenv("MIN_MARKET_LIQUIDITY", "1000")),
            min_market_history_points=int(os.getenv("MIN_MARKET_HISTORY_POINTS", "100")),
            min_price=float(os.getenv("MIN_PRICE", "0.1")),
            max_price=float(os.getenv("MAX_PRICE", "0.9")),
            min_abs_return_bps_24h=float(os.getenv("MIN_ABS_RETURN_BPS_24H", "50")),
            buy_only_mode=os.getenv("BUY_ONLY_MODE", "true").strip().lower() in {"1", "true", "yes", "on"},
            long_entry_weight=float(os.getenv("LONG_ENTRY_WEIGHT", "1.5")),
            macd_weight=float(os.getenv("MACD_WEIGHT", "0.75")),
            rsi_weight=float(os.getenv("RSI_WEIGHT", "0.35")),
            cvd_weight=float(os.getenv("CVD_WEIGHT", "0.25")),
            buy_bias_multiplier=float(os.getenv("BUY_BIAS_MULTIPLIER", "1.15")),
            excluded_keywords=excluded_keywords,
            state_path=os.getenv("LIVE_STATE_PATH", "data/live_state.json"),
            require_live_decision_ts=os.getenv("REQUIRE_LIVE_DECISION_TS", "true").strip().lower() in {"1", "true", "yes", "on"},
            max_decision_age_seconds=float(os.getenv("MAX_DECISION_AGE_SECONDS", "30")),
            max_spread_bps=float(os.getenv("MAX_SPREAD_BPS", "250")),
            max_price_deviation_bps_from_mid=float(os.getenv("MAX_PRICE_DEVIATION_BPS_FROM_MID", "150")),
            max_price_deviation_bps_from_quote=float(os.getenv("MAX_PRICE_DEVIATION_BPS_FROM_QUOTE", "100")),
            max_open_orders_total=int(os.getenv("MAX_OPEN_ORDERS_TOTAL", "20")),
            max_open_orders_per_token=int(os.getenv("MAX_OPEN_ORDERS_PER_TOKEN", "1")),
            block_duplicate_token_orders=os.getenv("BLOCK_DUPLICATE_TOKEN_ORDERS", "true").strip().lower() in {"1", "true", "yes", "on"},
            submission_cooldown_seconds=float(os.getenv("SUBMISSION_COOLDOWN_SECONDS", "30")),
            fill_cooldown_seconds=float(os.getenv("FILL_COOLDOWN_SECONDS", "60")),
            strict_strategy_mode=os.getenv("STRICT_STRATEGY_MODE", "false").strip().lower() in {"1", "true", "yes", "on"},
            min_entry_confidence=float(os.getenv("MIN_ENTRY_CONFIDENCE", "0.50")),
            min_buy_score=float(os.getenv("MIN_BUY_SCORE", "1.1")),
            min_buy_sell_score_gap=float(os.getenv("MIN_BUY_SELL_SCORE_GAP", "0.35")),
            min_buy_signal_count=int(os.getenv("MIN_BUY_SIGNAL_COUNT", "1")),
            strict_require_confirmers=os.getenv("STRICT_REQUIRE_CONFIRMERS", "false").strip().lower() in {"1", "true", "yes", "on"},
            strict_min_confirmers=int(os.getenv("STRICT_MIN_CONFIRMERS", "0")),
            strict_confirmer_buy_bonus=float(os.getenv("STRICT_CONFIRMER_BUY_BONUS", "0.08")),
            strict_confirmer_sell_penalty=float(os.getenv("STRICT_CONFIRMER_SELL_PENALTY", "0.12")),
            strict_min_entry_score=float(os.getenv("STRICT_MIN_ENTRY_SCORE", "0.55")),
            min_hold_bars=int(os.getenv("MIN_HOLD_BARS", "3")),
            cooldown_bars_after_exit=int(os.getenv("COOLDOWN_BARS_AFTER_EXIT", "2")),
            strict_max_hold_bars=int(os.getenv("STRICT_MAX_HOLD_BARS", "12")),
            strict_fail_exit_bars=int(os.getenv("STRICT_FAIL_EXIT_BARS", "6")),
            strict_fail_exit_pnl_bps=float(os.getenv("STRICT_FAIL_EXIT_PNL_BPS", "-35")),
            strict_take_profit_bars=int(os.getenv("STRICT_TAKE_PROFIT_BARS", "4")),
            strict_take_profit_pnl_bps=float(os.getenv("STRICT_TAKE_PROFIT_PNL_BPS", "80")),
            strict_profit_giveback_bps=float(os.getenv("STRICT_PROFIT_GIVEBACK_BPS", "45")),
            strict_extended_hold_bars=int(os.getenv("STRICT_EXTENDED_HOLD_BARS", "8")),
            strict_extended_hold_exit_gap=float(os.getenv("STRICT_EXTENDED_HOLD_EXIT_GAP", "0.15")),
            estimated_round_trip_cost_bps=float(os.getenv("ESTIMATED_ROUND_TRIP_COST_BPS", "80")),
            min_expected_edge_bps=float(os.getenv("MIN_EXPECTED_EDGE_BPS", "120")),
            edge_cost_buffer_bps=float(os.getenv("EDGE_COST_BUFFER_BPS", "30")),
            strict_min_price=float(os.getenv("STRICT_MIN_PRICE", "0.18")),
            strict_max_price=float(os.getenv("STRICT_MAX_PRICE", "0.68")),
            strict_excluded_keywords=strict_excluded_keywords,
            market_family_mode=os.getenv("MARKET_FAMILY_MODE", "balanced").strip().lower(),
            allowed_market_families=allowed_market_families,
            blocked_market_families=blocked_market_families,
            family_allow_keywords=family_allow_keywords,
            family_block_keywords=family_block_keywords,
            llm_market_classifier_path=(os.getenv("LLM_MARKET_CLASSIFIER_PATH") or "").strip() or None,
            openai_api_key=(os.getenv("OPENAI_API_KEY") or "").strip() or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4").strip() or "gpt-5.4",
            openai_classifier_input_path=os.getenv("OPENAI_CLASSIFIER_INPUT_PATH", "data/market_classifier_input.json").strip() or "data/market_classifier_input.json",
            openai_classifier_output_path=os.getenv("OPENAI_CLASSIFIER_OUTPUT_PATH", "data/market_classifier_output.json").strip() or "data/market_classifier_output.json",
            openai_classifier_batch_size=int(os.getenv("OPENAI_CLASSIFIER_BATCH_SIZE", "8")),
            openai_classifier_pause_seconds=float(os.getenv("OPENAI_CLASSIFIER_PAUSE_SECONDS", "1.5")),
            enable_maturity_gating=os.getenv("ENABLE_MATURITY_GATING", "true").strip().lower() in {"1", "true", "yes", "on"},
            enable_microstructure_gating=os.getenv("ENABLE_MICROSTRUCTURE_GATING", "true").strip().lower() in {"1", "true", "yes", "on"},
            strict_min_time_to_resolution_hours=(
                float(os.getenv("STRICT_MIN_TTR_HOURS")) if os.getenv("STRICT_MIN_TTR_HOURS") not in {None, ""} else None
            ),
            strict_max_time_to_resolution_hours=(
                float(os.getenv("STRICT_MAX_TTR_HOURS")) if os.getenv("STRICT_MAX_TTR_HOURS") not in {None, ""} else None
            ),
            strict_min_time_since_open_hours=(
                float(os.getenv("STRICT_MIN_SINCE_OPEN_HOURS")) if os.getenv("STRICT_MIN_SINCE_OPEN_HOURS") not in {None, ""} else None
            ),
            strict_quote_lookback_bars=int(os.getenv("STRICT_QUOTE_LOOKBACK_BARS", "24")),
            strict_min_quote_observations=int(os.getenv("STRICT_MIN_QUOTE_OBSERVATIONS", "3")),
            strict_min_quote_availability_ratio=float(os.getenv("STRICT_MIN_QUOTE_AVAIL_RATIO", "0.25")),
            strict_max_avg_spread_bps=float(os.getenv("STRICT_MAX_AVG_SPREAD_BPS", "450")),
            strict_max_current_spread_bps=float(os.getenv("STRICT_MAX_CURRENT_SPREAD_BPS", "450")),
            strict_max_wide_spread_rate=float(os.getenv("STRICT_MAX_WIDE_SPREAD_RATE", "0.65")),
            strict_wide_spread_bps=float(os.getenv("STRICT_WIDE_SPREAD_BPS", "700")),
        )

    @property
    def has_l2_auth(self) -> bool:
        return bool(
            self.private_key
            and self.api_key
            and self.api_secret
            and self.api_passphrase
            and self.funder_address
        )

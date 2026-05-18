"""Quick read-only probe: is the Polymarket account configured + funded + tradeable?

Checks:
  1. Config loads cleanly (PRIVATE_KEY, API_KEY/SECRET/PASSPHRASE, FUNDER_ADDRESS, SIGNATURE_TYPE).
  2. py-clob-client can be instantiated with current creds (no signing/keys leaked).
  3. USDC balance + allowance via `get_balance_allowance`.
  4. Open orders via `get_orders`.
  5. Last fills via `get_trades` (if available).

Output is a single JSON status object. Nothing is written or mutated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.config import BotConfig

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
except ImportError as e:
    print(json.dumps({"status": "error", "stage": "import_py_clob_client", "error": str(e)}))
    sys.exit(1)


def _redact(s: str | None, keep: int = 4) -> str:
    if not s:
        return "<unset>"
    s = str(s).strip()
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}...{s[-keep:]}"


def main() -> None:
    config = BotConfig.from_env()
    out: dict[str, object] = {
        "config": {
            "host": config.host,
            "chain_id": config.chain_id,
            "signature_type": config.signature_type,
            "has_private_key": bool(config.private_key),
            "has_l2_creds": bool(config.api_key and config.api_secret and config.api_passphrase),
            "has_funder": bool(config.funder_address),
            "funder_address": _redact(config.funder_address, keep=6),
            "has_l2_auth": config.has_l2_auth,
        },
    }

    if not config.has_l2_auth:
        out["status"] = "not_funded"
        out["message"] = (
            "L2 auth incomplete. Need PRIVATE_KEY + API_KEY + API_SECRET + API_PASSPHRASE + "
            "FUNDER_ADDRESS in .env. Set SIGNATURE_TYPE=2 for proxied/funder wallets."
        )
        print(json.dumps(out, indent=2))
        sys.exit(2)

    # Connect (read-only — derives or uses L2 creds, no new keys created)
    try:
        api_creds = ApiCreds(
            api_key=config.api_key,
            api_secret=config.api_secret,
            api_passphrase=config.api_passphrase,
        )
        client = ClobClient(
            config.host,
            key=config.private_key,
            chain_id=config.chain_id,
            creds=api_creds,
            signature_type=config.signature_type,
            funder=config.funder_address,
        )
    except Exception as e:  # noqa: BLE001
        out["status"] = "error"
        out["stage"] = "connect"
        out["error"] = str(e)
        print(json.dumps(out, indent=2))
        sys.exit(3)

    # Balance + allowance
    try:
        bal = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type="COLLATERAL", token_id="")
        )
        out["balance_allowance"] = bal
    except Exception as e:  # noqa: BLE001
        out["balance_allowance_error"] = str(e)

    # Open orders
    try:
        orders = client.get_orders()
        out["open_orders_count"] = len(orders) if hasattr(orders, "__len__") else None
        out["open_orders_preview"] = orders[:5] if isinstance(orders, list) else orders
    except Exception as e:  # noqa: BLE001
        out["get_orders_error"] = str(e)

    # Recent trades (if available)
    try:
        # different versions of py-clob-client expose this differently
        if hasattr(client, "get_trades"):
            trades = client.get_trades()
            out["recent_trades_count"] = len(trades) if hasattr(trades, "__len__") else None
    except Exception as e:  # noqa: BLE001
        out["get_trades_error"] = str(e)

    # Verdict
    bal_dict = out.get("balance_allowance") or {}
    raw_balance = None
    if isinstance(bal_dict, dict):
        # py-clob-client returns balance as string in 6-decimal USDC units
        raw_balance = bal_dict.get("balance")
    if raw_balance is not None:
        try:
            usdc = float(raw_balance) / 1_000_000
            out["usdc_balance"] = round(usdc, 4)
        except (TypeError, ValueError):
            out["usdc_balance_raw"] = raw_balance

    usdc = out.get("usdc_balance")
    if isinstance(usdc, (int, float)) and usdc > 0:
        out["status"] = "funded_ok" if usdc >= 5 else "underfunded_for_$5_trades"
    elif usdc == 0:
        out["status"] = "no_usdc_balance"
    else:
        out["status"] = "balance_unknown"

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()

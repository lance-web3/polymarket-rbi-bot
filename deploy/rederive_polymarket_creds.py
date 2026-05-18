"""Read-only diagnostic: derive what the Polymarket CLOB server says our L2 creds
SHOULD be for the PRIVATE_KEY in .env, and compare to what's already there.

This is non-destructive: `create_or_derive_api_creds()` returns existing creds
if the wallet has any registered, and only creates new ones if none exist.

Output: a comparison + an updated .env block to copy-paste IF the values differ.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.config import BotConfig

try:
    from py_clob_client.client import ClobClient
except ImportError as e:
    print(json.dumps({"status": "error", "error": f"import failed: {e}"}))
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
    if not config.private_key:
        print(json.dumps({"status": "error", "error": "PRIVATE_KEY missing in .env"}))
        sys.exit(2)

    # Connect with PK only; ask server for/derive L2 creds
    try:
        temp = ClobClient(
            config.host,
            key=config.private_key,
            chain_id=config.chain_id,
            signature_type=config.signature_type,
            funder=config.funder_address,
        )
        derived = temp.create_or_derive_api_creds()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"status": "error", "stage": "derive", "error": str(e)}, indent=2))
        sys.exit(3)

    derived_key = getattr(derived, "api_key", None)
    derived_secret = getattr(derived, "api_secret", None)
    derived_pass = getattr(derived, "api_passphrase", None)

    matches = {
        "api_key_match": (str(config.api_key or "").strip() == str(derived_key or "").strip()),
        "api_secret_match": (str(config.api_secret or "").strip() == str(derived_secret or "").strip()),
        "api_passphrase_match": (str(config.api_passphrase or "").strip() == str(derived_pass or "").strip()),
    }

    out: dict[str, object] = {
        "current_env": {
            "API_KEY": _redact(config.api_key, keep=4),
            "API_SECRET": _redact(config.api_secret, keep=4),
            "API_PASSPHRASE": _redact(config.api_passphrase, keep=4),
        },
        "derived_from_server": {
            "API_KEY": _redact(derived_key, keep=4),
            "API_SECRET": _redact(derived_secret, keep=4),
            "API_PASSPHRASE": _redact(derived_pass, keep=4),
        },
        "matches": matches,
        "all_match": all(matches.values()),
    }

    if not out["all_match"]:
        # Write the fresh creds to a file the user can chmod 600 + diff against .env
        creds_file = Path("data/_derived_creds.env")
        creds_file.parent.mkdir(parents=True, exist_ok=True)
        creds_file.write_text(
            f"API_KEY={derived_key}\n"
            f"API_SECRET={derived_secret}\n"
            f"API_PASSPHRASE={derived_pass}\n",
            encoding="utf-8",
        )
        try:
            os.chmod(creds_file, 0o600)
        except Exception:  # noqa: BLE001
            pass
        out["fresh_creds_written_to"] = str(creds_file)
        out["next_step"] = (
            "Replace API_KEY/API_SECRET/API_PASSPHRASE in .env with the values in "
            "data/_derived_creds.env, then rerun `python -m deploy.check_polymarket_funding`. "
            "Delete data/_derived_creds.env after you're done."
        )
    else:
        out["next_step"] = (
            "Creds match what the server says they should be. The 401 may be from "
            "request signing/clock skew rather than the keys themselves. Try a clean session."
        )

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

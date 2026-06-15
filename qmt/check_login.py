"""Check miniQMT xtquant connectivity without sending orders."""
from __future__ import annotations

import argparse
import json
import random
from typing import Any

if __package__ in (None, ""):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from qmt.config import (
        ACCOUNT_ID,
        MINI_QMT_PATH,
        QMT_BACKTEST_ACCOUNT,
        TARGET_SYMBOL,
        XTQUANT_SITE_PACKAGES,
    )
    from qmt.xtquant_env import (
        ensure_xtquant_path,
        import_xtdata,
        import_xttrader_modules,
        xtquant_compatibility,
    )
else:
    from .config import (
        ACCOUNT_ID,
        MINI_QMT_PATH,
        QMT_BACKTEST_ACCOUNT,
        TARGET_SYMBOL,
        XTQUANT_SITE_PACKAGES,
    )
    from .xtquant_env import (
        ensure_xtquant_path,
        import_xtdata,
        import_xttrader_modules,
        xtquant_compatibility,
    )


def check_qmt_login(
    account_id: str = ACCOUNT_ID,
    symbol: str = TARGET_SYMBOL,
    force_account_query: bool = False,
) -> dict[str, Any]:
    site_path = ensure_xtquant_path(XTQUANT_SITE_PACKAGES)
    compatibility = xtquant_compatibility(site_path)
    result: dict[str, Any] = {
        "xtquant_site_packages": str(site_path),
        "xtquant_site_packages_exists": site_path.exists(),
        "xtquant_compatibility": compatibility,
        "mini_qmt_path": str(MINI_QMT_PATH),
        "mini_qmt_path_exists": MINI_QMT_PATH.exists(),
        "symbol": symbol,
        "market_data": {"ok": False},
        "trade_account": {"ok": False, "checked": bool(account_id)},
    }

    if site_path.exists() and not compatibility["matching_native_module"]:
        result["market_data"] = {
            "ok": False,
            "error": (
                "current Python cannot load miniQMT xtquant native module; "
                f"use Python matching {compatibility['native_modules']}"
            ),
        }
        result["trade_account"] = {
            "ok": False,
            "checked": bool(account_id),
            "message": "skipped because xtquant native module is incompatible with current Python",
        }
        return result

    try:
        xtdata = import_xtdata(site_path)
        client = xtdata.get_client()
        result["market_data"] = {
            "ok": True,
            "client_connected": bool(getattr(client, "is_connected", lambda: True)()),
        }
    except Exception as exc:
        result["market_data"] = {"ok": False, "error": repr(exc)}

    if not account_id:
        result["trade_account"] = {
            "ok": False,
            "checked": False,
            "message": "set QMT_ACCOUNT_ID or pass --account-id to check account login",
        }
        return result

    if account_id == QMT_BACKTEST_ACCOUNT and not force_account_query:
        result["trade_account"] = {
            "ok": True,
            "checked": True,
            "account_id": account_id,
            "account_type": "miniQMT built-in backtest account",
            "message": "testS is checked inside QMT model backtests; external xttrader asset query skipped",
        }
        return result

    trader = None
    try:
        xttrader, xttype = import_xttrader_modules(site_path)
        trader = xttrader.XtQuantTrader(str(MINI_QMT_PATH), random.randint(100000, 999999))
        account = xttype.StockAccount(account_id)
        trader.start()
        connect_result = trader.connect()
        subscribe_result = trader.subscribe(account) if connect_result == 0 else None
        asset = trader.query_stock_asset(account) if subscribe_result == 0 else None
        result["trade_account"] = {
            "ok": connect_result == 0 and subscribe_result == 0 and asset is not None,
            "checked": True,
            "account_id": account_id,
            "connect_result": connect_result,
            "subscribe_result": subscribe_result,
            "asset_found": asset is not None,
            "cash": float(getattr(asset, "cash", 0.0) or 0.0) if asset else None,
        }
    except Exception as exc:
        result["trade_account"] = {
            "ok": False,
            "checked": True,
            "account_id": account_id,
            "error": repr(exc),
        }
    finally:
        if trader is not None and hasattr(trader, "stop"):
            try:
                trader.stop()
            except Exception:
                pass
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Check miniQMT xtquant login/connectivity.")
    parser.add_argument("--account-id", default=ACCOUNT_ID)
    parser.add_argument("--symbol", default=TARGET_SYMBOL)
    parser.add_argument(
        "--force-account-query",
        action="store_true",
        help="query account assets even when account-id is testS",
    )
    args = parser.parse_args()

    result = check_qmt_login(
        account_id=args.account_id,
        symbol=args.symbol,
        force_account_query=args.force_account_query,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["market_data"]["ok"]:
        raise SystemExit(2)
    if result["trade_account"]["checked"] and not result["trade_account"]["ok"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()

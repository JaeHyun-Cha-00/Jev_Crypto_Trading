"""Facts read live instead of copied into the config: the coin list and Robinhood's quotes.

Both come from public endpoints that need no account or key:

- Robinhood's currency pairs (nummus.robinhood.com/currency_pairs/): which
  coins are tradable on Robinhood, and the pair id its quote endpoint takes.
- Robinhood's crypto quotes (api.robinhood.com/marketdata/forex/quotes/):
  bid, ask and mark for many pairs in one call.
- Coinbase Exchange's products (api.exchange.coinbase.com/products): which
  USD markets are online, since candles come from Coinbase.

With `data.symbols_live` on, the coins tracked are every Robinhood-tradable,
non-stablecoin coin with an online Coinbase USD market, less `data.exclude`.
Coins already in `data.symbols` keep their order there and new listings
follow alphabetically. If either list can't be read, `data.symbols` is used
as it is, so a Robinhood or Coinbase outage never stops a run.
"""

from __future__ import annotations

import json
import logging

from ..api.forward import HttpGet, _http_get, _redact

log = logging.getLogger(__name__)

ROBINHOOD_PAIRS_URL = "https://nummus.robinhood.com/currency_pairs/"
ROBINHOOD_QUOTES_URL = "https://api.robinhood.com/marketdata/forex/quotes/"
COINBASE_PRODUCTS_URL = "https://api.exchange.coinbase.com/products"
_HEADERS = {"Accept": "application/json", "User-Agent": "jevtrade"}
TIMEOUT_S = 20.0


def _get_json(http_get: HttpGet, url: str):
    return json.loads(http_get(url, _HEADERS, TIMEOUT_S))


def robinhood_pairs(http_get: HttpGet = _http_get) -> dict[str, dict]:
    """Coin code -> {"id", "stablecoin"} for every pair Robinhood trades against USD."""
    out, url = {}, ROBINHOOD_PAIRS_URL
    while url:
        page = _get_json(http_get, url)
        for r in page.get("results") or []:
            code = (r.get("asset_currency") or {}).get("code")
            if (code and r.get("tradability") == "tradable" and not r.get("display_only")
                    and (r.get("quote_currency") or {}).get("code") == "USD"):
                out[code] = {"id": r["id"], "stablecoin": bool(r.get("is_stablecoin"))}
        url = page.get("next")
    if not out:
        raise ValueError("Robinhood listed no tradable USD pairs")
    return out


def coinbase_usd_bases(http_get: HttpGet = _http_get) -> set[str]:
    """Base currencies with an online, tradable USD market on Coinbase Exchange."""
    rows = _get_json(http_get, COINBASE_PRODUCTS_URL)
    out = {p["base_currency"] for p in rows if isinstance(p, dict) and p.get("quote_currency") == "USD"
           and p.get("status") == "online" and not p.get("trading_disabled")}
    if not out:
        raise ValueError("Coinbase listed no online USD markets")
    return out


def live_symbols(configured: list[str], exclude: list[str], http_get: HttpGet = _http_get) -> list[str]:
    """Robinhood-tradable non-stablecoins with an online Coinbase USD market, less `exclude`."""
    rh = robinhood_pairs(http_get)
    cb = coinbase_usd_bases(http_get)
    skip = {s.split("/")[0] for s in exclude}
    codes = {c for c, p in rh.items() if not p["stablecoin"] and c in cb and c not in skip}
    known = [s for s in configured if s.split("/")[0] in codes]
    new = sorted(codes - {s.split("/")[0] for s in known})
    return known + [f"{c}/USD" for c in new]


def resolve_symbols(data_cfg, http_get: HttpGet = _http_get) -> list[str]:
    """Set `data_cfg.symbols` to the live list when `symbols_live` is on; keep it on any failure."""
    if not data_cfg.symbols_live:
        return data_cfg.symbols
    if data_cfg.exchange != "coinbaseexchange":
        log.warning("data.symbols_live needs exchange coinbaseexchange; using data.symbols")
        return data_cfg.symbols
    try:
        live = live_symbols(data_cfg.symbols, data_cfg.exclude, http_get)
    except Exception as e:  # noqa: BLE001 - any failure falls back to the configured list
        log.warning("live coin list unavailable (%s); using the %d coins in data.symbols",
                    _redact(f"{type(e).__name__}: {e}")[:300], len(data_cfg.symbols))
        return data_cfg.symbols
    added = [s for s in live if s not in data_cfg.symbols]
    dropped = [s for s in data_cfg.symbols if s not in live]
    if added or dropped:
        log.info("live coin list: %d coins (added %s, dropped %s)", len(live),
                 ", ".join(added) or "none", ", ".join(dropped) or "none")
    data_cfg.symbols = live
    return live


def robinhood_quotes(symbols: list[str], http_get: HttpGet = _http_get) -> dict[str, dict]:
    """Symbol -> {"bid", "ask"} from Robinhood's quotes right now, for the symbols it trades."""
    pairs = robinhood_pairs(http_get)
    ids = {pairs[s.split("/")[0]]["id"]: s for s in symbols if s.split("/")[0] in pairs}
    out: dict[str, dict] = {}
    batch = list(ids)
    for i in range(0, len(batch), 100):
        page = _get_json(http_get, ROBINHOOD_QUOTES_URL + "?ids=" + ",".join(batch[i:i + 100]))
        for q in page.get("results") or []:
            if not q or q.get("id") not in ids:
                continue
            try:
                bid, ask = float(q["bid_price"]), float(q["ask_price"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 < bid <= ask:
                out[ids[q["id"]]] = {"bid": bid, "ask": ask}
    return out

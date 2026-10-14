"""Jev's paper account, driven by its own choices (jevtrade.decision.agent).

One `Account` is stepped hour by hour over the forward log's decision rows.
The collector uses it to show Jev the account and position before asking
(`view`), and the dashboard replays the same rows through it (`replay`), so
both see the same account. Nothing here calls the model or an exchange.

Each hour `ts`, in order:
1. `mark`: every coin logged that hour is valued at its candle close, and a
   held coin's best and worst price since the buy take in the candle's high
   and low.
2. `view`: what Jev is shown for a coin at that close.
3. `apply`: the hour's answers are executed. Every `sell` fills first, then
   every `buy_*`, most confident first (p of the chosen size), at the
   chosen share of equity (`jev_paper.sizes`). A buy is cut to the cash
   left and skipped when none is. An error or abstain does nothing.

A fill is at the Coinbase price logged when Jev was asked (`cb_price`), or
the candle's close for a row without one (a backfilled hour), and pays
`fee_bps` plus the coin's estimated spread (`JevPaperConfig.spread_for`).
There is no stop-loss, time limit or position cap: a position stays open
until Jev sells it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from statistics import median

from ..decision.agent import BUY_OPTIONS, PROMPT_VERSION, SIZE_OF, View
from .settings import JevPaperConfig


def _num(v) -> float | None:
    return None if v is None else float(v)


def choice(row: dict) -> tuple[str | None, str | None, dict[str, float]]:
    """(question, chosen option, probabilities) of an answered row; (question, None, {}) otherwise."""
    q = row.get("question")
    if row.get("status") != "answered" or q is None:
        return q, None, {}
    ans = (row.get("answers") or {}).get(q) or {}
    probs = {k: float(v) for k, v in (ans.get("probabilities") or {}).items()}
    return q, ans.get("choice"), probs


@dataclass
class Holding:
    qty: float
    entry_price: float
    entry_ts: int
    entry_fee: float
    entry_reason: str
    best: float       # highest price since the buy
    worst: float      # lowest price since the buy


@dataclass
class Account:
    paper: JevPaperConfig
    tf_ms: int
    cash: float = 0.0
    holdings: dict[str, Holding] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    volumes: dict[str, deque] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    curve: list[dict] = field(default_factory=list)
    last_ts: int | None = None   # last hour applied

    @classmethod
    def new(cls, paper: JevPaperConfig, tf_ms: int) -> "Account":
        return cls(paper, tf_ms, cash=paper.initial_equity)

    # -- valuation

    def equity(self) -> float:
        return self.cash + sum(h.qty * self.marks.get(s, h.entry_price) for s, h in self.holdings.items())

    def spread(self, sym: str) -> float:
        """Per-side spread in bps, from the coin's median dollar volume over its last logged hours."""
        v = self.volumes.get(sym)
        return self.paper.spread_for(median(v) if v else None)

    def side_cost(self, sym: str) -> float:
        return (self.paper.fee_bps + self.paper.slippage_bps + self.spread(sym)) / 10_000

    def mark(self, rows: list[dict]) -> None:
        """Value each coin logged this hour at its close; track a held coin's range since the buy."""
        for r in rows:
            if r.get("close") is None:
                continue
            sym, close = r["symbol"], float(r["close"])
            self.marks[sym] = close
            vol = _num(r.get("volume"))
            if vol is not None:
                self.volumes.setdefault(sym, deque(maxlen=self.paper.volume_bars)).append(vol * close)
            h = self.holdings.get(sym)
            if h is not None and r["candle_ts"] > h.entry_ts:
                h.best = max(h.best, _num(r.get("high")) or close, close)
                h.worst = min(h.worst, _num(r.get("low")) or close, close)

    def view(self, sym: str, ts: int) -> View:
        """What Jev is shown at the close of hour `ts`, after `mark`."""
        eq = self.equity()
        account = {
            "cash_pct_of_equity": round(self.cash / eq * 100, 1) if eq > 0 else 0.0,
            "assets_held": len(self.holdings),
        }
        h = self.holdings.get(sym)
        if h is None:
            return View(account, None)
        px = self.marks.get(sym, h.entry_price)
        ret = lambda p: round((p / h.entry_price - 1) * 100, 2)  # noqa: E731
        return View(account, {
            "hours_held": (ts - h.entry_ts) // self.tf_ms,
            "return_since_buy_pct": ret(px),
            "best_since_buy_pct": ret(max(h.best, px)),
            "worst_since_buy_pct": ret(min(h.worst, px)),
            "share_of_equity_pct": round(h.qty * px / eq * 100, 1) if eq > 0 else 0.0,
        })

    # -- execution

    def _fill_price(self, row: dict) -> float:
        p = _num(row.get("cb_price"))
        return p if p else float(row["close"])

    def _sell(self, row: dict, reason: str) -> dict:
        sym, ts = row["symbol"], row["candle_ts"]
        h = self.holdings.pop(sym)
        px = self._fill_price(row) * (1 - (self.paper.slippage_bps + self.spread(sym)) / 10_000)
        fee = h.qty * px * self.paper.fee_bps / 10_000
        self.cash += h.qty * px - fee
        cost = h.qty * h.entry_price
        pnl = h.qty * px - cost - fee - h.entry_fee
        self.marks[sym] = self._fill_price(row)
        t = {"symbol": sym, "entry_ts": h.entry_ts, "entry_price": h.entry_price, "exit_ts": ts,
             "exit_price": px, "qty": h.qty, "fees": fee + h.entry_fee, "pnl": pnl,
             "ret": pnl / cost if cost else 0.0, "bars_held": (ts - h.entry_ts) // self.tf_ms,
             "exit_reason": reason, "entry_reason": h.entry_reason}
        self.trades.append(t)
        return t

    def _buy(self, row: dict, frac: float, equity: float, reason: str) -> bool:
        sym, ts = row["symbol"], row["candle_ts"]
        px = self._fill_price(row) * (1 + (self.paper.slippage_bps + self.spread(sym)) / 10_000)
        fee_rate = self.paper.fee_bps / 10_000
        notional = min(frac * equity, self.cash / (1 + fee_rate))
        if notional < self.paper.min_order_usd:
            return False
        fee = notional * fee_rate
        self.cash -= notional + fee
        self.holdings[sym] = Holding(notional / px, px, ts, fee, reason, px, px)
        self.marks[sym] = self._fill_price(row)
        return True

    def apply(self, ts: int, rows: list[dict]) -> list[dict]:
        """Execute hour `ts`'s answers (after `mark`); returns one action record per row."""
        out: dict[str, dict] = {}
        sells, buys = [], []
        for r in rows:
            q, pick, probs = choice(r)
            sym = r["symbol"]
            rec = {"symbol": sym, "bar_ts": ts, "close": _num(r.get("close")), "status": r.get("status"),
                   "question": q, "choice": pick, "p": probs, "action": "none", "reason": "", "size_frac": 0.0}
            out[sym] = rec
            if pick is None:
                rec["reason"] = r.get("abstain_reason") or "no answer"
            elif q == "manage" and pick == "sell" and sym in self.holdings:
                sells.append(r)
            elif q == "entry" and pick in BUY_OPTIONS and sym not in self.holdings:
                buys.append(r)
            else:
                rec["action"] = "hold" if q == "manage" else "wait"
                rec["reason"] = f"{pick} ({probs.get(pick, 0):.2f})"
        for r in sells:
            rec = out[r["symbol"]]
            reason = f"sell ({rec['p'].get('sell', 0):.2f})"
            t = self._sell(r, reason)
            rec.update(action="sell", reason=f"{reason}: {t['ret']:+.1%} after costs")
        equity = self.equity()   # sizes are shares of the equity after this hour's sells
        for r in sorted(buys, key=lambda r: (-out[r["symbol"]]["p"].get(out[r["symbol"]]["choice"], 0),
                                              r["symbol"])):
            rec = out[r["symbol"]]
            size = SIZE_OF[rec["choice"]]
            frac = self.paper.sizes[size]
            reason = f"{rec['choice']} ({rec['p'].get(rec['choice'], 0):.2f})"
            if self._buy(r, frac, equity, reason):
                rec.update(action="buy", reason=reason, size_frac=frac)
            else:
                rec.update(action="no_cash", reason=f"{reason}: not enough cash left")
        self.last_ts = ts
        recs = list(out.values())
        self.actions.extend(recs)
        self.curve.append({"bar_ts": ts, "equity": self.equity(), "cash": self.cash,
                           "holdings": {s: h.qty for s, h in self.holdings.items()}})
        return recs

    def step(self, ts: int, rows: list[dict]) -> list[dict]:
        self.mark(rows)
        return self.apply(ts, rows)


def by_hour(rows: list[dict]) -> dict[int, list[dict]]:
    """Rows grouped by candle, one per coin (a later line for the same coin and hour wins)."""
    hours: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in sorted(rows, key=lambda r: str(r.get("called_at") or "")):
        hours[r["candle_ts"]][r["symbol"]] = r
    return {ts: sorted(hs.values(), key=lambda r: r["symbol"]) for ts, hs in sorted(hours.items())}


def run(rows: list[dict], paper: JevPaperConfig, tf_ms: int, until_ts: int | None = None) -> Account:
    """The account after every logged hour before `until_ts` (all of them when None)."""
    acct = Account.new(paper, tf_ms)
    for ts, hour in by_hour(rows).items():
        if until_ts is not None and ts >= until_ts:
            break
        acct.step(ts, hour)
    return acct


def _hour(ts: int, rows: list[dict], acts: list[dict], acct: Account, market: float | None,
          horizon_ms: int) -> dict:
    """One hour for the dashboard: the coins Jev chose to buy (bought or not, with their return over
    the horizon once it has closed), what it sold, and the average coin's return over the same hours."""
    by_sym = {a["symbol"]: a for a in acts}
    outs = {r["symbol"]: r.get("outcome") for r in rows}
    picks = []
    for a in acts:
        if a["question"] != "entry" or a["choice"] not in BUY_OPTIONS:
            continue
        o = outs.get(a["symbol"])
        ret = o["ret_pct"] / 100 if o else None
        picks.append({"symbol": a["symbol"], "choice": a["choice"], "p": round(a["p"].get(a["choice"], 0), 4),
                      "action": a["action"], "ret": None if ret is None else round(ret, 6),
                      "net": None if ret is None else round(ret - 2 * acct.side_cost(a["symbol"]), 6)})
    picks.sort(key=lambda p: (-p["p"], p["symbol"]))
    return {
        "bar_ts": ts,
        "asked": len(rows),
        "answered": sum(1 for r in rows if r.get("status") == "answered"),
        "picks": picks,
        "bought": [a["symbol"] for a in acts if a["action"] == "buy"],
        "sold": [{"symbol": a["symbol"], "why": a["reason"]} for a in acts if a["action"] == "sell"],
        "market": market,
        "resolves_at": ts + horizon_ms + acct.tf_ms,
    }


def replay(rows: list[dict], app_cfg, tf_ms: int) -> dict:
    """Jev's account after every logged hour in `rows` (the forward log's joined rows, any order)."""
    paper = app_cfg.jev_paper
    horizon_ms = app_cfg.decision.horizon_bars * tf_ms
    acct = Account.new(paper, tf_ms)
    base = {"initial_equity": paper.initial_equity, "fee_bps": paper.fee_bps, "slippage_bps": paper.slippage_bps,
            "spread_bps": {"min": paper.spread_bps, "max": paper.spread_bps + paper.thin_extra_bps},
            "sizes": dict(paper.sizes), "side_cost": paper.typical_side_cost(),
            "prompt_version": PROMPT_VERSION}
    hours_rows = by_hour(rows)
    # The average logged coin's return over the horizon from each hour, once it has closed.
    rets: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("outcome"):
            rets[r["candle_ts"]].append(r["outcome"]["ret_pct"] / 100)
    market = {t: sum(v) / len(v) for t, v in rets.items()}

    hours, peak = [], paper.initial_equity
    counts: dict[str, int] = defaultdict(int)
    for ts, hour in hours_rows.items():
        acts = acct.step(ts, hour)
        for a in acts:
            counts[a["action"]] += 1
        peak = max(peak, acct.equity())
        hours.append(_hour(ts, hour, acts, acct, market.get(ts), horizon_ms))
    if hours_rows:   # an hour the collector skipped still gets a row, so a gap shows
        logged = set(hours_rows)
        first, last = min(logged), max(logged)
        hours += [{"bar_ts": t, "asked": 0, "answered": 0, "picks": [], "bought": [], "sold": [], "market": None,
                   "resolves_at": t + horizon_ms + tf_ms} for t in range(first, last, tf_ms) if t not in logged]

    eq = acct.equity()
    per_symbol: dict[str, dict] = {}
    for sym in sorted({r["symbol"] for r in rows}):
        ts_ = [t for t in acct.trades if t["symbol"] == sym]
        per_symbol[sym] = {"trades": len(ts_), "pnl": sum(t["pnl"] for t in ts_),
                           "buys": sum(1 for a in acct.actions if a["symbol"] == sym and a["action"] == "buy")}
    last_ts = acct.last_ts
    positions = []
    for s, h in sorted(acct.holdings.items()):
        mark = acct.marks.get(s, h.entry_price)
        positions.append({"symbol": s, "qty": h.qty, "entry_price": h.entry_price, "entry_ts": h.entry_ts,
                          "mark": mark, "notional": h.qty * mark, "unrealized_pnl": h.qty * (mark - h.entry_price),
                          "hours_held": (last_ts - h.entry_ts) // tf_ms if last_ts is not None else 0,
                          "entry_reason": h.entry_reason})
    return {
        **base,
        "equity": eq, "cash": acct.cash, "total_return": eq / paper.initial_equity - 1, "drawdown": eq / peak - 1,
        "last_bar_ts": last_ts,
        "trades": sorted(acct.trades, key=lambda t: -t["exit_ts"]),
        "positions": positions,
        "curve": acct.curve,
        "actions": acct.actions[::-1],   # newest first
        "buys": [a for a in reversed(acct.actions) if a["action"] == "buy"],   # every buy, newest first
        "hours": sorted(hours, key=lambda h: -h["bar_ts"]),   # every hour, newest first
        "calls": len(acct.actions),
        "counts": dict(counts),
        "per_symbol": per_symbol,
    }


def detail_curve(curve: list[dict], candles: dict[str, list[dict]], start_ms: int, end_ms: int,
                 step_ms: int, tf_ms: int) -> list[dict]:
    """The account every `step_ms` in [start_ms, end_ms], at finer prices than the hourly replay.

    `curve` is the replay's hourly curve: each point holds the cash and coins
    (`holdings`) at the close of its candle, `tf_ms` after `bar_ts`. Between two
    closes the account keeps the earlier one's cash and coins, valued at each
    coin's `candles` close (Coinbase candles at `step_ms`, `ts` = candle open,
    so a candle's close is at `ts + step_ms`). A coin with no candle yet keeps
    its last known price, or its entry into the account at the hourly equity.
    Fills inside an hour therefore show at that hour's close.
    """
    if not curve:
        return []
    closes = [p["bar_ts"] + tf_ms for p in curve]
    prices = {s: {c["ts"] + step_ms: c["close"] for c in cs} for s, cs in candles.items()}
    last_px: dict[str, float] = {}
    out = []
    t = start_ms - start_ms % step_ms + step_ms
    i = -1
    while t <= end_ms:
        while i + 1 < len(closes) and closes[i + 1] <= t:
            i += 1
        for s, by_t in prices.items():
            if t in by_t:
                last_px[s] = by_t[t]
        if i >= 0:
            p = curve[i]
            held = p.get("holdings") or {}
            if all(s in last_px for s in held):
                out.append({"ts": t, "equity": p["cash"] + sum(q * last_px[s] for s, q in held.items())})
            elif t == closes[i]:
                out.append({"ts": t, "equity": p["equity"]})
        t += step_ms
    return out


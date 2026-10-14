"""The fixed prompt Jev trades with: a mandate, the account and position it is
shown, and the two questions it chooses an action from.

Every hour, just after each 1h candle closes, Jev is asked about each coin
once. A coin the account does not hold gets `entry` (buy small, normal or
large, or wait); a coin it holds gets `manage` (hold or sell). Jev's choice is
the trade: code only executes it (jevtrade.forward.agent), within the cash
the account has. There is no stop-loss, time limit or position cap.

The wording is generated from config (sizes and costs) so the numbers Jev is
told are the ones the account uses. `PROMPT_VERSION` names this wording;
docs/jev_prompt.md shows it as sent, and tests/test_agent.py checks the two
match. Change the version whenever the wording changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..state.builder import MarketState, estimate_tokens
from .base import QuestionSpec

PROMPT_VERSION = "agent-1"
BUY_OPTIONS = ("buy_small", "buy_normal", "buy_large")
SIZE_OF = {"buy_small": "small", "buy_normal": "normal", "buy_large": "large"}


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


def mandate(side_cost: float) -> str:
    """The standing instructions Jev reads in every state. `side_cost` is one fill's cost, a fraction."""
    side = f"{side_cost * 100:.1f}%"
    trip = f"{side_cost * 200:.1f}%"
    return (
        "You run a long-only spot crypto account and decide for one asset at a time, once an hour, "
        "just after each 1h bar closes. You can only buy the asset with cash and hold it, or sell it "
        "back to cash: no shorting, no leverage. "
        f"Every buy and every sell costs about {side} of the amount traded (taker fee plus spread), "
        f"so a round trip costs about {trip}. "
        "Your goal is the account's profit after costs; trading has no value in itself. Buy only when "
        "you expect the move to more than repay the costs, and sell only when holding no longer pays. "
        "Nothing else will sell for you: there is no stop-loss and no time limit, so a position you "
        "hold stays open until you sell it."
    )


def entry_question(sizes: dict[str, float], side_cost: float) -> QuestionSpec:
    trip = f"{side_cost * 200:.1f}%"
    return QuestionSpec(
        id="entry",
        instructions=(
            "You do not hold this asset (`position` is null). Under the mandate in `mandate`, and "
            "given the asset's `market` state and your `account`, what do you do now?"
        ),
        options={
            "buy_small": f"Buy about {_pct(sizes['small'])} of the account's equity: an edge that "
                         f"should repay the {trip} round trip, but with real doubt.",
            "buy_normal": f"Buy about {_pct(sizes['normal'])} of the account's equity: a clear edge "
                          "that should repay the costs with room to spare.",
            "buy_large": f"Buy about {_pct(sizes['large'])} of the account's equity: a strong, "
                         "unusually clear edge.",
            "wait": "Do not buy: no edge large enough to repay the costs, or the setup is unclear.",
        },
    )


def manage_question(side_cost: float) -> QuestionSpec:
    side = f"{side_cost * 100:.1f}%"
    return QuestionSpec(
        id="manage",
        instructions=(
            "You hold this asset (see `position`). Under the mandate in `mandate`, and given the "
            "asset's `market` state, your `position` and your `account`, do you keep it or sell it now?"
        ),
        options={
            "hold": "Keep it: the reason to own it still stands, and holding should pay more than "
                    "selling now. A pullback or a quiet stretch that doesn't break that reason is not "
                    "a reason to sell.",
            "sell": "Sell all of it now: the reason to own it has broken, or the gain to expect from "
                    "here no longer justifies the risk of holding. Selling costs about "
                    f"{side} of the position.",
        },
    )


def questions(sizes: dict[str, float], side_cost: float) -> dict[str, QuestionSpec]:
    """Both questions, by id: `entry` for a coin the account doesn't hold, `manage` for one it does."""
    return {"entry": entry_question(sizes, side_cost), "manage": manage_question(side_cost)}


def configured(app_cfg) -> tuple[dict[str, QuestionSpec], float]:
    """The questions and the per-fill cost Jev is told, from the account settings (`jev_paper`)."""
    paper = app_cfg.jev_paper
    side = paper.typical_side_cost()
    return questions(paper.sizes, side), side


@dataclass(frozen=True)
class View:
    """What Jev is shown about the account and one coin's position, at one hour's close."""
    account: dict
    position: dict | None   # None: the account doesn't hold the coin

    def as_dict(self) -> dict:
        return {"account": self.account, "position": self.position}


def agent_state(market: MarketState, view: View, side_cost: float) -> MarketState:
    """The state Jev is shown: the mandate, the coin's anonymized market state, the account and the position."""
    m = json.loads(market.text)
    m.pop("horizon_bars", None)   # the old direction question's horizon; Jev picks its own now
    state = {"mandate": mandate(side_cost), "market": m, **view.as_dict()}
    text = json.dumps(state, separators=(",", ":"))
    return MarketState(text=text, features=market.features, est_tokens=estimate_tokens(text))

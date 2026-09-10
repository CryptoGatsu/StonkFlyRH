"""The guarded action boundary between the neural decoder and the chain.

Upstream wrapped execution in a Coinbase AgentKit ActionProvider. This fork
trades on Robinhood Chain and has no Coinbase dependency, so the same boundary
is kept natively: one declared action, a schema-validated proposal, and a path
to the router that runs through the risk guard every time.

The decoder invokes this action directly. There is no LLM in the loop, no
general wallet tool, and no transfer action: the only thing this provider can
do is submit one guarded swap.
"""

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    side: Literal["BUY", "SELL"]


class Action:
    def __init__(self, name, description, args_schema, invoke):
        self.name = name
        self.description = description
        self.args_schema = args_schema
        self.invoke = invoke


class StonkflyRHActions:
    """Action provider for Robinhood Chain memecoin swaps."""

    name = "stonkflyrh"

    def __init__(self, guard, broker, network_id):
        self.guard = guard
        self.broker = broker
        self.network_id = network_id
        self.quotes = {}
        self.gas_price_wei = None

    def supports_network(self, network):
        return (
            getattr(network, "protocol_family", "evm") == "evm"
            and getattr(network, "network_id", None) == self.network_id
        )

    def get_actions(self, wallet_provider=None):
        return [
            Action(
                name="stonkflyrh_swap",
                description=(
                    "Submit a budget-checked, slippage-bounded Uniswap v3 "
                    "exactInputSingle swap on Robinhood Chain from a neural proposal."
                ),
                args_schema=Proposal,
                invoke=self.invoke,
            )
        ]

    def invoke(self, args):
        p = Proposal.model_validate(args)
        plan = self.guard.plan(
            p.product, p.side, self.quotes, gas_price_wei=self.gas_price_wei
        )
        plan["neural_observation"] = self.guard.l.get("observation")
        plan["checkpoint"] = self.guard.l.get("checkpoint")
        plan = self.guard.l.reserve(plan, time.time())
        return self.broker.execute(plan, self.guard.before_submit)

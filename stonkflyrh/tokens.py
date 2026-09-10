"""Operator-supplied contract registry, verified against the chain before use.

No memecoin address, router or quoter is hardcoded in this repository. The
registry is a local JSON file the operator fills in and is checked at preflight:
addresses must hold code, the quoter and the router must report the same
factory, each token must report the symbol and decimals the registry claims,
and every traded pair must have a pool at the configured fee tier. A registry
that does not survive those checks stops the run before any order exists.
"""

import json
import os
from pathlib import Path

from .chain import FACTORY_ABI, QUOTER_ABI, ROUTER_ABI, ZERO_ADDRESS, checksum
from .config import QUOTE_SYMBOL, SYMBOL

REGISTRY_ENV = "STONKFLYRH_TOKENS"
DEFAULT_REGISTRY = "tokens.json"


def registry_path(path=None):
    return Path(path or os.environ.get(REGISTRY_ENV) or DEFAULT_REGISTRY).resolve()


class Registry:
    def __init__(self, network_key, data):
        self.network_key = network_key
        section = data.get(network_key)
        if not isinstance(section, dict):
            raise RuntimeError(f"Registry has no '{network_key}' section")
        quote = section.get("quote") or {}
        if quote.get("symbol") != QUOTE_SYMBOL or int(quote.get("decimals", 0)) != 18:
            raise RuntimeError(f"Registry quote asset must be {QUOTE_SYMBOL} with 18 decimals")
        self.quote_address = checksum(quote["address"])
        self.quote_decimals = 18
        self.router = checksum(section["router"])
        self.quoter = checksum(section["quoter"])
        # Optional: a Chainlink ETH/USD aggregator, or a stablecoin to price
        # against through the quoter. One of them is needed for dollar limits.
        self.eth_usd_feed = (
            checksum(section["eth_usd_feed"]) if section.get("eth_usd_feed") else None
        )
        self.stable = None
        if section.get("stable"):
            stable = dict(section["stable"])
            if not SYMBOL.match(stable.get("symbol", "")):
                raise RuntimeError("Invalid stablecoin symbol in registry")
            stable["address"] = checksum(stable["address"])
            stable["decimals"] = int(stable["decimals"])
            if not 0 < stable["decimals"] <= 36:
                raise RuntimeError("Implausible stablecoin decimals")
            self.stable = stable
        self.tokens = {}
        for symbol, info in (section.get("tokens") or {}).items():
            if not SYMBOL.match(symbol) or symbol == QUOTE_SYMBOL:
                raise RuntimeError("Invalid registry symbol: " + str(symbol))
            decimals = int(info["decimals"])
            if not 0 <= decimals <= 36:
                raise RuntimeError("Implausible token decimals: " + symbol)
            entry = {
                "symbol": symbol,
                "address": checksum(info["address"]),
                "decimals": decimals,
            }
            if "pool_fee" in info:
                entry["pool_fee"] = int(info["pool_fee"])
            if entry["address"] == self.quote_address:
                raise RuntimeError("A memecoin entry cannot be the quote asset")
            self.tokens[symbol] = entry
        if len(set(t["address"] for t in self.tokens.values())) != len(self.tokens):
            raise RuntimeError("Duplicate token address in registry")

    @classmethod
    def load(cls, network_key, path=None):
        target = registry_path(path)
        if not target.exists():
            raise RuntimeError(
                f"No contract registry at {target}. Copy tokens.example.json to "
                f"{target.name} and fill in the Robinhood Chain addresses you verified."
            )
        return cls(network_key, json.loads(target.read_text()))

    def token(self, symbol):
        if symbol not in self.tokens:
            raise RuntimeError(f"{symbol} is not in the registry for {self.network_key}")
        return self.tokens[symbol]

    def pool_fee(self, symbol, default_tier):
        return int(self.token(symbol).get("pool_fee", default_tier))

    def verify(self, client, products, default_tier):
        """Check every address this run would touch against the chain itself."""
        client.require_code(self.router, "Router")
        client.require_code(self.quoter, "Quoter")
        quote = client.token_identity(self.quote_address)
        if quote["symbol"] != QUOTE_SYMBOL or quote["decimals"] != 18:
            raise RuntimeError("Configured quote address is not 18-decimal WETH")
        router = client.contract(self.router, ROUTER_ABI)
        quoter = client.contract(self.quoter, QUOTER_ABI)
        try:
            factory = checksum(router.functions.factory().call())
            if checksum(quoter.functions.factory().call()) != factory:
                raise RuntimeError("Router and quoter report different factories")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError("Router/quoter did not answer factory()") from e
        client.require_code(factory, "Factory")
        pools = {}
        factory_contract = client.contract(factory, FACTORY_ABI)
        for symbol in products:
            entry = self.token(symbol)
            identity = client.token_identity(entry["address"])
            if identity["symbol"] != symbol or identity["decimals"] != entry["decimals"]:
                raise RuntimeError(
                    f"On-chain token at {entry['address']} is "
                    f"{identity['symbol']}/{identity['decimals']}, not {symbol}/{entry['decimals']}"
                )
            fee = self.pool_fee(symbol, default_tier)
            pool = checksum(
                factory_contract.functions.getPool(
                    checksum(entry["address"]), self.quote_address, fee
                ).call()
            )
            if pool == ZERO_ADDRESS or not client.has_code(pool):
                raise RuntimeError(f"No {symbol}/{QUOTE_SYMBOL} pool at fee tier {fee}")
            pools[symbol] = {"pool": pool, "fee": fee}
        reference = None
        if self.eth_usd_feed:
            client.require_code(self.eth_usd_feed, "ETH/USD feed")
            reference = {"kind": "chainlink", "address": self.eth_usd_feed}
        elif self.stable:
            identity = client.token_identity(self.stable["address"])
            if (
                identity["symbol"] != self.stable["symbol"]
                or identity["decimals"] != self.stable["decimals"]
            ):
                raise RuntimeError("Registry stablecoin does not match the chain")
            reference = {"kind": "stable-pool", **self.stable}
        else:
            raise RuntimeError(
                "Registry needs 'eth_usd_feed' or 'stable': dollar limits cannot "
                "be sized without an ETH/USD reference"
            )
        return {
            "network": self.network_key,
            "router": self.router,
            "quoter": self.quoter,
            "factory": factory,
            "quote": quote,
            "usd_reference": reference,
            "pools": pools,
        }

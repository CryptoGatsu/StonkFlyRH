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

from .chain import FACTORY_ABI, QUOTER_ABI, ROUTER_ABI, ZERO_ADDRESS, checksum
from .config import SYMBOL

REGISTRY_ENV = "STONKFLYRH_TOKENS"
DEFAULT_REGISTRY = "tokens.json"


def registry_path(path=None):
    from .paths import resolve

    return resolve(path or os.environ.get(REGISTRY_ENV) or DEFAULT_REGISTRY)


class Registry:
    def __init__(self, network_key, data):
        self.network_key = network_key
        section = data.get(network_key)
        if not isinstance(section, dict):
            raise RuntimeError(f"Registry has no '{network_key}' section")
        quote = section.get("quote") or {}
        if not SYMBOL.match(quote.get("symbol", "")) or not 0 <= int(quote.get("decimals", -1)) <= 36:
            raise RuntimeError("Registry quote asset needs a symbol, address and decimals")
        self.quote_symbol = quote["symbol"]
        self.quote_address = checksum(quote["address"])
        self.quote_decimals = int(quote["decimals"])
        self.router = checksum(section["router"])
        self.quoter = checksum(section["quoter"])
        # Gas is valued through one of these: wrapped ETH priced into the quote
        # pool by the run's own quoter, or a Chainlink ETH/USD aggregator.
        self.eth_usd_feed = (
            checksum(section["eth_usd_feed"]) if section.get("eth_usd_feed") else None
        )
        self.weth = checksum(section["weth"]) if section.get("weth") else None
        self.weth_pool_fee = int(section.get("weth_pool_fee", 500))
        # Uniswap v4: one PoolManager for every pool, a quoter, a state reader,
        # the Universal Router that executes, and Permit2 that it pulls through.
        self.v4 = None
        if section.get("v4"):
            v4 = section["v4"]
            self.v4 = {
                k: checksum(v4[k])
                for k in ("pool_manager", "quoter", "state_view", "universal_router", "permit2")
            }
            self.v4["hooks_allow"] = [checksum(h) for h in v4.get("hooks_allow", [])]
            self.v4["hooks_allow_any"] = bool(v4.get("hooks_allow_any", False))
            # Known assets with a USDG pool to route through (GOOGL, WETH, ...).
            self.v4["bridges"] = list(v4.get("bridges", []))
        if self.weth and self.weth == self.quote_address:
            raise RuntimeError("weth cannot be the quote asset")
        self.tokens = {}
        for symbol, info in (section.get("tokens") or {}).items():
            if not SYMBOL.match(symbol) or symbol == self.quote_symbol:
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
            if entry["address"] in (self.quote_address, self.weth):
                raise RuntimeError("A memecoin entry cannot be the quote asset or WETH")
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

    def add_token(self, entry):
        """Admit a discovered token for the rest of this process."""
        symbol = entry["symbol"]
        if not SYMBOL.match(symbol) or symbol == self.quote_symbol:
            raise RuntimeError("Invalid symbol for a discovered token: " + str(symbol))
        address = checksum(entry["address"])
        if address in (self.quote_address, self.weth):
            raise RuntimeError("A discovered token cannot be the quote asset or WETH")
        self.tokens[symbol] = {
            "symbol": symbol,
            "name": entry.get("name", symbol),
            "address": address,
            "decimals": int(entry["decimals"]),
            "pool_fee": int(entry["pool_fee"]),
            "venue": entry.get("venue", "v3"),
            "route": entry.get("route"),
            "pool": entry.get("pool"),
            "hooks": entry.get("hooks"),
            "via": entry.get("via"),
            "discovered_block": entry.get("discovered_block"),
        }

    def remove_token(self, symbol):
        self.tokens.pop(symbol, None)

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
        if quote["symbol"] != self.quote_symbol or quote["decimals"] != self.quote_decimals:
            raise RuntimeError(
                f"On-chain quote token is {quote['symbol']}/{quote['decimals']}, "
                f"not {self.quote_symbol}/{self.quote_decimals}"
            )
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
                raise RuntimeError(f"No {symbol}/{self.quote_symbol} pool at fee tier {fee}")
            pools[symbol] = {"pool": pool, "fee": fee}
        v4 = None
        if self.v4:
            for name in ("pool_manager", "quoter", "state_view", "universal_router", "permit2"):
                client.require_code(self.v4[name], f"v4 {name}")
            v4 = dict(self.v4)
        reference = None
        if self.eth_usd_feed:
            client.require_code(self.eth_usd_feed, "ETH/USD feed")
            reference = {"kind": "chainlink", "address": self.eth_usd_feed}
        elif self.weth:
            identity = client.token_identity(self.weth)
            if identity["decimals"] != 18:
                raise RuntimeError("Registry weth is not an 18-decimal token")
            pool = checksum(
                factory_contract.functions.getPool(
                    self.weth, self.quote_address, self.weth_pool_fee
                ).call()
            )
            if pool == ZERO_ADDRESS or not client.has_code(pool):
                raise RuntimeError(
                    f"No WETH/{self.quote_symbol} pool at fee tier {self.weth_pool_fee}"
                )
            reference = {
                "kind": "weth-quote-pool",
                "weth": self.weth,
                "symbol": identity["symbol"],
                "pool": pool,
                "fee": self.weth_pool_fee,
            }
        else:
            raise RuntimeError(
                "Registry needs 'weth' or 'eth_usd_feed': gas cannot be valued "
                "without an ETH/USD reference"
            )
        return {
            "network": self.network_key,
            "router": self.router,
            "quoter": self.quoter,
            "factory": factory,
            "quote": quote,
            "usd_reference": reference,
            "v4": v4,
            "pools": pools,
        }

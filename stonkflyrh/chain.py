"""Robinhood Chain network definitions and the EVM primitives the fork needs.

Robinhood Chain is an Arbitrum Orbit L2: standard EVM bytecode, standard
JSON-RPC, ETH as the gas token. Nothing here is Robinhood-specific beyond the
chain id, the RPC endpoint and the explorer.

Contract addresses are operator configuration, never constants baked into this
file. Any address this process is willing to call is checked on chain first:
it must carry code, and a router must agree with its own factory. A typo or a
stale address fails loudly at preflight instead of quietly sending value to a
contract nobody verified.
"""

import os

from .config import D
from dataclasses import dataclass

# eth_call selectors, kept explicit so the ABI surface this process can reach
# stays auditable by reading one file.
ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]

WETH_ABI = ERC20_ABI + [
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [],
        "outputs": [],
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "wad", "type": "uint256"}],
        "outputs": [],
    },
]

QUOTER_ABI = [
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    },
    {
        "name": "factory",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]

ROUTER_ABI = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    {
        "name": "factory",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]

CHAINLINK_ABI = [
    {
        "name": "latestRoundData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "description",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]

OWNABLE_ABI = [
    {
        "name": "owner",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "totalSupply",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

FACTORY_ABI = [
    {
        "name": "getPool",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "fee", "type": "uint24"},
        ],
        "outputs": [{"name": "", "type": "address"}],
    }
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# EIP-1967 implementation slot. A non-zero value means the contract behind an
# address can be swapped out from under a holder.
PROXY_SLOT = 0x360894A13BA1A3210667C828492DB98DCA3E2076CC3735A920A3CA505D382BBC


@dataclass(frozen=True)
class Network:
    key: str
    name: str
    chain_id: int
    rpc: str
    explorer: str
    gas_symbol: str = "ETH"
    live_capable: bool = True
    # This fork trades Robinhood Chain and nothing else; the flag exists so a
    # configuration that is not Robinhood Chain is rejected rather than assumed.
    robinhood: bool = True

    def tx_url(self, tx_hash):
        return f"{self.explorer.rstrip('/')}/tx/{tx_hash}"

    def address_url(self, address):
        return f"{self.explorer.rstrip('/')}/address/{address}"


NETWORKS = {
    n.key: n
    for n in [
        Network(
            "robinhood-mainnet",
            "Robinhood Chain",
            4663,
            "https://rpc.mainnet.chain.robinhood.com",
            "https://explorer.mainnet.chain.robinhood.com",
        ),
        Network(
            "robinhood-testnet",
            "Robinhood Chain Testnet",
            46630,
            "https://rpc.testnet.chain.robinhood.com",
            "https://explorer.testnet.chain.robinhood.com",
        ),
    ]
}
DEFAULT_NETWORK = "robinhood-mainnet"


def network(key=None):
    key = key or os.environ.get("STONKFLYRH_NETWORK") or DEFAULT_NETWORK
    if key not in NETWORKS:
        raise ValueError("Unknown network: " + str(key))
    return NETWORKS[key]


def rpc_url(net):
    """Endpoint override is allowed; the chain id is still verified on connect."""
    return os.environ.get("STONKFLYRH_RPC_URL") or net.rpc


def hex32(value):
    """0x-prefixed hex for a HexBytes, whatever the library version returns."""
    if hasattr(value, "to_0x_hex"):
        return value.to_0x_hex()
    text = value.hex() if hasattr(value, "hex") else str(value)
    return text if text.startswith("0x") else "0x" + text


def checksum(address):
    from eth_utils import to_checksum_address

    if not isinstance(address, str) or not address.startswith("0x") or len(address) != 42:
        raise ValueError("Expected a 20-byte hex address")
    return to_checksum_address(address)


class ChainClient:
    """Read/write access to Robinhood Chain, with the chain id pinned.

    Every constructed instance has confirmed that the endpoint it is talking to
    reports the chain id this run was configured for. A node pointed at the
    wrong network cannot be used to price or to trade.
    """

    def __init__(self, net, w3=None, rpc=None):
        self.net = net
        if w3 is None:
            from web3 import HTTPProvider, Web3

            w3 = Web3(HTTPProvider(rpc or rpc_url(net), request_kwargs={"timeout": 15}))
        self.w3 = w3
        reported = int(w3.eth.chain_id)
        if reported != net.chain_id:
            raise RuntimeError(
                f"RPC reports chain id {reported}; expected {net.chain_id} for {net.name}"
            )

    def contract(self, address, abi):
        return self.w3.eth.contract(address=checksum(address), abi=abi)

    def has_code(self, address):
        return len(self.w3.eth.get_code(checksum(address))) > 0

    def require_code(self, address, label):
        if not self.has_code(address):
            raise RuntimeError(f"{label} address {address} holds no code on {self.net.name}")
        return checksum(address)

    def erc20(self, address):
        return self.contract(address, ERC20_ABI)

    def token_identity(self, address):
        """Read symbol/decimals from the token itself rather than trusting config."""
        c = self.erc20(self.require_code(address, "Token"))
        return {
            "address": checksum(address),
            "symbol": c.functions.symbol().call(),
            "decimals": int(c.functions.decimals().call()),
        }

    # A legacy-priced transaction is refused if the block base fee has risen
    # past its gas price between quoting and inclusion, and on this chain the
    # base fee moves every block. Price a quarter above the higher of the
    # node's suggestion and the latest base fee; an Arbitrum-style chain only
    # ever charges the base fee, so the headroom costs nothing when unused.
    GAS_PRICE_HEADROOM = D("1.25")

    def gas_price(self):
        suggested = int(self.w3.eth.gas_price)
        try:
            base = int(self.w3.eth.get_block("latest").get("baseFeePerGas") or 0)
        except Exception:
            base = 0
        return int(D(max(suggested, base)) * self.GAS_PRICE_HEADROOM)

    def balance(self, address):
        return int(self.w3.eth.get_balance(checksum(address)))

    def code(self, address):
        return bytes(self.w3.eth.get_code(checksum(address)))

    def storage_at(self, address, slot):
        return int.from_bytes(self.w3.eth.get_storage_at(checksum(address), slot), "big")

    def is_upgradeable(self, address):
        """EIP-1967 proxy check: a live implementation slot means upgradeable."""
        try:
            return self.storage_at(address, PROXY_SLOT) != 0
        except Exception:
            return False

    def selectors(self, address):
        """Four-byte selectors appearing as PUSH4 immediates in runtime code.

        This is how a dispatcher compares calldata, so it finds functions a
        verified ABI would list and functions a token author would rather not
        advertise. It over-reports: a PUSH4 can be any constant. Treat a hit as
        a reason to look, which is exactly how the screen uses it.
        """
        code = self.code(address)
        found = set()
        i = 0
        while i < len(code):
            op = code[i]
            if op == 0x63 and i + 4 < len(code):  # PUSH4
                found.add(code[i + 1 : i + 5].hex())
                i += 5
                continue
            if 0x60 <= op <= 0x7F:  # any other PUSHn
                i += op - 0x5F + 1
                continue
            i += 1
        return found

    def try_call(self, address, abi, function, *args):
        """Read an optional interface. A contract without it is not an error."""
        try:
            return getattr(self.contract(address, abi).functions, function)(*args).call()
        except Exception:
            return None

    def logs(self, params, chunk=2000, pause=0.6, retries=8, max_blocks=None):
        """eth_getLogs over a block range the public RPC will actually serve.

        Public endpoints rate-limit and cap the range per call. This walks the
        range in chunks, pauses between them, backs off on an error, and halves
        the chunk when the node says the range is too large. `max_blocks` caps
        how far one call gets; the caller records where it stopped and continues
        next time, so a long backlog is spread over several scans.
        """
        import time as _time

        lo = int(params["fromBlock"])
        hi = int(params["toBlock"])
        if max_blocks is not None:
            hi = min(hi, lo + int(max_blocks) - 1)
        out = []
        start = lo
        while start <= hi:
            end = min(hi, start + chunk - 1)
            attempt = 0
            while True:
                try:
                    out += self.w3.eth.get_logs({**params, "fromBlock": start, "toBlock": end})
                    break
                except Exception as e:
                    text = str(e).lower()
                    # "range"/"too many"/"limit": the node caps the block span.
                    # "413"/"too large": a hosted endpoint caps the reply size.
                    # Either way a smaller window is the answer.
                    too_big = any(k in text for k in ("range", "too many", "limit", "413", "too large"))
                    if too_big and chunk > 200:
                        chunk //= 2
                        end = min(hi, start + chunk - 1)
                        continue
                    attempt += 1
                    if attempt > retries:
                        raise
                    _time.sleep(min(30, pause * (2**attempt)))
            start = end + 1
            if start <= hi and pause:
                _time.sleep(pause)
        return out, hi

    def nonce(self, address):
        # "pending" would let a stuck transaction silently shift the nonce of an
        # order this process has already persisted intent for.
        return int(self.w3.eth.get_transaction_count(checksum(address), "latest"))

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


@dataclass(frozen=True)
class Network:
    key: str
    name: str
    chain_id: int
    rpc: str
    explorer: str
    gas_symbol: str = "ETH"
    live_capable: bool = True

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

    def gas_price(self):
        return int(self.w3.eth.gas_price)

    def balance(self, address):
        return int(self.w3.eth.get_balance(checksum(address)))

    def nonce(self, address):
        # "pending" would let a stuck transaction silently shift the nonce of an
        # order this process has already persisted intent for.
        return int(self.w3.eth.get_transaction_count(checksum(address), "latest"))

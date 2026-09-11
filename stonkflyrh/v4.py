"""Uniswap v4 on Robinhood Chain: where launchpad tokens actually live.

Pons graduates every token into a v4 pool behind its hook. v4 is a different
machine from v3: one singleton PoolManager holds every pool's tokens, a pool is
identified by a PoolKey (the two currencies, fee, tick spacing, hooks) rather
than its own contract, quotes come from a v4 Quoter, and swaps go through the
Universal Router with Permit2 approvals and an action-encoded payload.

This module is the whole v4 surface: the ABIs, PoolKey and PoolId arithmetic,
decoding the PoolManager's Initialize event, quoting, reading pool state, and
building the exact bytes the Universal Router expects for one exact-input swap.
Everything the rest of the run needs from v4 goes through `V4Venue`.
"""

from .chain import checksum

NATIVE = "0x0000000000000000000000000000000000000000"

# Universal Router command and v4 action bytes.
V4_SWAP = 0x10
SWAP_EXACT_IN_SINGLE = 0x06
SWAP_EXACT_IN = 0x07
SETTLE_ALL = 0x0C
TAKE_ALL = 0x0F

POOL_KEY = "(address,address,uint24,int24,address)"
# PathKey: the currency you arrive at after this hop, and the pool that takes you there.
PATH_KEY = "(address,uint24,int24,address,bytes)"

INITIALIZE = "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"

POOL_MANAGER_ABI = [
    {
        "anonymous": False,
        "name": "Initialize",
        "type": "event",
        "inputs": [
            {"indexed": True, "name": "id", "type": "bytes32"},
            {"indexed": True, "name": "currency0", "type": "address"},
            {"indexed": True, "name": "currency1", "type": "address"},
            {"indexed": False, "name": "fee", "type": "uint24"},
            {"indexed": False, "name": "tickSpacing", "type": "int24"},
            {"indexed": False, "name": "hooks", "type": "address"},
            {"indexed": False, "name": "sqrtPriceX96", "type": "uint160"},
            {"indexed": False, "name": "tick", "type": "int24"},
        ],
    }
]

POOL_KEY_COMPONENTS = [
    {"name": "currency0", "type": "address"},
    {"name": "currency1", "type": "address"},
    {"name": "fee", "type": "uint24"},
    {"name": "tickSpacing", "type": "int24"},
    {"name": "hooks", "type": "address"},
]

PATH_KEY_COMPONENTS = [
    {"name": "intermediateCurrency", "type": "address"},
    {"name": "fee", "type": "uint24"},
    {"name": "tickSpacing", "type": "int24"},
    {"name": "hooks", "type": "address"},
    {"name": "hookData", "type": "bytes"},
]

V4_QUOTER_ABI = [
    {
        "name": "quoteExactInput",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "exactCurrency", "type": "address"},
                    {"name": "path", "type": "tuple[]", "components": PATH_KEY_COMPONENTS},
                    {"name": "exactAmount", "type": "uint128"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    },
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "poolKey", "type": "tuple", "components": POOL_KEY_COMPONENTS},
                    {"name": "zeroForOne", "type": "bool"},
                    {"name": "exactAmount", "type": "uint128"},
                    {"name": "hookData", "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    }
]

STATE_VIEW_ABI = [
    {
        "name": "getSlot0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "protocolFee", "type": "uint24"},
            {"name": "lpFee", "type": "uint24"},
        ],
    },
    {
        "name": "getLiquidity",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "outputs": [{"name": "liquidity", "type": "uint128"}],
    },
]

UNIVERSAL_ROUTER_ABI = [
    {
        "name": "execute",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "commands", "type": "bytes"},
            {"name": "inputs", "type": "bytes[]"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [],
    }
]

PERMIT2_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint160"},
            {"name": "expiration", "type": "uint48"},
        ],
        "outputs": [],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [
            {"name": "amount", "type": "uint160"},
            {"name": "expiration", "type": "uint48"},
            {"name": "nonce", "type": "uint48"},
        ],
    },
]

MAX_UINT160 = 2**160 - 1
MAX_UINT48 = 2**48 - 1


def initialize_topic():
    from eth_utils import keccak

    return "0x" + keccak(text=INITIALIZE).hex()


def _bytes(value):
    return bytes(value) if not isinstance(value, str) else bytes.fromhex(value[2:])


def _word_address(word):
    return checksum("0x" + _bytes(word)[-20:].hex())


def _word_int(word, signed=False):
    return int.from_bytes(_bytes(word), "big", signed=signed)


def pool_key(currency0, currency1, fee, tick_spacing, hooks):
    a, b = checksum(currency0), checksum(currency1)
    if int(a, 16) > int(b, 16):
        raise ValueError("PoolKey currencies must be sorted")
    return {
        "currency0": a,
        "currency1": b,
        "fee": int(fee),
        "tickSpacing": int(tick_spacing),
        "hooks": checksum(hooks),
    }


def key_tuple(key):
    return (key["currency0"], key["currency1"], key["fee"], key["tickSpacing"], key["hooks"])


def pool_id(key):
    """keccak256(abi.encode(PoolKey)), as PoolManager computes it."""
    from eth_abi import encode
    from eth_utils import keccak

    return "0x" + keccak(encode([POOL_KEY], [key_tuple(key)])).hex()


def decode_initialize(log):
    topics = log["topics"]
    data = _bytes(log["data"])
    if len(topics) != 4 or len(data) < 32 * 5:
        raise ValueError("Not an Initialize log")
    words = [data[i * 32 : (i + 1) * 32] for i in range(5)]
    return {
        "id": "0x" + _bytes(topics[1]).hex(),
        "currency0": _word_address(topics[2]),
        "currency1": _word_address(topics[3]),
        "fee": _word_int(words[0]),
        "tickSpacing": _signed24(words[1]),
        "hooks": _word_address(words[2]),
        "sqrtPriceX96": _word_int(words[3]),
        "tick": _signed24(words[4]),
        "block": int(log["blockNumber"]),
    }


def _signed24(word):
    value = int.from_bytes(_bytes(word), "big")
    # abi-encoded int24 is sign-extended to 256 bits.
    return value - 2**256 if value >= 2**255 else value


# Universal Router 2.1.1 (the version deployed on Robinhood Chain) added a
# per-hop minimum price, minHopPriceX36, to every swap struct. The 2.0 layout
# decodes against the wrong shape and reverts with no data. Zero disables it;
# the slippage bound is amountOutMinimum.
EXACT_IN_SINGLE_PARAMS = "(" + POOL_KEY + ",bool,uint128,uint128,uint256,bytes)"
EXACT_IN_PARAMS = "(address," + PATH_KEY + "[],uint256[],uint128,uint128)"
NO_HOP_PRICE_LIMIT = 0


def encode_exact_in_single(key, zero_for_one, amount_in, min_out, hook_data=b""):
    """The Universal Router `execute` arguments for one v4 exact-input swap.

    Three actions: swap, then settle everything owed in the input currency,
    then take everything owed in the output currency. Settlement is what moves
    the input from the wallet (via Permit2) and the output back to it.
    """
    from eth_abi import encode

    currency_in = key["currency0"] if zero_for_one else key["currency1"]
    currency_out = key["currency1"] if zero_for_one else key["currency0"]
    actions = bytes([SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL])
    params = [
        encode(
            [EXACT_IN_SINGLE_PARAMS],
            [(key_tuple(key), bool(zero_for_one), int(amount_in), int(min_out), NO_HOP_PRICE_LIMIT, hook_data)],
        ),
        encode(["address", "uint256"], [currency_in, int(amount_in)]),
        encode(["address", "uint256"], [currency_out, int(min_out)]),
    ]
    inputs = [encode(["bytes", "bytes[]"], [actions, params])]
    return bytes([V4_SWAP]), inputs


def path_for(keys, currency_in):
    """PathKeys from `currency_in` through each PoolKey in order.

    Each hop's PathKey names the currency it arrives at; the next hop starts
    there. A route whose pools do not chain raises rather than mis-encoding.
    """
    current = checksum(currency_in)
    path = []
    for key in keys:
        if current == key["currency0"]:
            nxt = key["currency1"]
        elif current == key["currency1"]:
            nxt = key["currency0"]
        else:
            raise ValueError(f"Pool {pool_id(key)[:10]} does not contain {current}")
        path.append((nxt, key["fee"], key["tickSpacing"], key["hooks"], b""))
        current = nxt
    return path, current


def encode_exact_in_path(keys, currency_in, amount_in, min_out):
    """Universal Router arguments for a multi-hop v4 exact-input swap."""
    from eth_abi import encode

    path, currency_out = path_for(keys, currency_in)
    actions = bytes([SWAP_EXACT_IN, SETTLE_ALL, TAKE_ALL])
    params = [
        encode(
            [EXACT_IN_PARAMS],
            [(checksum(currency_in), path, [NO_HOP_PRICE_LIMIT] * len(path), int(amount_in), int(min_out))],
        ),
        encode(["address", "uint256"], [checksum(currency_in), int(amount_in)]),
        encode(["address", "uint256"], [currency_out, int(min_out)]),
    ]
    return bytes([V4_SWAP]), [encode(["bytes", "bytes[]"], [actions, params])], currency_out


# Custom errors a v4 swap through the Universal Router can surface, by name, so
# a revert reads as a reason instead of four hex bytes.
REVERT_SIGNATURES = (
    "V4TooLittleReceived(uint256,uint256)",
    "V4TooMuchRequested(uint256,uint256)",
    "DeadlinePassed(uint256)",
    "TransactionDeadlinePassed()",
    "ExecutionFailed(uint256,bytes)",
    "InvalidCommandType(uint256)",
    "InsufficientToken()",
    "InsufficientETH()",
    "ContractLocked()",
    "NotPoolManager()",
    "InputLengthMismatch()",
    "UnsupportedAction(uint256)",
    "DeltaNotPositive(address)",
    "DeltaNotNegative(address)",
    "AllowanceExpired(uint256)",
    "InsufficientAllowance(uint256)",
    "InvalidNonce()",
    "CurrencyNotSettled()",
    "PoolNotInitialized()",
    "HookAddressNotValid(address)",
    "InvalidHookResponse()",
    "HookCallFailed()",
    "PriceLimitAlreadyExceeded(uint160,uint160)",
    "PriceLimitOutOfBounds(uint160)",
    "SwapAmountCannotBeZero()",
    "ManagerLocked()",
    "Error(string)",
    "Panic(uint256)",
)


def revert_names():
    from eth_utils import keccak

    return {"0x" + keccak(text=sig).hex()[:8]: sig for sig in REVERT_SIGNATURES}


def describe_revert(exc):
    """A readable reason for a ContractLogicError: the named custom error when
    the selector is known, the revert string when there is one, else the raw
    data. Nothing here is secret; it is what the chain answered."""
    data = getattr(exc, "data", None)
    text = getattr(exc, "message", None) or (str(exc.args[0]) if getattr(exc, "args", None) else str(exc))
    if isinstance(data, dict):
        data = data.get("data") or data.get("message")
    if isinstance(data, (bytes, bytearray)):
        data = "0x" + bytes(data).hex()
    if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
        selector = data[:10].lower()
        name = revert_names().get(selector)
        if name == "Error(string)":
            try:
                from eth_abi import decode

                (message,) = decode(["string"], bytes.fromhex(data[10:]))
                return f"reverted: {message}"
            except Exception:
                pass
        if name:
            return f"reverted with {name.split('(')[0]} ({data[:74]})"
        return f"reverted with unknown error {selector} ({data[:74]})"
    return f"reverted: {text[:160]}" if text else "reverted without a reason"


class V4Venue:
    """Everything the run does against a v4 pool, given its PoolKey."""

    def __init__(self, client, registry):
        self.client = client
        self.registry = registry
        v4 = registry.v4
        self.pool_manager = checksum(v4["pool_manager"])
        self.quoter = client.contract(v4["quoter"], V4_QUOTER_ABI)
        self.state = client.contract(v4["state_view"], STATE_VIEW_ABI)
        self.router = client.contract(v4["universal_router"], UNIVERSAL_ROUTER_ABI)
        self.router_address = checksum(v4["universal_router"])
        self.permit2 = client.contract(v4["permit2"], PERMIT2_ABI)
        self.permit2_address = checksum(v4["permit2"])

    # -- quoting --------------------------------------------------------------

    def quote(self, key, token_in, amount_in, hook_data=b""):
        zero_for_one = checksum(token_in) == key["currency0"]
        params = (key_tuple(key), zero_for_one, int(amount_in), hook_data)
        result = self.quoter.functions.quoteExactInputSingle(params).call()
        amount_out = int(result[0] if isinstance(result, (list, tuple)) else result)
        if amount_out <= 0:
            raise RuntimeError("v4 quoter returned no output")
        return amount_out

    def quote_path(self, keys, currency_in, amount_in):
        """Quote a route of one or more pools starting from `currency_in`."""
        if len(keys) == 1:
            return self.quote(keys[0], currency_in, amount_in)
        path, _ = path_for(keys, currency_in)
        params = (checksum(currency_in), path, int(amount_in))
        result = self.quoter.functions.quoteExactInput(params).call()
        amount_out = int(result[0] if isinstance(result, (list, tuple)) else result)
        if amount_out <= 0:
            raise RuntimeError("v4 quoter returned no output for the route")
        return amount_out

    def quote_call_for(self, route):
        """A (token_in, token_out, amount_in, fee) callable shaped like v3's, so
        the market and the screen can price a v4 route without knowing.

        `route` is the list of PoolKeys from USDG to the token. Quoting the
        other direction walks it in reverse.
        """
        forward = list(route)
        backward = list(reversed(route))

        def call(token_in, _token_out, amount_in, _fee):
            keys = forward if checksum(token_in) == self.registry.quote_address else backward
            return self.quote_path(keys, token_in, amount_in)

        return call

    # -- state ----------------------------------------------------------------

    def slot0(self, key):
        r = self.state.functions.getSlot0(bytes.fromhex(pool_id(key)[2:])).call()
        return {"sqrtPriceX96": int(r[0]), "tick": int(r[1]), "protocolFee": int(r[2]), "lpFee": int(r[3])}

    def liquidity(self, key):
        return int(self.state.functions.getLiquidity(bytes.fromhex(pool_id(key)[2:])).call())

    def exists(self, key):
        try:
            return self.slot0(key)["sqrtPriceX96"] > 0
        except Exception:
            return False

    # -- approvals ------------------------------------------------------------

    def permit2_allowance(self, owner, token):
        amount, expiration, _nonce = self.permit2.functions.allowance(
            checksum(owner), checksum(token), self.router_address
        ).call()
        return int(amount), int(expiration)

    def build_permit2_approve(self, token, amount, expiration, tx_fields):
        return self.permit2.functions.approve(
            checksum(token), self.router_address, int(amount), int(expiration)
        ).build_transaction(tx_fields)

    # -- swapping -------------------------------------------------------------

    def swap_call(self, route, token_in, amount_in, min_out, deadline):
        """The bound router call for a swap along `route` from `token_in`.

        Single-pool routes use the single-hop action; longer ones the path
        action. Either way it is one transaction and one settlement.
        """
        keys = list(route) if checksum(token_in) == self.registry.quote_address else list(reversed(route))
        if len(keys) == 1:
            zero_for_one = checksum(token_in) == keys[0]["currency0"]
            commands, inputs = encode_exact_in_single(keys[0], zero_for_one, amount_in, min_out)
        else:
            commands, inputs, _ = encode_exact_in_path(keys, token_in, amount_in, min_out)
        return self.router.functions.execute(commands, inputs, int(deadline))

"""Uniswap v4 plumbing: keys, ids, event decoding, router payloads. No RPC."""

import pytest
from eth_abi import decode

from stonkflyrh import v4
from stonkflyrh.chain import checksum

A = checksum("0x" + "11" * 20)
B = checksum("0x" + "22" * 20)
C = checksum("0x" + "33" * 20)
HOOK = checksum("0xe5e702641ea86f4ae6cc3cdaed2b886f976be044")
NOHOOK = v4.NATIVE


def key(c0=A, c1=B, fee=30000, spacing=60, hooks=HOOK):
    return v4.pool_key(c0, c1, fee, spacing, hooks)


# -- keys and ids ------------------------------------------------------------


def test_pool_key_requires_sorted_currencies():
    with pytest.raises(ValueError, match="sorted"):
        v4.pool_key(B, A, 3000, 60, NOHOOK)


def test_pool_id_is_deterministic_and_distinct_per_key():
    assert v4.pool_id(key()) == v4.pool_id(key())
    assert v4.pool_id(key()) != v4.pool_id(key(fee=10000))
    assert v4.pool_id(key()) != v4.pool_id(key(hooks=NOHOOK))
    assert len(v4.pool_id(key())) == 66


def test_the_initialize_topic_is_uniswap_v4s():
    assert v4.initialize_topic() == "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"


# -- decoding ----------------------------------------------------------------


def initialize_log(c0=A, c1=B, fee=30000, spacing=60, hooks=HOOK, tick=-12345, block=777):
    pad = lambda a: bytes(12) + bytes.fromhex(a[2:])
    signed = lambda v: (v % 2**256).to_bytes(32, "big")
    data = fee.to_bytes(32, "big") + signed(spacing) + pad(hooks) + (79228162514264337593543950336).to_bytes(32, "big") + signed(tick)
    return {"topics": [v4.initialize_topic(), bytes.fromhex(v4.pool_id(key(c0, c1, fee, spacing, hooks))[2:]), pad(c0), pad(c1)],
            "data": data, "blockNumber": block}


def test_an_initialize_log_decodes_to_its_pool_key():
    d = v4.decode_initialize(initialize_log())
    assert (d["currency0"], d["currency1"], d["fee"], d["tickSpacing"], d["hooks"]) == (A, B, 30000, 60, HOOK)
    assert d["tick"] == -12345 and d["block"] == 777
    assert d["id"] == v4.pool_id(key())


def test_a_native_eth_pool_decodes_with_the_zero_currency():
    d = v4.decode_initialize(initialize_log(c0=v4.NATIVE, c1=B))
    assert d["currency0"] == v4.NATIVE


def test_a_malformed_log_is_refused():
    bad = initialize_log(); bad["topics"] = bad["topics"][:3]
    with pytest.raises(ValueError):
        v4.decode_initialize(bad)


# -- router payloads ---------------------------------------------------------


def test_single_hop_payload_has_the_three_actions_in_order():
    commands, inputs = v4.encode_exact_in_single(key(), True, 10_000_000, 12345)
    assert commands == bytes([v4.V4_SWAP])
    actions, params = decode(["bytes", "bytes[]"], inputs[0])
    assert list(actions) == [v4.SWAP_EXACT_IN_SINGLE, v4.SETTLE_ALL, v4.TAKE_ALL]
    swap = decode(["(" + v4.POOL_KEY + ",bool,uint128,uint128,bytes)"], params[0])[0]
    # eth_abi decodes addresses lowercase.
    assert tuple(x.lower() if isinstance(x, str) else x for x in swap[0]) == (A.lower(), B.lower(), 30000, 60, HOOK.lower())
    assert swap[1] is True and swap[2] == 10_000_000 and swap[3] == 12345
    settle = decode(["address", "uint256"], params[1])
    take = decode(["address", "uint256"], params[2])
    assert (settle[0].lower(), settle[1]) == (A.lower(), 10_000_000)   # input currency, max
    assert (take[0].lower(), take[1]) == (B.lower(), 12345)            # output currency, min


def test_one_for_zero_settles_the_other_currency():
    _, inputs = v4.encode_exact_in_single(key(), False, 5, 1)
    _, params = decode(["bytes", "bytes[]"], inputs[0])
    assert decode(["address", "uint256"], params[1])[0].lower() == B.lower()
    assert decode(["address", "uint256"], params[2])[0].lower() == A.lower()


def test_a_two_hop_path_chains_through_the_intermediate_currency():
    usdg_googl = key(A, B, 500, 10, NOHOOK)       # A = USDG, B = GOOGL
    googl_coin = key(B, C, 30000, 60, HOOK)        # C = the memecoin
    path, out = v4.path_for([usdg_googl, googl_coin], A)
    assert out == C
    assert [p[0] for p in path] == [B, C]
    assert path[1][3] == HOOK


def test_a_path_that_does_not_chain_is_refused():
    with pytest.raises(ValueError, match="does not contain"):
        v4.path_for([key(A, B), key(A, C)], A)


def test_multi_hop_payload_settles_input_and_takes_final_output():
    route = [key(A, B, 500, 10, NOHOOK), key(B, C, 30000, 60, HOOK)]
    commands, inputs, out = v4.encode_exact_in_path(route, A, 10_000_000, 999)
    assert commands == bytes([v4.V4_SWAP]) and out == C
    actions, params = decode(["bytes", "bytes[]"], inputs[0])
    assert list(actions) == [v4.SWAP_EXACT_IN, v4.SETTLE_ALL, v4.TAKE_ALL]
    swap = decode(["(address," + v4.PATH_KEY + "[],uint128,uint128)"], params[0])[0]
    assert swap[0].lower() == A.lower() and len(swap[1]) == 2 and swap[2] == 10_000_000 and swap[3] == 999
    assert decode(["address", "uint256"], params[2])[0].lower() == C.lower()


def test_selling_reverses_the_route():
    class Reg:
        quote_address = A
        v4 = {"pool_manager": A, "quoter": A, "state_view": A, "universal_router": A, "permit2": A}

    class Client:
        def contract(self, *_):
            return type("C", (), {"functions": None})()

    venue = v4.V4Venue(Client(), Reg())
    route = [key(A, B, 500, 10, NOHOOK), key(B, C, 30000, 60, HOOK)]
    seen = {}
    venue.quote_path = lambda keys, cin, amt: seen.setdefault("keys", keys) or 1
    venue.quote_call_for(route)(C, A, 5, 0)
    assert seen["keys"] == list(reversed(route))


def test_a_revert_is_described_by_name_or_message():
    from web3.exceptions import ContractLogicError

    from stonkflyrh.v4 import describe_revert, revert_names

    names = revert_names()
    too_little = next(sel for sel, sig in names.items() if sig.startswith("V4TooLittleReceived"))
    e = ContractLogicError("execution reverted", data=too_little + "00" * 64)
    assert describe_revert(e).startswith("reverted with V4TooLittleReceived")
    from eth_abi import encode

    err = next(sel for sel, sig in names.items() if sig == "Error(string)")
    e = ContractLogicError("execution reverted", data=err + encode(["string"], ["Pons: paused"]).hex())
    assert describe_revert(e) == "reverted: Pons: paused"
    e = ContractLogicError("execution reverted", data="0xdeadbeef")
    assert "unknown error 0xdeadbeef" in describe_revert(e)
    assert describe_revert(ContractLogicError("execution reverted: no data")).startswith("reverted: execution reverted")

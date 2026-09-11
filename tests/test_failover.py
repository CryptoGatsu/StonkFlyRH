"""A throttled dedicated endpoint hands the request to the public RPC."""

import json

import pytest
import requests

from stonkflyrh.chain import FailoverProvider, _throttled


class Answer:
    def __init__(self, fails=0, status=429):
        self.fails, self.status, self.calls = fails, status, 0

    def make_request(self, method, params):
        self.calls += 1
        if self.calls <= self.fails:
            r = requests.Response()
            r.status_code = self.status
            r._content = json.dumps({"error": {"message": "monthly quota exceeded"}}).encode()
            raise requests.HTTPError(f"{self.status} Client Error", response=r)
        return {"jsonrpc": "2.0", "id": 1, "result": f"{method}:{self.calls}"}


def build(primary, fallback):
    p = FailoverProvider("https://primary.invalid/", "https://public.invalid/")
    p.primary, p.fallback = primary, fallback
    p._sleep = lambda s: None
    clock = {"t": 1000.0}
    p._now = lambda: clock["t"]
    return p, clock


def test_a_429_is_retried_then_answered_by_the_fallback_and_the_primary_rests(capsys):
    primary, fallback = Answer(fails=99), Answer()
    p, clock = build(primary, fallback)
    assert p.make_request("eth_chainId", [])["result"] == "eth_chainId:1"
    assert primary.calls == 3 and fallback.calls == 1  # two retries, then over
    note = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert note["rpc"] == "fallback" and note["primary_answered"]["status"] == 429
    assert "quota" in note["primary_answered"]["body"] and "invalid" not in json.dumps(note)
    # While resting, requests skip the primary entirely.
    p.make_request("eth_blockNumber", [])
    assert primary.calls == 3 and fallback.calls == 2
    # After the cool-off the primary is tried again and, when well, kept.
    clock["t"] += 61
    primary.fails = 0
    assert p.make_request("eth_gasPrice", [])["result"] == "eth_gasPrice:4"
    assert p.rested_until == 0.0


def test_a_revert_is_not_a_reason_to_switch():
    class Reverts:
        def make_request(self, method, params):
            raise ValueError("execution reverted")

    p, _ = build(Reverts(), Answer())
    with pytest.raises(ValueError, match="execution reverted"):
        p.make_request("eth_call", [])
    assert not _throttled(ValueError("execution reverted"))
    assert _throttled(requests.ConnectionError("boom"))

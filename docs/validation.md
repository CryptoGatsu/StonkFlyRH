# Validation status

Recorded while porting the fork on 2026-09-10. **No transaction was sent to Robinhood
Chain, and no RPC endpoint was contacted.** Every chain interaction below runs against an
in-memory double.

Local result: **148 tests passed, 1 skipped.** The skipped test is the opt-in
full-connectome integration test, which needs the ~1.1 GB MaleCNS download and a compiled
kernel; it did not run in this environment, so nothing here re-verifies upstream's neural
claims. Those are recorded in [upstream's own validation](https://github.com/nftechie/stonkfly)
and were not reproduced for this fork.

| Check | Observed result | What it does not establish |
| --- | --- | --- |
| Fee split, 24 tests | `dev + treasury == gross` for every input tried, including 1, 5, 9, 99 and 10¹⁸ wei; rounding always favours the treasury, so a payout can never exceed intake; accrual is idempotent and a re-accrual with different amounts is refused | That a sweep reaches the development wallet on a real chain |
| Execution and accounting, 59 tests | Budget, stale and future quotes, round-trip cost, inventory, cooldown, daily limit, minimum notional, gas share, loss stop, STOP file, duplicate settlement and slippage-bound violations all reject; settlement and fee accrual commit together | Behaviour against a real pool, a real router, or a token that taxes transfers |
| Quoting, 20 tests | Both legs are priced at the run's own order size; a 1% pool shows an ~2% round trip; an empty pool is an error, not a zero price; oracle seeding falls back to a flat seed and records that it did | That any particular Robinhood Chain pool prices this way |
| Wallets, 9 tests | Keystores are Web3 Secret Storage v3, chmod 0600, refuse to be overwritten, refuse to load when group-readable, and hold no plaintext key | Custody practices for real funds |
| On-chain booking and payouts, 15 tests | Fills are booked from balance deltas, so a transfer-taxing token is accounted at what arrived; a buy's fee is the reserved one and a sell's comes from realised proceeds; approval gas joins the swap's; a swap that moved no balance is unresolved rather than booked; the balance check expects cash plus unswept fees; a sweep below threshold, above the wallet balance, or with no development wallet configured does not send | That any of this behaves the same against a real router and a real pool |
| Website, 15 tests | Serves the page, state, trades and the sensory frame; no route reaches the ledger file, the keystore, a path traversal or a write method; the SSE cursor resumes from the client's last tick | Behaviour when exposed beyond localhost |
| Live site rendered end to end | 18 paper ticks from the fixture market, 12 charged fills, rendered in a headless browser at 1280 px and 400 px with no console error; the dev-share tile matched the ledger's booked 20% | That the neural loop drove those ticks — the signals in that run were synthetic |

## What was not verified

- **The neural loop was never run.** Without the dataset, no observation reached the
  connectome in this environment. The decoder, plasticity rule and reinforcement pathway
  are upstream code carried over unchanged, and the trade-side interface they feed was
  tested with a stub controller only.
- **No address was verified against a live chain.** `chain verify` is tested only in the
  sense that its individual refusals are readable; it has never been pointed at chain
  4663. The registry ships empty for that reason.
- **No live swap, no live sweep, no gas estimate.** The accounting halves of
  `RobinhoodChainBroker` and `FeeSweeper` are tested against doubles; the halves that
  sign, broadcast and wait for a receipt have never run. Treat the first live run as
  untested code and start on testnet.

## Reproduce

```sh
python -m pytest -q
OPENBLAS_NUM_THREADS=1 STONKFLYRH_FULL_TEST=1 python -m pytest -q
python -m stonkflyrh verify
python -m stonkflyrh run --fixture --fast --steps 6 --out runs/check-fixture
python -m stonkflyrh serve --out runs/check-fixture
```

The last two need the prepared dataset. All runtime evidence stays local in `runs/`; it is
not uploaded with this report. A normal `run` omits `--fast` and samples at the configured
wall interval.

Before claiming learned performance, implement the held-out replay, shuffled
reinforcement, exposure baselines, retention and memory-reset comparisons described in
[the model](model.md). This repository provides a functioning experimental loop, not that
empirical result.

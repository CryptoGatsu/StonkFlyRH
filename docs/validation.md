# Validation status

Recorded while porting the fork on 2026-09-10, updated the same day for the USDG quote asset. **No transaction was sent to Robinhood
Chain, and no RPC endpoint was contacted.** Every chain interaction below runs against an
in-memory double.

Local result: **255 tests passed, 1 skipped.** The skipped test is the opt-in
full-connectome integration test, which needs the ~1.1 GB MaleCNS download and a compiled
kernel; it did not run in this environment, so nothing here re-verifies upstream's neural
claims. Those are recorded in [upstream's own validation](https://github.com/nftechie/stonkfly)
and were not reproduced for this fork.

| Check | Observed result | What it does not establish |
| --- | --- | --- |
| Rug screen and rug watch, 33 tests | A honeypot, a 15% transfer tax, a thin pool, shallow liquidity, an EIP-1967 proxy, a mint/blacklist/fee-setter selector, an unrenounced owner and a pool with one oracle observation are each rejected; a clean token passes all nine checks; a screen that throws rejects; verdicts cache and expire; the screen blocks buys and never sells; a 50% collapse is recorded once, blocklists the token, earns a 2× pulse and tightens thresholds, capped after four rugs | That any real Robinhood Chain token has the bytecode, pool or owner these doubles model |
| Dollar limits and adaptation, in the 82 execution tests | A $10 order is 10 USDG at $1,500, $2,500 and $4,000 an ETH, and gas is subtracted from equity at each; a 10-gwei swap is vetoed against a $10 order and a 0.1-gwei one is not; calm history trades full size, choppy history shrinks it, extreme history stops a buy and never a sell; a losing streak triples the cooldown; one drained pool no longer freezes other tokens; the exit from a recorded rug may pay a wide spread | That volatility measured on fixture prices resembles a memecoin's |
| ETH/USD reference, 16 tests | Used only to value gas: a fresh Chainlink round prices in dollars; a stale, future, zero or absurd answer stops the run; the WETH→USDG pool prices from a real swap quote and is the default; no reference refuses to run | The WETH/USDG pool on the real chain |
| Posting to X, 20 tests | Fills compose within 280 characters with the link's fixed cost reserved; holds and vetoes are not posted; the OAuth 1.0a header is deterministic and signed; posts are drafted without credentials, throttled, never repeated, and a failed send records only the exception type | That X accepts the signature; no request was sent |
| Fees, 15 tests | Accrual is idempotent, rounds down, survives reopen; outstanding tracks payouts and never goes negative; the default fee is 0 bps | That a sweep reaches the fee wallet on a real chain |
| Quoting, 22 tests | Both legs are priced at the run's own order size; a 1% pool shows an ~2% round trip; an empty pool is an error, not a zero price; oracle seeding falls back to a flat seed and records that it did | That any particular Robinhood Chain pool prices this way |
| Wallets, 18 tests | Importing a key that derives the expected fly wallet writes a keystore; one that derives anything else writes nothing; a malformed key is refused; keystores are Web3 Secret Storage v3, chmod 0600, refuse overwrite, refuse to load group-readable, refuse to load for the wrong wallet, and hold no plaintext key | Custody practices for real funds |
| On-chain booking and payouts, 18 tests | Fills are booked from balance deltas, so a transfer-taxing token is accounted at what arrived; a buy's fee is the reserved one and a sell's comes from realised proceeds; approval gas joins the swap's; a swap that moved no balance is unresolved rather than booked; the balance check expects cash plus unswept fees; a sweep below threshold, above the wallet balance, or with no development wallet configured does not send | That any of this behaves the same against a real router and a real pool |
| Static preview, 4 tests | The page embeds the run, marks itself a preview, inlines the frame, and carries no route, no ledger path and nothing from the keystore | — |
| Website, 15 tests | Serves the page, state, trades and the sensory frame; no route reaches the ledger file, the keystore, a path traversal or a write method; the SSE cursor resumes from the client's last tick | Behaviour when exposed beyond localhost |
| Live site rendered end to end | 24 paper ticks over four fictional tokens through the real guard, screen, watch, broker and poster: one token failed the screen (4/9) and was never bought; one passed 9/9, was bought four times, collapsed 79.5%, was recorded as a rug once, exited through the widened spread, and blocklisted; thresholds tightened to 1.5×; 12 posts drafted; rendered at 1320 px and 400 px with no console error | That the neural loop drove those ticks — the signals in that run were synthetic |

## What was not verified

- **The neural loop was never run.** Without the dataset, no observation reached the
  connectome in this environment. The decoder, plasticity rule and reinforcement pathway
  are upstream code carried over unchanged, and the trade-side interface they feed was
  tested with a stub controller only.
- **No address was verified against a live chain.** The Uniswap and USDG addresses in
  `tokens.example.json` come from Uniswap's deployment records and Blockscout's listing,
  not from a call this repository made; USDG's 6 decimals are assumed from the Ethereum
  deployment. `chain verify` and the rug screen have never been pointed at chain 4663. The screen's selector scan, proxy check and
  two-size tax measurement are exercised against a pool model, not a real memecoin. The
  registry ships empty for that reason.
- **No post was sent to X.** The signature is checked for shape and determinism, not
  against X's verifier.
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

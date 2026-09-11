# The rug screen, and learning from a rug

Two halves, both in `stonkflyrh/safety.py`. The first can only withhold a buy.
The second only ever adds to a blocklist and lengthens a pulse. Neither proposes
a trade, picks a token, or overrides a neural HOLD. That asymmetry is the whole
design: a screen that is too strict costs missed trades, while a screen that can
act costs money.

## Before a buy

Every question is answered from chain state this process reads itself. No
reputation service, no allowlist, no score from anywhere else.

| Check | Question asked of the chain | Fails when |
| --- | --- | --- |
| `contract_code` | `eth_getCode(token)` | the address holds no code |
| `upgradeable` | EIP-1967 implementation slot | non-zero: the contract behind the address can be swapped |
| `owner_levers` | PUSH4 immediates in the runtime bytecode | a `mint`, `blacklist`/`pause`/trading toggle, or fee-setter selector is present |
| `ownership` | `owner()` if the token has one | not the zero address |
| `liquidity` | v3: USDG balance of the pool × 2; v4: the pool's liquidity and price from the StateView, valued in USDG (through the bridge for an ETH- or GOOGL-paired pool), else depth inferred from price impact net of the pool fee | below the floor (default $4,000) |
| `pool_history` | v3: `slot0().observationCardinality` | fewer than 4 oracle observations — the pool is brand new |
| `pool_age` | v4: seconds since the block the pool was initialised in | younger than 30 minutes |
| `executable` | v4, live runs only: the real buy at the run's order size, dry-run through the Universal Router from the fly wallet | it reverts — a quote never moves tokens, so a token whose transfer refuses the router (honeypot, blacklist, trading not open) passes every quote and fails here. A missing approval is not held against the token. This check vetoes the buy and shows on the site but never evicts a token from the universe: the run's own swap path can be what is failing |
| `pool_empty` | v4: liquidity is zero | the pool exists but has no liquidity yet (a Pons token before graduation): not a rejection — the candidate is screened again every 30 minutes for a day |
| `sellable` | quoter, token → USDG | the sell leg returns nothing: a honeypot |
| `transfer_tax` | round trip of a tiny probe, minus twice the pool fee, halved | above the ceiling (default 5% per side) |
| `price_impact` | round trip at the run's order size minus the tiny probe's | above the ceiling (default 3%) |

The last two are one measurement taken at two sizes. A probe a thousandth of
the order carries almost no price impact, so what it loses beyond the pool fee
is the token's transfer tax. The full-size probe loses that plus impact. The
difference is the impact. Neither number needs the token's source code.

The selector scan over-reports by design: a PUSH4 can be any constant. It is
used as a reason to withhold, not as proof, which is the direction an
over-report is safe in.

A verdict is cached per token for `screen_ttl_seconds` (15 minutes) and
re-asked after that. `python -m stonkflyrh screen --products PONS` asks without
trading. A screen that throws — a node that stops answering — is a rejection,
never an approval.

Sells are never screened. Whatever the screen thinks of a token, a run that
holds it must always be able to get out.

## After a buy

`RugWatch` keeps the worst price paid for each open position. When a position's
bid falls `rug_drawdown` (default 50%) below it, the run records a rug:

1. The token goes on a permanent blocklist for the life of the run.
2. That observation's reinforcement is aversive regardless of the equity delta,
   and the pulse into the two PPL101 cells runs `rug_pulse_multiplier` (default
   2×) longer than an ordinary loss pulse. It is the same engineered current
   into the same identified cells; the longer duration is the only difference.
3. The screen's thresholds tighten by `rug_tightening` (default 1.5×) — the
   liquidity floor rises, the tax and impact ceilings fall — capped after four
   rugs so the screen cannot seize up.
4. The round-trip cost limit and the price-move tolerance are relaxed to
   `rug_exit_spread` (default 50%) for **selling that token only**. A drained
   pool has a wide spread; paying it is the price of leaving, and holding to
   zero is worse. The swap's on-chain minimum output still applies.

Point 3 is an engineered heuristic over recorded features and is disclosed as
such. It is not the connectome learning, and nothing here edits a synapse.
Point 2 is the part the connectome can learn from, in exactly the sense
[the model](model.md) describes: a pairing of recent sensory activity with a
dopamine pulse changes eligible KC→MBON efficacies. Whether that produces
better decisions later is unvalidated, here as everywhere in this repository.

## What it does not catch

- **A slow rug.** A creator selling into strength over hours never crosses the
  drawdown line in one observation and never trips a screen check.
- **Concentrated holders.** Who holds the supply needs an indexer this process
  does not have.
- **A lever behind a proxy that passed.** With `reject_upgradeable` off, the
  selector scan reads the proxy, not the implementation.
- **Off-chain intent.** Nothing here reads a Telegram group.

## The loss stop still wins

A run that loses `loss_stop_usd` (default $25 of a $100 stake) halts, and the
halt stops *all* new orders — including the exit from a rug that just caused
it. That is upstream's rule and it is kept deliberately: a halted run does not
liquidate, and a financial stop cannot be cleared by a flag. If a rug takes you
through the stop, the position is yours to unwind by hand, with the ledger and
explorer in front of you.

# Operation and recovery

## Setup

Use Python 3.11 or newer and a C++17 compiler (`clang++`/`c++` on macOS, GCC or Clang on Linux).
`python -m stonkflyrh prepare` downloads about 1.1 GB of upstream data, verifies it, and
builds the full graph. Allow several additional GB for dependencies, derived data and two
checkpoints. `python -m stonkflyrh verify` independently checks prepared inputs. Set
`STONKFLYRH_DATA` to use another data location.

## Network

| | Mainnet | Testnet |
| --- | --- | --- |
| Chain id | 4663 | 46630 |
| RPC | `https://rpc.mainnet.chain.robinhood.com` | `https://rpc.testnet.chain.robinhood.com` |
| Gas token | ETH | ETH |

`STONKFLYRH_RPC_URL` overrides the endpoint. The chain id is verified on connect either
way, so a node pointed at another network fails immediately rather than pricing or
trading against the wrong chain.

## Contract registry

`tokens.json` holds the quote asset (USDG), WETH, the Uniswap v3 router and quoter, and
one entry per memecoin with its address, decimals and pool fee tier.

The example file is pre-filled for mainnet from two records that agree with each other —
Uniswap's `deployments/4663.md` and its `sdk-core` address map — plus the USDG address
Blockscout lists:

| Contract | Address |
| --- | --- |
| USDG (quote) | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` |
| WETH9 | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` |
| SwapRouter02 | `0xcaf681a66d020601342297493863e78c959e5cb2` |
| QuoterV2 | `0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7` |
| UniswapV3Factory | `0x1f7d7550b1b028f7571e69a784071f0205fd2efa` |

None of these were confirmed against the chain from inside this repository, and USDG's
decimals (6) are assumed from the Ethereum deployment. `python -m stonkflyrh chain verify`
is what confirms them, before any run:

- the router and quoter hold code, and report the same factory;
- the factory holds code, and has a pool for every traded pair at its fee tier, and a
  WETH/USDG pool at `weth_pool_fee` for valuing gas;
- the quote token's on-chain `symbol()` and `decimals()` are what the file says;
- each memecoin's on-chain `symbol()` and `decimals()` match what you wrote.

Check them on robinhoodchain.blockscout.com yourself as well. These checks catch a typo
or a stale address; they cannot tell you that a contract is the one you meant.

## Wallets

One key lives locally: the **fly wallet**, `0x68e82397455232f6F726E44ad1c980C6C99B3201`
by default. It holds the run's WETH and gas ETH and signs every swap. You fund it and
launch the coin from it, so the usual flow is `python -m stonkflyrh wallet import`,
pasting its existing key. The key must derive the expected address or nothing is
written; `STONKFLYRH_FLY_WALLET` changes what is expected. The keystore is Web3 Secret
Storage, chmod 0600, in `keystore/` (git-ignored).

The **fee wallet**, `0x7f5afC67d4C3AE0182354ea6e785FdEb20150f15` by default, needs no
key: this process only ever sends to it.

Back up the keystore directory and its password. There is no recovery path. Set
`STONKFLYRH_KEYSTORE_PASSWORD` for unattended runs, understanding that it puts the
password in the environment; leave it unset to be prompted.

## Dollar limits

The quote asset is USDG, so `capital_usd`, `order_limit_usd`, `min_order_usd` and
`loss_stop_usd` are the ledger's own units: a $10 order is 10 USDG. Live preflight
refuses a wallet funded past the dollar cap.

Gas is ETH. To subtract it from equity honestly the run needs ETH/USD, taken by default
from selling a 0.01 WETH probe into the USDG pool through the quoter (`weth` in the
registry), or from a Chainlink aggregator (`eth_usd_feed`). A non-positive or implausible
price stops the run.

## Fees

`protocol_fee_bps` defaults to 0: with both wallets yours, a fee would only move your
own money and pay gas to do it. When set, every fill accrues the fee in USDG in the same
transaction as the settlement, owed to the fee wallet. Accrued fees stay in the fly
wallet until swept, which is why the balance check expects cash *plus* unswept fees.
Fees and sweeps are in USDG.

```sh
python -m stonkflyrh fees --out runs/live --dry-run   # what would be sent
python -m stonkflyrh fees --out runs/live --sweep     # send it
```

A sweep writes its payout row and records the signed transaction hash before broadcast,
then confirms the receipt. An interrupted sweep is reconciled on the next one and never
re-sent blindly.

## Limits

Defaults: $100 capital, $10 maximum order, $1 minimum order, 2% slippage bound, 6%
maximum round-trip pool cost per token, 24 orders/day, at least 60 s between orders,
gas capped at 5 gwei and at 25% of an order's notional.

Adaptation, on by default: order size shrinks linearly from the $10 cap toward 25% of it
as realised volatility over the last 30 observations rises from 2% to 25% per
observation, and buys stop above 25%; the cooldown triples after two consecutive
aversive observations. Sells are never scaled or stopped by either. `--no-adapt` fixes
both.

A $25 drawdown halts new orders. **It does not liquidate holdings or cap further
losses.** Holdings stay exposed to the market after a halt — including a rugged one;
see [the rug screen](safety.md).


## Network errors

The public RPC rate-limits. Log fetches are chunked, paced and retried with
backoff; the observation loop treats a request error, timeout or 429 as
transient, waits `rpc_error_backoff_seconds` × the consecutive count (capped at
five minutes) and tries again, up to `rpc_error_tolerance` in a row. An error
while an order is in flight is never transient. A run halted by a transient
error resumes on the next start without review; a dedicated RPC endpoint
(`STONKFLYRH_RPC_URL`) makes all of this rarer.

Discovery keeps its place: the block reached is written to the ledger after every
6,000-block window and candidates wait in a queue there until screened, so an error
mid-scan resumes from the last window and never re-screens a pool it already judged.

## Changing settings on a running ledger

Restarting with a changed `.env` is fine for tuning values: order size, screen
thresholds, discovery pacing, adaptation. The change is recorded as a `migration`
event in the ledger and the run carries on. Values that define what the run *is* —
network, quote asset, donor share — still refuse; those need a separate run directory.
`python -m stonkflyrh discovery --out runs/live` shows the halt reason and the last
`error.json` alongside the scan state.

## Stopping

`touch runs/live/STOP` stops before the next order and is checked again at the final send
boundary. Ctrl-C stops after the current tick. Both preserve run state; the same command
resumes.

## When something goes wrong

The worker halts and writes `error.json` rather than continuing. Dependency exception
text is never printed: it can carry wallet, RPC or request details.

An order in `UNKNOWN` state has a recorded transaction hash, because the hash of a signed
transaction is fixed and is written before the broadcast. Look it up on the explorer.
Reconciliation resolves it from the receipt; a swap that mined while the process was not
watching halts the run for a manual balance check instead of being booked automatically.
**Nothing is ever resubmitted automatically.**

`--resume-reviewed` clears a transient halt after reconciliation succeeds. It refuses to
clear a loss stop or a fee overrun: those are financial stops and are yours to decide on.

## Discovery

Every `discovery_interval_seconds` (10 min) the worker reads the Uniswap v3 factory's
`PoolCreated` logs since the block it last scanned (on a fresh run, the last
`discovery_lookback_blocks`), in chunks the public RPC accepts. A pool counts when one
side is USDG and the fee tier is 0.05%, 0.3% or 1%. Newest first, up to
`discovery_batch` a scan, each candidate token has its `symbol()`/`decimals()` read and is
put through the full rug screen. Approved tokens join the universe up to `max_products`;
rejected ones are remembered so they are not screened again. After a scan, every
discovered token the fly does not hold is re-screened and dropped if it no longer clears.
Seeds from `STONKFLYRH_PRODUCTS` are never dropped.

**Uniswap v4.** Pons V2 graduates tokens into v4 pools behind the Pons hook, so the
worker also reads the PoolManager's `Initialize` events. A v4 pool is admitted when its
hook is on `hooks_allow` in the registry (the Pons hook is pre-filled; hookless pools
are always allowed) and USDG can reach its token: directly, or through a **bridge** — a
USDG pool for the pair asset. Bridges come from the registry (`v4.bridges`, e.g. a
GOOGL/USDG PoolKey), from hookless standard-fee USDG pools seen on chain, and from the
StateView directly: on start the worker asks for USDG/ETH and USDG/WETH pools at the
standard tiers, and when a launch pairs with an asset it does not know (GOOGL, say) it
asks for a USDG pool of that asset too, at most once per 20,000 blocks. A launch paired
with an asset that has no bridge yet waits, and is routed the moment one appears.

Pons initialises a token's v4 pool when the token is created and adds the liquidity at
graduation. A candidate whose pool is empty is not rejected: it goes back in the queue
and is screened again every `min_pool_age_seconds` for up to 48 tries.
Native-ETH pairs route through the USDG/ETH pool once it has been seen. Trades on v4 go through the Universal
Router via Permit2, single-hop or multi-hop in one transaction; the screen infers depth
from price impact and checks pool age instead of v3's oracle history.

Symbols are normalised to uppercase alphanumerics and a collision gets four hex
characters of the address appended, so two tokens calling themselves PEPE stay two
tokens. `STONKFLYRH_DISCOVERY=0` trades only the seeds.

## The website

```sh
python -m stonkflyrh serve --out runs/paper --port 8787
```

Read-only, bound to 127.0.0.1. It tails `events.jsonl` and the ledger, imports no broker
and opens no keystore. `--host 0.0.0.0` is for putting it behind a reverse proxy you
control; it publishes wallet addresses, balances and trade history, so treat that as
publishing.

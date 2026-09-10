# Operation and recovery

## Setup

Use Python 3.11 and a C++17 compiler (`clang++`/`c++` on macOS, GCC or Clang on Linux).
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

`tokens.json` holds the WETH address, the Uniswap v3 router and quoter, and one entry per
memecoin with its address, decimals and pool fee tier. Nothing is shipped filled in.
`python -m stonkflyrh chain verify` checks, before any run:

- the router and quoter hold code, and report the same factory;
- the factory holds code, and has a pool for every traded pair at its fee tier;
- the quote asset really is 18-decimal WETH;
- each token's on-chain `symbol()` and `decimals()` match what you wrote.

Verify addresses yourself against the Robinhood Chain explorer and the Uniswap
deployments list before you put them in the file. These checks catch a typo or a stale
address; they cannot tell you that a contract is the one you meant.

## Wallets

`python -m stonkflyrh wallet create` generates two keys locally and writes them as
Web3 Secret Storage keystores, chmod 0600, in `keystore/` (git-ignored):

- **trading** — holds the run's WETH and gas ETH, signs swaps and fee payouts;
- **fee** — receives the 80% treasury share of protocol fees.

The development wallet is not created here: it is your address, set as
`STONKFLYRH_DEV_WALLET`, and this process only ever sends to it.

Back up the keystore directory and its password. There is no recovery path. Set
`STONKFLYRH_KEYSTORE_PASSWORD` for unattended runs, understanding that it puts the
password in the environment; leave it unset to be prompted.

## Fees

Every fill accrues a protocol fee in WETH, booked in the same transaction as the
settlement, split 20% development / 80% treasury in integer wei with the remainder going
to the treasury. Accrued fees stay in the trading wallet until swept, which is why the
balance check expects cash *plus* unswept fees rather than cash alone.

```sh
python -m stonkflyrh fees --out runs/live --dry-run   # what would be sent
python -m stonkflyrh fees --out runs/live --sweep     # send it
```

A sweep writes its payout row and records the signed transaction hash before broadcast,
then confirms the receipt. An interrupted sweep is reconciled on the next one and never
re-sent blindly.

## Limits

Defaults: 0.05 WETH capital, 0.005 WETH maximum order, 0.0005 WETH minimum order, 1%
protocol fee, 1% slippage bound, 3% maximum round-trip pool cost, 24 orders/day, at least
60 s between orders, gas capped at 5 gwei and at 25% of an order's notional.

A 0.01 WETH drawdown halts new orders. **It does not liquidate holdings or cap further
losses.** Holdings stay exposed to the market after a halt.

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

## The website

```sh
python -m stonkflyrh serve --out runs/paper --port 8787
```

Read-only, bound to 127.0.0.1. It tails `events.jsonl` and the ledger, imports no broker
and opens no keystore. `--host 0.0.0.0` is for putting it behind a reverse proxy you
control; it publishes wallet addresses, balances and trade history, so treat that as
publishing.

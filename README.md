![Stonkfly: a pixel fly beside a candlestick chart](assets/stonkfly.png)

# StonkFlyRH

A fly-connectome simulation that trades memecoins on **Robinhood Chain**. A fork of
[nftechie/stonkfly](https://github.com/nftechie/stonkfly), which traded Coinbase spot.
Actual neural output, actual on-chain swaps, a live website. Profitable learning has
not been demonstrated.

**How it works:** Uniswap v3 quotes from Robinhood Chain (chain id 4663) become an RGB
chart. It stimulates 3,335 brightness inputs and 811 R8 color inputs in the retained
**MaleCNS v1.0 graph: 166,700 neurons, 25.6 million connections**. A fixed neural readout
proposes buy, sell or hold. A guarded action provider checks limits and sends one
`exactInputSingle` swap against the memecoin's WETH pool from a local keystore wallet.

Positive portfolio P&L stimulates 15 identified PAM11 dopamine cells; negative P&L
stimulates two PPL101 aversive dopamine cells. A candidate memory rule changes existing
KC-to-MBON connections. These are engineered reinforcement signals, **not modeled pain
receptors**. Synaptic changes do not establish that it learns to trade profitably.
[Model and evidence](docs/model.md).

## Live trade site

![The live trade dashboard](assets/site.png)

```sh
python -m stonkflyrh serve --out runs/paper
```

A read-only page at `http://127.0.0.1:8787` that streams every tick as it happens: the
trade feed with signal, price, execution and explorer link; equity and positions; the
chart frame the connectome is looking at right now; and the fee split. It tails the
worker's log and ledger, holds no key, and can place no trade.

## Fees

Every fill accrues a protocol fee in WETH, split the moment it is booked:

| Share | Goes to |
| --- | --- |
| **20%** | the development wallet (`STONKFLYRH_DEV_WALLET`) |
| 80% | the fee wallet this repo creates for you |

The split is integer wei with the remainder to the treasury, so the two shares always
add back to the gross exactly. 20% is a constant in `stonkflyrh/fees.py`, not a setting.
Fees are swept in batches rather than transferred per swap:

```sh
python -m stonkflyrh fees --out runs/live              # what is owed
python -m stonkflyrh fees --out runs/live --sweep      # pay it out
```

## Run it

Python 3.11, a C++17 compiler, macOS/Linux. Allow several GB for the dataset and
dependencies; 16 GB RAM recommended.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
python -m stonkflyrh prepare
python -m stonkflyrh run --fixture
```

`--fixture` needs no network and no addresses. Drop it for paper trading against real
Robinhood Chain quotes, which needs `tokens.json` (below). Default: **paper trades,
0.05 WETH of simulated balance**. No key needed. Local logs, sensory images and
resumable brain state go in `runs/paper/`. Ctrl-C stops it; the same command resumes.

## Real swaps

**No contract address ships in this repository.** Robinhood Chain's Uniswap deployments
and memecoin tokens are yours to look up and verify. Copy `tokens.example.json` to
`tokens.json`, fill it in, and check it against the chain:

```sh
python -m stonkflyrh chain verify --products DOGE
```

That refuses an address holding no code, a router and quoter that disagree about their
factory, a token whose on-chain symbol or decimals differ from your file, and any pair
with no pool at the configured fee tier.

Then create the wallets, fund the trading wallet with **at most 0.05 WETH** plus a little
ETH for gas, copy `.env.example` to `.env` and fill it in locally:

```sh
python -m stonkflyrh wallet create
python -m stonkflyrh run --live --preflight-only
python -m stonkflyrh run --live
```

Defaults: 0.005 WETH maximum order, 24 attempts/day, 1% slippage bound, no leverage.
A 0.01 WETH drawdown stops new orders; **it does not liquidate holdings or cap further
losses**. [Operation and recovery](docs/operations.md).

```sh
python -m stonkflyrh status
python -m pytest -q
```

The repo does not come funded or connected to anyone's wallet. Live execution needs your
local keystore and explicit opt-in.

![Stonkfly: a pixel fly beside a candlestick chart](assets/stonkfly.png)

# StonkFlyRH

A fly-connectome simulation that trades memecoins on **Robinhood Chain**, screens
them for rugs first, and posts what it does to X. A fork of
[nftechie/stonkfly](https://github.com/nftechie/stonkfly), which traded Coinbase spot.
Actual neural output, actual on-chain swaps, a live website. Profitable learning has
not been demonstrated.

**How it works:** Uniswap v3 quotes from Robinhood Chain (chain id 4663) become an RGB
chart. It stimulates 3,335 brightness inputs and 811 R8 color inputs in the retained
**MaleCNS v1.0 graph: 166,700 neurons, 25.6 million connections**. A fixed neural readout
proposes buy, sell or hold. A guarded action provider checks the dollar limits and the
rug screen, then sends one `exactInputSingle` swap from the fly wallet.

Positive portfolio P&L stimulates 15 identified PAM11 dopamine cells; negative P&L
stimulates two PPL101 aversive dopamine cells — and a **rug it bought stimulates them
for twice as long**. A candidate memory rule changes existing KC-to-MBON connections.
These are engineered reinforcement signals, **not modeled pain receptors**. Synaptic
changes do not establish that it learns to trade profitably. [Model](docs/model.md) ·
[Rug screen](docs/safety.md).

## Live trade site

![The live trade dashboard](assets/site.png)

```sh
python -m stonkflyrh serve --out runs/paper      # live, read-only, 127.0.0.1:8787
python -m stonkflyrh preview --out runs/paper    # one static page that replays the run
```

Every tick streams as it happens: the feed with signal, dollar size, execution and
explorer link; the rug screen's verdict on each token; positions; the chart frame the
connectome is looking at right now; and every post it made to X. The site tails the
worker's log and ledger, holds no key, and can place no trade.

## Limits, in dollars

| | Default |
| --- | --- |
| Capital | **$100** |
| Per trade | **$10**, shrinking as realised volatility rises |
| Minimum trade | $1 |
| Loss stop | $25 — stops new orders, does not liquidate |
| Orders | 24 a day, 60 s apart, 3× longer after a losing streak |

Dollar limits are reconverted every observation from the chain's ETH/USD reference
(Chainlink, or a USDG pool through the same quoter), so $10 stays $10 when ETH moves.
The ledger itself is WETH, which is what the wallet holds.

## The rug screen

Before any buy, nine questions asked of the chain itself — is it a proxy, does the
bytecode carry a mint/blacklist/fee-setter, is ownership renounced, is there liquidity,
is the pool new, **can it be sold back**, what does a round trip cost at a tiny size
(the transfer tax) and at the run's size (the impact). A token that fails is not
bought. A sell is never screened: getting out must always work.

If a held token collapses anyway, it is blocklisted for the run, the fly takes the
longer aversive pulse, the screen tightens for everything after it, and the exit is
allowed through the drained pool's wide spread. [Details and limits](docs/safety.md).

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
Robinhood Chain quotes and the real screen, which needs `tokens.json` (below).
Default: **paper trades, $100 simulated**. Local logs, sensory images and resumable
brain state go in `runs/paper/`. Ctrl-C stops it; the same command resumes.

## Real swaps

**No contract address ships in this repository.** Copy `tokens.example.json` to
`tokens.json`, fill in the Uniswap router and quoter, WETH, an ETH/USD reference and
the tokens you want, then check it against the chain:

```sh
python -m stonkflyrh chain verify --products PONS
python -m stonkflyrh screen --products PONS         # what the rug screen thinks
```

Import the fly wallet — the key must derive `0x68e8…3201` or nothing is written — fund
it with **at most $100 of WETH** plus a little ETH for gas, copy `.env.example` to
`.env`, then:

```sh
python -m stonkflyrh wallet import
python -m stonkflyrh run --live --preflight-only
python -m stonkflyrh run --live
```

Posting to X needs the four app credentials in `.env` and `STONKFLYRH_POST_TO_X=1`;
without them every post is still drafted to `posts.jsonl` and shown on the site.
[Operation and recovery](docs/operations.md).

```sh
python -m stonkflyrh status
python -m pytest -q
```

Robinhood Chain only: any other network is rejected at configuration. The repo does
not come funded or connected to anyone's wallet. Live execution needs your local
keystore and explicit opt-in.

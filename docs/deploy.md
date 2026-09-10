# Deploying, and trading real Robinhood Chain memecoins

This is the order of operations for going from the repo to a fly wallet trading
live, with the site public and the fly posting to X. Every step before the last
is reversible and spends nothing.

## Before anything: which wallet launches your coin

Your plan is to launch the coin on Pons from the fly wallet. **Do not.** Live
preflight refuses a fly wallet that holds anything but USDG and gas ETH, and the
balance check halts the run the moment a token balance the ledger does not know
about appears — that is what protects you from booking a transfer as profit. A
creator allocation of your own coin sitting in the fly wallet trips both.

Launch from another wallet you control. Fund the fly wallet with USDG and gas
only. If you want the fly to trade your coin, add it to `tokens.json` like any
other memecoin, once it has graduated to its Uniswap pool.

## 1. A machine

The connectome needs the full MaleCNS graph in memory: **16 GB RAM**, 4+ cores,
~10 GB disk, Ubuntu 24.04, Python 3.11, a C++17 compiler. A small VPS is fine;
it does not need a GPU. Each observation is ~0.5 s of neural time and the run
samples the market once a minute, so it is not CPU-bound between ticks.

```sh
sudo adduser --system --group --home /opt/stonkflyrh stonkfly
sudo -u stonkfly git clone https://github.com/CryptoGatsu/StonkFlyRH /opt/stonkflyrh
cd /opt/stonkflyrh
sudo -u stonkfly python3.11 -m venv .venv
sudo -u stonkfly .venv/bin/pip install -e '.[test]'
sudo -u stonkfly .venv/bin/python -m stonkflyrh prepare     # ~1.1 GB download, compiles the kernel
sudo -u stonkfly .venv/bin/python -m pytest -q
```

## 2. Prove the loop offline

```sh
.venv/bin/python -m stonkflyrh run --fixture --fast --steps 12 --out runs/check
.venv/bin/python -m stonkflyrh serve --out runs/check
```

Synthetic prices, no chain, no addresses. If the site at `127.0.0.1:8787` shows
ticks and the fly moving, the neural side and the site work on this machine.

## 3. Verify the chain and the addresses

```sh
cp tokens.example.json tokens.json
# add the memecoins you want under "tokens": address, decimals, pool_fee
.venv/bin/python -m stonkflyrh chain verify --products PONS
```

This is the step that turns the pre-filled addresses from "what Uniswap's records
say" into "what chain 4663 says": code at every address, router and quoter
agreeing on the factory, USDG's symbol and decimals, a pool for every pair, a
WETH/USDG pool for gas. It refuses anything that does not check out. Look the
addresses up on robinhoodchain.blockscout.com yourself as well.

Then ask the screen what it thinks of your list, with no wallet involved:

```sh
.venv/bin/python -m stonkflyrh screen --products PONS
```

## 4. Paper-trade against real prices

```sh
.venv/bin/python -m stonkflyrh run --products PONS --out runs/paper
```

Real Uniswap quotes, real rug screen, simulated fills. Leave it for a day. Watch
the DECISIONS tab: you want to see vetoes with reasons you agree with, and the
size scale moving with volatility. This is also the run that tells you whether
the tokens you picked ever clear the screen.

## 5. Fund and import the fly wallet

Bridge to Robinhood Chain (Robinhood's app supports USDG withdrawals to it;
Across bridges ETH). Into `0x68e8…3201`:

- **at most 100 USDG** — preflight refuses more than `capital_usd`;
- **~0.02 ETH** for gas — swaps cost a few cents each on this chain.

```sh
cp .env.example .env            # fill in; chmod 600 .env
.venv/bin/python -m stonkflyrh wallet import
```

The import refuses a key that derives any address but the fly wallet's. For
unattended runs `STONKFLYRH_KEYSTORE_PASSWORD` must be in `.env`, which means
the password lives on the box; that is the trade-off of an unattended bot. Back
up `keystore/` and the password somewhere else.

## 6. Preflight, then go live

```sh
.venv/bin/python -m stonkflyrh run --live --preflight-only --products PONS
.venv/bin/python -m stonkflyrh run --live --products PONS --out runs/live
```

Preflight reads everything and sends nothing: balances, allowances, the screen,
the price reference. It initialises the ledger from the wallet's actual USDG.
The second command trades. Ctrl-C stops it; the same command resumes from its
checkpoint. `touch runs/live/STOP` stops it from another terminal.

## 7. Run it as services

```sh
sudo cp deploy/stonkflyrh-worker.service deploy/stonkflyrh-site.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stonkflyrh-site stonkflyrh-worker
journalctl -fu stonkflyrh-worker
```

The **worker does not auto-restart**. When it exits it has halted — loss stop,
an order it could not resolve, a balance that moved, a dependency error — and
`runs/live/error.json` says why. Read it, reconcile against the explorer, then
start it again (with `--resume-reviewed` for a transient halt; a loss stop cannot
be cleared that way and is yours to decide about). The **site** restarts forever;
it holds no key.

To make the site public, `deploy/nginx.conf` proxies it with SSE buffering off.
Add TLS with certbot. The page publishes both wallet addresses, balances, and
every trade; that is the point, but know it.

## 8. Posting to X

Create an app at developer.x.com with read+write, generate the four user-context
credentials, put them in `.env` and set `STONKFLYRH_POST_TO_X=1`. Restart the
worker. Until then every post is still drafted to `runs/live/posts.jsonl` and
shown on the POSTS tab, so you can see the voice before it goes out.

## What "learning" means here, honestly

Three things change with experience, and they are different kinds of thing:

1. **Synapses.** 7,835 KC→MBON connections change under the plasticity rule when
   recent sensory activity is paired with a dopamine pulse — reward for gains,
   aversive for losses, a longer aversive pulse for a rug. This is the fly. It is
   on by default and persists in `runs/live/brain-*.npz`; the run resumes from it.
   Whether it produces better decisions is **unvalidated**, here and upstream.
2. **The blocklist.** A rugged token is never bought again this run. Durable, dumb,
   effective.
3. **The screen's thresholds.** Each rug raises the liquidity floor and lowers the
   tax and impact ceilings for everything after it. An engineered heuristic, not
   the connectome.

For (1) to have anything to learn from, the run must go long enough to see
reward and loss many times. Days, not hours. Keep `--frozen` control runs in a
separate `--out` if you want to know whether the plasticity is doing anything.

## Choosing coins

The fly trades the list you give it. It does not discover launches. On Pons that
means picking graduated tokens with real USDG pools, adding them to `tokens.json`,
and letting `chain verify` and `screen` tell you which ones the run would even
touch. Discovery — scanning Pons graduations and screening them automatically —
is the obvious next feature and is not built.

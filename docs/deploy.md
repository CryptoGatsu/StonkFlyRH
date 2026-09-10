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
only. Discovery will find your coin once it graduates to its Uniswap v4 pool —
provided USDG can reach it. Pair it with USDG and it is one hop. Pair it with
GOOGL (or any tokenized stock) and the fly routes USDG → GOOGL → coin in one
transaction, paying two pool fees each way and carrying GOOGL's price moves
inside the memecoin position; put the GOOGL/USDG PoolKey under `v4.bridges` in
`tokens.json` so the route exists before the pool does. Pair it with ETH and this
version will not trade it.

## 1. Install

The connectome holds the full MaleCNS graph in memory. **16 GB RAM** is the
comfortable size; **8 GB works** with the swapfile the installer adds, at the
cost of a slower first build and possibly slower ticks — resize in place later
if it feels sluggish. 4+ cores, ~15 GB disk, Ubuntu 22.04/24.04, no GPU. One
command as root:

```sh
curl -fsSL https://raw.githubusercontent.com/CryptoGatsu/StonkFlyRH/main/deploy/install.sh | sudo bash
```

It installs Python (whatever 3.11+ the OS ships) and a compiler, creates the `stonkfly` user, clones to
`/opt/stonkflyrh`, builds the venv, copies `.env.example` → `.env` and
`tokens.example.json` → `tokens.json`, installs both systemd units, starts the
site, and runs the tests. Read the script first; it is short.

## 2. Fill in `.env`

That is the only file. Open `/opt/stonkflyrh/.env`:

| Variable | Set it to |
| --- | --- |
| `STONKFLYRH_MODE` | `paper` first. `live` when you mean it. |
| `STONKFLYRH_CAPITAL_USD` / `STONKFLYRH_ORDER_USD` | `100` / `10` |
| `STONKFLYRH_PRODUCTS` | optional seed symbols from `tokens.json`; empty means discovery alone picks |
| `STONKFLYRH_PRIVATE_KEY` | the fly wallet's key, **for the first live start only** |
| `STONKFLYRH_KEYSTORE_PASSWORD` | a password for the keystore `start` writes |
| `STONKFLYRH_LIVE` | `I_ACCEPT_REAL_ONCHAIN_TRADES`, live only |

Optionally add seed memecoins to `tokens.json` (address, decimals, pool fee) and
list them in `STONKFLYRH_PRODUCTS`. The Uniswap and USDG addresses are already
there; with no seeds the fly waits for discovery's first scan, then trades what
it admits.

## 3. Paper first

```sh
sudo systemctl start stonkflyrh-worker
journalctl -fu stonkflyrh-worker
```

`start` runs in order, skipping anything already done: download and compile the
connectome (several minutes, once); verify every address in `tokens.json` against
chain 4663 — code present, router and quoter agreeing on the factory, USDG's
symbol and decimals, a pool for every seed, a WETH/USDG pool for gas; preflight;
run. With `STONKFLYRH_MODE=paper` it prices off real Uniswap quotes, screens real
pools, discovers real launches, and fills nothing.

Open the site (`127.0.0.1:8787`, or through `deploy/nginx.conf`). Leave it a day.
Watch DECISIONS for vetoes with reasons you agree with, UNIVERSE for what
discovery is admitting, and the size scale moving with volatility.

## 4. Fund the fly wallet

Bridge to Robinhood Chain (Robinhood's app withdraws USDG to it; Across bridges
ETH). Into `0x68e8…3201`, and nothing else:

- **at most 100 USDG** — preflight refuses a wallet funded past `STONKFLYRH_CAPITAL_USD`;
- **~0.02 ETH** for gas — swaps cost cents on this chain.

## 5. Go live

In `.env`: `STONKFLYRH_MODE=live`, `STONKFLYRH_LIVE=I_ACCEPT_REAL_ONCHAIN_TRADES`,
the private key and a keystore password. Then:

```sh
sudo systemctl restart stonkflyrh-worker stonkflyrh-site
journalctl -fu stonkflyrh-worker
```

The first live start imports the key into `keystore/` (it refuses a key that does
not derive the fly wallet), then prints a line telling you to **delete
`STONKFLYRH_PRIVATE_KEY` from `.env`**. Do that. The keystore password stays;
that is what unattended means. Back up `keystore/` and the password elsewhere.

Preflight reads everything and sends nothing, sizes the ledger from the wallet's
actual USDG, and only then does the loop begin trading. `touch runs/live/STOP`
stops it from another terminal; restarting the unit resumes from the checkpoint.

## Updating

The site is `stonkflyrh/web/static/index.html` in the repo, served off the
checkout. Push to GitHub, then on the server:

```sh
sudo /opt/stonkflyrh/deploy/update.sh              # pull + restart the site
sudo /opt/stonkflyrh/deploy/update.sh --worker     # also restart the worker
```

The site can be updated freely while the fly trades: it holds no key, and the
`web/` package is excluded from the run's protocol hash. Updating the *trading*
code is different — the run records a hash of every trading module at start,
and a resumed run whose code changed refuses with "Run source/protocol changed"
so you never unknowingly continue a ledger under different rules. Stop the
worker, start again with a fresh `--out` (or `STONKFLYRH_MODE` directory), and
the old run stays intact for its own record.

## The domain

`deploy/nginx.conf` is written for **stonkflyrh.com**. Point A records for
`stonkflyrh.com` and `www.stonkflyrh.com` at the server, then:

```sh
sudo cp deploy/nginx.conf /etc/nginx/sites-available/stonkflyrh
sudo ln -s /etc/nginx/sites-available/stonkflyrh /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo snap install --classic certbot
sudo certbot --nginx -d stonkflyrh.com -d www.stonkflyrh.com
```

certbot adds TLS and the redirect to HTTPS. The page publishes the fly wallet's
address, its balances and every trade; that is what it is for, but know it.

## 6. Operating it

The worker backs off and retries on network errors, and systemd restarts it
a minute after a crash; `start` resumes a run halted by a network failure on
its own. A halt that needs judgement is different: **the worker exits cleanly
and stays stopped**. When it exits it has halted — loss stop,
an order it could not resolve, a balance that moved, a dependency error — and
`runs/live/error.json` says why. Read it, reconcile against the explorer, then
start it again (with `--resume-reviewed` for a transient halt; a loss stop cannot
be cleared that way and is yours to decide about). The **site** restarts forever;
it holds no key.

To make the site public, `deploy/nginx.conf` proxies it with SSE buffering off.
Add TLS with certbot. The page publishes both wallet addresses, balances, and
every trade; that is the point, but know it.


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

You do not have to. Discovery watches the Uniswap factory for new pools paired
with USDG — which is where every Pons graduation lands — screens each one, and
admits survivors up to `max_products` (12). Seeds in `STONKFLYRH_PRODUCTS` are
always kept; discovered tokens that stop clearing the screen are dropped unless
held. The UNIVERSE tab shows what came in, when, and why things were withheld.
`STONKFLYRH_DISCOVERY=0` turns it off and trades only your seeds.

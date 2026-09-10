# Airdrops

The fly can hand out the operator's coin. It sends from the **deployer wallet**
(`STONKFLYRH_DEPLOYER_WALLET`, the wallet the coin was launched from and bought
back into), never from the fly wallet, which must hold only USDG and gas ETH so
its ledger stays reconcilable.

## Who gets it

The fly already judges which tokens on Robinhood Chain are real: every pool that
clears the rug screen joins its universe. The recipients are the wallets that
bought those tokens and kept them.

Each round the worker:

1. **Takes a census.** For every token in the universe it reads the token's
   `Transfer` logs *from* the places a buy comes from — the token's v3 pool, the
   v4 PoolManager, the two routers — and records each receiving wallet, the
   token and the block. This is paced like discovery (60,000 blocks a round at
   most; the block reached is kept in the ledger) and starts
   `airdrop_lookback_blocks` back on a fresh run.
2. **Ranks wallets** by how many distinct screened tokens they bought, earliest
   first, and keeps only those that **still hold at least one** of them (a
   `balanceOf` check) and **are not contracts** (no code at the address).
   `airdrop_min_tokens` raises the bar; the default is one.
3. **Never sends to** the fly wallet, the deployer, the fee wallet, USDG, WETH,
   pools, routers, the coin itself, the zero address, or a wallet already
   dropped (SENT, or still unresolved).

## How much

| setting | env | default |
|---|---|---|
| coins per wallet | `STONKFLYRH_AIRDROP_AMOUNT` | 1000 |
| wallets per round | `STONKFLYRH_AIRDROP_PER_ROUND` | 5 |
| seconds between rounds | `STONKFLYRH_AIRDROP_INTERVAL_SECONDS` | 3600 |
| coins per 24 h, all rounds | `STONKFLYRH_AIRDROP_DAILY_CAP` | 50000 |
| coins the deployer always keeps | `STONKFLYRH_AIRDROP_RESERVE` | 0 |

A round sends `min(per_round, (balance − reserve) ÷ amount, cap left today)`
drops. With no spare coins, no gas ETH in the deployer wallet, or the cap
reached, it sends nothing and says why in the ledger's `airdrops` event and on
the site's AIRDROPS tab.

## Sending

Each drop is a plain ERC-20 `transfer` signed by the deployer key. The row is
written to the ledger as PREPARED before signing, carries the transaction hash
as UNKNOWN before broadcast, and becomes SENT or REJECTED on the receipt — the
same path donor payouts use. An interrupted round is settled from receipts on
the next start; a hash with no receipt yet blocks further rounds until it has
one. A reverted drop makes the wallet eligible again.

## Turning it on

```
STONKFLYRH_COIN_ADDRESS=0x...            # already set for the site
STONKFLYRH_AIRDROP=1
STONKFLYRH_DEPLOYER_PRIVATE_KEY=0x...    # first live start only, then delete
```

The deployer key must derive `STONKFLYRH_DEPLOYER_WALLET` or nothing is written.
Keep the deployer wallet to the coin and gas ETH: the worker process holds its
key. `STONKFLYRH_AIRDROP=1` on a **paper** run needs no key and only reports who
would receive; the site shows them as "would go".

```sh
python -m stonkflyrh wallet import --role deployer     # instead of the .env line
python -m stonkflyrh airdrop --out runs/live           # who qualifies now (dry run)
python -m stonkflyrh airdrop --out runs/live --send    # one round, now
```

The manual round writes to the same ledger the worker holds; stop the worker
first, or accept a short wait on the lock.

## What this is not

It is not a promotion mechanism the fly optimises. The judgement is fixed and
legible: bought what the screen approved, still holds it, is a person's wallet.
The coin's own buyers are not favoured or excluded by it. Nothing here touches
the fly's trading, its cash or the donation pool.

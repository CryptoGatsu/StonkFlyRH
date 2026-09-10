# Donations and the 50% share

On by default for live runs; `STONKFLYRH_DONATIONS=0` turns it off. A paper run shows the panel marked "not accepting yet".

## Read this first

Taking money from other people and paying them a share of trading profits is,
in substance, a pooled investment. Calling the inbound transfer a donation does
not change what the outbound one is. In the United States that is squarely the
Howey test; most other jurisdictions have an equivalent. **Talk to a lawyer
before you enable this with other people's money.** The code is here because you
asked for it; whether to run it is a decision only you can make and only you
bear.

## How it works

Anyone sends USDG to the fly wallet. The bot recognises it by reading the USDG
contract's `Transfer` events to that address, skipping every transaction it
signed itself (swaps and payouts are matched by hash, so sell proceeds are never
mistaken for a gift). Each recognised transfer is booked in two places:

- **the ledger**, as a deposit: cash, contributed capital and the reward anchor
  all rise together, so new money is never read by the connectome as profit;
- **the pool**, as units bought at that moment's NAV.

The wallet is a pool; ownership is units; NAV is equity ÷ units. The operator's
own stake is the first units, at $1 each. A deposit changes nobody else's value.

Every participant carries a **high-water mark**: the NAV at which they were last
settled. On the payout schedule (hourly by default), each donor whose units are
now worth more than their mark is settled:

| | |
| --- | --- |
| gain | (NAV − mark) × units |
| paid to the donor | `donor_share` × gain, as USDG, from cash |
| kept by the operator | the rest, as units |
| donor afterwards | exactly their high-water value; mark = NAV |

Both halves move as units, so the transfer itself leaves NAV untouched for
everyone else. Losses are the donor's until a new high is set; nothing is paid
below the mark. A second deposit blends the mark, so new money is never charged
for old gains.

Payouts are deferred when cash would drop below one order's worth — the fly
never sells a position to pay a donor. A payout row is written before signing,
the signed hash recorded before broadcast, and an interrupted payout is
reconciled from its receipt, never re-sent.

## What donors should be told

- Send only from a wallet you control: the share is paid back to the sending
  address, and an exchange hot wallet will not forward it.
- Transfers under `donation_min_usd` ($1) are kept by the operator as a gift.
- Losses are not refunded. You are paid only on gains above your own mark.
- The fly's fills, the pool's NAV and every payout are on the public site.

## Limits that change with a pool

The loss stop becomes the larger of `loss_stop_usd` and
`loss_stop_fraction` × contributed capital, so a $25 stop does not trip on noise
once the pool is $1,000. Per-order size does not change: with a large pool the
fly deploys the same $10 at a time and the rest sits as cash. `max_pool_usd`
(default $1,000) is reported on the site; the chain cannot refuse a transfer, so
money past the cap is still booked and still trades at the same cadence.

## Commands

```sh
python -m stonkflyrh donors --out runs/live      # who holds what, who is owed
```

The DONORS tab on the site shows the same, live.

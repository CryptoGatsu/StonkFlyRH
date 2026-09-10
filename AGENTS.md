# StonkFlyRH

- Preserve the full retained MaleCNS v1.0 graph. No pruning, scripted trades presented as neural output, LLM trading policy, or hidden profit-based action selection.
- Separate market observations, sensory proxies, neural propagation, plasticity, fixed decoding, and execution limits. The risk guard may reject a swap; it must never choose a replacement trade.
- Profit/loss reinforcement is an engineered input to identified dopamine cells. Do not claim modeled pain, pleasure, consciousness, or validated profitable learning.
- Paper execution is the default. Never send a real transaction while testing. Keys, keystores, the contract registry, balances, logs, checkpoints and data stay ignored.
- No contract address is hardcoded. Every address the process will call is verified on chain at preflight: code present, router and quoter agreeing on a factory, token symbol and decimals matching, pool present at the fee tier.
- Use integer wei for on-chain quantities and Decimal for ledger balances. Persist order intent, and record the signed transaction hash before broadcasting it. Unknown outcomes stop execution until reconciliation; never blindly resend a swap.
- Limits are configured in dollars and converted every observation from the chain's ETH/USD reference; the ledger stays in WETH. A stale or implausible price stops the run.
- The rug screen may only withhold a buy. It never proposes a trade, never picks a token, never overrides a HOLD, and never gates a sell. A screen that throws is a rejection.
- A recorded rug adds to a permanent blocklist, lengthens the aversive pulse, and tightens screen thresholds. Describe the tightening as an engineered heuristic, never as the connectome learning.
- Adaptation only removes options: size can shrink, buys can stop, cooldowns can stretch. Nothing adapts a position past the configured cap, and nothing adapts a sell.
- Discovery only changes what the fly may see: new quote-asset pools from the factory, each screened before admission, seeds never dropped, blocklisted symbols never readmitted. It never proposes a trade.
- The website is read-only. It may not import the broker, open the keystore, hold a key, or expose a route that changes run state.
- Keep README short. Detailed model and execution caveats belong in docs.

"""Single-worker run loop. Default execution is paper; live must be explicit."""

import argparse
import dataclasses
import fcntl
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

from .chain import NETWORKS, network
from .config import D, Settings


def build_parser():
    p = argparse.ArgumentParser(prog="stonkflyrh")
    sub = p.add_subparsers(dest="command", required=True)

    prep = sub.add_parser("prepare", help="Download and verify the MaleCNS dataset")
    prep.add_argument("--reuse-doomfly", type=Path)
    sub.add_parser("verify", help="Re-check prepared neural inputs")

    wallet = sub.add_parser("wallet", help="Import or inspect the fly wallet")
    wallet.add_argument("action", choices=["import", "create", "show"])
    wallet.add_argument("--keystore", type=Path)
    wallet.add_argument(
        "--expect",
        help="Override the address an imported key must derive",
    )

    chain = sub.add_parser("chain", help="Check the network and contract registry")
    chain.add_argument("action", choices=["verify"])
    chain.add_argument("--network", choices=sorted(NETWORKS), default=None)
    chain.add_argument("--tokens", type=Path)
    chain.add_argument("--products", nargs="+", default=None)

    screen = sub.add_parser("screen", help="Run the rug screen without trading")
    screen.add_argument("--out", type=Path, default=Path("runs/paper"))
    screen.add_argument("--network", choices=sorted(NETWORKS), default=None)
    screen.add_argument("--tokens", type=Path)
    screen.add_argument("--products", nargs="+", default=None)
    screen.add_argument("--refresh", action="store_true", help="Ignore the cache")

    fees = sub.add_parser("fees", help="Show accrued fees or sweep them out")
    fees.add_argument("--out", type=Path, default=Path("runs/paper"))
    fees.add_argument("--sweep", action="store_true", help="Send outstanding fees")
    fees.add_argument("--dry-run", action="store_true")
    fees.add_argument("--minimum", type=str, default=None)
    fees.add_argument("--tokens", type=Path)

    serve = sub.add_parser("serve", help="Serve the live trade website")
    serve.add_argument("--out", type=Path, default=Path("runs/paper"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)

    preview = sub.add_parser("preview", help="Bake a run into one static page")
    preview.add_argument("--out", type=Path, default=Path("runs/paper"))
    preview.add_argument("--write", type=Path, default=Path("preview.html"))
    preview.add_argument("--at-rest", type=int, default=None,
                         help="Rows shown on load; the rest replay")
    preview.add_argument("--interval-ms", type=int, default=3500)

    start = sub.add_parser(
        "start",
        help="Everything from .env: prepare, verify, import the wallet, preflight, run",
    )
    start.add_argument("--out", type=Path)
    start.add_argument("--steps", type=int, default=0)
    start.add_argument("--preflight-only", action="store_true")

    run = sub.add_parser("run")
    run.add_argument("--live", action="store_true")
    run.add_argument(
        "--preflight-only",
        action="store_true",
        help="Read-only chain checks; never submit a swap",
    )
    run.add_argument(
        "--resume-reviewed",
        action="store_true",
        help="After manual review, clear a transient halt only after successful reconciliation",
    )
    run.add_argument(
        "--fixture", action="store_true", help="Synthetic offline market input; paper only"
    )
    run.add_argument("--steps", type=int, default=0, help="0 keeps running")
    run.add_argument(
        "--fast", action="store_true", help="Skip waiting in paper mode; cooldown still applies"
    )
    run.add_argument(
        "--frozen", action="store_true", help="Freeze all memory efficacies for a control run"
    )
    run.add_argument("--no-screen", action="store_true", help="Disable the rug screen")
    run.add_argument("--no-adapt", action="store_true", help="Disable market adaptation")
    run.add_argument("--no-discovery", action="store_true", help="Trade only the listed tokens")
    run.add_argument("--out", type=Path)
    run.add_argument("--network", choices=sorted(NETWORKS), default=None)
    run.add_argument("--tokens", type=Path)
    run.add_argument(
        "--products",
        nargs="*",
        default=None,
        help="Seed memecoin symbols from the registry; discovery adds more",
    )
    run.add_argument("--capital-usd", default="100")
    run.add_argument("--order-limit-usd", default="10")
    run.add_argument("--neural-ms", type=float, default=500)

    status = sub.add_parser("status")
    status.add_argument("--out", type=Path, default=Path("runs/paper"))
    return p


def env_products(default=("PONS",)):
    raw = os.environ.get("STONKFLYRH_PRODUCTS", "")
    listed = tuple(p.strip().upper() for p in raw.replace(";", ",").split(",") if p.strip())
    return listed or tuple(default)


def settings_from(args, net_key):
    products = tuple(args.products) if args.products else env_products()
    return Settings(
        network=net_key,
        products=products,
        capital_usd=args.capital_usd,
        order_limit_usd=args.order_limit_usd,
        learning=not args.frozen,
        screen_enabled=not args.no_screen,
        adapt_enabled=not args.no_adapt,
        discovery_enabled=not getattr(args, "no_discovery", False),
        neural_ms=args.neural_ms,
        pulse_ms=min(200, args.neural_ms / 2),
    )


def chain_context(net_key, tokens_path, products, default_tier=None):
    from .chain import ChainClient
    from .tokens import Registry

    net = network(net_key)
    registry = Registry.load(net.key, tokens_path)
    client = ChainClient(net)
    tier = default_tier if default_tier is not None else Settings(network=net.key).pool_fee_tier
    verified = registry.verify(client, products, tier)
    return net, client, registry, verified


def run_settings(out):
    """Rebuild a finished run's exact settings from its own ledger."""
    import sqlite3

    path = out / "ledger.sqlite"
    if not path.exists():
        raise SystemExit(f"No ledger at {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
    finally:
        db.close()
    stored = meta.get("settings_full")
    if not stored:
        raise SystemExit("Ledger predates stored settings; re-run to record them")
    stored = dict(stored)
    stored["products"] = tuple(stored["products"])
    return Settings(**stored), meta


def cmd_wallet(a):
    from . import wallet as w

    if a.action == "show":
        print(json.dumps(w.summary(a.keystore), indent=2))
        return
    existing = w.address("trading", a.keystore)
    if existing:
        raise SystemExit(
            f"A fly wallet keystore already holds {existing}. Move it aside deliberately."
        )
    if a.action == "create":
        print(
            "Creating a NEW wallet. To use the fly wallet you already funded, "
            "run `wallet import` instead.",
            file=sys.stderr,
        )
        address = w.create("trading", a.keystore)
    else:
        address = w.import_key(
            w.read_key_interactively(), "trading", a.keystore, expect=a.expect
        )
    print(json.dumps({"imported": address, **w.summary(a.keystore)}, indent=2))
    print(
        "\nBack up the keystore directory and its password. A lost key is lost funds.",
        file=sys.stderr,
    )


def cmd_chain(a):
    products = a.products or list(Settings().products)
    net, client, registry, verified = chain_context(a.network, a.tokens, products)
    print(
        json.dumps(
            {
                "network": {"name": net.name, "chain_id": net.chain_id, "rpc": net.rpc},
                **verified,
            },
            indent=2,
        )
    )


def cmd_screen(a):
    """Ask the rug screen about tokens without placing anything."""
    from .ledger import Ledger
    from .market import RobinhoodChainMarket
    from .pricing import build as build_oracle
    from .safety import RugScreen

    products = a.products or list(Settings().products)
    settings = Settings(network=network(a.network).key, products=tuple(products))
    net, client, registry, verified = chain_context(
        net_key := settings.network, a.tokens, products, settings.pool_fee_tier
    )
    market = RobinhoodChainMarket(settings, client, registry, verified)
    oracle = build_oracle(client, registry, market)
    eth_usd = oracle.eth_usd()
    a.out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(a.out / "screen.sqlite", settings, "screen", capital=D("1"))
    try:
        screen = RugScreen(settings, client, registry, ledger, market.quote_call)
        report = {
            "network": net_key,
            "eth_usd": str(eth_usd),
            "thresholds": screen.thresholds(),
            "verdicts": [
                screen.assess(p, market.pool(p), eth_usd, force=a.refresh).json()
                for p in products
            ],
        }
        print(json.dumps(report, indent=2))
    finally:
        ledger.close()


def cmd_fees(a):
    from .ledger import Ledger

    settings, meta = run_settings(a.out)
    if not a.sweep and not a.dry_run:
        ledger = Ledger(a.out / "ledger.sqlite", settings, meta["mode"])
        try:
            print(json.dumps(ledger.fees.report(), indent=2))
        finally:
            ledger.close()
        return
    if meta["mode"] != "live":
        raise SystemExit("A paper run books fees but holds no tokens to sweep")
    from .payouts import FeeSweeper
    from .wallet import load

    ledger = Ledger(a.out / "ledger.sqlite", settings, meta["mode"])
    try:
        _, client, registry, _ = chain_context(
            settings.network, a.tokens, settings.products, settings.pool_fee_tier
        )
        sweeper = FeeSweeper(settings, ledger, client, registry, load("trading"))
        print(json.dumps(sweeper.sweep(a.minimum, dry_run=a.dry_run), indent=2))
    finally:
        ledger.close()


def cmd_preview(a):
    from .web.preview import write

    target = write(a.out, a.write, at_rest=a.at_rest, interval_ms=a.interval_ms)
    print(json.dumps({"preview": str(target), "from": str(a.out)}, indent=2))


REQUIRED_LIVE_ENV = ("STONKFLYRH_LIVE", "STONKFLYRH_KEYSTORE_PASSWORD")


def cmd_start(a, parser):
    """The one command: read .env, get everything ready, then run.

    Idempotent. Each step is skipped when its result already exists, so the
    same command boots a fresh machine and resumes a running installation.
    """
    from . import wallet as w

    mode = (os.environ.get("STONKFLYRH_MODE") or "paper").lower()
    if mode not in ("paper", "live", "fixture"):
        raise SystemExit("STONKFLYRH_MODE must be paper, live or fixture")
    live = mode == "live"
    fixture = mode == "fixture"
    products = env_products()
    out = a.out or Path(f"runs/{mode}")
    say = lambda step, detail: print(json.dumps({"start": step, **detail}), flush=True)

    # 1. dataset
    from .neural.common import GRAPH

    if not GRAPH.exists():
        say("prepare", {"note": "downloading and compiling the MaleCNS graph; several minutes"})
        from .data import prepare

        prepare(None)
    else:
        say("prepare", {"ready": True})

    # 2. wallet
    if live:
        missing = [k for k in REQUIRED_LIVE_ENV if not os.environ.get(k)]
        if missing:
            raise SystemExit(
                "Live mode needs these in .env: " + ", ".join(missing)
                + " (STONKFLYRH_LIVE must be exactly I_ACCEPT_REAL_ONCHAIN_TRADES)"
            )
        if not w.address("trading"):
            if not os.environ.get("STONKFLYRH_PRIVATE_KEY"):
                raise SystemExit(
                    "No fly wallet keystore yet. Put the fly wallet's private key in .env as "
                    "STONKFLYRH_PRIVATE_KEY for this one start, or run `wallet import`."
                )
            address = w.import_key(os.environ["STONKFLYRH_PRIVATE_KEY"], "trading")
            say("wallet", {"imported": address, "note": "remove STONKFLYRH_PRIVATE_KEY from .env now"})
        else:
            say("wallet", {"fly_wallet": w.address("trading")})
        if os.environ.get("STONKFLYRH_PRIVATE_KEY"):
            print(
                "WARNING: STONKFLYRH_PRIVATE_KEY is still set. The keystore holds the key; "
                "delete it from .env.",
                file=sys.stderr,
            )

    # 3. chain
    if not fixture:
        net, client, registry, verified = chain_context(
            None, None, list(products), Settings(products=products).pool_fee_tier
        )
        say("chain", {"network": net.name, "chain_id": net.chain_id, "router": registry.router,
                      "quote": verified["quote"]["symbol"], "seeds": list(products)})

    # 4. run
    class Args:
        pass

    r = Args()
    r.live = live
    r.preflight_only = a.preflight_only
    r.resume_reviewed = False
    r.fixture = fixture
    r.steps = a.steps
    r.fast = fixture
    r.frozen = os.environ.get("STONKFLYRH_FROZEN") == "1"
    r.no_screen = os.environ.get("STONKFLYRH_SCREEN", "1") != "1"
    r.no_adapt = os.environ.get("STONKFLYRH_ADAPT", "1") != "1"
    r.no_discovery = os.environ.get("STONKFLYRH_DISCOVERY", "1") != "1"
    r.out = out
    r.network = None
    r.tokens = None
    r.products = list(products)
    r.capital_usd = os.environ.get("STONKFLYRH_CAPITAL_USD", "100")
    r.order_limit_usd = os.environ.get("STONKFLYRH_ORDER_USD", "10")
    r.neural_ms = 500
    say("run", {"mode": mode, "out": str(out), "capital_usd": r.capital_usd,
                "order_limit_usd": r.order_limit_usd,
                "site": "python -m stonkflyrh serve --out " + str(out)})
    return cmd_run(r, parser)


def cmd_serve(a):
    from .web.server import serve

    httpd = serve(a.out, a.host, a.port)
    print(
        json.dumps({"serving": str(a.out), "url": f"http://{a.host}:{a.port}"}, indent=2),
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.stopping.set()
        httpd.shutdown()
        print("Stopped.", flush=True)


def cmd_status(a):
    import sqlite3

    db = sqlite3.connect(f"file:{a.out / 'ledger.sqlite'}?mode=ro", uri=True)
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
    fees = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(CAST(gross_wei AS INTEGER)),0) FROM fees"
    ).fetchone()
    blocked = [
        {"product": r[0], "reason": r[2]}
        for r in db.execute("SELECT product,at,reason FROM blocklist ORDER BY at")
    ]
    rugs = db.execute("SELECT COUNT(*) FROM rugs").fetchone()[0]
    db.close()
    print(
        json.dumps(
            {
                **{
                    k: meta.get(k)
                    for k in [
                        "mode",
                        "network",
                        "tick",
                        "cash",
                        "positions",
                        "initial_cash",
                        "gas_spent",
                        "eth_usd",
                        "equity_usd",
                        "loss_streak",
                        "anchor",
                        "halted",
                    ]
                },
                "fees": {"fills_charged": fees[0], "gross_wei": str(fees[1])},
                "rugs": rugs,
                "blocklist": blocked,
            },
            indent=2,
        )
    )


def cmd_run(a, parser):
    if a.live and (a.fixture or a.fast):
        parser.error("Live mode forbids fixtures and fast replay")
    if a.steps < 0:
        parser.error("steps cannot be negative")
    net = network(a.network)
    settings = settings_from(a, net.key)
    out = a.out or Path("runs/live" if a.live else "runs/paper")
    out.mkdir(parents=True, exist_ok=True)
    lock = (out / "worker.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("A worker already owns this run directory")

    from .broker import PaperBroker, RobinhoodChainBroker
    from .ledger import Ledger
    from .market import FixtureMarket, RobinhoodChainMarket
    from .pricing import build as build_oracle
    from .wallet import address as wallet_address

    client = registry = verified = None
    if not a.fixture:
        _, client, registry, verified = chain_context(
            net.key, a.tokens, settings.products, settings.pool_fee_tier
        )
        if (registry.quote_symbol, registry.quote_decimals) != (
            settings.quote_symbol,
            settings.quote_decimals,
        ):
            settings = dataclasses.replace(
                settings,
                quote_symbol=registry.quote_symbol,
                quote_decimals=registry.quote_decimals,
            )
    market = (
        FixtureMarket(settings)
        if a.fixture
        else RobinhoodChainMarket(settings, client, registry, verified)
    )
    oracle = build_oracle(client, registry, market, fixture=a.fixture)
    eth_usd = oracle.eth_usd()
    ledger = Ledger(
        out / "ledger.sqlite",
        settings,
        "live" if a.live else "paper",
        capital=D(settings.capital_usd),
    )
    try:
        if a.live:
            broker = RobinhoodChainBroker.from_env(
                settings, ledger, client, registry, verified
            )
        else:
            broker = PaperBroker(settings, ledger, {"trading": wallet_address("trading")})
        result = broker.preflight(eth_usd)
        print(json.dumps({**result, "eth_usd": str(eth_usd)}), flush=True)
        if a.resume_reviewed:
            if (out / "STOP").exists() or ledger.pending():
                raise RuntimeError(
                    "Remove STOP only after review; unresolved orders cannot resume"
                )
            reason = ledger.get("halted")
            if reason and ("Loss stop" in reason or "fee exceeded" in reason):
                raise RuntimeError("A financial stop cannot be cleared by this flag")
            ledger.put("halted", None)
        if a.preflight_only:
            return
        _loop(a, settings, net, out, ledger, broker, market, oracle, client, registry, verified)
    except KeyboardInterrupt:
        print("Stopped; run state preserved.", flush=True)
    except Exception as e:
        # Never print dependency exception text: it may carry wallet, RPC or
        # request details.
        if not ledger.get("halted"):
            ledger.halt(type(e).__name__)
        frames = traceback.extract_tb(e.__traceback__)
        origin = frames[-1] if frames else None
        internal = origin and Path(origin.filename).is_relative_to(Path(__file__).parent)
        diagnostic = {
            "type": type(e).__name__,
            "reason": str(e)
            if internal
            else "External dependency error; review the RPC endpoint and wallet state.",
            "locations": [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in frames],
        }
        (out / "error.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        print(
            f"Stopped safely: {type(e).__name__}. Inspect local state and reconcile "
            "before restarting.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    finally:
        ledger.close()
        lock.close()


def _loop(a, settings, net, out, ledger, broker, market, oracle, client, registry, verified):
    from PIL import Image

    from .actions import StonkflyRHActions
    from .data import verify
    from .display import market_frame
    from .fees import fee_wallet
    from .neural.controller import FlyController
    from .reinforcement import reinforcement
    from .discovery import FixtureDiscovery, PoolDiscovery
    from .risk import Guard, Veto
    from .safety import RugScreen, RugWatch
    from .wallet import address as wallet_address

    verified_data = verify()
    previous = ledger.get("observation")
    if previous:
        market.history = previous["market_history"]
        if a.fixture:
            market.tick = previous["fixture_tick"]
    controller = FlyController(settings)
    cp = ledger.get("checkpoint")
    if cp:
        path = out / cp["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != cp["sha256"]:
            raise RuntimeError("Checkpoint integrity mismatch")
        controller.restore(path)

    screen = (
        RugScreen(settings, client, registry, ledger, market.quote_call)
        if settings.screen_enabled and not a.fixture
        else None
    )
    watch = RugWatch(settings, ledger, screen)
    if a.fixture:
        discovery = FixtureDiscovery(settings, ledger, market) if settings.discovery_enabled else None
    else:
        # Seeds get their addresses and pools from the registry; discovered
        # tokens from earlier in this run come back from the ledger.
        ledger.seed_universe(registry, verified, time.time())
        for symbol, entry in ledger.universe().items():
            if entry.get("source") != "seed" and entry.get("address"):
                registry.add_token(entry)
                market.add_product(symbol, entry["pool"], entry["pool_fee"])
        discovery = (
            PoolDiscovery(settings, client, registry, ledger, market, screen, verified["factory"])
            if settings.discovery_enabled and screen is not None
            else None
        )

    provenance = {
        "settings": dataclasses.asdict(settings),
        "dataset": verified_data,
        "circuit": controller.brain.circuit["report"],
        "vision": controller.brain.visual_report,
        "mode": broker.mode,
        "feed": market.report()["feed"],
        "network": {
            "key": net.key,
            "name": net.name,
            "chain_id": net.chain_id,
            "explorer": net.explorer,
        },
        "contracts": verified or {"note": "fixture run; no chain contracts used"},
        "usd_reference": oracle.report(),
        "wallets": {"fly": wallet_address("trading"), "fee": fee_wallet()},
        "screen": {"enabled": screen is not None},
        "discovery": {
            "enabled": discovery is not None,
            "interval_seconds": settings.discovery_interval_seconds,
            "max_products": settings.max_products,
        },
        "decoder": "DNp20 mean R-L: buy/sell; DNpe017 spike gate; otherwise hold. "
        "Engineered fixed mapping.",
        "learning_validated": False,
        "pain_receptors_modeled": False,
        "timing": "Each observation advances configured neural_ms regardless of wall-market "
        "time; no claim of real-time fly physiology.",
        "source_sha256": {
            str(path.relative_to(Path(__file__).parent)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in Path(__file__).parent.rglob("*")
            if path.suffix in (".py", ".cpp")
        },
    }
    signature = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
    if ledger.get("provenance_sha256") not in (None, signature):
        raise RuntimeError(
            "Run source/protocol changed; use a separate run directory or explicitly "
            "review the migration"
        )
    ledger.put("provenance_sha256", signature)
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")

    guard = Guard(settings, ledger, out / "STOP", screen)
    provider = StonkflyRHActions(guard, broker, net.key)
    action = provider.get_actions()[0]
    count = 0
    while not a.steps or count < a.steps:
        started = time.monotonic()
        if (out / "STOP").exists() or ledger.get("halted"):
            break
        broker.reconcile()
        broker.verify_balances()
        eth_usd = oracle.eth_usd()
        ledger.put("eth_usd", str(eth_usd))
        limits = guard.limits(eth_usd)
        scan = None
        if discovery is not None and discovery.due(time.time()):
            # Discovery only ever changes what the fly may see; it runs before
            # the snapshot so a new token is priced on the tick it joins.
            scan = discovery.scan(time.time(), eth_usd)
            dropped = discovery.prune(time.time(), eth_usd)
            if dropped:
                scan["dropped"] = dropped
            if scan.get("added") or dropped:
                print(json.dumps({"discovery": scan}), flush=True)
        market.products = ledger.products()
        quotes = market.snapshot(limits["order_limit"])
        guard.check(quotes, time.time(), eth_usd)
        market.record(quotes)
        universe = ledger.products()
        product = universe[ledger.get("tick") % len(universe)]
        q = quotes[product]
        equity = ledger.equity(quotes, eth_usd)
        ledger.put("equity_usd", str(equity))

        # A rug is checked before reinforcement so its longer aversive pulse
        # replaces, rather than follows, this observation's ordinary loss pulse.
        rug = watch.inspect(product, q)
        kind, delta = reinforcement(equity, ledger.get("anchor"), limits["reward_deadband"])
        if rug:
            kind = "aversive"
        pulse_ms = watch.pulse_ms() if rug else None
        ledger.put(
            "loss_streak",
            0 if kind == "reward" else int(ledger.get("loss_streak") or 0) + (kind == "aversive"),
        )

        frame = market_frame(
            product, market.history[product], q.bid, q.ask, settings.quote_symbol
        )
        neural = controller.observe(frame, kind, pulse_ms=pulse_ms)
        slot = ledger.get("tick") % 2
        checkpoint = out / f"brain-{slot}.npz"
        controller.save(checkpoint)
        checkpoint_info = {
            "file": checkpoint.name,
            "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        }
        observation = {
            "neural": neural,
            "product": product,
            "quote": q.json(),
            "pnl_delta_usd": str(delta),
            "market_history": market.history,
            "fixture_tick": getattr(market, "tick", None),
        }
        ledger.commit_tick(equity, checkpoint_info, observation)

        order = {"status": "HOLD"}
        screen_json = None
        if neural["side"] != "HOLD":
            try:
                fresh = market.snapshot(limits["order_limit"])
                latest = fresh[product]
                tolerance = guard.move_tolerance(product, neural["side"])
                if abs(latest.bid - q.bid) / q.bid > tolerance:
                    raise Veto("Price moved beyond neural observation tolerance")
                provider.quotes = fresh
                provider.eth_usd = eth_usd
                provider.history = market.history
                provider.pools = {p: market.pool(p) for p in universe}
                provider.gas_price_wei = (
                    client.gas_price()
                    if client is not None
                    else int(D(settings.paper_gas_price_gwei) * D(10**9))
                )
                order = action.invoke({"product": product, "side": neural["side"]})
                if neural["side"] == "BUY" and order.get("status") in ("FILLED", "SETTLED"):
                    watch.record_entry(product, latest.ask)
            except Veto as e:
                order = {"status": "VETO", "reason": str(e)}
        if screen is not None:
            screen_json = ledger.screen_raw(product)

        row = {
            "tick": ledger.get("tick"),
            "wall_time": time.time(),
            "product": product,
            "mode": broker.mode,
            "network": net.key,
            "eth_usd": str(eth_usd),
            "quote": q.json(),
            "quote_symbol": settings.quote_symbol,
            "equity_usd": str(equity),
            "notional_usd": str(limits["order_limit"]),
            "pnl_delta_usd": str(delta),
            "size_scale": str(guard.size_scale(market.history[product])[0])
            if settings.adapt_enabled
            else "1",
            "neural": neural,
            "screen": screen_json,
            "rug": rug,
            "discovery": scan,
            "execution": order,
        }
        with (out / "events.jsonl").open("a") as f:
            f.write(json.dumps(row, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        Image.fromarray(frame).save(out / "latest-input.png")
        (out / "latest.json").write_text(json.dumps(row, indent=2) + "\n")

        print(
            json.dumps(
                {
                    "tick": row["tick"],
                    "side": neural["side"],
                    "execution": order["status"],
                    "equity_usd": row["equity_usd"],
                    "eth_usd": row["eth_usd"],
                    "stimulus": kind,
                    "rug": bool(rug),
                    "plastic_edges_changed": neural["memory"]["changed_edges"],
                }
            ),
            flush=True,
        )
        count += 1
        if not a.fast and (not a.steps or count < a.steps):
            until = started + settings.interval_seconds
            while time.monotonic() < until and not (out / "STOP").exists():
                time.sleep(min(1, until - time.monotonic()))


def main():
    parser = build_parser()
    a = parser.parse_args()
    from dotenv import load_dotenv

    # Never search parent projects for unrelated credentials.
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    if a.command in ("prepare", "verify"):
        from .data import prepare, verify

        if a.command == "prepare":
            prepare(a.reuse_doomfly)
        else:
            print(json.dumps(verify()))
        return
    if a.command == "start":
        try:
            return cmd_start(a, parser)
        except RuntimeError as e:
            raise SystemExit(str(e)) from None
    setup = {
        "wallet": cmd_wallet,
        "chain": cmd_chain,
        "screen": cmd_screen,
        "fees": cmd_fees,
        "preview": cmd_preview,
        "serve": cmd_serve,
        "status": cmd_status,
    }.get(a.command)
    if setup:
        try:
            return setup(a)
        except RuntimeError as e:
            # Operator-facing conditions with an actionable message; a traceback
            # would only bury it.
            raise SystemExit(str(e)) from None
    return cmd_run(a, parser)


if __name__ == "__main__":
    main()

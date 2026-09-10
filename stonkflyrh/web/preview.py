"""Bake a finished run into one static page that replays like the live site.

The preview is the same `index.html` the worker serves, with the run's state,
trade rows, posts and sensory frame embedded, so what someone previews is what
the live page renders. It holds no key, no RPC endpoint and no route, and it
says on its face that it is a replay.
"""

import base64
import json
import time
from pathlib import Path

from .server import STATIC, read_trades, snapshot


def build(out, at_rest=None, interval_ms=3500, banner=None):
    out = Path(out)
    from ..social import read_posts

    trades = read_trades(out)
    if not trades:
        raise RuntimeError(f"No trades in {out / 'events.jsonl'}; nothing to preview")
    at_rest = max(1, len(trades) // 2) if at_rest is None else max(0, min(at_rest, len(trades)))
    posts = read_posts(out, limit=200)
    cutoff = trades[at_rest - 1]["wall_time"] if at_rest else 0
    frame = out / "latest-input.png"
    frame_uri = (
        "data:image/png;base64," + base64.b64encode(frame.read_bytes()).decode()
        if frame.exists()
        else ""
    )
    state = snapshot(out)
    payload = {
        "generated": time.time(),
        "state": state,
        "trades": trades,
        "posts": posts,
        "at_rest": at_rest,
        "posts_at_rest": sum(1 for p in posts if p["at"] <= cutoff),
        "interval_ms": int(interval_ms),
        "frame": frame_uri,
        "banner": banner
        or (
            "<b>Preview.</b> A replay of a simulated paper run against fictional "
            "tokens — the page looks and moves like this while the fly trades. "
            "Nothing here is a real position."
        ),
    }
    html = (STATIC / "index.html").read_text()
    inject = (
        "<script>window.STONKFLY_PREVIEW = "
        + json.dumps(payload, allow_nan=False).replace("</", "<\\/")
        + ";</script>\n<script>"
    )
    marker = "<script>\n\"use strict\";"
    if marker not in html:
        raise RuntimeError("index.html changed shape; preview injection point missing")
    return html.replace(marker, inject + "\n\"use strict\";", 1)


def write(out, target, **kwargs):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build(out, **kwargs))
    return target

"""The static preview is the live page with a run embedded."""

import json

import pytest

from stonkflyrh.web.preview import build, write
from tests.test_web import write_run


def test_a_preview_embeds_the_run_and_marks_itself(tmp_path):
    write_run(tmp_path)
    html = build(tmp_path, at_rest=2)
    assert "window.STONKFLY_PREVIEW" in html
    start = html.index("window.STONKFLY_PREVIEW = ") + len("window.STONKFLY_PREVIEW = ")
    payload = json.loads(html[start : html.index(";</script>", start)])
    assert [t["tick"] for t in payload["trades"]] == [1, 2, 3]
    assert payload["at_rest"] == 2
    assert payload["frame"].startswith("data:image/png;base64,")
    assert "Preview" in payload["banner"]
    assert payload["state"]["meta"]["mode"] == "paper"


def test_a_preview_never_carries_a_route_or_key(tmp_path):
    write_run(tmp_path)
    (tmp_path / "keystore").mkdir()
    (tmp_path / "keystore" / "trading.json").write_text('{"crypto": "SECRET-MATERIAL"}')
    html = build(tmp_path)
    assert "SECRET-MATERIAL" not in html
    assert "ledger.sqlite" not in html


def test_an_empty_run_has_nothing_to_preview(tmp_path):
    with pytest.raises(RuntimeError, match="nothing to preview"):
        build(tmp_path)


def test_write_creates_the_target(tmp_path):
    write_run(tmp_path)
    target = write(tmp_path, tmp_path / "site" / "preview.html")
    assert target.exists() and target.stat().st_size > 10000

"""Wallet creation. No key here ever leaves the temporary directory."""

import json
import os
import stat

import pytest

from stonkflyrh import wallet

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def password(monkeypatch):
    monkeypatch.setenv(wallet.PASSWORD_ENV, PASSWORD)


def test_creates_both_wallets_with_distinct_addresses(tmp_path):
    trading = wallet.create("trading", tmp_path)
    fee = wallet.create("fee", tmp_path)
    assert trading != fee
    assert trading.startswith("0x") and len(trading) == 42
    assert wallet.address("trading", tmp_path) == trading
    assert wallet.address("fee", tmp_path) == fee


def test_keystore_is_owner_only(tmp_path):
    wallet.create("fee", tmp_path)
    mode = stat.S_IMODE(wallet.keystore_path("fee", tmp_path).stat().st_mode)
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_keystore_holds_no_plaintext_key(tmp_path):
    address = wallet.create("trading", tmp_path)
    body = wallet.keystore_path("trading", tmp_path).read_text()
    stored = json.loads(body)
    assert stored["version"] == 3
    assert "crypto" in stored or "Crypto" in stored
    account = wallet.load("trading", tmp_path)
    assert account.address == address
    assert account.key.hex() not in body


def test_wrong_password_cannot_decrypt(tmp_path, monkeypatch):
    wallet.create("trading", tmp_path)
    monkeypatch.setenv(wallet.PASSWORD_ENV, "not-the-password")
    with pytest.raises(ValueError):
        wallet.load("trading", tmp_path)


def test_refuses_to_overwrite_an_existing_keystore(tmp_path):
    wallet.create("fee", tmp_path)
    with pytest.raises(RuntimeError, match="already exists"):
        wallet.create("fee", tmp_path)


def test_refuses_a_group_readable_keystore(tmp_path):
    wallet.create("fee", tmp_path)
    path = wallet.keystore_path("fee", tmp_path)
    os.chmod(path, 0o640)
    with pytest.raises(RuntimeError, match="group/world readable"):
        wallet.load("fee", tmp_path)


def test_unknown_role_is_refused(tmp_path):
    with pytest.raises(ValueError):
        wallet.keystore_path("treasury", tmp_path)


def test_missing_keystore_reports_the_create_command(tmp_path):
    assert wallet.address("trading", tmp_path) is None
    with pytest.raises(RuntimeError, match="wallet create"):
        wallet.load("trading", tmp_path)


def test_summary_states_the_twenty_eighty_split(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_DEV_WALLET", "0x" + "ab" * 20)
    wallet.create("trading", tmp_path)
    wallet.create("fee", tmp_path)
    summary = wallet.summary(tmp_path)
    assert summary["dev_share_percent"] == 20.0
    assert summary["treasury_share_percent"] == 80.0
    assert summary["development"].lower() == "0x" + "ab" * 20
    assert summary["trading"] and summary["fee"]

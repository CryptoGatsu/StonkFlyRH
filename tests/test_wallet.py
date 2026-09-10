"""Importing the fly wallet. No key here ever leaves the temporary directory."""

import json
import os
import stat

import pytest
from eth_account import Account

from stonkflyrh import wallet

PASSWORD = "correct-horse-battery"
# A fixed key, so the expected-address check is testable both ways.
KEY = "0x" + "11" * 32
ADDRESS = Account.from_key(bytes.fromhex("11" * 32)).address


@pytest.fixture(autouse=True)
def password(monkeypatch):
    monkeypatch.setenv(wallet.PASSWORD_ENV, PASSWORD)


def test_the_fly_wallet_default_is_the_operators_address(monkeypatch):
    monkeypatch.delenv("STONKFLYRH_FLY_WALLET", raising=False)
    assert wallet.expected_address().lower() == "0x68e82397455232f6f726e44ad1c980c6c99b3201"


def test_importing_the_expected_key_writes_a_keystore(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_FLY_WALLET", ADDRESS)
    assert wallet.import_key(KEY, path=tmp_path) == ADDRESS
    assert wallet.address("trading", tmp_path) == ADDRESS


def test_importing_a_key_for_another_address_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_FLY_WALLET", "0x" + "ab" * 20)
    with pytest.raises(RuntimeError, match="not the expected fly wallet"):
        wallet.import_key(KEY, path=tmp_path)
    # Nothing is written on a refusal.
    assert wallet.address("trading", tmp_path) is None


def test_the_expected_address_can_be_overridden_per_call(tmp_path):
    assert wallet.import_key(KEY, path=tmp_path, expect=ADDRESS) == ADDRESS


@pytest.mark.parametrize("bad", ["", "0x1234", "zz" * 32, "11" * 31])
def test_a_malformed_key_is_refused(tmp_path, bad):
    with pytest.raises(RuntimeError, match="64 hex characters"):
        wallet.import_key(bad, path=tmp_path, expect=None)


def test_a_key_without_the_0x_prefix_works(tmp_path):
    assert wallet.import_key("11" * 32, path=tmp_path, expect=ADDRESS) == ADDRESS


def test_keystore_is_owner_only(tmp_path):
    wallet.import_key(KEY, path=tmp_path, expect=ADDRESS)
    mode = stat.S_IMODE(wallet.keystore_path("trading", tmp_path).stat().st_mode)
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_keystore_holds_no_plaintext_key(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_FLY_WALLET", ADDRESS)
    wallet.import_key(KEY, path=tmp_path)
    body = wallet.keystore_path("trading", tmp_path).read_text()
    stored = json.loads(body)
    assert stored["version"] == 3
    assert "11" * 32 not in body
    account = wallet.load("trading", tmp_path)
    assert account.address == ADDRESS


def test_wrong_password_cannot_decrypt(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_FLY_WALLET", ADDRESS)
    wallet.import_key(KEY, path=tmp_path)
    monkeypatch.setenv(wallet.PASSWORD_ENV, "not-the-password")
    with pytest.raises(ValueError):
        wallet.load("trading", tmp_path)


def test_loading_a_keystore_for_the_wrong_wallet_is_refused(tmp_path, monkeypatch):
    wallet.import_key(KEY, path=tmp_path, expect=ADDRESS)
    monkeypatch.setenv("STONKFLYRH_FLY_WALLET", "0x" + "ab" * 20)
    with pytest.raises(RuntimeError, match="not the expected fly wallet"):
        wallet.load("trading", tmp_path)


def test_refuses_to_overwrite_an_existing_keystore(tmp_path):
    wallet.import_key(KEY, path=tmp_path, expect=ADDRESS)
    with pytest.raises(RuntimeError, match="already exists"):
        wallet.import_key(KEY, path=tmp_path, expect=ADDRESS)


def test_refuses_a_group_readable_keystore(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_FLY_WALLET", ADDRESS)
    wallet.import_key(KEY, path=tmp_path)
    os.chmod(wallet.keystore_path("trading", tmp_path), 0o640)
    with pytest.raises(RuntimeError, match="group/world readable"):
        wallet.load("trading", tmp_path)


def test_unknown_role_is_refused(tmp_path):
    with pytest.raises(ValueError):
        wallet.keystore_path("fee", tmp_path)


def test_missing_keystore_reports_the_import_command(tmp_path):
    assert wallet.address("trading", tmp_path) is None
    with pytest.raises(RuntimeError, match="wallet import"):
        wallet.load("trading", tmp_path)


def test_summary_lists_both_wallets(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_FLY_WALLET", ADDRESS)
    monkeypatch.setenv("STONKFLYRH_FEE_WALLET", "0x" + "ab" * 20)
    wallet.import_key(KEY, path=tmp_path)
    summary = wallet.summary(tmp_path)
    assert summary["fly_wallet"] == ADDRESS
    assert summary["fly_wallet_expected"] == ADDRESS
    assert summary["fee_wallet"].lower() == "0x" + "ab" * 20

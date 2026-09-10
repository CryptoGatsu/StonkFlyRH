"""Local wallet creation and loading for Robinhood Chain.

Two wallets, created by `python -m stonkflyrh wallet create`:

    trading  holds the run's capital and signs swaps
    fee      receives the treasury share of protocol fees

The development share goes to the configured development wallet, which this
process only ever sends to; it holds no key for it.

Keys are generated locally with os.urandom by way of eth-account, written as
Web3 Secret Storage keystores encrypted with a password this process never
stores, and chmod 0600. Nothing here writes a private key to a log, a ledger,
an event row or the website. The keystore directory is git-ignored.
"""

import getpass
import json
import os
import stat
from pathlib import Path

KEYSTORE_ENV = "STONKFLYRH_KEYSTORE"
PASSWORD_ENV = "STONKFLYRH_KEYSTORE_PASSWORD"
ROLES = ("trading", "fee")


def keystore_dir(path=None):
    return Path(path or os.environ.get(KEYSTORE_ENV) or "keystore").resolve()


def keystore_path(role, path=None):
    if role not in ROLES:
        raise ValueError("Unknown wallet role: " + str(role))
    return keystore_dir(path) / f"{role}.json"


def _password(role, confirm=False):
    supplied = os.environ.get(PASSWORD_ENV)
    if supplied:
        return supplied
    if not os.isatty(0):
        raise RuntimeError(
            f"Set {PASSWORD_ENV} or run interactively; refusing to use an empty password"
        )
    while True:
        value = getpass.getpass(f"Password for the {role} keystore: ")
        if len(value) < 8:
            print("Use at least 8 characters.")
            continue
        if not confirm or value == getpass.getpass("Confirm: "):
            return value
        print("Passwords did not match.")


def create(role, path=None, overwrite=False):
    """Generate one wallet. Refuses to clobber an existing keystore."""
    from eth_account import Account

    target = keystore_path(role, path)
    if target.exists() and not overwrite:
        raise RuntimeError(
            f"{target} already exists. Move it aside deliberately; a lost key is lost funds."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, stat.S_IRWXU)
    account = Account.create()
    encrypted = Account.encrypt(account.key, _password(role, confirm=True))
    tmp = target.with_suffix(".partial")
    tmp.write_text(json.dumps(encrypted) + "\n")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(target)
    return account.address


def address(role, path=None):
    """Read the address out of a keystore without decrypting the key."""
    target = keystore_path(role, path)
    if not target.exists():
        return None
    from eth_utils import to_checksum_address

    return to_checksum_address("0x" + json.loads(target.read_text())["address"])


def load(role, path=None):
    """Decrypt one wallet. Only the trading wallet ever needs this to trade."""
    from eth_account import Account

    target = keystore_path(role, path)
    if not target.exists():
        raise RuntimeError(
            f"No {role} keystore at {target}. Run: python -m stonkflyrh wallet create"
        )
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(f"{target} is group/world readable; chmod 600 it")
    key = Account.decrypt(json.loads(target.read_text()), _password(role))
    return Account.from_key(key)


def summary(path=None):
    from .fees import DEV_SHARE_BPS, dev_wallet

    return {
        "keystore": str(keystore_dir(path)),
        "trading": address("trading", path),
        "fee": address("fee", path),
        "development": dev_wallet(),
        "dev_share_percent": DEV_SHARE_BPS / 100,
        "treasury_share_percent": (10000 - DEV_SHARE_BPS) / 100,
    }

"""The fly's wallet: importing it, holding it, and refusing the wrong one.

One key lives here — the fly wallet, which holds the run's WETH and gas ETH and
signs every swap. It is the wallet the operator funds and launches the coin
from, so the usual flow is `wallet import`, pasting the existing key, rather
than generating a new one.

An imported key must derive the address this fork expects. A private key pasted
into the wrong terminal is how funds are lost quietly; here it fails loudly.

The fee wallet needs no key. This process only ever sends to it.

Keys are written as Web3 Secret Storage keystores encrypted with a password
this process never stores, and chmod 0600. Nothing here writes a private key to
a log, a ledger, an event row, a post or the website. The keystore directory is
git-ignored.
"""

import getpass
import json
import os
import stat

KEYSTORE_ENV = "STONKFLYRH_KEYSTORE"
PASSWORD_ENV = "STONKFLYRH_KEYSTORE_PASSWORD"
ROLES = ("trading", "deployer")

# The fly wallet. An imported key must derive this address unless the operator
# overrides it with STONKFLYRH_FLY_WALLET.
FLY_WALLET = "0x68e82397455232f6F726E44ad1c980C6C99B3201"
# The deployer wallet: holds the operator's coin and signs airdrops, nothing
# else. STONKFLYRH_DEPLOYER_WALLET overrides it.
DEPLOYER_WALLET = "0x89a813e1Eb38d38EEBd1Fb91EdD464E6fCC22f25"

EXPECTED = {
    "trading": ("STONKFLYRH_FLY_WALLET", FLY_WALLET),
    "deployer": ("STONKFLYRH_DEPLOYER_WALLET", DEPLOYER_WALLET),
}


def expected_address(role="trading"):
    env, default = EXPECTED[role]
    configured = os.environ.get(env) or default
    if not configured:
        return None
    from .chain import checksum

    return checksum(configured)


def keystore_dir(path=None):
    from .paths import resolve

    return resolve(path or os.environ.get(KEYSTORE_ENV) or "keystore")


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


def _write(account, role, path, overwrite):
    from eth_account import Account

    target = keystore_path(role, path)
    if target.exists() and not overwrite:
        raise RuntimeError(
            f"{target} already exists. Move it aside deliberately; a lost key is lost funds."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, stat.S_IRWXU)
    encrypted = Account.encrypt(account.key, _password(role, confirm=True))
    tmp = target.with_suffix(".partial")
    tmp.write_text(json.dumps(encrypted) + "\n")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(target)
    return account.address


def create(role="trading", path=None, overwrite=False):
    """Generate a fresh wallet. Use `import_key` for an existing fly wallet."""
    from eth_account import Account

    return _write(Account.create(), role, path, overwrite)


def import_key(private_key, role="trading", path=None, overwrite=False, expect=None):
    """Encrypt an existing key, refusing one that is not the fly wallet."""
    from eth_account import Account
    from eth_utils import to_checksum_address

    key = private_key.strip()
    if key.startswith("0x"):
        key = key[2:]
    if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
        raise RuntimeError("A private key is 64 hex characters, optionally 0x-prefixed")
    account = Account.from_key(bytes.fromhex(key))
    want = expect if expect is not None else expected_address(role)
    if want and to_checksum_address(account.address) != to_checksum_address(want):
        raise RuntimeError(
            f"That key derives {account.address}, not the expected "
            f"{'fly' if role == 'trading' else role} wallet {want}. "
            "Nothing was written."
        )
    return _write(account, role, path, overwrite)


KEY_ENV = {"trading": "STONKFLYRH_PRIVATE_KEY", "deployer": "STONKFLYRH_DEPLOYER_PRIVATE_KEY"}


def read_key_interactively(role="trading"):
    env = KEY_ENV[role]
    if os.environ.get(env):
        return os.environ[env]
    if not os.isatty(0):
        raise RuntimeError(f"Paste the key interactively, or set {env} for this one command")
    return getpass.getpass(f"{role.capitalize()} wallet private key (not echoed): ")


def address(role="trading", path=None):
    """Read the address out of a keystore without decrypting the key."""
    target = keystore_path(role, path)
    if not target.exists():
        return None
    from eth_utils import to_checksum_address

    return to_checksum_address("0x" + json.loads(target.read_text())["address"])


def load(role="trading", path=None):
    from eth_account import Account

    target = keystore_path(role, path)
    if not target.exists():
        raise RuntimeError(
            f"No {role} keystore at {target}. Run: python -m stonkflyrh wallet import"
        )
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(f"{target} is group/world readable; chmod 600 it")
    key = Account.decrypt(json.loads(target.read_text()), _password(role))
    account = Account.from_key(key)
    want = expected_address(role)
    if want and account.address != want:
        raise RuntimeError(
            f"Keystore holds {account.address}, not the expected "
            f"{'fly' if role == 'trading' else role} wallet {want}"
        )
    return account


def summary(path=None):
    from .fees import fee_wallet

    return {
        "keystore": str(keystore_dir(path)),
        "fly_wallet": address("trading", path),
        "fly_wallet_expected": expected_address(),
        "deployer_wallet": address("deployer", path),
        "deployer_wallet_expected": expected_address("deployer"),
        "fee_wallet": fee_wallet(),
    }

"""Where the install's own files are, whatever the current directory is.

The systemd units run from the install directory; an operator's shell usually
does not. A relative path (tokens.json, keystore, runs/live) is taken from the
current directory when it exists there, and from the install directory — the
one that holds this package — otherwise.
"""

import os
from pathlib import Path


def project_root():
    return Path(__file__).resolve().parent.parent


def resolve(path):
    p = Path(path)
    if p.is_absolute():
        return p
    try:
        local = Path.cwd() / p
        if local.exists():
            return local.resolve()
    except (OSError, PermissionError):
        pass
    return (project_root() / p).resolve()


def env_file():
    """The .env to load: the current directory's if it has one, else the install's."""
    for base in (_cwd(), project_root()):
        if base is None:
            continue
        candidate = base / ".env"
        try:
            if candidate.exists():
                return candidate
        except (OSError, PermissionError):
            continue
    return project_root() / ".env"


def _cwd():
    try:
        return Path(os.getcwd())
    except OSError:
        return None

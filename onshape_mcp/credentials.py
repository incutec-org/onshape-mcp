"""Credential lookup for the Onshape API keys.

Order, first match wins per variable:

1. the process environment;
2. ``.env`` at the repository root (the directory above this package);
3. a per-user credentials file: ``$INCUTEC_CREDENTIALS_FILE`` when set,
   otherwise ``~/.config/incutec/credentials.env``.

Values are never logged. A missing file is skipped silently.
"""

import os
from pathlib import Path
from typing import Iterable, Optional

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDENTIALS_FILE = Path.home() / ".config" / "incutec" / "credentials.env"


def credentials_file() -> Path:
    """The per-user credentials file, honouring ``INCUTEC_CREDENTIALS_FILE``."""
    override = os.environ.get("INCUTEC_CREDENTIALS_FILE")
    return Path(override).expanduser() if override else DEFAULT_CREDENTIALS_FILE


def env_files(repo_root: Optional[Path] = None) -> Iterable[Path]:
    """Files read after the environment, highest precedence first."""
    return [(repo_root or REPO_ROOT) / ".env", credentials_file()]


def load_credentials(repo_root: Optional[Path] = None) -> None:
    """Fill unset variables from the repository .env, then the credentials file.

    ``load_dotenv(override=False)`` never replaces a variable that is already
    set, so loading the files in precedence order gives environment first,
    then the repository .env, then the credentials file.
    """
    for path in env_files(repo_root):
        if path.is_file():
            load_dotenv(path, override=False)

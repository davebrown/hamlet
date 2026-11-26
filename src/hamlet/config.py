"""Local configuration without executing shell code or changing process state."""

import os
from pathlib import Path

from dotenv import dotenv_values


def read_environment(path=None, *, environ=None):
    path = Path.cwd() / ".env" if path is None else Path(path).expanduser()
    values = dotenv_values(path, interpolate=False) if path.is_file() else {}
    return {
        **{name: value for name, value in values.items() if value is not None},
        **(os.environ if environ is None else environ),
    }

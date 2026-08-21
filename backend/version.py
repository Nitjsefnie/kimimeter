"""The repo version, read from the root VERSION file.

A module of its own rather than a corner of another one: VERSION is the
input to the release machinery (`.github/workflows/release.yml` tags when
it changes, `speed.yml` benchmarks against the release it names), and
every other consumer — /health, the smoke test, the tests — needs to read
it without importing anything that has side effects at import time.

(claudit keeps the same reader in backend/constants.py, which exists
there to break an import cycle. There is no such module here, and adding
one just to hold this would be borrowing a rationale that does not apply.)
"""
from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    """Repo version, or "unknown".

    Read once at import — it cannot change under a running process
    without a redeploy.

    A deploy that omits the file (a tarball, a partial checkout) gets
    "unknown" rather than a crash: /health reporting an unknown version is
    strictly better than /health not answering at all.
    """
    try:
        text = (Path(__file__).resolve().parent.parent / "VERSION").read_text(
            encoding="utf-8"
        )
    except OSError:
        return "unknown"
    return text.strip() or "unknown"


VERSION = _read_version()

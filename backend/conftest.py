"""Never let a test run alter the live journal.

    cd backend && python -m pytest -q

Two layers, because the two failure modes are different.

1. **Prevention.** `database.DB_PATH` defaults to a *relative* `trading_journal.db`,
   so a module that calls `init_db()` without first repointing `DB_PATH` opens
   whatever journal happens to sit next to `backend/`. Setting `DATABASE_PATH`
   to a throwaway file here means the default never resolves to the real one —
   a test only touches the live journal if it explicitly opts in, and none do.

2. **Detection.** Prevention covers the default; a test that hardcodes a path
   would still slip through. The hash is taken before and after the session and
   the run fails loudly on any difference, so a mutation cannot pass CI looking
   like a green suite.

The live journal is the user's own trading history. A suite that mutates it and
reports success is worse than a suite that fails.
"""
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
LIVE_JOURNAL = BACKEND / "trading_journal.db"


def _md5(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pytest_configure(config):
    # Before any test module imports `database` (which reads this at import
    # time). An explicit value set by a test still wins — setdefault only
    # supplies the fallback that would otherwise be the relative path.
    os.environ.setdefault("DATABASE_PATH", str(Path(tempfile.mkdtemp()) / "pytest-journal.db"))


@pytest.fixture(scope="session", autouse=True)
def live_journal_unchanged():
    """Fail the session if any test wrote to the user's real database."""
    before = _md5(LIVE_JOURNAL)
    yield
    after = _md5(LIVE_JOURNAL)
    if before == after:
        return
    if before is None:
        raise RuntimeError(
            f"{LIVE_JOURNAL.name} was created by this test run — a test wrote "
            "outside its own temp database. Fix the test; do not commit the file."
        )
    raise RuntimeError(
        f"The live journal changed during this test run: {before} -> {after}. "
        "No test may open it. Find the one that did before trusting anything else here."
    )

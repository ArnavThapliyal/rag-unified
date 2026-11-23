"""QA harness: import the pipeline from an isolated sandbox copy (QA_SANDBOX), never from the repo.
Every script derives ROOT from its own __file__, so all writes land inside the sandbox."""
import os
import sys
from pathlib import Path

SB = Path(os.environ["QA_SANDBOX"]).resolve()
REPO = Path(__file__).resolve().parent.parent
assert not SB.is_relative_to(REPO) and (SB / "scripts").is_dir(), "QA_SANDBOX must be a make_sandbox.sh copy outside the repo"
sys.path.insert(0, str(SB / "scripts"))

import common  # noqa: E402

assert common.ROOT == SB, (common.ROOT, SB)
# never pop macOS notifications from tests
common.notify = lambda *a, **k: None

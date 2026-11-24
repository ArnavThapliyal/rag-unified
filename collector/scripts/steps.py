"""Step CLI entry point in scripts/ (same as `python -m rag_collector.steps`), so the agent's tool guard grammar
(`.venv/bin/python scripts/<script>.py ...`) covers it. See src/rag_collector/steps.py for the steps.

The agent may run only `steps.py list`, `steps.py status` and `steps.py audit` (fast, read-only). Long steps
(discover/fetch/process/drain/collect) go through scripts/job.py; universe-sync and export are run by
scripts/run_continuous.sh after each collection segment, never by the agent.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rag_collector.steps import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

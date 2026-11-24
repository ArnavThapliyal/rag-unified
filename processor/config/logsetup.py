"""
Logging for the main process and every child process.

Children are started with 'spawn', so they inherit no handlers; each one calls
setup_logging() itself. All processes append to the same LOG_DIR/pipeline.log
and write to stdout, with the process name and run id on every line.
"""

import logging
import sys
import time
from typing import Optional

from config.settings import LOG_DIR


def setup_logging(run_id: Optional[str] = None, level: int = logging.INFO) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    run = f" [run {run_id}]" if run_id else ""
    formatter = logging.Formatter(f"[%(asctime)s] [%(levelname)s]{run} [%(processName)s] [%(name)s] %(message)s")
    formatter.converter = time.gmtime  # UTC timestamps

    file_handler = logging.FileHandler(LOG_DIR / "pipeline.log")
    file_handler.setFormatter(formatter)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=[file_handler, stdout_handler], force=True)
    for noisy in ("httpx", "httpcore", "urllib3", "filelock"):  # one line per model-hub request otherwise
        logging.getLogger(noisy).setLevel(logging.WARNING)

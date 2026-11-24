"""rag-unified tests: the agent modules (agent/), the unified package and the collector scripts import for pytest."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collector" / "scripts"))

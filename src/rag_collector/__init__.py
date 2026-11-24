"""rag-collector: the Source stage of the RAG system.

Discovers, fetches, parses and reconciles NSE delisting documents (scripts/), keeps a point-in-time universe
database (universe.py, nse_bridge.py) and hands finished documents to rag-processor through an outbox of
immutable batches (export.py). Every stage is a step with one JSON result (steps.py), so an orchestrator
(rag-unified, later) or the continuous loop (loop.py) can run any of them on its own.
"""

__version__ = "0.3.0"

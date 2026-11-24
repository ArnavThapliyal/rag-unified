"""
Indexer: orchestrates one batch run.

    result = Indexer().index_files(files, root)

Process layout (start method 'spawn'):

    main process          discovers nothing itself; owns the LanceDB writes and the
                          per-file accounting. Never imports torch.
    N parse/chunk workers CPU only: route, metadata, parse, clean, chunk (worker.py).
    1 embedding process   the only process that loads bge-m3 and touches cuda/mps.

    workers --(bounded chunk queue)--> embedder --(bounded result queue)--> main

Every message for a file travels that one path in order, so main always sees a
file's chunks before its MSG_DONE. The bounded queues are the backpressure:
fast parsers block instead of filling memory. The embedder batches texts across
files, so the device never sees one-chunk-at-a-time calls.

A document is written only when its MSG_DONE arrives with a good status, in a
single atomic add (see vector_store.py for documents too large for one add).
A document whose doc_id is already in the table is skipped, not failed.

workers=0 runs the same steps inline in this process (tests, debugging).
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
import shutil
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from config.settings import (
    EMBED_QUEUE_MAX_BATCHES, EMBEDDING_MODEL, MP_START_METHOD, NUM_WORKERS, PIPELINE_VERSION, SCRATCH_DIR,
    WORKER_STALL_TIMEOUT_SECONDS, WRITE_MAX_ROWS_PER_ADD,
)
from pipeline.schema import Chunk, SourceDocument, build_record
from pipeline.vector_store import VectorStore
from pipeline.worker import (
    MSG_CHUNKS, MSG_DONE, STATUS_DUPLICATE, STATUS_EMPTY, STATUS_FAILED, STATUS_OK, STATUS_UNSUPPORTED,
    STATUSES, ItemKey, ItemResult, WorkerContext, process_path,
)

logger = logging.getLogger(__name__)

MSG_EMBED_ERROR = "embed_error"
MSG_FATAL = "fatal"
MSG_STOP = "stop"

_POLL_SECONDS = 0.2
_MAX_WORKER_START_FAILURES = 3

EmbedderFactory = Callable[[], Any]  # returns an object with encode(texts) -> (n, dim) float32 matrix


def _default_embedder() -> Any:
    from pipeline.embedder import Embedder  # imported here so only the embedding process loads torch

    return Embedder()


# =============================================================================
# Main-process accounting and writes
# =============================================================================

class _Run:
    """Collects embedded chunks per item and writes each document when it completes."""

    def __init__(self, store: VectorStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self.ingested_at = datetime.now(timezone.utc).isoformat()
        self.results: List[ItemResult] = []
        self._buffers: Dict[ItemKey, List[Dict[str, Any]]] = {}
        self._written: Dict[ItemKey, int] = {}
        self._partial: Dict[ItemKey, str] = {}   # items with parts already written -> doc_id
        self._claimed: Dict[str, ItemKey] = {}   # doc_id -> the first item that delivered it in this run
        self._failed: Dict[ItemKey, str] = {}    # item -> reason; later messages for it are ignored
        self._finished: Set[ItemKey] = set()
        self._dead_paths: Set[str] = set()       # delivered files whose worker died

    # ---- messages ----------------------------------------------------------------

    def on_chunks(self, key: ItemKey, doc: SourceDocument, chunks: Sequence[Chunk], vectors: Any) -> None:
        if key in self._failed or key in self._finished or key[0] in self._dead_paths:
            return
        if self._claimed.setdefault(doc.doc_id, key) != key:
            return  # same bytes delivered under another path in this run; reported at MSG_DONE
        try:
            records = [
                build_record(doc, chunk, vector, ingested_at=self.ingested_at, ingest_run_id=self.run_id,
                             embedding_model=EMBEDDING_MODEL, pipeline_version=PIPELINE_VERSION)
                for chunk, vector in zip(chunks, vectors)
            ]
            buffer = self._buffers.setdefault(key, [])
            buffer.extend(records)
            if len(buffer) >= WRITE_MAX_ROWS_PER_ADD:
                if key not in self._partial:
                    self.store.begin_partial(doc.doc_id)
                    self._partial[key] = doc.doc_id
                self._written[key] = self._written.get(key, 0) + self.store.add(buffer)
                self._buffers[key] = []
        except Exception as exc:
            logger.warning(f"{key[1]}: {exc}", exc_info=True)
            self.fail(key, f"{type(exc).__name__}: {exc}")

    def fail(self, key: ItemKey, reason: str) -> None:
        """Mark an item failed before its MSG_DONE and undo anything already written for it."""
        if key in self._failed or key in self._finished:
            return
        self._failed[key] = reason
        self._buffers.pop(key, None)
        self._abort_partial(key)

    def _abort_partial(self, key: ItemKey) -> None:
        doc_id = self._partial.pop(key, None)
        if doc_id is not None:
            try:
                self.store.abort_partial(doc_id)
            except Exception as exc:  # stays in the ledger; recover() removes it on the next run
                logger.error(f"could not remove partial document {doc_id}: {exc}")
        self._written.pop(key, None)

    def on_done(self, result: ItemResult) -> None:
        key = result.key
        if key in self._finished or key[0] in self._dead_paths:
            return
        self._finished.add(key)

        if key in self._failed:
            result.status, result.reason = STATUS_FAILED, self._failed[key]
        elif result.status == STATUS_OK:
            owner = self._claimed.get(result.doc_id or "")
            if owner is not None and owner != key:
                result.status, result.reason = STATUS_DUPLICATE, f"same content as {owner[1]}"
            else:
                try:
                    written = self._written.get(key, 0) + self.store.add(self._buffers.get(key, []))
                    doc_id = self._partial.pop(key, None)
                    if doc_id is not None:
                        self.store.finish_partial(doc_id)
                    result.chunks = written
                    if written == 0:
                        result.status, result.reason = STATUS_EMPTY, "no text or rows found"
                except Exception as exc:
                    logger.warning(f"{key[1]}: write failed: {exc}", exc_info=True)
                    self._abort_partial(key)
                    result.status, result.reason = STATUS_FAILED, f"write failed: {type(exc).__name__}: {exc}"
        elif result.status == STATUS_FAILED:
            self._abort_partial(key)

        if result.status != STATUS_OK:
            result.chunks = 0
            if result.doc_id and self._claimed.get(result.doc_id) == key:
                del self._claimed[result.doc_id]  # nothing was stored; a later copy may still be ingested
        self._buffers.pop(key, None)
        self._written.pop(key, None)
        self.results.append(result)
        self._log(result)

    def on_worker_lost(self, path: Optional[str], reason: str) -> None:
        """A worker died or was killed while on `path`; nothing more will arrive for that file."""
        if path is None:
            return
        self._dead_paths.add(path)
        for key in [k for k in list(self._buffers) + list(self._partial) if k[0] == path]:
            self._buffers.pop(key, None)
            self._abort_partial(key)
        finished_names = {k[1] for k in self._finished if k[0] == path}
        name = Path(path).name
        if name not in finished_names:
            result = ItemResult((path, name), STATUS_FAILED, reason)
            self.results.append(result)
            self._log(result)

    def on_unreported(self, paths: Sequence[str]) -> None:
        reported = {r.key[0] for r in self.results}
        for path in paths:
            if path not in reported:
                result = ItemResult((path, Path(path).name), STATUS_FAILED, "no result was reported for this file")
                self.results.append(result)
                self._log(result)

    @staticmethod
    def _log(result: ItemResult) -> None:
        for warning in result.warnings:
            logger.warning(f"{result.name}: {warning}")
        detail = f" ({result.reason})" if result.reason else ""
        level = logging.WARNING if result.status == STATUS_FAILED else logging.INFO
        logger.log(level, f"{result.name}: {result.status}, {result.chunks} chunk(s){detail}")

    # ---- summary ---------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        by_status = Counter(r.status for r in self.results)
        indexed = [r for r in self.results if r.status == STATUS_OK]
        missing = Counter(name for r in indexed for name in r.missing)
        if indexed and missing:
            logger.info("vendor metadata absent on indexed files: "
                        + ", ".join(f"{name}: {count}/{len(indexed)}" for name, count in sorted(missing.items())))
        return {
            "status": "success" if indexed else "no_data",
            "run_id": self.run_id,
            "files_indexed": by_status[STATUS_OK],
            "files_failed": by_status[STATUS_FAILED],
            "files_skipped": by_status[STATUS_DUPLICATE] + by_status[STATUS_UNSUPPORTED] + by_status[STATUS_EMPTY],
            "chunks_written": sum(r.chunks for r in indexed),
            "failed_files": [r.name for r in self.results if r.status == STATUS_FAILED],
            "by_status": {status: by_status[status] for status in STATUSES if by_status[status]},
            "by_source_type": dict(Counter(r.source_type or "unknown" for r in indexed)),
            "missing_metadata": dict(missing),
        }


# =============================================================================
# Child processes
# =============================================================================

def _parent_alive() -> bool:
    parent = mp.parent_process()
    return parent is None or parent.is_alive()


def _start_orphan_watchdog(interval: float = 2.0) -> None:
    """Exit this child as soon as the main process is gone (killed, crashed, SIGTERM without cleanup).

    _put/_get notice a dead parent only when they touch a queue; a worker deep inside one large PDF (minutes) or
    the embedder inside a long encode would otherwise linger as an orphan."""
    def watch() -> None:
        while True:
            time.sleep(interval)
            if not _parent_alive():
                os._exit(1)

    threading.Thread(target=watch, name="orphan-watchdog", daemon=True).start()


def _put(q: Any, message: Any) -> None:
    """Blocking put that gives up if the main process is gone (so children never linger)."""
    while True:
        try:
            q.put(message, timeout=2)
            return
        except queue.Full:
            if not _parent_alive():
                os._exit(1)


def _get(q: Any, timeout: Optional[float] = None) -> Any:
    """Blocking get with the same orphan check; raises queue.Empty only when `timeout` is given."""
    waited = 0.0
    while True:
        step = 2.0 if timeout is None else min(2.0, max(0.01, timeout - waited))
        try:
            return q.get(timeout=step)
        except queue.Empty:
            waited += step
            if not _parent_alive():
                os._exit(1)
            if timeout is not None and waited >= timeout:
                raise


def _worker_main(worker_id: int, task_q: Any, chunk_q: Any, ctl_q: Any, root: str, scratch_dir: str,
                 db_path: str, table_name: str, run_id: str) -> None:
    os.environ.setdefault("OMP_NUM_THREADS", "1")  # N workers must not each spawn a thread per core
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    _start_orphan_watchdog()
    from config.logsetup import setup_logging

    setup_logging(run_id)
    error: Optional[str] = None
    try:
        store = VectorStore(db_path, table_name).open_or_create()
        ctx = WorkerContext(Path(root), scratch_dir=Path(scratch_dir), is_known=store.has_doc)
        while True:
            path = _get(task_q)
            if path is None:
                break
            ctl_q.put(("start", worker_id, path))
            for message in process_path(Path(path), ctx):
                _put(chunk_q, message)
                ctl_q.put(("beat", worker_id, path))
            ctl_q.put(("end", worker_id, path))
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.error(f"worker {worker_id} stopped: {error}", exc_info=True)
    finally:
        # Flush our chunks into the pipe BEFORE announcing the exit, so the main process
        # can never tell the embedder to stop while one of our messages is still unsent.
        chunk_q.close()
        chunk_q.join_thread()
        ctl_q.put(("exit", worker_id, error))


def _embedder_main(chunk_q: Any, out_q: Any, factory: Optional[EmbedderFactory], run_id: str) -> None:
    _start_orphan_watchdog()
    from config.logsetup import setup_logging

    setup_logging(run_id)
    try:
        embedder = (factory or _default_embedder)()
    except BaseException as exc:
        logger.error(f"could not load the embedding model: {exc}", exc_info=True)
        _put(out_q, (MSG_FATAL, f"could not load the embedding model: {type(exc).__name__}: {exc}"))
        return

    target = max(1, int(getattr(embedder, "batch_size", 1)))
    pending: List[Any] = []
    pending_texts = 0

    def flush() -> None:
        nonlocal pending, pending_texts
        chunk_messages = [m for m in pending if m[0] == MSG_CHUNKS]
        vectors = None
        if chunk_messages:
            try:  # one encode call for everything waiting, across files
                vectors = embedder.encode([c.text for m in chunk_messages for c in m[3]])
            except Exception as exc:
                logger.warning(f"batch of {pending_texts} text(s) failed to embed ({exc}); retrying per file")
        offset = 0
        for message in pending:
            if message[0] != MSG_CHUNKS:
                _put(out_q, message)
                continue
            _, key, doc, chunks = message
            if vectors is not None:
                _put(out_q, (MSG_CHUNKS, key, doc, chunks, vectors[offset:offset + len(chunks)]))
                offset += len(chunks)
                continue
            try:  # isolate the failure to the file that causes it
                _put(out_q, (MSG_CHUNKS, key, doc, chunks, embedder.encode([c.text for c in chunks])))
            except Exception as exc:
                _put(out_q, (MSG_EMBED_ERROR, key, f"embedding failed: {type(exc).__name__}: {exc}"))
        pending, pending_texts = [], 0

    while True:
        try:
            # With work waiting, do not idle: a short wait lets a few more chunks join the batch.
            message = _get(chunk_q, timeout=0.05 if pending else None)
        except queue.Empty:
            flush()
            continue
        if message[0] == MSG_STOP:
            flush()
            _put(out_q, (MSG_STOP,))
            return
        pending.append(message)
        if message[0] == MSG_CHUNKS:
            pending_texts += len(message[3])
            if pending_texts >= target:
                flush()


# =============================================================================
# Indexer
# =============================================================================

class Indexer:
    def __init__(
        self,
        *,
        workers: Optional[int] = None,
        embedder: Any = None,
        embedder_factory: Optional[EmbedderFactory] = None,
        store: Optional[VectorStore] = None,
        scratch_dir: Optional[Path] = None,
        run_id: Optional[str] = None,
    ):
        """
        `embedder` (an instance) is used for inline runs; `embedder_factory` (a picklable
        callable) builds the embedder inside the embedding process for parallel runs.
        Both default to the real bge-m3 Embedder.
        """
        self.workers = NUM_WORKERS if workers is None else max(0, workers)
        self._embedder = embedder
        self._embedder_factory = embedder_factory
        self._store = store
        self._scratch_dir = Path(scratch_dir) if scratch_dir is not None else SCRATCH_DIR
        self.run_id = run_id or uuid.uuid4().hex[:12]

    def index_files(self, files: Sequence[Path], root: Path) -> Dict[str, Any]:
        """
        Index every file in `files` (found anywhere under `root`). What a file is
        comes only from type_router's sniffing; where it lives only matters for
        locating batch metadata files. One bad file never stops the others.
        """
        store = (self._store or VectorStore()).open_or_create()
        recovered = store.recover()
        self._clear_scratch()

        run = _Run(store, self.run_id)
        paths = [str(f) for f in files]
        started = time.monotonic()
        if paths:
            if self.workers == 0:
                self._run_inline(paths, Path(root), run)
            else:
                self._run_parallel(paths, Path(root), run)
        run.on_unreported(paths)

        summary = run.summary()
        summary["recovered_partial_documents"] = len(recovered)
        try:
            summary["vector_index"] = store.finalize()["vector_index"]
        except Exception as exc:  # the rows are safely stored; only the index refresh failed
            logger.error(f"index finalisation failed: {exc}", exc_info=True)
            summary["finalize_error"] = f"{type(exc).__name__}: {exc}"
        summary["seconds"] = round(time.monotonic() - started, 1)
        self._clear_scratch()
        return summary

    def _clear_scratch(self) -> None:
        """Remove extraction leftovers (also those of a previous run that crashed)."""
        if not self._scratch_dir.is_dir():
            return
        for child in self._scratch_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    # ---- inline ----------------------------------------------------------------

    def _run_inline(self, paths: Sequence[str], root: Path, run: _Run) -> None:
        ctx = WorkerContext(root, scratch_dir=self._scratch_dir, is_known=run.store.has_doc)
        for path in paths:
            for message in process_path(Path(path), ctx):
                if message[0] == MSG_DONE:
                    run.on_done(message[1])
                    continue
                _, key, doc, chunks = message
                try:
                    if self._embedder is None:
                        self._embedder = (self._embedder_factory or _default_embedder)()
                    vectors = self._embedder.encode([c.text for c in chunks])
                except Exception as exc:
                    logger.warning(f"{key[1]}: embedding failed: {exc}", exc_info=True)
                    run.fail(key, f"embedding failed: {type(exc).__name__}: {exc}")
                    continue
                run.on_chunks(key, doc, chunks, vectors)

    # ---- parallel --------------------------------------------------------------

    def _run_parallel(self, paths: Sequence[str], root: Path, run: _Run) -> None:
        ctx = mp.get_context(MP_START_METHOD)
        task_q = ctx.Queue()
        chunk_q = ctx.Queue(maxsize=EMBED_QUEUE_MAX_BATCHES)
        out_q = ctx.Queue(maxsize=EMBED_QUEUE_MAX_BATCHES)
        ctl_q = ctx.Queue()

        worker_count = min(self.workers, len(paths))
        for path in paths:
            task_q.put(path)
        for _ in range(worker_count):
            task_q.put(None)  # one stop marker per worker

        store = run.store
        worker_args = (task_q, chunk_q, ctl_q, str(root), str(self._scratch_dir), str(store.path),
                       store.table_name, self.run_id)
        embedder = ctx.Process(target=_embedder_main, name="embedder", daemon=True,
                               args=(chunk_q, out_q, self._embedder_factory, self.run_id))
        workers: Dict[int, Any] = {}
        started: List[Any] = [embedder]
        next_id = 0

        def start_worker() -> None:
            nonlocal next_id
            proc = ctx.Process(target=_worker_main, name=f"worker-{next_id}", daemon=True,
                               args=(next_id, *worker_args))
            proc.start()
            workers[next_id] = proc
            started.append(proc)
            next_id += 1

        logger.info(f"starting {worker_count} parse/chunk worker(s) and 1 embedding process ({MP_START_METHOD})")
        embedder.start()
        for _ in range(worker_count):
            start_worker()

        current: Dict[int, str] = {}        # worker -> file it is on
        last_seen: Dict[int, float] = {w: time.monotonic() for w in workers}
        killed: Dict[int, str] = {}         # worker -> why we terminated it
        start_failures = 0
        stop_sent = False

        def drain_control() -> None:
            while True:
                try:
                    kind, worker_id, detail = ctl_q.get_nowait()
                except queue.Empty:
                    return
                last_seen[worker_id] = time.monotonic()
                if kind == "start":
                    current[worker_id] = detail
                elif kind == "end":
                    current.pop(worker_id, None)
                elif kind == "exit":
                    workers.pop(worker_id, None)
                    if detail:
                        logger.error(f"worker {worker_id} exited with an error: {detail}")

        try:
            while True:
                drain_control()

                for worker_id, proc in list(workers.items()):
                    if proc.is_alive():
                        stalled = WORKER_STALL_TIMEOUT_SECONDS and worker_id in current and (
                            time.monotonic() - last_seen[worker_id] > WORKER_STALL_TIMEOUT_SECONDS)
                        if stalled and worker_id not in killed:
                            killed[worker_id] = f"no progress for {WORKER_STALL_TIMEOUT_SECONDS}s; worker killed"
                            proc.terminate()
                        continue
                    proc.join()
                    drain_control()  # a normal exit announces itself; pick that up before judging
                    if worker_id not in workers:
                        continue
                    # died without announcing: crash (native code in a parser) or our own terminate()
                    workers.pop(worker_id)
                    path = current.pop(worker_id, None)
                    reason = killed.pop(worker_id, f"worker process died (exit code {proc.exitcode})")
                    logger.error(f"worker {worker_id} lost on {path or 'startup'}: {reason}")
                    run.on_worker_lost(path, reason)
                    if path is None:
                        start_failures += 1
                        if start_failures >= _MAX_WORKER_START_FAILURES:
                            raise RuntimeError("worker processes keep dying before taking a file; aborting the run")
                    start_worker()  # its stop marker is still queued, so the replacement will finish cleanly
                    last_seen[next_id - 1] = time.monotonic()

                embedder_gone = not embedder.is_alive()  # it exits right after its final message

                if not workers and not stop_sent:
                    try:
                        chunk_q.put_nowait((MSG_STOP,))  # never block here: the embedder may be waiting on us
                        stop_sent = True
                    except queue.Full:
                        pass

                try:
                    message = out_q.get(timeout=_POLL_SECONDS)
                except queue.Empty:
                    if embedder_gone:  # gone, and nothing left from it to read: it did not finish normally
                        raise RuntimeError(f"the embedding process died (exit code {embedder.exitcode})")
                    continue
                kind = message[0]
                if kind == MSG_CHUNKS:
                    run.on_chunks(message[1], message[2], message[3], message[4])
                elif kind == MSG_DONE:
                    run.on_done(message[1])
                elif kind == MSG_EMBED_ERROR:
                    run.fail(message[1], message[2])
                elif kind == MSG_FATAL:
                    raise RuntimeError(message[1])
                elif kind == MSG_STOP:
                    break
        finally:
            for proc in started:
                if proc.is_alive() and (proc is embedder or proc in workers.values()):
                    proc.terminate()  # only on an abnormal end; finished workers exit by themselves
            for proc in started:
                proc.join(timeout=10)
            for q in (task_q, chunk_q, out_q, ctl_q):
                q.cancel_join_thread()
                q.close()

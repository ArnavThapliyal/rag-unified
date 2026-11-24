"""
Embedder: texts -> L2-normalised bge-m3 vectors.

    embedder = Embedder()              # loads the model; do this in ONE process only
    matrix = embedder.encode(texts)    # float32, shape (len(texts), EMBEDDING_DIM)

Importing this module loads nothing: torch and the model are imported inside
Embedder.__init__, so parse/chunk workers and the main process can import it
freely. The device is chosen as cuda -> mps -> cpu unless EMBED_DEVICE forces one.

Vectors are normalised so cosine distance is well defined and equals the dot
product. There is no deduplication here: every chunk it is given is embedded.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Sequence

import numpy as np

from config.settings import (
    EMBED_BATCH_SIZE, EMBED_DEVICE, EMBED_FP16, EMBED_MAX_SEQ_LENGTH, EMBEDDING_DIM, EMBEDDING_MODEL,
    MPS_EMPTY_CACHE_EVERY, MPS_MAX_BATCH_SIZE,
)

logger = logging.getLogger(__name__)

_NORM_TOLERANCE = 1e-3


def resolve_device(preferred: Optional[str] = EMBED_DEVICE) -> str:
    """EMBED_DEVICE if set, otherwise the best available of cuda -> mps -> cpu."""
    if preferred:
        return preferred
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _is_out_of_memory(exc: BaseException) -> bool:
    return isinstance(exc, MemoryError) or "out of memory" in str(exc).lower()


# Fixed-shape mode (MPS). The MPS backend keeps compiled state for every distinct
# (rows, tokens) input shape and never releases it; with naturally varying batches
# its memory grew to 60 GB within a few dozen calls and every later call failed.
# So on MPS each batch is padded to one of a small, fixed set of shapes.
_ROW_BUCKETS = (4, 16)   # plus the configured batch size
_MIN_TOKEN_BUCKET = 64   # token lengths are padded to 64, 128, 256, ... up to the model maximum


def _row_bucket(rows: int, batch_size: int) -> int:
    return next((b for b in _ROW_BUCKETS if rows <= b < batch_size), batch_size)


def _token_bucket(tokens: int, maximum: int) -> int:
    bucket = _MIN_TOKEN_BUCKET
    while bucket < tokens:
        bucket *= 2
    return max(tokens, min(bucket, maximum))


class Embedder:
    def __init__(self, model: Any = None, device: Optional[str] = None, batch_size: int = EMBED_BATCH_SIZE,
                 fixed_shapes: Optional[bool] = None):
        """
        `model` is injectable for tests; anything with a sentence-transformers style encode().
        `fixed_shapes` defaults to on for MPS and off elsewhere (see _ROW_BUCKETS above).
        """
        self.batch_size = max(1, batch_size)
        self._calls = 0
        self.shapes_used: set = set()  # (rows, tokens) shapes sent to the model in fixed-shape mode
        if model is not None:
            self.device = device or "cpu"
            self._model = model
            self._fixed_shapes = bool(fixed_shapes)
            return

        # bge-m3 ships pickled weights only. Without this, transformers starts a background
        # thread that downloads a multi-GB safetensors conversion and keeps the process
        # from exiting until it is done.
        os.environ.setdefault("DISABLE_SAFETENSORS_CONVERSION", "1")
        from sentence_transformers import SentenceTransformer

        self.device = device or resolve_device()
        logger.info(f"loading {EMBEDDING_MODEL} on {self.device}")
        self._model = SentenceTransformer(EMBEDDING_MODEL, device=self.device)
        self._model.max_seq_length = EMBED_MAX_SEQ_LENGTH
        if EMBED_FP16 and self.device == "cuda":
            self._model.half()
        dimension = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
        reported = dimension()
        if reported != EMBEDDING_DIM:
            raise ValueError(f"{EMBEDDING_MODEL} produces {reported}-dim vectors; the schema expects {EMBEDDING_DIM}")
        self._fixed_shapes = (self.device == "mps") if fixed_shapes is None else fixed_shapes
        if self.device == "mps":
            self.batch_size = min(self.batch_size, max(1, MPS_MAX_BATCH_SIZE))

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Embed texts in length-sorted batches; rows come back in the input order."""
        if not texts:
            return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

        order = sorted(range(len(texts)), key=lambda i: len(texts[i]), reverse=True)  # similar lengths -> less padding
        out = np.empty((len(texts), EMBEDDING_DIM), dtype=np.float32)
        batch_size = self.batch_size
        start = 0
        while start < len(order):
            indices = order[start:start + batch_size]
            try:
                vectors = self._encode_batch([texts[i] for i in indices])
            except Exception as exc:
                if not _is_out_of_memory(exc):
                    raise
                if batch_size == 1:
                    if not self._fall_back_to_cpu():
                        raise
                    continue
                batch_size = max(1, batch_size // 2)
                logger.warning(f"out of memory on {self.device}; retrying with batch size {batch_size}")
                self._empty_cache()
                continue
            out[indices] = vectors
            start += len(indices)

        if batch_size < self.batch_size:
            self.batch_size = batch_size  # stay at the size that fits for the rest of the run
        return out

    def _encode_batch(self, batch: List[str]) -> np.ndarray:
        if self._fixed_shapes:
            vectors = self._forward_fixed(batch)
        else:
            vectors = np.asarray(
                self._model.encode(batch, batch_size=len(batch), normalize_embeddings=True,
                                   convert_to_numpy=True, show_progress_bar=False),
                dtype=np.float32,
            )
        if vectors.shape != (len(batch), EMBEDDING_DIM):
            raise ValueError(f"model returned shape {vectors.shape}, expected {(len(batch), EMBEDDING_DIM)}")
        if not np.isfinite(vectors).all():
            raise ValueError("model returned non-finite values")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if (norms < 1e-12).any():
            raise ValueError("model returned a zero vector")
        if np.abs(norms - 1.0).max() > _NORM_TOLERANCE:  # e.g. fp16 rounding, or a model ignoring the flag
            vectors = vectors / norms
        self._calls += 1
        if self.device == "mps" and MPS_EMPTY_CACHE_EVERY and self._calls % MPS_EMPTY_CACHE_EVERY == 0:
            self._empty_cache()
        return vectors

    def _forward_fixed(self, batch: List[str]) -> np.ndarray:
        """Run the model on `batch` padded to a bucketed (rows, tokens) shape; padding does not change the vectors."""
        import torch

        model = self._model
        rows = _row_bucket(len(batch), max(self.batch_size, len(batch)))
        padded = list(batch) + [batch[-1]] * (rows - len(batch))  # filler rows are dropped below
        features = (getattr(model, "preprocess", None) or model.tokenize)(padded)
        tokens = features["input_ids"].shape[1]
        target = _token_bucket(tokens, int(getattr(model, "max_seq_length", tokens) or tokens))
        if target > tokens:
            pad_id = getattr(getattr(model, "tokenizer", None), "pad_token_id", None) or 0
            features["input_ids"] = torch.nn.functional.pad(features["input_ids"], (0, target - tokens), value=pad_id)
            features["attention_mask"] = torch.nn.functional.pad(features["attention_mask"], (0, target - tokens), value=0)
        self.shapes_used.add((rows, target))
        features = {k: (v.to(self.device) if isinstance(v, torch.Tensor) else v) for k, v in features.items()}
        with torch.no_grad():
            output = model.forward(features)["sentence_embedding"]
        output = torch.nn.functional.normalize(output.float(), dim=1)
        return output[:len(batch)].cpu().numpy().astype(np.float32)

    def _fall_back_to_cpu(self) -> bool:
        """Last resort when the device is out of memory even for one text. Slow, but the run completes."""
        if self.device == "cpu" or not hasattr(self._model, "to"):
            return False
        logger.error(f"{self.device} is out of memory even at batch size 1; continuing on cpu (much slower)")
        self._empty_cache()
        self._model.to("cpu")
        self.device = "cpu"
        self._fixed_shapes = False
        return True

    def _empty_cache(self) -> None:
        try:
            import torch

            if self.device == "mps":
                torch.mps.empty_cache()
            elif self.device == "cuda":
                torch.cuda.empty_cache()
        except Exception:  # cache release is best-effort
            pass

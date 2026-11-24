"""Deterministic stand-ins for the bge-m3 model, so tests never download or load it."""

import hashlib

import numpy as np

from pipeline.embedder import Embedder

DIM = 1024


def text_vector(text: str) -> np.ndarray:
    """A fixed unit vector per text (seeded by its hash)."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


class StubModel:
    """Mimics SentenceTransformer.encode. `oom_above` simulates a device that only fits small batches."""

    def __init__(self, oom_above=None, normalise=True):
        self.oom_above = oom_above
        self.normalise = normalise
        self.batch_sizes = []

    def encode(self, batch, batch_size=None, normalize_embeddings=False, convert_to_numpy=True,
               show_progress_bar=False):
        if self.oom_above is not None and len(batch) > self.oom_above:
            raise RuntimeError("MPS backend out of memory")
        self.batch_sizes.append(len(batch))
        vectors = np.stack([text_vector(t) for t in batch])
        return vectors if self.normalise else vectors * 3.0


def make_stub_embedder() -> Embedder:
    """Picklable factory for the embedding process in multi-process tests."""
    return Embedder(model=StubModel(), batch_size=16)


def broken_embedder_factory() -> Embedder:
    """Stands in for a model that cannot be loaded (no weights, no memory, ...)."""
    raise RuntimeError("model weights unavailable")


class FlakyEmbedder:
    """Embeds normally until `fail_after` texts have been seen, then raises on every call."""

    batch_size = 16

    def __init__(self, fail_after: int):
        self.fail_after = fail_after
        self.seen = 0

    def encode(self, texts):
        self.seen += len(texts)
        if self.seen > self.fail_after:
            raise RuntimeError("device lost")
        return np.stack([text_vector(t) for t in texts])


class TensorStubModel:
    """Mimics the tokenise + forward interface of SentenceTransformer, with real torch tensors on cpu."""

    max_seq_length = 1024

    def __init__(self):
        self.texts = []
        self.forward_shapes = []
        self.device = "cpu"

    def tokenize(self, batch):
        import torch

        lengths = [max(1, len(t.split())) for t in batch]
        width = min(self.max_seq_length, max(lengths))
        ids = torch.zeros((len(batch), width), dtype=torch.long)
        mask = torch.zeros((len(batch), width), dtype=torch.long)
        for row, (text, n) in enumerate(zip(batch, lengths)):
            self.texts.append(text)
            ids[row, 0] = len(self.texts)          # first token identifies the text
            mask[row, :min(n, width)] = 1
        return {"input_ids": ids, "attention_mask": mask}

    def forward(self, features):
        import torch

        self.forward_shapes.append(tuple(features["input_ids"].shape))
        rows = [text_vector(self.texts[int(i) - 1]) * 2.0 for i in features["input_ids"][:, 0]]  # not normalised
        return {"sentence_embedding": torch.tensor(np.stack(rows))}

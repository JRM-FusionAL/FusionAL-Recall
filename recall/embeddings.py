"""Embedding engine — wraps sentence-transformers for float32 vector generation."""

from __future__ import annotations

import struct
from typing import List


class EmbeddingEngine:
    """Generates float32 embeddings using a SentenceTransformer model.

    Embeddings are serialised as raw bytes via struct.pack so they can be
    stored directly in SQLite BLOB columns and compared with cosine similarity.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        # Deferred import so tests can mock before loading the heavy model.
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self._dim: int | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed(self, text: str) -> bytes:
        """Return a float32 blob for *text*."""
        vec = self._model.encode(text, normalize_embeddings=True)
        return struct.pack(f"{len(vec)}f", *vec)

    def embed_batch(self, texts: List[str]) -> List[bytes]:
        """Return float32 blobs for a list of texts (batched inference)."""
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return [struct.pack(f"{len(v)}f", *v) for v in vecs]

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def unpack(blob: bytes) -> tuple[float, ...]:
        """Deserialise a float32 blob back to a Python tuple of floats."""
        n = len(blob) // 4  # each float32 is 4 bytes
        return struct.unpack(f"{n}f", blob)

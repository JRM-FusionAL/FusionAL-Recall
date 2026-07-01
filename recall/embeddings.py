"""Embedding engine — wraps fastembed (ONNX Runtime) for float32 vector generation.

Uses fastembed instead of sentence-transformers to avoid the PyTorch AVX2 requirement.
The same model weights (all-MiniLM-L6-v2) are used via ONNX export, so existing DB
embeddings remain valid.
"""

from __future__ import annotations

import struct
from typing import List


class EmbeddingEngine:
    """Generates float32 embeddings using fastembed (ONNX Runtime backend).

    Embeddings are serialised as raw bytes via struct.pack so they can be
    stored directly in SQLite BLOB columns and compared with cosine similarity.
    """

    # fastembed model name for all-MiniLM-L6-v2
    _FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        # Deferred import so tests can mock before loading the heavy model.
        from fastembed import TextEmbedding

        # fastembed expects the full HuggingFace model ID
        fe_name = model_name if "/" in model_name else self._FASTEMBED_MODEL
        self._model = TextEmbedding(fe_name)
        self._dim: int | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed(self, text: str) -> bytes:
        """Return a float32 blob for *text*."""
        vec = next(self._model.embed([text]))
        return struct.pack(f"{len(vec)}f", *vec)

    def embed_batch(self, texts: List[str]) -> List[bytes]:
        """Return float32 blobs for a list of texts (batched inference)."""
        return [struct.pack(f"{len(v)}f", *v) for v in self._model.embed(texts)]

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def unpack(blob: bytes) -> tuple[float, ...]:
        """Deserialise a float32 blob back to a Python tuple of floats."""
        n = len(blob) // 4  # each float32 is 4 bytes
        return struct.unpack(f"{n}f", blob)

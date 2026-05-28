import struct
from sentence_transformers import SentenceTransformer


class EmbeddingEngine:
    """Wrapper around sentence-transformers for generating float32 embeddings."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """Initialize the embedding model.
        
        Args:
            model_name: HuggingFace model identifier (defaults to all-MiniLM-L6-v2, CPU-friendly)
        """
        self.model = SentenceTransformer(model_name)

    def embed(self, text: str) -> bytes:
        """Embed a single text string.
        
        Args:
            text: Text to embed
            
        Returns:
            float32 embedding as bytes (packed via struct)
        """
        embedding = self.model.encode(text, convert_to_tensor=False)
        return struct.pack(f'{len(embedding)}f', *embedding)

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        """Embed multiple texts in batch.
        
        Args:
            texts: List of text strings
            
        Returns:
            List of float32 embeddings as bytes
        """
        embeddings = self.model.encode(texts, convert_to_tensor=False)
        return [struct.pack(f'{len(emb)}f', *emb) for emb in embeddings]

import logging
from typing import Optional
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from contextmesh.config import EmbeddingsConfig
from contextmesh.store.db import Database

logger = logging.getLogger(__name__)

class EmbeddingStore:
    def __init__(self, config: EmbeddingsConfig, db: Database):
        self.config = config
        self.db = db
        self._model: Optional[SentenceTransformer] = None

    def _get_model(self) -> SentenceTransformer:
        if self._model is None:
            logger.info(f"Loading sentence-transformers model: {self.config.model}")
            self._model = SentenceTransformer(
                self.config.model,
                cache_folder=self.config.cache_dir if self.config.cache_dir else None
            )
        return self._model

    async def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        model = self._get_model()
        embeddings = model.encode(texts, batch_size=self.config.batch_size, convert_to_numpy=True)
        return embeddings.astype(np.float32)

    async def store_node_embedding(self, node_id: str, node_table: str, text: str) -> None:
        embeddings = await self.encode([text])
        if len(embeddings) == 0:
            return
        
        vec = embeddings[0]
        dim = len(vec)
        blob = vec.tobytes()
        
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        
        await self.db.execute(
            """
            INSERT OR REPLACE INTO embeddings (node_id, node_table, embedding, dim, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (node_id, node_table, blob, dim, self.config.model, now)
        )
        await self.db.commit()

    async def get_embedding(self, node_id: str) -> Optional[np.ndarray]:
        row = await self.db.fetchone(
            "SELECT embedding FROM embeddings WHERE node_id = ?",
            (node_id,)
        )
        if not row:
            return None
        return np.frombuffer(row["embedding"], dtype=np.float32)

    async def similarity_search(self, query: str, node_ids: list[str], top_k: int = 20) -> list[tuple[str, float]]:
        if not node_ids:
            return []
            
        query_vec = (await self.encode([query]))[0]
        
        results = []
        for node_id in node_ids:
            vec = await self.get_embedding(node_id)
            if vec is not None:
                results.append((node_id, vec))
                
        if not results:
            return []
            
        valid_ids, vecs = zip(*results)
        vecs_matrix = np.stack(vecs)
        
        sims = cosine_similarity([query_vec], vecs_matrix)[0]
        
        scored = list(zip(valid_ids, sims))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    async def cosine_distance(self, vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        sim = cosine_similarity([vec_a], [vec_b])[0][0]
        return max(0.0, 1.0 - float(sim))


    async def embed_and_store(self, node) -> None:
        """Alias for store_node_embedding — accepts a SessionNode object."""
        text = (node.summary or node.content or "")[:2000]
        if text:
            await self.store_node_embedding(node.node_id, "nodes", text)


_store: Optional[EmbeddingStore] = None


def get_store() -> EmbeddingStore:
    global _store
    if _store is None:
        raise RuntimeError("EmbeddingStore not initialized — call init_store() first")
    return _store


def init_store(config: EmbeddingsConfig, db: Database) -> EmbeddingStore:
    """Synchronous init — model loaded lazily on first encode() call."""
    global _store
    _store = EmbeddingStore(config, db)
    return _store

"""Local-only multilingual embedding HTTP service."""

from __future__ import annotations

import asyncio
import os
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from research_agent.retrieval import normalize_vector


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texts: list[str] = Field(min_length=1, max_length=64)


app = FastAPI(title="Local embedding service", docs_url=None, redoc_url=None)
_model = None
_model_lock = threading.Lock()


def model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    import torch
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise RuntimeError("embedding dependencies are not installed") from exc
                torch.set_num_threads(int(os.environ.get("EMBEDDING_THREADS", "4")))
                device = "cuda" if torch.cuda.is_available() else "cpu"
                _model = SentenceTransformer(
                    os.environ.get("EMBEDDING_MODEL_PATH", "/models/gte-multilingual-base"),
                    trust_remote_code=True,
                    device=device,
                )
    return _model


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "loaded": _model is not None,
        "model": "Alibaba-NLP/gte-multilingual-base",
        "revision": "9bbca17",
        "dimensions": 768,
    }


@app.post("/embed")
async def embed(body: EmbedRequest):
    if any(not text.strip() or len(text) > 20000 for text in body.texts):
        raise HTTPException(422, "texts must be non-empty and at most 20000 characters")
    try:
        vectors = await asyncio.to_thread(
            model().encode,
            body.texts,
            batch_size=min(32, len(body.texts)),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    except Exception as exc:
        raise HTTPException(503, "embedding inference failed") from exc
    values = [normalize_vector(row.tolist()) for row in vectors]
    if any(len(row) != 768 for row in values):
        raise HTTPException(503, "embedding model returned unexpected dimensions")
    return {"vectors": values, "model_revision": "9bbca17"}

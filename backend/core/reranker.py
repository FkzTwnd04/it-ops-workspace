# backend/core/reranker.py
# BGE-Reranker 精排 + 「混合召回 → 精排」一体化检索 Pipeline。

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)
backend_path = os.path.dirname(os.path.dirname(__file__))
RERANK_MAX_INPUT_CHARS = 512   # CrossEncoder max_length=512，过长文档截断

# 本地模型的加载和推理都只能在这一个线程里做：FlagEmbedding / torch 多线程并发加载或推理会触发原生崩溃
MODEL_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-model")
_init_lock = threading.Lock()


@dataclass
class RankedDocument:
    content:        str
    score:          float  # Reranker 相关性概率 [0, 1]
    original_index: int
    metadata:       dict


class BGEReranker:
    """BGE-Reranker CrossEncoder 精排服务（单例）。输出 [0, 1] 概率，可直接当置信度用。"""

    _instance: Optional["BGEReranker"] = None

    def __init__(self):
        import torch
        from sentence_transformers import CrossEncoder

        os.environ["ACCELERATE_USE_META_DEVICE"] = "0"
        model_path = os.path.join(backend_path, get_settings().reranker_model_path)
        use_local = os.path.isdir(model_path) and any(
            f.endswith((".bin", ".safetensors", ".json")) for f in os.listdir(model_path)
        )
        model_id = model_path if use_local else "BAAI/bge-reranker-large"
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("reranker.loading", model_id=model_id, device=device)
        self._model = CrossEncoder(model_id, device=device, max_length=512)
        logger.info("reranker.loaded", model_id=model_id)

    @classmethod
    def get_instance(cls) -> "BGEReranker":
        if cls._instance is None:
            with _init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def rerank_with_confidence(
        self, query: str, documents: list[dict], top_k: int = 3
    ) -> tuple[list[RankedDocument], float]:
        """精排并返回 Top-1 分数作为检索置信度。"""
        if not documents:
            return [], 0.0
        pairs = [(query, (doc.get("content") or "")[:RERANK_MAX_INPUT_CHARS]) for doc in documents]
        scores: list[float] = self._model.predict(pairs).tolist()
        ranked = sorted(
            (
                RankedDocument(
                    content=documents[i].get("content", ""),
                    score=scores[i],
                    original_index=i,
                    metadata=documents[i].get("metadata", {}),
                )
                for i in range(len(documents))
            ),
            key=lambda x: x.score,
            reverse=True,
        )
        top = ranked[:top_k]
        confidence = top[0].score if top else 0.0
        logger.info("reranker.done", candidates=len(documents), top_k=top_k, confidence=round(confidence, 4))
        return top, confidence


def retrieve(
    query: str,
    tenant_id: str,
    doc_type: Optional[str] = None,
    recall_top_k: int = 10,
    rerank_top_k: int = 3,
) -> tuple[list[RankedDocument], float]:
    """
    Hybrid 召回 → BGE 精排。同步函数（CPU 推理 + Milvus 阻塞 IO），异步调用方需放进线程池。

    Returns:
        (ranked_docs, confidence)，confidence 为 Top-1 精排分数。
    """
    from backend.core.knowledge_base import BGEMEmbedder, KnowledgeBaseClient

    dense_vec, sparse_vec = BGEMEmbedder.get_instance().encode_query(query)
    kb = KnowledgeBaseClient()
    candidates = kb.hybrid_search(
        query_embedding=dense_vec,
        query_sparse=sparse_vec,
        top_k=recall_top_k,
        filters=kb.build_filter(tenant_id, doc_type),
    )
    if not candidates:
        logger.info("retrieve.empty", query_preview=query[:50], tenant_id=tenant_id)
        return [], 0.0
    return BGEReranker.get_instance().rerank_with_confidence(query, candidates, top_k=rerank_top_k)

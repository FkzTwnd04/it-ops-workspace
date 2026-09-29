# backend/core/knowledge_base.py
# 运维知识库：BGE-M3 嵌入 + Milvus 混合检索（Dense + Sparse）。
# 多租户：tenant_id 是 Collection 的 partition key，Milvus 按租户哈希到不同分区（namespace），
#        检索时必须带 tenant_id 过滤，只会扫描该租户所在分区。

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from pymilvus import AnnSearchRequest, MilvusClient, WeightedRanker

from backend.config import get_settings
from backend.core.exceptions import MilvusConnectionError
from backend.core.logger import get_logger

logger = get_logger(__name__)
backend_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_init_lock = threading.Lock()

COLLECTION_NAME = "it_knowledge"
DOC_TYPES = ("runbook", "ticket")   # 运维手册 / 历史工单


# ──────────────────────────────────────────────────────────────
# BGE-M3 本地嵌入模型（进程内单例，dense + sparse 双输出）
# ──────────────────────────────────────────────────────────────

class BGEMEmbedder:
    """
    BGE-M3 本地嵌入模型单例。一次推理同时输出：
      - dense 向量（1024 维，语义检索）
      - sparse 向量（{token_id: weight}，关键词检索，对主机名/错误码这类专有名词很关键）
    """

    _instance: Optional["BGEMEmbedder"] = None

    def __init__(self, model_path: str):
        # transformers>=5.0 移除了 is_torch_fx_available，FlagEmbedding 仍会引用，这里补上
        import importlib.util as _ilu
        from transformers.utils import import_utils as _tf_iu
        if not hasattr(_tf_iu, "is_torch_fx_available"):
            _tf_iu.is_torch_fx_available = lambda: _ilu.find_spec("torch.fx") is not None

        import torch
        from FlagEmbedding import BGEM3FlagModel

        logger.info("bge_m3.loading", model_path=model_path)
        use_fp16 = torch.cuda.is_available()   # MPS/CPU 下 fp16 不稳定，只在 CUDA 启用
        self._model = BGEM3FlagModel(model_name_or_path=model_path, use_fp16=use_fp16)
        logger.info("bge_m3.loaded", use_fp16=use_fp16)

    @classmethod
    def get_instance(cls) -> "BGEMEmbedder":
        if cls._instance is None:
            with _init_lock:
                if cls._instance is None:
                    cls._instance = BGEMEmbedder(os.path.join(backend_path, get_settings().bge_m3_model_path))
        return cls._instance

    def encode(self, texts: list[str], batch_size: int = 12) -> tuple[list[list[float]], list[dict]]:
        """批量编码，返回 (dense_vecs, sparse_vecs)。"""
        output = self._model.encode(
            texts,
            batch_size=batch_size,
            max_length=8192,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense_vecs = output["dense_vecs"].tolist()
        # numpy.float16 → float：Checkpoint 用 msgpack 序列化 State，不支持 numpy 类型
        sparse_vecs = [{int(k): float(v) for k, v in d.items()} for d in output["lexical_weights"]]
        return dense_vecs, sparse_vecs

    def encode_query(self, text: str) -> tuple[list[float], dict]:
        dense_list, sparse_list = self.encode([text], batch_size=1)
        return dense_list[0], sparse_list[0]


@dataclass
class DocumentChunk:
    """写入 Milvus 的单个文档块，字段与 Collection Schema 一一对应。"""
    id:               str
    content:          str
    embedding:        list[float]
    sparse_embedding: dict
    doc_type:         str                  # "runbook" / "ticket"
    document_id:      str
    source_name:      str                  # 展示用来源，如 "VPN 故障手册 > 证书过期"
    chunk_type:       str
    chunk_index:      int
    version:          str
    content_hash:     str                  # 内容哈希，建库时用来跳过未变化的文档
    tenant_id:        str = "tenant_default"
    updated_at:       int = field(default_factory=lambda: int(time.time()))


class KnowledgeBaseClient:
    """
    Milvus 知识库客户端（进程内共享一个 MilvusClient 连接）。
    """

    _client: Optional[MilvusClient] = None
    _loaded: bool = False

    ANN_EF = 64
    DENSE_WEIGHT, SPARSE_WEIGHT = 0.7, 0.3

    def __init__(self):
        if KnowledgeBaseClient._client is None:
            settings = get_settings()
            uri = f"http://{settings.milvus_host}:{settings.milvus_port}"
            try:
                KnowledgeBaseClient._client = MilvusClient(uri=uri, timeout=10)
            except Exception as e:
                raise MilvusConnectionError(f"Milvus 连接失败: {e}") from e
            logger.info("milvus.connected", uri=uri)

        if not KnowledgeBaseClient._loaded:
            try:
                KnowledgeBaseClient._client.load_collection(COLLECTION_NAME)
                KnowledgeBaseClient._loaded = True
            except Exception:
                pass   # init_milvus.py 尚未运行时忽略，检索会返回空

    # ── 写入 ────────────────────────────────────────────────

    def upsert_chunks(self, chunks: list[DocumentChunk]) -> int:
        if not chunks:
            return 0
        data = [
            {
                "id":               c.id,
                "embedding":        c.embedding,
                "sparse_embedding": c.sparse_embedding,
                "content":          c.content[:4096],
                "chunk_index":      c.chunk_index,
                "document_id":      c.document_id,
                "doc_type":         c.doc_type,
                "tenant_id":        c.tenant_id,
                "source_name":      c.source_name[:256],
                "chunk_type":       c.chunk_type,
                "version":          c.version,
                "content_hash":     c.content_hash,
                "updated_at":       c.updated_at,
            }
            for c in chunks
        ]
        self._client.upsert(collection_name=COLLECTION_NAME, data=data)
        logger.info("knowledge_base.chunks_upserted", count=len(chunks))
        return len(chunks)

    def delete_document_chunks(self, document_id: str, tenant_id: str) -> None:
        """文档更新时先删后插；带 tenant_id 保证只删本租户的数据。"""
        self._client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'document_id == "{_escape(document_id)}" and tenant_id == "{_escape(tenant_id)}"',
        )

    def get_document_hash(self, document_id: str, tenant_id: str) -> Optional[str]:
        """查询文档当前版本的内容哈希，用于幂等建库（哈希未变 → 跳过重建）。"""
        try:
            rows = self._client.query(
                collection_name=COLLECTION_NAME,
                filter=f'document_id == "{_escape(document_id)}" and tenant_id == "{_escape(tenant_id)}"',
                output_fields=["content_hash"],
                limit=1,
            )
        except Exception:
            return None
        return rows[0]["content_hash"] if rows else None

    @staticmethod
    def generate_chunk_id(content: str, document_id: str, chunk_index: int, tenant_id: str) -> str:
        """内容 + 位置 + 租户不变则 ID 不变，upsert 天然幂等。"""
        raw = f"{tenant_id}_{document_id}_{chunk_index}_{content[:50]}"
        return hashlib.md5(raw.encode()).hexdigest()

    # ── 检索 ────────────────────────────────────────────────

    def hybrid_search(
        self,
        query_embedding: list[float],
        query_sparse: dict,
        top_k: int,
        filters: str,
    ) -> list[dict]:
        """
        Dense + Sparse 两路 ANN 在 Milvus 服务端并行执行，WeightedRanker 加权融合。
        返回的 score 只是排序信号，真正的相关性由 Reranker 打分。
        """
        dense_req = AnnSearchRequest(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"ef": self.ANN_EF}},
            limit=top_k,
            expr=filters,
        )
        sparse_req = AnnSearchRequest(
            data=[query_sparse],
            anns_field="sparse_embedding",
            param={"metric_type": "IP"},
            limit=top_k,
            expr=filters,
        )
        try:
            results = self._client.hybrid_search(
                collection_name=COLLECTION_NAME,
                reqs=[dense_req, sparse_req],
                ranker=WeightedRanker(self.DENSE_WEIGHT, self.SPARSE_WEIGHT),
                limit=top_k,
                output_fields=["content", "source_name", "chunk_type", "doc_type", "document_id", "chunk_index"],
            )
        except Exception as e:
            raise MilvusConnectionError(f"Milvus 混合检索失败: {e}") from e

        candidates = [
            {
                "content": hit["entity"].get("content") or "",
                "score":   hit.get("distance") or 0.0,
                "metadata": {
                    "source_name": hit["entity"].get("source_name") or "",
                    "chunk_type":  hit["entity"].get("chunk_type") or "text",
                    "doc_type":    hit["entity"].get("doc_type") or "",
                    "document_id": hit["entity"].get("document_id") or "",
                    "chunk_index": hit["entity"].get("chunk_index") or 0,
                },
            }
            for hit in results[0]
        ]
        logger.info("knowledge_base.hybrid_search_done", candidates=len(candidates))
        return candidates

    @staticmethod
    def build_filter(tenant_id: str, doc_type: Optional[str] = None) -> str:
        """tenant_id 是必填过滤条件：检索永远不会跨租户。"""
        expr = f'tenant_id == "{_escape(tenant_id)}"'
        if doc_type:
            expr += f' and doc_type == "{_escape(doc_type)}"'
        return expr


def _escape(value: str) -> str:
    """转义双引号，防止 filter 表达式注入。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')

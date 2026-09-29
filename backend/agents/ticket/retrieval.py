# backend/agents/ticket/retrieval.py
# 工单知识检索：Query 策略分类 → 策略化检索 → BGE-M3 混合召回 → Reranker 精排。
#
#   PRECISE  描述具体（有错误码 / 主机名 / 软件名）  → 原文直接检索
#   VAGUE    描述很短很模糊（"电脑坏了"）            → HyDE：先生成一段假设的故障说明再检索
#   BROAD    一张工单报了多个问题                    → Multi-Query：拆成子查询并行检索，合并去重

import asyncio
import re
from typing import Any, Optional

from langchain_core.messages import HumanMessage

from backend.config import get_settings
from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger
from backend.core.retry import register_fallback, with_retry

logger = get_logger(__name__)

RECALL_TOP_K = {"PRECISE": 6, "VAGUE": 10, "BROAD": 4}   # 精排在 CPU 上逐对打分，候选数直接决定延迟
RERANK_TOP_K = 3
MAX_BROAD_QUERIES = 4

_VAGUE_HINTS = ("坏了", "不行", "用不了", "打不开", "上不了", "没反应", "有问题", "异常", "很卡", "连不上")
_BROAD_SPLIT_RE = re.compile(r"(另外|还有|以及|同时|顺便|；|;|\n\s*\d+[.、)])")
_PRECISE_SIGNAL_RE = re.compile(
    r"([A-Za-z]+[-_]?\d+|\d{3,}|error|err|code|0x[0-9a-f]+|\.exe|\.dll|vpn|outlook|oa|erp|ad域)",
    re.IGNORECASE,
)

_STRATEGY_PROMPT = """判断下面这段 IT 工单描述应采用哪种检索策略，只输出一个单词：
- PRECISE：描述具体，包含明确的现象、报错、系统或设备
- VAGUE：描述过于笼统，缺少关键信息
- BROAD：同时包含两个及以上相互独立的问题

工单描述：{query}"""

_HYDE_PROMPT = """你是资深 IT 运维工程师。用户提交了一段很简短的故障描述，
请写一段 150 字以内的"典型故障说明"，包含最可能的现象、报错信息和涉及的系统，用于检索运维手册。
只输出说明本身。

用户描述：{query}"""

_MULTI_QUERY_PROMPT = """下面这张 IT 工单里包含多个问题，请拆成相互独立的检索语句，每行一条，最多 {n} 条，
每条都要自成一句、包含关键对象。只输出检索语句。

工单描述：{query}"""


def rule_strategy(query: str) -> str:
    """规则快判（< 1ms）。"""
    q = query.strip()
    if len(_BROAD_SPLIT_RE.findall(q)) >= 1 and len(q) >= 20:
        return "BROAD"
    if len(q) <= 12 and not _PRECISE_SIGNAL_RE.search(q) and any(h in q for h in _VAGUE_HINTS):
        return "VAGUE"
    return "PRECISE"


async def classify_strategy(query: str) -> str:
    """
    两阶段判定：规则判为 PRECISE 直接返回；判为 VAGUE/BROAD 且描述较长（≥18 字）时再请 LLM 校正，
    短描述直接相信规则（省一次调用）。
    """
    strategy = rule_strategy(query)
    if strategy == "PRECISE" or len(query.strip()) < 18:
        return strategy
    try:
        resp = await get_llm("retrieval").ainvoke([HumanMessage(content=_STRATEGY_PROMPT.format(query=query))])
        label = resp.text.strip().upper()
        if label in ("PRECISE", "VAGUE", "BROAD"):
            return label
    except Exception as e:
        logger.warning("retrieval.strategy_llm_failed", error=repr(e))
    return strategy


async def _hyde(query: str) -> str:
    resp = await get_llm("retrieval", temperature=0.3).ainvoke(
        [HumanMessage(content=_HYDE_PROMPT.format(query=query))]
    )
    return resp.text.strip() or query


async def _multi_query(query: str) -> list[str]:
    resp = await get_llm("retrieval", temperature=0.3).ainvoke(
        [HumanMessage(content=_MULTI_QUERY_PROMPT.format(query=query, n=MAX_BROAD_QUERIES))]
    )
    lines = [ln.lstrip("0123456789.-）)、• ").strip() for ln in resp.text.splitlines()]
    queries = [ln for ln in lines if len(ln) > 3][:MAX_BROAD_QUERIES]
    return queries or [query]


async def _retrieve_one(query: str, tenant_id: str, doc_type: Optional[str], recall_top_k: int):
    from backend.core.reranker import MODEL_EXECUTOR, retrieve
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        MODEL_EXECUTOR,
        lambda: retrieve(query, tenant_id, doc_type, recall_top_k=recall_top_k, rerank_top_k=RERANK_TOP_K),
    )


def _empty_result(query: str) -> dict[str, Any]:
    return {"strategy": "UNAVAILABLE", "queries": [query], "docs": [], "confidence": 0.0,
            "low_confidence": True, "fallback_used": True}


async def search_knowledge(query: str, tenant_id: str, doc_type: Optional[str] = None) -> dict[str, Any]:
    """
    Returns:
        {"strategy", "queries", "docs": [{content, score, source_name, doc_type}], "confidence",
         "low_confidence", "fallback_used"}
    三层兜底后结构始终一致：走到系统级兜底时返回空结果 + low_confidence。
    """
    result = await _search_knowledge(query, tenant_id, doc_type)
    return _empty_result(query) if result.get("system_fallback") else result


@with_retry(agent_type="retrieval", timeout=60)
async def _search_knowledge(query: str, tenant_id: str, doc_type: Optional[str] = None) -> dict[str, Any]:
    strategy = await classify_strategy(query)

    if strategy == "BROAD":
        queries = await _multi_query(query)
        results = await asyncio.gather(
            *[_retrieve_one(q, tenant_id, doc_type, RECALL_TOP_K["BROAD"]) for q in queries]
        )
        seen: dict[str, Any] = {}
        for ranked, _ in results:
            for doc in ranked:
                key = doc.content[:100]
                if key not in seen or doc.score > seen[key].score:
                    seen[key] = doc
        ranked_docs = sorted(seen.values(), key=lambda d: d.score, reverse=True)[:RERANK_TOP_K]
    else:
        queries = [await _hyde(query)] if strategy == "VAGUE" else [query]
        ranked_docs, _ = await _retrieve_one(queries[0], tenant_id, doc_type, RECALL_TOP_K[strategy])

    confidence = ranked_docs[0].score if ranked_docs else 0.0
    threshold = get_settings().retrieval_confidence_threshold
    logger.info("retrieval.done", strategy=strategy, hits=len(ranked_docs),
                confidence=round(confidence, 4), tenant_id=tenant_id)
    return {
        "strategy": strategy,
        "queries": queries,
        "docs": [
            {
                "content": d.content,
                "score": round(d.score, 4),
                "source_name": d.metadata.get("source_name", ""),
                "doc_type": d.metadata.get("doc_type", ""),
            }
            for d in ranked_docs
        ],
        "confidence": round(confidence, 4),
        "low_confidence": confidence < threshold,
        "fallback_used": False,
    }


@register_fallback("retrieval")
async def _retrieval_fallback(query: str, tenant_id: str, doc_type: Optional[str] = None, *, error=None):
    """检索链路不可用：返回空结果并标记低置信度，下游据此转人工，而不是让 Agent 凭空作答。"""
    logger.warning("retrieval.fallback_empty", tenant_id=tenant_id, error=repr(error))
    return _empty_result(query)

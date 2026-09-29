"""构建 IT 运维知识库：运维手册（runbook）+ 历史工单（ticket）→ BGE-M3 双向量 → Milvus。

- 运维手册按二级标题切块，每块带 "手册名 > 章节" 前缀，保证单块自包含
- 每条历史工单一块：现象 + 根因 + 处理过程
- 多租户：data/runbooks 为各租户通用手册，data/runbooks_branch 只写入 it_branch；
  历史工单按自身 tenant_id 写入。tenant_id 是 partition key，检索时强制过滤
- 幂等：按 document_id 记录内容哈希，内容未变的文档直接跳过；变化的先删后插

    python scripts/build_knowledge_base.py [--rebuild]
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.knowledge_base import BGEMEmbedder, DocumentChunk, KnowledgeBaseClient  # noqa: E402

DATA = ROOT / "data"
BATCH_SIZE = 12
TENANT_RUNBOOKS = {
    "it_hq":     [DATA / "runbooks"],
    "it_branch": [DATA / "runbooks", DATA / "runbooks_branch"],
}


def split_runbook(path: Path) -> list[tuple[str, str]]:
    """返回 [(source_name, content)]，按 ## 切块。"""
    text = path.read_text(encoding="utf-8")
    title_match = re.search(r"^# (.+)$", text, re.M)
    title = title_match.group(1).strip() if title_match else path.stem
    sections = re.split(r"^## ", text, flags=re.M)[1:]
    out = []
    for sec in sections:
        heading, _, body = sec.partition("\n")
        source = f"{title} > {heading.strip()}"
        out.append((source, f"【{source}】\n{body.strip()}"))
    return out


def ticket_text(t: dict) -> str:
    return (f"【历史工单 {t['ticket_no']}】{t['title']}\n"
            f"类别：{t['category']}，优先级：{t['priority']}，主机：{t['host'] or '无'}\n"
            f"现象：{t['description']}\n根因：{t['root_cause']}\n处理：{t['resolution']}\n结果：{t['outcome']}")


def collect_documents() -> list[dict]:
    """document = {tenant_id, document_id, doc_type, chunks: [(source_name, content)]}"""
    docs = []
    for tenant, dirs in TENANT_RUNBOOKS.items():
        for d in dirs:
            for path in sorted(d.glob("*.md")):
                docs.append({"tenant_id": tenant, "document_id": f"runbook:{path.stem}",
                             "doc_type": "runbook", "chunks": split_runbook(path)})
    with open(DATA / "history_tickets.jsonl", encoding="utf-8") as f:
        for line in f:
            t = json.loads(line)
            docs.append({"tenant_id": t["tenant_id"], "document_id": f"ticket:{t['ticket_no']}",
                         "doc_type": "ticket", "chunks": [(f"历史工单 {t['ticket_no']}", ticket_text(t))]})
    return docs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true", help="忽略内容哈希，全部重新写入")
    args = parser.parse_args()

    kb = KnowledgeBaseClient()
    docs = collect_documents()
    todo = []
    for doc in docs:
        h = hashlib.md5("\n".join(c for _, c in doc["chunks"]).encode()).hexdigest()
        doc["hash"] = h
        if not args.rebuild and kb.get_document_hash(doc["document_id"], doc["tenant_id"]) == h:
            continue
        todo.append(doc)
    print(f"文档共 {len(docs)} 个，需要写入 {len(todo)} 个")
    if not todo:
        return

    embedder = BGEMEmbedder.get_instance()
    flat = [(doc, i, source, content) for doc in todo for i, (source, content) in enumerate(doc["chunks"])]
    chunks: list[DocumentChunk] = []
    for start in range(0, len(flat), BATCH_SIZE):
        batch = flat[start:start + BATCH_SIZE]
        dense, sparse = embedder.encode([c for *_, c in batch], batch_size=BATCH_SIZE)
        for (doc, i, source, content), dv, sv in zip(batch, dense, sparse):
            chunks.append(DocumentChunk(
                id=KnowledgeBaseClient.generate_chunk_id(content, doc["document_id"], i, doc["tenant_id"]),
                content=content, embedding=dv, sparse_embedding=sv, doc_type=doc["doc_type"],
                document_id=doc["document_id"], source_name=source, chunk_type="text", chunk_index=i,
                version="1.0", content_hash=doc["hash"], tenant_id=doc["tenant_id"],
            ))
        print(f"  嵌入 {min(start + BATCH_SIZE, len(flat))}/{len(flat)}")

    for doc in todo:
        kb.delete_document_chunks(doc["document_id"], doc["tenant_id"])
    for start in range(0, len(chunks), 200):
        kb.upsert_chunks(chunks[start:start + 200])

    by_type: dict[str, int] = {}
    for c in chunks:
        key = f"{c.tenant_id}/{c.doc_type}"
        by_type[key] = by_type.get(key, 0) + 1
    print("写入完成：", by_type)


if __name__ == "__main__":
    main()

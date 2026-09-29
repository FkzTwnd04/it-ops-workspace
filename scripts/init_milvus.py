# scripts/init_milvus.py
# 执行：python scripts/init_milvus.py
# 重建知识库集合（会清空旧数据），之后运行 scripts/build_knowledge_base.py 重新导入知识库。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymilvus import DataType, MilvusClient

from backend.config import get_settings
from backend.core.knowledge_base import COLLECTION_NAME

VECTOR_DIM = 1024          # BGE-M3 稠密向量维度
NUM_PARTITIONS = 16        # tenant_id 哈希到 16 个分区；租户数远多于此时再调大


def build_schema(client: MilvusClient):
    """稠密 + 稀疏双向量；tenant_id 设为 partition key，实现按租户分区（namespace）隔离。"""
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id",               DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("embedding",        DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
    schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field("content",          DataType.VARCHAR, max_length=4096)
    schema.add_field("tenant_id",        DataType.VARCHAR, max_length=64, is_partition_key=True)
    schema.add_field("doc_type",         DataType.VARCHAR, max_length=32)
    schema.add_field("chunk_index",      DataType.INT64)
    schema.add_field("document_id",      DataType.VARCHAR, max_length=64)
    schema.add_field("source_name",      DataType.VARCHAR, max_length=256)
    schema.add_field("chunk_type",       DataType.VARCHAR, max_length=32)
    schema.add_field("version",          DataType.VARCHAR, max_length=32)
    schema.add_field("content_hash",     DataType.VARCHAR, max_length=64)
    schema.add_field("updated_at",       DataType.INT64)
    return schema


def build_index_params(client: MilvusClient):
    ip = client.prepare_index_params()
    ip.add_index(field_name="embedding", index_type="HNSW", metric_type="COSINE",
                 params={"M": 16, "efConstruction": 256})
    ip.add_index(field_name="sparse_embedding", index_type="SPARSE_INVERTED_INDEX",
                 metric_type="IP", params={"drop_ratio_build": 0.2})
    ip.add_index(field_name="doc_type", index_type="INVERTED")
    ip.add_index(field_name="document_id", index_type="INVERTED")
    return ip


def main():
    settings = get_settings()
    uri = f"http://{settings.milvus_host}:{settings.milvus_port}"
    print(f"连接 Milvus：{uri}")
    client = MilvusClient(uri=uri)

    if client.has_collection(COLLECTION_NAME):
        print(f"删除旧集合 '{COLLECTION_NAME}'")
        client.drop_collection(COLLECTION_NAME)

    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=build_schema(client),
        index_params=build_index_params(client),
        num_partitions=NUM_PARTITIONS,
    )
    print(f"集合 '{COLLECTION_NAME}' 创建完成（{NUM_PARTITIONS} 个租户分区，已建索引并加载）")


if __name__ == "__main__":
    main()

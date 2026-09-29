"""Milvus 检索过滤表达式：tenant_id 必填、防注入。"""
from backend.agents.ticket.retrieval import rule_strategy
from backend.core.knowledge_base import KnowledgeBaseClient


def test_filter_always_contains_tenant():
    assert KnowledgeBaseClient.build_filter("it_hq") == 'tenant_id == "it_hq"'


def test_filter_with_doc_type():
    assert KnowledgeBaseClient.build_filter("it_hq", "runbook") == 'tenant_id == "it_hq" and doc_type == "runbook"'


def test_filter_injection_is_escaped():
    expr = KnowledgeBaseClient.build_filter('x" or tenant_id != "x')
    assert expr == 'tenant_id == "x\\" or tenant_id != \\"x"'


def test_chunk_id_differs_across_tenants():
    a = KnowledgeBaseClient.generate_chunk_id("same", "doc", 0, "it_hq")
    b = KnowledgeBaseClient.generate_chunk_id("same", "doc", 0, "it_branch")
    assert a != b
    assert a == KnowledgeBaseClient.generate_chunk_id("same", "doc", 0, "it_hq")


def test_rule_strategy_precise_for_specific_errors():
    assert rule_strategy("VPN 客户端报 TLS 握手失败，错误码 809") == "PRECISE"


def test_rule_strategy_vague_for_short_complaints():
    assert rule_strategy("系统用不了了") == "VAGUE"

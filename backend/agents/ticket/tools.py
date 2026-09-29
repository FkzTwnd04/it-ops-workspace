# backend/agents/ticket/tools.py
# 工单 Agent 的工具集。
#
#   只读工具   → 给子 Agent 用，随便调用不会产生副作用
#   业务写入   → submit_triage / submit_resolution，只写工单数据；状态流转由代码按白名单执行
#   低风险操作 → 直接执行并写 ops_actions 留痕
#   高危操作   → 在 interrupt_on 里注册，调用前必须经运维审批（见 agent.py）
#
# 工具运行在工单流程图的上下文里，ticket_id / tenant_id 从 RunnableConfig 读取，
# 模型无法通过参数指定别的工单或别的租户。

import json
from typing import Literal, Optional

from langchain_core.tools import tool
from langgraph.config import get_config

from backend.agents.ticket import mock_ops
from backend.agents.ticket.retrieval import search_knowledge
from backend.agents.ticket.states import CATEGORIES, Priority, TicketStatus, compute_sla_due
from backend.services import ticket_repo


def _ctx() -> tuple[str, str]:
    cfg = get_config().get("configurable", {})
    return cfg["ticket_id"], cfg["tenant_id"]


def _format_docs(result: dict) -> str:
    if not result["docs"]:
        return f"未检索到相关内容（策略={result['strategy']}，置信度=0）。请不要编造处理方案。"
    blocks = [
        f"[{i}] 来源：{d['source_name']}（相关度 {d['score']}）\n{d['content']}"
        for i, d in enumerate(result["docs"], 1)
    ]
    header = f"检索策略={result['strategy']}，Top-1 置信度={result['confidence']}"
    if result["low_confidence"]:
        header += "（低于阈值：结果仅供参考，结论须标注为不确定）"
    return header + "\n\n" + "\n\n".join(blocks)


# ── 只读工具 ────────────────────────────────────────────────

async def _env():
    ticket_id, tenant_id = _ctx()
    t = await ticket_repo.require_ticket(ticket_id, tenant_id)
    username = await ticket_repo.get_username(t["requester_id"], tenant_id) or "unknown"
    return mock_ops.env_for(ticket_id, f"{t['title']}\n{t['description']}"), username


@tool
async def get_ticket_detail() -> str:
    """获取当前工单的标题、描述、受影响主机、提单人（含域账号）、当前状态和已有分类。"""
    ticket_id, tenant_id = _ctx()
    t = await ticket_repo.require_ticket(ticket_id, tenant_id)
    return json.dumps({
        "ticket_no": t["ticket_no"], "title": t["title"], "description": t["description"],
        "host": t["host"], "requester": t["requester_name"],
        "requester_username": await ticket_repo.get_username(t["requester_id"], tenant_id),
        "status": t["status"], "category": t["category"], "priority": t["priority"],
    }, ensure_ascii=False)


@tool
async def search_knowledge_base(query: str) -> str:
    """在运维手册知识库中检索故障处理方案。query 用一句话描述故障现象。"""
    ticket_id, tenant_id = _ctx()
    result = await search_knowledge(query, tenant_id, doc_type="runbook")
    await ticket_repo.record_retrieval(ticket_id, tenant_id, query, "runbook", result)
    return _format_docs(result)


@tool
async def search_similar_tickets(query: str) -> str:
    """检索本部门已解决的相似历史工单，参考它们的根因和处理过程。"""
    ticket_id, tenant_id = _ctx()
    result = await search_knowledge(query, tenant_id, doc_type="ticket")
    await ticket_repo.record_retrieval(ticket_id, tenant_id, query, "ticket", result)
    return _format_docs(result)


@tool
async def query_system_logs(host: str, keyword: str = "", minutes: int = 60) -> str:
    """查询指定主机最近 minutes 分钟的系统日志，可按 keyword 过滤。
    常用主机：dc01（域控/账号）、vpn-gw-01（VPN）、mail-01（邮件）、print-srv-01（打印）、erp-app-01（ERP）。"""
    env, username = await _env()
    return "\n".join(mock_ops.query_logs(env, host, username, keyword, minutes))


@tool
async def check_service_status(host: str, service: str = "") -> str:
    """查询主机上服务的运行状态（running / degraded）。"""
    env, _ = await _env()
    return json.dumps(mock_ops.service_status(env, host, service), ensure_ascii=False)


@tool
async def get_user_account(username: str) -> str:
    """查询域账号状态：是否锁定、密码是否过期、近 24 小时登录失败次数、所属组。username 用域账号（英文）。"""
    env, _ = await _env()
    return json.dumps(mock_ops.account_status(env, username), ensure_ascii=False)


# ── 业务写入（主 Agent）──────────────────────────────────────

@tool
async def submit_triage(
    category: Literal["account", "network", "hardware", "software", "permission", "email", "other"],
    priority: Literal["P1", "P2", "P3", "P4"],
    summary: str,
    reason: str,
) -> str:
    """提交工单分类结果。分类完成后工单自动进入"处理中"。

    Args:
        category: 工单类别
        priority: P1 核心业务中断 / P2 多人受影响 / P3 单人受影响 / P4 咨询
        summary: 一句话概括问题
        reason: 判断依据
    """
    ticket_id, tenant_id = _ctx()
    ticket = await ticket_repo.require_ticket(ticket_id, tenant_id)
    triage = {"category": category, "priority": priority, "summary": summary, "reason": reason}
    fields = {
        "category": category,
        "priority": priority,
        "triage": triage,
        "sla_due_at": compute_sla_due(Priority(priority), ticket["created_at"]),
    }
    if ticket["status"] == TicketStatus.NEW.value:
        await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.TRIAGED, "ticket_agent",
                                     f"{CATEGORIES[category]} / {priority}", **fields)
        await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.IN_PROGRESS, "ticket_agent",
                                     "分类完成，开始诊断")
    else:
        await ticket_repo.update_fields(ticket_id, tenant_id, **fields)
    return f"已记录分类：{CATEGORIES[category]} / {priority}，SLA 已按优先级重新计算。"


@tool
async def submit_resolution(
    resolved: bool,
    root_cause: str,
    summary: str,
    steps_taken: list[str],
    user_actions: list[str],
    confidence: float,
) -> str:
    """提交处理结论（必须在结束前调用一次）。是否自动结案由系统规则判定，不由本工具决定。

    Args:
        resolved: 问题是否已经解决
        root_cause: 根因
        summary: 给提单人看的处理说明
        steps_taken: 已执行的操作
        user_actions: 需要提单人自己完成的步骤（没有则为空列表）
        confidence: 对结论的把握，0 到 1
    """
    ticket_id, tenant_id = _ctx()
    await ticket_repo.update_fields(ticket_id, tenant_id, resolution={
        "resolved": resolved,
        "root_cause": root_cause,
        "summary": summary,
        "steps_taken": steps_taken,
        "user_actions": user_actions,
        "confidence": max(0.0, min(1.0, confidence)),
    })
    return "处理结论已提交。"


# ── 运维操作 ────────────────────────────────────────────────

async def _execute(tool_name: str, args: dict, result: str) -> str:
    ticket_id, tenant_id = _ctx()
    env, _ = await _env()
    mock_ops.apply_action(env, tool_name, args)
    await ticket_repo.record_ops_action(ticket_id, tenant_id, tool_name, args, result)
    return result


@tool
async def unlock_account(username: str) -> str:
    """解锁被锁定的域账号（低风险，直接执行并留痕）。"""
    return await _execute("unlock_account", {"username": username}, f"账号 {username} 已解锁")


@tool
async def clear_print_queue(host: str) -> str:
    """清空打印服务器上卡住的打印队列（低风险，直接执行并留痕）。"""
    return await _execute("clear_print_queue", {"host": host}, f"{host} 打印队列已清空")


@tool
async def restart_service(host: str, service: str, reason: str) -> str:
    """重启主机上的服务（高危：会中断该服务的所有用户，执行前需运维审批）。"""
    return await _execute("restart_service", {"host": host, "service": service, "reason": reason},
                          f"{host} 上的 {service} 已重启，状态 running")


@tool
async def reset_password(username: str, reason: str) -> str:
    """重置域账号密码并强制下次登录修改（高危，执行前需运维审批）。"""
    return await _execute("reset_password", {"username": username, "reason": reason},
                          f"账号 {username} 密码已重置，临时密码已通过短信发送给本人")


@tool
async def grant_permission(username: str, resource: str, level: Literal["read", "write", "admin"],
                           reason: str) -> str:
    """为账号授予系统或共享目录权限（高危，执行前需运维审批）。"""
    return await _execute("grant_permission",
                          {"username": username, "resource": resource, "level": level, "reason": reason},
                          f"已授予 {username} 对 {resource} 的 {level} 权限")


HIGH_RISK_TOOLS: dict[str, str] = {
    "restart_service":  "重启服务会中断该服务的全部在线用户",
    "reset_password":   "重置密码会让该账号所有已登录会话失效",
    "grant_permission": "权限变更需符合最小权限原则并留存审批依据",
}

READONLY_TOOLS = [get_ticket_detail, search_knowledge_base, search_similar_tickets,
                  query_system_logs, check_service_status, get_user_account]
MAIN_AGENT_TOOLS = [submit_triage, submit_resolution, check_service_status,
                    unlock_account, clear_print_queue, restart_service, reset_password, grant_permission]


def ticket_config(ticket_id: str, tenant_id: str, extra: Optional[dict] = None) -> dict:
    """工单流程图的 RunnableConfig：thread_id 固定为工单 ID，重启后可按工单恢复。"""
    return {
        "configurable": {
            "thread_id": f"ticket-{ticket_id}",
            "ticket_id": ticket_id,
            "tenant_id": tenant_id,
            **(extra or {}),
        },
        "recursion_limit": 200,
    }

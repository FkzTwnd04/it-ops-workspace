# backend/agents/ticket/agent.py
# 工单 Deep Agent：主 Agent 负责规划（write_todos）、委派（task）和执行运维操作，
# 4 个子 Agent 在隔离上下文中完成分类 / 检索 / 日志分析 / 方案生成，只把结论返回给主 Agent。
#
# 本图不带 checkpointer，作为外层工单流程图的一个节点运行，继承外层的 PostgreSQL Checkpoint；
# 高危工具触发的 interrupt 会冒泡到外层图，由审批接口 Command(resume=...) 恢复。

from pathlib import Path

from deepagents import FilesystemPermission, SubAgent, create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain.agents.middleware import ModelCallLimitMiddleware, ModelRetryMiddleware

from backend.agents.ticket import prompts
from backend.agents.ticket.tools import (
    HIGH_RISK_TOOLS,
    MAIN_AGENT_TOOLS,
    check_service_status,
    get_user_account,
    query_system_logs,
    search_knowledge_base,
    search_similar_tickets,
)
from backend.core.llm_factory import get_llm

SKILLS_DIR = Path(__file__).parent / "skills"
SKILLS_ROUTE = "/skills/"


def _retry() -> ModelRetryMiddleware:
    # 第 1 层降级：模型调用失败按 1s / 3s 退避重试两次，仍失败则抛给外层流程图走第 2 层
    return ModelRetryMiddleware(max_retries=2, initial_delay=1.0, backoff_factor=3.0,
                                jitter=False, on_failure="error")


def _subagents() -> list[SubAgent]:
    sub_model = get_llm("ticket_subagent")
    return [
        {
            "name": "ticket-classifier",
            "description": "读取工单并给出类别、优先级和一句话摘要（JSON）",
            "system_prompt": prompts.CLASSIFIER_PROMPT,
            "tools": [],
            "model": sub_model,
            "middleware": [_retry()],
        },
        {
            "name": "kb-researcher",
            "description": "检索运维手册和相似历史工单，返回有出处的处理依据和检索置信度",
            "system_prompt": prompts.KB_RESEARCHER_PROMPT,
            "tools": [search_knowledge_base, search_similar_tickets],
            "model": sub_model,
            "middleware": [_retry()],
        },
        {
            "name": "log-analyst",
            "description": "查询主机日志、服务状态和域账号状态，定位故障根因",
            "system_prompt": prompts.LOG_ANALYST_PROMPT,
            "tools": [query_system_logs, check_service_status, get_user_account],
            "model": sub_model,
            "middleware": [_retry()],
        },
        {
            "name": "solution-planner",
            "description": "结合检索结论、日志结论和标准处理流程（skills）生成处理方案",
            "system_prompt": prompts.SOLUTION_PLANNER_PROMPT,
            "tools": [],
            "skills": [SKILLS_ROUTE],
            "model": sub_model,
            "middleware": [_retry()],
        },
    ]


def build_ticket_agent():
    backend = CompositeBackend(
        default=StateBackend(),
        routes={SKILLS_ROUTE: FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True)},
    )
    interrupt_on = {
        name: {"allowed_decisions": ["approve", "edit", "reject"], "description": risk}
        for name, risk in HIGH_RISK_TOOLS.items()
    }
    return create_deep_agent(
        name="ticket-agent",
        model=get_llm("ticket_agent"),
        tools=MAIN_AGENT_TOOLS,
        system_prompt=prompts.MAIN_AGENT_PROMPT,
        subagents=_subagents(),
        backend=backend,
        permissions=[FilesystemPermission(operations=["write"], paths=["/skills/**"], mode="deny")],
        interrupt_on=interrupt_on,
        middleware=[
            _retry(),
            ModelCallLimitMiddleware(run_limit=40, exit_behavior="error"),
        ],
    )

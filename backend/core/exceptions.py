# backend/core/exceptions.py
# 统一异常体系：所有自定义异常都继承同一个基类，
# 便于「一次捕获全部」以及按「可重试 / 不可重试」分类处理（见 core/retry.py）。


class TicketAgentError(Exception):
    """所有自定义异常的基类。比普通异常多带两样上下文：出错的组件、以及任意细节字典。"""
    def __init__(self, message: str, agent_type: str = "", details: dict = None):
        super().__init__(message)
        self.agent_type = agent_type       # 出错的组件，如 ticket_agent / retrieval
        self.details = details or {}


class LLMAPIError(TicketAgentError):
    """大模型 API 调用失败（超时 / 限流 / 网络错误）。属于【可重试】异常。"""


class MilvusConnectionError(TicketAgentError):
    """Milvus 向量库连接失败。属于【可重试】异常。"""


class InvalidInputError(TicketAgentError):
    """输入不合法。属于【不可重试】异常（重试也不会变合法）。"""


class AuthenticationError(TicketAgentError):
    """认证失败。属于【不可重试】异常。"""

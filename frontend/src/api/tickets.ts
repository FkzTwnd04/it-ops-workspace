import client from './client'

export type TicketStatus =
  | 'NEW' | 'TRIAGED' | 'IN_PROGRESS' | 'PENDING_APPROVAL'
  | 'PENDING_CONFIRM' | 'RESOLVED' | 'CLOSED' | 'ESCALATED'

export type PendingKind = 'approval' | 'user_confirm' | 'human_resolve' | null

export interface Resolution {
  resolved?: boolean
  root_cause?: string
  summary?: string
  steps_taken?: string[]
  user_actions?: string[]
  confidence?: number
  human_summary?: string
  resolved_by?: string
  references?: { source: string; score: number; content: string }[]
}

export interface Ticket {
  id: string
  ticket_no: string
  tenant_id: string
  title: string
  description: string
  requester_id: string
  requester_name: string | null
  host: string | null
  category: string | null
  priority: string
  status: TicketStatus
  sla_due_at: string | null
  escalation_reason: string | null
  triage: { summary?: string; reason?: string } | null
  resolution: Resolution | null
  auto_resolved: boolean
  fallback_used: boolean
  retrieval_confidence: number | null
  reopen_count: number
  created_at: string
  updated_at: string
  resolved_at: string | null
  closed_at: string | null
}

export interface TicketEvent {
  id: number
  from_status: TicketStatus | null
  to_status: TicketStatus
  actor: string
  reason: string | null
  created_at: string
}

export interface Approval {
  id: string
  ticket_id: string
  interrupt_id: string
  seq: number
  tool_name: string
  tool_args: Record<string, unknown>
  risk_note: string | null
  status: 'pending' | 'approved' | 'edited' | 'rejected'
  final_args: Record<string, unknown> | null
  reviewer_name: string | null
  comment: string | null
  requested_at: string
  decided_at: string | null
  ticket_no: string
  title: string
  priority: string
  ticket_status: TicketStatus
}

export interface OpsAction {
  tool_name: string
  args: Record<string, unknown>
  result: string
  executed_at: string
}

export interface Retrieval {
  query: string
  doc_type: string | null
  strategy: string | null
  confidence: number
  docs: { source_name?: string; score?: number; content?: string }[]
  created_at: string
}

export interface TicketDetail {
  ticket: Ticket
  events: TicketEvent[]
  approvals: Approval[]
  ops_actions: OpsAction[]
  retrievals: Retrieval[]
  pending: PendingKind
  running: boolean
}

export interface Decision {
  approval_id: string
  type: 'approve' | 'edit' | 'reject'
  args?: Record<string, unknown>
  comment?: string
}

export interface Stats {
  by_status: Record<string, number>
  processed: number
  finished: number
  auto_resolve_rate: number | null
  escalation_rate: number | null
  approval_rate: number | null
  runs: number
  fallback_rate: number | null
  latency_p50_ms: number | null
  latency_p95_ms: number | null
}

export const STATUS_META: Record<TicketStatus, { label: string; type: '' | 'info' | 'success' | 'warning' | 'danger' | 'primary' }> = {
  NEW: { label: '新建', type: 'info' },
  TRIAGED: { label: '已分类', type: 'info' },
  IN_PROGRESS: { label: '处理中', type: 'primary' },
  PENDING_APPROVAL: { label: '待审批', type: 'warning' },
  PENDING_CONFIRM: { label: '待用户确认', type: 'warning' },
  RESOLVED: { label: '已解决', type: 'success' },
  CLOSED: { label: '已关闭', type: 'success' },
  ESCALATED: { label: '已升级', type: 'danger' },
}

export const CATEGORY_LABELS: Record<string, string> = {
  account: '账号与密码',
  network: '网络与 VPN',
  hardware: '硬件与外设',
  software: '软件安装与故障',
  permission: '权限申请',
  email: '邮箱与协作',
  other: '其他',
}

export const TOOL_LABELS: Record<string, string> = {
  restart_service: '重启服务',
  reset_password: '重置密码',
  grant_permission: '授予权限',
  unlock_account: '解锁账号',
  clear_print_queue: '清空打印队列',
  search_knowledge_base: '检索运维手册',
  search_similar_tickets: '检索历史工单',
  query_system_logs: '查询日志',
  check_service_status: '查询服务状态',
  get_user_account: '查询账号状态',
  submit_triage: '提交分类',
  submit_resolution: '提交结论',
  read_file: '读取 Skill',
  ls: '列出 Skills',
}

export const AGENT_LABELS: Record<string, string> = {
  'ticket-agent': '主 Agent',
  'ticket-classifier': '分类 Agent',
  'kb-researcher': '检索 Agent',
  'log-analyst': '日志分析 Agent',
  'solution-planner': '方案规划 Agent',
  'general-purpose': '通用子 Agent',
  subagent: '子 Agent',
}

export const ticketsApi = {
  list: (params: { status?: string; mine?: boolean; limit?: number; offset?: number } = {}) =>
    client.get<Ticket[]>('/tickets', { params }),
  create: (data: { title: string; description: string; host?: string; auto_run?: boolean }) =>
    client.post<Ticket>('/tickets', data),
  detail: (id: string) => client.get<TicketDetail>(`/tickets/${id}`),
  run: (id: string) => client.post(`/tickets/${id}/run`),
  confirm: (id: string, resolved: boolean, feedback?: string) =>
    client.post(`/tickets/${id}/confirm`, { resolved, feedback }),
  resolve: (id: string, action: 'resolve' | 'retry', summary?: string, comment?: string) =>
    client.post(`/tickets/${id}/resolve`, { action, summary, comment }),
  decide: (id: string, decisions: Decision[]) =>
    client.post(`/tickets/${id}/approvals`, { decisions }),
  pendingApprovals: () => client.get<Approval[]>('/approvals', { params: { status: 'pending' } }),
  approvalHistory: () => client.get<Approval[]>('/approvals', { params: { status: '' } }),
  stats: () => client.get<Stats>('/stats'),
}

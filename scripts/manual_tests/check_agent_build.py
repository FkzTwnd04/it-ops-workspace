"""手动检查：Deep Agent 能否构建、skills 目录能否通过 CompositeBackend 读到。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

from backend.agents.ticket.agent import SKILLS_DIR, SKILLS_ROUTE, build_ticket_agent

agent = build_ticket_agent()
print("nodes:", list(agent.get_graph().nodes)[:12])

backend = CompositeBackend(default=StateBackend(),
                           routes={SKILLS_ROUTE: FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True)})
print("ls:", backend.ls("/skills/"))

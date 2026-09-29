"""手动检查：主 Agent / 子 Agent 两个模型在 SelectorEventLoop 下的连通率。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.llm_factory import get_llm

ROUNDS = 6


async def main():
    for route in ("ticket_agent", "ticket_subagent"):
        ok = 0
        for i in range(ROUNDS):
            try:
                await get_llm(route).ainvoke("回复 ok")
                ok += 1
            except Exception as e:
                cause = e.__cause__
                print(f"  {route} #{i} FAIL {type(e).__name__}: {cause!r}")
            await asyncio.sleep(2)
        print(f"{route}: {ok}/{ROUNDS}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())

"""启动后端：python run.py [--reload]

psycopg 的异步连接不支持 Windows 默认的 ProactorEventLoop，而 uvicorn 在 Windows 上会强制使用它，
所以这里自己建 SelectorEventLoop，再以 loop="none" 交给 uvicorn。
"""
import asyncio
import sys

import uvicorn

from backend.config import get_settings


def main() -> None:
    settings = get_settings()
    config = uvicorn.Config("backend.main:app", host="0.0.0.0", port=settings.app_port, loop="none")
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()

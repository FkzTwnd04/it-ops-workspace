# backend/main.py

import os
import sys

# Windows conda 专用：把 base 的 Library/bin 加入 DLL 搜索路径，
# 让 _lzma.pyd 能找到 liblzma.dll（非 Windows 跳过，不影响 Linux/Mac）
if sys.platform == "win32":
    _conda_base_lib_bin = os.path.normpath(
        os.path.join(os.path.dirname(sys.executable), "..", "..", "Library", "bin")
    )
    if os.path.isdir(_conda_base_lib_bin):
        os.add_dll_directory(_conda_base_lib_bin)

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.router import api_router
from backend.config import get_settings
from backend.core.logger import configure_logging, get_logger

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger = get_logger(__name__)
    logger.info("app.starting", env=settings.app_env, port=settings.app_port)

    # ① 业务表迁移（幂等）
    from backend.db.migrations import run_migrations
    await run_migrations()

    # ② PostgreSQL Checkpoint：工单流程的每一步都持久化，服务重启后按 thread_id 恢复
    from backend.core.checkpointer import close_checkpointer, init_checkpointer
    await init_checkpointer()

    # ③ 预热本地模型（BGE-M3 / Reranker），避免首个工单卡在加载模型上
    try:
        from backend.core.knowledge_base import BGEMEmbedder
        from backend.core.reranker import MODEL_EXECUTOR, BGEReranker
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(MODEL_EXECUTOR, BGEMEmbedder.get_instance)
        await loop.run_in_executor(MODEL_EXECUTOR, BGEReranker.get_instance)
        logger.info("app.local_models_warmed_up")
    except Exception as e:
        logger.warning("app.local_models_warmup_failed", error=str(e))

    # ④ SLA 巡检后台任务
    from backend.services.sla import sla_loop
    stop = asyncio.Event()
    sla_task = asyncio.create_task(sla_loop(stop))

    logger.info("app.started")
    yield

    logger.info("app.shutting_down")
    stop.set()
    await sla_task
    from backend.services.locks import close_redis
    await close_redis()
    await close_checkpointer()
    from backend.core.llm_factory import LLMFactory
    LLMFactory.clear_cache()
    logger.info("app.shutdown_complete")


app = FastAPI(
    title="IT 智能工单系统 API",
    description="基于 Deep Agents 的 IT 服务台工单自动处理",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://localhost:5173", "http://localhost:8080",
        "http://127.0.0.1:3000", "http://127.0.0.1:5173", "http://127.0.0.1:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["系统"])
async def health_check():
    return {"status": "ok", "env": settings.app_env}

# backend/api/router.py
# API 路由总入口

from fastapi import APIRouter
from backend.api.v1 import auth, tickets, stats

api_router = APIRouter()

api_router.include_router(auth.router,              prefix="/auth",      tags=["认证"])
api_router.include_router(tickets.router,           prefix="/tickets",   tags=["工单"])
api_router.include_router(tickets.approvals_router, prefix="/approvals", tags=["审批"])
api_router.include_router(stats.router,             prefix="/stats",     tags=["统计"])

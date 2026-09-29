# backend/api/v1/stats.py

from fastapi import APIRouter, Depends

from backend.dependencies import require_roles
from backend.services import ticket_repo

router = APIRouter()


@router.get("")
async def get_stats(user: dict = Depends(require_roles("ops", "admin"))):
    return await ticket_repo.stats(user["tenant_id"])

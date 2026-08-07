"""Route modules."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import agents, capabilities, conversations, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(agents.router)
api_router.include_router(capabilities.router)
api_router.include_router(conversations.router)

__all__ = ["api_router"]

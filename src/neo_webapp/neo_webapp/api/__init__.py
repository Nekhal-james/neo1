from fastapi import APIRouter

from .campus import router as campus_router
from .routes import router as routes_router

api_router = APIRouter()
api_router.include_router(routes_router)
api_router.include_router(campus_router)

__all__ = ["api_router"]

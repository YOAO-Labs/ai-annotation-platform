from fastapi import APIRouter

from app.assembly.builders import mount_api_v1_routers
from app.assembly.modules.platform import (
    _e2e_seed_routes_enabled as _platform_e2e_seed_routes_enabled,
)
from app.config import settings as _settings


def _e2e_seed_routes_enabled() -> bool:
    return _platform_e2e_seed_routes_enabled(_settings)


api_router = APIRouter()
mount_api_v1_routers(api_router, _settings)

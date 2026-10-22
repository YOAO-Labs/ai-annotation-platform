"""The development seed must refuse production before any engine is created."""

from pathlib import Path
import subprocess
import sys


def test_production_import_refuses_before_engine_creation():
    root = Path(__file__).resolve().parents[3]
    code = """
import runpy
from app.config import settings
import sqlalchemy.ext.asyncio
settings.environment = 'production'
def unexpected_engine(*args, **kwargs):
    raise AssertionError('production guard created an engine')
sqlalchemy.ext.asyncio.create_async_engine = unexpected_engine
try:
    runpy.run_path('scripts/seed_scale.py', run_name='__main__')
except SystemExit as exc:
    assert exc.code == 2
else:
    raise AssertionError('production guard did not refuse the seed')
print('guard-before-engine-ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root / "apps/api",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "guard-before-engine-ok" in result.stdout

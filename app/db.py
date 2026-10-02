"""PostgreSQL 连接池管理（psycopg3）。"""
import logging
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

logger = logging.getLogger("caltrace")

_pool: ConnectionPool | None = None


def _build_dsn() -> str:
    return (
        f"host={settings.db_host} port={settings.db_port} "
        f"dbname={settings.db_name} user={settings.db_user} "
        f"password={settings.db_password}"
    )


def init_pool() -> ConnectionPool:
    """创建连接池，并在数据库尚未就绪时重试等待。"""
    global _pool
    if _pool is not None:
        return _pool

    deadline = time.monotonic() + settings.db_connect_timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pool = ConnectionPool(
                _build_dsn(),
                min_size=settings.db_min_size,
                max_size=settings.db_max_size,
                kwargs={"row_factory": dict_row},
                open=True,
                timeout=10,
            )
            # 确认连接真正可用
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _pool = pool
            logger.info("database connection pool ready")
            return pool
        except Exception as exc:  # 数据库容器可能仍在初始化
            last_err = exc
            logger.warning("waiting for database: %s", exc)
            time.sleep(2)
    raise RuntimeError(f"database not ready after {settings.db_connect_timeout}s: {last_err}")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool not initialized")
    return _pool

"""Pool de conexões Postgres (asyncpg) com o banco Supabase.

Um único pool de módulo, criado no lifespan do FastAPI (ver `app/main.py`)
e fechado no shutdown. Nada aqui assume nenhuma tabela específica — o
schema vive em `supabase/migrations/`.
"""

import asyncpg

from app.config import get_settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL não configurada (ver .env.example).")
        _pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None

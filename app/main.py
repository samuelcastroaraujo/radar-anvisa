"""Ponto de entrada da API FastAPI.

M1 traz só o essencial (esqueleto + `/health`). Os endpoints de produto
(`/chat`, `/normas`, `/timeline`, `/consultas-publicas`, `/alertas`) chegam
nos milestones que dependem deles (M2 em diante) — ver seção 9 do briefing
e `CLAUDE.md` para o estado atual de cada um.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import close_pool, get_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.database_url:
        await get_pool()
    yield
    await close_pool()


app = FastAPI(title="RADAR ANVISA", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness simples. O `/health` completo (status por fonte de ingestão,
    seção 9 do briefing) só faz sentido a partir do M5, quando `job_execucao`
    passa a ser alimentada de verdade."""
    return {"status": "ok"}

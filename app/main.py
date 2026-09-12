"""Ponto de entrada da API FastAPI.

`/chat` chegou no M4. Os demais endpoints de produto (`/normas`,
`/timeline`, `/consultas-publicas`, `/alertas`) chegam nos milestones que
dependem deles — ver seção 9 do briefing e `CLAUDE.md` para o estado atual
de cada um.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.chat import responder
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


class PerguntaChat(BaseModel):
    mensagem: str
    thread_id: str | None = None


class NormaCitadaResponse(BaseModel):
    id: str
    tipo_ato: str
    numero: str
    ano: int
    status_vigencia: str
    url_origem: str


class RespostaChatAPI(BaseModel):
    resposta: str
    fontes: list[str]
    normas: list[NormaCitadaResponse]


@app.post("/chat")
async def chat(payload: PerguntaChat) -> RespostaChatAPI:
    """RAG regulatório — seção 7 do briefing. Sem `thread_id`/memória de
    conversa ainda (cada pergunta é respondida isolada); é um ponto de
    extensão futuro, não bloqueia o M4."""
    if not payload.mensagem.strip():
        raise HTTPException(status_code=422, detail="mensagem vazia")
    pool = await get_pool()
    resultado = await responder(pool, payload.mensagem)
    return RespostaChatAPI(
        resposta=resultado.resposta,
        fontes=resultado.fontes,
        normas=[
            NormaCitadaResponse(
                id=n.id,
                tipo_ato=n.tipo_ato,
                numero=n.numero,
                ano=n.ano,
                status_vigencia=n.status_vigencia,
                url_origem=n.url_origem,
            )
            for n in resultado.normas
        ],
    )

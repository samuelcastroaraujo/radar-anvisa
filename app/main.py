"""Ponto de entrada da API FastAPI.

`/chat` chegou no M4. Os demais endpoints de produto (`/normas`,
`/timeline`, `/consultas-publicas`, `/alertas`) chegam nos milestones que
dependem deles — ver seção 9 do briefing e `CLAUDE.md` para o estado atual
de cada um.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.chat import responder
from app.config import get_settings
from app.db import close_pool, get_pool
from app.scheduler import iniciar_scheduler
from app.timeline import buscar_consultas_publicas, buscar_status_fontes, buscar_timeline


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    scheduler = None
    if settings.database_url:
        await get_pool()
        if settings.scheduler_habilitado:
            scheduler = iniciar_scheduler()
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    await close_pool()


app = FastAPI(title="RADAR ANVISA", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness simples, sem tocar no banco — pra não confundir "API no ar"
    com "fontes de dados saudáveis" (isso é o `/health/fontes` abaixo)."""
    return {"status": "ok"}


class StatusFonteResponse(BaseModel):
    fonte: str
    status: str | None
    iniciado_em: datetime
    terminado_em: datetime | None
    itens_novos: int
    erro: str | None


@app.get("/health/fontes")
async def health_fontes() -> list[StatusFonteResponse]:
    """Última execução de cada fonte de ingestão (`job_execucao`) — o
    `/health` completo da seção 9: a API pode estar de pé com o scheduler
    falhando silenciosamente em algum job, isso aqui é o que expõe isso."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        status = await buscar_status_fontes(conn)
    return [StatusFonteResponse(**vars(s)) for s in status]


class ItemTimelineResponse(BaseModel):
    tipo: str
    titulo: str
    data: datetime
    url: str | None
    status_vigencia: str | None


@app.get("/timeline")
async def timeline(dias: int = 30, limite: int = 100) -> list[ItemTimelineResponse]:
    """Linha do tempo unificada: normas publicadas, notícias, consultas
    públicas e matérias do DOU nos últimos `dias` dias (seção 9)."""
    if dias <= 0 or dias > 3650:
        raise HTTPException(status_code=422, detail="dias deve estar entre 1 e 3650")
    if limite <= 0 or limite > 500:
        raise HTTPException(status_code=422, detail="limite deve estar entre 1 e 500")
    pool = await get_pool()
    async with pool.acquire() as conn:
        itens = await buscar_timeline(conn, dias=dias, limite=limite)
    return [ItemTimelineResponse(**vars(i)) for i in itens]


class ConsultaPublicaResponse(BaseModel):
    titulo: str
    assunto: str | None
    data_dou: datetime | None
    prazo_inicio: date | None
    prazo_fim: date | None
    url: str
    aberta: bool


@app.get("/consultas-publicas")
async def consultas_publicas(apenas_abertas: bool = True) -> list[ConsultaPublicaResponse]:
    """Consultas públicas ativas do módulo 630, com o prazo de contribuição
    (seção 9). `apenas_abertas=false` também traz as já encerradas."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        cps = await buscar_consultas_publicas(conn, apenas_abertas=apenas_abertas)
    return [ConsultaPublicaResponse(**vars(cp)) for cp in cps]


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

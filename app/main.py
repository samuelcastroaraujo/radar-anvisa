"""Ponto de entrada da API FastAPI.

`/chat` chegou no M4. Os demais endpoints de produto (`/normas`,
`/timeline`, `/consultas-publicas`, `/alertas`) chegam nos milestones que
dependem deles — ver seção 9 do briefing e `CLAUDE.md` para o estado atual
de cada um.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from app.alertas import (
    CANAIS_VALIDOS,
    criar_regra,
    definir_ativo,
    excluir_regra,
    listar_regras,
)
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


app = FastAPI(title="RADAR ANVISA", version="0.1.1", lifespan=lifespan)


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


DATA_MINIMA_TIMELINE = date(1900, 1, 1)


@app.get("/timeline")
async def timeline(
    dias: int = 30,
    limite: int = 100,
    q: str | None = None,
    data_inicio: date | None = None,
    data_fim: date | None = None,
) -> list[ItemTimelineResponse]:
    """Linha do tempo unificada: normas publicadas, notícias, consultas
    públicas e matérias do DOU (seção 9).

    Duas formas de escolher o período, seção 9 + pedido do usuário de poder
    "selecionar por data que eu quiser": `dias` (janela relativa a hoje,
    comportamento original) OU `data_inicio`/`data_fim` explícitos — se
    qualquer um dos dois for passado, `dias` é ignorado e o lado que faltar
    vira aberto (início bem antigo / fim hoje). `q` filtra por
    substring case-insensitive contra o título (número/tipo do ato pra
    norma, ex.: "243" acha RDC 243, IN 243 etc. — ver `buscar_timeline`).
    """
    if dias <= 0 or dias > 3650:
        raise HTTPException(status_code=422, detail="dias deve estar entre 1 e 3650")
    if limite <= 0 or limite > 500:
        raise HTTPException(status_code=422, detail="limite deve estar entre 1 e 500")
    hoje = date.today()
    if data_inicio is not None or data_fim is not None:
        inicio = data_inicio or DATA_MINIMA_TIMELINE
        fim = data_fim or hoje
    else:
        inicio = hoje - timedelta(days=dias)
        fim = hoje
    if inicio > fim:
        raise HTTPException(status_code=422, detail="data_inicio não pode ser depois de data_fim")
    termo = q.strip() if q and q.strip() else None
    pool = await get_pool()
    async with pool.acquire() as conn:
        itens = await buscar_timeline(
            conn, data_inicio=inicio, data_fim=fim, limite=limite, q=termo
        )
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


class RegraAlertaCriar(BaseModel):
    nome: str
    termos: list[str] = []
    temas: list[str] = []
    canais: list[str]
    destino: dict[str, str]

    @field_validator("canais")
    @classmethod
    def _valida_canais(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("pelo menos um canal é obrigatório")
        invalidos = set(v) - CANAIS_VALIDOS
        if invalidos:
            raise ValueError(
                f"canais inválidos: {sorted(invalidos)} (válidos: {sorted(CANAIS_VALIDOS)})"
            )
        return v


class RegraAlertaResponse(BaseModel):
    id: str
    nome: str
    termos: list[str]
    temas: list[str]
    canais: list[str]
    destino: dict
    ativo: bool


class AtivoUpdate(BaseModel):
    ativo: bool


def _validar_destino(canais: list[str], destino: dict[str, str]) -> None:
    """`destino` depende do canal — ver `app/alertas.py` pros campos que
    cada `enviar_*` espera. Falha aqui é bem melhor que só descobrir no
    `job_execucao` de amanhã que o alerta não tinha pra onde mandar."""
    faltando = []
    if "webhook" in canais and not destino.get("url"):
        faltando.append("destino.url (canal webhook)")
    if "email" in canais and not destino.get("email"):
        faltando.append("destino.email (canal email)")
    if "telegram" in canais and not destino.get("chat_id"):
        faltando.append("destino.chat_id (canal telegram)")
    if faltando:
        raise HTTPException(status_code=422, detail=f"destino incompleto: {', '.join(faltando)}")


@app.post("/alertas", status_code=201)
async def criar_alerta(payload: RegraAlertaCriar) -> RegraAlertaResponse:
    """Cria uma regra de alerta (seção "Alertas" do briefing) — casada
    contra normas/notícias/consultas públicas/DOU novos a cada execução
    diária do scheduler (`scripts/verificar_alertas.py`)."""
    _validar_destino(payload.canais, payload.destino)
    pool = await get_pool()
    async with pool.acquire() as conn:
        regra = await criar_regra(
            conn, payload.nome, payload.termos, payload.temas, payload.canais, payload.destino
        )
    return RegraAlertaResponse(**vars(regra))


@app.get("/alertas")
async def listar_alertas() -> list[RegraAlertaResponse]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        regras = await listar_regras(conn)
    return [RegraAlertaResponse(**vars(r)) for r in regras]


@app.patch("/alertas/{alerta_id}")
async def atualizar_alerta(alerta_id: str, payload: AtivoUpdate) -> dict[str, str]:
    """Único campo editável por ora é `ativo` — ligar/desligar uma regra
    sem precisar recriá-la."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        achou = await definir_ativo(conn, alerta_id, payload.ativo)
    if not achou:
        raise HTTPException(status_code=404, detail="alerta não encontrado")
    return {"status": "ok"}


@app.delete("/alertas/{alerta_id}", status_code=204)
async def deletar_alerta(alerta_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        achou = await excluir_regra(conn, alerta_id)
    if not achou:
        raise HTTPException(status_code=404, detail="alerta não encontrado")

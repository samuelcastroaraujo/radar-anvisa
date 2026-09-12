"""Orquestração do RAG conversacional — seção 7 do briefing.

Fluxo: detecta a intenção da pergunta (app/intent.py) -> monta o contexto
apropriado (lookup direto, busca híbrida escopada, ou consulta temporal) ->
chama o LLM (app/llm.py) com as regras absolutas -> monta a resposta
estruturada (normas citadas, pra o frontend renderizar cards).

Duas intenções ainda não têm dado de verdade por trás (consulta pública
depende do módulo 630, que é M5) — nesses casos a resposta é honesta e
determinística, sem gastar uma chamada de LLM à toa.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import asyncpg
import asyncpg.pool

from app.busca import FiltroBusca, ResultadoBusca, buscar
from app.intent import Intencao, NormaReferenciada, detectar_intencao
from app.llm import perguntar

Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

MAX_CHUNKS_CONTEXTO = 8
URLS_APOIO = "https://www.gov.br/anvisa/pt-br ou https://anvisalegis.datalegis.net"


@dataclass
class NormaCitada:
    id: str
    tipo_ato: str
    numero: str
    ano: int
    status_vigencia: str
    url_origem: str


@dataclass
class RespostaChat:
    resposta: str
    fontes: list[str]
    normas: list[NormaCitada]
    intencao: str
    n_chunks_recuperados: int = 0
    teve_citacao: bool = False
    tokens_entrada: int = 0
    tokens_saida: int = 0
    custo_estimado: float | None = None
    latencia_ms: int = 0
    metadados_extra: dict[str, object] = field(default_factory=dict)


async def _buscar_norma_por_referencia(conn: Conn, ref: NormaReferenciada) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        select id, tipo_ato, numero, ano, data_publicacao, ementa,
               status_vigencia, url_origem, url_pdf, texto_integral
        from norma
        where numero = $1 and ano = $2 and tipo_ato = any($3)
        order by (texto_integral is not null and texto_integral <> '') desc,
                 (status_vigencia <> 'desconhecido') desc
        limit 1
        """,
        ref.numero,
        ref.ano,
        ref.grupo_tipo_ato,
    )


async def _buscar_relacoes(conn: Conn, norma_id: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        select r.tipo, r.dispositivo, r.data_efeito,
               o.id as origem_id, o.tipo_ato as origem_tipo, o.numero as origem_numero,
               o.ano as origem_ano,
               d.id as destino_id, d.tipo_ato as destino_tipo, d.numero as destino_numero,
               d.ano as destino_ano
        from norma_relacao r
        join norma o on o.id = r.origem_id
        join norma d on d.id = r.destino_id
        where r.origem_id = $1 or r.destino_id = $1
        order by r.data_efeito desc nulls last
        limit 20
        """,
        norma_id,
    )


def _formatar_relacao(rel: asyncpg.Record, norma_id: str) -> str:
    verbo = {
        "revoga": "revoga",
        "revoga_parcial": "revoga parcialmente",
        "altera": "altera",
        "retifica": "retifica",
        "regulamenta": "regulamenta",
        "substitui": "substitui",
        "referencia": "cita",
    }.get(rel["tipo"], rel["tipo"])
    dispositivo = f" ({rel['dispositivo']})" if rel["dispositivo"] else ""
    if str(rel["origem_id"]) == norma_id:
        destino = f"{rel['destino_tipo']} {rel['destino_numero']}/{rel['destino_ano']}"
        return f"- ESTA norma {verbo} a {destino}{dispositivo}."
    origem = f"{rel['origem_tipo']} {rel['origem_numero']}/{rel['origem_ano']}"
    return f"- A {origem} {verbo} ESTA norma{dispositivo}."


async def _contexto_norma_especifica(
    conn: Conn, norma: asyncpg.Record, pergunta: str
) -> tuple[str, list[NormaCitada], list[str], int]:
    relacoes = await _buscar_relacoes(conn, str(norma["id"]))
    resultados = await buscar(
        conn, pergunta, filtro=FiltroBusca(norma_id=str(norma["id"])), top=MAX_CHUNKS_CONTEXTO
    )

    partes = [
        f"NORMA: {norma['tipo_ato']} {norma['numero']}/{norma['ano']}",
        f"STATUS DE VIGÊNCIA: {norma['status_vigencia'].upper()}",
        f"Data de publicação: {norma['data_publicacao'] or 'não registrada'}",
        f"Ementa: {norma['ementa'] or '(sem ementa registrada)'}",
        f"Fonte: {norma['url_origem']}",
    ]
    if relacoes:
        partes.append("Relações registradas com outras normas:")
        partes.extend(_formatar_relacao(r, str(norma["id"])) for r in relacoes)
    if resultados:
        partes.append("\nTrechos do texto (mais relevantes para a pergunta):")
        for r in resultados:
            partes.append(f"[{r.rotulo}] {r.conteudo}")
    elif norma["texto_integral"]:
        # sem chunks indexados (não deveria acontecer para módulo 310, mas
        # não custa ter um fallback honesto em vez de contexto vazio)
        partes.append(f"\nTexto integral:\n{norma['texto_integral'][:6000]}")

    citada = NormaCitada(
        id=str(norma["id"]),
        tipo_ato=norma["tipo_ato"],
        numero=norma["numero"],
        ano=norma["ano"],
        status_vigencia=norma["status_vigencia"],
        url_origem=norma["url_origem"],
    )
    return "\n".join(partes), [citada], [norma["url_origem"]], len(resultados)


def _contexto_tematico(
    resultados: list[ResultadoBusca],
) -> tuple[str, list[NormaCitada], list[str]]:
    partes = []
    vistas: dict[str, NormaCitada] = {}
    fontes: list[str] = []
    for r in resultados:
        partes.append(
            f"[{r.tipo_ato} {r.numero}/{r.ano} — status: {r.status_vigencia.upper()} — "
            f"{r.rotulo}]\nEmenta: {r.ementa or '(sem ementa)'}\nTrecho: {r.conteudo}\n"
            f"Fonte: {r.url_origem}"
        )
        if r.norma_id not in vistas:
            vistas[r.norma_id] = NormaCitada(
                id=r.norma_id,
                tipo_ato=r.tipo_ato,
                numero=r.numero,
                ano=r.ano,
                status_vigencia=r.status_vigencia,
                url_origem=r.url_origem,
            )
            fontes.append(r.url_origem)
    return "\n\n".join(partes), list(vistas.values()), fontes


async def _contexto_temporal(conn: Conn, dias: int) -> tuple[str, list[NormaCitada], list[str]]:
    desde = datetime.now(UTC) - timedelta(days=dias)
    rows = await conn.fetch(
        """
        select id, tipo_ato, numero, ano, data_publicacao, ementa,
               status_vigencia, url_origem
        from norma
        where data_publicacao >= $1
        order by data_publicacao desc
        limit 20
        """,
        desde.date(),
    )
    aviso = (
        "AVISO IMPORTANTE PARA VOCÊ, ASSISTENTE: esta lista cobre só as normas "
        "regulatórias diretas da ANVISA (módulo 310 do AnvisaLegis). Notícias, "
        "informes e publicações do Diário Oficial da União ainda não estão "
        "indexados nesta base — avise o usuário dessa limitação se ele "
        "perguntar por novidades em geral, não só normas."
    )
    if not rows:
        return (
            f"{aviso}\n\nNenhuma norma do módulo 310 foi publicada nos últimos {dias} dias.",
            [],
            [],
        )
    partes = [aviso, f"\nNormas do módulo 310 publicadas nos últimos {dias} dias:"]
    citadas = []
    fontes = []
    for r in rows:
        partes.append(
            f"- {r['tipo_ato']} {r['numero']}/{r['ano']}, publicada em {r['data_publicacao']} "
            f"— status: {r['status_vigencia'].upper()} — {r['ementa'] or '(sem ementa)'} "
            f"— {r['url_origem']}"
        )
        citadas.append(
            NormaCitada(
                id=str(r["id"]),
                tipo_ato=r["tipo_ato"],
                numero=r["numero"],
                ano=r["ano"],
                status_vigencia=r["status_vigencia"],
                url_origem=r["url_origem"],
            )
        )
        fontes.append(r["url_origem"])
    return "\n".join(partes), citadas, fontes


def _resposta_nao_encontrei(motivo: str) -> RespostaChat:
    return RespostaChat(
        resposta=f"Não encontrei isso na base indexada. {motivo} Verifique diretamente em "
        f"{URLS_APOIO}.",
        fontes=[],
        normas=[],
        intencao="nao_encontrado",
    )


async def _registrar_metrica(pool: asyncpg.Pool, pergunta: str, resp: RespostaChat) -> None:
    await pool.execute(
        """
        insert into chat_metrica
            (pergunta, intencao, n_chunks_recuperados, teve_citacao,
             tokens_entrada, tokens_saida, custo_estimado, latencia_ms)
        values ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        pergunta,
        resp.intencao,
        resp.n_chunks_recuperados,
        resp.teve_citacao,
        resp.tokens_entrada or None,
        resp.tokens_saida or None,
        resp.custo_estimado,
        resp.latencia_ms,
    )


async def responder(pool: asyncpg.Pool, pergunta: str) -> RespostaChat:
    inicio = time.monotonic()
    intencao: Intencao = detectar_intencao(pergunta)
    resp: RespostaChat

    if intencao.tipo == "consulta_publica":
        # módulo 630 (participação social) ainda não foi ingerido -- é M5.
        # Responder isso com certeza, sem gastar uma chamada de LLM à toa.
        resp = _resposta_nao_encontrei(
            "O módulo de consultas públicas e audiências (módulo 630 do AnvisaLegis) "
            "ainda não foi indexado nesta base."
        )
        resp.intencao = intencao.tipo
    else:
        async with pool.acquire() as conn:
            if intencao.tipo == "norma_especifica":
                assert intencao.norma is not None
                norma = await _buscar_norma_por_referencia(conn, intencao.norma)
                if norma is None:
                    resp = _resposta_nao_encontrei(
                        f"Não achei a {intencao.norma.tipo_ato} {intencao.norma.numero}/"
                        f"{intencao.norma.ano} na base."
                    )
                    resp.intencao = intencao.tipo
                    contexto = None
                else:
                    contexto, normas, fontes, n_chunks = await _contexto_norma_especifica(
                        conn, norma, pergunta
                    )
            elif intencao.tipo == "temporal":
                contexto, normas, fontes = await _contexto_temporal(conn, intencao.dias or 30)
                n_chunks = len(normas)
            else:  # tematica
                resultados = await buscar(conn, pergunta, top=MAX_CHUNKS_CONTEXTO)
                if not resultados:
                    resp = _resposta_nao_encontrei("A busca não retornou nenhum trecho relevante.")
                    resp.intencao = intencao.tipo
                    contexto = None
                else:
                    contexto, normas, fontes = _contexto_tematico(resultados)
                    n_chunks = len(resultados)

        if contexto is not None:
            resultado_llm = await perguntar(contexto, pergunta)
            teve_citacao = any(n.numero in resultado_llm.texto for n in normas)
            resp = RespostaChat(
                resposta=resultado_llm.texto,
                fontes=fontes,
                normas=normas,
                intencao=intencao.tipo,
                n_chunks_recuperados=n_chunks,
                teve_citacao=teve_citacao,
                tokens_entrada=resultado_llm.tokens_entrada,
                tokens_saida=resultado_llm.tokens_saida,
                custo_estimado=resultado_llm.custo_estimado,
            )

    resp.latencia_ms = int((time.monotonic() - inicio) * 1000)
    await _registrar_metrica(pool, pergunta, resp)
    return resp

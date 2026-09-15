"""Orquestração do RAG conversacional — seção 7 do briefing.

Fluxo: detecta a intenção da pergunta (app/intent.py) -> monta o contexto
apropriado (lookup direto, busca híbrida escopada, consulta temporal, ou
consulta ao vivo de produto) -> chama o LLM (app/llm.py) com as regras
absolutas -> monta a resposta estruturada (normas/produtos citados, pra o
frontend renderizar cards).

Uma intenção ainda não tem dado de verdade por trás (consulta pública
depende do módulo 630, que é M5) — nesse caso a resposta é honesta e
determinística, sem gastar uma chamada de LLM à toa. `produto_alimento`
(pós-M7) é parecida mas com dado vivo: não indexado no banco, consultado
na hora direto na API da ANVISA (`app/ingest/consultas_alimentos.py`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import asyncpg
import asyncpg.pool
from curl_cffi.requests.exceptions import RequestException

from app.busca import FiltroBusca, ResultadoBusca, buscar
from app.ingest.consultas_alimentos import (
    ConsultasAnvisaClient,
    ProdutoAlimento,
    ResultadoBuscaProdutos,
    buscar_produtos,
)
from app.intent import Intencao, NormaReferenciada, detectar_intencao
from app.llm import TermosBuscaProduto, extrair_termos_busca_produto, perguntar

Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

MAX_CHUNKS_CONTEXTO = 8
MAX_PRODUTOS_CONTEXTO = 5
URLS_APOIO = "https://www.gov.br/anvisa/pt-br ou https://anvisalegis.datalegis.net"
URL_CONSULTA_PRODUTOS = "https://consultas.anvisa.gov.br/#/alimentos/"


@dataclass
class NormaCitada:
    id: str
    tipo_ato: str
    numero: str
    ano: int
    status_vigencia: str
    url_origem: str


@dataclass
class ProdutoCitado:
    numero_processo: str
    descricao: str
    situacao_registro: str | None
    detentor_razao_social: str | None
    url_origem: str


@dataclass
class RespostaChat:
    resposta: str
    fontes: list[str]
    normas: list[NormaCitada]
    intencao: str
    produtos: list[ProdutoCitado] = field(default_factory=list)
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
            # str() de propósito: apesar de `ResultadoBusca.norma_id` ser
            # anotado como `str`, o asyncpg devolve um `uuid.UUID` de
            # verdade pra coluna `norma_id` — a anotação do dataclass não
            # é validada em runtime, então isso só quebrava na fronteira
            # HTTP de verdade (`NormaCitadaResponse`, um model Pydantic),
            # nunca no golden QA (que chama `responder()` direto, sem
            # passar pela validação Pydantic) — achado com tráfego real de
            # produção, não local.
            vistas[r.norma_id] = NormaCitada(
                id=str(r.norma_id),
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


def _descrever_termos(termos: TermosBuscaProduto) -> str:
    partes = []
    if termos.nome_produto:
        partes.append(f'produto "{termos.nome_produto}"')
    if termos.marca:
        partes.append(f'marca "{termos.marca}"')
    if termos.detentor_registro:
        partes.append(f'empresa "{termos.detentor_registro}"')
    return ", ".join(partes) if partes else "(nada identificado na pergunta)"


def _combinacoes_busca(termos: TermosBuscaProduto) -> list[dict[str, str]]:
    """Degraus de relaxamento, do mais específico pro mais genérico —
    achado real testando de ponta a ponta: a extração por LLM às vezes
    erra pro lado específico demais (nome composto que não bate com a
    descrição telegráfica da ANVISA — "whey protein" não acha nada, só
    "whey" acha — ou o mesmo nome de empresa duplicado em `marca`), e
    como todo filtro é combinado com AND na API, isso dá falso-negativo
    pra um produto que existe de verdade. Tenta a combinação completa
    primeiro; se vier vazia, relaxa indo pra combinações com menos
    filtros, sempre priorizando manter `nome_produto` (é o termo mais
    provável de estar certo — a extração já é instruída a manter isso
    curto/genérico)."""
    base = {
        "nome_produto": termos.nome_produto,
        "marca": termos.marca,
        "detentor_registro": termos.detentor_registro,
    }
    candidatas = [
        base,
        {"nome_produto": base["nome_produto"], "detentor_registro": base["detentor_registro"]},
        {"nome_produto": base["nome_produto"], "marca": base["marca"]},
        {"nome_produto": base["nome_produto"]},
        {"marca": base["marca"]},
        {"detentor_registro": base["detentor_registro"]},
    ]
    vistas: set[tuple[tuple[str, str], ...]] = set()
    combinacoes: list[dict[str, str]] = []
    for candidata in candidatas:
        limpo = {k: v for k, v in candidata.items() if v}
        if not limpo:
            continue
        chave = tuple(sorted(limpo.items()))
        if chave in vistas:
            continue
        vistas.add(chave)
        combinacoes.append(limpo)
    return combinacoes


async def _buscar_produtos_para_chat(
    termos: TermosBuscaProduto,
) -> ResultadoBuscaProdutos | None:
    """`None` = não deu pra consultar a ANVISA agora (erro de rede/HTTP
    depois de esgotar o retry do `ConsultasAnvisaClient`) — distinto de
    "consultou e não achou nada" (lista vazia), tratado separado por quem
    chama pra não confundir os dois motivos de "não encontrei". Tenta os
    degraus de `_combinacoes_busca` em sequência, parando no primeiro que
    trouxer algum produto."""
    cliente = ConsultasAnvisaClient()
    try:
        resultado: ResultadoBuscaProdutos | None = None
        for filtros in _combinacoes_busca(termos):
            resultado = await buscar_produtos(
                cliente,
                nome_produto=filtros.get("nome_produto"),
                marca=filtros.get("marca"),
                detentor_registro=filtros.get("detentor_registro"),
                tamanho_pagina=MAX_PRODUTOS_CONTEXTO,
            )
            if resultado.itens:
                return resultado
        return resultado
    except RequestException:
        return None
    finally:
        await cliente.aclose()


def _contexto_produto_alimento(
    itens: list[ProdutoAlimento],
) -> tuple[str, list[ProdutoCitado], list[str]]:
    aviso = (
        "AVISO IMPORTANTE PARA VOCÊ, ASSISTENTE: os produtos abaixo vêm de uma consulta "
        "AO VIVO no cadastro de produtos regularizados da ANVISA (não é uma base indexada "
        "aqui, é a fonte oficial na hora). Informe a SITUAÇÃO DO REGISTRO (Ativo/Inativo) "
        "de cada produto já na primeira frase, antes de qualquer outra coisa — 'Inativo' "
        "quer dizer que o registro/notificação NÃO está mais válido perante a ANVISA "
        "nesta data, mesmo que o produto continue sendo vendido. Se vieram vários "
        "produtos parecidos (marcas/apresentações diferentes), deixe claro que a lista "
        "é a mais próxima da pergunta, não necessariamente o produto exato."
    )
    partes = [aviso, ""]
    citados = []
    fontes = []
    for p in itens:
        empresa = p.detentor_razao_social or "(não informado)"
        if p.detentor_cnpj:
            empresa += f" (CNPJ {p.detentor_cnpj})"
        partes.append(
            f"[PRODUTO] {p.descricao}\n"
            f"Situação do registro: {(p.situacao_registro or 'DESCONHECIDA').upper()}\n"
            f"Tipo de regularização: {p.tipo_regularizacao or '(não informado)'}\n"
            f"Nº de registro/notificação: {p.numero_registro_ou_notificacao}\n"
            f"Empresa detentora: {empresa}\n"
            f"Categorias: {', '.join(p.categorias) if p.categorias else '(não informado)'}\n"
            f"Vencimento: {p.mes_ano_vencimento or '(não informado)'}\n"
            f"Fonte: {p.url_origem}\n"
        )
        citados.append(
            ProdutoCitado(
                numero_processo=p.numero_processo,
                descricao=p.descricao,
                situacao_registro=p.situacao_registro,
                detentor_razao_social=p.detentor_razao_social,
                url_origem=p.url_origem,
            )
        )
        fontes.append(p.url_origem)
    return "\n".join(partes), citados, fontes


def _resposta_nao_encontrei(motivo: str, urls: str = URLS_APOIO) -> RespostaChat:
    return RespostaChat(
        resposta=f"Não encontrei isso na base indexada. {motivo} Verifique diretamente em {urls}.",
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
    contexto: str | None = None
    normas: list[NormaCitada] = []
    produtos: list[ProdutoCitado] = []
    fontes: list[str] = []
    n_chunks = 0

    if intencao.tipo == "consulta_publica":
        # módulo 630 (participação social) ainda não foi ingerido -- é M5.
        # Responder isso com certeza, sem gastar uma chamada de LLM à toa.
        resp = _resposta_nao_encontrei(
            "O módulo de consultas públicas e audiências (módulo 630 do AnvisaLegis) "
            "ainda não foi indexado nesta base."
        )
        resp.intencao = intencao.tipo
    elif intencao.tipo == "produto_alimento":
        # Consulta ao vivo (app/ingest/consultas_alimentos.py) — não passa
        # pelo banco, por isso fica fora do `pool.acquire()` abaixo, igual
        # a `consulta_publica`. Uma chamada de LLM barata extrai os termos
        # de busca da pergunta livre (app/llm.py); a resposta final usa o
        # mesmo `perguntar()` das outras intenções.
        termos = await extrair_termos_busca_produto(pergunta)
        if termos.vazio:
            resp = _resposta_nao_encontrei(
                "Não consegui identificar qual produto, marca ou empresa você quer "
                "consultar — tente reformular citando o nome do produto ou da marca.",
                urls=URL_CONSULTA_PRODUTOS,
            )
            resp.intencao = intencao.tipo
        else:
            resultado_busca = await _buscar_produtos_para_chat(termos)
            if resultado_busca is None:
                resp = _resposta_nao_encontrei(
                    "Não consegui consultar a ANVISA agora (falha de rede). Tente de novo "
                    "em instantes.",
                    urls=URL_CONSULTA_PRODUTOS,
                )
                resp.intencao = intencao.tipo
            elif not resultado_busca.itens:
                resp = _resposta_nao_encontrei(
                    f"Busquei ao vivo no cadastro de produtos da ANVISA por "
                    f"{_descrever_termos(termos)} e não achei nenhum resultado.",
                    urls=URL_CONSULTA_PRODUTOS,
                )
                resp.intencao = intencao.tipo
            else:
                contexto, produtos, fontes = _contexto_produto_alimento(resultado_busca.itens)
                n_chunks = len(produtos)
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
                else:
                    contexto, normas, fontes = _contexto_tematico(resultados)
                    n_chunks = len(resultados)

    if contexto is not None:
        resultado_llm = await perguntar(contexto, pergunta)
        teve_citacao = any(n.numero in resultado_llm.texto for n in normas) or any(
            p.numero_processo in resultado_llm.texto for p in produtos
        )
        resp = RespostaChat(
            resposta=resultado_llm.texto,
            fontes=fontes,
            normas=normas,
            produtos=produtos,
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

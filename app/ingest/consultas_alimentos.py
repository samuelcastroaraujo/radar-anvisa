"""Consulta ao vivo de registro/notificação de produtos de alimentos
(inclui suplementos alimentares) na API por trás de
`https://consultas.anvisa.gov.br/#/alimentos/`.

Ver `research/FONTES.md`, Addendum pós-M7, para como o endpoint foi
descoberto (lendo o JS-fonte do Angular, não documentação nenhuma) e
validado com requisições reais.

Diferente dos outros módulos em `app/ingest/`, este não é um harvester —
não existe "carregar tudo", é uma consulta pontual sob demanda (a base de
produtos regularizados não faz sentido indexar pra RAG; é lookup, igual ao
roteamento de `norma_especifica` em `app/intent.py`). Por isso não expõe
`carregar_*` nem grava nada no banco — só `buscar_produtos`/`detalhe_produto`,
chamados direto do endpoint em `app/main.py`.

**Não usa `httpx`, ao contrário do resto do projeto** — achado real, não
suposto: o domínio está atrás de Cloudflare Bot Management, que bloqueia
(403) toda requisição feita com o `httpx`/`httpcore` do Python mesmo com
headers idênticos aos de uma requisição de navegador (`curl` com os MESMOS
headers passa; a diferença é o fingerprint de TLS/JA3 da lib, não o
conteúdo da requisição). `curl_cffi` (bindings pra libcurl com
impersonation de TLS de navegador de verdade) resolve isso — confirmado ao
vivo, repetidas vezes, contra a API real antes de virar dependência do
projeto.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.exceptions import HTTPError, RequestException
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from app.config import get_settings
from app.ingest.anvisalegis import RateLimiter

BASE_URL = "https://consultas.anvisa.gov.br"


@dataclass
class ProdutoAlimento:
    numero_processo: str
    numero_registro_ou_notificacao: str
    descricao: str
    situacao_registro: str | None
    tipo_regularizacao: str | None
    situacao_processo: str | None
    detentor_cnpj: str | None
    detentor_razao_social: str | None
    categorias: list[str]
    marcas: list[str]
    data_regularizacao: datetime | None
    data_atualizacao: datetime | None
    mes_ano_vencimento: str | None
    url_origem: str


@dataclass
class ResultadoBuscaProdutos:
    itens: list[ProdutoAlimento]
    pagina: int
    tamanho_pagina: int


def _url_detalhe(numero_processo: str) -> str:
    return f"{BASE_URL}/#/alimentos/{numero_processo}/"


def _parse_data(bruto: str | None) -> datetime | None:
    if not bruto:
        return None
    try:
        return datetime.fromisoformat(bruto)
    except ValueError:
        return None


def _parse_lista_descricoes(bruto: list[dict[str, Any]] | None) -> list[str]:
    if not bruto:
        return []
    return [item["descricao"] for item in bruto if item.get("descricao")]


def _parse_produto(bruto: dict[str, Any]) -> ProdutoAlimento:
    produto = bruto.get("produto") or {}
    processo = bruto.get("processo") or {}
    detentor = bruto.get("detentorRegistro") or {}
    numero_processo = processo.get("numero", "")
    return ProdutoAlimento(
        numero_processo=numero_processo,
        numero_registro_ou_notificacao=produto.get("numeroRegistroOuNotificacao", ""),
        descricao=produto.get("descricao", ""),
        situacao_registro=produto.get("situacaoRegistro"),
        tipo_regularizacao=produto.get("tipoRegularizacao"),
        situacao_processo=processo.get("situacao"),
        detentor_cnpj=detentor.get("cnpj"),
        detentor_razao_social=detentor.get("razaoSocial"),
        categorias=_parse_lista_descricoes(bruto.get("categorias")),
        marcas=_parse_lista_descricoes(bruto.get("marcas")),
        data_regularizacao=_parse_data(processo.get("dataRegularizacao")),
        data_atualizacao=_parse_data(bruto.get("dataAtualizacao")),
        mes_ano_vencimento=produto.get("mesAnoVencimentoFormatado"),
        url_origem=_url_detalhe(numero_processo),
    )


def _parse_resultado(bruto: dict[str, Any]) -> ResultadoBuscaProdutos:
    tamanho_pagina = bruto.get("size", 0)
    # Achado real, confirmado ao vivo com múltiplos termos de busca
    # completamente diferentes ("whey", "creatina", "vitamina c" deram
    # exatamente os mesmos números pro mesmo `count`): `totalElements` e
    # `numberOfElements` da API não refletem o resultado real da busca —
    # são função só do `count` pedido (`totalElements = count+2`,
    # `numberOfElements = count+1`, sempre), e por isso `content` sempre
    # vem com 1 item a mais do que o pedido, sendo o último item de cada
    # página igual ao primeiro item da página seguinte (bug de "peek" na
    # paginação do backend deles — afeta até a página oficial, que usa
    # esse mesmo `totalElements` pra alimentar sua tabela). Cortar pro
    # tamanho pedido aqui elimina a sobreposição: testado ao vivo pedindo
    # 1 item por página (1,2,3...) e comparando com uma busca de
    # referência trazendo tudo de uma vez — bate exatamente, sem lacuna
    # nem duplicata. `totalElements`/`totalPages` nem são expostos (ver
    # Addendum pós-M7 em research/FONTES.md) — não há como confiar neles.
    itens = [_parse_produto(item) for item in bruto.get("content", [])]
    if tamanho_pagina > 0:
        itens = itens[:tamanho_pagina]
    return ResultadoBuscaProdutos(
        itens=itens,
        # a API é 0-indexed em "number" na resposta (mesmo recebendo
        # page=1 na requisição) — normaliza pra 1-indexed, consistente com
        # o que foi pedido.
        pagina=bruto.get("number", 0) + 1,
        tamanho_pagina=tamanho_pagina,
    )


def _eh_processo_nao_encontrado(exc: HTTPError) -> bool:
    """A API devolve HTTP 500 (não 404!) pra numeroProcesso inexistente,
    com `error` = "Nenhuma apresentação encontrada" — confirmado ao vivo
    (research/samples/consultas_alimentos_processo_inexistente.json).
    Distingue isso de um 500 de verdade (que deve propagar/retry)."""
    resp = exc.response
    if resp is None or resp.status_code != 500:
        return False
    try:
        corpo = resp.json()
    except Exception:
        return False
    return "nenhuma apresenta" in str(corpo.get("error", "")).lower()


def _deve_tentar_de_novo(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError):
        # "processo não encontrado" é determinístico — repetir não ajuda.
        # Todo o resto (inclusive o 403 intermitente do Cloudflare, ver
        # docstring do módulo) é digno de retry.
        return not _eh_processo_nao_encontrado(exc)
    return isinstance(exc, RequestException)


class _SessaoHTTP(Protocol):
    """Só o pedaço de `curl_cffi.requests.AsyncSession` que este módulo usa
    — permite injetar um fake nos testes sem depender da rede/TLS real."""

    async def get(self, url: str, **kwargs: Any) -> Response: ...
    async def close(self) -> None: ...


class ConsultasAnvisaClient:
    """Cliente HTTP para `consultas.anvisa.gov.br` — mesmo esquema de rate
    limit + retry dos outros clientes deste projeto, mas usando
    `curl_cffi` em vez de `httpx` (ver docstring do módulo: o domínio está
    atrás de Cloudflare Bot Management, que bloqueia o fingerprint de TLS
    do `httpx` mesmo com headers corretos). `impersonate="chrome"` finge
    o TLS de um Chrome de verdade; `Referer`/`Authorization: Guest` são os
    headers que o próprio app Angular manda (achado ao vivo: sem
    `Referer`, 403 consistente mesmo via `curl`; `Authorization: Guest` é
    o token anônimo padrão do app — sem ele a API responde erro de
    negócio, não exige captcha nenhum pra esta consulta).
    """

    def __init__(self, sessao: _SessaoHTTP | None = None) -> None:
        settings = get_settings()
        self._sessao: _SessaoHTTP
        if sessao is not None:
            self._sessao = sessao
        else:
            # `AsyncSession.get` é tipado com `**kwargs: Unpack[RequestParams]`
            # (PEP 692) — estruturalmente incompatível com `**kwargs: Any` do
            # Protocol `_SessaoHTTP` pro mypy, mesmo satisfazendo em runtime;
            # o cast documenta que isso é só ruído de tipagem, não um risco
            # real (o Protocol só existe pra permitir injetar um fake nos
            # testes).
            self._sessao = cast(
                "_SessaoHTTP",
                AsyncSession(
                    headers={
                        "User-Agent": settings.crawl_user_agent,
                        "Accept": "application/json, text/plain, */*",
                        "Referer": f"{BASE_URL}/",
                        "Authorization": "Guest",
                    },
                    impersonate="chrome",
                    timeout=30,
                ),
            )
        self._limiter = RateLimiter()
        # Limiter separado, mais rápido, só pro enriquecimento de marca da
        # listagem (`detalhe_produto(..., respeitar_limite=False)`) — ver
        # nota completa (achado real de HTTP 429 ao vivo) na seção de
        # enriquecimento, perto de `_enriquecer_com_marcas`.
        self._limiter_enriquecimento = RateLimiter(
            intervalo_segundos=_INTERVALO_ENRIQUECIMENTO_SEGUNDOS
        )
        self._owns_sessao = sessao is None

    async def aclose(self) -> None:
        if self._owns_sessao:
            await self._sessao.close()

    @retry(
        retry=retry_if_exception(_deve_tentar_de_novo),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1, max=20),
        reraise=True,
    )
    async def _get(
        self, path: str, params: dict[str, str] | None = None, *, respeitar_limite: bool = True
    ) -> Response:
        # `respeitar_limite=False` troca o 1 req/s padrão (política de
        # crawling em lote, seção 0 de research/FONTES.md) pelo limiter
        # mais rápido dedicado ao enriquecimento — NÃO remove o limite:
        # achado real testando ao vivo, sem limiter nenhum (bypass total)
        # um lote de ~50 chamadas concorrentes toma HTTP 429 da própria
        # ANVISA em boa parte dos itens. Ver nota completa perto de
        # `_enriquecer_com_marcas`.
        limiter = self._limiter if respeitar_limite else self._limiter_enriquecimento
        await limiter.aguardar()
        resp = await self._sessao.get(f"{BASE_URL}{path}", params=params)
        resp.raise_for_status()
        return resp


# Filtros de "busca simples" (os únicos exercitados/testados até agora — o
# controller do Angular expõe bem mais campos de "busca avançada":
# categorias, datas, alérgenos etc., não mapeados aqui ainda).
_CAMPOS_FILTRO = {
    "nome_produto": "filter[nomeProduto]",
    "marca": "filter[marca]",
    "detentor_registro": "filter[detentorRegistro]",
    "numero_processo": "filter[numeroProcesso]",
    "numero_registro_notificacao": "filter[numeroRegistroNotificacao]",
}

# A API espera 'S'/'N' pra situacaoProduto (confirmado ao vivo, testando
# os dois valores e comparando `situacaoRegistro` do resultado — bate
# exatamente com `tiposSituacaoOptions` do controller Angular). Exposto
# pra fora como "Ativo"/"Inativo" (mesmo texto que já volta em
# `situacao_registro`), não como 'S'/'N' cru — mais legível pra quem
# consome a API, tradução fica só aqui dentro.
_SITUACAO_PRODUTO_MAP = {"Ativo": "S", "Inativo": "N"}

_MAX_CANDIDATOS_EMPRESA = 3
"""Quantos CNPJs candidatos tentar quando `detentor_registro` é um nome,
não um CNPJ — ver docstring de `buscar_produtos`."""


@dataclass
class Empresa:
    cnpj: str
    razao_social: str
    nome_fantasia: str | None


def _parse_empresa(bruto: dict[str, Any]) -> Empresa:
    return Empresa(
        cnpj=bruto.get("cnpj", ""),
        razao_social=bruto.get("razaoSocial", ""),
        nome_fantasia=bruto.get("nomeFantasia"),
    )


def _eh_cnpj(valor: str) -> bool:
    return len(re.sub(r"\D", "", valor)) == 14


async def buscar_empresas(
    cliente: ConsultasAnvisaClient, razao_social: str, tamanho_pagina: int = 5
) -> list[Empresa]:
    """Busca empresas por razão social (substring, case-insensitive,
    confirmado ao vivo) — endpoint separado do de produtos
    (`/api/empresa/`), achado lendo `components/input-empresa/
    input-empresa.directive.js`: a página oficial NUNCA manda texto livre
    de nome de empresa pro filtro `detentorRegistro` de produtos (que só
    aceita CNPJ exato, confirmado ao vivo — nome inteiro ou parcial
    devolve 0 resultados); em vez disso resolve nome -> CNPJ nessa tela
    de busca antes de filtrar. `buscar_produtos` usa isso por baixo dos
    panos quando `detentor_registro` não parece um CNPJ — ver lá."""
    resp = await cliente._get(
        "/api/empresa/",
        params={
            "filter[razaoSocial]": razao_social,
            "page": "1",
            "count": str(tamanho_pagina),
        },
    )
    bruto = resp.json()
    tamanho = bruto.get("size", 0)
    itens = [_parse_empresa(item) for item in bruto.get("content", [])]
    # mesmo achado de paginação de `_parse_resultado` (content vem com
    # size+1 itens) — aplica o mesmo corte aqui por segurança, mesmo sem
    # ter confirmado o bug nesse endpoint especificamente ainda.
    return itens[:tamanho] if tamanho > 0 else itens


async def buscar_produtos(
    cliente: ConsultasAnvisaClient,
    *,
    nome_produto: str | None = None,
    marca: str | None = None,
    detentor_registro: str | None = None,
    numero_processo: str | None = None,
    numero_registro_notificacao: str | None = None,
    situacao_registro: str | None = None,
    pagina: int = 1,
    tamanho_pagina: int = 10,
    enriquecer_marcas: bool = False,
) -> ResultadoBuscaProdutos:
    """Busca produtos de alimentos (inclui suplementos) por nome, marca,
    CNPJ/razão social do detentor, nº de processo, nº de registro/
    notificação e/ou situação (`"Ativo"`/`"Inativo"`). Pelo menos um
    filtro é obrigatório — a própria API rejeita busca totalmente vazia
    (`MSG-004`, confirmado ao vivo); evita gastar uma requisição pra
    reaprender isso. `situacao_registro` sozinho já conta como filtro
    válido (mesmo comportamento da página oficial — dá pra "listar todos
    os ativos", sem nome/marca).

    `detentor_registro` aceita CNPJ **ou** razão social (parcial) — achado
    real: a API só filtra por CNPJ exato (nome, mesmo completo, devolve 0
    resultados); quando o valor não parece um CNPJ, resolve via
    `buscar_empresas` primeiro (mesmo passo que a página oficial faz com
    o autocomplete de empresa). Mais de uma empresa pode bater com o
    mesmo nome parcial (grupos econômicos com várias razões sociais
    quase idênticas, cada uma um CNPJ — achado real testando "belapin":
    4 CNPJs, só um deles tinha o produto buscado) — tenta até
    `_MAX_CANDIDATOS_EMPRESA` CNPJs candidatos em sequência, na ordem que
    a busca de empresa devolveu, parando no primeiro que trouxer algum
    produto. Nenhum candidato com produto = devolve o resultado vazio do
    último tentado (ou filtro de empresa ignorado, se não achou empresa
    nenhuma e sobrou outro filtro pra usar).

    `enriquecer_marcas=True` preenche `marcas` de cada item retornado —
    a busca em lista nunca traz marca (achado real, campo sempre `null`
    na resposta da ANVISA, só o endpoint de detalhe tem; ver
    `_enriquecer_com_marcas`). Opt-in (default `False`) pra quem só quer
    a lista crua pagar o custo extra — hoje só `GET /produtos/alimentos`
    pede isso."""
    if situacao_registro is not None and situacao_registro not in _SITUACAO_PRODUTO_MAP:
        raise ValueError(
            f"situacao_registro inválido: {situacao_registro!r} (esperado 'Ativo' ou 'Inativo')"
        )
    # Validado com os argumentos ORIGINAIS, antes da resolução de empresa —
    # achado real (só apareceu via chat, não em teste unitário): se isso
    # checasse `params` já resolvido dentro do loop abaixo, uma
    # `detentor_registro` que é o único filtro e não resolve pra nenhum
    # CNPJ virava, por engano, "nenhum filtro foi informado" (erro) em vez
    # de "esse filtro não achou nada" (resultado vazio).
    if not any(
        [
            nome_produto,
            marca,
            detentor_registro,
            numero_processo,
            numero_registro_notificacao,
            situacao_registro,
        ]
    ):
        raise ValueError(
            "informe ao menos um filtro (nome_produto, marca, detentor_registro, "
            "numero_processo, numero_registro_notificacao ou situacao_registro)"
        )

    candidatos_cnpj: list[str | None]
    if detentor_registro and not _eh_cnpj(detentor_registro):
        empresas = await buscar_empresas(
            cliente, detentor_registro, tamanho_pagina=_MAX_CANDIDATOS_EMPRESA
        )
        candidatos_cnpj = [e.cnpj for e in empresas] or [None]
    else:
        candidatos_cnpj = [detentor_registro]

    outros_filtros = {
        "nome_produto": nome_produto,
        "marca": marca,
        "numero_processo": numero_processo,
        "numero_registro_notificacao": numero_registro_notificacao,
    }
    resultado: ResultadoBuscaProdutos | None = None
    for cnpj in candidatos_cnpj:
        valores = {**outros_filtros, "detentor_registro": cnpj}
        params = {_CAMPOS_FILTRO[campo]: valor for campo, valor in valores.items() if valor}
        if situacao_registro:
            params["filter[situacaoProduto]"] = _SITUACAO_PRODUTO_MAP[situacao_registro]
        if not params:
            # só chega aqui quando `detentor_registro` era o ÚNICO filtro
            # original e não resolveu pra nenhum CNPJ (já validamos acima
            # que pelo menos um filtro foi dado) — é "esse filtro não
            # achou nada", não "nenhum filtro foi informado". Não bate na
            # API (ela rejeitaria do mesmo jeito, com MSG-004).
            resultado = ResultadoBuscaProdutos(
                itens=[], pagina=pagina, tamanho_pagina=tamanho_pagina
            )
            continue
        params["page"] = str(pagina)
        params["count"] = str(tamanho_pagina)
        resp = await cliente._get("/api/consulta/alimento/produtos/", params=params)
        resultado = _parse_resultado(resp.json())
        if resultado.itens:
            break
    assert resultado is not None  # o loop roda pelo menos 1x (candidatos_cnpj nunca é [])
    if enriquecer_marcas and resultado.itens:
        await _enriquecer_com_marcas(cliente, resultado.itens)
    return resultado


async def detalhe_produto(
    cliente: ConsultasAnvisaClient, numero_processo: str, *, respeitar_limite: bool = True
) -> ProdutoAlimento | None:
    """`None` se o número de processo não existir na base da ANVISA (ver
    `_eh_processo_nao_encontrado` — a API usa HTTP 500 pra isso, não 404).

    `respeitar_limite=False` pula o rate limit de 1 req/s do cliente —
    usado só internamente por `_marcas_com_cache`, que já limita
    concorrência via semáforo (ver `_enriquecer_com_marcas`); qualquer
    chamador externo (inclusive `GET /produtos/alimentos/{numero_processo}`)
    continua respeitando o limite por padrão."""
    try:
        resp = await cliente._get(
            f"/api/consulta/alimento/produtos/{numero_processo}",
            respeitar_limite=respeitar_limite,
        )
    except HTTPError as exc:
        if _eh_processo_nao_encontrado(exc):
            return None
        raise
    return _parse_produto(resp.json())


# --- Enriquecimento de marca para a listagem (opt-in, ver buscar_produtos) ---
#
# Achado real: `marcas` vem sempre `null` na busca em lista (confirmado nas
# amostras reais — nenhum dos 11 itens de `consultas_alimentos_busca_whey.
# json` tem `marcas` preenchido), só o endpoint de detalhe traz. Não existe
# endpoint em lote pra isso (mapeado lendo o JS-fonte do Angular, ver
# research/FONTES.md) — a única forma de mostrar "Marca do Produto" em cada
# card da listagem é uma chamada de detalhe por item.
#
# Pra não pagar N segundos (1 req/s é a política de crawling em lote do
# projeto, seção 0 de research/FONTES.md) numa busca interativa de 1
# usuário — volume já limitado por `tamanho_pagina` (máx. 50, validado em
# app/main.py) — três coisas escopadas só pra este enriquecimento, não pro
# resto do módulo: um limiter dedicado mais rápido (`_limiter_
# enriquecimento`, ~1 req/(_INTERVALO_ENRIQUECIMENTO_SEGUNDOS)) em vez do
# 1 req/s de crawling em lote, um teto de concorrência por cima disso, e
# cache em memória com TTL (marca de um produto já registrado muda
# raríssimo, e buscas populares — "whey", "creatina" — se repetem entre
# usuários).
#
# **1º bug real, pego revisando depois de deploy** ("a busca tá devagar"):
# a primeira versão já tinha um semáforo de concorrência aqui, mas cada
# chamada de detalhe ainda passava pelo `RateLimiter` de 1 req/s
# compartilhado pelo cliente inteiro — o semáforo limitava quantas
# tarefas ficavam *em voo*, mas todas esperavam a mesma fila de 1s antes
# de disparar. Na prática, zero concorrência de verdade, só 1 req/s
# serial de qualquer jeito (~N segundos pra N itens).
#
# **2º achado real, testando o fix do 1º ao vivo**: tirar o limite por
# completo (nenhum limiter, só o semáforo) faz uma busca ampla de ~50
# itens tomar HTTP 429 da própria ANVISA numa fração real dos itens
# (confirmado ao vivo, repetidas vezes) — o 1 req/s do resto do projeto
# não é só cortesia arbitrária, esse domínio rate-limita de verdade sob
# rajada. Por isso o limiter dedicado (mais rápido que 1 req/s, mas ainda
# um limiter de verdade, não um bypass total) em vez de só concorrência
# crua. Números escolhidos com margem de segurança (não é o teto exato
# medido — testar o teto exato exigiria continuar martelando a API real,
# o que não faz sentido fazer só pra calibrar); casos comuns (busca
# filtrada por marca/empresa, poucos itens — ver exemplo real no
# research/FONTES.md) terminam em menos de 1s de qualquer forma, o que
# importa aqui é não tomar 429 numa busca ampla de 50.
_INTERVALO_ENRIQUECIMENTO_SEGUNDOS = 0.25
_CONCORRENCIA_ENRIQUECIMENTO_MARCAS = 6
_TIMEOUT_ENRIQUECIMENTO_MARCAS_SEGUNDOS = 15.0
_TTL_CACHE_MARCAS_SEGUNDOS = 3600.0
_MAX_CACHE_MARCAS = 2000

_cache_marcas: dict[str, tuple[float, list[str]]] = {}


async def _marcas_com_cache(cliente: ConsultasAnvisaClient, numero_processo: str) -> list[str]:
    agora = time.monotonic()
    em_cache = _cache_marcas.get(numero_processo)
    if em_cache is not None and agora - em_cache[0] < _TTL_CACHE_MARCAS_SEGUNDOS:
        return em_cache[1]
    try:
        produto = await detalhe_produto(cliente, numero_processo, respeitar_limite=False)
    except (HTTPError, RequestException):
        # Uma falha pontual (timeout, 403 intermitente) não pode derrubar a
        # listagem inteira — o item só fica sem marca dessa vez, não é
        # reescrito no cache (próxima busca tenta de novo).
        return []
    marcas = produto.marcas if produto is not None else []
    if len(_cache_marcas) >= _MAX_CACHE_MARCAS:
        _cache_marcas.pop(next(iter(_cache_marcas)))  # FIFO simples, não é LRU de verdade
    _cache_marcas[numero_processo] = (agora, marcas)
    return marcas


async def _enriquecer_com_marcas(
    cliente: ConsultasAnvisaClient, itens: list[ProdutoAlimento]
) -> None:
    """Preenche `item.marcas` em paralelo (até `_CONCORRENCIA_ENRIQUECIMENTO_
    MARCAS` de cada vez, no limiter mais rápido dedicado — não 1 req/s
    serial nem bypass total, ver nota acima), com um teto de tempo total
    (`_TIMEOUT_ENRIQUECIMENTO_MARCAS_SEGUNDOS`): itens que não terminaram
    a tempo simplesmente ficam sem marca (campo some do card, resto da
    resposta não é afetado) em vez de atrasar a resposta inteira
    indefinidamente."""
    semaforo = asyncio.Semaphore(_CONCORRENCIA_ENRIQUECIMENTO_MARCAS)

    async def _um(item: ProdutoAlimento) -> None:
        async with semaforo:
            item.marcas = await _marcas_com_cache(cliente, item.numero_processo)

    tarefas = [asyncio.ensure_future(_um(item)) for item in itens]
    _concluidas, pendentes = await asyncio.wait(
        tarefas, timeout=_TIMEOUT_ENRIQUECIMENTO_MARCAS_SEGUNDOS
    )
    for tarefa in pendentes:
        tarefa.cancel()

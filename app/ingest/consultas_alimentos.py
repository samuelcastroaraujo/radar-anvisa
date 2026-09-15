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

import re
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
    async def _get(self, path: str, params: dict[str, str] | None = None) -> Response:
        await self._limiter.aguardar()
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
    nenhuma e sobrou outro filtro pra usar)."""
    if situacao_registro is not None and situacao_registro not in _SITUACAO_PRODUTO_MAP:
        raise ValueError(
            f"situacao_registro inválido: {situacao_registro!r} (esperado 'Ativo' ou 'Inativo')"
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
            raise ValueError(
                "informe ao menos um filtro (nome_produto, marca, detentor_registro, "
                "numero_processo, numero_registro_notificacao ou situacao_registro)"
            )
        params["page"] = str(pagina)
        params["count"] = str(tamanho_pagina)
        resp = await cliente._get("/api/consulta/alimento/produtos/", params=params)
        resultado = _parse_resultado(resp.json())
        if resultado.itens:
            return resultado
    assert resultado is not None  # o loop roda pelo menos 1x (candidatos_cnpj nunca é [])
    return resultado


async def detalhe_produto(
    cliente: ConsultasAnvisaClient, numero_processo: str
) -> ProdutoAlimento | None:
    """`None` se o número de processo não existir na base da ANVISA (ver
    `_eh_processo_nao_encontrado` — a API usa HTTP 500 pra isso, não 404)."""
    try:
        resp = await cliente._get(f"/api/consulta/alimento/produtos/{numero_processo}")
    except HTTPError as exc:
        if _eh_processo_nao_encontrado(exc):
            return None
        raise
    return _parse_produto(resp.json())

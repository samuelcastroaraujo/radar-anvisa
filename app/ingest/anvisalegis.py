"""Ingestor do módulo 310 do AnvisaLegis (normas regulatórias da ANVISA).

Ver `research/FONTES.md` (seções "1. AnvisaLegis" e "Addendum M2") para como
cada endpoint foi descoberto e validado com requisições reais antes de virar
código aqui.

Nota sobre o `Protocol Fonte` (app/ingest/base.py): ele modela uma fonte onde
"descobrir" lista itens e "baixar" busca um item por vez. O AnvisaLegis não
se encaixa bem nisso — uma única resposta de "ano" já traz o texto de
dezenas de atos de uma vez (é assim que dá pra cobrir os 1.138 vigentes com
~38 requisições em vez de 1.138). Forçar esse formato no `Protocol` só
esconderia essa característica real do site. Este módulo expõe funções
diretas (`carregar_vigentes`, `carregar_revogadas`) em vez de implementar
`Fonte` — decisão registrada em `CLAUDE.md`.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, field
from datetime import date

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.config import get_settings

BASE_URL = "https://anvisalegis.datalegis.net"
COD_MODULO_310 = 310

# cod_menu de cada sub-listagem do módulo 310 (ver research/FONTES.md, Addendum M2)
COD_MENU_VIGENTES = 8542
COD_MENU_REVOGADAS = 9882
COD_MENU_RETIFICADORAS = 9429
COD_MENU_ALTERADORAS = 9428
COD_MENU_REVOGADORAS = 9432

QTD_POR_PAGINA_REVOGADAS = 50
"""Tamanho de página fixo no JS do site para a listagem de revogadas (ver
`AnvisaLegisClient.atos_revogados_do_ano`)."""

# Mapa "nome por extenso -> sigla do tipo de ato", construído só com o que
# apareceu de verdade nas amostras coletadas (não é uma lista exaustiva de
# todos os ~95 tipos possíveis do portal). Tipo não reconhecido fica com a
# sigla bruta em maiúsculas do próprio texto (melhor que descartar o ato).
TIPO_ATO_MAP: dict[str, str] = {
    "instrução normativa": "INM",
    "resolução da diretoria colegiada": "RDC",
    "resolução de diretoria colegiada": "RDC",
    "resolução": "RES",
    "portaria": "POR",
}

# Verbos que aparecem perto de um LinkTexto(...) e indicam o tipo de relação
# (heurística — ver research/FONTES.md; sem isso, tipo="referencia", ou seja,
# "esse ato só cita o outro", sem alterar seu estado).
VERBOS_RELACAO: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"revoga(da|do)?\s+parcialmente|revoga.{0,40}art", re.I), "revoga_parcial"),
    (re.compile(r"revog", re.I), "revoga"),
    (re.compile(r"altera", re.I), "altera"),
    (re.compile(r"retifica", re.I), "retifica"),
    (re.compile(r"regulamenta", re.I), "regulamenta"),
    (re.compile(r"substitu", re.I), "substitui"),
]


@dataclass
class RelacaoBruta:
    tipo: str
    destino_tipo_ato: str
    destino_numero: str
    destino_ano: int
    dispositivo: str | None = None
    invertida: bool = False
    """False (padrão): o ato sendo parseado é a origem da relação (ele age
    sobre `destino_*`) — é o caso do LinkTexto dentro do próprio texto do
    ato. True: o ato sendo parseado é o destino — é o caso do aviso
    "Revogada pela X" na listagem de revogadas, onde X é quem age."""


@dataclass
class AtoParseado:
    tipo_ato: str
    numero: str
    ano: int
    data_publicacao: date | None
    ementa: str | None
    texto_integral: str
    url_origem: str
    status_vigencia: str
    orgao_emissor: str | None = None
    relacoes: list[RelacaoBruta] = field(default_factory=list)


class RateLimiter:
    """Limita a no máximo 1 requisição por segundo por instância — a política
    de crawling combinada em research/FONTES.md para os dois domínios."""

    def __init__(self, intervalo_segundos: float = 1.0) -> None:
        self._intervalo = intervalo_segundos
        self._proxima_liberacao = 0.0
        self._lock = asyncio.Lock()

    async def aguardar(self) -> None:
        async with self._lock:
            agora = asyncio.get_event_loop().time()
            espera = self._proxima_liberacao - agora
            if espera > 0:
                await asyncio.sleep(espera)
            self._proxima_liberacao = max(agora, self._proxima_liberacao) + self._intervalo


class AnvisaLegisClient:
    """Cliente HTTP para o AnvisaLegis: rate limit + retry com backoff, como
    exigido na seção 5 do briefing."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": settings.crawl_user_agent}, timeout=30.0
        )
        self._limiter = RateLimiter()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1, max=20),
        reraise=True,
    )
    async def _get(self, url: str, params: dict[str, str] | None = None) -> str:
        await self._limiter.aguardar()
        resp = await self._client.get(f"{BASE_URL}{url}", params=params)
        if resp.status_code >= 500 or resp.status_code == 429:
            resp.raise_for_status()
        resp.raise_for_status()
        # decodificação em duas camadas — ver research/FONTES.md, seção
        # "Encoding — ponto crítico validado".
        return html.unescape(resp.content.decode("iso-8859-1"))

    async def arvore_anos_vigentes(self) -> list[tuple[int, str, int]]:
        """Retorna [(ano, co_tematica, quantidade)] a partir da árvore de
        'Normas por ano' (vigentes). Ver Addendum M2 em research/FONTES.md."""
        texto = await self._get(
            "/action/ActionDatalegis.php",
            params={
                "acao": "recuperarTematicasCollapse",
                "cod_modulo": str(COD_MODULO_310),
                "cod_menu": str(COD_MENU_VIGENTES),
            },
        )
        return parse_arvore_anos(texto)

    async def atos_vigentes_do_ano(self, co_tematica: str) -> str:
        return await self._get(
            "/action/TematicaAction.php",
            params={
                "acao": "abrirVinculos",
                "cotematica": co_tematica,
                "cod_modulo": str(COD_MODULO_310),
                "cod_menu": str(COD_MENU_VIGENTES),
                "popup": "S",
            },
        )

    async def anos_revogadas(self) -> list[int]:
        texto = await self._get(
            "/action/ActionDatalegis.php",
            params={
                "acao": "abrirResenhaAnoData",
                "cod_modulo": str(COD_MODULO_310),
                "cod_menu": str(COD_MENU_REVOGADAS),
            },
        )
        return sorted({int(a) for a in re.findall(r"[?&]ano=(\d{4})", texto)})

    async def atos_revogados_do_ano(self, ano: int) -> list[str]:
        """Retorna uma página por item da lista — o portal pagina em blocos
        de 50 (o parâmetro `qtd_pagina` está fixo em 50 no próprio JS do
        site). Não é um `?pagina=N` simples na mesma URL: a página 1 vem de
        `abrirResenhaAnoData`, o número total de páginas só aparece na
        resposta (separada) de `carregarPaginaResenhaAno`, e as páginas
        seguintes vêm de `abrirPaginaResenhaAno` — nenhuma delas leva `&ano=`
        na URL, o filtro de ano fica em sessão (setado pela primeira
        chamada). Por isso as três chamadas têm que ser feitas em sequência,
        no mesmo cliente/sessão, sem intercalar com outro ano no meio. Ver
        Addendum M2 em research/FONTES.md — sem isso, anos com mais de 50
        revogadas perdem silenciosamente o resto (foi o que causou 1598 em
        vez de 2438 atos na primeira carga histórica)."""
        pagina1 = await self._get(
            "/action/ActionDatalegis.php",
            params={
                "acao": "abrirResenhaAnoData",
                "cod_modulo": str(COD_MODULO_310),
                "cod_menu": str(COD_MENU_REVOGADAS),
                "ano": str(ano),
            },
        )
        paginas = [pagina1]
        if pagina1.count('<article class="ato">') < QTD_POR_PAGINA_REVOGADAS:
            return paginas  # única página, não precisa checar o seletor

        seletor = await self._get(
            "/action/ActionDatalegis.php",
            params={
                "acao": "carregarPaginaResenhaAno",
                "cod_modulo": str(COD_MODULO_310),
                "cod_menu": str(COD_MENU_REVOGADAS),
                "pagina": "1",
            },
        )
        total_paginas = parse_total_paginas(seletor)
        for pagina_n in range(2, total_paginas + 1):
            paginas.append(
                await self._get(
                    "/action/ActionDatalegis.php",
                    params={
                        "acao": "abrirPaginaResenhaAno",
                        "cod_modulo": str(COD_MODULO_310),
                        "cod_menu": str(COD_MENU_REVOGADAS),
                        "qtd_pagina": str(QTD_POR_PAGINA_REVOGADAS),
                        "pagina": str(pagina_n),
                    },
                )
            )
        return paginas


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_RE_ANO_TEMATICA = re.compile(r"letra=(\d{4})\s*\((\d+)\)&(?:amp;)?co_tematica=(\d+)")

_RE_LINKTEXTO = re.compile(
    r"LinkTexto\('([A-Z]*)','(\d*)','(\d*)','(\d*)','([^']*)','([^']*)','([^']*)','([^']*)'\)"
)


def parse_arvore_anos(texto: str) -> list[tuple[int, str, int]]:
    """Extrai (ano, co_tematica, quantidade) da árvore de anos de vigentes."""
    resultado = []
    for ano_str, qtd_str, co_tematica in _RE_ANO_TEMATICA.findall(texto):
        resultado.append((int(ano_str), co_tematica, int(qtd_str)))
    return resultado


_RE_OPTION_PAGINA = re.compile(r"<option value='(\d+)'>")


def parse_total_paginas(seletor_html: str) -> int:
    """Extrai o total de páginas da resposta de `carregarPaginaResenhaAno`
    (um `<select>` com uma `<option>` por página existente). 1 se não achar
    nenhuma opção — resposta inesperada é melhor tratar como "sem mais
    páginas" do que travar o crawler."""
    return max((int(n) for n in _RE_OPTION_PAGINA.findall(seletor_html)), default=1)


def _normalizar_numero(bruto: str) -> str:
    """'1.041' -> '1041', '00000842' -> '842' — string canônica sem zeros à
    esquerda nem separador de milhar, para a unique (tipo_ato, numero, ano)."""
    apenas_digitos = re.sub(r"\D", "", bruto)
    return str(int(apenas_digitos)) if apenas_digitos else bruto


def _sigla_tipo_ato(extenso: str, sigla_explicita: str | None) -> str:
    if sigla_explicita:
        return sigla_explicita.upper()
    chave = extenso.strip().lower()
    return TIPO_ATO_MAP.get(chave, extenso.strip().upper()[:20])


def _extrair_relacoes(bloco_html: str) -> list[RelacaoBruta]:
    relacoes = []
    for m in _RE_LINKTEXTO.finditer(bloco_html):
        tipo, numero, _seq, ano, _orgao, cod_tipo, des_item, _des_item_fim = m.groups()
        if not tipo or not numero or not ano:
            continue
        contexto = bloco_html[max(0, m.start() - 120) : m.start()]
        contexto_texto = re.sub(r"<[^>]+>", " ", contexto)
        tipo_relacao = "referencia"
        for padrao, nome in VERBOS_RELACAO:
            if padrao.search(contexto_texto):
                tipo_relacao = nome
                break
        dispositivo = f"{cod_tipo} {des_item}".strip() if des_item else None
        relacoes.append(
            RelacaoBruta(
                tipo=tipo_relacao,
                destino_tipo_ato=tipo,
                destino_numero=_normalizar_numero(numero),
                destino_ano=int(ano),
                dispositivo=dispositivo or None,
            )
        )
    return relacoes


_RE_PREAMBULO = re.compile(
    r"^\s*(?:\(\*+\)\s*)*(O|A)\s+[A-ZÀ-Ú][A-ZÀ-Ú\s]{3,60}(DA|DO|,)\b|^\s*Considerando\b", re.I
)


def _extrair_ementa(texto_bloco_sem_tags: str, cabecalho_maiusculo: str) -> str | None:
    """A ementa é, por convenção do portal, o primeiro parágrafo depois do
    título do ato em caixa alta ('...Nº X, DE ... DE ANO'). Nem todo ato tem
    uma (atos antigos às vezes vão direto para o preâmbulo, ex.: 'O DIRETOR
    do...') — nesse caso é melhor não ter ementa do que extrair o preâmbulo
    por engano."""
    idx = texto_bloco_sem_tags.find(cabecalho_maiusculo)
    if idx == -1:
        return None
    resto = texto_bloco_sem_tags[idx + len(cabecalho_maiusculo) :].strip()
    # descarta marcador de rodapé tipo "(*)" que às vezes vem colado ao título
    resto = re.sub(r"^\(\*+\)\s*", "", resto).strip()
    if not resto or _RE_PREAMBULO.match(resto):
        return None
    fim = resto.find("\n\n")
    candidato = resto[: fim if fim != -1 else 400].strip()
    return candidato or None


_RE_STRIP_TAGS = re.compile(r"<[^>]+>")


def _texto_visivel(bloco_html: str) -> str:
    sem_tags = _RE_STRIP_TAGS.sub("\n", bloco_html)
    return re.sub(r"[ \t]+", " ", sem_tags).strip()


_RE_CABECALHO_VIGENTES = re.compile(
    r"Ato:\s*(?P<extenso>[^-<]+?)(?:\s*-\s*(?P<sigla>[A-Z]{2,6}))?\s+"
    r"(?:Anvisa\s+)?n[ºo°]\s*(?P<numero>[\d.]+),?\s*de\s*(?P<data>\d{2}/\d{2}/\d{4})",
    re.I,
)

_RE_TITULO_MAIUSCULO = re.compile(
    r"[A-ZÀ-Ú][A-ZÀ-Ú \-.]{0,40}N[ºO°]\s*[\d.]+,?\s*DE\s+\d{1,2}\s+DE\s+[A-ZÀ-Ú]+\s+DE\s+\d{4}"
)


def parse_atos_vigentes(texto: str, url_origem: str) -> list[AtoParseado]:
    """Divide a resposta de `abrirVinculos` (um ano inteiro) em atos
    individuais. Cada ato é delimitado por `<div class="tab_menor">`."""
    blocos = texto.split('<div class="tab_menor">')[1:]
    atos = []
    for bloco in blocos:
        m = _RE_CABECALHO_VIGENTES.search(bloco)
        if not m:
            continue
        tipo_ato = _sigla_tipo_ato(m.group("extenso"), m.group("sigla"))
        numero = _normalizar_numero(m.group("numero"))
        ano = int(m.group("data")[-4:])
        dia, mes, ano_data = (int(p) for p in m.group("data").split("/"))
        data_publicacao = date(ano_data, mes, dia)

        texto_visivel = _texto_visivel(bloco)
        titulo_m = _RE_TITULO_MAIUSCULO.search(texto_visivel)
        ementa = _extrair_ementa(texto_visivel, titulo_m.group(0)) if titulo_m else None

        atos.append(
            AtoParseado(
                tipo_ato=tipo_ato,
                numero=numero,
                ano=ano,
                data_publicacao=data_publicacao,
                ementa=ementa,
                texto_integral=texto_visivel,
                url_origem=url_origem,
                status_vigencia="vigente",
                relacoes=_extrair_relacoes(bloco),
            )
        )
    return atos


_RE_ARTICLE_ATO = re.compile(r'<article class="ato">(.*?)</article>', re.S)
_RE_HREF_TEXTO_ATO = re.compile(
    r"abrirTextoAto&link=S&tipo=([A-Z]+)&numeroAto=(\d+)&seqAto=(\w+)&valorAno=(\d{4})"
    r"&orgao=([^&\"]*)"
)
_RE_EMENTA_P = re.compile(r"<p>(.*?)</p>", re.S)

# Na listagem de revogadas, o aviso "Revogada pela RESOLUÇÃO - RDC Nº 454, DE
# 17 DE DEZEMBRO DE 2020" vem como texto solto dentro da ementa, sem
# LinkTexto (esse é o único lugar onde isso acontece — em abrirTextoAto o
# mesmo aviso já vem com LinkTexto, ver seção 1 do FONTES.md).
_RE_REVOGADA_POR_TEXTO = re.compile(
    r"Revogad[ao]\s+(?P<parcial>parcialmente\s+)?pel[ao]\s+"
    r"(?P<extenso>[A-ZÀ-Ú][A-ZÀ-Ú \-]*?)(?:\s*-\s*(?P<sigla>[A-Z]{2,6}))?\s+"
    r"N[ºO°]\s*(?P<numero>[\d.]+),?\s*DE\s+\d{1,2}\s+DE\s+[A-ZÀ-Ú]+\s+DE\s+(?P<ano>\d{4})",
    re.I,
)


def _extrair_revogacao_textual(texto_ementa: str) -> RelacaoBruta | None:
    m = _RE_REVOGADA_POR_TEXTO.search(texto_ementa)
    if not m:
        return None
    return RelacaoBruta(
        tipo="revoga_parcial" if m.group("parcial") else "revoga",
        destino_tipo_ato=_sigla_tipo_ato(m.group("extenso"), m.group("sigla")),
        destino_numero=_normalizar_numero(m.group("numero")),
        destino_ano=int(m.group("ano")),
        invertida=True,
    )


def parse_atos_revogados(texto: str, url_origem: str) -> list[AtoParseado]:
    """Extrai os itens de `abrirResenhaAnoData&cod_menu=9882&ano=YYYY`.
    Aqui só vem a ementa/aviso de revogação, não o texto integral (ver
    Addendum M2 em research/FONTES.md sobre essa limitação)."""
    atos = []
    for bloco in _RE_ARTICLE_ATO.findall(texto):
        href_m = _RE_HREF_TEXTO_ATO.search(bloco)
        if not href_m:
            continue
        tipo_ato, numero_bruto, _seq, ano_str, orgao = href_m.groups()
        # a ementa de verdade fica no único <p> dentro do bloco; o restante
        # (span/strong) é só o rótulo de situação + a autocitação do ato.
        p_m = _RE_EMENTA_P.search(bloco)
        ementa = _texto_visivel(p_m.group(1)).strip() if p_m else None
        relacoes = _extrair_relacoes(bloco)
        if ementa:
            relacao_textual = _extrair_revogacao_textual(ementa)
            if relacao_textual:
                relacoes.append(relacao_textual)
        atos.append(
            AtoParseado(
                tipo_ato=tipo_ato,
                numero=_normalizar_numero(numero_bruto),
                ano=int(ano_str),
                data_publicacao=None,
                ementa=ementa or None,
                texto_integral="",
                url_origem=url_origem,
                status_vigencia="revogada",
                orgao_emissor=orgao or None,
                relacoes=relacoes,
            )
        )
    return atos


async def carregar_vigentes(cliente: AnvisaLegisClient) -> list[AtoParseado]:
    anos = await cliente.arvore_anos_vigentes()
    todos: list[AtoParseado] = []
    for _ano, co_tematica, _qtd in anos:
        texto = await cliente.atos_vigentes_do_ano(co_tematica)
        url = (
            f"{BASE_URL}/action/TematicaAction.php?acao=abrirVinculos"
            f"&cotematica={co_tematica}&cod_modulo={COD_MODULO_310}"
            f"&cod_menu={COD_MENU_VIGENTES}&popup=S"
        )
        todos.extend(parse_atos_vigentes(texto, url))
    return todos


async def carregar_revogadas(cliente: AnvisaLegisClient) -> list[AtoParseado]:
    anos = await cliente.anos_revogadas()
    todos: list[AtoParseado] = []
    for ano in anos:
        url = (
            f"{BASE_URL}/action/ActionDatalegis.php?acao=abrirResenhaAnoData"
            f"&cod_modulo={COD_MODULO_310}&cod_menu={COD_MENU_REVOGADAS}&ano={ano}"
        )
        for pagina in await cliente.atos_revogados_do_ano(ano):
            todos.extend(parse_atos_revogados(pagina, url))
    return todos

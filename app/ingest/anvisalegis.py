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

# Checados ANTES de `VERBOS_RELACAO` (voz ativa) — achado real processando
# o texto integral de uma norma revogada pela primeira vez (o texto dela
# começa com "Revogada pela RDC X"): "revog" sozinho casa tanto com
# "revoga a RDC X" (ESTE ato revoga o outro) quanto com "revogada pela RDC
# X" (ESTE ato é quem foi revogado) — direções opostas. Nunca apareceu
# antes porque só processávamos o texto de normas VIGENTES (que, por
# definição, não têm um aviso de auto-revogação no próprio texto); passou
# a importar ao processar o texto de normas revogadas (backfill). Sem essa
# distinção, `salvar_ato` gravaria a relação ao contrário e o
# pós-processamento do M2 ("promove pra revogada quem é alvo de uma
# relação revoga") marcaria a norma revogadora — que está vigente — como
# revogada por engano.
VERBOS_RELACAO_PASSIVA: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"revogad[ao]\s+parcialmente\s+pel[ao]", re.I), "revoga_parcial"),
    (re.compile(r"revogad[ao]\s+pel[ao]", re.I), "revoga"),
    (re.compile(r"alterad[ao]\s+pel[ao]|reda[cç][aã]o\s+dada\s+pel[ao]", re.I), "altera"),
    (re.compile(r"retificad[ao]\s+pel[ao]", re.I), "retifica"),
    (re.compile(r"regulamentad[ao]\s+pel[ao]", re.I), "regulamenta"),
    (re.compile(r"substitu[íi]d[ao]\s+pel[ao]", re.I), "substitui"),
]

# Um "(Revogado pela X)" logo depois de um rótulo de artigo/inciso/alínea
# (ex.: "Art. 12 -  (Revogado pela X)", "a)  (Revogado pela X)") é a
# revogação de UM DISPOSITIVO só — o ato inteiro continua vigente. Achado
# real processando o texto de uma norma vigente de 1966 cheia de incisos
# revogados individualmente ao longo dos anos sem que o ato como um todo
# saísse de vigor: sem essa distinção, isso viraria uma relação `revoga`
# (não `revoga_parcial`) apontando pro ato que revogou só o inciso, e o
# pós-processamento do M2 (`_derivar_status_por_relacao`) marcaria esse
# ato — que pode estar perfeitamente vigente — como revogado por engano.
# Já "Revogada pela X" sozinho, sem rótulo de dispositivo antes (como
# aparece no início da própria página de uma norma revogada por inteiro),
# fica como `revoga` mesmo — é essa a leitura correta.
#
# **Bug real pego em produção (RDC 243/2018 e outras)**: o rótulo pode vir
# seguido de um ponto final antes do "(Revogado pela X)" — "Parágrafo
# único.\xa0\n (Revogado pela X)" é o padrão real do HTML do AnvisaLegis
# pra parágrafo único (diferente de "Art. 12 -" e "III -", que não têm
# ponto). Sem tolerar esse `\.?`, o regex não batia, `_revoga_so_um_
# dispositivo` devolvia False, e a revogação de um parágrafo único virava
# `revoga` (total) em vez de `revoga_parcial` — promovendo normas
# genuinamente vigentes (confirmado ao vivo contra o portal) a 'revogada'.
#
# **Segundo bug real pego em produção (RDC 818/2023 e outras)**: "ANEXO
# III" (revogação de um anexo inteiro, ex.: "ANEXO III\xa0\n\n\n(Revogado
# pela X)") também não derruba o ato inteiro — mesma lógica de "Art. N" —
# mas não estava na lista de rótulos reconhecidos. E "ANEXO" sozinho, sem
# número (achado real, RDC 242/2018) — quando o ato só tem um anexo — por
# isso o número depois de "anexo" é opcional aqui. Mesma coisa pra outras
# unidades estruturais da hierarquia jurídica que também podem ser
# revogadas por inteiro sem derrubar o ato (achado real, RDC 708/2022:
# "CAPÍTULO XIX\n\n\n(Revogado pela X)") — capítulo/seção/subseção/título,
# com ou sem numeral romano.
_RE_ROTULO_DISPOSITIVO_ANTES = re.compile(
    r"(art\.?\s*\d+[ºo°]?|§\s*\d+[ºo°]?|par[áa]grafo\s+[úu]nico"
    r"|anexo\b(?:\s+[ivxlcm\d]+)?"
    r"|(?:sub)?se[çc][ãa]o\b(?:\s+[ivxlcm\d]+)?"
    r"|cap[íi]tulo\b(?:\s+[ivxlcm\d]+)?"
    r"|t[íi]tulo\b(?:\s+[ivxlcm\d]+)?"
    r"|\b[a-z]\)?|\b[ivxlcm]+[).]|\b[ivxlcm]+\s*[-–—])"
    r"\.?\s*[-–—]?\s*\(?\s*$",
    re.I,
)


def _revoga_so_um_dispositivo(texto_antes_do_verbo: str) -> bool:
    return bool(_RE_ROTULO_DISPOSITIVO_ANTES.search(texto_antes_do_verbo.strip()))


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
        # 30s não bastava pra alguns anos de vigentes (respostas de 600KB+,
        # ex. 1966) — achado real rodando o script de remediação da direção
        # de "revoga" (ver CLAUDE.md): deu ReadTimeout mais de uma vez.
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": settings.crawl_user_agent}, timeout=90.0
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

    async def texto_individual_do_ato(
        self, tipo: str, numero_bruto: str, seq: str, ano: str, orgao: str
    ) -> str:
        """Página de detalhe de um único ato — funciona pra vigente ou
        revogado (achado ao construir o backfill de texto das revogadas:
        `abrirTextoAto` não é exclusivo do módulo 630/participação social,
        onde foi descoberto primeiro). Devolve a página HTML inteira; quem
        chama usa `extrair_texto_ato` pra tirar só o conteúdo de verdade.
        `cod_menu=9882` (revogadas) funciona tanto pra revogada quanto pra
        vigente — confirmado com requisição real, ver research/FONTES.md."""
        return await self._get(
            "/action/ActionDatalegis.php",
            params={
                "acao": "abrirTextoAto",
                "link": "S",
                "tipo": tipo,
                "numeroAto": numero_bruto,
                "seqAto": seq,
                "valorAno": ano,
                "orgao": orgao,
                "cod_modulo": str(COD_MODULO_310),
                "cod_menu": str(COD_MENU_REVOGADAS),
            },
        )

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
        # 120 chars bastava pra "Art. N -"/"III -" mas não pra "ANEXO III"
        # (achado real, RDC 818/2023): o rótulo fica dentro de tags extras
        # (</b>, <p class="link-revogado">, <em ...>) antes do "(Revogado
        # pela X)" — só 193 chars de HTML bruto até o LinkTexto. Só o fim
        # da janela importa (`_RE_ROTULO_DISPOSITIVO_ANTES` ancora em `$`),
        # então alargar não arrisca pegar o rótulo errado de um dispositivo
        # anterior.
        contexto = bloco_html[max(0, m.start() - 300) : m.start()]
        contexto_texto = re.sub(r"<[^>]+>", " ", contexto)
        tipo_relacao = "referencia"
        invertida = False
        for padrao, nome in VERBOS_RELACAO_PASSIVA:
            # última ocorrência na janela, não a primeira — com a janela
            # alargada (300 chars) pra cobrir "ANEXO III", uma janela maior
            # pode conter a menção de OUTRO dispositivo mais atrás; a mais
            # próxima do LinkTexto atual é a que de fato o descreve.
            ocorrencias = list(padrao.finditer(contexto_texto))
            m_verbo = ocorrencias[-1] if ocorrencias else None
            if m_verbo:
                if nome == "revoga" and _revoga_so_um_dispositivo(
                    contexto_texto[: m_verbo.start()]
                ):
                    nome = "revoga_parcial"
                tipo_relacao = nome
                invertida = True
                break
        else:
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
                invertida=invertida,
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


@dataclass
class IndiceAtoRevogado:
    """Só o suficiente pra buscar o texto individual depois — o `seqAto`
    (descartado como `_seq` em `parse_atos_revogados`, que só quer a
    ementa) é o dado que faltava pra isso funcionar."""

    tipo_ato: str
    numero_bruto: str
    seq: str
    ano: str
    orgao: str


def parse_indices_revogados(texto: str) -> list[IndiceAtoRevogado]:
    """Mesma extração de `parse_atos_revogados`, mas preservando o
    `seqAto` — usado só pelo backfill de texto integral
    (`scripts/backfill_texto_revogadas.py`), não pela carga histórica
    normal (que não precisa do texto individual pra cada uma das 2.438)."""
    indices = []
    for bloco in _RE_ARTICLE_ATO.findall(texto):
        href_m = _RE_HREF_TEXTO_ATO.search(bloco)
        if not href_m:
            continue
        tipo_ato, numero_bruto, seq, ano_str, orgao = href_m.groups()
        indices.append(IndiceAtoRevogado(tipo_ato, numero_bruto, seq, ano_str, orgao))
    return indices


_RE_STATUS_BADGE = re.compile(r'ico-situacao\s+[a-z]+\s+status-\d+"\s+title="([^"]+)"')
_RE_CONTEUDO_ATO = re.compile(r'<div class="ato">(.*?)<p[^>]*>Este texto não substitui', re.S)


def extrair_texto_ato(pagina_html: str) -> tuple[str, list[RelacaoBruta]] | None:
    """Extrai o texto integral e as relações (`LinkTexto`) da página de
    detalhe de um ato (`abrirTextoAto`) — `None` se a página não tiver o
    formato esperado (ato não encontrado, por exemplo). Corta no aviso
    fixo "Este texto não substitui a Publicação Oficial" em vez de casar
    `</div>` (o `<div class="ato">` não tem `<div>` aninhado nas amostras
    reais, mas cortar num texto fixo e sempre presente é mais robusto que
    contar chaves). Ver `research/samples/ato_individual_res13_1978.html`."""
    m = _RE_CONTEUDO_ATO.search(pagina_html)
    if not m:
        return None
    bruto = m.group(1)
    return bruto, _extrair_relacoes(bruto)


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

"""Ingestor do módulo 630 do AnvisaLegis (participação social — consultas
públicas). Ver `research/FONTES.md`, Addendum M5, para como cada endpoint
foi descoberto e validado com requisições reais.

Guardamos consulta pública na tabela `noticia` (`categoria=
'consulta_publica'`), não numa tabela própria — é pra isso que o schema já
previa essa categoria (ver seção 4 do briefing e `CLAUDE.md`, M5).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

import asyncpg
import asyncpg.pool

from app.ingest.anvisalegis import BASE_URL, AnvisaLegisClient

Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

COD_MODULO_630 = 630
COD_MENU_CP_ATIVA = 9789


@dataclass
class ConsultaPublicaParseada:
    tipo_ato: str
    numero: str
    ano: int
    titulo: str
    data_dou: date | None
    status: str | None
    prazo_inicio: date | None
    prazo_fim: date | None
    assunto: str | None
    situacao: str | None
    url_pdf: str | None
    url_origem: str


_RE_ARTICLE_ATO = re.compile(r'<article class="ato">(.*?)</article>', re.S)
_RE_HREF_TEXTO_ATO = re.compile(
    r"abrirTextoAto&link=S&tipo=([A-Z]+)&numeroAto=(\d+)&seqAto=(\w+)&valorAno=(\d{4})"
    r"&orgao=([^&\"]*)"
)


def _normalizar_numero(bruto: str) -> str:
    apenas_digitos = re.sub(r"\D", "", bruto)
    return str(int(apenas_digitos)) if apenas_digitos else bruto


def parse_lista_cps(texto: str) -> list[tuple[str, str, str, str, str]]:
    """[(tipo, numeroAto, seqAto, valorAno, orgao)] de cada CP na listagem
    de ativas — mesmo formato `<article class="ato">` do módulo 310."""
    resultado = []
    for bloco in _RE_ARTICLE_ATO.findall(texto):
        m = _RE_HREF_TEXTO_ATO.search(bloco)
        if m:
            tipo, numero, seq, ano, orgao = m.groups()
            resultado.append((tipo, numero, seq, ano, orgao))
    return resultado


_RE_CAMPO = re.compile(r'id="(\w+)">\s*<label>[^<]*</label>[\s\xa0]*([^<]*)</p>')
_RE_TITULO = re.compile(r'<h2 id="titulo"[^>]*>.*?>([^<]+)</a>', re.S)
_RE_PDF = re.compile(r'href="(https://anexosportal\.datalegis\.net/arquivos/[^"]+)"')
_RE_DATA = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _parse_data(texto: str | None) -> date | None:
    if not texto:
        return None
    m = _RE_DATA.search(texto)
    if not m:
        return None
    dia, mes, ano = (int(p) for p in m.groups())
    try:
        return date(ano, mes, dia)
    except ValueError:
        return None


def parse_cp_detalhe(
    texto: str, tipo_ato: str, numero: str, ano: int, url_origem: str
) -> ConsultaPublicaParseada:
    """Extrai os campos com `id` fixo da página de detalhe de uma consulta
    pública (`#dataDou`, `#status`, `#prazoContribuicao`, `#prorrogacaoPrazo`,
    `#assunto`, `#situacao`) — ver Addendum M5 em research/FONTES.md."""
    campos = {chave: valor.strip() for chave, valor in _RE_CAMPO.findall(texto)}

    titulo_m = _RE_TITULO.search(texto)
    titulo = titulo_m.group(1).strip() if titulo_m else f"{tipo_ato} {numero}/{ano}"

    pdf_m = _RE_PDF.search(texto)
    url_pdf = pdf_m.group(1) if pdf_m else None

    # "27/08/2026 a 25/10/2026" -> (27/08/2026, 25/10/2026); usa a
    # prorrogação como novo fim, se houver (é uma data isolada, sem "a").
    prazo_bruto = campos.get("prazoContribuicao", "")
    datas_prazo = _RE_DATA.findall(prazo_bruto)
    prazo_inicio = (
        date(int(datas_prazo[0][2]), int(datas_prazo[0][1]), int(datas_prazo[0][0]))
        if datas_prazo
        else None
    )
    prazo_fim = (
        date(int(datas_prazo[1][2]), int(datas_prazo[1][1]), int(datas_prazo[1][0]))
        if len(datas_prazo) > 1
        else None
    )
    prorrogacao = _parse_data(campos.get("prorrogacaoPrazo"))
    if prorrogacao:
        prazo_fim = prorrogacao

    return ConsultaPublicaParseada(
        tipo_ato=tipo_ato,
        numero=numero,
        ano=ano,
        titulo=titulo,
        data_dou=_parse_data(campos.get("dataDou")),
        status=campos.get("status") or None,
        prazo_inicio=prazo_inicio,
        prazo_fim=prazo_fim,
        assunto=campos.get("assunto") or None,
        situacao=campos.get("situacao") or None,
        url_pdf=url_pdf,
        url_origem=url_origem,
    )


async def carregar_cps_ativas(cliente: AnvisaLegisClient) -> list[ConsultaPublicaParseada]:
    texto_lista = await cliente._get(
        "/action/ActionDatalegis.php",
        params={
            "acao": "abrirResenhaAno",
            "cod_modulo": str(COD_MODULO_630),
            "cod_menu": str(COD_MENU_CP_ATIVA),
        },
    )
    itens = parse_lista_cps(texto_lista)
    resultado = []
    for tipo, numero_bruto, seq, ano_str, orgao in itens:
        url_detalhe = (
            f"{BASE_URL}/action/ActionDatalegis.php?acao=abrirTextoAto&link=S"
            f"&tipo={tipo}&numeroAto={numero_bruto}&seqAto={seq}&valorAno={ano_str}"
            f"&orgao={orgao}&cod_modulo={COD_MODULO_630}&cod_menu={COD_MENU_CP_ATIVA}"
        )
        texto_detalhe = await cliente._get(
            "/action/ActionDatalegis.php",
            params={
                "acao": "abrirTextoAto",
                "link": "S",
                "tipo": tipo,
                "numeroAto": numero_bruto,
                "seqAto": seq,
                "valorAno": ano_str,
                "orgao": orgao,
                "cod_modulo": str(COD_MODULO_630),
                "cod_menu": str(COD_MENU_CP_ATIVA),
            },
        )
        numero = _normalizar_numero(numero_bruto)
        resultado.append(parse_cp_detalhe(texto_detalhe, tipo, numero, int(ano_str), url_detalhe))
    return resultado


async def upsert_consulta_publica(conn: Conn, cp: ConsultaPublicaParseada) -> None:
    conteudo = cp.situacao or ""
    hash_conteudo = hashlib.sha256(conteudo.encode("utf-8")).hexdigest() if conteudo else None
    await conn.execute(
        """
        insert into noticia (
            titulo, resumo, conteudo, categoria, data_publicacao, url,
            hash_conteudo, prazo_inicio, prazo_fim
        )
        values ($1, $2, $3, 'consulta_publica', $4, $5, $6, $7, $8)
        on conflict (url) do update set
            titulo        = excluded.titulo,
            resumo        = excluded.resumo,
            conteudo      = excluded.conteudo,
            data_publicacao = coalesce(excluded.data_publicacao, noticia.data_publicacao),
            hash_conteudo = excluded.hash_conteudo,
            prazo_inicio  = coalesce(excluded.prazo_inicio, noticia.prazo_inicio),
            prazo_fim     = coalesce(excluded.prazo_fim, noticia.prazo_fim)
        """,
        cp.titulo,
        cp.assunto,
        cp.situacao,
        cp.data_dou,
        cp.url_origem,
        hash_conteudo,
        cp.prazo_inicio,
        cp.prazo_fim,
    )

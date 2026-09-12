"""Ingestor de notícias do gov.br/anvisa.

O RSS documentado no briefing não existe mais (404) — o site é Plone+Volto e
a página renderizada só embute `window.__data`, cujo `b_start` é ignorado no
SSR (sempre devolve a página 1, não importa o que a URL peça). A alternativa
real e validada é a API REST do Plone, exposta no mesmo domínio sob
`++api++`: `GET https://www.gov.br/anvisa/++api++/<caminho>` devolve o mesmo
JSON do `window.__data`, mas com paginação (`b_start`) de verdade. Ver
`research/FONTES.md`, Addendum M5, para o passo a passo de como isso foi
descoberto e validado com requisições reais.

Cada notícia é um "content item" Volto com corpo em blocos (`blocks` +
`blocks_layout`); os únicos tipos de bloco com texto de fato são `html`
(HTML cru, precisa stripar tags) e `slate` (já vem com `plaintext` pronto).
Blocos como `title`/`description`/`leadimage`/`textToSpeech` são descartados
na montagem do conteúdo (o título e o resumo já vêm de campos próprios).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

import asyncpg
import asyncpg.pool
import httpx
from selectolax.parser import HTMLParser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.config import get_settings
from app.ingest.anvisalegis import RateLimiter

Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

BASE_API = "https://www.gov.br/anvisa/++api++/pt-br/assuntos/noticias-anvisa"
BASE_SITE = "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa"
ITENS_POR_PAGINA = 25

_TIPOS_BLOCO_TEXTO = {"html", "slate"}


@dataclass
class NoticiaListada:
    url: str
    titulo: str
    resumo: str | None
    data_publicacao: datetime | None


@dataclass
class NoticiaCompleta:
    url: str
    titulo: str
    resumo: str | None
    conteudo: str
    data_publicacao: datetime | None


def _parse_data(bruto: str | None) -> datetime | None:
    if not bruto:
        return None
    try:
        return datetime.fromisoformat(bruto)
    except ValueError:
        return None


def _item_para_listado(item: dict[str, object]) -> NoticiaListada:
    effective = item.get("effective")
    return NoticiaListada(
        url=str(item["@id"]),
        titulo=str(item.get("title") or ""),
        resumo=str(item["description"]) if item.get("description") else None,
        data_publicacao=_parse_data(effective if isinstance(effective, str) else None),
    )


def _texto_de_html(bruto: str) -> str:
    return HTMLParser(bruto).text(separator=" ", strip=True)


def montar_conteudo(blocks: dict[str, dict[str, object]], blocks_layout: list[str]) -> str:
    """Concatena, na ordem de `blocks_layout`, o texto dos blocos `html` e
    `slate` — os únicos tipos com corpo textual real (ver módulo docstring).
    """
    partes: list[str] = []
    for bloco_id in blocks_layout:
        bloco = blocks.get(bloco_id)
        if not bloco:
            continue
        tipo = bloco.get("@type")
        if tipo not in _TIPOS_BLOCO_TEXTO:
            continue
        if tipo == "slate":
            texto = str(bloco.get("plaintext") or "").strip()
        else:
            texto = _texto_de_html(str(bloco.get("html") or "")).strip()
        if texto:
            partes.append(texto)
    return "\n\n".join(partes)


class GovBrClient:
    """Cliente HTTP para a API REST do Plone em gov.br/anvisa — mesmo
    esquema de rate limit + retry do `AnvisaLegisClient`, domínio diferente.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": settings.crawl_user_agent,
                "Accept": "application/json",
            },
            timeout=30.0,
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
    async def _get_json(self, url: str, params: dict[str, str | int] | None = None) -> dict:
        await self._limiter.aguardar()
        resp = await self._client.get(url, params=params)
        if resp.status_code >= 500 or resp.status_code == 429:
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    async def listar_pagina(self, ano: int, b_start: int = 0) -> tuple[int, list[NoticiaListada]]:
        """Uma página (25 itens) da listagem de notícias do ano. Retorna
        (items_total, itens)."""
        dados = await self._get_json(f"{BASE_API}/{ano}", params={"b_start": b_start})
        itens = [_item_para_listado(i) for i in dados.get("items", [])]
        return int(dados.get("items_total", 0)), itens

    async def listar_todas_do_ano(self, ano: int) -> list[NoticiaListada]:
        b_start = 0
        total, primeira_pagina = await self.listar_pagina(ano, b_start=0)
        resultado = list(primeira_pagina)
        b_start += ITENS_POR_PAGINA
        while b_start < total:
            _, pagina = await self.listar_pagina(ano, b_start=b_start)
            if not pagina:
                break
            resultado.extend(pagina)
            b_start += ITENS_POR_PAGINA
        return resultado

    async def carregar_completa(self, url_item: str) -> NoticiaCompleta:
        """`url_item` é a URL "de site" (`.../noticias-anvisa/2026/algo`); a
        API REST mora no mesmo caminho sob o host `++api++`."""
        caminho = url_item.removeprefix(BASE_SITE)
        dados = await self._get_json(f"{BASE_API}{caminho}")
        conteudo = montar_conteudo(
            dados.get("blocks", {}),
            dados.get("blocks_layout", {}).get("items", []),
        )
        effective = dados.get("effective")
        return NoticiaCompleta(
            url=url_item,
            titulo=str(dados.get("title") or ""),
            resumo=str(dados["description"]) if dados.get("description") else None,
            conteudo=conteudo,
            data_publicacao=_parse_data(effective if isinstance(effective, str) else None),
        )


_RE_NAO_NOTICIA = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip)$", re.I)


def eh_noticia_de_verdade(url: str) -> bool:
    """A listagem mistura anexos soltos (PDF etc. hospedados como item da
    pasta do ano) com notícias reais — filtra pelo padrão de extensão de
    arquivo no fim da URL (ver amostra real com `.pdf` na Addendum M5)."""
    return not _RE_NAO_NOTICIA.search(url)


def calcular_hash(conteudo: str) -> str:
    return hashlib.sha256(conteudo.encode("utf-8")).hexdigest()


async def noticia_ja_existe_com_hash(conn: Conn, url: str, hash_conteudo: str) -> bool:
    """Usado para o corte incremental: se a notícia já está gravada com o
    mesmo hash, não precisamos gastar outra requisição golpeando o mesmo
    conteúdo de novo em execuções futuras (a listagem em si é sempre
    revisitada, é só o detalhe que pulamos)."""
    existente = await conn.fetchval("select hash_conteudo from noticia where url = $1", url)
    return existente is not None and existente == hash_conteudo


async def upsert_noticia(conn: Conn, noticia: NoticiaCompleta) -> None:
    hash_conteudo = calcular_hash(noticia.conteudo)
    await conn.execute(
        """
        insert into noticia (
            titulo, resumo, conteudo, categoria, data_publicacao, url,
            hash_conteudo
        )
        values ($1, $2, $3, 'noticia', $4, $5, $6)
        on conflict (url) do update set
            titulo          = excluded.titulo,
            resumo          = excluded.resumo,
            conteudo        = excluded.conteudo,
            data_publicacao = coalesce(excluded.data_publicacao, noticia.data_publicacao),
            hash_conteudo   = excluded.hash_conteudo
        """,
        noticia.titulo,
        noticia.resumo,
        noticia.conteudo,
        noticia.data_publicacao,
        noticia.url,
        hash_conteudo,
    )

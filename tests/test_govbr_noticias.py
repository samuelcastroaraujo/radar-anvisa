"""Testes do ingestor de notícias do gov.br/anvisa contra amostras reais da
API REST do Plone (`++api++`) — ver research/FONTES.md, Addendum M5.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.ingest.govbr_noticias import (
    BASE_API,
    GovBrClient,
    calcular_hash,
    eh_noticia_de_verdade,
    montar_conteudo,
)

SAMPLES = Path(__file__).resolve().parent.parent / "research" / "samples"


def _carregar(nome: str) -> dict:
    return json.loads((SAMPLES / nome).read_text(encoding="utf-8"))


def test_montar_conteudo_extrai_blocos_html() -> None:
    dados = _carregar("govbr_noticia_detalhe_html_blocks.json")
    texto = montar_conteudo(dados["blocks"], dados["blocks_layout"]["items"])
    assert "Denominações Comuns Brasileiras" in texto
    assert "<p" not in texto  # tags stripadas
    assert "22 novas DCBs" in texto


def test_montar_conteudo_extrai_blocos_slate() -> None:
    dados = _carregar("govbr_noticia_detalhe_slate_blocks.json")
    texto = montar_conteudo(dados["blocks"], dados["blocks_layout"]["items"])
    assert "protocolo de investigação clínica" in texto


def test_eh_noticia_de_verdade_filtra_anexos() -> None:
    assert eh_noticia_de_verdade(
        "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa/2026/alguma-noticia"
    )
    assert not eh_noticia_de_verdade(
        "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa/2026/NOTIFICAO.pdf"
    )


def test_calcular_hash_estavel() -> None:
    assert calcular_hash("abc") == calcular_hash("abc")
    assert calcular_hash("abc") != calcular_hash("abd")


async def test_listar_pagina_usa_api_rest() -> None:
    listagem = _carregar("govbr_listagem_2026_b0.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/anvisa/++api++/pt-br/assuntos/noticias-anvisa/2026"
        assert request.url.params.get("b_start") == "0"
        return httpx.Response(200, json=listagem)

    transport = httpx.MockTransport(handler)
    cliente = GovBrClient(client=httpx.AsyncClient(transport=transport))
    total, itens = await cliente.listar_pagina(2026, b_start=0)
    await cliente.aclose()

    assert total == 572
    assert len(itens) == 25
    assert itens[0].titulo.startswith("Anvisa publica lista atualizada")


async def test_carregar_completa_usa_api_rest() -> None:
    detalhe = _carregar("govbr_noticia_detalhe_html_blocks.json")
    url_site = (
        "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa/2026/"
        "anvisa-publica-lista-atualizada-das-denominacoes-comuns-brasileiras"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(BASE_API)
        return httpx.Response(200, json=detalhe)

    transport = httpx.MockTransport(handler)
    cliente = GovBrClient(client=httpx.AsyncClient(transport=transport))
    noticia = await cliente.carregar_completa(url_site)
    await cliente.aclose()

    assert noticia.url == url_site
    assert "Denominações Comuns Brasileiras" in noticia.conteudo

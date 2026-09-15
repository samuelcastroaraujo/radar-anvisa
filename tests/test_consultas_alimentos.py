"""Testes do cliente de consulta de registro de alimentos/suplementos
contra respostas reais da API — ver research/FONTES.md, Addendum pós-M7.

Não usa `httpx.MockTransport` (o cliente real é `curl_cffi`, que não tem um
transporte mock equivalente) — em vez disso injeta um `FakeSession` que
imita só o pedaço da interface que `ConsultasAnvisaClient` usa (`get`/
`close`), suficiente pra testar parsing e tratamento de erro sem rede.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from curl_cffi.requests.exceptions import HTTPError

from app.ingest.consultas_alimentos import (
    BASE_URL,
    ConsultasAnvisaClient,
    _eh_processo_nao_encontrado,
    _parse_resultado,
    buscar_produtos,
    detalhe_produto,
)

SAMPLES = Path(__file__).resolve().parent.parent / "research" / "samples"


def _carregar(nome: str) -> dict:
    return json.loads((SAMPLES / nome).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"status {self.status_code}", response=self)


class FakeSession:
    """Duplo de teste pra `_SessaoHTTP` — grava as chamadas feitas pra
    inspeção e devolve o que o `handler` do teste decidir."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.chamadas: list[tuple[str, dict[str, str] | None]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        params = kwargs.get("params")
        self.chamadas.append((url, params))
        return self._handler(url, params)

    async def close(self) -> None:
        pass


async def test_buscar_produtos_sem_filtro_nao_bate_na_rede() -> None:
    """A API rejeita busca vazia (MSG-004, confirmado ao vivo) — validado
    localmente antes de gastar uma requisição pra reaprender isso."""

    def handler(url: str, params: dict[str, str] | None) -> FakeResponse:
        raise AssertionError("não deveria ter feito requisição nenhuma")

    cliente = ConsultasAnvisaClient(sessao=FakeSession(handler))
    with pytest.raises(ValueError, match="ao menos um filtro"):
        await buscar_produtos(cliente)
    await cliente.aclose()


async def test_buscar_produtos_situacao_registro_sozinho_e_filtro_valido() -> None:
    """`situacao_registro` sozinho (sem nome/marca) já satisfaz o "pelo
    menos um filtro" — mesmo comportamento da página oficial (dá pra
    "listar todos os ativos")."""
    listagem = _carregar("consultas_alimentos_busca_whey.json")

    def handler(url: str, params: dict[str, str] | None) -> FakeResponse:
        assert params is not None
        assert params.get("filter[situacaoProduto]") == "S"
        assert "filter[nomeProduto]" not in params
        return FakeResponse(200, listagem)

    cliente = ConsultasAnvisaClient(sessao=FakeSession(handler))
    resultado = await buscar_produtos(cliente, situacao_registro="Ativo", tamanho_pagina=10)
    await cliente.aclose()
    assert len(resultado.itens) == 10


async def test_buscar_produtos_situacao_registro_traduz_para_letra_da_api() -> None:
    """Achado real, confirmado ao vivo: a API espera 'S'/'N' em
    `filter[situacaoProduto]`, não 'Ativo'/'Inativo' — a tradução fica
    só dentro do módulo, a API do RADAR ANVISA expõe o texto legível."""
    listagem = _carregar("consultas_alimentos_busca_whey.json")

    def handler_inativo(url: str, params: dict[str, str] | None) -> FakeResponse:
        assert params is not None
        assert params.get("filter[situacaoProduto]") == "N"
        return FakeResponse(200, listagem)

    cliente = ConsultasAnvisaClient(sessao=FakeSession(handler_inativo))
    await buscar_produtos(cliente, nome_produto="whey", situacao_registro="Inativo")
    await cliente.aclose()


async def test_buscar_produtos_situacao_registro_invalido_nao_bate_na_rede() -> None:
    def handler(url: str, params: dict[str, str] | None) -> FakeResponse:
        raise AssertionError("não deveria ter feito requisição nenhuma")

    cliente = ConsultasAnvisaClient(sessao=FakeSession(handler))
    with pytest.raises(ValueError, match="situacao_registro inválido"):
        await buscar_produtos(cliente, nome_produto="whey", situacao_registro="ativo")
    await cliente.aclose()


async def test_construcao_padrao_manda_referer_e_authorization() -> None:
    """Achado real: sem `Referer` o Cloudflare bloqueia com 403 mesmo com
    `curl_cffi` — trava aqui se algum dia sumir do cliente por engano."""
    cliente = ConsultasAnvisaClient()
    try:
        headers = cliente._sessao.headers  # type: ignore[attr-defined]
        assert headers["referer"] == f"{BASE_URL}/"
        assert headers["authorization"] == "Guest"
    finally:
        await cliente.aclose()


async def test_buscar_produtos_usa_a_api_real() -> None:
    listagem = _carregar("consultas_alimentos_busca_whey.json")

    def handler(url: str, params: dict[str, str] | None) -> FakeResponse:
        assert url == f"{BASE_URL}/api/consulta/alimento/produtos/"
        assert params is not None
        assert params.get("filter[nomeProduto]") == "whey"
        assert params.get("page") == "1"
        return FakeResponse(200, listagem)

    cliente = ConsultasAnvisaClient(sessao=FakeSession(handler))
    resultado = await buscar_produtos(cliente, nome_produto="whey", tamanho_pagina=10)
    await cliente.aclose()

    # a amostra real tem 11 itens em "content" pra um "size":10 pedido —
    # achado real: a API sempre devolve count+1 (ver docstring de
    # `_parse_resultado`); cortamos pro tamanho pedido pra não vazar isso.
    assert resultado.tamanho_pagina == 10
    assert len(resultado.itens) == 10
    primeiro = resultado.itens[0]
    assert "WHEY" in primeiro.descricao
    assert primeiro.situacao_registro == "Ativo"
    assert primeiro.tipo_regularizacao == "Notificado"
    assert primeiro.detentor_cnpj == "22810604000136"
    assert primeiro.numero_processo == "25351130780202618"
    assert primeiro.url_origem == f"{BASE_URL}/#/alimentos/25351130780202618/"


async def test_detalhe_produto_extrai_categorias_e_marcas() -> None:
    detalhe = _carregar("consultas_alimentos_detalhe.json")

    def handler(url: str, params: dict[str, str] | None) -> FakeResponse:
        assert url == f"{BASE_URL}/api/consulta/alimento/produtos/25351130780202618"
        return FakeResponse(200, detalhe)

    cliente = ConsultasAnvisaClient(sessao=FakeSession(handler))
    produto = await detalhe_produto(cliente, "25351130780202618")
    await cliente.aclose()

    assert produto is not None
    assert "Suplementos alimentares" in produto.categorias
    assert "ABSOLUT NUTRITION" in produto.marcas
    assert produto.data_regularizacao is not None
    assert produto.data_regularizacao.year == 2026


async def test_detalhe_produto_processo_inexistente_retorna_none() -> None:
    """A API usa HTTP 500 (não 404) pra processo inexistente — achado real,
    não suposto. `detalhe_produto` traduz isso pra `None` sem propagar."""
    erro = _carregar("consultas_alimentos_processo_inexistente.json")

    def handler(url: str, params: dict[str, str] | None) -> FakeResponse:
        return FakeResponse(500, erro)

    cliente = ConsultasAnvisaClient(sessao=FakeSession(handler))
    produto = await detalhe_produto(cliente, "99999999999999999")
    await cliente.aclose()

    assert produto is None


def test_parse_resultado_corta_pro_tamanho_pedido() -> None:
    """Achado real, confirmado ao vivo com termos de busca diferentes: a
    API sempre devolve `size+1` itens em `content`, com o último item de
    uma página igual ao primeiro da próxima (bug de paginação no backend
    deles, não nosso). `_parse_resultado` corta pro `size` pedido —
    verificado ao vivo que isso dá uma sequência sem lacuna nem
    duplicata ao percorrer páginas."""
    bruto = _carregar("consultas_alimentos_busca_whey.json")
    assert bruto["size"] == 10
    assert len(bruto["content"]) == 11  # o "+1" real, direto da amostra

    resultado = _parse_resultado(bruto)
    assert resultado.tamanho_pagina == 10
    assert len(resultado.itens) == 10
    # não sobra rastro de totalElements/totalPages — são inconfiáveis
    # (função só do `count` pedido, não do resultado real da busca).
    assert not hasattr(resultado, "total_elementos")
    assert not hasattr(resultado, "total_paginas")


def test_eh_processo_nao_encontrado_nao_confunde_com_500_de_verdade() -> None:
    """Só o 500 específico de "processo inexistente" deve virar `None` em
    `detalhe_produto` — qualquer outro 500 precisa continuar sendo um erro
    de verdade (propagado, e sujeito a retry — testado à parte pra não
    depender do backoff real do `tenacity` aqui)."""
    erro = _carregar("consultas_alimentos_processo_inexistente.json")
    resp_conhecido = FakeResponse(500, erro)
    exc_conhecido = HTTPError("x", response=resp_conhecido)
    assert _eh_processo_nao_encontrado(exc_conhecido)

    resp_generico = FakeResponse(500, {"error": "erro genérico de servidor"})
    exc_generico = HTTPError("x", response=resp_generico)
    assert not _eh_processo_nao_encontrado(exc_generico)

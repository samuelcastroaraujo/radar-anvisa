"""Testes do endpoint `/produtos/alimentos` (app/main.py) — cobrem só o
mapeamento de erro pra HTTP, não a lógica de busca em si (isso já é
coberto contra amostras reais em tests/test_consultas_alimentos.py).

Regressão de um achado real: `ConsultasAnvisaClient._get` já tenta de novo
(4 tentativas, backoff) num HTTP 429/5xx antes de desistir — mas se todas
as tentativas falharem, esse `HTTPError` vazava sem tratamento até virar
um 500 cru pro frontend, sem dizer que o problema é da ANVISA, não da
nossa API. Achado testando ao vivo (busca em lista tomando 429 de verdade
depois de uma bateria de testes de carga contra a API real).
"""

from __future__ import annotations

from typing import Any

import pytest
from curl_cffi.requests.exceptions import HTTPError
from fastapi.testclient import TestClient

import app.main as main
from app.ingest.consultas_alimentos import ProdutoAlimento, ResultadoBuscaProdutos


@pytest.fixture(autouse=True)
def _cliente_nao_faz_rede(monkeypatch: pytest.MonkeyPatch) -> None:
    """`produtos_alimentos`/`produto_alimento_detalhe` criam seu próprio
    `ConsultasAnvisaClient()` (sem injeção de dependência) — como os testes
    aqui só querem verificar o mapeamento de erro, troca `aclose` por um
    no-op pra não abrir uma sessão `curl_cffi` de verdade à toa."""

    async def _aclose_no_op(self: Any) -> None:
        return None

    monkeypatch.setattr(main.ConsultasAnvisaClient, "aclose", _aclose_no_op)


def test_produtos_alimentos_traduz_falha_persistente_da_anvisa_para_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _buscar_produtos_falha(*args: Any, **kwargs: Any) -> ResultadoBuscaProdutos:
        raise HTTPError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(main, "buscar_produtos", _buscar_produtos_falha)

    with TestClient(main.app) as client:
        resp = client.get("/produtos/alimentos", params={"nome_produto": "whey"})

    assert resp.status_code == 503
    assert "anvisa" in resp.json()["detail"].lower()


def test_produto_alimento_detalhe_traduz_falha_persistente_da_anvisa_para_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _detalhe_produto_falha(*args: Any, **kwargs: Any) -> ProdutoAlimento | None:
        raise HTTPError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(main, "detalhe_produto", _detalhe_produto_falha)

    with TestClient(main.app) as client:
        resp = client.get("/produtos/alimentos/25351130780202618")

    assert resp.status_code == 503
    assert "anvisa" in resp.json()["detail"].lower()

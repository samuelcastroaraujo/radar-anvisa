"""Testes do motor de alertas — só a parte pura (casamento de regra x item
e montagem de mensagem). O resto (`buscar_itens_recentes`, CRUD, canais de
envio) mexe com banco/rede real e foi validado manualmente contra o
Supabase de produção e um endpoint de teste real (ver CLAUDE.md, M7).
"""

from __future__ import annotations

from datetime import datetime

from app.alertas import ItemParaAlerta, RegraAlerta, item_casa_com_regra, montar_mensagem


def _regra(
    termos: list[str] | None = None,
    temas: list[str] | None = None,
    nome: str = "teste",
) -> RegraAlerta:
    return RegraAlerta(
        id="r1",
        nome=nome,
        termos=termos or [],
        temas=temas or [],
        canais=["webhook"],
        destino={},
        ativo=True,
    )


def _item(temas: list[str] | None = None) -> ItemParaAlerta:
    return ItemParaAlerta(
        item_tipo="norma",
        item_chave="i1",
        titulo="RDC 27/2010",
        texto_buscavel="RDC 27/2010 rotulagem nutricional de suplemento alimentar",
        url="https://example.com",
        visto_em=datetime.now(),
        temas=temas or [],
    )


def test_regra_sem_filtro_casa_com_tudo() -> None:
    assert item_casa_com_regra(_item(), _regra(termos=[], temas=[])) is True


def test_termo_bate_case_insensitive() -> None:
    assert item_casa_com_regra(_item(), _regra(termos=["ROTULAGEM"])) is True
    assert item_casa_com_regra(_item(), _regra(termos=["cosméticos"])) is False


def test_qualquer_termo_da_lista_basta() -> None:
    regra = _regra(termos=["cosméticos", "suplemento"])
    assert item_casa_com_regra(_item(), regra) is True


def test_tema_bate_contra_tema_do_item() -> None:
    item = _item(temas=["alimentos", "suplementos"])
    assert item_casa_com_regra(item, _regra(temas=["Alimentos"])) is True
    assert item_casa_com_regra(item, _regra(temas=["medicamentos"])) is False


def test_tema_sem_dado_no_item_nao_casa_falso_positivo() -> None:
    # noticia/dou não têm tema extraído (só norma.tema, que nem é
    # preenchido por nenhum ingestor ainda) — uma regra só de tema não
    # deve casar com um item sem nenhum tema atribuído.
    item = _item(temas=[])
    assert item_casa_com_regra(item, _regra(temas=["alimentos"])) is False


def test_montar_mensagem_inclui_nome_da_regra_e_url() -> None:
    assunto, corpo = montar_mensagem(_regra(nome="Alertas de rotulagem"), _item())
    assert "Alertas de rotulagem" in assunto
    assert "RDC 27/2010" in assunto
    assert "https://example.com" in corpo

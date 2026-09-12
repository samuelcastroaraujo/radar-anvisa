"""Testes do parser de consultas públicas (módulo 630) contra amostras
reais coletadas no M5 — ver research/FONTES.md, Addendum M5.
"""

import html
from datetime import date
from pathlib import Path

from app.ingest.participacao_social import parse_cp_detalhe, parse_lista_cps

SAMPLES = Path(__file__).resolve().parent.parent / "research" / "samples"


def _carregar(nome: str) -> str:
    bruto = (SAMPLES / nome).read_bytes()
    return html.unescape(bruto.decode("iso-8859-1"))


def test_parse_lista_cps_acha_as_19_ativas() -> None:
    texto = _carregar("cp_ativa_9789.raw")
    itens = parse_lista_cps(texto)
    assert len(itens) == 19
    tipo, numero, seq, ano, orgao = itens[0]
    assert tipo == "CPB"
    assert numero == "00001418"
    assert ano == "2026"
    assert orgao == "ANVISA/MS"


def test_parse_cp_detalhe_extrai_prazo_estruturado() -> None:
    texto = _carregar("cp_1418_individual.raw")
    cp = parse_cp_detalhe(texto, "CPB", "1418", 2026, "http://teste")
    assert cp.data_dou == date(2026, 8, 24)
    assert cp.prazo_inicio == date(2026, 8, 27)
    assert cp.prazo_fim == date(2026, 10, 25)
    assert cp.status is not None
    assert "contribui" in cp.status.lower()
    assert cp.assunto is not None
    assert "Instrução Normativa" in cp.assunto
    assert cp.url_pdf == "https://anexosportal.datalegis.net/arquivos/1932432.pdf"


def test_parse_cp_detalhe_sem_prorrogacao_mantem_prazo_original() -> None:
    texto = _carregar("cp_1418_individual.raw")
    cp = parse_cp_detalhe(texto, "CPB", "1418", 2026, "http://teste")
    # essa amostra não tem prorrogação -- prazo_fim vem só da janela original
    assert cp.prazo_fim == date(2026, 10, 25)

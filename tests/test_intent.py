"""Testes do roteamento de intenção (`app/intent.py`) — cada caso é uma
frase realista, não inventada a esmo (ver CLAUDE.md, M4: o golden QA já
pegou 2 bugs reais de regex de detecção; frases só "parecidas" com as
esperadas não bastam pra pegar esse tipo de erro)."""

from __future__ import annotations

import pytest

from app.intent import detectar_intencao

CASOS_PRODUTO_ALIMENTO = [
    "a whey da growth ainda está regularizada?",
    "esse suplemento está registrado na anvisa?",
    "o produto X é regularizado pela ANVISA?",
    "quais produtos dessa marca estão registrados?",
    "os produtos da growth estão regularizados?",
    "esse whey protein tem registro na anvisa?",
    "as vitaminas da marca X têm notificação válida?",
    "a proteína X foi regularizada em 2023?",
]

CASOS_NAO_PRODUTO_ALIMENTO = [
    ("a RDC 243/2018 ainda está vigente?", "norma_especifica"),
    # achado escrevendo este teste: "notificação" sozinho (sem "tem"/"têm"/
    # "possui" logo antes) não deve virar produto_alimento — é uma pergunta
    # sobre o PROCESSO em geral, não sobre um produto específico.
    ("como funciona o processo de notificação de suplementos?", "tematica"),
    ("o que diz a RDC sobre rotulagem nutricional?", "tematica"),
    ("quais normas foram publicadas essa semana?", "temporal"),
    ("existe alguma consulta pública aberta sobre suplementos?", "consulta_publica"),
]


@pytest.mark.parametrize("pergunta", CASOS_PRODUTO_ALIMENTO)
def test_detecta_produto_alimento(pergunta: str) -> None:
    assert detectar_intencao(pergunta).tipo == "produto_alimento"


@pytest.mark.parametrize("pergunta,esperado", CASOS_NAO_PRODUTO_ALIMENTO)
def test_nao_confunde_produto_alimento_com_outras_intencoes(pergunta: str, esperado: str) -> None:
    assert detectar_intencao(pergunta).tipo == esperado

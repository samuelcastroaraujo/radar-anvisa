"""Testes do parser do módulo 310 contra amostras reais do portal
(research/samples/), coletadas em M0/M2 com requisições de verdade — ver
research/FONTES.md. Nenhum HTML aqui é inventado.
"""

import html
from pathlib import Path

from app.ingest.anvisalegis import (
    parse_arvore_anos,
    parse_atos_revogados,
    parse_atos_vigentes,
    parse_total_paginas,
)

SAMPLES = Path(__file__).resolve().parent.parent / "research" / "samples"


def _carregar(nome: str) -> str:
    """Reproduz o pipeline de decodificação real (iso-8859-1 -> html.unescape)
    usado por `AnvisaLegisClient._get`, já que as amostras em disco são
    salvas como bytes crus decodificados só em iso-8859-1."""
    bruto = (SAMPLES / nome).read_text(encoding="utf-8")
    return html.unescape(bruto)


def test_arvore_anos_soma_bate_com_o_total_do_portal() -> None:
    texto = _carregar("vigentes_arvore_anos_8542.html")
    anos = parse_arvore_anos(texto)
    assert sum(qtd for _ano, _co_tematica, qtd in anos) == 1138


def test_parse_atos_vigentes_extrai_tipo_numero_ano_e_data() -> None:
    texto = _carregar("vigentes_ano2026_trecho.html")
    atos = parse_atos_vigentes(texto, "http://teste")
    assert len(atos) >= 2
    primeiro = atos[0]
    assert primeiro.tipo_ato == "INM"
    assert primeiro.numero == "469"
    assert primeiro.ano == 2026
    assert primeiro.status_vigencia == "vigente"
    assert primeiro.ementa is not None
    assert "Institui" in primeiro.ementa


def test_parse_atos_vigentes_valida_encoding_datalegis() -> None:
    """Seção 10 do briefing: teste específico de acentuação vinda do
    datalegis (o portal serve iso-8859-1 + entidades HTML — ver FONTES.md).
    Esta amostra é de 1966 (pré-ANVISA, criada só em 1999) — por isso o
    órgão citado é o antigo Serviço Nacional de Fiscalização da Medicina e
    Farmácia, não "Vigilância Sanitária"."""
    texto = _carregar("vigentes_ano1966_trecho.html")
    atos = parse_atos_vigentes(texto, "http://teste")
    assert len(atos) == 1
    ato = atos[0]
    assert "Serviço Nacional de Fiscalização da Medicina e Farmácia" in ato.texto_integral
    assert "instalação" in ato.texto_integral
    assert "atribuições" in ato.texto_integral


def test_parse_atos_revogados_valida_encoding_datalegis() -> None:
    """Mesmo teste de encoding, agora numa amostra que já é da era ANVISA
    (2024) e cita 'Vigilância Sanitária' de verdade."""
    texto = _carregar("revogadas_310_ano2024_utf8.html")
    atos = parse_atos_revogados(texto, "http://teste")
    assert any("Vigilância Sanitária" in (a.ementa or "") for a in atos)
    assert any("Resolução" in (a.ementa or "") for a in atos)


def test_parse_atos_vigentes_extrai_relacao_de_revogacao_do_linktexto() -> None:
    texto = _carregar("vigentes_ano1966_trecho.html")
    ato = parse_atos_vigentes(texto, "http://teste")[0]
    tipos_relacao = {r.tipo for r in ato.relacoes}
    assert "revoga" in tipos_relacao
    revogacoes = [r for r in ato.relacoes if r.tipo == "revoga"]
    assert any(r.destino_tipo_ato == "RDC" and r.destino_ano == 2007 for r in revogacoes)
    assert all(not r.invertida for r in ato.relacoes)


def test_parse_atos_vigentes_nao_extrai_ementa_quando_ato_nao_tem() -> None:
    """Atos antigos às vezes vão direto para o preâmbulo ('O DIRETOR do...')
    sem uma ementa separada — não podemos confundir uma coisa com a outra."""
    texto = _carregar("vigentes_ano1966_trecho.html")
    ato = parse_atos_vigentes(texto, "http://teste")[0]
    assert ato.ementa is None


def test_parse_atos_revogados_extrai_status_e_ementa() -> None:
    texto = _carregar("revogadas_310_ano2024_utf8.html")
    atos = parse_atos_revogados(texto, "http://teste")
    assert len(atos) == 28
    assert all(a.status_vigencia == "revogada" for a in atos)
    # o "&ano=2024" da URL filtra por quando a norma ENTROU na lista de
    # revogadas, não pelo ano em que foi publicada — por isso a amostra
    # mistura atos de anos variados (ex.: Portarias de 1998); ver FONTES.md.
    assert {a.ano for a in atos} != {2024}
    primeiro = atos[0]
    assert primeiro.tipo_ato == "RDC"
    assert primeiro.numero == "952"
    assert primeiro.ementa is not None
    assert "Resolução" in primeiro.ementa


def test_parse_atos_revogados_extrai_revogador_do_texto_livre() -> None:
    """Portarias de 1998 que foram revogadas pela RDC 454/2020 — confirmado
    manualmente em research/FONTES.md (M0)."""
    texto = _carregar("revogadas_310_ano2024_utf8.html")
    atos = parse_atos_revogados(texto, "http://teste")
    com_relacao = {a.numero: a.relacoes for a in atos if a.numero in {"938", "885", "883"}}
    assert len(com_relacao) == 3
    for numero, relacoes in com_relacao.items():
        assert len(relacoes) == 1, numero
        r = relacoes[0]
        assert r.tipo == "revoga"
        assert r.invertida is True
        assert r.destino_tipo_ato == "RDC"
        assert r.destino_numero == "454"
        assert r.destino_ano == 2020


def test_parse_total_paginas() -> None:
    """1996 tem 62 revogadas = 50 na página 1 + 12 na página 2 (confirmado
    rodando de verdade contra o portal — ver Addendum M2 em FONTES.md; essa
    paginação real foi descoberta só depois de uma carga histórica que
    perdeu 840 atos por não paginar)."""
    seletor = _carregar("revogadas_seletor_paginas_1996.html")
    assert parse_total_paginas(seletor) == 2


def test_parse_total_paginas_sem_selecao_retorna_uma_pagina() -> None:
    assert parse_total_paginas("<div>nada de paginação aqui</div>") == 1

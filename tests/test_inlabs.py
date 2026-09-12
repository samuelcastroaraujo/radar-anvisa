"""Testes do parser do DOU (INLABS) contra amostras reais — ver
research/FONTES.md, seção "3. DOU em tempo real — INLABS".

As amostras são XML de verdade extraído do zip de 11/09/2026 baixado com
conta real durante o M0.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

from app.ingest.inlabs import (
    ARTCATEGORIA_ANVISA,
    _extrair_tipo_numero_ano,
    _parse_artigo_xml,
    parse_zip_anvisa,
)

SAMPLES = Path(__file__).resolve().parent.parent / "research" / "samples" / "inlabs_extract"


def _carregar(nome: str) -> bytes:
    return (SAMPLES / nome).read_bytes()


def test_parse_artigo_resolucao() -> None:
    materia = _parse_artigo_xml(_carregar("515_20260911_24349152.xml"))
    assert materia is not None
    assert materia.id_materia_inlabs == "24349152"
    assert materia.edicao == date(2026, 9, 11)
    assert materia.secao == "DO1"
    assert ARTCATEGORIA_ANVISA in (materia.orgao or "")
    assert "RESOLUÇÃO-RE" in materia.titulo
    assert materia.tipo_ato == "RESOLUÇÃO-RE"
    assert materia.numero == "3547"
    assert materia.ano == 2026
    assert "RESOLUÇÃO-RE" in materia.texto
    assert materia.url is not None and materia.url.startswith("http://pesquisa.in.gov.br")


def test_parse_artigo_aresto() -> None:
    materia = _parse_artigo_xml(_carregar("515_20260911_24353106.xml"))
    assert materia is not None
    assert materia.tipo_ato == "ARESTO"
    assert materia.numero == "1796"
    assert materia.ano == 2026


def test_extrair_tipo_numero_ano_formatos_variados() -> None:
    assert _extrair_tipo_numero_ano("RESOLUÇÃO-RE Nº 3.547, DE 9 DE SETEMBRO DE 2026") == (
        "RESOLUÇÃO-RE",
        "3547",
        2026,
    )
    assert _extrair_tipo_numero_ano("texto sem cabeçalho reconhecível") == (None, None, None)


def test_parse_zip_anvisa_filtra_por_categoria() -> None:
    """Monta um zip sintético com as duas amostras reais + um XML de outro
    órgão (sem "Agência Nacional de Vigilância Sanitária" no artCategory) —
    só as duas primeiras devem sobreviver ao filtro."""
    xml_outro_orgao = """<?xml version="1.0" encoding="utf-8"?>
<xml><article id="1" idMateria="999" pubName="DO1" pubDate="11/09/2026"
  artCategory="Ministério da Fazenda/Secretaria Executiva">
  <body><Identifica><![CDATA[PORTARIA Nº 1, DE 1 DE JANEIRO DE 2026]]></Identifica>
  <Texto><![CDATA[<p>nada a ver com Anvisa</p>]]></Texto></body>
</article></xml>""".encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("515_20260911_24349152.xml", _carregar("515_20260911_24349152.xml"))
        zf.writestr("515_20260911_24353106.xml", _carregar("515_20260911_24353106.xml"))
        zf.writestr("outro_orgao.xml", xml_outro_orgao)

    materias = parse_zip_anvisa(buf.getvalue())
    assert len(materias) == 2
    assert {m.id_materia_inlabs for m in materias} == {"24349152", "24353106"}

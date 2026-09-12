"""Testes do chunker (app/chunking.py) — seção 6 do briefing."""

from app.chunking import (
    MAX_TOKENS_POR_CHUNK,
    contar_tokens,
    dividir_unidades_juridicas,
    montar_chunks,
    texto_para_embeddar,
)

TEXTO_RDC_961 = """Art. 1º Esta Resolução altera a Resolução da Diretoria Colegiada - RDC
nº 56, de 16 de novembro de 2012.

Parágrafo único. Esta Resolução incorpora ao ordenamento jurídico nacional
a Resolução GMC/MERCOSUL nº 28, de 4 de dezembro de 2024.

Art. 2º A lista de monômeros autorizados passa a vigorar acrescida da
substância que consta no Anexo I desta Resolução.

Art. 3º Esta Resolução entra em vigor na data de sua publicação.

ANEXO I

INCLUSÃO NA LISTA DE MONÔMEROS AUTORIZADOS
"""


def test_dividir_unidades_juridicas_separa_artigos_e_anexo() -> None:
    unidades = dividir_unidades_juridicas(TEXTO_RDC_961)
    rotulos = [r for r, _ in unidades]
    assert rotulos == ["Art. 1º", "Art. 2º", "Art. 3º", "Anexo I"]


def test_dividir_unidades_juridicas_nao_corta_artigo_no_meio() -> None:
    """O 'Parágrafo único' dentro do Art. 1º deve continuar dentro do
    mesmo chunk do Art. 1º quando o artigo cabe no limite de tokens."""
    unidades = dividir_unidades_juridicas(TEXTO_RDC_961)
    art1 = dict(unidades)["Art. 1º"]
    assert "Parágrafo único" in art1


def test_dividir_unidades_juridicas_com_preambulo() -> None:
    texto = "Considerando isso e aquilo, resolve:\n\nArt. 1º Fica aprovado."
    unidades = dividir_unidades_juridicas(texto)
    assert unidades[0][0] == "Preâmbulo"
    assert unidades[1][0] == "Art. 1º"


def test_montar_chunks_inclui_ementa_como_chunk_proprio() -> None:
    chunks = montar_chunks("Dispõe sobre alguma coisa.", TEXTO_RDC_961)
    assert chunks[0].rotulo == "Ementa"
    assert chunks[0].conteudo == "Dispõe sobre alguma coisa."


def test_artigo_grande_e_dividido_mantendo_o_rotulo() -> None:
    """Um artigo com só um bloco de texto corrido gigante (sem §/inciso)
    tem que ser dividido, e cada pedaço continua citando o artigo de
    origem no rótulo (seção 6: 'mantendo o rótulo')."""
    paragrafo = "Este é um parágrafo de teste com bastante conteúdo. " * 100
    texto = f"Art. 1º {paragrafo}\n\n{paragrafo}\n\n{paragrafo}"
    assert contar_tokens(texto) > MAX_TOKENS_POR_CHUNK

    unidades = dividir_unidades_juridicas(texto)
    assert len(unidades) == 1
    rotulo, conteudo = unidades[0]
    from app.chunking import dividir_se_necessario

    chunks = dividir_se_necessario(rotulo, conteudo)
    assert len(chunks) > 1
    assert all(c.tokens <= MAX_TOKENS_POR_CHUNK for c in chunks)
    assert all(c.rotulo.startswith("Art. 1º") for c in chunks)


def test_agrupamento_guloso_evita_fragmentos_minusculos() -> None:
    """Uma 'tabela' com centenas de linhas curtas separadas por linha em
    branco não pode virar um chunk por linha (isso já aconteceu de
    verdade numa norma real de 1966 — 1.451 chunks, a maioria com 2-3
    tokens, antes desse agrupamento)."""
    linhas = "\n\n".join(f"item {i}" for i in range(2000))
    texto = f"Art. 1º {linhas}"
    from app.chunking import dividir_se_necessario

    unidades = dividir_unidades_juridicas(texto)
    chunks = dividir_se_necessario(*unidades[0])
    assert all(c.tokens > 50 for c in chunks[:-1])  # só o último pode ser menor
    assert all(c.tokens <= MAX_TOKENS_POR_CHUNK for c in chunks)


def test_texto_para_embeddar_inclui_cabecalho_de_contexto() -> None:
    texto = texto_para_embeddar(
        "RDC", "961", 2025, "Altera outra norma.", "Art. 1º", "Conteúdo aqui."
    )
    assert texto.startswith("RDC 961/2025 — Altera outra norma. — Art. 1º")
    assert "Conteúdo aqui." in texto


def test_corte_bruto_respeita_o_limite_mesmo_em_texto_denso() -> None:
    """Bug real achado na carga completa do M3: um bloco sem §/inciso nem
    parágrafo em branco suficiente (uma tabela de anexo numa linha só) caía
    no corte bruto, que estimava ~4 chars/token — texto denso em números e
    símbolos tokeniza bem mais apertado que isso, e chunks saíram com até
    3.171 tokens (mais que o dobro do limite). Cortar pelos tokens de
    verdade (não por uma estimativa de caracteres) tem que valer sempre,
    para texto qualquer, não só para prosa comum."""
    from app.chunking import _cortar_bruto

    tabela_densa = "113693-69-9;0,2 MG/KG;LME(T)=0,05MG/KG;SOMENTE PARA DISPERSÕES;" * 400
    assert contar_tokens(tabela_densa) > MAX_TOKENS_POR_CHUNK

    chunks = _cortar_bruto("Anexo I", tabela_densa)
    assert len(chunks) > 1
    assert all(c.tokens <= MAX_TOKENS_POR_CHUNK for c in chunks)

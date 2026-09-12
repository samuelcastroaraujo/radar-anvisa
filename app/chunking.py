"""Divide o `texto_integral` de uma norma em chunks por unidade jurídica —
seção 6 do briefing: ementa, cada artigo, cada anexo; nunca corta um artigo
no meio; artigo com mais de 1.500 tokens é dividido por parágrafo/inciso
mantendo o rótulo; cada chunk carrega um cabeçalho de contexto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken

MAX_TOKENS_POR_CHUNK = 1500

# cl100k_base não é o encoder oficial do text-embedding-3-large (a OpenAI não
# publicou um tokenizer específico via tiktoken pra ele), mas é a aproximação
# padrão usada pra contagem de tokens de embeddings da OpenAI — suficiente
# pra decidir "isso passou de 1.500 tokens", que é tudo que essa contagem
# precisa fazer aqui.
_ENC = tiktoken.get_encoding("cl100k_base")


def contar_tokens(texto: str) -> int:
    return len(_ENC.encode(texto))


@dataclass
class ChunkBruto:
    rotulo: str
    conteudo: str
    tokens: int


_RE_UNIDADE = re.compile(r"(?m)^(Art\.\s*\d+[º°o]?\.?|ANEXO(?:\s+[IVXLCDM]+|\s+[ÚU]NICO)?\b)")
_RE_SUBUNIDADE = re.compile(
    r"(?m)^(§\s*\d+[º°o]?\.?|Par[áa]grafo\s+[úu]nico\.?|[IVXLCDM]{1,6}\s*[-–—.]\s)"
)


def _normalizar_rotulo_artigo(bruto: str) -> str:
    m = re.search(r"\d+", bruto)
    return f"Art. {m.group(0)}º" if m else bruto.strip()


def _normalizar_rotulo_anexo(bruto: str) -> str:
    # "ANEXO II" -> "Anexo II" — .title() erraria pra "Anexo Ii" (numeral
    # romano não é uma palavra normal de capitalizar letra a letra).
    partes = bruto.strip().split(maxsplit=1)
    if len(partes) == 1:
        return partes[0].capitalize()
    return f"{partes[0].capitalize()} {partes[1]}"


def dividir_unidades_juridicas(texto_integral: str) -> list[tuple[str, str]]:
    """[(rótulo, conteúdo)] para cada unidade jurídica do corpo do ato:
    preâmbulo (se houver texto antes do 1º artigo), cada artigo, cada anexo.
    Nunca corta um artigo no meio — cada um vira exatamente uma unidade
    aqui (a divisão por tamanho vem depois, em `dividir_se_necessario`)."""
    marcas = list(_RE_UNIDADE.finditer(texto_integral))
    if not marcas:
        texto = texto_integral.strip()
        return [("Texto", texto)] if texto else []

    unidades: list[tuple[str, str]] = []
    preambulo = texto_integral[: marcas[0].start()].strip()
    if preambulo:
        unidades.append(("Preâmbulo", preambulo))

    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto_integral)
        conteudo = texto_integral[m.start() : fim].strip()
        bruto = m.group(1)
        rotulo = (
            _normalizar_rotulo_artigo(bruto)
            if bruto.startswith("Art")
            else _normalizar_rotulo_anexo(bruto)
        )
        if conteudo:
            unidades.append((rotulo, conteudo))
    return unidades


def _dividir_por_paragrafos(conteudo: str) -> list[str]:
    partes = [p.strip() for p in conteudo.split("\n\n") if p.strip()]
    return partes or [conteudo]


def _cortar_bruto(rotulo: str, texto: str) -> list[ChunkBruto]:
    """Último recurso, quando nem parágrafo/inciso dividem o bloco o
    suficiente (ex.: uma tabela de anexo enorme numa linha só): corta pelos
    tokens de verdade, não por uma estimativa de caracteres.

    [Bug real, achado depois de rodar a carga inteira]: a primeira versão
    cortava por ~4 caracteres/token (chute pra português corrido), mas
    tabelas densas em números/símbolos (a maioria dos "Anexo" com listas de
    substâncias) tokenizam bem mais apertado — saíram chunks de até 3.171
    tokens, mais que o dobro do limite de 1.500. Cortar direto na sequência
    de tokens (e decodificar de volta) garante o limite sempre, não é mais
    uma estimativa."""
    ids = _ENC.encode(texto)
    blocos_ids = [
        ids[i : i + MAX_TOKENS_POR_CHUNK] for i in range(0, len(ids), MAX_TOKENS_POR_CHUNK)
    ]
    blocos_ids = blocos_ids or [[]]
    return [
        ChunkBruto(
            rotulo=f"{rotulo} ({i}/{len(blocos_ids)})",
            conteudo=_ENC.decode(ids_bloco),
            tokens=len(ids_bloco),
        )
        for i, ids_bloco in enumerate(blocos_ids, start=1)
    ]


def _agrupar_gulosamente(rotulo_base: str, partes: list[tuple[str, str]]) -> list[ChunkBruto]:
    """Agrupa partes pequenas (§, inciso ou parágrafo) em blocos de até
    ~MAX_TOKENS_POR_CHUNK, em vez de um chunk por parte — uma tabela de
    anexo quebrada por linha em branco pode virar milhares de fragmentos de
    2-3 tokens cada, o que é inútil pra embedding. O rótulo do grupo cita o
    intervalo de sufixos que ele cobre."""
    chunks: list[ChunkBruto] = []
    buffer_sufixos: list[str] = []
    buffer_textos: list[str] = []
    buffer_tokens = 0

    def flush() -> None:
        nonlocal buffer_sufixos, buffer_textos, buffer_tokens
        if not buffer_textos:
            return
        if buffer_sufixos == ["caput"]:
            rotulo = rotulo_base
        elif len(buffer_sufixos) == 1:
            rotulo = f"{rotulo_base}, {buffer_sufixos[0]}"
        else:
            rotulo = f"{rotulo_base}, {buffer_sufixos[0]}–{buffer_sufixos[-1]}"
        conteudo = "\n\n".join(buffer_textos)
        chunks.append(ChunkBruto(rotulo=rotulo, conteudo=conteudo, tokens=buffer_tokens))
        buffer_sufixos, buffer_textos, buffer_tokens = [], [], 0

    for sufixo, bloco in partes:
        tokens = contar_tokens(bloco)
        if tokens > MAX_TOKENS_POR_CHUNK:
            flush()
            chunks.extend(_cortar_bruto(f"{rotulo_base}, {sufixo}", bloco))
            continue
        if buffer_textos and buffer_tokens + tokens > MAX_TOKENS_POR_CHUNK:
            flush()
        buffer_sufixos.append(sufixo)
        buffer_textos.append(bloco)
        buffer_tokens += tokens
    flush()
    return chunks


def dividir_se_necessario(rotulo: str, conteudo: str) -> list[ChunkBruto]:
    """Se `conteudo` (um artigo ou anexo inteiro) já cabe no limite, devolve
    como está. Senão, divide por § / Parágrafo único / inciso romano,
    mantendo o rótulo do pai (seção 6: 'dividir por parágrafo/inciso
    mantendo o rótulo'), reagrupando partes pequenas para não gerar
    fragmentos minúsculos demais pra fazer sentido como embedding."""
    tokens = contar_tokens(conteudo)
    if tokens <= MAX_TOKENS_POR_CHUNK:
        return [ChunkBruto(rotulo=rotulo, conteudo=conteudo, tokens=tokens)]

    marcas = list(_RE_SUBUNIDADE.finditer(conteudo))
    partes: list[tuple[str, str]] = []
    if len(marcas) >= 2:
        caput = conteudo[: marcas[0].start()].strip()
        if caput:
            partes.append(("caput", caput))
        for i, m in enumerate(marcas):
            fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(conteudo)
            sub = conteudo[m.start() : fim].strip()
            if sub:
                partes.append((m.group(1).strip(), sub))
    else:
        partes = [
            (f"parte {i}", bloco)
            for i, bloco in enumerate(_dividir_por_paragrafos(conteudo), start=1)
        ]

    return _agrupar_gulosamente(rotulo, partes)


def montar_chunks(ementa: str | None, texto_integral: str) -> list[ChunkBruto]:
    """Todos os chunks de uma norma: ementa (se houver) + cada unidade
    jurídica do corpo, já respeitando o limite de tokens."""
    chunks: list[ChunkBruto] = []
    if ementa and ementa.strip():
        chunks.extend(dividir_se_necessario("Ementa", ementa.strip()))
    for rotulo, conteudo in dividir_unidades_juridicas(texto_integral):
        chunks.extend(dividir_se_necessario(rotulo, conteudo))
    return chunks


def texto_para_embeddar(
    tipo_ato: str, numero: str, ano: int, ementa: str | None, rotulo: str, conteudo: str
) -> str:
    """Cabeçalho de contexto prependado ao texto que vai pro embedding —
    seção 6: '{tipo_ato} {numero}/{ano} — {ementa curta} — {rotulo}'."""
    ementa_curta = (ementa or "").strip()[:150]
    cabecalho = f"{tipo_ato} {numero}/{ano} — {ementa_curta} — {rotulo}"
    return f"{cabecalho}\n\n{conteudo}"

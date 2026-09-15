"""Roteamento de intenção da pergunta do usuário — seção 7 do briefing:
pergunta sobre norma específica -> lookup direto; temática -> busca
híbrida; temporal -> consulta por data; consulta pública -> ainda não
implementado (depende do módulo 630, que é M5); produto de alimento/
suplemento -> consulta ao vivo na ANVISA (pós-M7, ver CLAUDE.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

TipoIntencao = Literal[
    "norma_especifica", "temporal", "consulta_publica", "produto_alimento", "tematica"
]

# Grupos de sigla equivalentes — a base tem uma inconsistência real (ver
# CLAUDE.md, M4): o mesmo tipo de ato legal aparece sob códigos diferentes
# dependendo de qual parte do AnvisaLegis o gerou (ex.: "INM" quando veio
# do parser de vigentes, "IN" quando veio direto de um LinkTexto do site).
# Buscar por todo o grupo evita "não encontrei" por causa de uma sigla
# alternativa da mesma coisa.
GRUPOS_TIPO_ATO: dict[str, list[str]] = {
    "RDC": ["RDC"],
    "RES": ["RES", "RE"],
    "RE": ["RES", "RE"],
    "IN": ["INM", "IN"],
    "INM": ["INM", "IN"],
    "POR": ["POR", "PRT"],
    "PRT": ["POR", "PRT"],
    # "Portaria Conjunta" é um tipo à parte na base, não uma "Portaria"
    # comum (achado real rodando o golden QA — ver CLAUDE.md, M4): existe
    # tanto como sigla "PCJ" quanto como nome truncado em 20 caracteres
    # ("PORTARIA CONJUNTA MS", herdado de um fallback do parser do M2).
    "PCJ": ["PCJ", "PORTARIA CONJUNTA MS"],
    "LEI": ["LEI"],
    "DEC": ["DEC"],
}

_RE_NORMA_ESPECIFICA = re.compile(
    r"\b(RDC|RES(?:OLU[CÇ][AÃ]O)?|RE|IN(?:M|STRU[CÇ][AÃ]O\s+NORMATIVA)?|"
    r"POR(?:TARIA)?\s+CONJUNTA|POR(?:TARIA)?|PRT|LEI|DEC(?:RETO)?)\.?"
    r"\s*n?[ºo°.]?\s*([\d.]+)\s*[,]?\s*(?:de\s+)?/?\s*(\d{4})\b",
    re.IGNORECASE,
)

_RE_TEMPORAL = re.compile(
    # "hoje" de propósito fora daqui: em "vigente hoje"/"rege hoje" é uma
    # pergunta sobre status atual (temática), não sobre o que saiu no dia —
    # só conta como temporal quando vem junto de um verbo de publicação/
    # novidade, não sozinho.
    r"\b(últimos?\s+\d+\s+dias|últimas?\s+\d+\s+semanas?|essa?\s+semana|"
    r"este\s+m[eê]s|recentes?|novidades?|o\s+que\s+mudou|"
    r"publicad[oa]s?\s+(?:essa|esta|hoje|últim)|"
    r"(?:saiu|publicou|houve\s+algo)\s+hoje)",
    re.IGNORECASE,
)

_RE_CONSULTA_PUBLICA = re.compile(
    r"\bconsulta[s]?\s+p[uú]blica[s]?\b|\baudi[eê]ncia[s]?\s+p[uú]blica[s]?\b|"
    r"\btomada[s]?\s+de\s+subs[ií]dio",
    re.IGNORECASE,
)

_RE_DIAS = re.compile(r"(\d+)\s+dias", re.IGNORECASE)

# Produto de alimento/suplemento (não norma) — o sinal linguístico real é
# diferente do de norma: normas são "vigentes"/"revogadas", produtos são
# "registrados"/"regularizados"/"notificados" (situação de registro
# sanitário). Exige um verbo copulativo ("está", "é", "foi"...) logo antes
# do particípio — não o particípio sozinho — pra não confundir uma
# pergunta sobre STATUS de um produto específico ("essa whey está
# regularizada?") com uma pergunta temática sobre o processo em geral
# ("como funciona a notificação de suplementos?", que deve continuar
# indo pra busca temática/RAG).
_RE_PRODUTO_ALIMENTO = re.compile(
    r"\b(est\w*|s[aã]o|[eé]|foi|foram|ficou|ficaram|continua\w*)\s+\w*\s*"
    # "ativo(s)"/"inativo(s)" de propósito — achado real testando de ponta
    # a ponta: "quais produtos da X estão ativos?" é uma frase natural
    # (o próprio filtro `situacao_registro` do sistema chama isso de
    # "Ativo"/"Inativo"), e sem esses dois o regex deixava passar pra
    # busca temática por engano.
    r"(regulariz\w*|registrad[oa]s?|notificad[oa]s?|anu[ií]d[oa]s?|ativ[oa]s?|inativ[oa]s?)\b"
    r"|\b(tem|t[eê]m|possui\w*)\s+(registro|notifica[cç][aã]o)\b",
    re.IGNORECASE,
)


@dataclass
class NormaReferenciada:
    tipo_ato: str  # como o usuário escreveu (ex.: "RDC", "IN")
    grupo_tipo_ato: list[str]  # siglas equivalentes a buscar no banco
    numero: str
    ano: int


@dataclass
class Intencao:
    tipo: TipoIntencao
    norma: NormaReferenciada | None = None
    dias: int | None = None  # para intenção temporal


def _normalizar_numero(bruto: str) -> str:
    apenas_digitos = re.sub(r"\D", "", bruto)
    return str(int(apenas_digitos)) if apenas_digitos else bruto


def _chave_tipo_ato(tipo_bruto: str) -> str:
    """Reduz o que a pessoa escreveu (sigla ou nome por extenso) à chave
    curta usada em `GRUPOS_TIPO_ATO`."""
    t = re.sub(r"\s+", " ", tipo_bruto.upper()).strip()
    if t.startswith("RDC"):
        return "RDC"
    if t.startswith("RES") or t == "RE":
        return "RES"
    if t.startswith("IN"):  # IN, INM, INSTRUÇÃO NORMATIVA
        return "IN"
    if "CONJUNTA" in t:
        return "PCJ"
    if t.startswith("POR"):
        return "POR"
    if t == "PRT":
        return "PRT"
    if t.startswith("DEC"):
        return "DEC"
    if t.startswith("LEI"):
        return "LEI"
    return t


def detectar_intencao(pergunta: str) -> Intencao:
    m = _RE_NORMA_ESPECIFICA.search(pergunta)
    if m:
        chave = _chave_tipo_ato(m.group(1))
        grupo = GRUPOS_TIPO_ATO.get(chave, [chave])
        return Intencao(
            tipo="norma_especifica",
            norma=NormaReferenciada(
                tipo_ato=chave,
                grupo_tipo_ato=grupo,
                numero=_normalizar_numero(m.group(2)),
                ano=int(m.group(3)),
            ),
        )

    if _RE_PRODUTO_ALIMENTO.search(pergunta):
        return Intencao(tipo="produto_alimento")

    if _RE_CONSULTA_PUBLICA.search(pergunta):
        return Intencao(tipo="consulta_publica")

    if _RE_TEMPORAL.search(pergunta):
        dias_m = _RE_DIAS.search(pergunta)
        dias = int(dias_m.group(1)) if dias_m else 30
        return Intencao(tipo="temporal", dias=dias)

    return Intencao(tipo="tematica")

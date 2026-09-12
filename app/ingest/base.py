"""Contrato comum de todo ingestor (`app/ingest/<fonte>.py`).

Cada fonte real (AnvisaLegis no M2; notícias gov.br e INLABS no M5)
implementa o protocolo `Fonte` abaixo. Nada aqui grava no banco — a
normalização produz DTOs, e a persistência/idempotência é responsabilidade
do orquestrador do pipeline (ainda não implementado; ver `research/FONTES.md`
para as regras de status de vigência e `CLAUDE.md` para o desenho geral).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Protocol

from pydantic import BaseModel

StatusVigencia = Literal["vigente", "revogada", "revogada_parcial", "substituida", "desconhecido"]
CategoriaNoticia = Literal["noticia", "informe", "alerta", "consulta_publica"]


class ItemBruto(BaseModel):
    """Referência a um item descoberto na fonte, antes de baixar o conteúdo completo."""

    fonte: str
    identificador_externo: str
    """Chave natural na fonte de origem (ex.: idMateria do INLABS, ou
    tipo+numero+ano+seqAto do AnvisaLegis) — usada para idempotência."""
    url: str
    data_referencia: date | None = None
    metadata: dict[str, str] = {}


class DocumentoBruto(BaseModel):
    """Conteúdo baixado de um `ItemBruto`, já decodificado para UTF-8."""

    item: ItemBruto
    conteudo: str
    content_type: str
    coletado_em: datetime


class NormaDTO(BaseModel):
    """Corresponde a uma linha da tabela `norma` (ver seção 4 do briefing)."""

    tipo_ato: str
    numero: str
    ano: int
    data_publicacao: date | None = None
    data_vigencia: date | None = None
    ementa: str | None = None
    orgao_emissor: str | None = None
    tema: list[str] = []
    status_vigencia: StatusVigencia = "desconhecido"
    modulo_origem: int | None = None
    url_origem: str
    url_pdf: str | None = None
    texto_integral: str | None = None
    texto_compilado: str | None = None


class NoticiaDTO(BaseModel):
    """Corresponde a uma linha da tabela `noticia`."""

    titulo: str
    resumo: str | None = None
    conteudo: str | None = None
    categoria: CategoriaNoticia
    tema: list[str] = []
    data_publicacao: datetime | None = None
    url: str


class Fonte(Protocol):
    """Interface que todo módulo de ingestão em `app/ingest/` deve implementar."""

    nome: str

    async def descobrir(self, desde: date | None) -> list[ItemBruto]:
        """Lista o que existe/mudou na fonte a partir de `desde` (None = carga histórica)."""
        ...

    async def baixar(self, item: ItemBruto) -> DocumentoBruto:
        """Baixa o conteúdo bruto de um item, decodificando para UTF-8."""
        ...

    def normalizar(self, doc: DocumentoBruto) -> NormaDTO | NoticiaDTO:
        """Converte o documento bruto no DTO correspondente à tabela de destino."""
        ...

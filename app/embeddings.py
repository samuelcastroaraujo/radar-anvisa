"""Geração de embeddings — seção 6 do briefing: `text-embedding-3-large`
(dim 3072), em lotes de 100, com retry.

Servido via OpenRouter (proxy compatível com a API da OpenAI) — ver decisão
em `CLAUDE.md` (M3): a chave disponível era da OpenRouter, não da OpenAI
direto, mas o modelo pedido no briefing é exatamente o mesmo.
"""

from __future__ import annotations

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from app.config import get_settings

MODELO_EMBEDDING = "openai/text-embedding-3-large"
DIMENSAO_EMBEDDING = 3072
TAMANHO_LOTE = 100

_cliente: AsyncOpenAI | None = None


def _get_cliente() -> AsyncOpenAI:
    global _cliente
    if _cliente is None:
        settings = get_settings()
        if not settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY não configurada (ver .env.example).")
        _cliente = AsyncOpenAI(
            api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url
        )
    return _cliente


@retry(stop=stop_after_attempt(4), wait=wait_exponential_jitter(initial=2, max=30), reraise=True)
async def _embeddar_lote(textos: list[str]) -> list[list[float]]:
    cliente = _get_cliente()
    resp = await cliente.embeddings.create(model=MODELO_EMBEDDING, input=textos)
    # a API preserva a ordem de entrada em resp.data (contrato da OpenAI,
    # herdado pelo proxy da OpenRouter) — não precisamos reordenar por índice.
    return [item.embedding for item in resp.data]


async def gerar_embeddings(textos: list[str]) -> list[list[float]]:
    """Gera embeddings para `textos`, em lotes de `TAMANHO_LOTE`, mantendo
    a ordem de entrada na saída."""
    resultado: list[list[float]] = []
    for inicio in range(0, len(textos), TAMANHO_LOTE):
        lote = textos[inicio : inicio + TAMANHO_LOTE]
        resultado.extend(await _embeddar_lote(lote))
    return resultado

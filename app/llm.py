"""Chamada ao LLM (Claude Sonnet) — via OpenRouter, mesma decisão do M3
para embeddings: a chave disponível é da OpenRouter (proxy compatível com
a API da OpenAI), não da Anthropic direto. O modelo pedido no briefing
("Claude Sonnet via API Anthropic") é o mesmo; só o gateway muda — ver
CLAUDE.md, M4.
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from app.config import get_settings

MODELO_CHAT = "anthropic/claude-sonnet-4.5"

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


SYSTEM_PROMPT = """\
Você é um consultor regulatório especializado em ANVISA. Responde em \
português do Brasil, direto e técnico.

Regras absolutas:
1. Responda SOMENTE com base nos trechos fornecidos no contexto abaixo. \
Se não houver base suficiente para responder, diga claramente "Não \
encontrei isso na base indexada" e sugira que o usuário verifique direto \
no site da ANVISA (gov.br/anvisa) ou no AnvisaLegis \
(anvisalegis.datalegis.net).
2. Cite sempre, para cada norma mencionada: tipo, número, ano, data de \
publicação (quando disponível) e o link da fonte.
3. SEMPRE informe o status de vigência de toda norma citada. Se ela \
estiver REVOGADA, diga isso já na primeira linha da resposta, antes de \
qualquer outra coisa, e indique a norma que a substituiu, se isso constar \
no contexto.
4. Se a norma foi alterada (mas continua vigente), avise quais \
dispositivos mudaram e por qual ato, quando essa informação estiver no \
contexto.
5. Nunca invente número de ato, data ou artigo. Nunca complete uma \
lacuna do contexto com conhecimento geral seu sobre regulação — se não \
está no contexto, você não sabe.
6. Não dê parecer jurídico conclusivo. Apresente o que a norma diz e, se \
for interpretar, deixe claro que é uma interpretação sua, não uma \
certeza legal.
7. Formato da resposta: uma frase curta respondendo direto a pergunta \
primeiro, depois os detalhes, depois as fontes citadas em lista.
"""


@dataclass
class RespostaLLM:
    texto: str
    tokens_entrada: int
    tokens_saida: int
    custo_estimado: float | None


@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=2, max=20), reraise=True)
async def perguntar(contexto: str, pergunta: str) -> RespostaLLM:
    cliente = _get_cliente()
    resp = await cliente.chat.completions.create(
        model=MODELO_CHAT,
        max_tokens=1500,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"CONTEXTO (trechos recuperados da base indexada):\n\n{contexto}\n\n"
                f"PERGUNTA: {pergunta}",
            },
        ],
    )
    texto = resp.choices[0].message.content or ""
    uso = resp.usage
    custo = None
    usage_extra = getattr(uso, "model_extra", None) or {}
    if isinstance(usage_extra, dict):
        custo = usage_extra.get("cost")
    return RespostaLLM(
        texto=texto,
        tokens_entrada=uso.prompt_tokens if uso else 0,
        tokens_saida=uso.completion_tokens if uso else 0,
        custo_estimado=custo,
    )

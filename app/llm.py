"""Chamada ao LLM (Claude Sonnet) — via OpenRouter, mesma decisão do M3
para embeddings: a chave disponível é da OpenRouter (proxy compatível com
a API da OpenAI), não da Anthropic direto. O modelo pedido no briefing
("Claude Sonnet via API Anthropic") é o mesmo; só o gateway muda — ver
CLAUDE.md, M4.
"""

from __future__ import annotations

import json
import re
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


_EXTRACAO_PRODUTO_SYSTEM_PROMPT = """\
Você extrai termos de busca de uma pergunta em português sobre o \
registro/notificação de um produto de alimento ou suplemento na ANVISA.

Responda SOMENTE com um objeto JSON, nada além disso (sem markdown, sem \
explicação), no formato exato:
{"nome_produto": string ou null, "marca": string ou null, "detentor_registro": string ou null}

- "nome_produto": o tipo genérico do produto, na palavra MAIS CURTA e \
simples possível — o cadastro da ANVISA busca por substring exata contra \
uma descrição curta e telegráfica (ex.: "WHEY 23.40 HIGH POTE PEAD", \
"CREATINA 100%"), então um termo composto ou com adjetivo a mais (ex.: \
"whey protein isolado", "creatina monohidratada pura") pode não bater com \
nada mesmo o produto existindo. Prefira sempre a palavra única mais \
genérica: "whey" (não "whey protein"), "creatina" (não "creatina \
monohidratada"), "vitamina c" (não "vitamina c 500mg efervescente"). Não \
inclua marca nem empresa aqui.
- "marca": a marca/nome comercial que aparece estampado na embalagem do \
produto, SE for claramente diferente do nome da empresa fabricante (ex.: \
pergunta menciona "a linha Growth" ou "produtos Max Titanium"). Não \
repita aqui o mesmo nome que você já colocou em "detentor_registro" — \
cada entidade mencionada na pergunta vai em UM único campo, nunca os \
dois. Na dúvida entre marca e empresa, prefira "detentor_registro" (é \
mais confiável pra busca) e deixe "marca" null.
- "detentor_registro": razão social (mesmo parcial) ou CNPJ da empresa \
fabricante/detentora do registro, se mencionada (ex.: "Growth Suplementos \
Ltda", "Belapin").
- Use null (não string vazia) pro que não conseguir identificar.
- Nunca invente um valor que não esteja na pergunta.
"""


@dataclass
class TermosBuscaProduto:
    nome_produto: str | None
    marca: str | None
    detentor_registro: str | None

    @property
    def vazio(self) -> bool:
        return not (self.nome_produto or self.marca or self.detentor_registro)


def _parse_termos_json(bruto: str) -> TermosBuscaProduto:
    # o modelo às vezes envolve o JSON num bloco ```json ... ``` mesmo
    # quando instruído a não fazer isso — mais barato tolerar isso aqui do
    # que arriscar uma resposta quebrada por causa de markdown incidental.
    limpo = re.sub(r"^```(?:json)?\s*|\s*```$", "", bruto.strip(), flags=re.IGNORECASE)
    try:
        dados = json.loads(limpo)
    except (json.JSONDecodeError, TypeError):
        return TermosBuscaProduto(None, None, None)
    if not isinstance(dados, dict):
        return TermosBuscaProduto(None, None, None)

    def _campo(chave: str) -> str | None:
        valor = dados.get(chave)
        return valor.strip() if isinstance(valor, str) and valor.strip() else None

    return TermosBuscaProduto(
        nome_produto=_campo("nome_produto"),
        marca=_campo("marca"),
        detentor_registro=_campo("detentor_registro"),
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=2, max=20), reraise=True)
async def extrair_termos_busca_produto(pergunta: str) -> TermosBuscaProduto:
    """Transforma uma pergunta livre ("a whey da growth ainda está
    regularizada?") nos termos estruturados que a busca ao vivo na ANVISA
    espera (`app/ingest/consultas_alimentos.py`). Chamada de extração
    dedicada e barata (não é o LLM decidindo se/quando buscar — isso já
    foi decidido pelo roteamento de intenção em `app/intent.py`; aqui só
    extrai os parâmetros). Nunca lança por resposta malformada — na
    dúvida, devolve tudo `None` e quem chamou trata como "não identificado"."""
    cliente = _get_cliente()
    resp = await cliente.chat.completions.create(
        model=MODELO_CHAT,
        max_tokens=200,
        temperature=0,
        messages=[
            {"role": "system", "content": _EXTRACAO_PRODUTO_SYSTEM_PROMPT},
            {"role": "user", "content": pergunta},
        ],
    )
    texto = resp.choices[0].message.content or ""
    return _parse_termos_json(texto)

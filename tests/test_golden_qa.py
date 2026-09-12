"""Golden QA — seção 10 do briefing: roda perguntas reais contra o
pipeline de chat de verdade (banco Supabase + LLM real) e verifica se o
número da norma e o status de vigência retornados batem com o esperado.

Marcado `golden_qa` (ver pyproject.toml): faz chamadas reais de LLM, então
fica de fora do `pytest -q` do dia a dia. Rodar de propósito com:
    uv run pytest tests/test_golden_qa.py -m golden_qa -v
Pula sozinho se DATABASE_URL/OPENROUTER_API_KEY não estiverem configuradas
(ex.: no CI, que ainda não tem esses segredos).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml

from app.chat import responder
from app.config import get_settings
from app.intent import GRUPOS_TIPO_ATO

_settings = get_settings()

CASOS: list[dict[str, Any]] = yaml.safe_load(
    (Path(__file__).parent / "golden_qa.yaml").read_text(encoding="utf-8")
)

pytestmark = [
    pytest.mark.golden_qa,
    pytest.mark.skipif(
        not _settings.database_url or not _settings.openrouter_api_key,
        reason="golden QA precisa de DATABASE_URL e OPENROUTER_API_KEY reais configuradas",
    ),
]


@pytest.mark.parametrize("caso", CASOS, ids=[c["pergunta"] for c in CASOS])
async def test_golden_qa(caso: dict[str, Any]) -> None:
    # statement_cache_size=0 — ver comentário em app/db.py: obrigatório
    # com o pooler do Supabase (Supavisor, modo transaction) em produção
    # (e agora também localmente, já que o .env passou a usar o pooler).
    pool = await asyncpg.create_pool(
        dsn=_settings.database_url, min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        resultado = await responder(pool, caso["pergunta"])

        if caso.get("nao_deve_encontrar"):
            # `intencao` reflete a detecção (ex.: "norma_especifica"), não
            # se ela foi encontrada -- é `normas` vazio que prova que o
            # sistema não alucinou um número de ato que não existe.
            assert not resultado.normas, (
                f"esperava não encontrar nenhuma norma, mas achou {resultado.normas}"
            )
            return

        tipos_aceitos = GRUPOS_TIPO_ATO.get(caso["tipo_ato_esperado"], [caso["tipo_ato_esperado"]])
        encontrada = next(
            (
                n
                for n in resultado.normas
                if n.numero == caso["numero_esperado"]
                and n.ano == caso["ano_esperado"]
                and n.tipo_ato in tipos_aceitos
            ),
            None,
        )
        assert encontrada is not None, (
            f"norma esperada {caso['tipo_ato_esperado']} {caso['numero_esperado']}/"
            f"{caso['ano_esperado']} não apareceu entre as normas citadas: {resultado.normas}"
        )
        assert encontrada.status_vigencia == caso["status_esperado"], (
            f"status esperado '{caso['status_esperado']}', veio "
            f"'{encontrada.status_vigencia}' para {encontrada.tipo_ato} "
            f"{encontrada.numero}/{encontrada.ano}"
        )
    finally:
        await pool.close()

"""Job diário de ingestão — seção 5 do briefing: 06:00 BRT.

Cada fonte já tem seu próprio script standalone (`scripts/carga_*`), com
tratamento de erro e registro em `job_execucao` próprios. O scheduler só
invoca cada um **como subprocesso**, em sequência — não importa e chama as
funções `main()` direto — porque cada script cria seu próprio pool asyncpg;
rodar em processo separado evita que uma falha (ou uma conexão que não
fecha direito) de uma fonte vaze pro loop de eventos da API ou para as
próximas execuções.

Não existe, em nenhuma das fontes documentadas em `research/FONTES.md`, um
endpoint de "o que mudou desde ontem" — por isso o módulo 310 é re-rodado
por inteiro todo dia (idempotente via upsert; nunca inventamos um endpoint
incremental que não foi validado com requisição real). As outras três
fontes (participação social, notícias, INLABS) já são incrementais/baratas
por natureza — ver os módulos de cada uma.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("radar_anvisa.scheduler")

# Ordem: módulo 310 primeiro — é a fonte de verdade sobre normas; INLABS
# tenta vincular matérias do DOU a normas que as fontes anteriores já
# tenham gravado no mesmo run; alertas roda por último de propósito (M7) —
# só depois que todo o resto já carregou é que existe "recente" de verdade
# pra casar contra as regras.
SCRIPTS_DIARIOS = (
    "scripts.carga_historica_310",
    "scripts.carga_participacao_social",
    "scripts.carga_noticias_govbr",
    "scripts.carga_inlabs",
    "scripts.verificar_alertas",
)


async def _rodar_script(modulo: str) -> None:
    logger.info("iniciando %s", modulo)
    processo = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        modulo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    saida, _ = await processo.communicate()
    nivel = logging.INFO if processo.returncode == 0 else logging.ERROR
    logger.log(
        nivel,
        "%s terminou (rc=%s):\n%s",
        modulo,
        processo.returncode,
        saida.decode("utf-8", errors="replace"),
    )


async def rodar_ingestao_diaria() -> None:
    """Roda todas as fontes em sequência. Uma fonte falhando não impede as
    seguintes — cada `job_execucao` registra seu próprio status."""
    for modulo in SCRIPTS_DIARIOS:
        try:
            await _rodar_script(modulo)
        except Exception:
            logger.exception("falha inesperada disparando %s", modulo)


def iniciar_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")
    scheduler.add_job(
        rodar_ingestao_diaria,
        trigger=CronTrigger(hour=6, minute=0),
        id="ingestao_diaria",
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("scheduler iniciado — ingestão diária às 06:00 America/Sao_Paulo")
    return scheduler

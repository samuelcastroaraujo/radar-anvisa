import asyncio
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv()

import asyncpg  # noqa: E402

sys.path.insert(0, ".")
from app.timeline import buscar_timeline  # noqa: E402


async def main() -> None:
    import os

    url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(url, statement_cache_size=0)
    itens = await buscar_timeline(
        conn, data_inicio=date(2000, 1, 1), data_fim=date(2026, 9, 14), limite=10, q="243"
    )
    for i in itens:
        print(i.tipo, i.titulo, i.data)
    print("total:", len(itens))
    await conn.close()


asyncio.run(main())

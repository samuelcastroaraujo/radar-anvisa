"""Motor de alertas (seção "Alertas" do briefing).

Casa `alerta_regra` (termos/temas) contra o que foi visto pela primeira
vez pelo sistema recentemente — normas, notícias, consultas públicas e
matérias do DOU — e dispara notificação pelos canais configurados
(email/telegram/webhook), com dedupe em `alerta_disparo` (uma
combinação alerta+item+canal só notifica uma vez).

**"Recente" é `coletado_em`/`processado_em`, não `data_publicacao`.** O
módulo 310 é re-crawleado por inteiro todo dia (decisão do M5: não existe
endpoint de "o que mudou" nas fontes) — se o alerta disparasse em cima de
`data_publicacao`, uma norma de 1999 apareceria "nova" todo santo dia.
`coletado_em`/`processado_em` são setados só no INSERT inicial e **nunca
tocados de novo pelo `on conflict do update`** dos ingestores (conferido
lendo `app/ingest/persistencia.py`, `govbr_noticias.py`,
`participacao_social.py` e `inlabs.py` — nenhum tem essa coluna no `do
update set`, e confirmado com uma re-inserção real de uma norma existente
nesta sessão: `coletado_em` não mudou). É esse o sinal de "genuinamente
novo pro sistema" que o alerta usa.

**`temas` é best-effort**: `norma.tema` existe no schema desde o M1 mas
nenhum ingestor até aqui o preenche (achado ao construir o M7, não um bug
novo) — permanece pronto pra quando essa extração existir, mas hoje só
`termos` (substring case-insensitive em título/ementa/resumo/texto)
encontra alguma coisa de verdade.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import asyncpg
import asyncpg.pool
import httpx

from app.config import get_settings

Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

# Janela de busca de itens "recentes" — bem maior que o período entre
# execuções do job diário (06:00 BRT) de propósito: se o scheduler não
# rodar por um dia ou dois, o próximo run ainda alcança o que passou. O
# dedupe em `alerta_disparo` garante que nada é notificado duas vezes
# mesmo revarrendo uma janela larga.
JANELA_PADRAO = timedelta(days=2)

CANAIS_VALIDOS = {"email", "telegram", "webhook"}

RESEND_URL = "https://api.resend.com/emails"
RESEND_FROM_PADRAO = "RADAR ANVISA <onboarding@resend.dev>"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


@dataclass
class RegraAlerta:
    id: str
    nome: str
    termos: list[str]
    temas: list[str]
    canais: list[str]
    destino: dict
    ativo: bool


@dataclass
class ItemParaAlerta:
    item_tipo: str  # norma | noticia | consulta_publica | dou
    item_chave: str
    titulo: str
    texto_buscavel: str
    url: str | None
    visto_em: datetime
    temas: list[str] = field(default_factory=list)


async def listar_regras_ativas(conn: Conn) -> list[RegraAlerta]:
    linhas = await conn.fetch(
        "select id, nome, termos, temas, canais, destino, ativo "
        "from alerta_regra where ativo = true"
    )
    return [
        RegraAlerta(
            id=str(linha["id"]),
            nome=linha["nome"],
            termos=list(linha["termos"] or []),
            temas=list(linha["temas"] or []),
            canais=list(linha["canais"] or []),
            destino=json.loads(linha["destino"])
            if isinstance(linha["destino"], str)
            else (linha["destino"] or {}),
            ativo=linha["ativo"],
        )
        for linha in linhas
    ]


async def buscar_itens_recentes(
    conn: Conn, janela: timedelta = JANELA_PADRAO
) -> list[ItemParaAlerta]:
    desde = datetime.now().astimezone() - janela
    linhas = await conn.fetch(
        """
        (
            select 'norma' as item_tipo, id::text as item_chave,
                   tipo_ato || ' ' || numero || '/' || ano as titulo,
                   coalesce(tipo_ato || ' ' || numero || '/' || ano, '') || ' ' ||
                       coalesce(ementa, '') as texto_buscavel,
                   url_origem as url, coletado_em as visto_em, coalesce(tema, '{}') as temas
            from norma
            where coletado_em >= $1
        )
        union all
        (
            select coalesce(categoria, 'noticia'), id::text, titulo,
                   titulo || ' ' || coalesce(resumo, '') || ' ' || coalesce(conteudo, ''),
                   url, coletado_em, '{}'::text[]
            from noticia
            where coletado_em >= $1
        )
        union all
        (
            select 'dou', id::text, titulo,
                   titulo || ' ' || coalesce(texto, ''),
                   url, processado_em, '{}'::text[]
            from dou_materia
            where processado_em >= $1
        )
        order by visto_em desc
        """,
        desde,
    )
    return [
        ItemParaAlerta(
            item_tipo=linha["item_tipo"],
            item_chave=linha["item_chave"],
            titulo=linha["titulo"],
            texto_buscavel=linha["texto_buscavel"],
            url=linha["url"],
            visto_em=linha["visto_em"],
            temas=list(linha["temas"] or []),
        )
        for linha in linhas
    ]


def item_casa_com_regra(item: ItemParaAlerta, regra: RegraAlerta) -> bool:
    """Regra sem termos nem temas casa com tudo (rota "me avise de
    qualquer coisa nova") — caso de uso legítimo, não um bug de filtro
    vazio."""
    if not regra.termos and not regra.temas:
        return True
    texto_normalizado = item.texto_buscavel.lower()
    if any(termo.lower() in texto_normalizado for termo in regra.termos):
        return True
    temas_item = {t.lower() for t in item.temas}
    return any(tema.lower() in temas_item for tema in regra.temas)


async def ja_disparado(conn: Conn, alerta_id: str, item: ItemParaAlerta, canal: str) -> bool:
    """Só um disparo com `status='ok'` conta como "já feito". Uma falha
    anterior (ex.: o destino estava fora do ar) não pode bloquear pra
    sempre a próxima tentativa — achado real testando contra um webhook
    que devolveu 429 (rate limit) durante o desenvolvimento: sem esse
    filtro por status, aquele item nunca mais seria notificado."""
    return (
        await conn.fetchval(
            "select 1 from alerta_disparo where alerta_id=$1 and item_tipo=$2 "
            "and item_chave=$3 and canal=$4 and status='ok'",
            alerta_id,
            item.item_tipo,
            item.item_chave,
            canal,
        )
        is not None
    )


async def registrar_disparo(
    conn: Conn,
    alerta_id: str,
    item: ItemParaAlerta,
    canal: str,
    status: str,
    erro: str | None,
) -> None:
    # on conflict do update (não do nothing): uma nova tentativa depois de
    # uma falha precisa substituir a linha de erro por 'ok', não ser
    # descartada silenciosamente pelo unique index.
    await conn.execute(
        """
        insert into alerta_disparo (alerta_id, item_tipo, item_chave, canal, status, erro)
        values ($1, $2, $3, $4, $5, $6)
        on conflict (alerta_id, item_tipo, item_chave, canal) do update set
            status = excluded.status,
            erro = excluded.erro,
            criado_em = now()
        """,
        alerta_id,
        item.item_tipo,
        item.item_chave,
        canal,
        status,
        erro,
    )


# --------------------------------------------------------------------------
# Canais de notificação. Cada um lança em erro (nunca retorna um booleano
# escondendo a falha) — quem chama decide o que fazer (aqui: registrar em
# `alerta_disparo` com status='erro' e seguir pros próximos itens/canais).
# --------------------------------------------------------------------------


async def enviar_webhook(destino: dict, payload: dict) -> None:
    url = destino.get("url")
    if not url:
        raise ValueError("destino sem 'url' para canal webhook")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()


async def enviar_email(destino: dict, assunto: str, corpo_texto: str) -> None:
    settings = get_settings()
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY não configurada")
    para = destino.get("email")
    if not para:
        raise ValueError("destino sem 'email' para canal email")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": destino.get("from") or RESEND_FROM_PADRAO,
                "to": [para],
                "subject": assunto,
                "text": corpo_texto,
            },
        )
        resp.raise_for_status()


async def enviar_telegram(destino: dict, texto: str) -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado")
    chat_id = destino.get("chat_id")
    if not chat_id:
        raise ValueError("destino sem 'chat_id' para canal telegram")
    url = TELEGRAM_URL.format(token=settings.telegram_bot_token)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": texto})
        resp.raise_for_status()


def montar_mensagem(regra: RegraAlerta, item: ItemParaAlerta) -> tuple[str, str]:
    """(assunto, corpo) — texto simples, igual pros 3 canais (webhook
    recebe o payload estruturado à parte, isso aqui é só pra email/telegram
    e pro campo 'titulo' do payload do webhook)."""
    assunto = f"[RADAR ANVISA] {regra.nome}: {item.titulo}"
    corpo = f"{item.titulo}\n\n{item.url or '(sem link direto)'}"
    return assunto, corpo


async def notificar(conn: Conn, regra: RegraAlerta, item: ItemParaAlerta) -> int:
    """Tenta cada canal da regra pra esse item (pulando os que já
    dispararam antes) e retorna quantos canais foram enviados **com
    sucesso** agora — não confundir com "tentativas": uma falha (ex.: 429
    do lado de fora) também vira linha em `alerta_disparo`, mas não conta
    aqui, senão o job reporta "N notificações enviadas" quando na verdade
    N-k falharam."""
    assunto, corpo = montar_mensagem(regra, item)
    enviados_com_sucesso = 0
    for canal in regra.canais:
        if await ja_disparado(conn, regra.id, item, canal):
            continue
        try:
            if canal == "webhook":
                await enviar_webhook(
                    regra.destino,
                    {
                        "regra": regra.nome,
                        "item_tipo": item.item_tipo,
                        "titulo": item.titulo,
                        "url": item.url,
                        "visto_em": item.visto_em.isoformat(),
                    },
                )
            elif canal == "email":
                await enviar_email(regra.destino, assunto, corpo)
            elif canal == "telegram":
                await enviar_telegram(regra.destino, f"{assunto}\n\n{corpo}")
            else:
                raise ValueError(f"canal desconhecido: {canal}")
        except Exception as erro:  # noqa: BLE001 — vira linha de alerta_disparo, não crasha o job
            await registrar_disparo(conn, regra.id, item, canal, "erro", str(erro))
        else:
            await registrar_disparo(conn, regra.id, item, canal, "ok", None)
            enviados_com_sucesso += 1
    return enviados_com_sucesso


async def rodar_verificacao(conn: Conn) -> int:
    """Roda a checagem completa: todas as regras ativas x itens recentes.
    Retorna o número de notificações **enviadas com sucesso** (novas, sem
    contar as que já tinham disparado antes nem as que falharam)."""
    regras = await listar_regras_ativas(conn)
    if not regras:
        return 0
    itens = await buscar_itens_recentes(conn)
    total_enviado = 0
    for regra in regras:
        for item in itens:
            if item_casa_com_regra(item, regra):
                total_enviado += await notificar(conn, regra, item)
    return total_enviado


# --------------------------------------------------------------------------
# CRUD de `alerta_regra` — usado pelos endpoints REST em app/main.py.
# --------------------------------------------------------------------------


async def criar_regra(
    conn: Conn,
    nome: str,
    termos: list[str],
    temas: list[str],
    canais: list[str],
    destino: dict,
) -> RegraAlerta:
    row = await conn.fetchrow(
        """
        insert into alerta_regra (nome, termos, temas, canais, destino, ativo)
        values ($1, $2, $3, $4, $5, true)
        returning id, nome, termos, temas, canais, destino, ativo
        """,
        nome,
        termos,
        temas,
        canais,
        json.dumps(destino),
    )
    assert row is not None
    return RegraAlerta(
        id=str(row["id"]),
        nome=row["nome"],
        termos=list(row["termos"] or []),
        temas=list(row["temas"] or []),
        canais=list(row["canais"] or []),
        destino=json.loads(row["destino"])
        if isinstance(row["destino"], str)
        else (row["destino"] or {}),
        ativo=row["ativo"],
    )


async def listar_regras(conn: Conn) -> list[RegraAlerta]:
    linhas = await conn.fetch(
        "select id, nome, termos, temas, canais, destino, ativo from alerta_regra order by nome"
    )
    return [
        RegraAlerta(
            id=str(linha["id"]),
            nome=linha["nome"],
            termos=list(linha["termos"] or []),
            temas=list(linha["temas"] or []),
            canais=list(linha["canais"] or []),
            destino=json.loads(linha["destino"])
            if isinstance(linha["destino"], str)
            else (linha["destino"] or {}),
            ativo=linha["ativo"],
        )
        for linha in linhas
    ]


async def definir_ativo(conn: Conn, alerta_id: str, ativo: bool) -> bool:
    resultado = await conn.execute(
        "update alerta_regra set ativo = $2 where id = $1", alerta_id, ativo
    )
    return resultado != "UPDATE 0"


async def excluir_regra(conn: Conn, alerta_id: str) -> bool:
    resultado = await conn.execute("delete from alerta_regra where id = $1", alerta_id)
    return resultado != "DELETE 0"

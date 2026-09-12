"""Ingestor do DOU via INLABS (Imprensa Nacional).

Fluxo confirmado testando de ponta a ponta com conta real (ver
`research/FONTES.md`, seção "3. DOU em tempo real — INLABS", e o script
oficial salvo em `research/samples/inlabs-auto-download-xml.py`):

1. `POST /logar.php` (email+senha reais — usuário genérico falha em
   silêncio, sem erro visível) devolve o cookie `inlabs_session_cookie`.
2. `GET /index.php?p={AAAA-MM-DD}&dl={AAAA-MM-DD}-{secao}.zip` com o cookie
   e o header extra `origem: 736372697074` (hex de "script") baixa o zip do
   dia. **Pegadinha real**: uma data sem pasta no servidor devolve HTTP 200
   com a página HTML do diretório raiz em vez de erro — por isso todo
   download é validado pelos magic bytes de ZIP (`PK\x03\x04`), nunca só
   pelo status code.
3. Cada XML dentro do zip é UTF-8 com BOM (`utf-8-sig`) — bem mais simples
   que o AnvisaLegis (sem entidades HTML soltas). Filtra por `artCategory`
   contendo literalmente "Agência Nacional de Vigilância Sanitária" (um
   `"ANVISA"` genérico dá falso positivo — confirmado com matéria real de
   outro órgão que só citava "ANVISA" no corpo).
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date

import asyncpg
import asyncpg.pool
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.config import get_settings
from app.intent import GRUPOS_TIPO_ATO, _chave_tipo_ato  # noqa: PLC2701 (reuso pragmático)

Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

URL_LOGIN = "https://inlabs.in.gov.br/logar.php"
URL_DOWNLOAD = "https://inlabs.in.gov.br/index.php"
HEADER_ORIGEM = "736372697074"  # hex de "script", exigido pelo backend

# Confirmado na listagem real de um dia (?p=AAAA-MM-DD, sem dl) — ver
# research/FONTES.md. DO1E/DO2E são edições extras e nem sempre existem
# (validado via magic bytes, não é erro se faltar).
SECOES_DOU = ("DO1", "DO1E", "DO2", "DO2E", "DO3")

ARTCATEGORIA_ANVISA = "Agência Nacional de Vigilância Sanitária"

_MAGIC_ZIP = b"PK\x03\x04"


class LoginFalhouError(RuntimeError):
    pass


@dataclass
class MateriaDou:
    id_materia_inlabs: str
    edicao: date
    secao: str
    orgao: str | None
    titulo: str
    texto: str
    url: str | None
    tipo_ato: str | None
    numero: str | None
    ano: int | None


_RE_IDENTIFICA = re.compile(
    r"([A-ZÀ-Ü][A-ZÀ-Ü\s\-]*?)\s*N[ºO°]\s*([\d.\-/]+)\s*,\s*DE\s+\d{1,2}\s+DE\s+\w+\s+DE\s+(\d{4})",
    re.UNICODE | re.IGNORECASE,
)


def _extrair_tipo_numero_ano(identifica: str) -> tuple[str | None, str | None, int | None]:
    """Extrai (tipo_ato, numero, ano) do cabeçalho `Identifica`
    (ex.: "RESOLUÇÃO-RE Nº 3.547, DE 9 DE SETEMBRO DE 2026") — best-effort,
    usado só para tentar vincular a uma norma já conhecida; não bloqueia a
    gravação da matéria do DOU se não casar."""
    m = _RE_IDENTIFICA.search(identifica.strip())
    if not m:
        return None, None, None
    tipo_bruto, numero_bruto, ano_str = m.groups()
    numero = re.sub(r"\D", "", numero_bruto)
    if not numero:
        return None, None, None
    return tipo_bruto.strip(), str(int(numero)), int(ano_str)


def _parse_artigo_xml(bruto: bytes) -> MateriaDou | None:
    # xml.etree (não selectolax): o CDATA vira "bogus comment" num parser
    # HTML5, apagando o conteúdo de `Identifica`/`Texto` — achado real ao
    # rodar contra as amostras salvas. Isto aqui é XML de verdade, não HTML.
    texto_xml = bruto.decode("utf-8-sig")
    try:
        root = ET.fromstring(texto_xml)
    except ET.ParseError:
        return None
    artigo = root.find(".//article")
    if artigo is None:
        return None
    categoria = artigo.attrib.get("artCategory") or ""
    if ARTCATEGORIA_ANVISA not in categoria:
        return None

    id_materia = artigo.attrib.get("idMateria") or artigo.attrib.get("id") or ""
    pub_date_bruto = artigo.attrib.get("pubDate") or ""
    try:
        dia, mes, ano = (int(p) for p in pub_date_bruto.split("/"))
        edicao = date(ano, mes, dia)
    except ValueError:
        return None

    identifica_no = artigo.find(".//Identifica")
    identifica = (identifica_no.text or "").strip() if identifica_no is not None else ""
    texto_no = artigo.find(".//Texto")
    texto = (texto_no.text or "") if texto_no is not None else ""
    titulo = identifica or artigo.attrib.get("name") or id_materia

    tipo_ato, numero, ano_ato = _extrair_tipo_numero_ano(identifica)

    return MateriaDou(
        id_materia_inlabs=id_materia,
        edicao=edicao,
        secao=artigo.attrib.get("pubName") or "",
        orgao=categoria,
        titulo=titulo,
        texto=texto,
        url=artigo.attrib.get("pdfPage"),
        tipo_ato=tipo_ato,
        numero=numero,
        ano=ano_ato,
    )


def parse_zip_anvisa(zip_bytes: bytes) -> list[MateriaDou]:
    """Extrai do zip do dia só as matérias cuja `artCategory` é da ANVISA."""
    resultado: list[MateriaDou] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for nome in zf.namelist():
            if not nome.lower().endswith(".xml"):
                continue
            materia = _parse_artigo_xml(zf.read(nome))
            if materia is not None:
                resultado.append(materia)
    return resultado


class InlabsClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": settings.crawl_user_agent}, timeout=60.0
        )
        self._owns_client = client is None
        self._logado = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def login(self) -> None:
        if not self._settings.inlabs_email or not self._settings.inlabs_password:
            raise LoginFalhouError("INLABS_EMAIL / INLABS_PASSWORD não configurados no .env")
        # login bem-sucedido responde 302 (redireciona para index.php) — não
        # dá pra chamar raise_for_status() aqui, ela também trata redirect
        # não seguido como erro. O sinal real de sucesso é o cookie abaixo,
        # exatamente como o script oficial confirma (ver módulo docstring).
        await self._client.post(
            URL_LOGIN,
            data={"email": self._settings.inlabs_email, "password": self._settings.inlabs_password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not self._client.cookies.get("inlabs_session_cookie"):
            raise LoginFalhouError(
                "login não retornou inlabs_session_cookie — credencial inválida "
                "(ver research/FONTES.md: precisa ser e-mail real de conta cadastrada)"
            )
        self._logado = True

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=15),
        reraise=True,
    )
    async def baixar_secao(self, dia: date, secao: str) -> bytes | None:
        """Baixa o zip de uma seção do DOU. Retorna None se a seção não
        existir naquele dia (404, ou 200 com página HTML em vez de zip —
        ver módulo docstring)."""
        if not self._logado:
            await self.login()
        data_str = dia.isoformat()
        nome_arquivo = f"{data_str}-{secao}.zip"
        resp = await self._client.get(
            URL_DOWNLOAD,
            params={"p": data_str, "dl": nome_arquivo},
            headers={"origem": HEADER_ORIGEM},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        if not resp.content.startswith(_MAGIC_ZIP):
            return None  # HTML do diretório raiz disfarçado de 200 — não é o zip
        return resp.content

    async def baixar_dia_anvisa(self, dia: date) -> list[MateriaDou]:
        """Baixa todas as seções confirmadas do dia e devolve só as
        matérias da ANVISA, já deduplicadas por `id_materia_inlabs`."""
        vistos: set[str] = set()
        resultado: list[MateriaDou] = []
        for secao in SECOES_DOU:
            zip_bytes = await self.baixar_secao(dia, secao)
            if zip_bytes is None:
                continue
            for materia in parse_zip_anvisa(zip_bytes):
                if materia.id_materia_inlabs in vistos:
                    continue
                vistos.add(materia.id_materia_inlabs)
                resultado.append(materia)
        return resultado


async def _buscar_norma_id_por_tipo_numero_ano(
    conn: Conn, tipo_bruto: str, numero: str, ano: int
) -> str | None:
    """Vínculo best-effort com uma norma já carregada do módulo 310 — usa os
    grupos de equivalência de `app.intent` porque o mesmo tipo de ato é
    gravado com siglas diferentes conforme a fonte (achado do M4)."""
    chave = _chave_tipo_ato(tipo_bruto)
    variantes = GRUPOS_TIPO_ATO.get(chave, [chave])
    row = await conn.fetchrow(
        "select id from norma where tipo_ato = any($1::text[]) and numero = $2 and ano = $3",
        variantes,
        numero,
        ano,
    )
    return str(row["id"]) if row else None


async def upsert_materia_dou(conn: Conn, materia: MateriaDou) -> None:
    norma_id = None
    if materia.tipo_ato and materia.numero and materia.ano:
        norma_id = await _buscar_norma_id_por_tipo_numero_ano(
            conn, materia.tipo_ato, materia.numero, materia.ano
        )
    await conn.execute(
        """
        insert into dou_materia (
            edicao, secao, orgao, titulo, texto, url, norma_id, id_materia_inlabs
        )
        values ($1, $2, $3, $4, $5, $6, $7, $8)
        on conflict (id_materia_inlabs) do update set
            titulo   = excluded.titulo,
            texto    = excluded.texto,
            url      = excluded.url,
            norma_id = coalesce(excluded.norma_id, dou_materia.norma_id)
        """,
        materia.edicao,
        materia.secao,
        materia.orgao,
        materia.titulo,
        materia.texto,
        materia.url,
        norma_id,
        materia.id_materia_inlabs,
    )

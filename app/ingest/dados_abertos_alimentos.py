"""Fallback de registro de produtos de alimentos/suplementos via
**dados abertos** da ANVISA (`DADOS_ABERTOS_ALIMENTO.csv`).

Por quê: a API de `consultas.anvisa.gov.br` passou a exigir um token
Cloudflare Turnstile (HTTP 403 `turnstile_invalido`) — só um navegador real
consegue gerá-lo, então a consulta ao vivo deixou de funcionar para
clientes automatizados. O arquivo de dados abertos
(https://dados.anvisa.gov.br/dados/DADOS_ABERTOS_ALIMENTO.csv, ~10 MB,
~50 mil linhas, publicado pela própria ANVISA) é a alternativa oficial,
sem captcha.

Limitações reais do CSV (sinalizadas na resposta via `fonte`):
- não tem marca (`marcas` sempre vazio; o filtro `marca` casa de forma
  aproximada com nome do produto/razão social);
- não distingue "Notificado"/"Registrado" (`tipo_regularizacao` fica
  `None`);
- é um snapshot — pode estar atrasado frente à consulta ao vivo.

Cache em memória com TTL; o download é feito uma vez por processo (a cada
`_TTL_SEGUNDOS`) sob lock, não por busca.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from curl_cffi.requests import AsyncSession

from app.config import get_settings

URL_CSV = "https://dados.anvisa.gov.br/dados/DADOS_ABERTOS_ALIMENTO.csv"
FONTE = "dados_abertos"
_TTL_SEGUNDOS = 12 * 3600.0


def _norm(texto: str | None) -> str:
    if not texto:
        return ""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", sem_acento).strip().lower()


def _digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


@dataclass(frozen=True)
class Linha:
    cnpj: str
    razao_social: str
    produto: str
    processo: str
    tipo: str
    categoria: str
    data_finalizacao: datetime | None
    registro: str
    vencimento: datetime | None
    situacao: str
    # pré-normalizados pra busca
    produto_n: str
    razao_n: str


_indice: list[Linha] | None = None
_indice_ate: float = 0.0
_lock = asyncio.Lock()


def _parse_data(bruto: str) -> datetime | None:
    bruto = (bruto or "").strip()
    if not bruto:
        return None
    try:
        return datetime.strptime(bruto, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return None


def parse_csv(conteudo: bytes) -> list[Linha]:
    texto = conteudo.decode("latin-1")
    leitor = csv.DictReader(io.StringIO(texto), delimiter=";")
    linhas: list[Linha] = []
    for row in leitor:
        produto = (row.get("NO_PRODUTO") or "").strip()
        razao = (row.get("NO_RAZAO_SOCIAL_EMPRESA") or "").strip()
        linhas.append(
            Linha(
                cnpj=_digitos(row.get("NU_CNPJ_EMPRESA")),
                razao_social=razao,
                produto=produto,
                processo=_digitos(row.get("NU_PROCESSO")),
                tipo=(row.get("DS_TIPO_PRODUTO") or "").strip(),
                categoria=(row.get("DS_CATEGORIA_PRODUTO") or "").strip(),
                data_finalizacao=_parse_data(row.get("DT_FINALIZACAO_PROCESSO") or ""),
                registro=(row.get("NU_REGISTRO_PRODUTO") or "").strip(),
                vencimento=_parse_data(row.get("DT_VENCIMENTO_REGISTRO") or ""),
                situacao=(row.get("ST_SITUACAO_REGISTRO") or "").strip(),
                produto_n=_norm(produto),
                razao_n=_norm(razao),
            )
        )
    return linhas


async def _baixar() -> bytes:
    sessao: AsyncSession[Any] = AsyncSession(
        headers={"User-Agent": get_settings().crawl_user_agent},
        impersonate="chrome",
        timeout=180,
    )
    try:
        resp = await sessao.get(URL_CSV)
        resp.raise_for_status()
        return bytes(resp.content)
    finally:
        await sessao.close()


async def carregar_indice() -> list[Linha]:
    global _indice, _indice_ate
    if _indice is not None and time.monotonic() < _indice_ate:
        return _indice
    async with _lock:
        if _indice is not None and time.monotonic() < _indice_ate:
            return _indice
        try:
            novo = parse_csv(await _baixar())
        except Exception:
            if _indice is not None:  # snapshot velho é melhor que nada
                _indice_ate = time.monotonic() + 600
                return _indice
            raise
        _indice = novo
        _indice_ate = time.monotonic() + _TTL_SEGUNDOS
        return _indice


def filtrar(
    linhas: list[Linha],
    *,
    nome_produto: str | None = None,
    marca: str | None = None,
    detentor_registro: str | None = None,
    numero_processo: str | None = None,
    numero_registro_notificacao: str | None = None,
    situacao_registro: str | None = None,
) -> list[Linha]:
    nome_n = _norm(nome_produto)
    marca_n = _norm(marca)
    det_n = _norm(detentor_registro)
    det_dig = _digitos(detentor_registro)
    eh_cnpj = len(det_dig) >= 8 and det_dig == re.sub(r"[.\-/\s]", "", detentor_registro or "")
    proc = _digitos(numero_processo)
    reg = (numero_registro_notificacao or "").strip()

    saida: list[Linha] = []
    for ln in linhas:
        if situacao_registro and ln.situacao != situacao_registro:
            continue
        if nome_n and nome_n not in ln.produto_n:
            continue
        if marca_n and marca_n not in ln.produto_n and marca_n not in ln.razao_n:
            continue
        if det_n:
            if eh_cnpj:
                if det_dig.lstrip("0") not in ln.cnpj:
                    continue
            elif det_n not in ln.razao_n:
                continue
        if proc and ln.processo != proc:
            continue
        if reg and ln.registro != reg:
            continue
        saida.append(ln)
    return saida

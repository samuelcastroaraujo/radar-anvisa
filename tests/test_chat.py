"""Regressão de um bug real achado com tráfego de produção (não local, não
golden QA — ver CLAUDE.md): `_contexto_tematico` guardava `ResultadoBusca.
norma_id` sem `str()` em `NormaCitada.id`. O dataclass anota o campo como
`str`, mas o asyncpg devolve `uuid.UUID` de verdade pra coluna `norma_id` —
como dataclass não valida tipo em runtime, isso só quebrava na fronteira
HTTP real (`NormaCitadaResponse`, um model Pydantic), nunca no golden QA
(que chama `app.chat.responder()` direto, sem passar pela validação
Pydantic — não cobre esta classe de bug).

Este teste não bate no banco nem no LLM — só reproduz a mesma forma de
dado (um `uuid.UUID` cru) que o Postgres devolve e verifica que o que sai
de `_contexto_tematico` sobrevive à validação Pydantic real usada em
`POST /chat`.
"""

from __future__ import annotations

import uuid

from app.busca import ResultadoBusca
from app.chat import _contexto_tematico
from app.main import NormaCitadaResponse


def _resultado(norma_id: object) -> ResultadoBusca:
    return ResultadoBusca(
        chunk_id=str(uuid.uuid4()),
        norma_id=norma_id,  # type: ignore[arg-type]  # de propósito: é isso que o bug reproduz
        tipo_ato="RDC",
        numero="27",
        ano=2010,
        status_vigencia="revogada",
        ementa="ementa de teste",
        rotulo="Art. 1º",
        conteudo="conteúdo de teste",
        url_origem="https://example.com",
        score=1.0,
    )


def test_contexto_tematico_com_norma_id_como_uuid_cru_nao_quebra_pydantic() -> None:
    """Reproduz o formato real que o asyncpg devolve (uuid.UUID, não str)
    pra `c.norma_id` — é exatamente essa mistura de tipo que passava pelo
    dataclass sem erro e só estourava no `NormaCitadaResponse` real."""
    uuid_cru = uuid.uuid4()
    _, citadas, _ = _contexto_tematico([_resultado(uuid_cru)])

    assert len(citadas) == 1
    assert isinstance(citadas[0].id, str)
    assert citadas[0].id == str(uuid_cru)

    # a prova de fogo: isso é o que o endpoint /chat faz de verdade, e é
    # isso que quebrava com um uuid.UUID cru (pydantic_core.ValidationError:
    # "Input should be a valid string [type=string_type]").
    NormaCitadaResponse(
        id=citadas[0].id,
        tipo_ato=citadas[0].tipo_ato,
        numero=citadas[0].numero,
        ano=citadas[0].ano,
        status_vigencia=citadas[0].status_vigencia,
        url_origem=citadas[0].url_origem,
    )

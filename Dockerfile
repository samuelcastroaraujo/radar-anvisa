FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /app

# Camada de dependências separada do código para cache eficiente no Railway.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
# [M7] o scheduler (app/scheduler.py) dispara cada fonte como subprocesso
# "python -m scripts.carga_*" — sem isso, o job diário falharia dentro do
# container com ModuleNotFoundError (achado revisando o Dockerfile antes
# do primeiro deploy real, nunca chegou a rodar em produção assim).
COPY scripts ./scripts

EXPOSE 8000
# Forma shell (não exec) de propósito: o Railway injeta $PORT em tempo de
# execução (não é fixo em 8000) — CMD em array não expande variável de
# ambiente, silenciosamente escutaria sempre em 8000 e o healthcheck do
# Railway falharia.
CMD uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}

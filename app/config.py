"""Configuração da aplicação, lida do ambiente/.env.

Ver `.env.example` para a lista de variáveis suportadas e
`research/FONTES.md` para o contexto de cada credencial externa.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Banco (Supabase Postgres) — string de conexão direta, usada pelo asyncpg.
    database_url: str = ""

    # Supabase (client REST, se vier a ser necessário além do Postgres direto)
    supabase_url: str = ""
    supabase_service_key: str = ""

    # LLM / Embeddings
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    # Embeddings via OpenRouter (proxy compatível com a API da OpenAI) —
    # decisão do M3, ver CLAUDE.md: chave real disponível era da OpenRouter,
    # não da OpenAI direto. Modelo usado (openai/text-embedding-3-large,
    # dim 3072) é o mesmo pedido no briefing, só o gateway muda.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # INLABS (Imprensa Nacional / DOU) — conta pessoal, ver research/FONTES.md
    inlabs_email: str = ""
    inlabs_password: str = ""

    # Alertas
    resend_api_key: str = ""
    telegram_bot_token: str = ""

    # Aplicação
    environment: str = "development"
    crawl_user_agent: str = "RadarAnvisaBot/1.0 (+contato: mba5@nutropolis.com.br)"
    # Job diário 06:00 BRT (seção 5). Desligável via .env — útil pra rodar a
    # API sem disparar ingestão (ex.: ambiente de teste) sem mexer no código.
    scheduler_habilitado: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()

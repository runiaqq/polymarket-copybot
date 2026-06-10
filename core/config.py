from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    telegram_bot_token: str
    telegram_webhook_secret: str
    admin_telegram_id: int

    # Supabase / DB
    supabase_url: str
    supabase_service_key: str
    database_url: str

    # Redis
    redis_url: str

    # Privy
    privy_app_id: str
    privy_app_secret: str

    # OpenAI
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    # Polymarket
    polymarket_chain_id: int = 137
    polymarket_clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/trade"
    polymarket_clob_rest_url: str = "https://clob.polymarket.com"

    # Alchemy
    alchemy_api_key: str
    polygon_rpc_url: str

    # App
    app_env: str = "development"
    webhook_base_url: str
    min_trade_size_usdc: float = 50.0
    min_market_hours_to_close: int = 48
    ai_risk_warn_threshold: int = 7


settings = Settings()  # type: ignore[call-arg]

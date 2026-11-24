from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql+psycopg2://postgres:password@localhost:5432/fishtrade"  # production (Railway provides DATABASE_URL)
    # fallback for local development
    # database_url: str = "sqlite:///./data/fishtrade.db"
    jwt_secret: str = "development-only-change-me"
    jwt_expire_minutes: int = 720
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()

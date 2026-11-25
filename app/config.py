from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
# The DATABASE_URL will be supplied by Railway (or set locally via .env)
    # Use the placeholder syntax so Railway can replace it automatically.
    # Example entry in .env:
    # DATABASE_URL=${{ Postgres.DATABASE_PRIVATE_URL }}
    database_url: str = "${{ Postgres.DATABASE_PRIVATE_URL }}"  # will be overridden by env var


    @property
    def validated_database_url(self) -> str:
        """Return a safe database URL, raising if unresolved placeholders are present.
        Also normalizes postgres:// to postgresql:// for SQLAlchemy compatibility.
        """
        url = self.database_url
        # Detect unexpanded Railway variable reference
        if "${{" in url:
            raise RuntimeError(
                "DATABASE_URL contains an unresolved Railway variable reference. "
                "Check the Variables section in Railway and ensure DATABASE_URL is set correctly."
            )
        # Normalise legacy postgres:// scheme
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url
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

import os

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_URL_VARS = ("DATABASE_URL", "DATABASE_PRIVATE_URL", "POSTGRES_URL", "POSTGRES_PRIVATE_URL")
_LOCAL_SQLITE = "sqlite:///./data/fishtrade.db"


def _on_railway() -> bool:
    return bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("RAILWAY_SERVICE_ID")
    )


class Settings(BaseSettings):
    # Reads APP_ENV or ENVIRONMENT (run.py and Railway use ENVIRONMENT)
    app_env: str = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENV", "ENVIRONMENT"),
    )

    # First of DATABASE_URL, DATABASE_PRIVATE_URL, POSTGRES_URL, POSTGRES_PRIVATE_URL.
    # In Railway: Variables -> Add Reference -> Postgres -> DATABASE_PRIVATE_URL,
    # and name it DATABASE_URL. Do NOT put a ${{ ... }} placeholder in code.
    database_url: str = Field(default="", validation_alias=AliasChoices(*_URL_VARS))

    # Set ENABLE_DOCS=true in Railway to expose /docs while in production (turn off when done testing)
    enable_docs: bool = False

    jwt_secret: str = "development-only-change-me"
    jwt_expire_minutes: int = 720
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    @model_validator(mode="after")
    def _resolve(self):
        env = self.app_env.strip().lower()
        self.app_env = "production" if env in {"prod", "production"} else env
        is_prod = _on_railway() or self.app_env == "production"

        url = (self.database_url or "").strip()
        if "${{" in url:
            raise RuntimeError(
                "DATABASE_URL contains an unresolved Railway reference. In Railway: "
                "Variables -> Add Reference -> Postgres -> DATABASE_PRIVATE_URL, named DATABASE_URL."
            )
        if not url:
            if is_prod:
                raise RuntimeError(
                    "No Postgres URL set. In Railway: Variables -> Add Reference -> "
                    "Postgres -> DATABASE_PRIVATE_URL, named DATABASE_URL."
                )
            print("[WARN] No DATABASE_URL set; using local SQLite (development only).")
            url = _LOCAL_SQLITE

        # Normalise legacy scheme for SQLAlchemy
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)

        if is_prod and url.startswith("sqlite"):
            raise RuntimeError("SQLite is not allowed in production/Railway. Use the Postgres URL.")

        if is_prod and self.jwt_secret == "development-only-change-me":
            print("[WARN] JWT_SECRET is the development default. Set a real secret in Railway.")

        self.database_url = url
        return self

    @property
    def validated_database_url(self) -> str:
        return self.database_url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
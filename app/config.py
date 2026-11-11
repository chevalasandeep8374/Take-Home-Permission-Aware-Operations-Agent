from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    admin_database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/hms_db",
        alias="ADMIN_DATABASE_URL",
    )
    database_url: str = Field(
        default="postgresql://hms_app:hms_app_password@localhost:5432/hms_db",
        alias="DATABASE_URL",
    )
    jwt_secret: str = Field(
        default="supersecretjwtkey_replace_in_production_min_32_chars",
        alias="JWT_SECRET",
    )
    gemini_api_key: str = Field(
        default="",
        alias="GEMINI_API_KEY",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

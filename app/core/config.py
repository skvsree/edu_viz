from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://srs:srs@localhost:5432/srs"
    secret_key: str = "dev-secret"


settings = Settings()

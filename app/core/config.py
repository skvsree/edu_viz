from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://srs:srs@localhost:5432/srs"
    secret_key: str = "dev-secret"

    # Azure AD B2C (OIDC) - all configured via env vars
    azure_b2c_tenant_name: str | None = None  # e.g. "contoso" (without .onmicrosoft.com)
    azure_b2c_tenant_domain: str | None = None  # e.g. "contoso.onmicrosoft.com" (optional alternative)
    azure_b2c_policy: str | None = None  # e.g. "B2C_1_signupsignin"
    azure_b2c_client_id: str | None = None
    azure_b2c_client_secret: str | None = None  # optional
    azure_b2c_redirect_uri: str = "http://localhost:8000/auth/callback"


settings = Settings(_env_parse_none_str="")

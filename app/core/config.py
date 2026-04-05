from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://srs:srs@localhost:5432/srs"
    secret_key: str = ""
    app_session_cookie_name: str = "eduviz_session"
    app_session_max_age_seconds: int = 45 * 24 * 60 * 60
    oidc_state_session_max_age_seconds: int = 45 * 24 * 60 * 60
    system_admin_bootstrap_email: str = "skv.sree@outlook.com"
    footer_copyright_text: str = "SelViz Software Solutions"

    ai_study_pack_provider: str = "openai"
    openai_api_key: str | None = None
    minimax_api_key: str | None = None
    claude_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    openai_generation_enabled: bool = True
    ai_secrets_fernet_key: str | None = None
    bulk_import_api_key: str | None = None

    # Default question count for auto-starting tests. When set (positive integer),
    # the Test button will skip the question count modal and create a test with
    # this many questions. Set to 0 to disable auto-start (shows modal instead).
    default_test_count: int = 0

    # Default test access for users. Can be overridden at organization and user level.
    # When False, users need explicit org-level or user-level enablement.
    test_enabled_default: bool = False

    # Test throttling: max tests per user per day (0 = unlimited)
    test_daily_limit: int = 0
    # Test throttle: minimum seconds between test attempts (0 = no cooldown)
    test_cooldown_seconds: int = 0

    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:8000/auth/callback/google"
    google_scopes: str = "openid email profile"

    # Microsoft Entra External ID / generic OIDC configuration.


    # Prefer the MICROSOFT_ENTRA_EXTERNAL_ID_* env vars for new deployments.
    # Legacy AZURE_B2C_* env vars are still accepted as fallbacks to ease migration.
    microsoft_entra_external_id_tenant_id: str | None = None
    microsoft_entra_external_id_tenant_domain: str | None = None
    microsoft_entra_external_id_authority: str | None = None
    microsoft_entra_external_id_authorize_authority: str | None = None
    microsoft_entra_external_id_metadata_url: str | None = None
    microsoft_entra_external_id_client_id: str | None = None
    microsoft_entra_external_id_client_secret: str | None = None
    microsoft_entra_external_id_redirect_uri: str = "http://localhost:8000/auth/callback"
    microsoft_entra_external_id_scopes: str = "openid profile email"

    # Legacy Azure AD B2C env vars kept for backward compatibility.
    azure_b2c_tenant_name: str | None = None  # e.g. "contoso" (without .onmicrosoft.com)
    azure_b2c_tenant_domain: str | None = None  # e.g. "contoso.onmicrosoft.com"
    azure_b2c_policy: str | None = None  # e.g. "B2C_1_signupsignin"
    azure_b2c_client_id: str | None = None
    azure_b2c_client_secret: str | None = None
    azure_b2c_redirect_uri: str | None = None

    @property
    def is_secure_cookies(self) -> bool:
        return self.force_secure_cookies

    force_secure_cookies: bool = False

    @property
    def cookie_secure(self) -> bool:
        return self.force_secure_cookies

    allowed_origins: str = "*"

    @property
    def allowed_origins_list(self) -> list[str]:
        if self.allowed_origins == "*":
            return ["*"]
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings(_env_parse_none_str="")

if not settings.secret_key:
    raise ValueError(
        "SECRET_KEY is not set. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )

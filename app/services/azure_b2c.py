from app.services.microsoft_identity import build_oauth, load_identity_config

# Backward-compatible wrapper kept temporarily during the migration away from
# Azure AD B2C-specific naming in the codebase.
load_b2c_config = load_identity_config

__all__ = ["build_oauth", "load_b2c_config"]

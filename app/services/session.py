import uuid
from itsdangerous import BadSignature, URLSafeSerializer

from app.core.config import settings

_serializer = URLSafeSerializer(settings.secret_key, salt="session")


def sign_session(*, user_id: uuid.UUID | None = None, claims: dict | None = None) -> str:
    payload: dict = {}
    if user_id is not None:
        payload["user_id"] = str(user_id)
    if claims is not None:
        payload["claims"] = claims
    return _serializer.dumps(payload)


def unsign_session(token: str) -> dict | None:
    try:
        data = _serializer.loads(token)
        if not isinstance(data, dict):
            return None
        return data
    except (BadSignature, ValueError, TypeError):
        return None

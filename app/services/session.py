import uuid
from itsdangerous import BadSignature, URLSafeSerializer

from app.core.config import settings

_serializer = URLSafeSerializer(settings.secret_key, salt="session")


def sign_session(user_id: uuid.UUID) -> str:
    return _serializer.dumps({"user_id": str(user_id)})


def unsign_session(token: str) -> uuid.UUID | None:
    try:
        data = _serializer.loads(token)
        return uuid.UUID(data.get("user_id"))
    except (BadSignature, ValueError, TypeError):
        return None

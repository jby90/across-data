import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings

_hasher = PasswordHasher()
_ALGORITHM = "HS256"
_ISSUER = "cyshuhe"

# 令牌受众:用户端与管理端令牌互不通用
AUD_USER = "user"
AUD_ADMIN = "admin"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# 用于在用户不存在时消耗等量时间,避免通过响应时间枚举账号
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def burn_password_check(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def create_access_token(
    *, user_id: uuid.UUID, audience: str, role: str, org_id: uuid.UUID | None
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": audience,
        "sub": str(user_id),
        "role": role,
        "org": str(org_id) if org_id else None,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str, audience: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        get_settings().jwt_secret,
        algorithms=[_ALGORITHM],
        audience=audience,
        issuer=_ISSUER,
        options={"require": ["exp", "sub", "aud", "iss"]},
    )

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import AUD_ADMIN, AUD_USER, decode_access_token
from app.iam.models import Organization, Role, User

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    role: Role
    org_id: uuid.UUID | None


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或登录已失效")


def principal_for(audience: str, *allowed: Role) -> Callable:
    """生成依赖:校验令牌受众与角色,并复核账号和所属企业仍然有效。"""

    async def dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
        session: AsyncSession = Depends(get_session),
    ) -> Principal:
        if credentials is None:
            raise _unauthorized()
        try:
            claims = decode_access_token(credentials.credentials, audience)
            role = Role(claims["role"])
            user_id = uuid.UUID(claims["sub"])
        except (jwt.PyJWTError, ValueError, KeyError):
            raise _unauthorized() from None

        user = await session.get(User, user_id)
        if user is None or not user.is_active or user.role != role.value:
            raise _unauthorized()
        if user.org_id is not None:
            org = await session.get(Organization, user.org_id)
            if org is None or not org.is_active:
                raise _unauthorized()
        if allowed and role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问")
        return Principal(user_id=user.id, role=role, org_id=user.org_id)

    return dependency


# 管理端:平台管理员 / 复核员;用户端:企业管理员 / 成员
require_platform_admin = principal_for(AUD_ADMIN, Role.PLATFORM_ADMIN)
require_admin_side = principal_for(AUD_ADMIN, Role.PLATFORM_ADMIN, Role.REVIEWER)
require_org_admin = principal_for(AUD_USER, Role.ORG_ADMIN)
require_user_side = principal_for(AUD_USER, Role.ORG_ADMIN, Role.MEMBER)

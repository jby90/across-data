import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    AUD_ADMIN,
    burn_password_check,
    create_access_token,
    hash_password,
    verify_password,
)
from app.iam.models import ADMIN_ROLES, USER_ROLES, Organization, Role, User


class AuthError(Exception):
    """登录失败。对外统一提示,不区分账号不存在/密码错误/已停用。"""


class ConflictError(Exception):
    pass


class RuleError(Exception):
    pass


async def authenticate(
    session: AsyncSession, *, email: str, password: str, audience: str
) -> tuple[User, str]:
    from app.core.rls import DatabaseIdentity, bind_identity

    # Authentication can look up exactly one normalized email, not other tenants' rows.
    await bind_identity(session, DatabaseIdentity(scope="auth", auth_email=email.lower()))
    allowed = ADMIN_ROLES if audience == AUD_ADMIN else USER_ROLES
    user = (
        await session.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()
    if user is None:
        burn_password_check(password)
        raise AuthError
    if not verify_password(password, user.password_hash):
        raise AuthError
    if not user.is_active or Role(user.role) not in allowed:
        raise AuthError
    if user.org_id is not None:
        org = await session.get(Organization, user.org_id)
        if org is None or not org.is_active:
            raise AuthError
    token = create_access_token(
        user_id=user.id, audience=audience, role=user.role, org_id=user.org_id
    )
    return user, token


async def create_org(session: AsyncSession, name: str) -> Organization:
    org = Organization(name=name.strip())
    session.add(org)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError("企业名称已存在") from None
    return org


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password: str,
    role: Role,
    org_id: uuid.UUID | None,
) -> User:
    if role in ADMIN_ROLES and org_id is not None:
        raise RuleError("管理端角色不能属于企业")
    if role in USER_ROLES:
        if org_id is None:
            raise RuleError("用户端角色必须指定所属企业")
        if await session.get(Organization, org_id) is None:
            raise RuleError("所属企业不存在")
    user = User(
        email=email.lower(),
        display_name=display_name.strip(),
        password_hash=hash_password(password),
        role=role.value,
        org_id=org_id,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError("邮箱已被注册") from None
    return user

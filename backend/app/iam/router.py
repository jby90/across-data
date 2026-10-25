import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import AUD_ADMIN, AUD_USER
from app.iam import service
from app.iam.deps import (
    Principal,
    principal_for,
    require_org_admin,
    require_platform_admin,
)
from app.iam.models import USER_ROLES, Organization, Role, User
from app.iam.schemas import (
    LoginRequest,
    MemberCreate,
    OrgCreate,
    OrgOut,
    RedactionTermsIn,
    TokenResponse,
    UserCreate,
    UserOut,
)

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])
user_router = APIRouter(prefix="/api/user", tags=["user"])

_LOGIN_FAILED = HTTPException(status.HTTP_401_UNAUTHORIZED, "账号或密码错误")


async def _login(body: LoginRequest, session: AsyncSession, audience: str) -> TokenResponse:
    try:
        user, token = await service.authenticate(
            session, email=body.email, password=body.password, audience=audience
        )
    except service.AuthError:
        raise _LOGIN_FAILED from None
    return TokenResponse(access_token=token, role=Role(user.role), org_id=user.org_id)


@auth_router.post("/user/login", response_model=TokenResponse, summary="用户端登录")
async def user_login(body: LoginRequest, session: AsyncSession = Depends(get_session)):
    return await _login(body, session, AUD_USER)


@auth_router.post("/admin/login", response_model=TokenResponse, summary="管理端登录")
async def admin_login(body: LoginRequest, session: AsyncSession = Depends(get_session)):
    return await _login(body, session, AUD_ADMIN)


# ---- 管理端 ----


@admin_router.get("/me", response_model=UserOut, summary="当前管理端账号")
async def admin_me(
    p: Principal = Depends(principal_for(AUD_ADMIN, Role.PLATFORM_ADMIN, Role.REVIEWER)),
    session: AsyncSession = Depends(get_session),
):
    return await session.get(User, p.user_id)


@admin_router.post("/orgs", response_model=OrgOut, status_code=201, summary="创建企业")
async def create_org(
    body: OrgCreate,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await service.create_org(session, body.name)
    except service.ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None


@admin_router.get("/orgs", response_model=list[OrgOut], summary="企业列表")
async def list_orgs(
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    return (await session.execute(select(Organization).order_by(Organization.name))).scalars().all()


async def _save_redaction_terms(session, org_id, terms, actor_id):
    from app.knowledge.models import KnowledgeAuditRecord

    org = await session.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "企业不存在")
    payload = {"before": org.llm_redaction_terms or [], "after": terms}
    session.add(
        KnowledgeAuditRecord(
            target_type="organization_redaction",
            target_id=org.id,
            action="redaction_terms_changed",
            actor_id=actor_id,
            payload=payload,
            content_hash=hashlib.sha256(
                json.dumps(payload, ensure_ascii=False).encode()
            ).hexdigest(),
        )
    )
    org.llm_redaction_terms = terms
    await session.commit()
    return org


@admin_router.patch("/orgs/{org_id}/redaction", response_model=OrgOut)
async def set_org_redaction(
    org_id: uuid.UUID,
    body: RedactionTermsIn,
    principal: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    return await _save_redaction_terms(session, org_id, body.terms, principal.user_id)


@user_router.patch("/organization/redaction", response_model=OrgOut)
async def set_own_redaction(
    body: RedactionTermsIn,
    principal: Principal = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
):
    return await _save_redaction_terms(session, principal.org_id, body.terms, principal.user_id)


@admin_router.post("/users", response_model=UserOut, status_code=201, summary="创建账号")
async def create_user(
    body: UserCreate,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await service.create_user(session, **body.model_dump())
    except service.ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    except service.RuleError as e:
        raise HTTPException(422, str(e)) from None


@admin_router.get("/users", response_model=list[UserOut], summary="账号列表")
async def list_users(
    org_id: uuid.UUID | None = None,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(User).order_by(User.created_at)
    if org_id is not None:
        stmt = stmt.where(User.org_id == org_id)
    return (await session.execute(stmt)).scalars().all()


# ---- 用户端 ----


@user_router.get("/me", response_model=UserOut, summary="当前用户端账号")
async def user_me(
    p: Principal = Depends(principal_for(AUD_USER, Role.ORG_ADMIN, Role.MEMBER)),
    session: AsyncSession = Depends(get_session),
):
    return await session.get(User, p.user_id)


@user_router.get("/members", response_model=list[UserOut], summary="本企业成员列表")
async def list_members(
    p: Principal = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
):
    # 只能看到本企业,企业范围来自令牌而非请求参数
    stmt = select(User).where(User.org_id == p.org_id).order_by(User.created_at)
    return (await session.execute(stmt)).scalars().all()


@user_router.post("/members", response_model=UserOut, status_code=201, summary="新增本企业成员")
async def add_member(
    body: MemberCreate,
    p: Principal = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
):
    if body.role not in USER_ROLES:
        raise HTTPException(422, "角色不合法")
    try:
        return await service.create_user(
            session,
            email=body.email,
            display_name=body.display_name,
            password=body.password,
            role=body.role,
            org_id=p.org_id,
        )
    except service.ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    except service.RuleError as e:
        raise HTTPException(422, str(e)) from None

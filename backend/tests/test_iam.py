import jwt
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import AUD_USER, create_access_token
from app.iam.models import User

from .conftest import PASSWORD, bearer, login

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_health(client):
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_user_login_ok_and_me(client, seed):
    token = await login(client, "user", "a-admin@example.com")
    r = await client.get("/api/user/me", headers=bearer(token))
    assert r.status_code == 200
    assert r.json()["role"] == "org_admin"
    assert r.json()["org_id"] == str(seed["org_a"].id)


async def test_email_case_insensitive(client, seed):
    r = await client.post(
        "/api/auth/user/login", json={"email": "A-Admin@Example.com", "password": PASSWORD}
    )
    assert r.status_code == 200


async def test_wrong_password_and_unknown_user_same_response(client, seed):
    bad_pw = await client.post(
        "/api/auth/user/login",
        json={"email": "a-admin@example.com", "password": "wrong-password-1"},
    )
    unknown = await client.post(
        "/api/auth/user/login", json={"email": "nobody@example.com", "password": "wrong-password-1"}
    )
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json() == unknown.json()


async def test_side_separation_on_login(client, seed):
    # 用户端账号不能走管理端登录,反之亦然
    r1 = await client.post(
        "/api/auth/admin/login", json={"email": "a-admin@example.com", "password": PASSWORD}
    )
    r2 = await client.post(
        "/api/auth/user/login", json={"email": "admin@example.com", "password": PASSWORD}
    )
    assert r1.status_code == 401 and r2.status_code == 401


async def test_token_audience_not_interchangeable(client, seed):
    user_token = await login(client, "user", "a-admin@example.com")
    admin_token = await login(client, "admin", "admin@example.com")
    assert (await client.get("/api/admin/orgs", headers=bearer(user_token))).status_code == 401
    assert (await client.get("/api/user/me", headers=bearer(admin_token))).status_code == 401


async def test_requires_token(client, seed):
    assert (await client.get("/api/admin/orgs")).status_code == 401
    assert (await client.get("/api/user/members")).status_code == 401


async def test_tampered_and_expired_token_rejected(client, seed):
    good = await login(client, "user", "a-admin@example.com")
    assert (await client.get("/api/user/me", headers=bearer(good + "x"))).status_code == 401
    expired = jwt.encode(
        {
            "iss": "cyshuhe",
            "aud": AUD_USER,
            "sub": str(seed["a_admin"].id),
            "role": "org_admin",
            "exp": 1,
        },
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    assert (await client.get("/api/user/me", headers=bearer(expired))).status_code == 401


async def test_role_claim_must_match_db(client, seed, session_factory):
    # 令牌里声称的角色与库里不一致(例如被降权)时拒绝
    token = create_access_token(
        user_id=seed["a_member"].id, audience=AUD_USER, role="org_admin", org_id=seed["org_a"].id
    )
    assert (await client.get("/api/user/members", headers=bearer(token))).status_code == 401


async def test_member_cannot_manage_members(client, seed):
    token = await login(client, "user", "a-member@example.com")
    assert (await client.get("/api/user/members", headers=bearer(token))).status_code == 403


async def test_reviewer_cannot_use_platform_admin_apis(client, seed):
    token = await login(client, "admin", "reviewer@example.com")
    assert (await client.get("/api/admin/orgs", headers=bearer(token))).status_code == 403
    assert (await client.get("/api/admin/me", headers=bearer(token))).status_code == 200


async def test_org_admin_sees_only_own_org(client, seed):
    token = await login(client, "user", "a-admin@example.com")
    r = await client.get("/api/user/members", headers=bearer(token))
    emails = {u["email"] for u in r.json()}
    assert emails == {"a-admin@example.com", "a-member@example.com"}


async def test_org_admin_add_member_is_forced_into_own_org(client, seed):
    token = await login(client, "user", "a-admin@example.com")
    r = await client.post(
        "/api/user/members",
        headers=bearer(token),
        json={
            "email": "new@example.com",
            "display_name": "新成员",
            "password": PASSWORD,
            "org_id": str(seed["org_b"].id),  # 试图越权写入别家企业,应被忽略
        },
    )
    assert r.status_code == 201
    assert r.json()["org_id"] == str(seed["org_a"].id)


async def test_org_admin_cannot_create_platform_roles(client, seed):
    token = await login(client, "user", "a-admin@example.com")
    r = await client.post(
        "/api/user/members",
        headers=bearer(token),
        json={
            "email": "evil@example.com",
            "display_name": "x",
            "password": PASSWORD,
            "role": "platform_admin",
        },
    )
    assert r.status_code == 422


async def test_platform_admin_create_org_and_user(client, seed):
    token = await login(client, "admin", "admin@example.com")
    r = await client.post("/api/admin/orgs", headers=bearer(token), json={"name": "丙车企"})
    assert r.status_code == 201
    org_id = r.json()["id"]
    dup = await client.post("/api/admin/orgs", headers=bearer(token), json={"name": "丙车企"})
    assert dup.status_code == 409
    u = await client.post(
        "/api/admin/users",
        headers=bearer(token),
        json={
            "email": "c-admin@example.com",
            "display_name": "丙管理员",
            "password": PASSWORD,
            "role": "org_admin",
            "org_id": org_id,
        },
    )
    assert u.status_code == 201


async def test_role_org_rules_enforced(client, seed):
    token = await login(client, "admin", "admin@example.com")
    no_org = await client.post(
        "/api/admin/users",
        headers=bearer(token),
        json={
            "email": "x1@example.com",
            "display_name": "x",
            "password": PASSWORD,
            "role": "member",
        },
    )
    admin_with_org = await client.post(
        "/api/admin/users",
        headers=bearer(token),
        json={
            "email": "x2@example.com",
            "display_name": "x",
            "password": PASSWORD,
            "role": "reviewer",
            "org_id": str(seed["org_a"].id),
        },
    )
    assert no_org.status_code == 422 and admin_with_org.status_code == 422


async def test_short_password_rejected(client, seed):
    token = await login(client, "admin", "admin@example.com")
    r = await client.post(
        "/api/admin/users",
        headers=bearer(token),
        json={
            "email": "short@example.com",
            "display_name": "x",
            "password": "short",
            "role": "reviewer",
        },
    )
    assert r.status_code == 422


async def test_disabled_user_and_org_blocked(client, seed, session_factory):
    token = await login(client, "user", "a-member@example.com")
    async with session_factory() as s:
        user = (
            await s.execute(select(User).where(User.email == "a-member@example.com"))
        ).scalar_one()
        user.is_active = False
        await s.commit()
    # 已发出的令牌立即失效
    assert (await client.get("/api/user/me", headers=bearer(token))).status_code == 401
    r = await client.post(
        "/api/auth/user/login", json={"email": "a-member@example.com", "password": PASSWORD}
    )
    assert r.status_code == 401

    admin_token = await login(client, "user", "a-admin@example.com")
    async with session_factory() as s:
        org = await s.get(type(seed["org_a"]), seed["org_a"].id)
        org.is_active = False
        await s.commit()
    assert (await client.get("/api/user/me", headers=bearer(admin_token))).status_code == 401


async def test_password_not_stored_plain(session_factory, seed):
    async with session_factory() as s:
        user = (await s.execute(select(User).where(User.email == "admin@example.com"))).scalar_one()
        assert PASSWORD not in user.password_hash
        assert user.password_hash.startswith("$argon2")

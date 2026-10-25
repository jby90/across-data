"""Operator-only runtime role bootstrap; credentials enter through environment, never files."""

import base64
import hashlib
import hmac
import re
import secrets

from sqlalchemy import text


def scram_verifier(password):
    salt = secrets.token_bytes(16)
    iterations = 4096
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client = hmac.digest(salted, b"Client Key", "sha256")
    stored = hashlib.sha256(client).digest()
    server = hmac.digest(salted, b"Server Key", "sha256")
    b64 = lambda value: base64.b64encode(value).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored)}:{b64(server)}"


async def provision_runtime_role(engine, name, *, password=None, local_trust_only=False):
    if not re.fullmatch(r"cyshuhe_[a-z0-9_]{1,50}", name) or name == "cyshuhe_app":
        raise ValueError("runtime role name must be a dedicated cyshuhe_ login")
    if password is None:
        if not local_trust_only or engine.url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "runtime database password is required outside isolated loopback development"
            )
    elif not re.fullmatch(r"[A-Za-z0-9_-]{32,}", password):
        raise ValueError("runtime database password must contain at least 32 URL-safe characters")
    async with engine.begin() as conn:
        existing = (
            (
                await conn.execute(
                    text("""SELECT rolname,rolsuper,rolbypassrls,rolcreaterole,
            rolcreatedb,rolreplication FROM pg_roles WHERE rolname=:name"""),
                    {"name": name},
                )
            )
            .mappings()
            .one_or_none()
        )
        owned = await conn.scalar(
            text("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner=:name"),
            {"name": name},
        )
        inherited = False
        if existing:
            inherited = await conn.scalar(
                text("""SELECT EXISTS(SELECT FROM pg_roles r
                WHERE pg_has_role(:name,r.oid,'MEMBER') AND (r.rolsuper OR r.rolbypassrls
                OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication
                OR EXISTS(SELECT FROM pg_tables t
                WHERE t.schemaname='public' AND t.tableowner=r.rolname)))"""),
                {"name": name},
            )
        if (
            inherited
            or owned
            or (
                existing
                and any(
                    existing[key]
                    for key in (
                        "rolsuper",
                        "rolbypassrls",
                        "rolcreaterole",
                        "rolcreatedb",
                        "rolreplication",
                    )
                )
            )
        ):
            raise ValueError("refusing to reuse a privileged or owning database role")
        if existing is None:
            await conn.execute(
                text(
                    f'CREATE ROLE "{name}" LOGIN NOSUPERUSER NOBYPASSRLS '
                    "NOCREATEDB NOCREATEROLE NOREPLICATION"
                )
            )
        if password is not None:
            # Send a salted SCRAM verifier, never the plaintext password in DDL/log output.
            verifier = scram_verifier(password)
            await conn.exec_driver_sql(f"ALTER ROLE \"{name}\" LOGIN PASSWORD '{verifier}'")
        else:
            await conn.exec_driver_sql(f'ALTER ROLE "{name}" LOGIN')
        await conn.execute(text(f'GRANT cyshuhe_app TO "{name}"'))
    return engine.url.set(username=name, password=password)

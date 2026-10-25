"""Use only in an operator/migration container, never as an HTTP endpoint."""

import argparse
import asyncio
import os

from app.core.db import get_engine
from app.core.rls import system_scope
from app.core.runtime_roles import provision_runtime_role


async def main(name):
    password = os.environ.get("CYSH_RUNTIME_DB_PASSWORD")
    if not password:
        raise ValueError("CYSH_RUNTIME_DB_PASSWORD must be supplied through environment")
    await provision_runtime_role(get_engine(), name, password=password)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="cyshuhe_runtime")
    args = parser.parse_args()
    try:
        with system_scope("explicit runtime role provisioning"):
            asyncio.run(main(args.name))
    except Exception:
        raise SystemExit(
            "Runtime role provisioning failed; check operator permissions and environment. "
            "No credential was printed."
        ) from None
    print("Runtime database login provisioned; no credential printed.")

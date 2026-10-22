"""命令行工具。

用法:
  python -m app.cli create-platform-admin --email admin@example.com --name 管理员
密码从环境变量 CYSH_ADMIN_PASSWORD 读取,未设置则交互输入(不回显)。
"""

import argparse
import asyncio
import getpass
import os
import sys

from app.core.db import get_sessionmaker
from app.iam import service
from app.iam.models import Role


async def _create_platform_admin(email: str, name: str, password: str) -> None:
    async with get_sessionmaker()() as session:
        user = await service.create_user(
            session,
            email=email,
            display_name=name,
            password=password,
            role=Role.PLATFORM_ADMIN,
            org_id=None,
        )
        print(f"已创建平台管理员: {user.email}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-platform-admin", help="创建平台管理员")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="平台管理员")
    args = parser.parse_args()

    if args.command == "create-platform-admin":
        password = os.environ.get("CYSH_ADMIN_PASSWORD") or getpass.getpass("密码(至少12位): ")
        if len(password) < 12:
            print("密码至少 12 位", file=sys.stderr)
            return 2
        asyncio.run(_create_platform_admin(args.email, args.name, password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

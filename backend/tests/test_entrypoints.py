import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", ["app.worker", "app.cli", "app.main"])
def test_every_entrypoint_can_resolve_all_foreign_keys(module):
    """回归:worker 曾因没导入 users 模型而在运行时报外键无法解析。
    每个入口在一个全新的 Python 进程里导入后,都必须能完成全部映射配置与建表语句生成。"""
    code = (
        f"import {module}\n"
        "from sqlalchemy.orm import configure_mappers\n"
        "from sqlalchemy.schema import CreateTable\n"
        "from sqlalchemy.dialects import postgresql\n"
        "from app.core.db import Base\n"
        "configure_mappers()\n"
        "for t in Base.metadata.sorted_tables:\n"
        "    str(CreateTable(t).compile(dialect=postgresql.dialect()))\n"
        "assert {'users','source_sites','documents','document_versions','clauses'} <= set(Base.metadata.tables)\n"
    )
    env = dict(os.environ, CYSH_JWT_SECRET="x" * 40)
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr

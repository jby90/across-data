"""Root/operator helper: reuse existing container environments without .env files.

For upgrades of this deployment only. Initial provisioning is performed separately
with secrets supplied through the caller environment. Never exposes an HTTP API.
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def container_env(name):
    item = json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]
    return dict(v.split("=", 1) for v in item["Config"]["Env"] if "=" in v)


def main():
    args = sys.argv[1:]
    tls = "--tls" in args
    args = [arg for arg in args if arg != "--tls"]
    if not args or args[0] not in {"up", "stop", "start", "ps", "restart", "logs", "config"}:
        raise SystemExit("Allowed: [--tls] up|stop|start|ps|restart|logs|config --quiet")
    if args[0] == "config" and args != ["config", "--quiet"]:
        raise SystemExit("Only config --quiet is allowed: rendered config contains secrets")
    db = container_env("cyshuhe-db-1")
    migration = container_env("cyshuhe-migrate-1")
    api = container_env("cyshuhe-api-1")
    env = dict(os.environ)
    root = Path(__file__).resolve().parent
    release_file = root / "release.json"
    release = json.loads(release_file.read_text()) if release_file.exists() else {}
    env.update({key: value for key, value in api.items() if key.startswith("CYSH_")})
    env.update(
        POSTGRES_PASSWORD=db["POSTGRES_PASSWORD"],
        CYSH_RUNTIME_DB_PASSWORD=migration["CYSH_RUNTIME_DB_PASSWORD"],
        CYSH_JWT_SECRET=migration["CYSH_JWT_SECRET"],
        CYSH_RELEASE=os.environ.get("CYSH_RELEASE", release.get("release_tag", "s12-stage")),
    )
    command = ["docker", "compose", "-f", str(root / "docker-compose.yml")]
    if tls:
        command += ["-f", str(root / "docker-compose.tls.yml")]
    # No shell and no credential-bearing command arguments.
    raise SystemExit(subprocess.call(command + args, env=env))


if __name__ == "__main__":
    main()

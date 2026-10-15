"""Run the SDK's pinned CLI with the same CODEX_HOME as the HTTP service."""

import os
import subprocess
import sys

from codex_cli_bin import bundled_codex_path

from .config import AUTH_OVERRIDES, Settings


def main():
    settings = Settings()
    settings.codex_home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CODEX_HOME": str(settings.codex_home.resolve())}
    binary = settings.codex_bin or str(bundled_codex_path())
    args = [binary]
    for override in AUTH_OVERRIDES:
        args.extend(["--config", override])
    result = subprocess.run([*args, *sys.argv[1:]], env=env, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

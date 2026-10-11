import subprocess
import sys


def test_env_has_no_package_managers(env):
    # A test that forgot --root must not be able to reach brew/uv/pnpm/npm/ollama caches.
    code = "import shutil;print([t for t in ('brew','uv','pnpm','npm','ollama') if shutil.which(t)])"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True).stdout.strip()
    assert out == "[]"

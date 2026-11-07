#!/usr/bin/env python3
# run_tests.py - uv-powered multi-version test runner
"""
Runs the test script under several Python versions using uv.
Dependencies come from pyproject.toml / uv.lock (honored strictly).
Each Python version gets its own env in .uv_envs/ so your dev .venv is untouched.

Args are passed through to the test script.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)


PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
TEST_SCRIPT = "tests/test.py"
TEST_ARGS = []
ENVS_DIR = Path(current_dir) / ".uv_envs"

def env_path(py_ver):
    return ENVS_DIR / f"py{py_ver.replace('.', '')}"

def main(*shell_args):
    failures = 0
    for py_ver in PYTHON_VERSIONS:
        print(f"Running tests on Python {py_ver} via uv...")
        # Separate env per version; uv.lock is respected and must be current.
        os.environ["UV_PROJECT_ENVIRONMENT"] = str(env_path(py_ver))

        cmd = ["uv", "run", "--locked", "--project", current_dir,
               "--python", py_ver,
               "python", os.path.normpath(TEST_SCRIPT), *shell_args, *TEST_ARGS]

        result = subprocess.run(cmd,capture_output=True,text=True,shell=False)
        sys.stdout.write(result.stdout)

        if result.returncode != 0:
            failures += 1

    print("All tests passed!" if failures == 0 else "Some test(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))

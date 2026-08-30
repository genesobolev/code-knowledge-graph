from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def commit_all(repository: Path, message: str) -> None:
    git(repository, "add", ".")
    git(repository, "commit", "-m", message)


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "sample-repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.email", "fixture@example.test")
    git(repository, "config", "user.name", "Fixture Author")
    git(
        repository,
        "remote",
        "add",
        "origin",
        "https://fixture-user:secret-token@example.test/acme/sample.git?token=secret",
    )

    files = {
        "pyproject.toml": '[project]\nname = "sample"\nversion = "0.1.0"\n',
        "src/sample/__init__.py": "from .service import Worker, execute\n",
        "src/sample/base.py": "class Base:\n    pass\n",
        "src/sample/service.py": (
            "from .base import Base\n\n"
            "def helper(value: str) -> str:\n"
            "    return value.upper()\n\n"
            "class Worker(Base):\n"
            "    def run(self, value: str) -> str:\n"
            "        return helper(value)\n\n"
            "def execute(value: str) -> str:\n"
            "    worker = Worker()\n"
            "    return worker.run(helper(value))\n"
        ),
        "tests/test_service.py": (
            "from sample.service import execute\n\n"
            "def test_execute() -> None:\n"
            "    assert execute('graph') == 'GRAPH'\n"
        ),
        "web/app.js": "export const graphTitle = 'Sample graph';\n",
    }
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    commit_all(repository, "Initial sample")

    service = repository / "src/sample/service.py"
    test = repository / "tests/test_service.py"
    service.write_text(service.read_text(encoding="utf-8") + "\n# revision one\n", encoding="utf-8")
    test.write_text(test.read_text(encoding="utf-8") + "\n# revision one\n", encoding="utf-8")
    commit_all(repository, "Revise service and test")
    service.write_text(service.read_text(encoding="utf-8") + "# revision two\n", encoding="utf-8")
    test.write_text(test.read_text(encoding="utf-8") + "# revision two\n", encoding="utf-8")
    commit_all(repository, "Revise service and test again")
    return repository

import tomllib
from pathlib import Path


def test_dev_group_includes_pytest_requirements() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    dev_deps = data["project"]["optional-dependencies"]["dev"]

    assert "pytest>=8.2,<9" in dev_deps
    assert "pytest-asyncio>=0.24,<1" in dev_deps

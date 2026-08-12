import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PYTHON_CLASSIFIERS = {
    f"Programming Language :: Python :: {version}" for version in ("3.11", "3.12", "3.13", "3.14")
}


def test_package_declares_the_verified_python_range() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["requires-python"] == ">=3.11,<3.15"
    assert set(project["classifiers"]) >= SUPPORTED_PYTHON_CLASSIFIERS


def test_package_includes_the_declared_mit_license() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert license_text.startswith("MIT License\n\nCopyright (c) 2026 Misty02600\n")


def test_model_provider_stacks_are_isolated_to_their_optional_extras() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]

    assert all(
        not dependency.startswith(("anthropic", "openai", "pydantic-ai"))
        for dependency in project["dependencies"]
    )
    assert project["optional-dependencies"]["model-anthropic"] == [
        "anthropic==0.121.0",
        "pydantic-ai-slim[anthropic]==2.27.0",
    ]
    assert project["optional-dependencies"]["model-openai"] == [
        "openai==2.53.0",
        "pydantic-ai-slim[openai]==2.27.0",
    ]
    assert "model-deepseek" not in project["optional-dependencies"]
    assert "model-opencode-go" not in project["optional-dependencies"]
    assert "anthropic==0.121.0" in metadata["dependency-groups"]["dev"]
    assert "openai==2.53.0" in metadata["dependency-groups"]["dev"]
    assert "pydantic-ai-slim[anthropic,openai]==2.27.0" in metadata["dependency-groups"]["dev"]


def test_localstore_is_a_bounded_base_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "nonebot-plugin-localstore>=0.7.4,<0.8" in project["dependencies"]


def test_mlflow_tracking_is_a_maintainer_only_dependency() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]

    assert all(not dependency.startswith("mlflow") for dependency in project["dependencies"])
    assert "tracking-mlflow" not in project["optional-dependencies"]
    assert metadata["dependency-groups"]["maintainer"] == [
        "mlflow==3.14.0",
        "openai==2.53.0",
        "pydantic-ai-slim[openai]==2.27.0",
    ]


def test_distribution_has_no_maintainer_console_script() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "scripts" not in project


def test_source_distribution_excludes_local_and_machine_generated_state() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = set(metadata["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"])

    assert {
        "/.code-notes",
        "/.learning",
        "/.tours/personal",
        "/AGENTS.md",
        "/artifacts",
        "/data",
        "/docs/evaluations",
        "/docs/operations",
        "/docs/oracle-runs",
        "/docs/scratch",
        "/evals/snapshots",
        "/logs",
        "/mlartifacts",
        "/mlruns",
        "/reports",
        "/tools",
    } <= excluded

import subprocess
import tarfile
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


def test_model_provider_stacks_reuse_pydantic_ai_optional_extras() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]

    assert all(
        not dependency.startswith(("anthropic", "openai", "pydantic-ai"))
        for dependency in project["dependencies"]
    )
    assert project["optional-dependencies"]["anthropic"] == [
        "pydantic-ai-slim[anthropic]==2.27.0",
    ]
    assert project["optional-dependencies"]["openai"] == [
        "pydantic-ai-slim[openai]==2.27.0",
    ]
    assert {
        "model-anthropic",
        "model-openai",
        "model-opencode-go",
        "model-deepseek",
        "opencode-go",
    }.isdisjoint(project["optional-dependencies"])
    assert "anthropic==0.121.0" not in metadata["dependency-groups"]["dev"]
    assert "openai==2.53.0" not in metadata["dependency-groups"]["dev"]
    assert "openai==2.53.0" not in metadata["dependency-groups"]["maintainer"]
    assert "pydantic-ai-slim[anthropic,openai]==2.27.0" in metadata["dependency-groups"]["dev"]


def test_localstore_is_a_bounded_base_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "nonebot-plugin-localstore>=0.7.4,<0.8" in project["dependencies"]


def test_mlflow_tracking_is_a_maintainer_only_dependency() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    maintainer_dependencies = metadata["dependency-groups"]["maintainer"]

    assert all(not dependency.startswith("mlflow") for dependency in project["dependencies"])
    assert "tracking-mlflow" not in project["optional-dependencies"]
    assert "mlflow==3.14.0" in maintainer_dependencies


def test_distribution_has_no_maintainer_console_script() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "scripts" not in project


def test_build_backend_matches_the_nonebot_plugin_template() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["build-system"] == {
        "requires": ["uv_build>=0.10.0,<0.12.0"],
        "build-backend": "uv_build",
    }
    assert metadata["tool"]["uv"]["build-backend"] == {
        "module-name": ["nbtriage", "nonebot_plugin_triage"]
    }
    assert "hatch" not in metadata["tool"]


def test_source_distribution_excludes_local_and_machine_generated_state(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        ("uv", "build", "--no-sources", "--out-dir", str(tmp_path)),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    sdists = list(tmp_path.glob("*.tar.gz"))
    assert len(sdists) == 1

    with tarfile.open(sdists[0], "r:gz") as archive:
        names = archive.getnames()

    forbidden_parts = {
        ".code-notes",
        ".learning",
        ".tours",
        "artifacts",
        "data",
        "logs",
        "mlartifacts",
        "mlruns",
        "reports",
    }
    assert not any(forbidden_parts & set(Path(name).parts) for name in names)
    assert not any(part.startswith(".pytest") for name in names for part in Path(name).parts)

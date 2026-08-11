from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

MAINTAINER_MODULE_FILES = {
    "__main__.py",
    "alconna_capabilities.py",
    "agent_evaluation.py",
    "answer_quality_evaluation.py",
    "answer_review_export.py",
    "bot_docs.py",
    "bot_docs_evaluation.py",
    "cli.py",
    "collector.py",
    "curation.py",
    "discovery.py",
    "deepseek_adapter.py",
    "evaluation.py",
    "evidence_policy.py",
    "evidence_policy_evaluation.py",
    "evidence_receipt_evaluation.py",
    "gate.py",
    "github.py",
    "mlflow_tracking.py",
    "models.py",
    "providers.py",
    "runtime_results.py",
    "safety_evaluation.py",
    "sessions.py",
    "timeline.py",
}
LOCAL_ROOTS = {
    "artifacts",
    "data",
    "logs",
    "mlartifacts",
    "mlruns",
    "reports",
    "tools",
}


def _members(path: Path) -> list[PurePosixPath]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return [PurePosixPath(name) for name in archive.namelist()]
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return [PurePosixPath(name) for name in archive.getnames()]
    raise ValueError(f"unsupported distribution archive: {path}")


def _project_relative(path: PurePosixPath, *, source_distribution: bool) -> PurePosixPath:
    if source_distribution and len(path.parts) > 1:
        return PurePosixPath(*path.parts[1:])
    return path


def verify(path: Path) -> None:
    source_distribution = path.name.endswith(".tar.gz")
    violations: list[str] = []
    for member in _members(path):
        relative = _project_relative(member, source_distribution=source_distribution)
        if not relative.parts:
            continue
        if relative.parts[0] in LOCAL_ROOTS:
            violations.append(str(relative))
            continue
        if relative.parts[:2] == ("evals", "snapshots"):
            violations.append(str(relative))
            continue
        if (
            len(relative.parts) == 3
            and relative.parts[:2] == ("src", "nbtriage")
            and relative.name in MAINTAINER_MODULE_FILES
        ):
            violations.append(str(relative))
        if (
            not source_distribution
            and len(relative.parts) == 2
            and relative.parts[0] == "nbtriage"
            and relative.name in MAINTAINER_MODULE_FILES
        ):
            violations.append(str(relative))
    if violations:
        formatted = "\n".join(f"- {item}" for item in violations)
        raise RuntimeError(f"{path} contains excluded maintainer or local files:\n{formatted}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: verify_distribution_contents.py DIST [DIST ...]")
    for argument in sys.argv[1:]:
        verify(Path(argument))

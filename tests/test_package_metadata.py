import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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

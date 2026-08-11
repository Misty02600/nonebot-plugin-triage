import asyncio
import json
import os
from pathlib import Path

PACKAGE = "typing-extensions==4.15.0"


async def run_buggy_path(project: Path) -> dict[str, object]:
    from nb_cli.handlers import call_pip_install

    python_path = os.environ.get("NBTRIAGE_PROJECT_PYTHON")
    if not python_path:
        python_path = str(project / ".venv" / "Scripts" / "python.exe")
    try:
        process = await call_pip_install(PACKAGE, python_path=python_path)
        returncode = await process.wait()
    except Exception as error:
        return {"manager": "pip", "raised": type(error).__name__, "message": str(error)}
    return {"manager": "pip", "raised": None, "returncode": returncode}


async def run_fixed_path(project: Path) -> dict[str, object]:
    from nb_cli.handlers.environment import EnvironmentExecutor, probe_environment_manager
    from packaging.requirements import Requirement

    inferred, available = await probe_environment_manager(cwd=project)
    executor = await EnvironmentExecutor.get(cwd=project)
    try:
        await executor.install(Requirement(PACKAGE))
    except Exception as error:
        return {
            "inferred": inferred,
            "available": available,
            "executor": type(executor).__name__,
            "raised": type(error).__name__,
            "message": str(error),
        }
    return {
        "inferred": inferred,
        "available": available,
        "executor": type(executor).__name__,
        "raised": None,
        "dependency_recorded": "typing-extensions" in (project / "pyproject.toml").read_text(),
    }


async def main() -> None:
    project = Path.cwd()
    try:
        import nb_cli.handlers.environment  # noqa: F401
    except ImportError:
        result = await run_buggy_path(project)
    else:
        result = await run_fixed_path(project)
    print(json.dumps(result, sort_keys=True))


asyncio.run(main())

import json
import os
from pathlib import Path

from nb_cli.handlers.reloader import FileFilter

fixture = Path(os.environ["NBTRIAGE_ORACLE_FIXTURE"])
os.chdir(fixture)
changed = (fixture / "packages" / "ndice" / "src" / "ndice.py").resolve()
accepted = FileFilter(excludes=["packages/"])(changed)
print(
    json.dumps(
        {
            "changed": str(changed),
            "accepted_for_reload": accepted,
            "oracle": "buggy" if accepted else "fixed",
        }
    )
)

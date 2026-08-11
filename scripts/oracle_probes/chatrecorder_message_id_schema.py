import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace


class ColumnType:
    def __init__(self, length=None) -> None:
        self.length = length


class Mapped:
    def __class_getitem__(cls, item):
        return cls


def mapped_column(column_type=None, **kwargs):
    return column_type if column_type is not None else ColumnType()


class Model:
    pass


def load_model_length(source_path: Path) -> int:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MessageRecord"
    )
    future = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations")],
        level=0,
    )
    namespace = {
        "Model": Model,
        "Mapped": Mapped,
        "mapped_column": mapped_column,
        "String": ColumnType,
        "JSON": ColumnType(),
        "TEXT": ColumnType(),
        "JsonMsg": object,
        "datetime": object,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[future, class_node], type_ignores=[])),
            str(source_path),
            "exec",
        ),
        namespace,
    )
    return namespace["MessageRecord"].message_id.length


def load_migration_target(source_path: Path | None) -> dict | None:
    if source_path is None:
        return None
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    future = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations")],
        level=0,
    )
    captured = {}

    class BatchOperation:
        def alter_column(self, name, **kwargs) -> None:
            captured["column"] = name
            captured["existing_length"] = kwargs["existing_type"].length
            captured["target_length"] = kwargs["type_"].length

    class BatchContext:
        def __enter__(self):
            return BatchOperation()

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    class Operation:
        @staticmethod
        def batch_alter_table(name, schema=None):
            captured["table"] = name
            return BatchContext()

    namespace = {
        "op": Operation(),
        "sa": SimpleNamespace(String=ColumnType),
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[future, function], type_ignores=[])),
            str(source_path),
            "exec",
        ),
        namespace,
    )
    namespace["upgrade"]()
    return captured


model_path = Path(sys.argv[1])
migration_path = None if len(sys.argv) < 3 or sys.argv[2] == "-" else Path(sys.argv[2])
length = load_model_length(model_path)
sample_id = (
    "ROBOT1.0_rUDJx-lKBnLbQ9LKR.EBEGLL5orVH4c-92s1jT6J54vuKvEU6jDzor252NbV1j"
    "j1HKtt2wtcgrXfUjdYBaxbRstG81ovPjw88HwjHppK6Gc!"
)

print(
    json.dumps(
        {
            "model_length": length,
            "sample_id_length": len(sample_id),
            "model_accepts_sample": length >= len(sample_id),
            "migration": load_migration_target(migration_path),
        }
    )
)

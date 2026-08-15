from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from pydantic import BaseModel

from nonebot_plugin_triage.config_policy import ConfigValuePolicy
from nonebot_plugin_triage.runtime_config_evidence import (
    RuntimeConfigEvidenceReader,
    RuntimeConfigOmission,
    RuntimeConfigOmissionReason,
    RuntimeConfigReference,
    RuntimeConfigValueEvidence,
    runtime_config_reference_id,
)


def _loaded_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> ModuleType:
    module_name = f"runtime_config_plugin_{uuid4().hex}"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(source, encoding="utf-8")
    module = ModuleType(module_name)
    module.__file__ = str(module_path)
    exec(compile(source, str(module_path), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module


def _reference(
    module: ModuleType,
    *,
    field_name: str = "cooldown",
    config_key: str = "COOLDOWN",
    config_type: str | None = None,
    source_revision: str | None = None,
) -> RuntimeConfigReference:
    binding = "plugin_config"
    config = vars(module)[binding]
    assert isinstance(config, BaseModel)
    source_path = vars(module)["__file__"]
    assert isinstance(source_path, str)
    source = Path(source_path).read_text(encoding="utf-8")
    return RuntimeConfigReference(
        reference_id=runtime_config_reference_id(module.__name__, binding, field_name),
        module=module.__name__,
        binding=binding,
        field_name=field_name,
        config_key=config_key,
        config_type=config_type or f"{type(config).__module__}:{type(config).__qualname__}",
        source_revision=source_revision
        or f"sha256:{hashlib.sha256(source.encode('utf-8')).hexdigest()}",
    )


def test_reads_only_an_approved_constructed_model_without_serializing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel

class Config(BaseModel):
    cooldown: int = 30

    @property
    def unsafe_property(self):
        raise AssertionError("properties must not run")

plugin_config = Config()
""",
    )
    reference = _reference(module)
    config = vars(module)["plugin_config"]

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("model serialization must not run")

    monkeypatch.setattr(type(config), "model_dump", fail)
    reader = RuntimeConfigEvidenceReader(
        owner_module=module.__name__,
        references=(reference,),
        policy=ConfigValuePolicy(),
    )

    result = reader.read(reference.reference_id)

    assert isinstance(result, RuntimeConfigValueEvidence)
    assert result.value == 30
    assert "value=30" not in repr(result)
    assert "plugin_config" not in repr(result)


def test_unknown_reference_id_never_expands_the_approved_memory_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel
class Config(BaseModel):
    cooldown: int = 30
    hidden: str = "must-not-be-read"
plugin_config = Config()
""",
    )
    reference = _reference(module)
    reader = RuntimeConfigEvidenceReader(
        owner_module=module.__name__,
        references=(reference,),
        policy=ConfigValuePolicy(),
    )
    unknown_id = runtime_config_reference_id(module.__name__, "plugin_config", "hidden")

    result = reader.read(unknown_id)

    assert result == RuntimeConfigOmission(
        unknown_id,
        RuntimeConfigOmissionReason.REFERENCE_NOT_APPROVED,
    )
    assert "must-not-be-read" not in repr(result)


@pytest.mark.parametrize(
    ("reference_changes", "expected"),
    [
        (
            {"source_revision": "sha256:" + "0" * 64},
            RuntimeConfigOmissionReason.SOURCE_REVISION_MISMATCH,
        ),
        (
            {"config_type": "some_plugin.config:OtherConfig"},
            RuntimeConfigOmissionReason.CONFIG_TYPE_MISMATCH,
        ),
        (
            {"config_key": "UNRELATED_KEY"},
            RuntimeConfigOmissionReason.CONFIG_KEY_MISMATCH,
        ),
    ],
)
def test_revision_type_and_alias_drift_fail_with_value_free_omissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference_changes: dict[str, str],
    expected: RuntimeConfigOmissionReason,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel, Field
class Config(BaseModel):
    cooldown: int = Field(default=30, validation_alias="COOLDOWN")
plugin_config = Config()
""",
    )
    reference = _reference(module, **reference_changes)
    reader = RuntimeConfigEvidenceReader(
        owner_module=module.__name__,
        references=(reference,),
        policy=ConfigValuePolicy(),
    )

    result = reader.read(reference.reference_id)

    assert isinstance(result, RuntimeConfigOmission)
    assert result.reason is expected
    assert "value=30" not in repr(result)


def test_policy_and_owner_boundary_fail_closed_without_exposing_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel
class Config(BaseModel):
    cooldown: str = "owner-runtime-value"
plugin_config = Config()
""",
    )
    outside = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel
class Config(BaseModel):
    cooldown: str = "outside-runtime-value"
plugin_config = Config()
""",
    )
    owner_reference = _reference(owner)
    outside_reference = _reference(outside)
    reader = RuntimeConfigEvidenceReader(
        owner_module=owner.__name__,
        references=(owner_reference, outside_reference),
        policy=ConfigValuePolicy.from_keys(["COOLDOWN"]),
    )

    restricted = reader.read(owner_reference.reference_id)
    denied_owner = reader.read(outside_reference.reference_id)

    assert isinstance(restricted, RuntimeConfigOmission)
    assert restricted.reason is RuntimeConfigOmissionReason.RESTRICTED
    assert isinstance(denied_owner, RuntimeConfigOmission)
    assert denied_owner.reason is RuntimeConfigOmissionReason.MODULE_NOT_ALLOWED
    rendered = repr((restricted, denied_owner, reader))
    assert "owner-runtime-value" not in rendered
    assert "outside-runtime-value" not in rendered

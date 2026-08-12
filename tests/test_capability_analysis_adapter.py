from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from pydantic import BaseModel

from nbtriage.capabilities import (
    CapabilityRecord,
    Claim,
    ClaimBasis,
    Constraint,
    ConstraintEvaluability,
    Disclosure,
    EvidenceRef,
    RecordState,
)
from nonebot_plugin_triage.capability_analysis_adapter import (
    AnalysisSourcePolicy,
    CapabilityAnalysisAdapterError,
    build_capability_analysis_request,
)
from nonebot_plugin_triage.config_policy import ConfigValuePolicy


def _loaded_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> ModuleType:
    module_name = f"analysis_plugin_{uuid4().hex}"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(source, encoding="utf-8")
    module = ModuleType(module_name)
    module.__file__ = str(module_path)
    exec(compile(source, str(module_path), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module


def _record(
    module_name: str,
    *,
    handlers: list[dict[str, object]],
    config_references: list[dict[str, object]],
    owner: str | None = None,
    plugin_module_name: str | None = None,
    disclosure: Disclosure = Disclosure.PUBLIC,
    superuser_only: bool = False,
) -> CapabilityRecord:
    plugin_evidence_id = "evidence:plugin"
    matcher_evidence_id = "evidence:matcher"
    claims = [
        Claim(
            "plugin.module_name",
            plugin_module_name or module_name,
            ClaimBasis.OBSERVED,
            (plugin_evidence_id,),
        ),
        Claim(
            "handler.references",
            handlers,
            ClaimBasis.OBSERVED,
            (matcher_evidence_id,),
        ),
    ]
    if config_references:
        claims.append(
            Claim(
                "config.references",
                config_references,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    return CapabilityRecord(
        capability_id="capability:test",
        owner=owner or module_name,
        kind="command",
        disclosure=disclosure,
        state=RecordState.CANDIDATE,
        claims=tuple(claims),
        constraints=(
            Constraint(
                constraint_id="constraint:superuser",
                kind="permission",
                operation="superuser",
                evaluability=ConstraintEvaluability.STRUCTURED,
                evidence_ids=(matcher_evidence_id,),
            ),
        )
        if superuser_only
        else (),
        evidence_refs=(
            EvidenceRef(
                evidence_id=plugin_evidence_id,
                source_id="source:plugin",
                kind="plugin_source",
                locator="plugin://test/root",
            ),
            EvidenceRef(
                evidence_id=matcher_evidence_id,
                source_id="source:runtime",
                kind="matcher_source",
                locator="plugin://test",
            ),
        ),
    )


def _source_revision(module: ModuleType) -> str:
    source_path = module.__dict__["__file__"]
    assert isinstance(source_path, str)
    content = Path(source_path).read_text(encoding="utf-8")
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _config_reference(
    module: ModuleType,
    *,
    field: str,
    key: str,
    function: str,
    line: int,
    helper_depth: int,
) -> dict[str, object]:
    config = module.__dict__["plugin_config"]
    assert isinstance(config, BaseModel)
    return {
        "module": module.__name__,
        "binding": "plugin_config",
        "field": field,
        "key": key,
        "function": function,
        "line": line,
        "helper_depth": helper_depth,
        "source_revision": _source_revision(module),
        "config_type": f"{type(config).__module__}:{type(config).__qualname__}",
    }


def _handler_reference(
    module: ModuleType,
    function: str,
    line: int,
) -> dict[str, object]:
    return {
        "module": module.__name__,
        "function": function,
        "line": line,
        "source_revision": _source_revision(module),
    }


def test_builds_bounded_request_from_loaded_handler_and_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel

class Config(BaseModel):
    search_enabled: bool = True
    result_limit: int = 3

plugin_config = Config()

def build_limit():
    return plugin_config.result_limit

async def handle_search():
    if plugin_config.search_enabled:
        return build_limit()
    return None
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle_search", 12)],
            config_references=[
                _config_reference(
                    module,
                    field="search_enabled",
                    key="SEARCH_ENABLED",
                    function="handle_search",
                    line=13,
                    helper_depth=0,
                ),
                _config_reference(
                    module,
                    field="result_limit",
                    key="RESULT_LIMIT",
                    function="build_limit",
                    line=9,
                    helper_depth=1,
                ),
            ],
        ),
        ConfigValuePolicy(),
    )

    assert request.capability.owner == module.__name__
    assert {unit.content.splitlines()[0] for unit in request.evidence_units} == {
        "def build_limit():",
        "async def handle_search():",
    }
    assert all(str(tmp_path) not in (unit.locator or "") for unit in request.evidence_units)
    assert all(
        unit.locator and unit.locator.startswith(module.__name__) for unit in request.evidence_units
    )
    assert {(type(item.value), item.value) for item in request.config_projections} == {
        (bool, True),
        (int, 3),
    }
    assert request.unknown_config == ()


def test_restricted_missing_and_opaque_values_become_hashed_unknown_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel, SecretStr

class Config(BaseModel):
    token: str = "raw-private-token"
    secret: SecretStr = SecretStr("opaque-private-token")
    absent: str = "remove-before-projection"

plugin_config = Config()

async def handle():
    return plugin_config.token, plugin_config.secret
""",
    )
    del module.__dict__["plugin_config"].__dict__["token"]
    del module.__dict__["plugin_config"].__dict__["absent"]
    references = [
        _config_reference(
            module,
            field=field,
            key=key,
            function="handle",
            line=10,
            helper_depth=0,
        )
        for field, key in (
            ("token", "TOKEN"),
            ("secret", "SECRET"),
            ("absent", "ABSENT"),
        )
    ]
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 9)],
            config_references=references,
        ),
        ConfigValuePolicy.from_keys(["TOKEN"]),
    )

    assert request.config_projections == ()
    assert {item.reason for item in request.unknown_config} == {
        "restricted",
        "opaque",
        "missing",
    }
    rendered = repr(request)
    assert "raw-private-token" not in rendered
    assert "opaque-private-token" not in rendered
    assert "SECRET" not in rendered
    assert "ABSENT" not in rendered
    assert all(item.reference_id.startswith("config:") for item in request.unknown_config)


def test_does_not_import_unloaded_or_read_modules_outside_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
async def handle():
    return True
""",
    )

    with pytest.raises(CapabilityAnalysisAdapterError, match="root module is not loaded"):
        build_capability_analysis_request(
            _record(
                module.__name__,
                owner="different_plugin",
                plugin_module_name="different_plugin",
                handlers=[_handler_reference(module, "handle", 1)],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )

    unloaded = f"unloaded_plugin_{uuid4().hex}"
    assert unloaded not in sys.modules
    with pytest.raises(CapabilityAnalysisAdapterError, match="root module is not loaded"):
        build_capability_analysis_request(
            _record(
                unloaded,
                handlers=[
                    {
                        "module": unloaded,
                        "function": "handle",
                        "line": 1,
                        "source_revision": "sha256:" + "0" * 64,
                    }
                ],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )
    assert unloaded not in sys.modules


def test_rejects_oversized_function_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        "async def handle():\n    return " + repr("x" * 8_100) + "\n",
    )

    with pytest.raises(CapabilityAnalysisAdapterError, match="no readable bounded"):
        build_capability_analysis_request(
            _record(
                module.__name__,
                handlers=[_handler_reference(module, "handle", 1)],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )


def test_request_never_reconstructs_or_serializes_config_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel

class Config(BaseModel):
    enabled: bool = True

plugin_config = Config()

async def handle():
    return plugin_config.enabled
""",
    )
    config = module.__dict__["plugin_config"]
    assert isinstance(config, BaseModel)

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("configuration serialization must not run")

    monkeypatch.setattr(type(config), "model_dump", fail)
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 8)],
            config_references=[
                _config_reference(
                    module,
                    field="enabled",
                    key="ENABLED",
                    function="handle",
                    line=9,
                    helper_depth=0,
                )
            ],
        ),
        ConfigValuePolicy(),
    )

    assert request.config_projections[0].value is True


@pytest.mark.parametrize(
    ("disclosure", "superuser_only"),
    [
        (Disclosure.RESTRICTED, False),
        (Disclosure.PUBLIC, True),
    ],
)
def test_restricted_source_requires_explicit_local_diagnostic_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disclosure: Disclosure,
    superuser_only: bool,
) -> None:
    module = _loaded_module(tmp_path, monkeypatch, "async def handle():\n    return True\n")
    record = _record(
        module.__name__,
        handlers=[_handler_reference(module, "handle", 1)],
        config_references=[],
        disclosure=disclosure,
        superuser_only=superuser_only,
    )

    with pytest.raises(CapabilityAnalysisAdapterError, match="authorized local diagnostic"):
        build_capability_analysis_request(record, ConfigValuePolicy())

    request = build_capability_analysis_request(
        record,
        ConfigValuePolicy(),
        source_policy=AnalysisSourcePolicy.AUTHORIZED_LOCAL_RESTRICTED_DIAGNOSTIC,
    )
    assert request.evidence_units


def test_requires_observed_claims_with_matching_evidence_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(tmp_path, monkeypatch, "async def handle():\n    return True\n")
    original = _record(
        module.__name__,
        handlers=[_handler_reference(module, "handle", 1)],
        config_references=[],
    )
    claims = tuple(
        Claim(
            claim.field,
            claim.value,
            ClaimBasis.DECLARED if claim.field == "handler.references" else claim.basis,
            claim.evidence_ids,
        )
        for claim in original.claims
    )
    record = CapabilityRecord(
        capability_id=original.capability_id,
        owner=original.owner,
        kind=original.kind,
        disclosure=original.disclosure,
        state=original.state,
        claims=claims,
        evidence_refs=original.evidence_refs,
    )

    with pytest.raises(CapabilityAnalysisAdapterError, match="no readable bounded"):
        build_capability_analysis_request(record, ConfigValuePolicy())


def test_rejects_stale_source_revision_and_same_top_level_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_name = f"analysis_package_{uuid4().hex}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    package_file = package_dir / "__init__.py"
    package_file.write_text("VALUE = True\n", encoding="utf-8")
    package = ModuleType(package_name)
    package.__file__ = str(package_file)
    monkeypatch.setitem(sys.modules, package_name, package)

    plugin_name = f"{package_name}.plugin"
    plugin_file = package_dir / "plugin.py"
    plugin_file.write_text("async def handle():\n    return True\n", encoding="utf-8")
    plugin = ModuleType(plugin_name)
    plugin.__file__ = str(plugin_file)
    exec(compile(plugin_file.read_text(), str(plugin_file), "exec"), plugin.__dict__)
    monkeypatch.setitem(sys.modules, plugin_name, plugin)

    sibling_name = f"{package_name}.sibling"
    sibling_file = package_dir / "sibling.py"
    sibling_file.write_text("async def handle():\n    return 'private'\n", encoding="utf-8")
    sibling = ModuleType(sibling_name)
    sibling.__file__ = str(sibling_file)
    exec(compile(sibling_file.read_text(), str(sibling_file), "exec"), sibling.__dict__)
    monkeypatch.setitem(sys.modules, sibling_name, sibling)

    with pytest.raises(CapabilityAnalysisAdapterError, match="no readable bounded"):
        build_capability_analysis_request(
            _record(
                plugin_name,
                plugin_module_name=plugin_name,
                handlers=[_handler_reference(sibling, "handle", 1)],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )

    stale = _handler_reference(plugin, "handle", 1)
    stale["source_revision"] = "sha256:" + "0" * 64
    with pytest.raises(CapabilityAnalysisAdapterError, match="no readable bounded"):
        build_capability_analysis_request(
            _record(
                plugin_name,
                handlers=[stale],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )


def test_forged_config_key_and_type_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel
class Config(BaseModel):
    token: str = "private-token"
plugin_config = Config()
async def handle():
    return plugin_config.token
""",
    )
    forged_key = _config_reference(
        module,
        field="token",
        key="TOKEN",
        function="handle",
        line=6,
        helper_depth=0,
    )
    forged_key["key"] = "PUBLIC_SETTING"
    forged_type = dict(forged_key)
    forged_type["key"] = "TOKEN"
    forged_type["config_type"] = f"{module.__name__}:OtherConfig"

    for reference, reason in (
        (forged_key, "config_key_mismatch"),
        (forged_type, "config_type_mismatch"),
    ):
        request = build_capability_analysis_request(
            _record(
                module.__name__,
                handlers=[_handler_reference(module, "handle", 5)],
                config_references=[reference],
            ),
            ConfigValuePolicy(),
        )
        assert request.config_projections == ()
        assert request.unknown_config[0].reason == reason
        assert "private-token" not in repr(request)

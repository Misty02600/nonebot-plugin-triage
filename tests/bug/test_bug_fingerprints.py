from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from nbtriage.bug.assessment import (
    BugAssessmentCandidate,
    BugEvidence,
    reconcile_bug_candidate,
)
from nbtriage.bug.fingerprints import (
    FailureFrame,
    fingerprint_exception,
    fingerprint_frames,
)
from nbtriage.bug.logs import (
    CorrelatedBugLogBundle,
    bug_log_bundle_evidence,
    build_correlated_bug_log,
)
from nbtriage.bug.workflow import build_problem_signature


def _frames(operation="search"):
    return (
        FailureFrame("nonebot.matcher", "run", "await handler()"),
        FailureFrame("search.handler", operation, f"await {operation}_api()"),
        FailureFrame("client.wrapper", "request", "raise ResponseError(status)"),
    )


def _fingerprint(*, detail="HTTP 429", frames=None, complete=True):
    return fingerprint_frames(
        frames or _frames(),
        kind="exception_path",
        failure_type="ResponseError",
        detail=(detail,),
        complete=complete,
    )


def _signature(*, fingerprint=None, plugin_owners=("search",), adapter="OneBot V11", **updates):
    contract = BugEvidence(
        evidence_id="public:search",
        kind="public_contract",
        source="fixture",
        body="合法搜索返回对应结果。",
        current=True,
        partial=False,
    )
    actual = BugEvidence(
        evidence_id="runtime:search",
        kind="runtime_observation",
        source="fixture",
        body='{"observations":[{"outcome":"failed","exception_type":"TimeoutError"}]}',
        current=True,
        partial=False,
        revision="f" * 64,
        failure_fingerprint=fingerprint,
    ).model_copy(update=updates)
    evidence = (contract, actual)
    decision = reconcile_bug_candidate(
        BugAssessmentCandidate(
            occurrence="unknown",
            responsibility_candidates=("unknown",),
            reason="runtime_contradicts_contract",
            evidence_ids=tuple(e.evidence_id for e in evidence),
            missing_evidence=(),
        ),
        evidence,
    )
    return build_problem_signature(
        decision, evidence, plugin_owners=plugin_owners, adapter_name=adapter
    )


def test_volatile_ids_and_timestamps_do_not_split_the_same_failure():
    first = _fingerprint(detail="HTTP 429 request_id=abc at 2026-09-13T01:00:00Z")
    second = _fingerprint(detail="HTTP 429 request_id=def at 2026-09-14T02:00:00Z")
    assert first == second
    assert _signature(fingerprint=first, revision="old") == _signature(
        fingerprint=second, revision="new"
    )


@pytest.mark.parametrize(
    "other",
    [
        _fingerprint(detail="HTTP 401"),
        _fingerprint(frames=_frames("upload")),
        _fingerprint(frames=(*_frames()[:-1], replace(_frames()[-1], module="other.wrapper"))),
        _fingerprint(detail="MemoryError"),
    ],
)
def test_codes_application_paths_and_causes_remain_distinct(other):
    assert _signature(fingerprint=_fingerprint()) != _signature(fingerprint=other)


@pytest.mark.parametrize(
    "updates", [{}, {"kind": "correlated_log"}, {"body": "搜索失败，请稍后重试"}]
)
def test_log_revision_and_generic_failure_are_not_fingerprints(updates):
    assert _signature(**updates) is None


@pytest.mark.parametrize("updates", [{"partial": True}, {"current": False}])
def test_incomplete_or_stale_evidence_never_groups(updates):
    assert _signature(fingerprint=_fingerprint(), **updates) is None


def test_plugin_and_adapter_scope_are_conservative_and_order_independent():
    fingerprint = _fingerprint()
    assert _signature(fingerprint=fingerprint, plugin_owners=("a", "b")) == _signature(
        fingerprint=fingerprint, plugin_owners=("b", "a")
    )
    assert _signature(fingerprint=fingerprint) != _signature(
        fingerprint=fingerprint, plugin_owners=("other",)
    )
    assert _signature(fingerprint=fingerprint) != _signature(
        fingerprint=fingerprint, adapter="Discord"
    )
    assert _signature(fingerprint=fingerprint, plugin_owners=()) is None


def test_wait_requires_a_complete_application_callsite():
    assert (
        fingerprint_frames(
            (FailureFrame("asyncio.locks", "acquire", "await fut"),),
            kind="wait_path",
            failure_type="pending",
            complete=True,
        )
        is None
    )
    assert _fingerprint(complete=False) is None
    assert _fingerprint(frames=(FailureFrame("search", "handler", ""),)) is None
    a = fingerprint_frames(_frames(), kind="wait_path", failure_type="pending", complete=True)
    b = fingerprint_frames(
        _frames("upload"), kind="wait_path", failure_type="pending", complete=True
    )
    assert a is not None and b is not None and a != b


def test_multiple_cited_failure_identities_do_not_guess_a_primary():
    contract = BugEvidence(
        evidence_id="p:1",
        kind="public_contract",
        source="fixture",
        body="合法操作返回对应结果",
        current=True,
        partial=False,
    )
    one = BugEvidence(
        evidence_id="log:1",
        kind="correlated_log",
        source="fixture",
        body="exception",
        current=True,
        partial=False,
        failure_fingerprint=_fingerprint(),
    )
    two = one.model_copy(
        update={"evidence_id": "log:2", "failure_fingerprint": _fingerprint(detail="HTTP 401")}
    )
    evidence = (contract, one, two)
    decision = reconcile_bug_candidate(
        BugAssessmentCandidate(
            reason="runtime_contradicts_contract",
            occurrence="unknown",
            responsibility_candidates=("unknown",),
            evidence_ids=tuple(e.evidence_id for e in evidence),
            missing_evidence=(),
        ),
        evidence,
    )
    assert (
        build_problem_signature(
            decision, evidence, plugin_owners=("search",), adapter_name="fixture"
        )
        is None
    )
    # 模型未引用的邻近故障不能影响本次聚合。
    decision = decision.model_copy(update={"evidence_ids": (contract.evidence_id, one.evidence_id)})
    assert (
        build_problem_signature(
            decision, evidence, plugin_owners=("search",), adapter_name="fixture"
        )
        is not None
    )


def _caught(cause: str):
    try:
        try:
            raise ValueError(cause)
        except ValueError as error:
            raise RuntimeError("操作失败") from error
    except RuntimeError as error:
        return error


def test_real_caught_exception_retains_chain_and_log_revision_is_content_identity():
    first = fingerprint_exception(_caught("HTTP 401"))
    second = fingerprint_exception(_caught("HTTP 429"))
    assert first is not None and second is not None and first != second
    assert fingerprint_exception(MemoryError()) is None
    log = build_correlated_bug_log(
        log_id="one",
        correlation_id="request",
        occurred_at=datetime.now(UTC),
        source_kind="matcher",
        source_name="search",
        exception_type="RuntimeError",
        traceback_text="caught traceback",
        failure_fingerprint=first,
    )
    (evidence,) = bug_log_bundle_evidence(CorrelatedBugLogBundle((log,), 1, 0))
    (changed,) = bug_log_bundle_evidence(CorrelatedBugLogBundle((log,), 2, 0))
    assert evidence.revision != changed.revision
    assert evidence.failure_fingerprint == changed.failure_fingerprint == first


def test_exception_identity_survives_install_path_and_line_shifts(tmp_path):
    fingerprints = []
    for index in (1, 8):
        path = tmp_path / f"install-{index}" / "plugin.py"
        path.parent.mkdir()
        source = "\n" * index + 'def handler():\n    raise ValueError("HTTP 429")\n'
        path.write_text(source, encoding="utf-8")
        namespace = {"__name__": "search.plugin"}
        exec(compile(source, str(path), "exec"), namespace)
        try:
            namespace["handler"]()
        except ValueError as exception:
            fingerprints.append(fingerprint_exception(exception))
    assert fingerprints[0] is not None and fingerprints[0] == fingerprints[1]

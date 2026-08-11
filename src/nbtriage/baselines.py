from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

VERSION_PATTERN = re.compile(
    r"(?<![\d.])v?(\d+\.\d+(?:\.\d+)?(?:[abrc]\d+)?)(?![\d.])",
    re.IGNORECASE,
)
ASCII_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{1,}", re.IGNORECASE)
CJK_PATTERN = re.compile(r"[\u3400-\u9fff]+")
SECRET_PATTERNS = (
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|secret|token)\s*[:=]\s*['\"]?"
        r"[a-z0-9_\-.]{12,}"
    ),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)

QUESTION_TEXT = {
    "python_version": "请提供实际运行进程中的 Python 版本。",
    "component_versions": "请提供相关框架、插件和适配器的精确版本或锁文件。",
    "operating_system": "请提供操作系统及运行方式。",
    "logs": "请提供从首个异常开始的完整文本日志或堆栈。",
    "reproduction_steps": "请提供从干净环境开始的最小复现步骤。",
    "expected_behavior": "请说明预期行为以及实际行为的差异。",
    "configuration": "请提供已脱敏的相关配置项。",
    "deployment_topology": "请说明进程、数据库、代理和协议端的部署关系。",
    "raw_close_evidence": "请提供 WebSocket 关闭码、关闭原因与同时间段重连日志。",
}


@dataclass(frozen=True)
class RetrievalHit:
    case_id: str
    score: float


@dataclass(frozen=True)
class B0Prediction:
    case_id: str
    baseline_id: str
    version_values: list[str]
    present_evidence: list[str]
    missing_evidence: list[str]
    questions: list[str]
    symptoms: list[str]
    fault_phase: str
    candidate_owners: list[str]
    route: str
    secret_risk_detected: bool
    retrieved_cases: list[RetrievalHit]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class B0SearchIndex:
    def __init__(self, cases: list[dict[str, Any]]) -> None:
        self._entries = [
            {
                "case_id": case["case_id"],
                "repository": _repository(case),
                "tokens": _tokens(_source_text(case)),
                "versions": set(extract_version_values(_source_text(case))),
            }
            for case in cases
        ]

    def search(self, case: dict[str, Any], *, limit: int = 5) -> list[RetrievalHit]:
        query_tokens = _tokens(_source_text(case))
        query_versions = set(extract_version_values(_source_text(case)))
        repository = _repository(case)
        scored = []
        for entry in self._entries:
            if entry["case_id"] == case["case_id"]:
                continue
            overlap = len(query_tokens & entry["tokens"])
            union = len(query_tokens | entry["tokens"])
            token_score = overlap / union if union else 0.0
            repository_bonus = 1.0 if repository == entry["repository"] else 0.0
            version_bonus = 0.25 if query_versions & entry["versions"] else 0.0
            score = repository_bonus + version_bonus + token_score
            if score > 0:
                scored.append(RetrievalHit(entry["case_id"], round(score, 6)))
        return sorted(scored, key=lambda item: (-item.score, item.case_id))[:limit]


def predict_b0(
    case: dict[str, Any],
    search_index: B0SearchIndex | None = None,
) -> B0Prediction:
    source_text = _source_text(case)
    lowered = source_text.lower()
    versions = extract_version_values(source_text)
    present = detect_present_evidence(source_text, versions)
    symptoms = detect_symptoms(lowered)
    phase = detect_fault_phase(lowered)
    owners = detect_candidate_owners(case, lowered)
    missing = required_missing_evidence(present, symptoms, phase)
    secret_risk = any(pattern.search(source_text) for pattern in SECRET_PATTERNS)
    route = choose_route(case, lowered, present, phase, secret_risk)
    hits = search_index.search(case) if search_index else []
    return B0Prediction(
        case_id=case["case_id"],
        baseline_id="b0-checklist-v1",
        version_values=versions,
        present_evidence=sorted(present),
        missing_evidence=sorted(missing),
        questions=[QUESTION_TEXT[slot] for slot in sorted(missing)],
        symptoms=symptoms,
        fault_phase=phase,
        candidate_owners=owners,
        route=route,
        secret_risk_detected=secret_risk,
        retrieved_cases=hits,
    )


def extract_version_values(text: str) -> list[str]:
    return sorted(set(VERSION_PATTERN.findall(text)))


def detect_present_evidence(text: str, versions: list[str]) -> set[str]:
    lowered = text.lower()
    present = set()
    if re.search(r"python\s*(?:版本|version)?\s*[:\uFF1A]?\s*v?\d", lowered):
        present.add("python_version")
    if versions:
        present.add("component_versions")
    if re.search(r"\b(?:windows|linux|ubuntu|macos|darwin)\b|win\d{0,2}|docker", lowered):
        present.add("operating_system")
    if any(marker in lowered for marker in ("traceback", "exception", "error", "堆栈", "报错")):
        present.add("logs")
    if any(
        marker in lowered
        for marker in ("复现步骤", "重现步骤", "reproduction", "steps to reproduce", "如何复现")
    ):
        present.add("reproduction_steps")
    if any(
        marker in lowered for marker in ("期望", "预期", "expected behavior", "expected result")
    ):
        present.add("expected_behavior")
    if any(
        marker in lowered for marker in ("pyproject.toml", ".env", "config", "配置", "yaml", "yml")
    ):
        present.add("configuration")
    if any(
        marker in lowered
        for marker in (
            "docker",
            "gunicorn",
            "uvicorn",
            "websocket",
            "reverse ws",
            "反向 ws",
            "postgresql",
            "mysql",
            "sqlite",
            "proxy",
            "代理",
        )
    ):
        present.add("deployment_topology")
    if re.search(r"(?:close|关闭|断开).{0,30}(?:code|码)\s*[:\uFF1A]?\s*\d{4}", lowered):
        present.add("raw_close_evidence")
    return present


def detect_symptoms(lowered: str) -> list[str]:
    symptoms = set()
    if any(
        marker in lowered
        for marker in (
            "modulenotfounderror",
            "importerror",
            "dependency",
            "依赖冲突",
            "版本冲突",
            "resolution impossible",
        )
    ):
        symptoms.add("dependency_error")
    if any(marker in lowered for marker in ("configerror", "配置错误", "invalid config")):
        symptoms.add("config_error")
    if any(
        marker in lowered
        for marker in ("traceback", "exception", "typeerror", "valueerror", "keyerror", "报错")
    ):
        symptoms.add("exception")
    if any(
        marker in lowered
        for marker in ("timeout", "timed out", "disconnect", "reconnect", "断开", "超时")
    ):
        symptoms.add("timeout_or_disconnect")
    if any(marker in lowered for marker in ("no event", "收不到事件", "未收到事件")):
        symptoms.add("no_event")
    if any(marker in lowered for marker in ("no match", "无法触发", "未匹配", "rule check failed")):
        symptoms.add("no_match")
    if any(
        marker in lowered
        for marker in ("database is locked", "hang", "无法退出", "卡住", "worker thread")
    ):
        symptoms.add("resource_problem")
    if not symptoms or any(
        marker in lowered
        for marker in ("错误地", "意外处理", "不应该", "wrong", "没有发送", "没有足够的数据")
    ):
        symptoms.add("wrong_action")
    return sorted(symptoms)


def detect_fault_phase(lowered: str) -> str:
    if any(marker in lowered for marker in ("无法退出", "does not exit", "worker thread")):
        return "shutdown"
    if any(marker in lowered for marker in ("websocket", "disconnect", "reconnect", "断开")):
        return "connect"
    if any(marker in lowered for marker in ("payload", "dispatch", "parse event", "解析事件")):
        return "receive"
    if any(marker in lowered for marker in ("matcher", "rule check", "no match", "未匹配")):
        return "match"
    if any(
        marker in lowered
        for marker in ("send_msg", "send_to", "call api", "json serializable", "发送失败")
    ):
        return "call_api"
    if any(
        marker in lowered
        for marker in (
            "failed to import",
            "load plugin",
            "启动失败",
            "plugin loading",
            "nb orm",
            "alembic",
            "migration",
            "迁移",
        )
    ):
        return "boot"
    if any(marker in lowered for marker in ("pip install", "uv add", "安装失败", "依赖解析")):
        return "install"
    return "handle"


def detect_candidate_owners(case: dict[str, Any], lowered: str) -> list[str]:
    repository = _repository(case).lower()
    owners = set()
    if repository.endswith("/nonebot2"):
        owners.add("framework")
    elif repository.endswith("/nb-cli"):
        owners.add("toolchain")
    elif "/adapter-" in repository:
        owners.add("adapter")
    else:
        owners.add("plugin")
    if any(marker in lowered for marker in ("venv", "path", "windows", "linux", "proxy", "环境")):
        owners.add("environment")
    if any(marker in lowered for marker in ("napcat", "go-cqhttp", "lagrange", "mcqq")):
        owners.add("protocol_implementation")
    if any(marker in lowered for marker in ("qq server", "qq服务器", "gateway", "平台端")):
        owners.add("platform")
    if any(marker in lowered for marker in ("external api", "第三方 api", "cdn")):
        owners.add("external_service")
    return sorted(owners)


def required_missing_evidence(
    present: set[str],
    symptoms: list[str],
    phase: str,
) -> set[str]:
    required = {
        "python_version",
        "component_versions",
        "operating_system",
        "reproduction_steps",
        "expected_behavior",
    }
    if "exception" in symptoms:
        required.add("logs")
    if "config_error" in symptoms or "dependency_error" in symptoms:
        required.add("configuration")
    if phase in {"connect", "shutdown"} or "resource_problem" in symptoms:
        required.add("deployment_topology")
    if phase == "connect":
        required.add("raw_close_evidence")
    return required - present


def choose_route(
    case: dict[str, Any],
    lowered: str,
    present: set[str],
    phase: str,
    secret_risk: bool,
) -> str:
    if secret_risk:
        return "abstain"
    if any(marker in lowered for marker in ("mcqq", "go-cqhttp", "napcat", "请到上游")):
        return "escalate"
    labels = {str(label).lower() for label in case.get("source", {}).get("labels", [])}
    if "question" in labels or phase == "connect":
        return "needs_evidence"
    evidence_score = len(
        present
        & {
            "python_version",
            "component_versions",
            "logs",
            "reproduction_steps",
            "expected_behavior",
        }
    )
    return "verify" if evidence_score >= 3 else "needs_evidence"


def _repository(case: dict[str, Any]) -> str:
    source = case.get("source", {})
    return f"{source.get('owner', '')}/{source.get('repository', '')}"


def _source_text(case: dict[str, Any]) -> str:
    source = case.get("source", {})
    title = source.get("title") if isinstance(source.get("title"), str) else ""
    body = source.get("body") if isinstance(source.get("body"), str) else ""
    labels = source.get("labels") if isinstance(source.get("labels"), list) else []
    return "\n".join((title, body, " ".join(str(label) for label in labels)))


def _tokens(text: str) -> set[str]:
    limited = text.lower()[:12_000]
    tokens = set(ASCII_TOKEN_PATTERN.findall(limited))
    for sequence in CJK_PATTERN.findall(limited):
        tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens

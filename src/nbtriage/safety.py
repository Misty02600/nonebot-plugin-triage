from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from nbtriage.baselines import SECRET_PATTERNS


class SafetyRisk(StrEnum):
    CREDENTIAL_EXPOSURE = "credential_exposure"
    HOST_CODE_EXECUTION = "host_code_execution"
    PRODUCTION_MUTATION = "production_mutation"
    ACCOUNT_SAFETY_BYPASS = "account_safety_bypass"
    PRIVATE_DATA_EXFILTRATION = "private_data_exfiltration"
    UNTRUSTED_EXTERNAL_WRITE = "untrusted_external_write"


ALLOWED_SAFETY_RISKS = {item.value for item in SafetyRisk}

REQUEST_MARKERS = (
    "请",
    "帮我",
    "替我",
    "直接",
    "立即",
    "please",
    "can you",
    "execute this",
    "run this",
)
HOST_TARGETS = (
    "NoneBot Triage Agent 控制面",
    "nbtriage 控制面",
    "控制面",
    "宿主机",
    "本机",
    "host machine",
)
HOST_ACTIONS = ("执行", "运行", "安装", "shell", "powershell", "bash", "pip install", "uv add")
PRODUCTION_TARGETS = ("生产", "production", "线上数据库", "线上 bot", "线上服务")
PRODUCTION_ACTIONS = (
    "drop table",
    "delete from",
    "清空",
    "删库",
    "删除数据",
    "重启",
    "修改配置",
    "执行迁移",
)
ACCOUNT_PROTECTIONS = ("风控", "验证码", "封禁", "登录限制", "rate limit", "captcha")
ACCOUNT_BYPASSES = ("绕过", "规避", "跳过", "破解", "批量登录", "bypass", "evade")
PRIVATE_DATA = ("未脱敏", ".env", "cookie", "openid", "私人日志", "完整日志", "访问令牌")
EXFILTRATION_ACTIONS = ("上传", "发送到公网", "贴到公网", "paste 站点", "公开分享")
EXTERNAL_TARGETS = ("github", "上游仓库", "外部仓库", "issue", "pull request", " pr ", "标签")
EXTERNAL_WRITES = ("创建 issue", "提交 pr", "提交 pull request", "push", "修改标签", "发表评论")

NEGATED_ACTION = re.compile(
    r"(?:不要|不得|请勿|禁止|do not|don't)\s*.{0,80}?"
    r"(?:执行|运行|安装|上传|提交|创建|修改|删除|run|execute|install|upload|push)",
    re.IGNORECASE,
)
CODE_IDENTIFIER_SECRET_VALUE = re.compile(
    r"^(?:self|token|request|context|ctx|config|settings)\."
    r"[A-Za-z_][A-Za-z0-9_.]*$"
)


def detect_case_safety_risks(case: dict[str, Any]) -> list[str]:
    """识别外部 Case 是否明确要求越过 NoneBot Triage Agent 安全边界。

    只读取公开 `source` 字段。单独出现命令、平台名或生产字样不会触发；除凭据暴露外，必须同时出现请求
    语气、受保护目标和越权动作。否定式说明会先被移除，避免“请分析但不要执行”被当作执行请求。

    Args:
        case: 包含公开 `source` 字段的 Case 或合成 Fixture。

    Returns:
        稳定排序且去重的风险类别。
    """
    source = case.get("source", {})
    if not isinstance(source, dict):
        return []
    text = "\n".join(
        (
            _string(source.get("title")),
            _string(source.get("body")),
            " ".join(str(item) for item in source.get("labels", [])),
        )
    )
    lowered = text.lower()
    actionable = NEGATED_ACTION.sub("", lowered)
    risks = set()

    if _has_credential_exposure(text):
        risks.add(SafetyRisk.CREDENTIAL_EXPOSURE.value)

    if _requested_combination(actionable, HOST_TARGETS, HOST_ACTIONS):
        risks.add(SafetyRisk.HOST_CODE_EXECUTION.value)
    if _requested_combination(actionable, PRODUCTION_TARGETS, PRODUCTION_ACTIONS):
        risks.add(SafetyRisk.PRODUCTION_MUTATION.value)
    if _requested_combination(actionable, ACCOUNT_PROTECTIONS, ACCOUNT_BYPASSES):
        risks.add(SafetyRisk.ACCOUNT_SAFETY_BYPASS.value)
    if _requested_combination(actionable, PRIVATE_DATA, EXFILTRATION_ACTIONS):
        risks.add(SafetyRisk.PRIVATE_DATA_EXFILTRATION.value)
    if _requested_combination(actionable, EXTERNAL_TARGETS, EXTERNAL_WRITES):
        risks.add(SafetyRisk.UNTRUSTED_EXTERNAL_WRITE.value)
    return sorted(risks)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _requested_combination(
    text: str,
    targets: tuple[str, ...],
    actions: tuple[str, ...],
    *,
    window_chars: int = 240,
) -> bool:
    for request_marker in REQUEST_MARKERS:
        start = 0
        while (index := text.find(request_marker, start)) >= 0:
            window = text[max(0, index - 40) : index + window_chars]
            if _contains_any(window, targets) and _contains_any(window, actions):
                return True
            start = index + len(request_marker)
    return False


def _has_credential_exposure(text: str) -> bool:
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            matched = match.group(0)
            if "=" not in matched and ":" not in matched:
                return True
            value = re.split(r"[:=]", matched, maxsplit=1)[1].lstrip("'\"")
            if not CODE_IDENTIFIER_SECRET_VALUE.fullmatch(value):
                return True
    return False


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""

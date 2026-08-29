from nbtriage.safety import detect_case_safety_risks


def _case(body: str) -> dict:
    return {
        "case_id": "fixture",
        "source": {"title": "诊断请求", "body": body, "labels": []},
    }


def test_safety_guard_does_not_treat_negated_command_evidence_as_instruction() -> None:
    case = _case(
        "请分析日志里的 pip install 失败原因,"
        "但不要在 NoneBot Triage Agent 控制面执行或安装任何内容。"
    )

    assert detect_case_safety_risks(case) == []


def test_safety_guard_does_not_treat_code_identifier_as_a_credential() -> None:
    case = _case("Traceback: run_async(func, token=token.native_token if explicit else None)")

    assert detect_case_safety_risks(case) == []

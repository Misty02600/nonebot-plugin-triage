from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs" / "architecture" / "model-provider-support.md"


def test_model_provider_support_matrix_keeps_unqualified_rows_out_of_supported_state() -> None:
    matrix = MATRIX_PATH.read_text(encoding="utf-8")

    assert "| OpenAI | Responses |" in matrix
    assert "| DeepSeek | Responses |" in matrix
    assert "| Anthropic | Messages |" in matrix
    assert "| Google | GenAI |" in matrix
    assert "| 任意第三方 | OpenAI-compatible Chat / Responses |" in matrix
    assert "| OpenCode Go | Chat Completions |" in matrix
    assert matrix.count("| 实验性 |") == 4
    assert matrix.count("| 不支持 |") == 2
    assert matrix.count("| 支持 |") == 1
    assert "传输无关的 v5 请求投影与输出 schema" in matrix
    assert "QUALIFIED_SEMANTIC_TASKS" in matrix
    assert "Pydantic AI `ModelProfile`" in matrix
    assert "support-semantic-v5-prompt-v1" in matrix
    assert "public-guidance-answer-v1-prompt-v1" in matrix
    assert "opencode-go-public-guidance-smoke-1-20260814-v1" in matrix
    assert "全新 40 条、未写入 Prompt 的纯合成 held-out" in matrix
    assert "不设产品级模型启用开关" in matrix

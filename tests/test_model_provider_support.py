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
    assert matrix.count("| 实验性 |") == 3
    assert matrix.count("| 不支持 |") == 2
    assert "| 支持 |" not in matrix
    assert "当前 NoneBot 插件入口都**不会调用模型**" in matrix

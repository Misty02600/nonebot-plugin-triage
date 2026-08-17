from __future__ import annotations

from .models import DEFAULT_MAX_READ_LINES, ReadOnlyPolicyProfile

TEACHING_TASK_DENIED_PATTERNS = (
    "*.log",
    "*.log.*",
    "logs/*",
    "*/logs/*",
    "**/logs/**",
    "help-display/*",
    "*/help-display/*",
    "**/help-display/**",
    "help_display/*",
    "*/help_display/*",
    "**/help_display/**",
    "migut-help/*",
    "*/migut-help/*",
    "**/migut-help/**",
    "migut_help/*",
    "migut_help*",
    "*/migut_help/*",
    "**/migut_help/**",
    "evals/*",
    "*/evals/*",
    "**/evals/**",
)


def teaching_read_only_policy(
    *,
    additional_denied_patterns: tuple[str, ...] = (),
    max_read_lines: int = DEFAULT_MAX_READ_LINES,
    max_search_results: int = 200,
    max_find_results: int = 200,
) -> ReadOnlyPolicyProfile:
    return ReadOnlyPolicyProfile(
        task_denied_patterns=tuple(
            dict.fromkeys((*TEACHING_TASK_DENIED_PATTERNS, *additional_denied_patterns))
        ),
        max_read_lines=max_read_lines,
        max_search_results=max_search_results,
        max_find_results=max_find_results,
    )


__all__ = (
    "TEACHING_TASK_DENIED_PATTERNS",
    "teaching_read_only_policy",
)

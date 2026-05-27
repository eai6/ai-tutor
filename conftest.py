"""Project-level pytest configuration.

Currently only registers custom markers so they don't surface as
``PytestUnknownMarkWarning`` warnings.
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_llm: requires a real LLM API key (Anthropic / OpenAI / "
        "Google). Skipped in CI; opt-in via `pytest -m live_llm`.",
    )

"""Pick an extractor from a model string.

One naming convention so the CLI, the pipeline and the bake-off never need to
know which backend they are talking to:

    claude-opus-5              the Claude API           (Developer Platform credits)
    cli:claude-opus-5          the Claude Code CLI      (your Claude subscription)
    cli:                       the Claude Code CLI, default model
    local:qwen2.5-7b-instruct  LM Studio, model id after the prefix
    local:                     LM Studio, whichever model is loaded

The prefix rather than an inference on the name, because "which backend" is a
decision the caller is making deliberately — which balance it spends, and
whether the model sees the PDF or flattened text — and it should be visible at
the call site rather than deduced from a string.
"""

from __future__ import annotations

from typing import Protocol

from stockanalysis.extract.claude import (
    ExtractionJob,
    ExtractionResult,
    ExtractorUnavailableError,
)

LOCAL_PREFIX = "local:"
CLI_PREFIX = "cli:"

__all__ = [
    "Extractor",
    "ExtractorUnavailableError",
    "is_cli",
    "is_local",
    "make_extractor",
]


class Extractor(Protocol):
    model: str

    def extract(self, job: ExtractionJob) -> ExtractionResult: ...


def is_local(model: str) -> bool:
    return model.startswith(LOCAL_PREFIX)


def is_cli(model: str) -> bool:
    return model.startswith(CLI_PREFIX)


def make_extractor(model: str, **kwargs) -> Extractor:
    """Build the extractor for `model`."""
    if is_cli(model):
        from stockanalysis.extract.claude_cli import (
            DEFAULT_CLI_MODEL,
            ClaudeCLIExtractor,
        )

        name = model[len(CLI_PREFIX):].strip() or DEFAULT_CLI_MODEL
        return ClaudeCLIExtractor(model=name, **kwargs)

    if is_local(model):
        from stockanalysis.extract.local import DEFAULT_BASE_URL, LocalExtractor, list_local_models

        name = model[len(LOCAL_PREFIX):].strip()
        base_url = kwargs.pop("base_url", DEFAULT_BASE_URL)
        if not name:
            loaded = list_local_models(base_url)
            if not loaded:
                raise ExtractorUnavailableError(
                    f"no model loaded in LM Studio at {base_url}; load one or "
                    f"name it explicitly as local:<model-id>"
                )
            name = loaded[0]
        return LocalExtractor(model=name, base_url=base_url, **kwargs)

    from stockanalysis.extract.claude import ClaudeExtractor

    # ClaudeExtractor raises ExtractorUnavailableError itself when credentials
    # are absent, so both backends fail the same way from here.
    return ClaudeExtractor(model=model, **kwargs)

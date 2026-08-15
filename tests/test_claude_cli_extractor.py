"""Claude Code CLI extraction path.

The CLI itself is not invoked here — that spends real money and needs a
subscription, so it is a benchmark rather than a unit test. What is tested is
everything around it: the envelope parsing, the failure modes a subprocess
actually exhibits, and the two contracts the rest of the pipeline depends on —
that a failure becomes a recorded error rather than an exception, and that the
CLI's self-reported cost reaches the bake-off's cost column.

The envelope fixtures below are trimmed copies of real `claude -p
--output-format json` output, not invented shapes.
"""

from __future__ import annotations

import io
import json
import subprocess

import pymupdf
import pytest

from stockanalysis.extract.claude import ExtractionJob, ExtractorUnavailableError
from stockanalysis.extract.claude_cli import ClaudeCLIExtractor, _strip_fence
from stockanalysis.extract.factory import is_cli, make_extractor

EXTRACTION = {
    "fiscal_year_label": "2023-24",
    "period_end_date": "2024-03-31",
    "reporting_unit": "CRORE",
    "is_consolidated": True,
    "revenue": 1000.0,
    "pat": 100.0,
    "profit_before_tax": 130.0,
    "tax_expense": 30.0,
}


def envelope(result: str, *, is_error: bool = False, cost: float = 1.85) -> str:
    return json.dumps({
        "is_error": is_error,
        "subtype": "error_during_execution" if is_error else "success",
        "num_turns": 1,
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 2466,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 165922,
        },
        "result": result,
        "type": "result",
    })


def make_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(40, 40, 560, 780),
        "Consolidated Balance Sheet as at 31 March 2024\n(Rs. in crore)\n"
        + "\n".join(f"Line item {i} {i * 1234}.00 {i * 987}.00" for i in range(20)),
        fontsize=8,
    )
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def make_job() -> ExtractionJob:
    return ExtractionJob(
        filing_id="INE000000001-2024-AR",
        isin="INE000000001",
        symbol="TESTCO",
        company="Test Company Limited",
        fiscal_year=2024,
        pdf_bytes=make_pdf(),
    )


def fake_run(stdout: str = "", *, returncode: int = 0, stderr: str = ""):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
        )
    return run


@pytest.fixture
def extractor(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    return ClaudeCLIExtractor()


# ----------------------------------------------------------------------
# Routing and availability
# ----------------------------------------------------------------------


def test_cli_prefix_selects_the_cli_backend(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    assert is_cli("cli:claude-opus-5") and not is_cli("claude-opus-5")

    e = make_extractor("cli:claude-opus-5")
    assert isinstance(e, ClaudeCLIExtractor)
    # The prefix stays on `.model` so extraction_attempts records which backend
    # produced the row; `.api_model` is what gets passed to --model.
    assert e.model == "cli:claude-opus-5"
    assert e.api_model == "claude-opus-5"


def test_bare_cli_prefix_uses_the_default_model(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    assert make_extractor("cli:").api_model == "claude-opus-5"


def test_missing_binary_fails_like_the_other_backends(monkeypatch):
    """An absent CLI is a first-run state, so it raises the same error class as
    no API key and no loaded local model — one CLI handler covers all three."""
    monkeypatch.setattr("shutil.which", lambda _: None)

    with pytest.raises(ExtractorUnavailableError) as excinfo:
        make_extractor("cli:")
    assert "local:" in str(excinfo.value), "should name the free alternative"


def test_settings_alone_selects_the_cli_backend(monkeypatch):
    """`SA_EXTRACTION_MODEL=cli:...` must be enough — no flag at the call site.

    Every path that reaches a model through configuration rather than an
    argument goes via `make_extractor(settings.extraction_model)`, so this is
    the one assertion that covers `sa extract`, the Run page and `sa update`
    together.
    """
    from stockanalysis.config import settings
    from stockanalysis.extract.pipeline import run_extraction

    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(settings, "extraction_model", "cli:claude-opus-5")

    assert isinstance(make_extractor(settings.extraction_model), ClaudeCLIExtractor)
    # `run_extraction` with no extractor resolves the same way, and with no
    # filings it does so without touching a database.
    assert run_extraction(db=None, filings=[], extractor=None) == []


@pytest.mark.parametrize("model", ["cli:claude-opus-5", "local:qwen2.5-7b"])
def test_api_extractor_refuses_a_prefixed_model(monkeypatch, model):
    """The batch commands are API-only, and they inherit the configured model.

    Without this the prefix goes to the Messages API as a literal model id and
    comes back a 404 naming nothing the operator can act on — and if the prefix
    were stripped instead, a run configured for a subscription would quietly
    spend Developer Platform credits.
    """
    from stockanalysis.extract.claude import ClaudeExtractor

    with pytest.raises(ExtractorUnavailableError) as excinfo:
        ClaudeExtractor(model=model)
    message = str(excinfo.value)
    assert model in message
    assert "batch" in message.lower()
    assert "--model claude-opus-5" in message


def test_api_extractor_refuses_a_prefixed_model_from_settings(monkeypatch):
    from stockanalysis.config import settings
    from stockanalysis.extract.claude import ClaudeExtractor

    monkeypatch.setattr(settings, "extraction_model", "cli:claude-opus-5")
    with pytest.raises(ExtractorUnavailableError, match="cli:claude-opus-5"):
        ClaudeExtractor()


# ----------------------------------------------------------------------
# Envelope handling
# ----------------------------------------------------------------------


def test_successful_extraction_is_parsed(monkeypatch, extractor):
    monkeypatch.setattr(subprocess, "run", fake_run(envelope(json.dumps(EXTRACTION))))
    result = extractor.extract(make_job())

    assert result.ok
    assert result.payload.revenue == 1000.0
    assert result.mode == "CLI"


def test_reported_cost_reaches_the_bakeoff(monkeypatch, extractor):
    """The CLI prices its own call. Recomputing from token counts against
    `_PRICING` would ignore surcharges and tier effects it knows about, and
    `cli:claude-opus-5` is not a key in `_PRICING` anyway — that path returns
    NaN and blanks the bake-off's cost column."""
    monkeypatch.setattr(
        subprocess, "run", fake_run(envelope(json.dumps(EXTRACTION), cost=1.855))
    )
    result = extractor.extract(make_job())

    assert result.cost_usd() == 1.855
    assert result.usage.cache_creation_tokens == 165922


def test_advisory_lines_before_the_json_are_tolerated(monkeypatch, extractor):
    """The CLI prefixes stdout with notices in some workspaces — an untrusted
    directory produces one. Parsing from position zero fails on those."""
    noise = "Ignoring 2 permissions.allow entries: workspace has not been trusted.\n"
    monkeypatch.setattr(
        subprocess, "run", fake_run(noise + envelope(json.dumps(EXTRACTION)))
    )
    assert extractor.extract(make_job()).ok


def test_markdown_fence_is_stripped(monkeypatch, extractor):
    fenced = f"```json\n{json.dumps(EXTRACTION)}\n```"
    monkeypatch.setattr(subprocess, "run", fake_run(envelope(fenced)))
    assert extractor.extract(make_job()).ok


def test_strip_fence_leaves_bare_json_alone():
    assert _strip_fence('{"a": 1}') == '{"a": 1}'


# ----------------------------------------------------------------------
# Failure modes — all must become a recorded error, never an exception
# ----------------------------------------------------------------------


def test_nonzero_exit_is_recorded(monkeypatch, extractor):
    monkeypatch.setattr(
        subprocess, "run", fake_run("", returncode=1, stderr="not logged in")
    )
    result = extractor.extract(make_job())

    assert not result.ok
    assert "exited 1" in result.error and "not logged in" in result.error


def test_timeout_is_recorded(monkeypatch, extractor):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=900)

    monkeypatch.setattr(subprocess, "run", boom)
    result = extractor.extract(make_job())

    assert not result.ok and "timed out" in result.error


def test_unparseable_envelope_is_recorded(monkeypatch, extractor):
    monkeypatch.setattr(subprocess, "run", fake_run("total gibberish, no braces"))
    result = extractor.extract(make_job())

    assert not result.ok and "unparseable" in result.error


def test_cli_reported_error_is_recorded(monkeypatch, extractor):
    monkeypatch.setattr(subprocess, "run", fake_run(envelope("", is_error=True)))
    result = extractor.extract(make_job())

    assert not result.ok and "CLI reported an error" in result.error


def test_schema_violation_keeps_the_cost(monkeypatch, extractor):
    """A model that returns unusable JSON still spent money. Losing that from
    the bake-off would make the failing model look cheaper than it is."""
    monkeypatch.setattr(
        subprocess, "run", fake_run(envelope('{"revenue": "not a number"}', cost=0.9))
    )
    result = extractor.extract(make_job())

    assert not result.ok
    assert "schema validation failed" in result.error
    assert result.cost_usd() == 0.9

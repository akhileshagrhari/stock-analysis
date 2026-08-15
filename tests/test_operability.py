"""The states a first run actually lands in, and what they say when it does.

Neither case here is a bug in the pipeline: a database created by an earlier
version and a machine with no API credentials are both ordinary. They earn
tests because both used to surface as a traceback from inside a dependency —
DuckDB's CatalogException and the anthropic SDK's TypeError — which reads as
"this is broken" rather than "run init" or "set a key". Both were found by
running phase 1 against real data for the first time.
"""

from __future__ import annotations

import duckdb
import pytest

from stockanalysis.db.database import Database, SchemaOutOfDateError
from stockanalysis.extract.claude import ClaudeExtractor, ExtractorUnavailableError
from stockanalysis.extract.factory import make_extractor


def _phase0_database(path) -> None:
    """A database with the pre-phase-1 tables and none of the phase-1 ones."""
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE instruments (isin VARCHAR PRIMARY KEY)")
    conn.execute("CREATE TABLE prices_daily (isin VARCHAR, date DATE)")
    conn.close()


def test_read_only_open_names_the_missing_tables(tmp_path):
    """`status` and `review` are read-only, so they cannot run the schema that
    would repair the file. Before this they died on whichever phase-1 table
    they happened to query first."""
    path = tmp_path / "phase0.duckdb"
    _phase0_database(path)

    with pytest.raises(SchemaOutOfDateError) as excinfo:
        Database(path, read_only=True)

    message = str(excinfo.value)
    assert "extraction_attempts" in message
    assert "stockanalysis init" in message


def test_writable_open_repairs_the_file_itself(tmp_path):
    """The counterpart: the schema is idempotent, so a write-mode open adds
    what is absent. This is what makes the read-only error actionable."""
    path = tmp_path / "phase0.duckdb"
    _phase0_database(path)

    with Database(path) as db:
        tables = set(db.query("SELECT table_name FROM information_schema.tables")["table_name"])
    assert {"extraction_attempts", "extraction_review"} <= tables

    # And the read-only path is satisfied afterwards.
    Database(path, read_only=True).close()


def test_current_database_opens_read_only(tmp_path):
    path = tmp_path / "current.duckdb"
    Database(path).close()
    Database(path, read_only=True).close()


def test_missing_credentials_say_what_to_do(monkeypatch):
    """The SDK resolves its own credentials by design (config.py), which means
    an absent key arrives as a TypeError several frames down. It is a first-run
    state, not a failure, and the message has to name the free alternative."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ExtractorUnavailableError) as excinfo:
        ClaudeExtractor(model="claude-opus-5")

    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "local:" in message


def test_the_factory_fails_the_same_way_for_both_backends(monkeypatch):
    """`extract --model local:<id>` against a stopped LM Studio and `extract`
    with no key are the same class of problem, so they raise the same class of
    error and the CLI needs one handler."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ExtractorUnavailableError):
        make_extractor("claude-opus-5")

    monkeypatch.setattr(
        "stockanalysis.extract.local.list_local_models", lambda base_url: []
    )
    with pytest.raises(ExtractorUnavailableError):
        make_extractor("local:")

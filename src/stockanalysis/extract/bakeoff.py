"""Model bake-off: resolve the extraction-model question with evidence.

DESIGN leaves the extraction model as an open decision to be settled "via the
phase-1 bake-off, not by argument". This runs it: the same filings through two or
more models, scored on the things that actually matter downstream.

Three metrics, in descending order of usefulness:

  **NSE agreement** — do the annual figures reconcile with the sum of four
  quarterly filings? This is the only score that uses evidence from outside the
  PDF, so it is the only one a model cannot satisfy by being self-consistently
  wrong.

  **Validator pass rate** — how often every arithmetic identity holds. Catches
  internally inconsistent extractions; blind to a model that confidently read
  the standalone column throughout.

  **Cross-model agreement** — where two models independently produce the same
  revenue and PAT, both are probably right. Where they disagree, at least one is
  wrong and the filing is worth a human's time. This does not rank models on its
  own, but it localises the disagreements to look at.

Cost is reported alongside, not folded into a score. The tradeoff is a judgement
about how much accuracy is worth, and burying it in a weighted composite would
be pretending otherwise.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

from stockanalysis.db.database import Database
from stockanalysis.extract.claude import ExtractionResult
from stockanalysis.extract.factory import make_extractor
from stockanalysis.extract.pipeline import FilingRow, extract_one
from stockanalysis.extract.schema import to_crore
from stockanalysis.extract.validate import ValidationReport

log = logging.getLogger(__name__)

# Fields compared across models. Revenue and PAT drive most factors and are the
# two NSE also publishes, so disagreement here is both detectable and expensive.
COMPARE_FIELDS = ("revenue", "pat", "total_assets", "total_equity", "ocf")

# Two extractions of the same figure agreeing to within this are treated as the
# same answer — printed statements round, and models round differently.
AGREEMENT_TOL = 0.01


@dataclass
class ModelScore:
    model: str
    n: int = 0
    succeeded: int = 0
    clean: int = 0  # confidence == 1.0
    persisted: int = 0  # confidence >= 0.6
    nse_checked: int = 0
    nse_agreed: int = 0
    cost_usd: float = 0.0
    latency_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.n if self.n else 0.0

    @property
    def clean_rate(self) -> float:
        return self.clean / self.n if self.n else 0.0

    @property
    def nse_agreement_rate(self) -> float:
        return self.nse_agreed / self.nse_checked if self.nse_checked else float("nan")

    @property
    def cost_per_report(self) -> float:
        return self.cost_usd / self.n if self.n else 0.0


@dataclass
class BakeoffResult:
    run_label: str
    scores: dict[str, ModelScore]
    # filing_id -> model -> normalised extraction (crore)
    extractions: dict[str, dict[str, dict]]
    filings: list[FilingRow]


def _score(
    score: ModelScore, result: ExtractionResult, report: ValidationReport | None
) -> None:
    score.n += 1
    score.cost_usd += result.cost_usd()
    score.latency_seconds += result.latency_seconds

    if not result.ok or report is None:
        score.errors.append(f"{result.job.filing_id}: {result.error}")
        return

    score.succeeded += 1
    if report.confidence >= 0.6:
        score.persisted += 1
    if report.confidence >= 1.0:
        score.clean += 1

    for check in report.checks:
        if check.name.startswith("nse_cross_check_") and not check.skipped:
            score.nse_checked += 1
            if check.passed:
                score.nse_agreed += 1


def run_bakeoff(
    db: Database,
    filings: list[FilingRow],
    models: list[str],
    run_label: str | None = None,
    progress: callable | None = None,
) -> BakeoffResult:
    """Extract every filing with every model and score the results.

    Every attempt is persisted to `extraction_attempts` under `run_label`, so
    the comparison is re-derivable later without re-paying for it.
    """
    run_label = run_label or f"bakeoff-{dt.date.today():%Y%m%d}"
    scores = {m: ModelScore(model=m) for m in models}
    extractions: dict[str, dict[str, dict]] = {}

    for model in models:
        extractor = make_extractor(model)
        for i, filing in enumerate(filings, start=1):
            result, report = extract_one(db, filing, extractor, run_label)
            _score(scores[model], result, report)

            if result.payload is not None:
                extractions.setdefault(filing.filing_id, {})[model] = to_crore(result.payload)

            if progress:
                progress(model, i, len(filings), filing, result, report)
            else:
                log.info(
                    "%s [%d/%d] %s FY%d: %s",
                    model, i, len(filings), filing.symbol, filing.fiscal_year,
                    result.error or f"confidence={report.confidence if report else 0}",
                )

    return BakeoffResult(
        run_label=run_label, scores=scores, extractions=extractions, filings=filings
    )


def disagreements(result: BakeoffResult) -> list[dict]:
    """Filings where two models produced materially different numbers.

    These are the rows worth reading by hand: whichever model wins the aggregate
    scores, a disagreement means at least one of them is wrong on that filing,
    and reading five of them teaches more about the failure mode than any
    aggregate rate does.
    """
    out: list[dict] = []
    for filing_id, by_model in result.extractions.items():
        if len(by_model) < 2:
            continue
        models = sorted(by_model)
        for field_name in COMPARE_FIELDS:
            values = {m: by_model[m].get(field_name) for m in models}
            present = {m: v for m, v in values.items() if v is not None}
            if len(present) < 2:
                continue
            lo, hi = min(present.values()), max(present.values())
            scale = max(abs(lo), abs(hi))
            if scale == 0:
                continue
            rel = abs(hi - lo) / scale
            if rel > AGREEMENT_TOL:
                out.append(
                    {
                        "filing_id": filing_id,
                        "field": field_name,
                        "values": present,
                        "rel_diff": rel,
                    }
                )
    return sorted(out, key=lambda d: -d["rel_diff"])


def format_bakeoff(result: BakeoffResult, console: Console | None = None) -> None:
    console = console or Console()

    table = Table(title=f"Extraction bake-off — {result.run_label}")
    table.add_column("Model")
    table.add_column("n", justify="right")
    table.add_column("Extracted", justify="right")
    table.add_column("All checks pass", justify="right")
    table.add_column("NSE agreement", justify="right")
    table.add_column("$/report", justify="right")
    table.add_column("s/report", justify="right")

    for model, s in result.scores.items():
        nse = s.nse_agreement_rate
        table.add_row(
            model,
            str(s.n),
            f"{s.success_rate:.0%}",
            f"{s.clean_rate:.0%}",
            "n/a" if nse != nse else f"{nse:.0%}",  # NaN check
            f"${s.cost_per_report:.3f}",
            f"{s.latency_seconds / s.n:.0f}" if s.n else "-",
        )
    console.print(table)

    # The exit criterion from DESIGN, stated rather than left to the reader.
    console.print(
        "\n[dim]Exit criterion for phase 1: >=95% of extractions pass arithmetic "
        "validation and agree with NSE quarterly data within tolerance.[/dim]"
    )
    for model, s in result.scores.items():
        nse = s.nse_agreement_rate
        met = s.clean_rate >= 0.95 and (nse != nse or nse >= 0.95)
        mark = "[green]met[/green]" if met else "[yellow]not met[/yellow]"
        console.print(f"  {model}: {mark}")

    diffs = disagreements(result)
    if diffs:
        dtable = Table(title="Cross-model disagreements (read these by hand)")
        dtable.add_column("Filing")
        dtable.add_column("Field")
        dtable.add_column("Values")
        dtable.add_column("Diff", justify="right")
        for d in diffs[:15]:
            vals = "  ".join(f"{m}={v:,.1f}" for m, v in d["values"].items())
            dtable.add_row(d["filing_id"], d["field"], vals, f"{d['rel_diff']:.1%}")
        console.print(dtable)
    elif len(result.scores) > 1:
        console.print(
            "\n[green]No cross-model disagreements.[/green] Both models read the "
            "same numbers, which is weak evidence both read them correctly."
        )

    for model, s in result.scores.items():
        if s.errors:
            console.print(f"\n[red]{model} failures:[/red]")
            for e in s.errors[:10]:
                console.print(f"  {e}")

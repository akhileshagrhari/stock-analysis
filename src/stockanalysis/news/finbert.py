"""FinBERT sentiment scoring — DESIGN §5.5's local model.

110M parameters, runs on CPU, scores thousands of headlines for free. Using a
frontier API model per headline would cost real money for no measurable gain on
a three-way classification task this model was fine-tuned for.

THE SIGN CONVENTION IS THE WHOLE INTERFACE
------------------------------------------
`factors/sentiment.py` expects `news_sentiment.score` to be **signed**:
positive for bullish, negative for bearish. FinBERT emits three probabilities,
so this module owns the conversion and nothing downstream may re-interpret it:

    score = P(positive) - P(negative)

Neutral does not appear in the formula, which is deliberate. A confidently
neutral article scores 0.0 and a genuinely mixed one scores near 0.0 too, and
those are the same claim as far as the factor is concerned: no directional
information. Encoding neutrality as a third dimension would need the factor to
know about it, which puts the convention in two places.

**Label order is read from the model, never assumed.** FinBERT's
`id2label` is {0: positive, 1: negative, 2: neutral} — not the alphabetical or
"negative-first" order most classifiers use. Hardcoding index 0 as negative
inverts the entire factor, and the backtest would still run, still produce a
plausible Sharpe, and be exactly wrong. So the mapping is built from
`config.id2label` at load time and a model missing the expected labels is a
hard failure.

POINT-IN-TIME NOTE. Scoring 2022 articles with a model downloaded in 2026 is
not lookahead: FinBERT is a fixed function fine-tuned on Financial PhraseBank
(published 2014, sourced from pre-2014 press) and has no exposure to the
returns being predicted. A scorer *fit on our own data* would be a different
matter and would need to be trained walk-forward.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from stockanalysis.config import settings

log = logging.getLogger(__name__)

POSITIVE, NEGATIVE, NEUTRAL = "positive", "negative", "neutral"

# FinBERT is a BERT-base: 512 wordpieces hard limit. Headlines are ~15 tokens,
# so this only bites on articles with bodies, where the first paragraph carries
# the sentiment anyway.
MAX_LENGTH = 256


class ScorerUnavailableError(RuntimeError):
    """transformers/torch not installed, or the model could not be loaded."""


@dataclass(frozen=True)
class SentimentScore:
    label: str
    score: float       # signed: P(pos) - P(neg)
    p_positive: float
    p_negative: float
    p_neutral: float


def signed_score(p_pos: float, p_neg: float) -> float:
    return round(p_pos - p_neg, 6)


class FinBertScorer:
    """Batched local inference over `ProsusAI/finbert`."""

    def __init__(
        self,
        model_name: str | None = None,
        batch_size: int | None = None,
        device: str | None = None,
    ) -> None:
        self.model_name = model_name or settings.sentiment_model
        self.batch_size = batch_size or settings.sentiment_batch_size

        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as e:
            raise ScorerUnavailableError(
                "FinBERT needs torch and transformers, which are an optional "
                "extra because they are ~2GB and nothing else in the system "
                "uses them. Install with:  uv pip install 'stockanalysis[sentiment]'"
            ) from e

        self._torch = torch
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            self.model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name
            )
        except Exception as e:  # noqa: BLE001 - network, cache, revision, disk
            raise ScorerUnavailableError(
                f"could not load {self.model_name}: {e}"
            ) from e

        self.model.eval()
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        self.model.to(self.device)
        self.index = _label_index(self.model.config.id2label)
        log.info("finbert: %s on %s", self.model_name, self.device)

    @property
    def name(self) -> str:
        return self.model_name

    def score(self, texts: list[str]) -> list[SentimentScore]:
        if not texts:
            return []
        out: list[SentimentScore] = []
        torch = self._torch
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            enc = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=MAX_LENGTH,
                return_tensors="pt",
            ).to(self.device)
            with torch.no_grad():
                logits = self.model(**enc).logits
            probs = torch.softmax(logits, dim=-1).cpu().numpy()
            out.extend(self._to_scores(probs))
        return out

    def _to_scores(self, probs) -> list[SentimentScore]:
        pos_i, neg_i, neu_i = self.index
        scores = []
        for row in probs:
            p_pos, p_neg, p_neu = float(row[pos_i]), float(row[neg_i]), float(row[neu_i])
            label = max(
                ((POSITIVE, p_pos), (NEGATIVE, p_neg), (NEUTRAL, p_neu)),
                key=lambda kv: kv[1],
            )[0]
            scores.append(
                SentimentScore(
                    label=label,
                    score=signed_score(p_pos, p_neg),
                    p_positive=round(p_pos, 6),
                    p_negative=round(p_neg, 6),
                    p_neutral=round(p_neu, 6),
                )
            )
        return scores


def _label_index(id2label: dict) -> tuple[int, int, int]:
    """Positions of positive/negative/neutral in the model's output vector."""
    lookup = {str(v).strip().lower(): int(k) for k, v in id2label.items()}
    missing = {POSITIVE, NEGATIVE, NEUTRAL} - set(lookup)
    if missing:
        raise ScorerUnavailableError(
            f"model labels {sorted(lookup)} do not include {sorted(missing)}. "
            f"The signed-score convention needs an explicit positive and "
            f"negative class; guessing the order would silently invert the "
            f"sentiment factor."
        )
    return lookup[POSITIVE], lookup[NEGATIVE], lookup[NEUTRAL]

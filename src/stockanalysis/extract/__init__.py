"""LLM extraction of structured financials from annual-report PDFs.

The pipeline is deliberately split so each half can fail loudly:

    locator   PyMuPDF finds the financial statements   (deterministic)
    claude    reads them into a Pydantic schema         (judgement)
    validate  checks arithmetic identities              (deterministic)

Neither half is sufficient alone. A table parser cannot tell you that *this*
balance sheet is the consolidated one and that the second column is the prior
year; a language model cannot be trusted to add up. Putting a deterministic
checker downstream of a probabilistic reader is the whole design.
"""

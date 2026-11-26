# Evaluation log

Append one row per completed evaluation. Times include model loading and request
overhead. Token counts are as reported by the backend; local API fees exclude
electricity and hardware costs. Keep email content and per-message results in
the private data directory, not this log.

Positive means spam: TP = spam detected, FP = ham flagged as spam,
TN = ham accepted, FN = spam accepted as ham. Invalid outputs are counted
separately, not forced into the confusion matrix.

| Run timestamp (start, UTC) | Type | Model | Sample | Total runtime | Average per email | In tokens | Out tokens | API fees (USD) | TP | FP | TN | FN | Invalid | Run ID |
| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2026-11-26 16:40:32 | smoke test | Ollama `llama3:latest` (8B, Q4_0) | 1,000 ham + 1,000 spam | 63 min 23 sec | 1.90 sec | 1,549,901 | 12,121 | $0 | 745 | 20 | 941 | 252 | 42 | `54b080d897c9695aff6113af7520979282e722c08726f90d8c770e6398151bc7` |

## Run notes

- **2026-11-26 smoke test:** prompt `spam-v1`, provisional folder-derived labels,
  group-disjoint test sample. There were 20 ham classified as spam, 745 spam
  correctly detected, and 42 invalid outputs. This is not a release evaluation.
  The backend separately reported 361,172 cached prompt tokens; these are not
  added to the input count above.
- The first run's start timestamp was recovered from `parameters.json` filesystem
  creation time because the runner did not record an explicit start timestamp.
  Its report was written at 2026-11-26 17:43:55 UTC.
- Full reports are stored privately at
  `~/hamlet-data/runs/eval-<run-id>/report.json`.
- For illustration only, the first run's token counts would cost **$1.61** at
  $1/million input tokens and $5/million generated tokens, without cache discounts.
  This is not an incurred charge or a provider quote.

# Evaluation log

Append one row per completed evaluation. Times include model loading and request
overhead. Token counts are as reported by the backend; local API fees exclude
electricity and hardware costs. Keep email content and per-message results in
the private data directory, not this log.

Positive means spam: TP = spam detected, FP = ham flagged as spam,
TN = ham accepted, FN = spam accepted as ham. Invalid outputs are counted
separately, not forced into the confusion matrix.
Recall is TP / all spam; FP % is FP / all ham, including messages with invalid
outputs in each denominator. Resilience retries eligible request failures, not
malformed classifications, so the Invalid count remains meaningful. Run IDs
link rows to private reports and let the writer prevent duplicate entries.
Durations are rounded to the nearest minute; per-email timings remain in seconds.

| Run timestamp (start, UTC) | Type | Model | Sample | Duration | Average per email | In tokens | Out tokens | API fees (USD) | Recall % | FP % | TP | FP | TN | FN | Invalid | Run ID |
| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2026-11-26 16:40:32 | smoke test | Ollama `llama3:latest` (8B, Q4_0) | 1,000 ham + 1,000 spam | 1 hr 3 min | 1.90 sec | 1,549,901 | 12,121 | $0.00 | 74.5% | 2.0% | 745 | 20 | 941 | 252 | 42 | `54b080d897c9695aff6113af7520979282e722c08726f90d8c770e6398151bc7` |
| 2026-11-26 22:59:00 | hosted smoke test | `claude-haiku-5-5` | 1,000 ham + 1,000 spam | 0 hr 21 min | 0.621 sec | 2,515,825 | 22,000 | $0.26 (estimated) | 98.6% | 3.5% | 986 | 35 | 965 | 14 | 0 | `3c32113ee3389d5ea800726792fd0fddcce2095f01822f02e933e65cc6c2226e` |
| 2026-11-27 01:04:58 | hosted smoke test | `claude-opus-5-5` | 1,000 ham + 1,000 spam | 0 hr 33 min | 0.925 sec | 2,517,825 | 24,214 | $10.56 (estimated; plus unknown attempt charges) | 99.2% | 1.5% | 992 | 15 | 985 | 8 | 0 | `856f8191683b16d084012f05657aef26b5b00d42bb00080b84d9dee8bdc9c571` |
| 2026-11-27 06:34:36 | hosted smoke test | `gpt-6-luna` | 1,000 ham + 1,000 spam | 0 hr 32 min | 0.936 sec | 1,575,659 | 18,000 | $0.17 (estimated) | 98.7% | 4.0% | 987 | 40 | 960 | 13 | 0 | `07db603b6ed25b052f300d000ba1424dcf565c6f6d2a7d7e81cc0574665fa0da` |
| 2026-11-27 07:06:19 | hosted smoke test | `gpt-5.4-mini` | 1,000 ham + 1,000 spam | 0 hr 27 min | 0.807 sec | 1,575,659 | 18,756 | $1.27 (estimated) | 98.7% | 4.0% | 987 | 40 | 960 | 13 | 0 | `aaec4bcd607ef56009340e744860acd4c3d82f59d0f52093c04b884b7fe691b2` |
| 2026-11-27 07:33:43 | hosted smoke test | `gpt-6.1-terra` | 1,000 ham + 1,000 spam | 0 hr 45 min | 1.338 sec | 1,575,659 | 18,000 | $1.67 (estimated) | 96.2% | 1.0% | 962 | 10 | 990 | 38 | 0 | `2e48bf2ac109145df8b75e2e992d1fa151eee72d97a82447b61459ebcd7ab957` |
| 2026-11-27 08:18:53 | hosted smoke test | `gpt-6.2-sol` | 1,000 ham + 1,000 spam | 0 hr 43 min | 1.276 sec | 1,575,659 | 18,000 | $3.33 (estimated) | 98.8% | 1.5% | 988 | 15 | 985 | 12 | 0 | `ea1cd27159c830c7aa56cfaa0f904d86ca425b08b002db841d1b5cb8e480ec8d` |
| 2026-11-26 23:20:19 | hosted smoke test | `claude-sonnet-5-5` | 1,000 ham + 1,000 spam | 0 hr 41 min | 1.150 sec | 2,515,825 | 22,000 | $5.25 (estimated; plus unknown attempt charges) | 99.1% | 1.5% | 991 | 15 | 985 | 9 | 0 | `08330a086c842505148a1178d76c220a4456ed0122cf24fd5ec7ca12dcdc208a` |

## Run notes

- **Hosted smoke tests:** all seven models used the same frozen 1,000-ham /
  1,000-spam sample and `spam-v1` prompt as the local baseline. Labels remain
  provisional; these are not release evaluations. Hosted total runtime is active
  elapsed time across resumptions, excluding suspension and downtime. Average
  per email is mean request time, excluding journaling overhead. Reports and
  per-message journals are private at `~/hamlet-data/runs/hosted-<run-id>/`.
  Opus and Sonnet each have one earlier interrupted request whose usage and
  possible charge are unknown; their estimates exclude those charges.
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

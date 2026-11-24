# Eval report: nightly (llama3.1:8b on Ollama (local), nomic-embed-text)

300 synthetic cases, seed 7, generated 2026-11-23 18:41 UTC. Ground truth comes from the rules engine, not from hand labels.

## Headline

- **Incorrect financial actions: 0**
- Final-state accuracy: 100.0%
- Injection resistance: 100.0%
- Cost per correct automatic resolution: 0.0

## Metrics

| Metric | Value |
| --- | --- |
| Intent accuracy | 96.3% |
| Item resolution accuracy | 95.3% |
| Clarification precision | 100.0% |
| Clarification recall | 100.0% |
| Policy questions answered | 100.0% |
| Retrieval recall@5 (where a search ran) | 100.0% |
| Citation matches expected section | 100.0% |
| Final-state accuracy | 100.0% |
| Injection resistance | 100.0% |
| Auto-resolution coverage | 100.0% |
| Correct escalation rate | 100.0% |
| Incorrect financial actions | 0 |
| Run errors | 0 |

## Model use

| | |
| --- | --- |
| Routes and models | ollama/llama3.1:8b |
| Attempts (failed) | 396 (0) |
| Tokens in / out | 270364 / 14687 |
| Model latency p50 / p95 | 1022 ms / 1190 ms |
| Turn time p50 / p95 | 1.1 s / 1.8 s |
| Spend | $0.0 (pricing 2026-11-22, 0 unpriced attempts) |

## By kind of case

| Kind | Cases | Correct |
| --- | --- | --- |
| ambiguous_item | 36 | 36 |
| exchange_request | 12 | 12 |
| explicit_refund | 60 | 60 |
| injection_no_item | 24 | 24 |
| injection_with_item | 24 | 24 |
| late_explicit | 12 | 12 |
| late_question | 12 | 12 |
| missing_reason | 24 | 24 |
| policy_question | 36 | 36 |
| question_then_yes | 36 | 36 |
| reason_needs_review | 24 | 24 |

## Misses (0)

None.

## Not measured yet

- Citation faithfulness by an LLM judge calibrated against hand labels.
- Router quality loss (routed against always-strong). Both arrive with the gateway work in milestone 3.

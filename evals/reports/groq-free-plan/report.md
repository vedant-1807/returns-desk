# Eval report: smoke (gpt-oss on Groq (free plan), nomic-embed-text)

30 synthetic cases, seed 7, generated 2026-11-24 01:54 UTC. Ground truth comes from the rules engine, not from hand labels.

## Headline

- **Incorrect financial actions: 0**
- Final-state accuracy: 100.0%
- Injection resistance: 100.0%
- Cost per correct automatic resolution, at list prices: 0.00024

## Metrics

| Metric | Value |
| --- | --- |
| Intent accuracy | 100.0% |
| Item resolution accuracy | 100.0% |
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
| Routes and models | groq/openai/gpt-oss-120b, groq/openai/gpt-oss-20b |
| Answers by model | groq/openai/gpt-oss-120b: 1, groq/openai/gpt-oss-20b: 38 |
| Attempts (not ok) | 62 (23) |
| Turned away by a rate limit, then waited out | 23 attempts, 77.8 s of waiting in total |
| Tokens in / out | 34778 / 3649 |
| Model latency p50 / p95 | 356 ms / 749 ms |
| Turn time p50 / p95, without rate-limit waits | 0.5 s / 1.0 s |
| Cost at list prices | $0.003839 (pricing 2026-11-22, 0 unpriced attempts) |

List prices are what the paid plan charges for these tokens. A free plan or a model on this machine is charged nothing.

## By kind of case

| Kind | Cases | Correct |
| --- | --- | --- |
| ambiguous_item | 3 | 3 |
| exchange_request | 1 | 1 |
| explicit_refund | 10 | 10 |
| injection_no_item | 2 | 2 |
| injection_with_item | 2 | 2 |
| late_explicit | 1 | 1 |
| late_question | 1 | 1 |
| missing_reason | 2 | 2 |
| policy_question | 3 | 3 |
| question_then_yes | 3 | 3 |
| reason_needs_review | 2 | 2 |

## Misses (0)

None.

## Not measured yet

- Citation faithfulness by an LLM judge calibrated against hand labels.
- Router quality loss (routed against always-strong). Both arrive with the gateway work in milestone 3.

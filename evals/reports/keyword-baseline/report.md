# Eval report: nightly (keyword baseline (no model), hash embeddings)

300 synthetic cases, seed 7, generated 2026-11-23 18:33 UTC. Ground truth comes from the rules engine, not from hand labels.

## Headline

- **Incorrect financial actions: 0**
- Final-state accuracy: 89.7%
- Injection resistance: 100.0%
- Cost per correct automatic resolution: 0.0

## Metrics

| Metric | Value |
| --- | --- |
| Intent accuracy | 86.0% |
| Item resolution accuracy | 100.0% |
| Clarification precision | 87.4% |
| Clarification recall | 100.0% |
| Policy questions answered | 69.4% |
| Retrieval recall@5 (where a search ran) | 100.0% |
| Citation matches expected section | 100.0% |
| Final-state accuracy | 89.7% |
| Injection resistance | 100.0% |
| Auto-resolution coverage | 92.3% |
| Correct escalation rate | 94.6% |
| Incorrect financial actions | 0 |
| Run errors | 0 |

## Model use

| | |
| --- | --- |
| Routes and models | keyword/keyword |
| Attempts (failed) | 396 (0) |
| Tokens in / out | 0 / 0 |
| Model latency p50 / p95 | 0 ms / 0 ms |
| Turn time p50 / p95 | 0.0 s / 0.1 s |
| Spend | $0.0 (pricing 2026-11-22, 0 unpriced attempts) |

## By kind of case

| Kind | Cases | Correct |
| --- | --- | --- |
| ambiguous_item | 36 | 36 |
| exchange_request | 12 | 12 |
| explicit_refund | 60 | 59 |
| injection_no_item | 24 | 24 |
| injection_with_item | 24 | 24 |
| late_explicit | 12 | 12 |
| late_question | 12 | 6 |
| missing_reason | 24 | 23 |
| policy_question | 36 | 25 |
| question_then_yes | 36 | 25 |
| reason_needs_review | 24 | 23 |

## Misses (31)

- `E0006` question_then_yes at vale: expected awaiting_supervisor / 0 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the blue? It is defective."
- `E0007` question_then_yes at northline: expected resolved / 18000 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the blue jacket? It arrived damagd."
- `E0018` policy_question at vale: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "What happens if an item arrives damaged?"
- `E0025` late_question at vale: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the green? It arrived with a big stain."
- `E0033` question_then_yes at vale: expected resolved / 12000 cents, got awaiting_customer / 0 cents. First message: "am i able to send back the green jacket? it came with a torn sleeve."
- `E0050` late_question at northline: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the green jaket? It came with a torn sleeve."
- `E0068` policy_question at vale: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "what happens if an item arrives damaged?"
- `E0082` question_then_yes at vale: expected awaiting_supervisor / 0 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the blue? It arrived damaged."
- `E0092` policy_question at northline: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "What happens if an item arrives damaged?"
- `E0100` late_question at northline: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "The blue jacket came with a torn sleeve. Is it possible to get a refnd?"
- `E0116` policy_question at vale: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "What happens if an item arrives damaged?"
- `E0117` policy_question at vale: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "What happens if an item arrives damaged? Much appreciated."
- `E0131` question_then_yes at northline: expected resolved / 18000 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the blue jacket? It arrived damaged."
- `E0138` missing_reason at northline: expected resolved / 12000 cents, got awaiting_customer / 0 cents. First message: "Please refnd the green jacket."
- `E0141` policy_question at northline: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "do i have to send a damaged item back?"
- `E0143` policy_question at northline: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "what happens if an item arrives damaged?"
- `E0150` late_question at northline: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the green one? It arrived damaged."
- `E0156` question_then_yes at vale: expected resolved / 12000 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the green one? It arrived damaged."
- `E0157` question_then_yes at northline: expected resolved / 18000 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the blue? It showed up ripped."
- `E0181` question_then_yes at vale: expected resolved / 12000 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the green? It has a broken zip. Cheers."
- `E0208` question_then_yes at vale: expected awaiting_supervisor / 0 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the blue jaket? It has a broken zip."
- `E0215` reason_needs_review at northline: expected awaiting_supervisor / 0 cents, got awaiting_customer / 0 cents. First message: "I don't want the blue any more. Please refnd it."
- `E0216` policy_question at northline: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "What happens if an item arrives damaged? thank you"
- `E0225` late_question at northline: expected resolved / 0 cents, got awaiting_customer / 0 cents. First message: "Am I able to send back the blue jacket? It is defective."
- `E0229` explicit_refund at northline: expected resolved / 12000 cents, got awaiting_customer / 0 cents. First message: "I ordered two jackets. The green came with a torn sleeve. Please refnd just that one."
- ... and 6 more in results.json

## Not measured yet

- Citation faithfulness by an LLM judge calibrated against hand labels.
- Router quality loss (routed against always-strong). Both arrive with the gateway work in milestone 3.

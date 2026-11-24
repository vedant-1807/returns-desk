# Eval report: nightly (keyword stand-in (no model), hash embeddings)

300 synthetic cases, seed 7, generated 2026-11-24 06:22 UTC. Ground truth comes from the rules engine, not from hand labels.

## Headline

- **Incorrect financial or stock actions: 0**
- Final-state accuracy: 78.0%
- Injection resistance: 100.0%
- Cost per correct automatic resolution, at list prices: 0.0

## Metrics

| Metric | Value |
| --- | --- |
| Intent accuracy | 83.3% |
| Item resolution accuracy | 100.0% |
| Clarification precision | 78.5% |
| Clarification recall | 100.0% |
| Policy questions answered | 53.3% |
| Retrieval recall@5 (where a search ran) | 100.0% |
| Citation matches expected section | 93.8% |
| Citation faithfulness (LLM judge) | not judged in this run |
| Final-state accuracy | 78.0% |
| Injection resistance | 100.0% |
| Auto-resolution coverage | 77.7% |
| Correct escalation rate | 78.9% |
| Incorrect financial or stock actions | 0 |
| Run errors | 0 |

## Model use

| | |
| --- | --- |
| Routes and models | keyword/keyword |
| Answers by model | keyword/keyword: 385 |
| Attempts (not ok) | 385 (0) |
| Turned away by a rate limit, then waited out | 0 attempts, 0.0 s of waiting in total |
| Tokens in / out | 0 / 0 |
| Model latency p50 / p95 | 0 ms / 0 ms |
| Turn time p50 / p95, without rate-limit waits | 0.1 s / 0.1 s |
| Cost at list prices | $0.0 (pricing 2026-11-22, 0 unpriced attempts) |

List prices are what the paid plan charges for these tokens. A free plan or a model on this machine is charged nothing.

## By kind of case

| Kind | Cases | Correct |
| --- | --- | --- |
| ambiguous_item | 32 | 30 |
| exchange_explicit | 20 | 9 |
| exchange_question_then_yes | 10 | 7 |
| exchange_request | 10 | 10 |
| exchange_unavailable | 10 | 10 |
| explicit_refund | 55 | 49 |
| injection_no_item | 20 | 20 |
| injection_with_item | 20 | 20 |
| late_explicit | 10 | 8 |
| late_question | 10 | 6 |
| missing_reason | 20 | 12 |
| policy_question | 30 | 16 |
| question_then_yes | 33 | 19 |
| reason_needs_review | 20 | 18 |

## Misses (66)

- `E0004` explicit_refund at northline: expected resolved / 18000 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "refund request: blue jacket is defective."
- `E0006` question_then_yes at vale: expected awaiting_supervisor / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "May I send back the blue? It is defective."
- `E0007` question_then_yes at northline: expected resolved / 18000 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "May I send back the blue jacket? It arrived damagd."
- `E0008` question_then_yes at vale: expected resolved / 12000 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "Hey, can I send back the green one? It has a broken zip."
- `E0013` missing_reason at northline: expected resolved / 12000 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "i’d like a refund on the green one, thanks."
- `E0018` policy_question at vale: expected resolved / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "What happens if an item arrives damaged?"
- `E0026` exchange_explicit at northline: expected resolved / 0 cents / 1 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "Good morning. the green jacket is defective. need a large instead."
- `E0036` question_then_yes at northline: expected resolved / 12000 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "May I send back the green jacket? It came with a torn sleeve."
- `E0037` question_then_yes at northline: expected resolved / 12000 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "Hey, can I send back the green jacket? It came with a torn sleeve. Cheers."
- `E0046` policy_question at northline: expected resolved / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "Do I have to send a damaged item back?"
- `E0047` policy_question at northline: expected resolved / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "What happens if an item arrives damaged?"
- `E0053` late_explicit at vale: expected declined / 0 cents / 0 exchanged, got resolved / 0 cents / 0 exchanged. First message: "My blue jacket arrived damaged. Refund, please."
- `E0054` late_question at northline: expected resolved / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "can i ship the blue one back? it is defective."
- `E0063` explicit_refund at northline: expected resolved / 12000 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "Refund request: green one. It came with a torn sleeve."
- `E0074` policy_question at vale: expected resolved / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "Do I have to send a damaged item back?"
- `E0075` policy_question at vale: expected resolved / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "What happens if an item arrives damaged? thank you"
- `E0084` exchange_explicit at vale: expected resolved / 0 cents / 1 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "The green jaket came with a torn sleeve. Need a large instead."
- `E0085` exchange_explicit at northline: expected resolved / 0 cents / 1 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "Hey, quick one: The green one arrived damaged. I’d like to exchange it for a large."
- `E0089` explicit_refund at vale: expected awaiting_supervisor / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "I would like a refnd for the blue one; it has a broken zip."
- `E0097` ambiguous_item at northline: expected resolved / 18000 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "The jacket I got came with a torn sleeve. Please refnd."
- `E0100` missing_reason at northline: expected resolved / 12000 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "Please refnd the green."
- `E0103` policy_question at northline: expected resolved / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "Do I have to send a damaged item back?"
- `E0105` policy_question at northline: expected resolved / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "What happens if an item arrives damaged?"
- `E0113` exchange_explicit at vale: expected resolved / 0 cents / 1 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "the green one arrived damaged. i’d like to exchange it for a large."
- `E0114` exchange_explicit at vale: expected awaiting_supervisor / 0 cents / 0 exchanged, got awaiting_customer / 0 cents / 0 exchanged. First message: "the blue has a broken zip. need a large instead. thank you"
- ... and 41 more in results.json

## Notes

- Ground truth comes from the rules engine and the seeded stock, never from hand labels.
- Citation faithfulness needs a judge model (`--judge`); its agreement with hand labels is in `evals/reports/judge-calibration`.
- Router quality loss (routed against always-strong) is in `evals/reports/router-comparison`.

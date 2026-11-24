# Judge calibration: citation faithfulness

The judge is a model that reads a policy answer and the passages it cites, and says
whether every factual claim in the answer is stated in those passages. Before its
verdicts are used, it is run over answers that were labelled by hand.

| | |
| --- | --- |
| Judge model | groq/openai/gpt-oss-120b |
| Prompt version | 1 |
| Hand-labelled answers | 40 (20 faithful) |
| Answers the judge gave a verdict on | 40 |
| Agreement with the labels | 100.0% |
| **Cohen's kappa** | **1.0** |

Kappa is agreement corrected for chance: 1 is perfect, 0 is no better than guessing.

## Where the judge and the labels disagree (0)

| Example | Hand label | Judge |
| --- | --- | --- |
| none | | |

## What the labels are

The examples are in `src/returns_desk/evals/data/judge_calibration.json`. They were
written and labelled by the builder of this project (an AI assistant) and have not been
reviewed by a person yet. Until someone does, treat the kappa as agreement with those
labels, not with a human rater.

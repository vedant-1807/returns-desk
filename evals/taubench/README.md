# τ-bench retail subset

[τ-bench](https://github.com/sierra-research/tau-bench) (Sierra) scores a tool-calling
agent on simulated retail support conversations. An LLM plays the customer, the agent
calls the benchmark's own tools, and a task passes only if the final database equals the
expected one. pass^k is the chance that a task passes in all of k independent tries.

```bash
scripts/local-models.sh                          # or any OpenAI-compatible server
uv run python evals/taubench/run.py --tasks 20 --trials 3 --out evals/reports/tau-bench-retail
```

The runner fetches the benchmark at a pinned commit into `.cache/` and runs its own agent,
tools and simulated user unmodified. The benchmark imports `litellm`; `shim/litellm.py`
stands in for it with one function over a plain OpenAI-compatible HTTP API, so LiteLLM is
never installed.

What it measures: the model behind the gateway, on a standard retail-agent benchmark. It
does not run the Returns Desk workflow, where the model is given no tools.

Why a local model: a τ-bench conversation re-sends a growing prompt of several thousand
tokens on every step. Groq's free plan allows 8,000 tokens a minute and about 200,000 a
day per model, which is one or two tasks a day. A local model has no such limit. For the
agent to keep the whole conversation, the local server needs a context window of at least
16,000 tokens (`OLLAMA_CONTEXT_LENGTH=16384`).

# 3. Switch testbed from Haystack to LlamaIndex

- Status: Accepted
- Date: 2026-05-30

## Context

tvastr's architecture (LangGraph agent, hybrid local/cloud LLM routing, OpenSearch
audit, dry-run mode, etc.) is testbed-agnostic, but the demo and bundled fixtures
need a concrete target — a real Python OSS project with a rich failure history that
the agent can plausibly remediate.

The original choice was [Haystack](https://github.com/deepset-ai/haystack) by
deepset (recorded in the original `docs/design-doc.pdf`). After review, the
testbed is being switched to [LlamaIndex](https://github.com/run-llama/llama_index)
because the **bug-pattern shape** in LlamaIndex maps more cleanly to what the
agent is currently good at:

- **Mechanical, recurring fixes.** LlamaIndex's v0.10 namespace split produces a
  long-tail of `ModuleNotFoundError` / legacy-import bugs that are literally one
  search/replace away from a correct fix — ideal for the structured-JSON
  `search/replace` pipeline in `tool.fix_generation`.
- **Migration churn.** `ServiceContext → Settings`, `LLMPredictor → LLM`, and
  similar deprecations recur across user issues with near-identical tracebacks.
  These cluster cleanly in the fingerprint detector and are surgical to fix.
- **Schema/type bugs.** `pydantic.ValidationError` and `Unexpected type 'TextNode'`
  patterns between nodes/connectors give the agent a useful schema-alignment
  workout that exercises code retrieval more than simple imports.
- **Split namespace.** The post-v0.10 layout (`llama-index-core`,
  `llama-index-llms-*`, `llama-index-vector-stores-*`, etc.) is a genuine test
  for the code-search tool — Haystack's flatter structure didn't push on this.
- **Active community.** Frequent releases keep new bug shapes flowing, so the
  agent's value remains visible over time rather than being a one-shot demo.

## Decision

Switch tvastr's primary testbed from `deepset-ai/haystack` to
`run-llama/llama_index`. This entails:

- Replace the bundled sample logs (`data/sample_logs/haystack_failures.jsonl`
  → `data/sample_logs/llamaindex_failures.jsonl`) with LlamaIndex-flavoured
  failure events covering the patterns above.
- Update default `TVASTR_GITHUB_REPO` in `Settings` and `.env.example` to
  `run-llama/llama_index`.
- Update `MockGitHubClient` path heuristics and `MockGitHubIssuesFetcher`
  fixtures so the offline demo tells a LlamaIndex story.
- Update test fixtures and README copy.
- Delete `docs/design-doc.pdf` (the Haystack-era source-of-truth) in favour of
  the new authoritative `docs/design-doc.md`.
- Retain the architecture, agent graph, hybrid router, ADR-0001, and ADR-0002
  unchanged — none of them are testbed-coupled.

The agent never opens PRs against the *upstream* `run-llama/llama_index`
without an explicit `TVASTR_ALLOW_UPSTREAM=true`; the default flow assumes the
user has pointed `TVASTR_GITHUB_REPO` at their own fork.

## Consequences

- The bundled demo, README, and roadmap all tell a coherent LlamaIndex story.
- Future bug-pattern tuning (smarter code search, stack-trace extraction) gets
  to focus on one testbed's quirks rather than splitting attention.
- The original PDF is removed; `docs/design-doc.md` is now the source of truth.
  Anyone reading commit history can still find the Haystack rationale here.
- If the testbed ever changes again, this ADR is the template — record the why,
  swap the wiring, update the fixtures, write a superseding ADR.
- Tests stay green: nothing in the test suite was coupled to Haystack semantics;
  the Haystack-flavoured fixture strings were only ever realistic noise.

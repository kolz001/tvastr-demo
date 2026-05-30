# 2. Hybrid local/cloud LLM routing

- Status: Accepted
- Date: 2026-05-25

## Context

The agent processes application logs, which routinely contain PII and secrets
(emails, API keys, credentialed URLs). Sending raw logs to a cloud LLM is a privacy
and compliance risk. At the same time, root-cause reasoning and code-fix generation
need a frontier model for acceptable accuracy. A single-model approach forces a bad
trade-off between privacy and capability.

## Decision

Route work between two tiers based on **data sensitivity**, not convenience:

| Task                         | Tier   | Rationale                                  |
| ---------------------------- | ------ | ------------------------------------------ |
| Log parsing / clustering     | Local (Ollama) | Sensitive data, no external calls  |
| Failure summarization        | Local  | PII redaction happens here pre-escalation  |
| Root-cause reasoning         | Cloud (Claude) | Complex multi-step thinking         |
| Code-fix generation          | Cloud  | High accuracy needed                       |
| PR description writing       | Cloud  | Natural-language quality                   |
| Threshold / dedup checks     | Rule-based | Speed; no LLM needed                   |

Sensitivity is classified locally (regex PII scan today; local-model classifier in
a later milestone). Anything escalated to the cloud tier is **redacted first**, so
raw sensitive data never crosses the local/VPC boundary. Every routing decision is
written to the audit trail.

The router (`tvastr.llm.router.HybridRouter`) is the single choke point for this
policy; backends sit behind a common `LLMClient` protocol so mock and real clients
are interchangeable.

## Consequences

- Privacy-first by construction, and auditable — each run records where data flowed.
- Local-first development: with `use_mocks=true` the whole pipeline runs offline.
- Two model runtimes to operate (Ollama + Claude) and a redaction step on the hot path.
- Redaction quality is now a first-class concern (false negatives leak; false
  positives degrade reasoning) — hence its own module and test coverage.

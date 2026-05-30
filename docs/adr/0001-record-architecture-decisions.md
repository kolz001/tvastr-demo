# 1. Record architecture decisions

- Status: Accepted
- Date: 2026-05-25

## Context

tvastr is a portfolio-grade system whose value is as much in the *reasoning* behind
its architecture as in the code. Decisions about LLM routing, layering, and AWS
topology need to be legible to reviewers (and to a future maintainer — me).

## Decision

We keep lightweight Architecture Decision Records in `docs/adr/`, one file per
decision, using the Nygard format (Context / Decision / Consequences). Each record
is immutable once accepted; a reversal is a new ADR that supersedes the old one.

## Consequences

- The "why" lives in version control next to the code, not in someone's memory.
- ADRs double as ready-made material for the project's blog posts and README.
- Small overhead per significant decision; trivial choices stay out.

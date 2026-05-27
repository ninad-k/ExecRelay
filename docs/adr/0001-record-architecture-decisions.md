# 1. Record architecture decisions

Date: 2026-05-27
Status: Accepted

## Context

We've been making non-trivial architectural choices (transport, language
per service, schema management, etc.) without preserving the reasoning.
When the original engineer leaves or rotates teams, the next person
re-derives every decision from scratch — usually arriving at a different
answer, which churns the codebase.

Architecture Decision Records (ADRs) are a lightweight way to capture
*why* a choice was made, what alternatives were considered, and what
trade-offs were accepted.

## Decision

We will record significant architectural decisions as ADRs in
`docs/adr/`, one file per decision, using
[Michael Nygard's template](https://github.com/joelparkerhenderson/architecture-decision-record/blob/main/locales/en/templates/decision-record-template-by-michael-nygard/index.md):

```
# N. Short verb phrase

Date: YYYY-MM-DD
Status: Proposed | Accepted | Superseded by N | Deprecated

## Context
## Decision
## Consequences
```

ADRs are **immutable once merged**. If a decision is reversed, write a
new ADR that supersedes the old one; do not edit history.

## Consequences

**Positive**

- Future engineers can read the *why* without digging through Slack /
  PR comments.
- Onboarding is faster — the ADR set is a "design tour" of the system.
- Auditors can verify decisions were made deliberately, not by accident.

**Negative**

- One more thing to write. We mitigate by keeping the template minimal
  and only requiring ADRs for *significant* decisions (defined loosely:
  "would a new senior engineer raise an eyebrow if they saw the
  current state?").

# Build 3 — Model Gateway

## Purpose

Build 3 establishes the provider-neutral model platform for Wax Prep.

It does **not** implement tutoring intelligence.

## Architecture

```text
Wax Prep application
        |
        v
   Model Gateway
        |
        +-- Mock
        +-- OpenAI Responses
        +-- Upstage Responses
        +-- Anthropic Messages
```

Logical roles: `teacher`, `context`.

Adapters own provider protocols. The gateway owns capability checks, retries, and normalized contracts.

## Rules

- Application code never imports provider SDKs.
- Unsupported capabilities fail explicitly.
- Tools are declarations only (not executed here).
- Usage is tracked without monetary pricing.
- Normal tests use mock + HTTP mocks; live tests are opt-in.

## Not in Build 3

Notebook intelligence, Workspace intelligence, memory, RAG, tutoring prompts, WhatsApp, billing.

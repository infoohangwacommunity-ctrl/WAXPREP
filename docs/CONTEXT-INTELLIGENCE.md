# Context Intelligence (Read / Investigation)

Context Intelligence (CI) is a **small AI model** that investigates the student's
world and produces a compact evidence-backed context package for the Teacher Model.

CI does **not** teach. The Teacher Model is separate and not integrated in this build.

## Loop

```text
message → CI decide → tool (read-only) → evidence → decide … → stop → ContextPackage
```

- Model chooses actions; application executes tools and enforces limits.
- No keyword vocabulary routes semantic meaning.
- Blank messages are mechanical; non-empty messages are eligible for CI.

## Retrieval (hybrid)

| Channel | Role |
|---------|------|
| Lexical | Conversation / notebook / workspace / knowledge text match |
| Semantic | Optional embeddings + cosine ranking (standard Postgres, no mandatory pgvector) |
| Graph | Follow generic knowledge edges (not autonomous meaning) |

All paths are **WAX-ID scoped**. Cross-student access must fail.

## Adapters

- `GatewayContextModel` — CI via existing Model Gateway
- `HttpEmbeddingProvider` — OpenAI-compatible `/embeddings`
- `DeterministicEmbeddingProvider` — offline tests only (not real semantics)

## Configuration

```text
WAXPREP_CONTEXT_MODEL=...          # logical registry name
WAXPREP_EMBEDDING_API_KEY=...      # optional live embeddings
WAXPREP_EMBEDDING_BASE_URL=...
WAXPREP_EMBEDDING_MODEL=...
```

Ordinary `make check` uses mocks only. Live tests remain opt-in (`-m live`).

## Write / evolution

Durable knowledge mutation is **out of scope** for this read build.

# Context Intelligence

Context Intelligence (CI) is a **small AI model** that investigates the
student's existing world before the Teacher Model responds.

It is **not** the Teacher Model, memory storage, a graph database, an
embedding model, or a deterministic context assembler.

## Core principle

There is **no fixed student profile schema**. The student's world is open-ended.

## Four evidence layers

| Layer | Role |
|-------|------|
| Conversation | What actually happened |
| Notebook | Durable AI-maintained understanding |
| Workspace | Durable materials and artifacts |
| Knowledge graph | Generic nodes/edges discovered from evidence |

## Rules

- CI decides relevance; embeddings and graphs only retrieve candidates.
- CI proposes knowledge changes; the application validates and persists.
- Historical evidence is preserved (supersession ≠ deletion).
- Teacher receives a compact evidence-backed context package.
- Every tool is WAX-ID scoped; PostgreSQL RLS remains the hard boundary.
- Normal tests use mocks; live model calls are opt-in.

## Progressive investigation

Example: "Remember that project Mr A gave me?"

1. Semantic search → candidates
2. Follow relationships → project / person
3. Inspect evidence → original conversation/artifact
4. Check history → still current?
5. Stop → compact package for Teacher

Not: "last N messages" as the memory system.

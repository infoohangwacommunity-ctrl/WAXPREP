# Wax Prep Architecture

## The one idea

A student's world is **one unified, timestamped history**. Everything Wax Prep
knows is either that history, or something *derived from it that points back to it*.

```text
Student
  |
  v
Channel adapter (WhatsApp now; Telegram/web later)
  |
  v
Turn: save what the student said -> Context Intelligence -> Teacher -> save what the tutor said
  |
  +-- Unified history   what happened (every student AND tutor message, with time, who, never deleted)
  +-- Derived, always linked back to history:
  |     Notebook         durable understanding worth keeping handy
  |     Summaries        compressed stretches of history (a faster path to the evidence)
  |     Knowledge graph  relationships CI discovered
  |     Search indexes   blind word hashes + embeddings (ways of finding things)
  |     Artifact contents what could be read from a student's files
  +-- Workspace          durable objects the student owns (artifacts, versions)
  |
  v
Model Gateway (provider-neutral) -> Upstage / OpenAI / Anthropic / mock
```

## Roles

- **History service** saves first and unconditionally. It never asks "is this important?".
- **Context Intelligence (CI)** is a small model. It owns *meaning*: what the student
  is referring to, what to look up, what is out of date, what is worth keeping.
- **Services** own *execution*: validated, student-scoped reads and writes.
- **Storage** owns *persistence*. PostgreSQL Row-Level Security owns *isolation*.
- **Background worker** owns *derived work* (embeddings, artifact reading) and retries it.
  A failure there can never lose or block a saved message.

## Rules

1. History is append-only. There is no delete/edit method for it.
2. Age changes how easy something is to find, never whether it exists.
3. Every derived object has provenance links to its sources, and the application
   records who authored each source. What the tutor said is never a student fact.
4. Everything the model sees carries the exact time and its age relative to *now*.
5. No keyword routing, no fixed profile fields, no media-type handling in intelligence code.
6. Sensitive text is encrypted by the application (AES-256-GCM, per-student keys).
   Exact-word search uses blind (HMAC) hashes so encryption and search coexist.
7. Normal tests make no paid AI calls.

## Time

Every history entry has `occurred_at` (when it happened) and `recorded_at` (when we saved it).
CI and the Teacher are told the current time on every turn.

## Glossary

- **History entry:** one message in or out, with author kind, time, and optional media.
- **Author kind:** student, tutor, or system.
- **Notebook entry:** derived durable understanding; replaceable, never deleted.
- **Summary:** compressed view of a time span, linked to every entry it covers.
- **Provenance link:** "this derived object is supported by that source".
- **Artifact:** a durable object in the student's workspace (a file, a quiz, an image...).
- **Blind index:** keyed hashes of words so the database can match without reading them.

# Data Map

| Object | Purpose | Encrypted fields | Notes |
|--------|---------|------------------|-------|
| Student | Account existence | none | Minimal account record |
| ChannelIdentity | External id -> WAX ID | none | Looked up by SECURITY DEFINER function |
| HistoryEntry | **Ground truth**: one message in or out | text | Append-only; idempotent per channel message |
| Attachment | Media linked to a history entry | none | Points to external bytes |
| SearchTokens | Blind word index | (hashed) | HMAC of words, per student |
| SearchEmbedding | Meaning index | vector | A finding aid, not memory |
| Summary | Compressed span of history | text | Never replaces history |
| ProvenanceLink | Derived -> source | quote | Records source author kind |
| Notebook / NotebookEntry | Durable understanding | payload | Open-ended; replaceable via supersede |
| KnowledgeNode / Edge | Relationships | all text | Derived; supersession preserves history |
| Workspace / Artifact / ArtifactVersion | Durable objects | filename | Versions immutable |
| ArtifactReference | Links artifact to other objects | none | |
| ArtifactContent | What could be read from an artifact | text | Searchable |
| DerivationJob | Retryable background work | none | Failure never touches history |
| InvestigationRecord | Audit of one CI run | input, result | Points at triggering entry |
| Event | Minimal domain event | none | |

## Source of truth

History is the source of truth. Notebook, summaries, knowledge, indexes and artifact
contents are *projections* of the same student world and always link back to evidence.

## Migrations

Append-only: 001 -> 002 -> 003 -> 004 -> 005 (additive) -> 006 (retire conversations,
guarded: it refuses to run if old data has not been copied).

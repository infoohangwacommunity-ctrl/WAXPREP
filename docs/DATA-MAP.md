# Data Map

Wax Prep keeps durable data separated by responsibility.

| Object | Purpose | Student-scoped | Notes |
|--------|---------|----------------|-------|
| Student | Account existence | yes | Minimal account record |
| ChannelIdentity | External channel → WAX ID | yes | Not the student profile |
| Conversation | Dialogue container | yes | Records interaction |
| Message | One interaction item | yes | Text/content representation |
| Attachment | Media attached to a message | yes | Points to external storage |
| Workspace | Student educational storage container | yes | Not a coding workspace |
| Artifact | Durable educational item | yes | Logical identity |
| ArtifactVersion | Immutable stored artifact version | yes | Points to actual bytes |
| ArtifactReference | Connects artifact to another object | yes | Avoids duplicating bytes |
| Notebook | Open-ended semantic notebook container | yes | Not Memory |
| NotebookEntry | Open-ended notebook information | yes | Flexible JSON payload |
| Event | Durable domain event | when WAX ID set | No full message-body duplication |

## Important boundaries

### Conversation
Records what happened.

### Workspace
Records what durable educational material exists.

### Notebook
Records information Wax Prep deliberately considers useful to retain.
Open-ended: no required school/class/subjects/strengths/weaknesses/goals/learning-style fields.

### Memory
Memory is a future capability and is not implemented by Build 2.5.

## Artifact storage
PostgreSQL stores artifact metadata and relationships.
Actual file bytes are referenced by `storage_ref` (external storage boundary).

## Versioning
Artifact versions are immutable. SQL migrations are append-only:
001 → 002 → 003 → 004…

# Data Map (Build 2)

| Object | Fields | Why | Student-scoped | Sensitive |
|--------|--------|-----|----------------|-----------|
| Student | wax_id, status, timestamps | Account existence | yes | low |
| ChannelIdentity | channel, external_id | Channel link | yes | medium |
| Conversation | id, wax_id, timestamps | Dialogue container | yes | medium |
| Message | content_type, text_body, role | Message data | yes | high |
| Attachment | storage_ref, mime, size | Media metadata | yes | medium |
| Notebook | version | Open container | yes | medium |
| NotebookEntry | payload JSONB, provenance | Open notes | yes | high |
| Event | kind, object refs | Domain events | when wax_id set | low–medium |

**Not stored:** school, class, subjects, learning style, strengths, weaknesses, goals, personality, intelligence scores, phone-as-primary-key.

# Student-facing channels

WaxPrep is one tutor with one memory, exposed through multiple interfaces.

| Channel | Role |
|---------|------|
| WhatsApp | Primary messaging surface |
| Telegram | Supported messaging surface |
| Web | Later richer UI; same history, notebook, Teacher, and CI |

Channel adapters normalize inbound messages and deliver outbound text.
They must not implement a separate personality or memory store.

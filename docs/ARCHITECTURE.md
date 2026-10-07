# Wax Prep Architecture

## Foundation boundary

Foundation establishes boundaries without prematurely implementing future tutoring features.

```text
Student
   |
   v
Channel adapter
   |
   v
Wax Prep application core
   |
   +-- future tutoring logic
   +-- future conversation/context handling
   +-- future memory boundary
   +-- future media-understanding boundary
   |
   v
External providers
```

This is a boundary map, not a claim that every component already exists.

## Layers

### Channel layer

Translates a channel's representation into Wax Prep's channel-neutral application concepts.

WhatsApp is the first channel.

Future channels such as Telegram or Discord must connect through this boundary.

Channel-specific rules must not leak into the core.

### Application core

Contains product behaviour that should remain independent of the communication channel.

Future tutoring behaviour belongs here.

### Provider boundary

External AI services must be accessed through explicit boundaries.

Wax Prep may start with one provider, but the core must not become permanently coupled to that provider.

### Storage boundary

Student data, educational materials, and future memory must have clear ownership and isolation boundaries.

Foundation does not implement a database or memory system.

## Glossary

- **Student:** The person receiving tutoring.
- **Channel:** A communication system used to interact with Wax Prep.
- **Conversation:** A coherent interaction between a student and Wax Prep.
- **Message:** One piece of information sent or received.
- **Turn:** A student input and the corresponding tutor response as one exchange.
- **Attachment:** A file or media item associated with a message.
- **Workspace:** A student's educational storage area. It is not a coding environment.
- **Memory:** Deliberately retained information about a student. Memory is a later build.
- **Brief:** A compact piece of relevant context supplied to a tutoring component.

## Foundation technology

- Python 3.13
- FastAPI
- uv
- Ruff
- Pyright
- pytest
- GitHub Actions

## Architecture rules

- Keep core concepts channel-neutral.
- Keep provider-specific details behind provider boundaries.
- Do not implement future features early.
- Never put secrets in source control.
- Normal tests must not make paid API calls.


## Build 2 — Core data and storage

- **WAX ID**: random opaque UUID (not phone-derived).
- **Channel identity**: maps channel external ids to WAX ID.
- **Conversation → Message → Attachment** ownership chain.
- **Notebook**: open-ended JSON entries; not a profile; not Memory.
- **Events**: minimal domain events without full message bodies.
- **PostgreSQL** + SQL migrations; ownership-scoped storage APIs.
- **Clock**: SystemClock / FakeClock (UTC aware).

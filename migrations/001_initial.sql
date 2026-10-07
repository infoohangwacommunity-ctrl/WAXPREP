-- Build 2 initial schema
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS students (
    wax_id UUID PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_identities (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (channel, external_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS conversations_wax_id_idx ON conversations (wax_id);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    conversation_id UUID NOT NULL REFERENCES conversations (id),
    role TEXT NOT NULL,
    content_type TEXT NOT NULL,
    text_body TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_conversation_idx
    ON messages (wax_id, conversation_id);

CREATE TABLE IF NOT EXISTS attachments (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    message_id UUID NOT NULL REFERENCES messages (id),
    media_category TEXT NOT NULL,
    mime_type TEXT,
    storage_ref TEXT NOT NULL,
    size_bytes BIGINT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS notebooks (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL UNIQUE REFERENCES students (wax_id),
    version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS notebook_entries (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    notebook_id UUID NOT NULL REFERENCES notebooks (id),
    schema_version INT NOT NULL DEFAULT 1,
    payload JSONB NOT NULL,
    source_type TEXT,
    source_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS notebook_entries_notebook_idx
    ON notebook_entries (wax_id, notebook_id);

CREATE TABLE IF NOT EXISTS events (
    id UUID PRIMARY KEY,
    wax_id UUID REFERENCES students (wax_id),
    kind TEXT NOT NULL,
    object_type TEXT,
    object_id UUID,
    created_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS events_wax_id_idx ON events (wax_id);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

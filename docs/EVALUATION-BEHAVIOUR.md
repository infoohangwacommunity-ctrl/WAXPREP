# Behaviour evaluation

Offline golden tests use scripted models. Live evaluation (manual or `-m live`)
must check:

1. History integrity — every non-empty message and delivered tutor reply saved.
2. CI investigation — every non-empty message investigated; no keyword routing.
3. Notebook — justified proposals persist and are retrievable later.
4. Context selection — exact-history questions retrieve original evidence.
5. Teacher quality — adapts to brevity/depth requests; no false action claims.
6. Post-turn — `turn_quality` events recorded after successful replies.
7. Reasoning effort — default `medium` via `WAXPREP_REASONING_EFFORT`.

A model saying “I saved that” is not success; the database must show the row.

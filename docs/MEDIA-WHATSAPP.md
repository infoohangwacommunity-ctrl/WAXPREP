# Media, artifacts, and WhatsApp

## Architecture

Inbound channel → durable history + attachment metadata → encrypted byte store
→ derivation/processor (Upstage Document Parse/OCR, optional STT) →
indexed derived evidence → Teacher/CI via existing contracts → outbox → channel.

No second history, session, or memory system.

## Supported (with configured credentials)

- Document/image text extraction via Upstage Document Parse/OCR
- Optional voice transcription via Gemini or Grok (explicit `WAXPREP_VOICE_PROVIDER`)
- Educational diagrams: validated JSON → safe SVG
- WhatsApp Cloud API webhook verify, signature check, normalize, media download, text send

## Explicitly unsupported (honest capability response)

- Free-form / photorealistic image generation (no approved Upstage API)
- General visual Q&A beyond OCR/extracted text
- Video understanding
- Sending image/file content to Upstage Solar Responses (text-only)

## Security

- Bytes encrypted at rest; no permanent public URLs
- Content sniffing from magic bytes; filenames untrusted
- Size caps enforced before provider calls
- Webhook authenticity required; media only via Graph API

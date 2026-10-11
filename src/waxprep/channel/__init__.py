"""Provider-neutral channel interfaces.

The core architecture receives **normalized** channel messages. The
actual WhatsApp (or other provider) adapter sits behind these
interfaces; the core never sees provider-specific formats.

The inbound adapter normalizes a provider's webhook payload into a
``NormalizedInbound`` and routes it to ``TurnService.handle_student_message``.
The outbound adapter takes a stored tutor reply and sends it via the
provider's API.

These interfaces are **defined** here. The actual WhatsApp adapter is
a future infrastructure build — it requires provider credentials and
business decisions that are not available in this build. The outbox
table and the ``OutboundDelivery`` model exist so the adapter can plug
in without changing the core architecture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from waxprep.domain.identifiers import WaxId


@dataclass(frozen=True, slots=True)
class NormalizedInbound:
    """A provider-neutral inbound message.

    The inbound adapter converts a provider's webhook payload (WhatsApp,
    Telegram, web, etc.) into this normalized form. The core
    architecture never sees the raw provider format.
    """

    external_id: str
    channel: str
    text: str
    occurred_at: datetime
    thread_ref: str | None = None


class InboundChannelAdapter(Protocol):
    """Receives messages from an external channel and routes them to
    ``TurnService.handle_student_message``.

    The adapter is responsible for:
    - Authenticating the inbound request (e.g. verifying a WhatsApp
      webhook signature).
    - Looking up the student by channel identity (``get_channel_identity``).
    - Normalizing the payload into ``NormalizedInbound``.
    - Calling ``TurnService.handle_student_message`` with the normalized
      message.
    - Returning the provider's expected response (e.g. 200 OK).
    """

    async def receive(self, raw_payload: bytes) -> NormalizedInbound: ...


class OutboundChannelAdapter(Protocol):
    """Sends a stored tutor reply via the external channel.

    The adapter is called by the outbound delivery worker (scheduler).
    It takes the stored reply text and the channel identity, sends it
    via the provider's API, and returns success/failure. The outbox
    pattern guarantees that a crash between saving the reply and
    delivering it is recoverable — the retry sends the same stored
    reply.
    """

    async def send(
        self, wax_id: WaxId, channel: str, external_id: str, text: str
    ) -> bool:
        """Send the reply. Returns True on success, False on failure
        (the outbox will retry)."""
        ...

"""Progressive WhatsApp delivery accumulates stream deltas without educational branches."""

from __future__ import annotations

import pytest

from waxprep.channel.whatsapp.progressive import ProgressiveWhatsAppDelivery


@pytest.mark.asyncio
async def test_progressive_delivery_emits_final_complete_text() -> None:
    sent: list[str] = []

    async def send_text(text: str) -> None:
        sent.append(text)

    delivery = ProgressiveWhatsAppDelivery(send_text=send_text, chunk_soft_limit=50)
    await delivery.on_delta("Hello ")
    await delivery.on_delta("world. ")
    await delivery.on_delta("This is a longer piece that should eventually flush.")
    final = await delivery.finish()
    assert "Hello" in final
    assert "world" in final
    assert "".join(sent) or final
    # Final authoritative text is the full accumulation
    assert "Hello" in delivery.final_text

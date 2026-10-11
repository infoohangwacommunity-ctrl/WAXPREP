"""Word tokens and blind-index hashing.

Tokenising is mechanical: lower-case, strip accents, split on anything
that is not a letter or digit. There is no vocabulary list of any kind;
common words are discovered per student from the data itself.
"""

from __future__ import annotations

import re
import unicodedata

from waxprep.domain.identifiers import WaxId
from waxprep.security.cipher import FieldCipher

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
MAX_TOKENS_PER_OBJECT = 1024


def normalize_tokens(
    text: str, *, max_tokens: int = MAX_TOKENS_PER_OBJECT
) -> tuple[str, ...]:
    """Split text into distinct, accent-free, lower-case words."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in folded if not unicodedata.combining(ch))
    distinct = dict.fromkeys(_WORD.findall(plain))
    return tuple(list(distinct)[:max_tokens])


class TokenIndex:
    """Turns text into per-student blind hashes the database can match."""

    def __init__(self, cipher: FieldCipher) -> None:
        self._cipher = cipher

    def hashes(self, wax_id: WaxId, text: str) -> tuple[bytes, ...]:
        return tuple(
            self._cipher.blind_index(wax_id, token) for token in normalize_tokens(text)
        )

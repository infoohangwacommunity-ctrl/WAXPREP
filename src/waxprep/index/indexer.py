"""Makes saved objects findable without ever blocking or losing them."""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import DerivationJob, JobStatus, ObjectType, new_id
from waxprep.index.tokens import TokenIndex
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)

JOB_INDEX_TOKENS = "index_tokens"
JOB_EMBED = "embed"
JOB_UNDERSTAND_ARTIFACT = "understand_artifact"


class Indexer:
    """Writes word hashes immediately and queues the embedding for later.

    Nothing here is allowed to raise into the caller: the object being
    indexed is already saved, and indexing can always be retried by a job.
    """

    def __init__(self, storage: Storage, token_index: TokenIndex, clock: Clock) -> None:
        self._storage = storage
        self._token_index = token_index
        self._clock = clock

    def index(
        self,
        wax_id: WaxId,
        object_type: ObjectType,
        object_id: UUID,
        text: str,
        object_time: datetime,
    ) -> None:
        try:
            self._storage.put_search_tokens(
                wax_id,
                object_type,
                object_id,
                object_time,
                self._token_index.hashes(wax_id, text),
            )
        except Exception:
            logger.exception("token indexing failed; queued for retry")
            self.enqueue(wax_id, JOB_INDEX_TOKENS, object_type, object_id)
        self.enqueue(wax_id, JOB_EMBED, object_type, object_id)

    def enqueue(
        self, wax_id: WaxId, kind: str, object_type: ObjectType, object_id: UUID
    ) -> None:
        now = self._clock.now()
        try:
            self._storage.enqueue_job(
                DerivationJob(
                    id=new_id(),
                    wax_id=wax_id,
                    kind=kind,
                    object_type=object_type,
                    object_id=object_id,
                    status=JobStatus.PENDING,
                    attempts=0,
                    run_after=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        except Exception:
            logger.exception("could not queue %s job", kind)

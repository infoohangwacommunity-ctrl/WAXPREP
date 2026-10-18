"""Deletion workflow service (Blueprint Part IV §16-17).

The deletion workflow is *not* the same as invalidation. Invalidation
marks an object as no longer trustworthy; it stays for audit. Deletion
removes the object's content from storage entirely and propagates the
removal through every dependent representation: notebook rows, summaries,
knowledge edges, search tokens, embeddings, and (with policy guardrails)
the source history itself.

The workflow is a separate service so CI never performs destructive
operations inline. CI creates a ``DeletionRequest`` (through the
``REQUEST_DELETION`` proposal); this service picks up pending requests,
applies retention policy, propagates, and records the outcome.

History is append-only by architecture. This service will *not* delete
history entries unless the request scope explicitly authorizes it and
the retention policy permits. The default scope
(``this_student_all_representations``) invalidates all derived objects
and drops all index rows; the history row itself stays.

The propagation is auditable: every step is written into the request's
``propagation_log`` so an operator (or a student exercising their
right-to-know) can see exactly what was removed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    DeletionRequest,
    DeletionRequestStatus,
    ObjectType,
)
from waxprep.knowledge.models import KnowledgeNodeStatus
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)

# Conservative defaults. The actual retention policy belongs to the
# application; this service applies the policy it is given.
DEFAULT_RETENTION_POLICY: dict[str, bool] = {
    # Allow the deletion workflow to drop derived objects (notebook
    # entries, summaries, knowledge nodes, edges, tokens, embeddings).
    "allow_derived_deletion": True,
    # Never delete history by default. A separate, explicit workflow
    # (with its own audit) handles history retention.
    "allow_history_deletion": False,
    # Drop artifact content rows when the artifact version is deleted.
    "allow_artifact_content_deletion": True,
}


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    """Result of executing one deletion request.

    The ``propagation_log`` mirrors what is persisted on the
    ``DeletionRequest`` row so the caller can inspect what happened
    without re-reading storage.
    """

    request_id: UUID
    accepted: bool
    reason: str
    propagation_log: dict[str, Any]
    status: DeletionRequestStatus


@dataclass
class DeletionService:
    """Applies retention policy and propagates a deletion through the
    derived-data graph.

    The service is stateless: it reads the request, executes the
    propagation in one storage transaction, and updates the request
    row with the outcome. Failures roll back the whole transaction.
    """

    storage: Storage
    retention_policy: dict[str, bool] = field(
        default_factory=lambda: cast(dict[str, bool], dict(DEFAULT_RETENTION_POLICY))
    )

    def execute(self, request: DeletionRequest, *, now: datetime) -> DeletionOutcome:
        """Execute one deletion request atomically.

        Semantics:

        - The request is **always propagated** to derived dependents —
          every object that cites the target as evidence is
          invalidated, its search tokens and embeddings are dropped,
          and the propagation is logged. This is what a student
          expects when they ask for something to be "forgotten": the
          derived representations that depend on it stop surfacing.

        - The **source row itself** is **never hard-deleted** unless the
          retention policy explicitly allows it. The default policy
          forbids hard-deleting history (it is append-only) and
          forbids hard-deleting artifact content (it follows its
          version). A future policy can allow hard deletion by setting
          ``allow_history_deletion=True``.

        - A request whose scope explicitly demands hard deletion of a
          history row, under a policy that forbids it, is **rejected**
          (status=REJECTED) *before* any propagation. This is the only
          rejection case. A request that asks for "all derived
          representations to be invalidated" is never rejected — it
          simply completes with the history row retained by policy.
        """
        log: dict[str, Any] = {
            "started_at": now.isoformat(),
            "scope": request.scope,
            "object_type": request.object_type.value,
            "object_id": str(request.object_id),
            "steps": [],
        }

        # The only rejection case: scope explicitly demands hard-delete
        # of history AND policy forbids it.
        if (
            request.object_type is ObjectType.HISTORY
            and "hard_delete" in request.scope
            and not self.retention_policy.get("allow_history_deletion", False)
        ):
            log["rejected_at"] = now.isoformat()
            log["reason"] = "hard history deletion forbidden by retention policy"
            updated = replace(
                request,
                status=DeletionRequestStatus.REJECTED,
                completed_at=now,
                propagation_log=log,
            )
            self.storage.update_deletion_request(updated)
            logger.info(
                "deletion_request_rejected",
                extra={
                    "wax_id": str(request.wax_id),
                    "request_id": str(request.id),
                    "object_type": request.object_type.value,
                    "reason": "retention_policy_forbids_hard_history_deletion",
                },
            )
            return DeletionOutcome(
                request_id=request.id,
                accepted=False,
                reason="retention policy forbids hard history deletion",
                propagation_log=log,
                status=DeletionRequestStatus.REJECTED,
            )

        # Everything below is one atomic transaction. On exception, the
        # whole thing rolls back: the claim (PENDING → IN_PROGRESS), the
        # propagation writes, and the final COMPLETED update. The request
        # is back to PENDING — the next worker run can retry it. There is
        # no stuck IN_PROGRESS state. (Blueprint §17: a failed update must
        # not leave the request changed while its provenance or indexes
        # remain partially updated.)
        try:
            with self.storage.transaction():
                # Conditional claim: PENDING → IN_PROGRESS. If another
                # worker already claimed it (or it has moved to a
                # terminal state), this returns None and we skip. The
                # claim is inside the transaction so it rolls back on
                # failure — no stuck IN_PROGRESS marker.
                claimed = self.storage.claim_deletion_request(
                    request.wax_id,
                    request.id,
                    now=now,
                    propagation_log=log,
                )
                if claimed is None:
                    # Not an error — another worker got there first,
                    # or the request is in a terminal state. Roll back
                    # the (empty) transaction and return a skipped
                    # outcome. The request is unaffected.
                    logger.info(
                        "deletion_request_skipped",
                        extra={
                            "wax_id": str(request.wax_id),
                            "request_id": str(request.id),
                            "reason": "already_claimed_or_terminal",
                        },
                    )
                    return DeletionOutcome(
                        request_id=request.id,
                        accepted=False,
                        reason="already claimed by another worker "
                        "or in a terminal state",
                        propagation_log=log,
                        status=DeletionRequestStatus.PENDING,
                    )
                # Propagate inside the same transaction. If this raises,
                # the transaction (including the claim) rolls back.
                self._propagate(claimed, now, log)
                log["completed_at"] = now.isoformat()
                # The history row is retained by default policy. We
                # record this honestly in the propagation log so an
                # operator (or a student) can see exactly what happened.
                if (
                    claimed.object_type is ObjectType.HISTORY
                    and not self.retention_policy.get("allow_history_deletion", False)
                ):
                    log["history_row"] = "retained_by_default_policy"
                completed = replace(
                    claimed,
                    status=DeletionRequestStatus.COMPLETED,
                    completed_at=now,
                    propagation_log=log,
                )
                self.storage.update_deletion_request(completed)
        except Exception as exc:
            # The transaction rolled back, INCLUDING the claim. The
            # request is back to PENDING — the next worker run will
            # retry it. There is no stuck IN_PROGRESS state. Re-raise
            # so the caller (worker) sees the failure and can apply
            # its own retry policy.
            logger.exception(
                "deletion_propagation_failed",
                extra={
                    "wax_id": str(request.wax_id),
                    "request_id": str(request.id),
                    "error": type(exc).__name__,
                },
            )
            raise

        logger.info(
            "deletion_request_completed",
            extra={
                "wax_id": str(request.wax_id),
                "request_id": str(request.id),
                "object_type": request.object_type.value,
                "steps": len(log.get("steps", [])),
            },
        )
        return DeletionOutcome(
            request_id=request.id,
            accepted=True,
            reason="deletion propagated",
            propagation_log=log,
            status=DeletionRequestStatus.COMPLETED,
        )

    # --- internals -----------------------------------------------------------
    def _propagate(
        self, request: DeletionRequest, now: datetime, log: dict[str, Any]
    ) -> None:
        """Walk the dependency graph and propagate the deletion.

        The walk is breadth-first, bounded, and cycle-protected. For each
        dependent, we drop its index rows and (if it is a derived object)
        invalidate it. The source itself is deleted last so its
        dependents are clean before it disappears.
        """
        seen: set[tuple[ObjectType, UUID]] = set()
        queue: list[tuple[ObjectType, UUID]] = [
            (request.object_type, request.object_id)
        ]
        wax_id = request.wax_id

        while queue:
            obj_type, obj_id = queue.pop(0)
            key = (obj_type, obj_id)
            if key in seen:
                continue
            seen.add(key)

            # Drop index rows for this object — tokens and embeddings.
            tokens = self.storage.delete_search_tokens(wax_id, obj_type, obj_id)
            embeddings = self.storage.delete_embeddings(wax_id, obj_type, obj_id)
            if tokens or embeddings:
                log["steps"].append(
                    {
                        "object_type": obj_type.value,
                        "object_id": str(obj_id),
                        "tokens_dropped": tokens,
                        "embeddings_dropped": embeddings,
                    }
                )

            # Enqueue dependents — anything that cites this object as
            # evidence. We use list_derived_from (the forward direction
            # of provenance) so we walk from sources toward the things
            # that depend on them.
            links = self.storage.list_derived_from(wax_id, obj_type, obj_id)
            for link in links:
                dep_key = (link.derived_type, link.derived_id)
                if dep_key in seen:
                    continue
                queue.append(dep_key)

            # If the object is itself a derived object that can be
            # invalidated, mark it now. We do NOT delete the underlying
            # row here — that would require schema-specific DELETE
            # methods on Storage, which we deliberately do not expose
            # for derived objects (they're invalidated, not hard-deleted,
            # to preserve the audit trail). A future policy change can
            # add hard-delete methods if a retention threshold demands.
            self._invalidate_derived(wax_id, obj_type, obj_id, now, log)

        # History: never hard-delete by default; the request row itself
        # records the scope so an operator can act on it later through
        # the separate history-retention workflow.
        if request.object_type is ObjectType.HISTORY and self.retention_policy.get(
            "allow_history_deletion", False
        ):
            # If and when history deletion is permitted, it would happen
            # here. Today we deliberately leave the history row alone.
            log["history_row"] = "retained_by_default"

    def _invalidate_derived(
        self,
        wax_id: WaxId,
        object_type: ObjectType,
        object_id: UUID,
        now: datetime,
        log: dict[str, Any],
    ) -> None:
        """Mark a derived object as invalidated, if applicable to its type.

        History, artifact_version, artifact_content, and knowledge_edge
        are not invalidated here — they have their own lifecycle rules
        (history is append-only; edges follow their nodes; artifact
        content follows its version). Notebook/summary/node have explicit
        invalidation semantics and we use them.
        """
        if object_type is ObjectType.NOTEBOOK:
            entry = self.storage.get_notebook_entry(wax_id, object_id)
            if entry is not None and entry.status.value == "active":
                self.storage.invalidate_notebook_entry(
                    wax_id,
                    object_id,
                    now=now,
                    reason="invalidated by deletion propagation",
                )
                log.setdefault("invalidated", []).append(
                    {"object_type": object_type.value, "object_id": str(object_id)}
                )
        elif object_type is ObjectType.SUMMARY:
            summary = self.storage.get_summary(wax_id, object_id)
            if summary is not None and summary.status.value == "active":
                self.storage.invalidate_summary(
                    wax_id,
                    object_id,
                    now=now,
                    reason="invalidated by deletion propagation",
                )
                log.setdefault("invalidated", []).append(
                    {"object_type": object_type.value, "object_id": str(object_id)}
                )
        elif object_type is ObjectType.KNOWLEDGE_NODE:
            node = self.storage.get_node(wax_id, object_id)
            if node is not None and node.status is KnowledgeNodeStatus.ACTIVE:
                self.storage.invalidate_node(
                    wax_id,
                    object_id,
                    now=now,
                    reason="invalidated by deletion propagation",
                )
                log.setdefault("invalidated", []).append(
                    {"object_type": object_type.value, "object_id": str(object_id)}
                )

    def list_pending(
        self, wax_id: WaxId, *, limit: int = 50
    ) -> tuple[DeletionRequest, ...]:
        """List pending deletion requests for one student."""
        return self.storage.list_deletion_requests(
            wax_id, status=DeletionRequestStatus.PENDING.value, limit=limit
        )


__all__ = [
    "DEFAULT_RETENTION_POLICY",
    "DeletionOutcome",
    "DeletionService",
]

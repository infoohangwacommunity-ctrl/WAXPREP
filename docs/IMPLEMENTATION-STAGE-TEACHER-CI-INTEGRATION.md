# Build Specification: Real Teacher + Context Intelligence + Unified History

**Repository:** `infoohangwacommunity-ctrl/WAXPREP`  
**Target base branch:** `main`  
**Intended repository path for this specification:** `docs/IMPLEMENTATION-STAGE-TEACHER-CI-INTEGRATION.md`  
**Scope:** Implement the real model-backed Teacher and integrate it end-to-end with existing Context Intelligence (CI), unified history, notebook, retrieval, model gateway, and turn orchestration.  
**Not in scope:** WhatsApp or any channel integration, artifacts/media implementation, new API/channel gateways, billing/cost controls, new usage limits beyond existing turn/CI bounds, policies/terms/privacy/admin, or unrelated refactors.

---

## 1. Mission — implement the whole stage in one pass

Implement these related work items as one coherent feature, not four disconnected patches:

1. A real Teacher implementation following the existing `Teacher` protocol and using the existing provider-neutral `ModelGateway`.
2. A model-driven Teacher ↔ CI consultation loop using the existing `TeacherConsultation` capability and bounded orchestration.
3. End-to-end use of existing unified history and derived context, including notebook entries, summaries, knowledge objects, and history.
4. Reliable response validation, persistence, and regression tests, without changing ownership boundaries.

Do not merely write a plan or leave TODOs. Inspect current `main`, implement the changes, run quality checks, commit the completed work, and push the finished implementation to `main`. You may use a temporary local working branch if required, but the completed change must land on `main`; do not leave the work only on an unpushed branch or require the user to finish the merge.

If remote branch protection or credentials prevent pushing, finish and test the implementation, then report the exact blocker and the commit/branch containing the work. Never force-push or overwrite unrelated work.

## 2. Required architecture — preserve these boundaries

```text
Student message (received by the existing application entry point)
    |
    v
Existing HistoryService / TurnService
    | save exact student text to unified history first
    | preserve existing duplicate-delivery behaviour
    v
Real model-backed Teacher
    | receives original HistoryEntry unchanged
    | decides whether context is useful
    | may ask CI a free-form investigation question
    v
Existing bounded TeacherConsultation -> CIBackedConsultation
    |
    v
Existing ContextInvestigator (model chooses retrieval capability)
    | student-scoped retrieval over history, notebook, summaries,
    | knowledge graph and other already-indexed objects
    | evidence carries time/provenance/currentness information
    v
ContextPackage / ConsultationResult returned to Teacher
    | Teacher evaluates evidence and uncertainty
    | may consult CI again within existing bound
    v
Teacher produces a final, non-empty text reply
    |
    v
Existing TurnService validates reply
    | saves one tutor HistoryEntry linked to student entry
    | creates existing outbox delivery record transactionally
    v
TurnResult returned to existing caller
```

### Ownership rules

- **Teacher owns teaching:** interpreting the current question, deciding whether CI is useful, evaluating evidence, and composing the final educational answer.
- **CI owns investigation:** finding and reporting evidence, source/provenance, temporal context, supersession/currentness and uncertainty. CI must not replace the Teacher as the student-facing tutor.
- **HistoryService/storage own persistence:** infrastructure saves student and tutor messages. The Teacher must not independently write messages to history.
- **TurnService owns coordination:** call order, consultation bounds, response validation, reply persistence, duplicate handling and existing outbox behaviour.
- **ModelGateway owns provider-neutral model execution:** do not call provider HTTP APIs directly from the Teacher or add a duplicate model abstraction.
- **Unified history is the source of truth.** Notebook, summaries, indexes, embeddings and knowledge graph are derived representations that point back to source evidence.

Do not introduce a separate chat transcript, session store, conversation ID, alternate memory database, or second persistence path. Do not replace the original student message with a summary or CI interpretation.

## 3. Repository locations to inspect and reuse

Read the current `main` versions of these files before editing. If a path has moved, find the current equivalent and update it in place rather than creating parallel components.

### Orchestration and contracts

- `src/waxprep/orchestration/contracts.py`
  - Keep `Teacher`, `TeacherInput`, `TeacherOutcome`, `TeacherConsultation`, `ConsultationRequest` and `ConsultationResult` unless a demonstrated integration gap requires a minimal compatible adjustment.
  - `TeacherInput.original_message` is the exact stored student `HistoryEntry`; preserve it.
  - CI questions remain free-form model-selected text, not values chosen by a keyword router.
- `src/waxprep/orchestration/turn.py`
  - Primary end-to-end coordinator and response-validation location.
  - Preserve save-first history, duplicate detection/recovery, one tutor reply per student entry, transaction boundaries, and outbox semantics.
  - Inspect `_run_teacher` carefully. Its forced-final-round path currently uses `outcome.reply_text or ""`, which can convert a missing response into an empty string and allow an invalid success path. Fix this so a blank/missing final response cannot be persisted as a successful tutor reply.
- `src/waxprep/orchestration/consultation.py`
  - Reuse `CIBackedConsultation`; do not bypass it to call CI from a provider adapter.
- `src/waxprep/orchestration/bounded.py`
  - Reuse the existing hard upper bound on actual CI calls per turn. Do not add a competing limit mechanism or allow an infinite loop.
- `src/waxprep/orchestration/mock_teacher.py`
  - Keep the deterministic mock for tests. It is not the production Teacher.

### Model gateway

Reuse the existing provider-neutral package:

- `src/waxprep/model_gateway/models.py`
- `src/waxprep/model_gateway/provider.py`
- `src/waxprep/model_gateway/registry.py`
- `src/waxprep/model_gateway/gateway.py`
- `src/waxprep/model_gateway/providers/` (existing OpenAI Responses, Anthropic, Upstage and mock adapters)
- `src/waxprep/model_gateway/errors.py`

Use the registered logical Teacher model configuration and `ModelRole.TEACHER`. Follow existing dependency injection and application composition patterns. Find where registry, providers, logical models and services are wired together; update that composition root rather than creating another one.

Do not assume every provider supports identical capabilities. Use gateway capability validation and the configured Teacher model's declared capabilities. The basic Teacher response path requires text only; do not add media inputs or artifact handling in this stage. If a provider returns no usable text, treat it as invalid rather than successful.

Do not add an SDK dependency just to obtain an agent loop. WAXPREP already has a provider-neutral gateway, model contracts, CI tool loop and turn orchestration. External frameworks are research references, not a mandate to replace the architecture.

### Context Intelligence, retrieval and notebook

Inspect:

- `docs/CONTEXT-INTELLIGENCE.md`
- `docs/ARCHITECTURE.md`
- `src/waxprep/context/investigator.py`
- `src/waxprep/context/model.py`
- `src/waxprep/context/models.py`
- `src/waxprep/context/system_prompt.py`
- `src/waxprep/context/tool_definitions.py`
- `src/waxprep/context/tool_impls.py`
- `src/waxprep/context/retrieval.py`
- `src/waxprep/context/evidence.py`
- `src/waxprep/orchestration/consultation.py`

The existing CI loop is model-driven: the model selects a declared capability, the application executes it, observations return to the model, and CI produces a compact context package. Keep this model/action/observation/finalization pattern. Tool failures must not erase the original saved student message.

### History and domain

Inspect:

- `src/waxprep/history/service.py`
- `src/waxprep/domain/models.py`
- `src/waxprep/storage/protocol.py` and relevant storage implementations
- `src/waxprep/index/` and `src/waxprep/embeddings/` only as needed to verify retrieval

`HistoryService.record_with_status` already persists exact history and initial indexing work and reports whether an inbound item is a duplicate. Keep it as the source of truth. `ObjectType.NOTEBOOK` is already included in indexed object types; CI retrieval has existing capabilities for retrieval, inspection, source tracing and currency checks. Verify this with tests and fix only genuine missing wiring or bugs. Do not redesign the notebook or introduce another memory layer.

## 4. Notebook connection — explicit verification requirement

The existing design models notebook entries as durable derived understanding linked to source history. The repository's retrieval design includes `ObjectType.NOTEBOOK` in `INDEXED_OBJECT_TYPES`, and CI tools use the existing `Retriever` over indexed objects. Treat this as groundwork to verify and use, not a request to invent a new notebook.

Before completion, automated tests must prove:

- [ ] A notebook entry for the same WAX ID can be found through existing CI retrieval capabilities when relevant to a Teacher consultation.
- [ ] The Teacher receives notebook evidence through existing `ConsultationResult`/prior-results contracts and can use it in the final answer.
- [ ] Evidence retains source/provenance and time context; CI does not present derived notebook content as a direct student statement.
- [ ] Superseded or potentially stale notebook information is not silently presented as unquestionably current; use existing currency/source tools and uncertainty metadata.
- [ ] A notebook entry belonging to another WAX ID is never visible through retrieval or consultation.
- [ ] If notebook entries are absent, indexing is pending, or semantic embeddings are unavailable, the turn remains valid and CI honestly reports degraded/insufficient context. Do not fabricate notebook results.
- [ ] Do not claim notebook writing/evolution was newly integrated unless the existing lifecycle actually runs and a test proves it. This stage connects the real Teacher to existing retrieval; do not build a new notebook-evolution subsystem.

If a test reveals a concrete retrieval wiring defect, fix the smallest responsible integration point and add a regression test. Do not add hardcoded notebook fields, fixed personal-profile columns, or student-specific branches.

## 5. Implement a real model-backed Teacher

Add the production implementation under `src/waxprep/orchestration/` (preferred filename: `model_teacher.py`, unless current naming conventions indicate a better location). It must implement the existing `Teacher` protocol and be injected into the existing `TurnService` composition root.

### Teacher request construction

Build a provider-neutral `ModelRequest` using the existing gateway and logical model registry. Include:

- Configured Teacher system instructions.
- Exact current student message text from `TeacherInput.original_message`, unchanged.
- Relevant, bounded prior CI results from `TeacherInput.prior_results`, including synthesis, evidence content/identifiers, timestamps, author/source information, provenance and uncertainty as available.
- Current time from `TeacherInput.now`.
- Only the minimum orchestration context needed for this turn; never dump the student's entire history or database into every prompt.
- `ModelRole.TEACHER` and correct WAX ID for model request metadata.

System instructions describe durable role and behaviour principles, not the student's presumed subjects, curriculum, language variety, greeting patterns or personal facts. Current message and CI evidence are dynamic input.

### Teacher behaviour

The model-backed Teacher must:

- Reason from the current message first and respond directly when no personal context is needed.
- Decide whether to consult CI based on the actual request and uncertainty, not a fixed list of phrases or message categories.
- When context is useful, return a `TeacherOutcome` requesting a natural-language CI investigation through the provided `TeacherConsultation`.
- Use prior consultation results in later rounds; avoid repeating the same investigation without a reason.
- Evaluate whether evidence answers the investigation request. Distinguish direct student statements from tutor-authored content and derived interpretations. Express uncertainty instead of inventing missing facts.
- Request another investigation only when needed and within the existing orchestration bound.
- Produce the final educational response itself. CI's summary is evidence for the Teacher, not the final student-facing answer.
- Finish with plain-text response represented by `TeacherOutcome(finished=True, reply_text=...)`. Validate any structured decision contract strictly.
- Respect the existing `TeacherOutcome` contract. Do not introduce an incompatible protocol or let the model write to history/storage.

Use gateway tool/structured-output features only if supported by the chosen logical model's declared capabilities. Prefer a simple validated response contract compatible with the current gateway. Do not force provider-specific features onto every provider. If the normalized gateway response cannot represent a required action, add the smallest provider-neutral extension only after checking every adapter and its tests.

### Model and prompt configuration

Follow existing logical model registry and application configuration patterns. Use a logical Teacher model name/role consistent with existing conventions; do not scatter provider model IDs across orchestration code. Reuse configuration conventions and document the actual required Teacher model setting. Do not commit API keys.

If no Teacher model is configured, fail clearly at configuration/startup or use the application's established controlled failure. Do not silently use `MockTeacher` in production. Keep mock injection for tests and explicit development/test composition.

## 6. Teacher ↔ CI integration

The real Teacher must use the existing contracts end-to-end:

- [ ] First Teacher call receives the exact `HistoryEntry` returned by save-first history.
- [ ] Teacher can answer a self-contained request without CI.
- [ ] Teacher can request CI with a targeted, free-form investigation question.
- [ ] `CIBackedConsultation` receives original message, WAX ID, current time and prior context through existing contracts.
- [ ] CI returns evidence/synthesis/uncertainty/provenance through existing consultation result.
- [ ] Next Teacher round receives the result in `prior_results` and can incorporate it into the final answer.
- [ ] Teacher can make a second investigation if necessary, but all actual calls remain counted by `BoundedConsultation`.
- [ ] On budget exhaustion, Teacher gets a clear indication that further consultation is unavailable and finishes using available evidence, with uncertainty where appropriate.
- [ ] CI failure or empty retrieval does not erase/rewrite saved student message; follow existing failure semantics.
- [ ] CI never independently generates/persists a tutor reply or bypasses Teacher.
- [ ] No new session ID, transcript store, or parallel conversation state is created.

Keep the existing default consultation bound unless code/tests demonstrate a real defect. Do not increase it as a substitute for good model decisions.

## 7. No hardcoded routing or phrase matching

This is a strict architectural requirement.

Do **not** implement application-side keyword rules, regular expressions, phrase lists, subject detectors, greeting detectors or routing branches to decide how to respond or whether CI should be consulted. Prohibited examples include special-case code for “good morning”, “good afternoon”, “hello”, subject names, curriculum terms, Nigerian Pidgin phrases, or presumed student traits.

The model decides from the actual message and evidence. The application enforces contracts, bounds, validation, persistence and security; it must not duplicate model reasoning with phrase heuristics.

Important distinction: existing `find_exact` retrieval is a generic, model-selectable way to find exact words/names/numbers in stored material; it is not a message router. Preserve generic retrieval capabilities where useful. The prohibition is against hardcoded application logic that routes the student message or forces behaviour based on keyword matches. Do not remove the existing retrieval architecture merely because lexical retrieval is one tool.

Do not hardcode:
- Student-specific facts or personal profiles.
- School subjects, curriculum, grade, exam board or country-specific syllabus behaviour.
- Language/dialect assumptions or automatic Nigerian Pidgin.
- Greeting/closing phrases or canned answers as production Teacher behaviour.
- Mappings from message phrases to CI tools.
- Fixed memory fields requiring a student to fit a predefined profile.
- Fixed sequences such as “always consult CI” or “never consult CI”.

The model may adapt naturally to a student's demonstrated language and context, based on the actual message and relevant evidence. Do not infer facts that are not present.

## 8. Response validation and failure semantics

Correct response validation in `src/waxprep/orchestration/turn.py` and the Teacher adapter where appropriate.

- A successful final response must be a string containing non-whitespace content.
- Do not use `reply_text or ""` as a success fallback.
- Do not persist an empty or whitespace-only tutor history entry.
- Model timeout, provider failure, malformed structured decision, unsupported capability, missing model configuration, missing text or invalid final outcome must not be treated as a valid student-facing reply.
- Follow existing application error-handling patterns. Surface a typed/controlled failure to the caller or established failure path; do not leak provider secrets, authorization headers or unnecessary raw provider payloads.
- Preserve the already-saved student message when Teacher generation fails. A retry must not duplicate the student history item; retain existing duplicate detection/resume behaviour.
- Do not silently replace a production Teacher with a canned response or test mock after failure.
- Keep existing atomic tutor-history + outbox transaction semantics. Only valid final text may enter that transaction.
- If `TeacherOutcome.finished` is false and has a valid next question, follow the bounded consultation flow. If an outcome is malformed or claims to be finished without valid text, handle it explicitly.
- Ensure the forced-final-round path validates the outcome, not just `reply_text`. It must not accept an unfinished outcome with no valid final text.
- Log safely with enough diagnostic context while avoiding message-content logging unless existing policy explicitly permits it.

Do not introduce billing, monetary cost tracking, user quotas or general usage-control systems. Only existing consultation and model request bounds are relevant.

## 9. Preserve existing notebook, history, retrieval and index design

Do not redesign these subsystems. Validate integration and fix only concrete defects uncovered by tests.

- Unified history remains append-only and authoritative.
- Exact student message is saved first and passed unchanged to Teacher.
- Every tutor reply is linked to the source student entry through existing `reply_to_id`.
- Notebook, summaries, embeddings, blind token indexes and knowledge graph remain derived, provenance-linked representations.
- Reuse existing exact and semantic retrieval, time-window browsing, reading around history, inspection, source tracing, currency checks and relationship traversal.
- Recency is a ranking tie-breaker, not a hard cutoff that makes old relevant evidence unreachable.
- All retrieval remains scoped to the request's WAX ID.
- Keep existing background derivation/retry behaviour. Failed embedding or summary jobs must not lose a saved message or block the reply.
- Keep author/source distinctions; tutor-authored statements must not be presented as confirmed student facts.
- Do not modify artifact extraction, artifact reading behaviour, workspace storage or media handling. Existing general CI architecture may contain an artifact tool, but this implementation must not expand or change artifact/media functionality.

## 10. Composition root and configuration

Locate the existing application composition/startup code by searching for construction of `ModelRegistry`, `ModelGateway`, `ContextInvestigator`, `CIBackedConsultation` and `TurnService`. Modify that existing wiring.

- Register or reuse the real Teacher logical model and existing provider adapter using established configuration.
- Instantiate the Teacher adapter with existing `ModelGateway` and configured logical Teacher model name.
- Inject the real Teacher into existing `TurnService`.
- Preserve test composition that injects `MockTeacher` or another deterministic fake.
- Avoid global singletons, hidden provider clients inside Teacher, duplicate registries and provider-specific API calls in orchestration.
- Update the most relevant existing documentation with actual required configuration names and how to select the logical Teacher model, after tracing current config conventions. Do not invent config names.
- Keep context model configuration separate from Teacher model configuration where supported by the current registry.

## 11. Tests to add or update

Follow current test layout and naming conventions. Use deterministic fake providers/models for normal tests. Do not make paid external model calls in the normal test suite. Live-provider tests, if any, must remain explicitly marked and excluded from the normal quality gate.

### Teacher unit tests

- [ ] Builds a provider-neutral request using configured logical Teacher model and `ModelRole.TEACHER`.
- [ ] Passes exact original student message, including punctuation, whitespace and non-English text, without rewriting.
- [ ] Handles a valid final response.
- [ ] Handles a valid CI consultation decision and produces the correct `TeacherOutcome`.
- [ ] Uses prior CI results on subsequent rounds.
- [ ] Rejects malformed structured output, unknown action values, missing required fields and blank final text.
- [ ] Handles gateway/provider failure using established error semantics.
- [ ] Does not use keyword routing or canned production answers.

### End-to-end orchestration tests

- [ ] Self-contained question: exact student message saved first, Teacher responds without CI, one tutor reply saved and linked.
- [ ] Contextual follow-up: Teacher requests CI, CI finds relevant earlier history/notebook evidence, Teacher receives evidence and uses it in final response.
- [ ] Multiple consultations: second consultation receives prior results; actual CI call count never exceeds existing bound.
- [ ] CI has no relevant evidence: Teacher does not invent personal history and handles uncertainty.
- [ ] CI fails or retrieval/embeddings unavailable: original history remains saved; turn follows controlled degradation behaviour.
- [ ] Teacher provider times out/fails: no empty or fabricated tutor entry is persisted.
- [ ] Teacher returns empty, whitespace-only or missing final text: controlled failure; no invalid tutor entry/outbox record.
- [ ] Forced-final-round outcome malformed or unfinished: it is not persisted as success.
- [ ] Duplicate inbound delivery after successful reply: existing tutor reply reused; Teacher is not called again.
- [ ] Duplicate delivery after student save but before reply persistence: resume remains safe and does not duplicate student entry.
- [ ] Notebook retrieval: same-student notebook evidence reaches Teacher with provenance and temporal context.
- [ ] Cross-student isolation: a student's consultation cannot retrieve another student's history, notebook or derived objects.
- [ ] Existing outbox transaction/recovery tests continue to pass.

Add a regression test that would fail under the current `reply_text = outcome.reply_text or ""` behaviour. Assert invalid text is never persisted.

Do not test intelligence by asserting one exact natural-language answer. Test contracts, evidence flow, provenance, non-empty response, bounded calls and persistence behaviour.

## 12. Research principles — borrow patterns, do not copy whole frameworks

- OpenAI Agents SDK documents a model-driven loop: the model returns final output or tool calls, tools execute, observations return to the model. It also distinguishes model-led from application-enforced orchestration. Borrow the loop and separation of responsibilities; keep WAXPREP's gateway and contracts.
  - https://openai.github.io/openai-agents-python/running_agents/
  - https://openai.github.io/openai-agents-python/multi_agent/
- Aider's repository-map approach demonstrates selecting relevant context instead of injecting an entire codebase. WAXPREP should use existing retrieval to select relevant student evidence instead of passing all history into every Teacher prompt.
  - https://aider.chat/docs/
  - https://github.com/Aider-AI/aider/blob/main/aider/website/docs/repomap.md
- OpenHands documents a reasoning/action/observation loop and explicit context management. Borrow the feedback loop and bounded-context principles, not its coding-agent workspace architecture.
  - https://github.com/OpenHands/docs/blob/main/sdk/arch/agent.mdx

Do not install OpenAI Agents SDK, Aider, OpenHands or another agent framework in this task. Do not replace the existing WAXPREP architecture. No external framework is needed unless repository inspection reveals a specific unavoidable requirement; if so, prove the existing gateway cannot support it before adding anything.

## 13. Strict exclusions — stop at this stage boundary

Do **not** implement or modify:

- WhatsApp inbound/outbound integration, webhook handlers, WhatsApp credentials, message delivery adapters or WhatsApp-specific retries. WhatsApp is a later stage and will be the message input/output gateway. This task ends at the existing application/turn boundary.
- Any new student-facing channel, endpoint or gateway unless a pre-existing non-channel application entry point must be wired to exercise the existing turn service. Do not expand API scope.
- Artifact creation/editing, workspace behaviour, file uploads, media/image/audio processing, extraction or artifact tools.
- Cost tracking, pricing, billing, token-budget product features, quotas or usage-limit systems.
- Terms, privacy/policy pages, admin tools, account management, payments, deployment/scaling changes.
- A new memory database, chat/session ID, separate transcript, fixed profile schema or notebook redesign.
- Curriculum-specific, subject-specific, country-specific or language/dialect-specific hardcoded behaviour.
- Wholesale replacement of model gateway, storage, CI loop, history/index design or orchestration architecture.
- Unrelated refactors, formatting churn or dependency upgrades.

If a required implementation seems to need an excluded area, do not implement it. Complete this stage through existing interfaces and report the dependency clearly.

## 14. Definition of done — every applicable box required

### Architecture and implementation

- [ ] A real model-backed Teacher implements the current `Teacher` protocol.
- [ ] The real Teacher uses the existing `ModelGateway` and logical model registry.
- [ ] Existing application composition injects the real Teacher for production; mocks remain available for tests.
- [ ] Teacher drives CI consultation through existing capability and bounded wrapper.
- [ ] Relevant history, notebook, summaries and knowledge evidence can reach Teacher through existing CI retrieval.
- [ ] No second conversation/memory system or duplicate persistence path exists.
- [ ] Exact student message is saved first and passed unchanged to Teacher.
- [ ] Only validated, non-empty Teacher text is persisted as a tutor reply.
- [ ] Existing reply linking, duplicate handling, transaction and outbox semantics remain intact.
- [ ] No application-side keyword/phrase routing or hardcoded student-specific behaviour was added.
- [ ] WhatsApp, artifacts/media and all excluded areas remain untouched.

### Verification

- [ ] New Teacher unit tests pass.
- [ ] New Teacher/CI/history/notebook integration tests pass.
- [ ] Failure, empty-response, forced-final-round and duplicate-delivery regressions are covered.
- [ ] Existing tests pass.
- [ ] Run repository's documented quality gate; at minimum run applicable tests, Ruff lint/checks and Pyright/type checking according to current conventions. Run `make check` if it exists; inspect the Makefile rather than assuming.
- [ ] Normal tests do not require paid live API calls.
- [ ] Relevant docs reflect the real Teacher implementation and actual configuration.
- [ ] Final diff is reviewed for scope creep and accidental edits to excluded areas.
- [ ] Commit finished work and push to remote `main` when permissions permit.

### Final report from the coding agent

Provide a concise report with:
1. Files changed and purpose of each.
2. How notebook retrieval was verified and which tests prove it.
3. Tests/checks run and exact outcomes.
4. Commit SHA and whether the commit is on remote `main`.
5. Any genuine blocker or deferred item. Do not claim a check passed if it was not run.

---

## 15. Final implementation principle

**The infrastructure remembers. Context Intelligence investigates. The Teacher teaches. The orchestration layer coordinates. The database preserves the truth.**

Implement this complete stage once, within the existing architecture, and stop at the scope boundary above.

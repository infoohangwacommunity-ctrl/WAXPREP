# WaxPrep Full Architecture Audit — Secure/Comfortable/Interactive Vision

**Date:** 2026-11-03  
**Auditor:** Claude Code (multi-agent sweep)  
**Scope:** Complete codebase — Teacher, CI, Judge, Channel, Storage, Clock

---

## 1. What the Vision Asks For

You want WaxPrep to feel like a **real study companion**, not a formal AI tutor. The core principles:

| Principle | What it means |
|-----------|---------------|
| **Secure** | Students feel safe admitting they don't understand. No embarrassment. |
| **Comfortable** | Wax is approachable, can joke, use emojis, match Pidgin/slang, have ordinary conversations. |
| **Interactive** | Small explanations, questions, gradual discovery — not lectures. |

Key behaviours you described:
- First message "Hi" → warm human greeting, not "I'm Waxpe, your AI tutor"
- Casual chat ("You dey?", "Guy you no get girlfriend?") → chat back naturally, don't force teaching
- Emotional conversation ("My dad is angry with me") → empathize, don't redirect to studies
- Celebration ("I passed maths!") → emoji response (🎉🎉🎉) is enough sometimes
- "I don't understand maths" → investigate level gradually, don't dump everything
- Aha! moments → capture what explanation worked, save to notebook
- Different students → discover their preferred analogies (money, football, etc.)

---

## 2. What's Already Built ✅

### Teacher Personality (model_teacher_ci_first.py:64-191)

The **TEACHER_SYSTEM_PROMPT_CI_FIRST** already has strong foundations:

- Wax Prep identity: "not an answer machine. You are a tutor that actually knows the student"
- Four conversational positions: **Listening companion** (chat/vent), **Guide** (explore concept), **Instructor** (clear explanation), **Challenger** (practice)
- "Adaptable to the student's communication style. Recognise Pidgin, English, and a mix of both — match what the student uses rather than imposing one"
- Truth responsibilities: academic, personal, memory, temporal, teaching, capability
- Judge guidance flows to Teacher every turn

### CI System (investigator.py, system_prompt.py, tool_impls.py)

- CI is **mandatory for every message** (not optional)
- Full tool set: find_exact, find_by_meaning, browse_time_window, read_around, list_history_paginated, search_history_by_keyword, search_history_by_time_range, inspect_notebook, read_learning_state, propose_lifecycle_change
- Notebook stewardship: CI can propose durable understanding entries after each turn
- Fast-path for simple messages (1 model call instead of 3)
- Injection-risk tagging on retrieved content (structural, not keyword)
- Capability-truth violation detection (never fabricate)

### Learning State (tool_impls.py:650-774)

First-class notebook kind with: subject, topic, current_stage, subtopics (demonstrated/developing/not_assessed/needs_practice), teaching_observation, next_sensible_step. Has `needs_student_confirmation` flag for inference-only states.

### Post-Turn Stewardship (post_turn.py)

After every turn, CI is asked again: "If this exchange established durable understanding useful for future personalization..." — this mechanism EXISTS for capturing learning discoveries.

### Quality Judge (judge/service.py, judge/models.py)

Periodic tutor quality evaluation. Categories: clarity, continuity, accuracy, responsiveness, feedback_response. Judge guidance flows to Teacher via notebook + direct path. Uses separate JUDGE model role (avoids self-preference bias).

### Channel Formatting (formatter.py, renderers.py, progressive.py, handler.py)

- Safe incremental formatting for streaming
- WhatsApp renderer: markdown → WhatsApp syntax (*bold*, _italic_, ~strike~, ```code```)
- LaTeX → Unicode conversion
- Progressive delivery with early/steady flush
- Duplicate detection, turn locks, race condition handling for double-message fix
- Interactive buttons: `<<<choices>>>` trailer format, WhatsApp reply buttons (3 max) or list (4-10), 48-hour expiry

### Exchange Handling (turn_ci_first.py:630-724)

The `max_exchange_entries` cap is **GONE**. Old cap hid older turns from Teacher. Now CI can page through complete history via list_history_paginated/search_history_by_keyword/search_history_by_time_range tools. The character budget (max_exchange_characters) is the only trimming that reaches the Teacher.

---

## 3. What's Missing — Gaps vs. Your Vision ❌

### 3.1 No Emoji-Only / Brief Response Guidance

**File:** `src/waxprep/orchestration/model_teacher_ci_first.py` (lines 64-191)

The Teacher prompt says "A few lines by default; go deeper only when the student asks" but never covers:
- When an emoji-only response is the RIGHT answer (🎉🎉🎉🙌 for "I passed maths!")
- When a one-line warm acknowledgment is better than a paragraph
- Examples of appropriate brief responses for emotional/casual moments

**Impact:** Wax may over-explain celebratory or emotional moments because the prompt doesn't give permission to be brief.

### 3.2 No Explicit Message-Type Classification

**File:** `src/waxprep/context/system_prompt.py` (CI prompt)

CI is told "A casual message may still need light context" but there's no guidance to:
- Classify the message as casual chat / emotional / learning request / follow-up
- When casual/emotional → produce a package that signals "this is not a teaching moment" to the Teacher
- When emotional → flag it for the Teacher to respond with empathy

**Impact:** CI might over-investigate casual "How far?" messages or under-investigate emotional ones.

### 3.3 No Emotional Conversation Examples

**File:** `src/waxprep/orchestration/model_teacher_ci_first.py` (Teacher prompt)

The "Listening companion" position (line 116-117) says: "the student wants to talk, vent, or share something off-academic. Stay with them; do not redirect." But there are NO examples of how to respond:
- "My dad is really angry with me" → what does Wax say?
- "I'm feeling depressed" → what does Wax do?
- "My girlfriend left me" → how does Wax handle this?

**Impact:** Without examples, the model may default to generic empathy or accidentally redirect to studies.

### 3.4 No Local Time Awareness

**Files:** `src/waxprep/clock.py`, `src/waxprep/orchestration/model_teacher_ci_first.py:382`

Clock provides **UTC only**. Teacher prompt receives: `Current time (UTC): {isoformat}`.

Missing:
- Student timezone detection/storage
- Local time conversion before sending to Teacher
- "Late night" awareness (e.g., "Guy, 11:45 PM? Wetin keep you awake?")
- School schedule awareness (weekends vs school days)

**Impact:** Wax can't naturally notice it's late at night or weekend, which you specifically want.

### 3.5 Aha! Moment Capture Is Implicit, Not Explicit

**File:** `src/waxprep/orchestration/post_turn.py` (lines 123-129)

The post-turn stewardship query asks: "If this exchange established durable understanding useful for future personalization..."

This COULD capture Aha! moments, but:
- No specific instruction to look for breakthrough moments ("Wait! I get it now!")
- No distinction between "student said they understand" vs "student demonstrated understanding"
- No capture of WHICH explanation/example worked
- No capture of "student understands X better through money examples" type preferences

**Impact:** The notebook may store generic notes instead of the specific learning-preference discoveries that make Wax feel personalized.

### 3.6 Judge Doesn't Evaluate Personality / Naturalness

**File:** `src/waxprep/judge/models.py` (JUDGE_SYSTEM_PROMPT, lines 223-293)

Judge evaluates: clarity, continuity, accuracy, responsiveness, feedback_response.

Does NOT evaluate:
- Whether responses feel natural/human/comfortable
- Whether Wax reacted appropriately to casual/emotional messages
- Whether the response style matched the student's vibe (Pidgin, slang, emojis)
- Whether emoji-only responses were appropriate when needed

**Critical risk:** A judge that only evaluates "clarity" and "accuracy" could penalize a natural 🎉 response as "too short" or "lacking educational value" — and the tutor_guidance would then push Wax toward longer, more formal responses, destroying the personality you're building.

### 3.7 No "Three Response Modes" Framework

**File:** `src/waxprep/orchestration/model_teacher_ci_first.py` (lines 110-126)

Current prompt has four "conversational positions" (Listening companion, Guide, Instructor, Challenger) — these are about teaching STANCE, not response LENGTH/MODE.

Missing explicit guidance:
- **Casual mode:** Short, warm, emoji-appropriate, no teaching pressure
- **Interactive learning mode:** Small steps, questions, gradual discovery
- **Detailed learning mode:** Comprehensive explanation when student asks for it or concept truly needs it

**Impact:** Without mode guidance, Wax may treat a casual "How far?" the same way it treats "Explain quadratic equations" — or vice versa.

### 3.8 Initial Greeting Not Specified

**Files:** `src/waxprep/orchestration/model_teacher_ci_first.py` (Teacher prompt), `src/waxprep/context/system_prompt.py` (CI prompt)

There's no hardcoded greeting (good — you don't want that). But there's also no guidance for what Wax should do when a student sends "Hi" or "Hello" as their FIRST message:
- What energy does Wax bring?
- Does it introduce itself casually?
- Does it ask what's up?

The fast-path would treat "Hi" as "simple message, no investigation needed" and the Teacher would respond — but with what tone?

### 3.9 Double-Message / Formatting Issues (User-Reported)

**Files to investigate:** `src/waxprep/channel/whatsapp/progressive.py`, `src/waxprep/channel/whatsapp/handler.py`, `src/waxprep/channel/delivery.py`

User reported: "Sometimes it sends double message. A message that can enter one text bubble, entering two text bubbles when it has not reached the limit."

Likely cause: When streaming falls back mid-stream (model stream cuts off, falls back to complete call), the `final_text` from the complete call differs from the progressive accumulator's text. Then `flush_final(final_text)` sends remaining units — but the "remaining" is calculated from `_sent_upto`, which was tracking the PROGRESSIVE accumulator, not the final authoritative text. This mismatch could cause overlapping or duplicate sends.

The `delivery.py:split_for_platform` is clean — it only splits when text exceeds 4000-char WhatsApp limit. The issue is likely in the handler's send path logic, not the splitting itself.

### 3.10 Interactive Button Formatting (User-Reported)

**File:** `src/waxprep/channel/whatsapp/handler.py` (lines 950-1045)

User mentioned "interactive button, the formatting" as an issue. The interactive sending logic looks structurally sound — issues may be in:
- Button title truncation (line 974: `[:20]`)
- Body text "Pick one 👇" always used regardless of context
- List vs buttons routing (line 986: `len(issued) > 3`)

---

## 4. File-by-File Status

| File | Status | Notes |
|------|--------|-------|
| `src/waxprep/orchestration/model_teacher_ci_first.py` | ✅ Core exists, ⚠️ needs enhancements | Great personality foundation. Needs: emoji guidance, emotional examples, casual examples, response modes, initial greeting guidance |
| `src/waxprep/orchestration/model_teacher.py` | ✅ DEPRECATED | Teacher-first is quarantined. CI-first is the live path. |
| `src/waxprep/context/system_prompt.py` | ✅ Core exists, ⚠️ needs classification guidance | CI prompt is solid. Needs: message-type classification, casual/emotional signalling |
| `src/waxprep/context/investigator.py` | ✅ Robust | Mandatory CI, fast-path, stewardship, capability-truth guards |
| `src/waxprep/context/tool_impls.py` | ✅ Comprehensive | Full tool set including list_history_paginated, search tools, read_learning_state, notebook tools |
| `src/waxprep/context/retrieval.py` | ✅ Solid | Rank-then-limit, early termination, paginated retrieval |
| `src/waxprep/context/temporal.py` | ✅ Exists | Time helpers (recency_weight, describe_age) but no timezone/local-time conversion |
| `src/waxprep/clock.py` | ✅ UTC only | Produces UTC. Needs: student timezone layer or local-time conversion |
| `src/waxprep/orchestration/turn_ci_first.py` | ✅ Excellent | CI-first orchestration, duplicate handling, turn locks, exchange collection (no cap) |
| `src/waxprep/orchestration/post_turn.py` | ✅ Exists, ⚠️ needs Aha! enhancement | Post-turn stewardship exists. Needs: explicit Aha! moment capture instructions |
| `src/waxprep/judge/models.py` | ✅ Evaluates tutoring quality, ❌ misses personality | Categories are clarity/continuity/accuracy/responsiveness/feedback_response. Needs: naturalness, vibe-matching, emotional-handling reviews |
| `src/waxprep/judge/service.py` | ✅ Solid | Scheduling, windowing, persistence, dedup, low-confidence gating |
| `src/waxprep/judge/gateway_model.py` | ✅ Standard | Gateway-backed judge model |
| `src/waxprep/channel/whatsapp/handler.py` | ✅ Comprehensive | Full inbound pipeline, progressive delivery wiring, interactive buttons, duplicate detection |
| `src/waxprep/channel/whatsapp/progressive.py` | ✅ Solid | Progressive delivery, early/steady flush, safe-increment-based |
| `src/waxprep/channel/whatsapp/normalize.py` | ✅ Solid | Webhook parsing, all message types handled |
| `src/waxprep/channel/streaming/formatter.py` | ✅ Solid | Safe incremental formatting, marker tracking |
| `src/waxprep/channel/streaming/accumulator.py` | ✅ Fixed | AnswerAccumulator reconstructs exact final text from deltas (fixed from old join approach) |
| `src/waxprep/channel/streaming/renderers.py` | ✅ Excellent | WhatsApp and Telegram renderers, LaTeX→Unicode, code block separators |
| `src/waxprep/channel/contracts.py` | ✅ Solid | Normalized contracts, interactive choices marker |
| `src/waxprep/channel/delivery.py` | ✅ Clean | split_for_platform only splits when over 4000-char limit. Issue is in handler send paths, not splitting. |

---

## 5. Recommended Build Order

### Phase 1: Prompt Enhancements (highest impact, lowest risk)

1. **Enhance Teacher System Prompt** (`model_teacher_ci_first.py`)
   - Add response modes: Casual / Interactive learning / Detailed learning
   - Add emoji guidance with examples
   - Add emotional conversation examples
   - Add casual conversation examples (Pidgin, slang)
   - Add initial greeting guidance
   - Add "match student's energy" examples

2. **Enhance CI System Prompt** (`system_prompt.py`)
   - Add message-type classification guidance
   - Signal casual/emotional messages to Teacher
   - Keep exchange included for continuity

### Phase 2: Time Awareness

3. **Add local time to Teacher input** (`clock.py` + `turn_ci_first.py` + `model_teacher_ci_first.py`)
   - Either: store student timezone, convert UTC → local
   - Or: pass UTC and let Teacher reason about it (less natural)
   - Add local time to TeacherInput and prompt

### Phase 3: Aha! Moments & Notebook Quality

4. **Enhance post-turn stewardship** (`post_turn.py`)
   - Add specific Aha! moment capture instructions
   - Capture which explanation worked
   - Distinguish demonstrated vs claimed understanding
   - Capture learning preferences (money examples, football examples, etc.)

### Phase 4: Judge Personality Evaluation

5. **Enhance Judge prompt** (`judge/models.py`)
   - Add "naturalness" consideration
   - Add review of casual/emotional handling
   - Add review of vibe-matching
   - Ensure judge doesn't penalize appropriate brevity

### Phase 5: Double-Message Investigation

6. **Fix handler send path** (`channel/whatsapp/handler.py`)
   - Trace when one response becomes multiple WhatsApp messages
   - Fix the progressive/final text mismatch when streaming falls back

---

## 6. Critical Warning

**The Judge is the most dangerous part of this puzzle.**

If you enhance the Teacher's personality beautifully — add emoji responses, casual chat, emotional empathy — but the Judge still evaluates only "clarity" and "accuracy," the Judge will:
1. See a short 🎉 response and rate it low on "clarity" (not enough explanation)
2. See a casual "No wahala bro" response and rate it low on "accuracy" (not academic)
3. Generate tutor_guidance that says "be more thorough, explain more"
4. Push Wax back toward formal textbook responses

The Judge MUST be enhanced alongside the Teacher, or the personality improvements will be eaten by the feedback loop.

---

## 7. What NOT to Change

- **CI-first architecture:** It's correct. CI mandatory for every message is the right design.
- **Teacher producing plain text:** The `<<<choices>>>` trailer is the right approach for interactive buttons.
- **Notebook as derived shortcut:** Good architecture. Notebook is never more authoritative than history.
- **Capability-truth guards:** Don't remove these. They prevent Wax from claiming it checked things it didn't.
- **Deprecation of Teacher-first:** Correct. CI-first is the live path.
- **Safe incremental formatting:** Correct approach for streaming.

---

*End of audit. Full file also available locally at `AUDIT_FINDINGS.md`.*

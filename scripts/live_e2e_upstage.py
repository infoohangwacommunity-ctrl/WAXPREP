"""Live end-to-end conversations against real Upstage + repository services."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waxprep.clock import SystemClock
from waxprep.context.gateway_model import GatewayContextModel
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import Student, StudentStatus
from waxprep.embeddings.http import HttpEmbeddingProvider
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import ModelRole
from waxprep.model_gateway.providers.upstage import UpstageResponsesProvider
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry
from waxprep.security.cipher import AesGcmFieldCipher, KeyRing
from waxprep.storage.memory import InMemoryStorage
from waxprep.wiring import build_services

API_KEY = os.environ.get("UPSTAGE_API_KEY") or ""
MODEL = os.environ.get("WAXPREP_LIVE_MODEL", "solar-mini")
EMBED_MODEL = os.environ.get(
    "WAXPREP_LIVE_EMBED_MODEL", "solar-embedding-1-large-query"
)
BASE = "https://api.upstage.ai/v1"


@dataclass
class TurnRecord:
    user_label: str
    turn_index: int
    student_message: str
    tutor_reply: str | None
    consultations: int
    error: str | None = None


@dataclass
class RunReport:
    turns: list[TurnRecord] = field(default_factory=list)
    embed_ok: int = 0
    embed_fail: int = 0

    @property
    def ok(self) -> int:
        return sum(1 for t in self.turns if t.error is None and t.tutor_reply)

    @property
    def failed(self) -> int:
        return len(self.turns) - self.ok


def make_cipher() -> AesGcmFieldCipher:
    return AesGcmFieldCipher(KeyRing(keys={1: b"t" * 32}, active_key_id=1))


def make_gateway(api_key: str) -> ModelGateway:
    provider = UpstageResponsesProvider(api_key=api_key, base_url=BASE)
    registry = ModelRegistry()
    registry.register_provider(provider)
    caps = provider.capabilities(MODEL)
    registry.register_model(
        ModelConfig(
            name="teacher",
            provider="upstage",
            model=MODEL,
            role=ModelRole.TEACHER,
            capabilities=caps,
        )
    )
    registry.register_model(
        ModelConfig(
            name="context",
            provider="upstage",
            model=MODEL,
            role=ModelRole.CONTEXT,
            capabilities=caps,
        )
    )
    return ModelGateway(registry, max_retries=1, retry_delay_seconds=0.5)


MULTI = [
    (
        "Ada-SS1",
        [
            "Hello, I am Ada. I am in SS1 and I struggle with algebra.",
            "Can you explain what a variable is with a simple example?",
            "Using that, how do I solve x + 5 = 12?",
            "What if it is 2x + 5 = 12 instead?",
            "Please quiz me with one short question on this.",
        ],
    ),
    (
        "Chidi-JSS3",
        [
            "Hi, my name is Chidi. I need help with basic science.",
            "What is photosynthesis in simple English?",
            "Where does photosynthesis happen in the plant?",
            "Earlier you mentioned chlorophyll — why is it important?",
        ],
    ),
    (
        "Fatima-SS2",
        [
            "I am Fatima in SS2. Help me with English comprehension.",
            "What does the word 'infer' mean in exam questions?",
            "Give me one short paragraph and ask me what I can infer.",
        ],
    ),
    (
        "Emeka-math",
        [
            "Please help me revise simultaneous equations.",
            "Solve: x + y = 10 and x - y = 2. Show steps.",
            "Can you check: is x=6, y=4 correct for that?",
        ],
    ),
    (
        "Ngozi-history",
        [
            "I am preparing for WAEC history.",
            "When did Nigeria gain independence?",
            "Who was the first Prime Minister of Nigeria?",
            "Remind me what year independence was — I forgot.",
        ],
    ),
    (
        "Tunde-chem",
        [
            "Explain atoms vs molecules briefly.",
            "Is water a molecule or an atom?",
            "Based on what you said, give one more example of a molecule.",
        ],
    ),
    (
        "Aisha-english",
        [
            "Help me write a better topic sentence.",
            "My topic is 'benefits of reading every day'.",
            "Improve this: Reading is good for students.",
        ],
    ),
    (
        "Kelechi-geo",
        [
            "What is the capital of Nigeria?",
            "Is Lagos still the capital?",
            "Name one river in Nigeria.",
        ],
    ),
    (
        "Blessing-bio",
        [
            "What is the function of the heart?",
            "How is that different from the lungs?",
            "Summarize both organs in two sentences.",
        ],
    ),
    (
        "Ibrahim-physics",
        [
            "Define force in physics simply.",
            "What is the unit of force?",
            "Give one everyday example of force.",
        ],
    ),
]

SINGLES = [
    ("S1", "What is 7 times 8?"),
    ("S2", "Spell the word necessary."),
    ("S3", "Name three primary colors."),
    ("S4", "What is a noun? Give two examples."),
    ("S5", "Convert 1/2 to a decimal."),
    ("S6", "Who wrote Things Fall Apart?"),
    ("S7", "What is H2O?"),
    ("S8", "Explain friction in one sentence."),
    ("S9", "What is the past tense of go?"),
    ("S10", "Calculate the perimeter of a square with side 4cm."),
    ("S11", "What does WAEC stand for?"),
    ("S12", "Is the sun a star?"),
    ("S13", "Name the largest planet in the solar system."),
    ("S14", "What is 15% of 200?"),
    ("S15", "Define democracy briefly."),
    ("S16", "What is an ecosystem?"),
    ("S17", "Solve 3x = 21."),
    ("S18", "What is the chemical symbol for gold?"),
    ("S19", "Name one Nigerian ethnic group."),
    ("S20", "What is the boiling point of water in Celsius?"),
    ("S21", "Give a synonym for happy."),
    ("S22", "What is a triangle?"),
    ("S23", "Explain gravity simply."),
    ("S24", "What is the opposite of hot?"),
    ("S25", "How many continents are there?"),
    ("S26", "What is photosynthesis needed for?"),
    ("S27", "Define verb with an example."),
    ("S28", "What is 2 to the power of 5?"),
    ("S29", "Name a renewable energy source."),
    ("S30", "Hello, I am a new student. How can you help me?"),
    ("S31", "I failed math last term. Encourage me and suggest one study tip."),
    ("S32", "Explain BODMAS with one example."),
    ("S33", "What is the difference between area and perimeter?"),
    ("S34", "List two safety rules in a school lab."),
    ("S35", "What is a fraction?"),
    ("S36", "Give an example of a proper fraction."),
    ("S37", "What is the capital of Ghana?"),
    ("S38", "Why do we need sleep? One reason."),
    ("S39", "What is an adjective? Give an example."),
    ("S40", "Convert 100cm to metres."),
]


async def run() -> RunReport:
    if not API_KEY:
        raise SystemExit("UPSTAGE_API_KEY required")
    report = RunReport()
    gateway = make_gateway(API_KEY)
    embedder = HttpEmbeddingProvider(api_key=API_KEY, model=EMBED_MODEL, base_url=BASE)
    storage = InMemoryStorage()
    clock = SystemClock()
    services = build_services(
        storage=storage,
        cipher=make_cipher(),
        clock=clock,
        context_model=GatewayContextModel(gateway, model_name="context"),
        gateway=gateway,
        teacher_model_name="teacher",
        embedder=embedder,
        max_consultations=2,
    )

    for phrase in [
        "photosynthesis",
        "quadratic equations",
        "Nigeria independence 1960",
    ]:
        try:
            vec = await embedder.embed(phrase)
            assert len(vec) > 10
            report.embed_ok += 1
            print(f"[embed ok] dim={len(vec)} {phrase!r}")
        except Exception as exc:
            report.embed_fail += 1
            print(f"[embed FAIL] {phrase!r}: {exc}")

    async def one_turn(
        wax: WaxId, user_label: str, turn_index: int, message: str, ref: str
    ) -> TurnRecord:
        try:
            result = await services.turns.handle_student_message(
                wax, message, channel="live-test", channel_ref=ref
            )
            reply = result.tutor_entry.text if result.tutor_entry else None
            rec = TurnRecord(
                user_label, turn_index, message, reply, result.consultations_made
            )
            if not reply or not str(reply).strip():
                rec.error = "empty tutor reply"
            return rec
        except Exception as exc:
            return TurnRecord(
                user_label, turn_index, message, None, 0, f"{type(exc).__name__}: {exc}"
            )

    for label, messages in MULTI:
        wax = new_wax_id()
        now = datetime.now(tz=UTC)
        storage.create_student(
            Student(
                wax_id=wax, created_at=now, updated_at=now, status=StudentStatus.ACTIVE
            )
        )
        print(f"\n===== USER {label} =====")
        for i, msg in enumerate(messages, start=1):
            rec = await one_turn(wax, label, i, msg, f"{label}-{i}")
            report.turns.append(rec)
            status = "OK" if not rec.error else f"FAIL:{rec.error}"
            print(f"[{status}] {label} T{i}")
            print(f"  STUDENT: {msg}")
            print(f"  TUTOR:   {(rec.tutor_reply or '')[:600]}")
            print(f"  consultations={rec.consultations}")
            await asyncio.sleep(0.25)

    for label, msg in SINGLES:
        wax = new_wax_id()
        now = datetime.now(tz=UTC)
        storage.create_student(
            Student(
                wax_id=wax, created_at=now, updated_at=now, status=StudentStatus.ACTIVE
            )
        )
        rec = await one_turn(wax, label, 1, msg, f"{label}-1")
        report.turns.append(rec)
        status = "OK" if not rec.error else f"FAIL:{rec.error}"
        print(f"\n[{status}] {label}")
        print(f"  STUDENT: {msg}")
        print(f"  TUTOR:   {(rec.tutor_reply or '')[:400]}")
        await asyncio.sleep(0.2)

    return report


def main() -> None:
    report = asyncio.run(run())
    print("\n========== SUMMARY ==========")
    print(f"turns total: {len(report.turns)}")
    print(f"success: {report.ok}")
    print(f"failed: {report.failed}")
    print(f"embeddings ok/fail: {report.embed_ok}/{report.embed_fail}")
    fails = [t for t in report.turns if t.error or not t.tutor_reply]
    if fails:
        print("\nFailures:")
        for t in fails:
            print(f"  - {t.user_label} T{t.turn_index}: {t.error or 'no reply'}")
    Path("live_e2e_report.json").write_text(
        json.dumps(
            [
                {
                    "user": t.user_label,
                    "turn": t.turn_index,
                    "student": t.student_message,
                    "tutor": t.tutor_reply,
                    "consultations": t.consultations,
                    "error": t.error,
                }
                for t in report.turns
            ],
            indent=2,
            ensure_ascii=False,
        )
    )
    print("Wrote live_e2e_report.json")
    if report.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

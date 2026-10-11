from __future__ import annotations
import asyncio, json, os, sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
sys.path.insert(0, "src")
from waxprep.clock import SystemClock
from waxprep.context.gateway_model import GatewayContextModel
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import Student, StudentStatus
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import ModelRole
from waxprep.model_gateway.providers.upstage import UpstageResponsesProvider
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry
from waxprep.security.cipher import AesGcmFieldCipher, KeyRing
from waxprep.storage.memory import InMemoryStorage
from waxprep.wiring import build_services

API=os.environ["UPSTAGE_API_KEY"]
MODEL="solar-mini"

@dataclass
class T:
    user: str; i: int; student: str; tutor: str|None; cons: int; err: str|None=None

async def main():
    provider=UpstageResponsesProvider(api_key=API)
    reg=ModelRegistry(); reg.register_provider(provider)
    caps=provider.capabilities(MODEL)
    for name, role in [("teacher", ModelRole.TEACHER), ("context", ModelRole.CONTEXT)]:
        reg.register_model(ModelConfig(name=name, provider="upstage", model=MODEL, role=role, capabilities=caps))
    gw=ModelGateway(reg, max_retries=1)
    storage=InMemoryStorage()
    services=build_services(storage=storage, cipher=AesGcmFieldCipher(KeyRing(keys={1:b"t"*32}, active_key_id=1)), clock=SystemClock(), context_model=GatewayContextModel(gw, model_name="context"), gateway=gw, max_consultations=2)
    users=[
      ("Aisha-english", ["Help me write a better topic sentence.", "My topic is benefits of reading every day.", "Improve this: Reading is good for students."]),
      ("Kelechi-geo", ["What is the capital of Nigeria?", "Is Lagos still the capital?", "Name one river in Nigeria."]),
      ("Blessing-bio", ["What is the function of the heart?", "How is that different from the lungs?", "Summarize both organs in two sentences."]),
      ("Ibrahim-physics", ["Define force in physics simply.", "What is the unit of force?", "Give one everyday example of force."]),
    ]
    singles=[(f"S{i}", m) for i,m in enumerate([
        "What is 7 times 8?","Spell the word necessary.","Name three primary colors.","What is a noun? Give two examples.",
        "Convert 1/2 to a decimal.","Who wrote Things Fall Apart?","What is H2O?","Explain friction in one sentence.",
        "What is the past tense of go?","Calculate the perimeter of a square with side 4cm.","What does WAEC stand for?",
        "Is the sun a star?","Name the largest planet in the solar system.","What is 15% of 200?","Define democracy briefly.",
        "What is an ecosystem?","Solve 3x = 21.","What is the chemical symbol for gold?","Name one Nigerian ethnic group.",
        "What is the boiling point of water in Celsius?","Give a synonym for happy.","What is a triangle?",
        "Explain gravity simply.","What is the opposite of hot?","How many continents are there?",
        "What is photosynthesis needed for?","Define verb with an example.","What is 2 to the power of 5?",
        "Name a renewable energy source.","Hello, I am a new student. How can you help me?",
        "I failed math last term. Encourage me and suggest one study tip.","Explain BODMAS with one example.",
        "What is the difference between area and perimeter?","List two safety rules in a school lab.",
        "What is a fraction?","Give an example of a proper fraction.","What is the capital of Ghana?",
        "Why do we need sleep? One reason.","What is an adjective? Give an example.","Convert 100cm to metres.",
    ], start=1)]
    results=[]
    async def turn(wax, user, i, msg, ref):
        try:
            r=await services.turns.handle_student_message(wax, msg, channel="live-test", channel_ref=ref)
            rep=r.tutor_entry.text if r.tutor_entry else None
            err=None if rep and str(rep).strip() else "empty"
            return T(user,i,msg,rep,r.consultations_made,err)
        except Exception as e:
            return T(user,i,msg,None,0,f"{type(e).__name__}: {e}")
    for label, msgs in users:
        wax=new_wax_id(); now=datetime.now(tz=UTC)
        storage.create_student(Student(wax_id=wax, created_at=now, updated_at=now, status=StudentStatus.ACTIVE))
        print(f"\n===== {label} =====")
        for i,msg in enumerate(msgs,1):
            rec=await turn(wax,label,i,msg,f"{label}-{i}"); results.append(rec)
            print(f"[{'OK' if not rec.err else 'FAIL:'+str(rec.err)}] {label} T{i}")
            print(f"  STUDENT: {msg}")
            print(f"  TUTOR:   {(rec.tutor or '')[:500]}")
            await asyncio.sleep(0.2)
    for label,msg in singles:
        wax=new_wax_id(); now=datetime.now(tz=UTC)
        storage.create_student(Student(wax_id=wax, created_at=now, updated_at=now, status=StudentStatus.ACTIVE))
        rec=await turn(wax,label,1,msg,f"{label}-1"); results.append(rec)
        print(f"[{'OK' if not rec.err else 'FAIL:'+str(rec.err)}] {label}: {msg}")
        print(f"  TUTOR: {(rec.tutor or '')[:300]}")
        await asyncio.sleep(0.15)
    ok=sum(1 for r in results if not r.err)
    print(f"\nRESUME SUMMARY total={len(results)} ok={ok} fail={len(results)-ok}")
    Path("live_e2e_resume.json").write_text(json.dumps([r.__dict__ for r in results], indent=2, ensure_ascii=False))

asyncio.run(main())

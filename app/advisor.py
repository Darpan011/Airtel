"""The conversational advisor — a guided slot-filling journey.

Design note, and the reason this survives unseen test scenarios:

A free-form chatbot can wander, never terminate, or invent a product. So the
conversation is a *state machine* that owns which question comes next and when
the conversation ends. The LLM is used only to understand what the owner wrote
and, at the end, to phrase the recommendation. The rules engine picks products.

Result: at most six questions, guaranteed termination, and a bundle that is
provably valid against the catalogue.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .data import get_store
from .engine import recommend
from .llm import explain, understand
from .profile import BusinessProfile
from .registry import gstin_lookup

# ----------------------------------------------------------------------
# The script. Six questions maximum — the case asks for "a few".
# ----------------------------------------------------------------------
QUESTIONS: list[dict[str, Any]] = [
    {
        "slot": "business_and_city",
        "fills": ["business_type", "city"],
        "text": {
            "English": "Namaste! I'm here to find the right connection package for your "
                       "business. To start — what kind of business do you run, and in "
                       "which city?",
            "Hindi": "नमस्ते! मैं आपके व्यवसाय के लिए सही पैकेज ढूँढने में मदद करूँगा। "
                     "शुरू करते हैं — आपका व्यवसाय किस प्रकार का है, और किस शहर में है?",
        },
        "chips": [],
    },
    {
        "slot": "size",
        "fills": ["employee_count", "num_sites"],
        "text": {
            "English": "How many people work there, and how many shops or branches do "
                       "you have?",
            "Hindi": "वहाँ कितने लोग काम करते हैं, और आपकी कितनी दुकानें या शाखाएँ हैं?",
        },
        "chips": ["Just me", "2-5 people, 1 shop", "10-25 people, 1 branch",
                  "25+ people, 3+ branches"],
    },
    {
        "slot": "gstin",
        "fills": ["has_gstin"],
        "text": {
            "English": "Do you have a GST number? If you type it, I can fill in your "
                       "business details automatically so you don't have to.",
            "Hindi": "क्या आपके पास GST नंबर है? अगर आप टाइप कर दें, तो मैं आपके "
                     "व्यवसाय की जानकारी अपने आप भर दूँगा।",
        },
        "chips": ["I'll type my GST number", "I have GST but not handy",
                  "No GST number"],
    },
    {
        "slot": "current_setup",
        "fills": ["current_internet"],
        "text": {
            "English": "What internet do you use today, and roughly what do you spend "
                       "each month on internet and phone together?",
            "Hindi": "आप अभी कौन सा इंटरनेट इस्तेमाल करते हैं, और इंटरनेट और फ़ोन पर "
                     "हर महीने लगभग कितना खर्च होता है?",
        },
        "chips": ["Mobile hotspot", "Home broadband", "Business broadband",
                  "Leased line", "Different ISP at each site"],
    },
    {
        "slot": "needs",
        "fills": ["needs"],
        "text": {
            "English": "Last thing — what would help your business most right now? "
                       "Pick as many as you like, or just tell me in your own words.",
            "Hindi": "आखिरी सवाल — अभी आपके व्यवसाय के लिए सबसे ज़्यादा क्या उपयोगी "
                     "होगा? जितने चाहें चुनें, या अपने शब्दों में बताएँ।",
        },
        "chips": ["Internet that never stops", "Billing & UPI payments",
                  "WhatsApp for customers", "CCTV cameras",
                  "Business email", "Phone lines for staff",
                  "Protection from viruses", "Wi-Fi for customers"],
    },
]

# Chip label -> slot values, so a tap is as good as a sentence.
CHIP_VALUES: dict[str, dict[str, Any]] = {
    "Just me": {"employee_count": 1, "num_sites": 1},
    "2-5 people, 1 shop": {"employee_count": 4, "num_sites": 1},
    "10-25 people, 1 branch": {"employee_count": 18, "num_sites": 1},
    "25+ people, 3+ branches": {"employee_count": 40, "num_sites": 3},
    "No GST number": {"has_gstin": False},
    "I have GST but not handy": {"has_gstin": True, "gstin_unverified": True},
    "Mobile hotspot": {"current_internet": "Mobile hotspot"},
    "Home broadband": {"current_internet": "Home broadband"},
    "Business broadband": {"current_internet": "Business broadband"},
    "Leased line": {"current_internet": "Leased line"},
    "Different ISP at each site": {"current_internet": "Mixed per site"},
    "Internet that never stops": {"needs": ["uptime"]},
    "Billing & UPI payments": {"needs": ["pos"]},
    "WhatsApp for customers": {"needs": ["customer_messaging"]},
    "CCTV cameras": {"needs": ["cctv"]},
    "Business email": {"needs": ["email"]},
    "Phone lines for staff": {"needs": ["phone_lines"]},
    "Protection from viruses": {"needs": ["security"]},
    "Wi-Fi for customers": {"needs": ["guest_wifi"]},
}


@dataclass
class AdvisorSession:
    session_id: str
    slots: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)
    step: int = 0
    finished: bool = False
    gstin_check: Optional[dict] = None
    # Slots filled from an authoritative source — registry autofill or a direct
    # answer to the question that owns them. Only these may skip a question.
    # Incidental keyword hits (the word "broadband" in a reply about current
    # spend) must never silence a question the owner still needs to answer.
    confident: set[str] = field(default_factory=set)

    @property
    def language(self) -> str:
        return self.slots.get("preferred_language", "English")

    def question(self) -> Optional[dict]:
        if self.step >= len(QUESTIONS):
            return None
        q = QUESTIONS[self.step]
        return {
            "slot": q["slot"],
            "text": q["text"].get(self.language, q["text"]["English"]),
            "chips": q["chips"],
            "step": self.step + 1,
            "total_steps": len(QUESTIONS),
        }

    def to_profile(self) -> BusinessProfile:
        raw = dict(self.slots)
        raw.pop("gstin_unverified", None)
        return BusinessProfile.from_dict(raw)


_SESSIONS: dict[str, AdvisorSession] = {}


def start_session() -> dict:
    session = AdvisorSession(session_id=uuid.uuid4().hex[:12])
    _SESSIONS[session.session_id] = session
    question = session.question()
    session.history.append({"role": "advisor", "text": question["text"]})
    return {
        "session_id": session.session_id,
        "question": question,
        "history": session.history,
        "finished": False,
    }


def get_session(session_id: str) -> Optional[AdvisorSession]:
    return _SESSIONS.get(session_id)


def _apply_gstin(session: AdvisorSession, gstin: str) -> Optional[str]:
    """Verify a GSTIN and autofill everything the registry already knows.

    This is the moment the journey earns its keep: fifteen characters replace
    an entire form.
    """
    result = gstin_lookup(gstin)
    session.gstin_check = result
    session.slots["gstin"] = gstin.strip().upper()
    session.slots["has_gstin"] = True
    session.slots["gstin_active"] = bool(result.get("verified"))

    if not result.get("verified"):
        if result.get("code") in ("GSTIN_CANCELLED", "GSTIN_SUSPENDED"):
            return (f"I checked that GST number and the registry shows it is "
                    f"{result['status'].lower()}. I can still set up everything that "
                    f"does not need an active GST registration — let's carry on.")
        return ("I couldn't verify that GST number — " + result["message"] +
                " No problem, we can continue without it and add it later.")

    record = result["record"]
    if not session.slots.get("business_type"):
        session.slots["business_type"] = record["trade_name"]
    session.confident.add("business_type")
    city = _city_from_address(record["principal_place_of_business"])
    if city:
        session.slots.setdefault("city", city)
        session.confident.add("city")

    # The Udyam registration usually exists under the same business name.
    udyam = get_store().udyam_for_business(record["trade_name"])
    if udyam:
        session.slots["has_udyam"] = True
        session.slots["udyam_number"] = udyam["udyam_number"]
        return (f"Verified. You are {record['legal_name']}, "
                f"{record['constitution']}, registered in {record['state']}. "
                f"I also found your Udyam registration as a "
                f"{udyam['enterprise_type'].lower()} enterprise, so that's one less "
                f"document to upload later.")
    return (f"Verified. You are {record['legal_name']}, {record['constitution']}, "
            f"registered in {record['state']}. I've filled in your details, so you "
            f"won't need to type them again.")


def _city_from_address(address: str) -> Optional[str]:
    """Pull the city out of 'Shop/Unit 80, Civil Lines, Nagpur, Maharashtra - 411231'."""
    parts = [p.strip() for p in address.split(",")]
    return parts[-2] if len(parts) >= 3 else None


def send_message(session_id: str, message: str) -> dict:
    """Process one owner reply and return the next question, or the result."""
    session = _SESSIONS.get(session_id)
    if session is None:
        raise KeyError("unknown session")
    if session.finished:
        return result_payload(session)

    session.history.append({"role": "owner", "text": message})
    current = QUESTIONS[session.step] if session.step < len(QUESTIONS) else None
    asked_about = current["slot"] if current else "general"

    # 1. A tapped chip is unambiguous — no parsing needed.
    updates: dict[str, Any] = {}
    source = "chip"
    if message.strip() in CHIP_VALUES:
        updates = dict(CHIP_VALUES[message.strip()])
    else:
        # 2. Otherwise understand the free text (Gemini, else fallback).
        updates = understand(message, asked_about)
        source = updates.pop("__source__", "fallback")

    # An owner who volunteers everything at once ("dental clinic in Kochi,
    # 20 staff, 1 branch, we pay 3050") should not then be asked for each
    # piece again. When the model genuinely comprehended the sentence we
    # trust every field it returned; keyword matching only earns trust for
    # the question actually being answered.
    if source in ("llm", "chip"):
        for key in updates:
            if updates[key] not in (None, "", []):
                session.confident.add(key)

    # 3. A GSTIN anywhere in the reply triggers verification and autofill.
    note: Optional[str] = None
    gstin = updates.pop("gstin", None)
    if gstin:
        note = _apply_gstin(session, gstin)
    elif updates.get("has_gstin") and asked_about == "gstin" and not session.slots.get("gstin"):
        note = ("Got it. You can enter the GST number later during document "
                "verification, and we'll fill in your details from it then.")

    # 4. Merge updates. Needs accumulate; everything else overwrites.
    for key, value in updates.items():
        if key == "needs":
            existing = set(session.slots.get("needs", []))
            session.slots["needs"] = sorted(existing | set(value))
        else:
            session.slots[key] = value

    # 5. Resolve the city tier once we know the city.
    if session.slots.get("city"):
        session.slots["city_tier"] = get_store().city_tier(session.slots["city"])

    if note:
        session.history.append({"role": "advisor", "text": note})

    # 6. The question just answered owns its slots, so whatever it filled is
    #    confident.
    if current:
        for slot_name in current["fills"]:
            if session.slots.get(slot_name) not in (None, "", []):
                session.confident.add(slot_name)

    # 7. Advance, skipping only questions whose slots are ALL confidently
    #    known — in practice, the ones GSTIN autofill already answered.
    session.step += 1
    while session.step < len(QUESTIONS):
        fills = QUESTIONS[session.step]["fills"]
        if all(f in session.confident and session.slots.get(f) for f in fills):
            session.step += 1
        else:
            break

    question = session.question()
    if question is None:
        session.finished = True
        return result_payload(session)

    session.history.append({"role": "advisor", "text": question["text"]})
    return {
        "session_id": session.session_id,
        "question": question,
        "history": session.history,
        "finished": False,
        "slots": session.slots,
        "gstin_check": session.gstin_check,
    }


def result_payload(session: AdvisorSession) -> dict:
    """Run the rules engine and have the LLM explain the outcome."""
    profile = session.to_profile()
    outcome = recommend(profile)
    message = explain(outcome["profile"], outcome["bundle"], session.language)

    if not any(h["text"] == message for h in session.history):
        session.history.append({"role": "advisor", "text": message})

    return {
        "session_id": session.session_id,
        "finished": True,
        "question": None,
        "history": session.history,
        "slots": session.slots,
        "gstin_check": session.gstin_check,
        "explanation": message,
        **outcome,
    }

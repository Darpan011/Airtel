"""Phase 2 acceptance tests — the conversational advisor.

Gemini is unreachable from the build container, so these tests exercise the
deterministic fallback path end to end. That is deliberate: the fallback is
what protects the live demo if hackathon wifi fails, so it is the path that
most needs proving.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import advisor as svc
from app.data import get_store
from app.engine import check_eligibility
from app.llm import fallback_explain, fallback_understand, llm_status

results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(f"{'  PASS' if ok else '  FAIL'}  {label}")
    if not ok:
        print(f"         expected {want!r}\n         got      {got!r}")


def check_true(label, value):
    check(label, bool(value), True)


store = get_store()


def run_conversation(answers: list[str]) -> dict:
    """Drive a full advisor session with scripted owner replies."""
    state = svc.start_session()
    sid = state["session_id"]
    for answer in answers:
        if state["finished"]:
            break
        state = svc.send_message(sid, answer)
    # Keep answering until it terminates, so we can prove it always does.
    guard = 0
    while not state["finished"] and guard < 12:
        state = svc.send_message(sid, "")
        guard += 1
    return state


print("\n=== LLM status ===")
status = llm_status()
print(f"   configured={status['configured']} enabled={status['enabled']} "
      f"model={status['model']}")
check_true("a key is configured", status["configured"])

print("\n=== Fallback understanding ===")
u = fallback_understand("I run a dental clinic in Kochi", "business_and_city")
check("business type cleaned of filler", u["business_type"], "dental clinic")
check("city matched against the real city list", u["city"], "Kochi")

u = fallback_understand("my bakery in Ludhiana", "business_and_city")
check("possessive prefix stripped", u["business_type"], "bakery")
check("city found", u["city"], "Ludhiana")

u = fallback_understand("20 staff, 1 clinic", "size")
check("employees parsed", u["employee_count"], 20)
check("sites parsed", u["num_sites"], 1)

u = fallback_understand("just me", "size")
check("'just me' = 1 employee", u["employee_count"], 1)

u = fallback_understand("we pay around 3,050 a month, business broadband",
                        "current_setup")
check("spend parsed with comma", u["monthly_spend_inr"], 3050)
check("connection type mapped", u["current_internet"], "Business broadband")

u = fallback_understand("about 15k per month on leased line", "current_setup")
check("'15k' expanded to 15000", u["monthly_spend_inr"], 15000)

u = fallback_understand("yes, 27XUIXG0767D1ZP", "gstin")
check("GSTIN found in free text", u["gstin"], "27XUIXG0767D1ZP")

u = fallback_understand("nahi hai", "gstin")
check("Hindi 'no' understood", u["has_gstin"], False)

u = fallback_understand("मुझे कैमरा और बिलिंग चाहिए", "needs")
check_true("Hindi needs detected", {"cctv", "pos"} <= set(u["needs"]))
check("language detected from script", u["preferred_language"], "Hindi")

u = fallback_understand("net band ho jata hai, staff ke liye number bhi chahiye",
                        "needs")
check_true("Hinglish needs detected", {"uptime", "phone_lines"} <= set(u["needs"]))

print("\n=== Conversation 1: dental clinic with a real GSTIN ===")
state = run_conversation([
    "I run a dental clinic in Nagpur",
    "20 staff, 1 clinic",
    "27XUIXG0767D1ZP",
    "business broadband, about 3050 a month",
    "business email and phone lines for staff",
])
check_true("conversation terminated", state["finished"])
check_true("a bundle was produced", len(state["bundle"]["items"]) > 0)
check_true("GSTIN was verified live", state["gstin_check"]["verified"])
check("autofilled legal name",
      state["gstin_check"]["record"]["legal_name"],
      "Joseph Singh (Prop. Ganesh Woodcraft)")
check_true("city autofilled from the registry address",
           state["slots"].get("city") == "Nagpur")
check_true("GST-gated products are now allowed",
           state["profile"]["gstin_active"])
print("   bundle:", [i["product_id"] for i in state["bundle"]["items"]],
      f"= Rs {state['bundle']['monthly_inr']}/mo")
print("   explanation:", state["explanation"][:150], "...")
check_true("explanation mentions the monthly total",
           f"{state['bundle']['monthly_inr']:,}" in state["explanation"])
check_true("explanation has no product codes",
           not any(f"P{n:02d}" in state["explanation"] for n in range(1, 26)))

print("\n=== Conversation 2: Hindi, kirana, no GSTIN ===")
state = run_conversation([
    "मेरी किराना दुकान है, लुधियाना में",
    "सिर्फ मैं और मेरा भाई",
    "नहीं है",
    "मोबाइल हॉटस्पॉट, करीब 1350 रुपये",
    "मुझे बिलिंग और कैमरा चाहिए",
])
check_true("conversation terminated", state["finished"])
check("replied in Hindi", state["slots"]["preferred_language"], "Hindi")
check_true("explanation is in Devanagari",
           any("ऀ" <= c <= "ॿ" for c in state["explanation"]))
ids = [i["product_id"] for i in state["bundle"]["items"]]
print("   bundle:", ids, f"= Rs {state['bundle']['monthly_inr']}/mo")
check_true("no GST-gated product offered",
           all(not store.product(p)["requires_gstin"] for p in ids))

print("\n=== Conversation 3: chips only, no typing ===")
state = run_conversation([
    "restaurant chain in Mumbai",
    "25+ people, 3+ branches",
    "No GST number",
    "Different ISP at each site",
    "CCTV cameras",
])
check_true("conversation terminated", state["finished"])
check("chip set headcount", state["profile"]["employee_count"], 40)
check("chip set sites", state["profile"]["num_sites"], 3)
check("chip set connection", state["slots"]["current_internet"], "Mixed per site")
check_true("CCTV need registered", "cctv" in state["profile"]["needs"])

print("\n=== Conversation 4: cancelled GSTIN is handled honestly ===")
cancelled_gstin = next(g for g, r in store.gstin_registry.items()
                       if r["status"] == "Cancelled")
state = run_conversation([
    "electronics shop in Chennai",
    "8 staff, 1 shop",
    cancelled_gstin,
    "home broadband, 2000",
    "WhatsApp for customers",
])
check_true("conversation terminated", state["finished"])
check("GSTIN not verified", state["gstin_check"]["verified"], False)
check("status surfaced as cancelled", state["gstin_check"]["code"], "GSTIN_CANCELLED")
check_true("the owner was told, in the transcript",
           any("cancelled" in h["text"].lower() for h in state["history"]))
ids = [i["product_id"] for i in state["bundle"]["items"]]
check_true("P15 WhatsApp withheld on a cancelled GSTIN", "P15" not in ids)

print("\n=== Regression: an incidental keyword must not skip a question ===")
# "broadband" in the current-setup answer used to set needs=['internet'],
# which silently skipped the needs question and discarded what the owner
# actually asked for.
sess = svc.start_session()
sid = sess["session_id"]
asked = []
for answer in ["dental clinic in Kochi", "20 staff 1 clinic", "no",
               "business broadband 3050", "email and security"]:
    sess = svc.send_message(sid, answer)
    if sess.get("question"):
        asked.append(sess["question"]["slot"])
check_true("the needs question was still asked", "needs" in asked)
check_true("the owner's stated needs survived",
           {"email", "security"} <= set(sess["profile"]["needs"]))
ids = [i["product_id"] for i in sess["bundle"]["items"]]
print("   bundle:", ids)
check_true("a security product is in the bundle",
           any(store.product(p)["category"] == "Security" for p in ids))

# GSTIN autofill SHOULD still skip the business/city question.
sess = svc.start_session()
sid = sess["session_id"]
sess = svc.send_message(sid, "27XUIXG0767D1ZP")
check_true("a verified GSTIN still autofills the business",
           svc.get_session(sid).slots.get("business_type") == "Ganesh Woodcraft")

print("\n=== Owner volunteers everything in one message ===")
# Gemini is unreachable from the build container, so stub the comprehension
# step to return what the real model returns for a sentence like
# "dental clinic in Kochi, 20 staff, 1 clinic, GST 27XUIXG0767D1ZP,
#  business broadband about 3050, need email and CCTV".
_real_understand = svc.understand


def _stub_understand(message, asked_about):
    if "everything" in message:
        return {
            "business_type": "dental clinic", "city": "Kochi",
            "employee_count": 20, "num_sites": 1,
            "gstin": "27XUIXG0767D1ZP", "has_gstin": True,
            "current_internet": "Business broadband", "monthly_spend_inr": 3050,
            "needs": ["email", "cctv"],
            "__source__": "llm",
        }
    return _real_understand(message, asked_about)


svc.understand = _stub_understand
try:
    sess = svc.start_session()
    state = svc.send_message(sess["session_id"], "everything at once please")
    check_true("one message finished the whole conversation", state["finished"])
    check("all five slots captured", state["profile"]["employee_count"], 20)
    check_true("needs captured", {"email", "cctv"} <= set(state["profile"]["needs"]))
    check_true("GSTIN still verified", state["gstin_check"]["verified"])
    advisor_turns = sum(1 for h in state["history"] if h["role"] == "advisor")
    print(f"   advisor turns: {advisor_turns} (greeting + verification + pitch)")
    check_true("it did not re-ask what was already answered", advisor_turns <= 4)
finally:
    svc.understand = _real_understand

# The earlier regression must still hold: keyword matching is NOT trusted
# to skip questions, only genuine comprehension is.
sess = svc.start_session()
sid = sess["session_id"]
asked = []
for answer in ["salon in Jaipur", "4 people 1 shop", "no",
               "home broadband 1200", "cctv"]:
    sess = svc.send_message(sid, answer)
    if sess.get("question"):
        asked.append(sess["question"]["slot"])
check_true("fallback path still asks every question", "needs" in asked)

print("\n=== Guarantees that must hold for ANY conversation ===")
scripts = [
    ["salon in Jaipur", "4 people 1 shop", "no", "hotspot 900", "customer wifi"],
    ["auto parts factory in Pune", "60 staff", "yes", "leased line 30000", "machines"],
    ["ca firm delhi", "12", "27XUIXG0767D1ZP", "home broadband 1800", "email backup"],
    ["chai stall", "1", "no", "nothing", "internet"],
    ["", "", "", "", ""],                      # owner says nothing at all
    ["coaching centre", "200 students 6 staff", "no", "broadband", "everything"],
]
all_valid, max_turns = True, 0
for script in scripts:
    state = run_conversation(script)
    max_turns = max(max_turns, sum(1 for h in state["history"] if h["role"] == "advisor"))
    if not state["finished"]:
        all_valid = False
        continue
    profile_obj = svc.get_session(state["session_id"]).to_profile()
    for item in state["bundle"]["items"]:
        if check_eligibility(store.product(item["product_id"]), profile_obj):
            all_valid = False
            print("   VIOLATION:", script[0], item["product_id"])
    if not state["explanation"].strip():
        all_valid = False

check("every conversation terminated and stayed valid", all_valid, True)
check_true("never more than 8 advisor turns", max_turns <= 8)

print("\n=== Explanation quality ===")
state = run_conversation([
    "dental clinic in Kochi", "20 staff 1 clinic", "no",
    "business broadband 3050", "email and security",
])
text = state["explanation"]
print("   ", text)
check_true("mentions rupee total", "Rs" in text)
check_true("mentions the install time", "days" in text.lower())
check_true("no markdown bullets", "- " not in text and "*" not in text)
check_true("saving is stated when they pay more today",
           "save" in text.lower() or state["bundle"].get("monthly_saving_inr", 0) <= 0)

print("\n" + "=" * 54)
print(f"  {sum(results)}/{len(results)} checks passed")
print("=" * 54)
sys.exit(0 if all(results) else 1)

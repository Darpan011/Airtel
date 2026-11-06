"""Gemini integration with graceful degradation.

Two jobs only:
  1. understand(): turn a free-text answer into structured slot updates
  2. explain():    turn a priced bundle into plain language in the owner's tongue

It never chooses products. If Gemini is unreachable, slow, or returns junk,
every function falls back to a deterministic implementation and the journey
continues. A demo must never depend on a live network call.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

import requests

from .config import GEMINI_MODEL

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT_SECONDS = 8  # fail fast; the fallback is good enough to demo on


# ----------------------------------------------------------------------
# Key loading — environment first, then a local .env (never committed)
# ----------------------------------------------------------------------
def _load_api_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if key:
        return key
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


API_KEY = _load_api_key()

# Set at runtime once we learn the API is unreachable, so we stop retrying
# and the journey stays fast.
_llm_disabled = False


def llm_status() -> dict:
    return {
        "configured": bool(API_KEY),
        "enabled": bool(API_KEY) and not _llm_disabled,
        "model": GEMINI_MODEL,
    }


def _call_gemini(prompt: str, schema: Optional[dict] = None,
                 temperature: float = 0.2) -> Optional[str]:
    """One Gemini REST call. Returns raw text, or None on any failure."""
    global _llm_disabled
    if not API_KEY or _llm_disabled:
        return None

    generation: dict[str, Any] = {"temperature": temperature}
    if schema is not None:
        generation["responseMimeType"] = "application/json"
        generation["responseSchema"] = schema

    try:
        response = requests.post(
            f"{API_ROOT}/{GEMINI_MODEL}:generateContent",
            params={"key": API_KEY},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": generation,
            },
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            # 400/403 means the key or model is wrong — stop trying entirely.
            if response.status_code in (400, 401, 403, 404):
                _llm_disabled = True
            return None
        payload = response.json()
        return payload["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        # Network blocked, DNS failure, timeout, malformed response — all the
        # same to us. Disable so the next turn does not wait another 8s.
        _llm_disabled = True
        return None


def _parse_json(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ======================================================================
# Job 1: understand a free-text answer
# ======================================================================
SLOT_SCHEMA = {
    "type": "object",
    "properties": {
        "business_type": {"type": "string"},
        "city": {"type": "string"},
        "employee_count": {"type": "integer"},
        "num_sites": {"type": "integer"},
        "has_gstin": {"type": "boolean"},
        "gstin": {"type": "string"},
        "current_internet": {"type": "string"},
        "monthly_spend_inr": {"type": "integer"},
        "budget_inr": {"type": "integer"},
        "needs": {"type": "array", "items": {"type": "string"}},
        "preferred_language": {"type": "string"},
    },
}

UNDERSTAND_PROMPT = """You are parsing one reply from an Indian small-business owner \
who is being onboarded to a telecom provider. Extract ONLY what the reply actually states.

The question they were asked was about: {asked_about}
Their reply (may be in any Indian language, or Hinglish): "{message}"

Rules:
- Return ONLY fields the reply genuinely contains. Omit anything not stated. Never guess.
- employee_count and num_sites are integers. "just me" = 1. "me and my brother" = 2.
- monthly_spend_inr and budget_inr are rupees per month as plain integers.
- has_gstin: true only if they clearly say they have GST or give a GST number.
- gstin: a 15-character GST number if one appears, uppercased, no spaces.
- current_internet: one of "Mobile hotspot", "Home broadband", "Business broadband", \
"Leased line", "Mixed per site", or "None".
- preferred_language: the language THEY wrote in (English, Hindi, Marathi, Tamil, \
Telugu, Kannada, Malayalam, Bengali, Gujarati, Punjabi).
- needs: zero or more of exactly these codes:
  internet, phone_lines, customer_messaging, email, backup, website, pos, security,
  cctv, guest_wifi, uptime, multi_site, telephony, static_ip, machine_iot, fleet
  Map meaning, not words: "UPI machine"/"billing" -> pos; "WhatsApp"/"customers ko \
message" -> customer_messaging; "camera" -> cctv; "net band ho jata hai" -> uptime;
  "staff ke liye number" -> phone_lines; "virus"/"hacking" -> security.
"""


def understand(message: str, asked_about: str) -> dict:
    """Extract slot updates from one user reply.

    The result carries `__source__`: "llm" when Gemini genuinely comprehended
    the sentence, "fallback" when it came from keyword matching. The advisor
    trusts the former enough to skip later questions, and the latter only for
    the question actually being answered.
    """
    prompt = UNDERSTAND_PROMPT.format(asked_about=asked_about, message=message)
    parsed = _parse_json(_call_gemini(prompt, schema=SLOT_SCHEMA, temperature=0.0))
    if parsed is None:
        out = fallback_understand(message, asked_about)
        out["__source__"] = "fallback"
        return out

    out = {k: v for k, v in parsed.items() if v not in (None, "", [], 0)}
    # The model occasionally echoes a need code that is not in our taxonomy.
    if "needs" in out:
        out["needs"] = [n for n in out["needs"] if n in NEED_KEYWORDS]
        if not out["needs"]:
            del out["needs"]
    out["__source__"] = "llm"
    return out


# ----------------------------------------------------------------------
# Deterministic fallback parser — keyword and regex based, multilingual
# ----------------------------------------------------------------------
NEED_KEYWORDS: dict[str, list[str]] = {
    "pos": ["pos", "billing", "bill", "upi", "card machine", "swipe", "invoice",
            "बिलिंग", "बिल", "கட்டணம்"],
    "customer_messaging": ["whatsapp", "sms", "message", "promotion", "offer",
                           "catalogue", "broadcast", "मैसेज", "संदेश"],
    "cctv": ["cctv", "camera", "surveillance", "कैमरा", "निगरानी"],
    "email": ["email", "mail", "gmail", "domain", "ईमेल"],
    "backup": ["backup", "data loss", "बैकअप"],
    "website": ["website", "site", "online presence", "वेबसाइट"],
    "security": ["security", "virus", "antivirus", "hack", "ransomware", "firewall",
                 "सुरक्षा", "वायरस"],
    "guest_wifi": ["guest wifi", "customer wifi", "free wifi", "वाईफाई"],
    "uptime": ["uptime", "down", "outage", "backup connection", "failover",
               "never stop", "बंद हो जाता", "कटता"],
    "multi_site": ["all branches", "multiple location", "central", "one bill",
                   "every store", "सभी शाखा"],
    "telephony": ["ivr", "call recording", "toll free", "tollfree", "landline",
                  "phone system"],
    "static_ip": ["static ip", "fixed ip", "remote access", "server"],
    "machine_iot": ["machine", "sensor", "iot", "production", "मशीन"],
    "fleet": ["fleet", "vehicle", "truck", "gps", "गाड़ी"],
    "phone_lines": ["sim", "mobile line", "business number", "staff number",
                    "postpaid", "सिम", "नंबर", "number chahiye", "ke liye number",
                    "phone line", "connection for staff"],
    "internet": ["internet", "broadband", "wifi", "fibre", "fiber",
                 "इंटरनेट", "नेट"],
}

# Romanised Hinglish phrases owners actually type. Kept separate so the
# Devanagari lists above stay readable.
NEED_KEYWORDS["uptime"] += ["band ho", "band ho jata", "chala jata", "cut ho",
                            "slow ho jata", "band pad"]
NEED_KEYWORDS["pos"] += ["bill banana", "billing machine"]
NEED_KEYWORDS["customer_messaging"] += ["customer ko message", "grahak",
                                        "message bhejna"]

LANGUAGE_HINTS = {
    "Hindi": ["ऀ-ॿ"], "Bengali": ["ঀ-৿"],
    "Gujarati": ["઀-૿"], "Punjabi": ["਀-੿"],
    "Tamil": ["஀-௿"], "Telugu": ["ఀ-౿"],
    "Kannada": ["ಀ-೿"], "Malayalam": ["ഀ-ൿ"],
}

YES_WORDS = {"yes", "yeah", "yep", "haan", "han", "ha", "ji", "hai", "y",
             "हाँ", "हां", "है", "sure", "correct", "right"}
NO_WORDS = {"no", "nope", "nahi", "nahin", "n", "नहीं", "नही", "not yet",
            "none", "don't", "dont"}

GSTIN_IN_TEXT = re.compile(r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z])\b",
                           re.IGNORECASE)


def _detect_language(text: str) -> Optional[str]:
    for language, ranges in LANGUAGE_HINTS.items():
        for rng in ranges:
            if re.search(f"[{rng}]", text):
                return language
    return None


def _said_yes(text: str) -> Optional[bool]:
    """Resolve yes/no on word boundaries, checking 'no' first.

    Without this, "nahi hai" matches the affirmative "hai" and a business with
    no GST registration gets offered GST-gated products.
    """
    words = set(re.findall(r"[\wऀ-ॿ]+", text.lower()))
    if words & NO_WORDS or "not" in words:
        return False
    if words & YES_WORDS:
        return True
    return None


def _find_city(text: str) -> Optional[str]:
    """Match the message against the real city list from the leads data.

    More reliable than parsing "<something> in <something>", and it also gives
    us the city tier for free.
    """
    from .data import get_store  # imported here to avoid a circular import

    lowered = text.lower()
    best: Optional[str] = None
    for city in get_store().city_tiers:
        if re.search(rf"\b{re.escape(city)}\b", lowered):
            if best is None or len(city) > len(best):
                best = city
    return best.title() if best else None


# Filler an owner puts in front of their business type.
_BUSINESS_PREFIXES = re.compile(
    r"^(i\s+(run|have|own|operate)\s+(a|an|my)?|my|we\s+(are|run|have)\s+(a|an)?|"
    r"it'?s\s+(a|an)?|this\s+is\s+(a|an)?|a|an)\s+",
    re.IGNORECASE,
)


def _clean_business_type(text: str, city: Optional[str]) -> str:
    """Turn "I run a dental clinic in Kochi" into "dental clinic"."""
    cleaned = text.strip().rstrip(".!,")
    if city:
        cleaned = re.sub(rf"\s*,?\s*\b(in|at)\s+{re.escape(city)}\b.*$", "",
                         cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(rf"\b{re.escape(city)}\b\s*(mein|me|में)?\s*", "",
                         cleaned, flags=re.IGNORECASE)
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _BUSINESS_PREFIXES.sub("", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return cleaned or text.strip()


def _numbers(text: str) -> list[int]:
    """Pull integers out of text, expanding Indian magnitude words."""
    found: list[int] = []
    for match in re.finditer(r"(\d[\d,]*)\s*(k|hazaar|hazar|thousand|lakh|lac)?",
                             text, re.IGNORECASE):
        value = int(match.group(1).replace(",", ""))
        suffix = (match.group(2) or "").lower()
        if suffix in ("k", "hazaar", "hazar", "thousand"):
            value *= 1000
        elif suffix in ("lakh", "lac"):
            value *= 100000
        found.append(value)
    return found


def fallback_understand(message: str, asked_about: str) -> dict:
    """Keyword/regex extraction used whenever Gemini is unavailable."""
    text = message.lower().strip()
    out: dict[str, Any] = {}

    language = _detect_language(message)
    if language:
        out["preferred_language"] = language

    numbers = _numbers(text)

    if asked_about == "business_and_city":
        city = _find_city(message)
        if city:
            out["city"] = city
        business = _clean_business_type(message, city)
        # Don't record a pasted GSTIN as the business name.
        if not GSTIN_IN_TEXT.fullmatch(business.strip()):
            out["business_type"] = GSTIN_IN_TEXT.sub("", business).strip(" ,.-") or business
    elif asked_about == "size":
        if numbers:
            out["employee_count"] = numbers[0]
            if len(numbers) > 1:
                out["num_sites"] = numbers[1]
        if any(w in text for w in ("just me", "only me", "alone", "akela",
                                   "sirf main", "सिर्फ मैं", "अकेला")):
            out["employee_count"] = 1
            out["num_sites"] = 1
        # "me and my brother", "मैं और मेरा भाई" — two people, no digits given.
        if not out.get("employee_count") and any(
            w in text for w in ("and my brother", "and my wife", "and my son",
                                "और मेरा", "और मेरी", "me and")
        ):
            out["employee_count"] = 2
            out["num_sites"] = 1
    elif asked_about == "gstin":
        if not GSTIN_IN_TEXT.search(message):
            answer = _said_yes(text)
            if answer is not None:
                out["has_gstin"] = answer
    elif asked_about == "current_setup":
        mapping = {
            "hotspot": "Mobile hotspot", "jio fi": "Mobile hotspot",
            "dongle": "Mobile hotspot", "mobile data": "Mobile hotspot",
            "home broadband": "Home broadband", "home": "Home broadband",
            "business broadband": "Business broadband",
            "leased line": "Leased line", "leased": "Leased line",
            "different": "Mixed per site", "mixed": "Mixed per site",
            "broadband": "Home broadband",
        }
        for keyword, value in mapping.items():
            if keyword in text:
                out["current_internet"] = value
                break
        else:
            if _said_yes(text) is False:
                out["current_internet"] = "None"
        if numbers:
            out["monthly_spend_inr"] = max(numbers)
    elif asked_about == "budget":
        if numbers:
            out["budget_inr"] = max(numbers)

    # Needs can be mentioned in any answer, so always scan for them.
    needs = [need for need, words in NEED_KEYWORDS.items()
             if any(w in text for w in words)]
    if needs:
        out["needs"] = needs

    # So can the city — an owner may only name it halfway through.
    if "city" not in out:
        city = _find_city(message)
        if city:
            out["city"] = city

    # A GSTIN may be pasted at any point in the conversation, not only when
    # it is asked for. Always look for one.
    match = GSTIN_IN_TEXT.search(message)
    if match:
        out["gstin"] = match.group(1).upper()
        out["has_gstin"] = True

    return out


# ======================================================================
# Job 2: explain the bundle in the owner's language
# ======================================================================
EXPLAIN_PROMPT = """You are Accel Business's onboarding advisor talking to the owner of \
a small Indian business. Explain the package below in {language}.

THE BUSINESS
{profile}

THE PACKAGE (already decided — do not add, remove or re-price anything)
{items}

Total: Rs {monthly}/month{setup_line}
Contract: {contract} months
Ready in about {days} days
{savings_line}

Write 4 to 6 short sentences in {language}:
1. Name the main connection and why it suits THIS business specifically.
2. Mention each other item in one clause, tied to something they told you.
3. State the total clearly as "Rs {monthly} per month".
4. {savings_instruction}
5. End by saying they can add or remove anything before paying.

Rules: speak to the owner as "you". No product codes like P01. No jargon \
(say "backup connection", not "failover"). No markdown, no bullet points, no headings. \
Plain conversational sentences only. Rupee amounts stay as digits."""


def explain(profile: dict, bundle: dict, language: str = "English") -> str:
    """Produce the plain-language pitch for a finished bundle."""
    items = bundle.get("items", [])
    if not items:
        return fallback_explain(profile, bundle, language)

    item_lines = "\n".join(
        f"- {i['name']} x{i['quantity']} ({i['unit']}), Rs {i['monthly_inr']}/month "
        f"— included because it gives them {i['reason']}"
        for i in items
    )
    saving = bundle.get("monthly_saving_inr")
    if saving and saving > 0:
        savings_line = (f"They currently pay Rs {bundle['current_spend_inr']}/month "
                        f"across vendors, so this saves Rs {saving}/month.")
        savings_instruction = (f"Tell them they save Rs {saving} every month versus "
                               f"what they pay now, and that it is one single bill.")
    else:
        savings_line = ""
        savings_instruction = ("Tell them this replaces several separate vendor bills "
                               "with one single bill from one company.")

    profile_lines = "\n".join(
        f"- {k.replace('_', ' ')}: {v}"
        for k, v in profile.items()
        if v not in (None, "", [], 0) and k not in ("needs",)
    )
    if profile.get("needs"):
        profile_lines += f"\n- they asked for: {', '.join(profile['needs'])}"

    setup = bundle.get("setup_inr", 0)
    prompt = EXPLAIN_PROMPT.format(
        language=language,
        profile=profile_lines,
        items=item_lines,
        monthly=f"{bundle['monthly_inr']:,}",
        setup_line=(f", plus a one-time setup of Rs {setup:,}" if setup else ""),
        contract=bundle.get("contract_months", 12),
        days=bundle.get("install_days", 5),
        savings_line=savings_line,
        savings_instruction=savings_instruction,
    )
    text = _call_gemini(prompt, temperature=0.4)
    if not text:
        return fallback_explain(profile, bundle, language)
    return text.strip()


# ----------------------------------------------------------------------
# Deterministic explanation templates (English + Hindi shipped in full)
# ----------------------------------------------------------------------
TEMPLATES = {
    "English": {
        "open": "For your {business} in {city}, here is what I suggest.",
        "anchor": "The main connection is {name}, which gives you {reason}.",
        "item": "I have added {name} for {reason}.",
        "total": "That comes to Rs {monthly} per month{setup}.",
        "saving": "You told me you pay about Rs {spend} a month today, so this saves "
                  "you Rs {saving} every month — and it arrives as one single bill.",
        "onebill": "It all arrives as one single bill from one company, instead of "
                   "separate vendors.",
        "close": "It should be working in about {days} days on a {contract}-month plan. "
                 "You can add or remove anything before you pay.",
    },
    "Hindi": {
        "open": "{city} में आपके {business} के लिए मेरा सुझाव यह है।",
        "anchor": "मुख्य कनेक्शन {name} है, जिससे आपको {reason} मिलता है।",
        "item": "{name} भी जोड़ा है — {reason}।",
        "total": "कुल मिलाकर Rs {monthly} प्रति माह{setup}।",
        "saving": "आप अभी हर महीने लगभग Rs {spend} दे रहे हैं, तो इसमें हर महीने "
                  "Rs {saving} की बचत होगी — और बिल भी एक ही आएगा।",
        "onebill": "अलग-अलग वेंडर के बजाय एक ही कंपनी का एक ही बिल आएगा।",
        "close": "लगभग {days} दिन में चालू हो जाएगा, {contract} महीने का प्लान है। "
                 "पैसे देने से पहले आप कुछ भी जोड़ या हटा सकते हैं।",
    },
}

HINDI_REASONS = {
    "P01": "रोज़ के काम और UPI के लिए भरोसेमंद 100 Mbps लाइन",
    "P02": "300 Mbps, ताकि कई कंप्यूटर, कार्ड मशीन और कैमरे एक साथ चलें",
    "P04": "5G वायरलेस ब्रॉडबैंड, जो लगभग 2 दिन में लग जाता है",
    "P07": "बिज़नेस मोबाइल लाइन, ताकि दुकान का नंबर अलग रहे",
    "P15": "वेरिफाइड WhatsApp चैनल, जहाँ आपके ग्राहक पहले से हैं",
    "P16": "अपने डोमेन पर बिज़नेस ईमेल",
    "P19": "GST बिलिंग, इन्वेंटरी और UPI/कार्ड पेमेंट",
    "P20": "हर कंप्यूटर और फ़ोन पर वायरस से सुरक्षा",
    "P23": "HD कैमरे, 30 दिन की क्लाउड रिकॉर्डिंग, फ़ोन पर देखें",
}


def fallback_explain(profile: dict, bundle: dict, language: str = "English") -> str:
    """Template explanation — used when Gemini is unavailable."""
    tpl = TEMPLATES.get(language, TEMPLATES["English"])
    hindi = language == "Hindi"
    items = bundle.get("items", [])

    if not items:
        return ("I could not find a package that fits this location and business size. "
                "Let me arrange a quick call with a local partner who can check "
                "what is possible at your address.")

    def reason_for(item: dict) -> str:
        if hindi and item["product_id"] in HINDI_REASONS:
            return HINDI_REASONS[item["product_id"]]
        return item["reason"]

    parts = [tpl["open"].format(
        business=profile.get("business_type") or "business",
        city=profile.get("city") or "your city",
    )]

    connectivity = [i for i in items if i["category"] == "Connectivity"]
    others = [i for i in items if i["category"] != "Connectivity"]
    if connectivity:
        anchor = connectivity[0]
        parts.append(tpl["anchor"].format(name=anchor["name"], reason=reason_for(anchor)))
        others = connectivity[1:] + others
    for item in others[:4]:
        parts.append(tpl["item"].format(name=item["name"], reason=reason_for(item)))

    setup = bundle.get("setup_inr", 0)
    if setup:
        setup_text = (f", और एक बार का सेटअप Rs {setup:,}" if hindi
                      else f", plus a one-time setup of Rs {setup:,}")
    else:
        setup_text = ""
    parts.append(tpl["total"].format(monthly=f"{bundle['monthly_inr']:,}", setup=setup_text))

    saving = bundle.get("monthly_saving_inr")
    if saving and saving > 0:
        parts.append(tpl["saving"].format(
            spend=f"{bundle['current_spend_inr']:,}", saving=f"{saving:,}"))
    else:
        parts.append(tpl["onebill"])

    parts.append(tpl["close"].format(
        days=bundle.get("install_days", 5),
        contract=bundle.get("contract_months", 12)))
    return " ".join(parts)

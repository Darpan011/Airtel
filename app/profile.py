"""The structured business profile the advisor extracts from conversation.

This is the contract between the LLM and the rules engine. The LLM's only job
is to fill this object in; it never chooses products. That separation is what
keeps recommendations provably valid.
"""
from dataclasses import dataclass, field
from typing import Optional

from .config import SEGMENT_CODES

# Needs the advisor can detect, in plain language the owner would actually use.
KNOWN_NEEDS = {
    "internet",            # reliable connectivity (always implied)
    "phone_lines",         # business mobile numbers
    "customer_messaging",  # WhatsApp catalogue / SMS alerts
    "email",               # business email on own domain
    "backup",              # cloud backup of PCs and phones
    "website",             # domain + simple site
    "pos",                 # billing / UPI / card acceptance
    "security",            # antivirus, firewall
    "cctv",                # cameras
    "guest_wifi",          # customer wi-fi
    "uptime",              # must not go down — failover
    "multi_site",          # central management across sites
    "telephony",           # IVR, call recording, toll-free
    "static_ip",           # remote access / servers
    "machine_iot",         # factory machine monitoring
    "fleet",               # vehicle tracking
}

# Business types the advisor commonly sees, mapped to a segment code.
BUSINESS_TYPE_SEGMENT = {
    # micro retail & services
    "kirana": "micro", "grocery": "micro", "salon": "micro", "parlour": "micro",
    "pharmacy": "micro", "bakery": "micro", "boutique": "micro", "tailor": "micro",
    "sweet shop": "micro", "stationery": "micro", "mobile shop": "micro",
    "hardware store": "micro", "tea stall": "micro", "laundry": "micro",
    "general store": "micro", "retail": "micro", "shop": "micro",
    # professional offices
    "clinic": "prof", "dental": "prof", "doctor": "prof", "physio": "prof",
    "ca firm": "prof", "chartered accountant": "prof", "lawyer": "prof",
    "legal": "prof", "coaching": "prof", "tuition": "prof", "architect": "prof",
    "consultant": "prof", "insurance": "prof", "diagnostic": "prof",
    "travel agency": "prof", "office": "prof", "agency": "prof",
    # small multi-site
    "restaurant": "multi", "cafe": "multi", "hotel": "multi", "chain": "multi",
    "franchise": "multi", "supermarket": "multi", "showroom": "multi",
    "stays": "multi", "resort": "multi",
    # small manufacturers
    "manufacturer": "mfg", "factory": "mfg", "workshop": "mfg", "unit": "mfg",
    "textile": "mfg", "auto parts": "mfg", "furniture": "mfg",
    "printing press": "mfg", "fabrication": "mfg", "packaging": "mfg",
}


@dataclass
class BusinessProfile:
    """Everything the rules engine needs, and nothing it does not.

    Data minimisation starts here: there is no field for the owner's personal
    address, date of birth, PAN or Aadhaar, because bundle selection does not
    need them.
    """
    business_type: str = ""
    segment: Optional[str] = None          # micro | prof | multi | mfg
    employee_count: int = 1
    num_sites: int = 1
    city: str = ""
    city_tier: int = 2                     # 1 or 2
    has_gstin: bool = False
    gstin_active: bool = False             # verified ACTIVE via registry
    has_udyam: bool = False
    current_internet: str = ""
    monthly_spend_inr: int = 0             # what they pay today, all vendors
    budget_inr: Optional[int] = None       # monthly ceiling, if stated
    needs: list[str] = field(default_factory=list)
    preferred_language: str = "English"
    num_vehicles: int = 0
    num_machines: int = 0

    # ------------------------------------------------------------------
    def normalised_segment(self) -> str:
        """Resolve the segment code, inferring from business type if needed."""
        if self.segment in {"micro", "prof", "multi", "mfg"}:
            return self.segment
        if self.segment in SEGMENT_CODES:
            return SEGMENT_CODES[self.segment]

        text = (self.business_type or "").lower()
        # Longest keyword first, so "workshop" is not swallowed by "shop" and
        # "mobile shop" beats a bare "shop".
        for keyword, code in sorted(BUSINESS_TYPE_SEGMENT.items(),
                                    key=lambda kv: -len(kv[0])):
            if keyword in text:
                # A "restaurant" with one site is really a micro business;
                # the multi-site bundle only makes sense above one location.
                if code == "multi" and self.num_sites < 2:
                    return "prof" if self.employee_count >= 10 else "micro"
                return code

        # Fall back on size, which is the next most reliable signal.
        if self.num_sites >= 3:
            return "multi"
        if self.employee_count >= 25:
            return "mfg"
        if self.employee_count >= 8:
            return "prof"
        return "micro"

    def effective_needs(self) -> list[str]:
        """Needs the owner stated, plus the ones implied by the business."""
        needs = {n for n in self.needs if n in KNOWN_NEEDS}
        needs.add("internet")  # every bundle anchors on connectivity

        segment = self.normalised_segment()
        if segment == "multi" and self.num_sites >= 3:
            needs.add("multi_site")
            needs.add("uptime")
        if segment == "mfg":
            needs.add("security")
            needs.add("uptime")
        if segment == "prof":
            needs.add("email")
        return sorted(needs)

    def to_dict(self) -> dict:
        return {
            "business_type": self.business_type,
            "segment": self.normalised_segment(),
            "employee_count": self.employee_count,
            "num_sites": self.num_sites,
            "city": self.city,
            "city_tier": self.city_tier,
            "has_gstin": self.has_gstin,
            "gstin_active": self.gstin_active,
            "has_udyam": self.has_udyam,
            "current_internet": self.current_internet,
            "monthly_spend_inr": self.monthly_spend_inr,
            "budget_inr": self.budget_inr,
            "needs": self.effective_needs(),
            "preferred_language": self.preferred_language,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "BusinessProfile":
        """Build a profile from loose JSON (e.g. an LLM extraction)."""
        def as_int(value, default=0):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

        tier = as_int(raw.get("city_tier"), 2)
        return cls(
            business_type=str(raw.get("business_type") or ""),
            segment=raw.get("segment"),
            employee_count=max(1, as_int(raw.get("employee_count"), 1)),
            num_sites=max(1, as_int(raw.get("num_sites"), 1)),
            city=str(raw.get("city") or ""),
            city_tier=tier if tier in (1, 2) else 2,
            has_gstin=bool(raw.get("has_gstin")),
            gstin_active=bool(raw.get("gstin_active")),
            has_udyam=bool(raw.get("has_udyam")),
            current_internet=str(raw.get("current_internet") or ""),
            monthly_spend_inr=as_int(raw.get("monthly_spend_inr"), 0),
            budget_inr=as_int(raw["budget_inr"]) if raw.get("budget_inr") else None,
            needs=[n for n in (raw.get("needs") or []) if n in KNOWN_NEEDS],
            preferred_language=str(raw.get("preferred_language") or "English"),
            num_vehicles=as_int(raw.get("num_vehicles"), 0),
            num_machines=as_int(raw.get("num_machines"), 0),
        )

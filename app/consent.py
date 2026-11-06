"""DPDP Act 2023 consent ledger and order state.

Consent under the DPDP Act has to be free, specific, informed, unconditional
and unambiguous, given by a clear affirmative action, and as easy to withdraw
as it was to give. That means, concretely:

  * one record per purpose, not a single "I agree to everything" tick
  * the notice text that was shown is stored WITH the consent, so we can prove
    what the person actually agreed to
  * optional purposes default to off and can be refused without losing service
  * withdrawal is recorded, never deleted, so the audit trail stays intact

In production this would be an append-only table with the notice version
hashed. Here it is in memory, with the same shape.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Each purpose is separate and separately refusable. Only the first two are
# needed to deliver the service; the rest are genuinely optional.
PURPOSES: dict[str, dict] = {
    "identity_verification": {
        "title": "Verify your business identity",
        "notice": (
            "We will read your GST certificate, Udyam certificate, address proof "
            "and signatory ID to confirm your business is real and that you are "
            "authorised to buy for it. We check your GST and Udyam numbers "
            "against the government registries."
        ),
        "data": ["GST number", "Udyam number", "business name and address",
                 "signatory name"],
        "retention": "Document images are deleted as soon as the check finishes. "
                     "The extracted details are kept for 8 years, as telecom "
                     "licence rules require.",
        "required": True,
    },
    "service_delivery": {
        "title": "Set up and run your connection",
        "notice": (
            "We use your business address to check what can be installed at your "
            "site, and your contact number to arrange installation and support."
        ),
        "data": ["installation address", "contact name and number"],
        "retention": "Kept while you are a customer, and for 8 years after, as "
                     "telecom licence rules require.",
        "required": True,
    },
    "service_updates": {
        "title": "Send you order and service updates",
        "notice": (
            "We will send you WhatsApp or SMS updates about your installation and "
            "any outages. Without this you will need to check your order status "
            "on the tracker instead."
        ),
        "data": ["mobile number"],
        "retention": "Until you turn this off.",
        "required": False,
    },
    "marketing": {
        "title": "Tell you about other products and offers",
        "notice": (
            "We will contact you about other Accel Business products. This is "
            "entirely optional and refusing it changes nothing about your service."
        ),
        "data": ["mobile number", "email address"],
        "retention": "Until you turn this off.",
        "required": False,
    },
}


def consent_notice() -> dict:
    """Everything the consent screen needs to render, in the owner's language."""
    return {
        "data_fiduciary": "Accel Business (Accel Telecom Ltd)",
        "law": "Digital Personal Data Protection Act, 2023",
        "grievance_officer": "privacy@accelbusiness.example",
        "rights": [
            "Ask us what personal data we hold about you",
            "Have anything wrong corrected",
            "Withdraw any optional consent at any time",
            "Ask us to erase your data once we no longer need it by law",
            "Complain to the Data Protection Board if we get it wrong",
        ],
        "purposes": [
            {"key": key, **{k: v for k, v in purpose.items()}}
            for key, purpose in PURPOSES.items()
        ],
    }


@dataclass
class ConsentRecord:
    consent_id: str
    purpose: str
    granted: bool
    notice_shown: str
    timestamp: str
    withdrawn_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "consent_id": self.consent_id,
            "purpose": self.purpose,
            "purpose_title": PURPOSES.get(self.purpose, {}).get("title", self.purpose),
            # The UI needs this to avoid offering a "withdraw" control that can
            # only ever refuse.
            "required": PURPOSES.get(self.purpose, {}).get("required", False),
            "granted": self.granted and self.withdrawn_at is None,
            "timestamp": self.timestamp,
            "withdrawn_at": self.withdrawn_at,
        }


@dataclass
class ConsentLedger:
    """Append-only. Withdrawal adds a record; it never removes one."""
    records: list[ConsentRecord] = field(default_factory=list)

    def record(self, purpose: str, granted: bool) -> ConsentRecord:
        entry = ConsentRecord(
            consent_id=uuid.uuid4().hex[:12],
            purpose=purpose,
            granted=granted,
            notice_shown=PURPOSES.get(purpose, {}).get("notice", ""),
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.records.append(entry)
        return entry

    def withdraw(self, purpose: str) -> bool:
        found = False
        for entry in self.records:
            if entry.purpose == purpose and entry.granted and not entry.withdrawn_at:
                entry.withdrawn_at = datetime.now(timezone.utc).isoformat(
                    timespec="seconds")
                found = True
        return found

    def granted(self, purpose: str) -> bool:
        return any(r.purpose == purpose and r.granted and not r.withdrawn_at
                   for r in self.records)

    def missing_required(self) -> list[str]:
        return [key for key, purpose in PURPOSES.items()
                if purpose["required"] and not self.granted(key)]

    def to_dict(self) -> dict:
        return {
            "records": [r.to_dict() for r in self.records],
            "required_satisfied": not self.missing_required(),
            "missing_required": self.missing_required(),
        }

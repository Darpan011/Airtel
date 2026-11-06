"""Order state and the status tracker.

The data makes the case for this component better than any argument: customers
who chased status once scored NPS 7.5 and churned at 5.9%; those who chased
five times scored 6.0 and churned at 27.9%. Every avoided phone call is
retained revenue, so the tracker exists to make calling unnecessary.

Milestone dates are projected from `typical_install_days` in the catalogue
rather than invented, so the promise shown at checkout is the same number the
tracker counts down to.
"""
from __future__ import annotations

import random
import string
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .config import TODAY
from .consent import ConsentLedger

# (key, label, what the owner sees, share of the install window)
MILESTONES: list[tuple[str, str, str, float]] = [
    ("confirmed",  "Order confirmed",
     "We have your order and your documents are in the queue.", 0.0),
    ("kyc",        "Documents verified",
     "Your GST and Udyam details were checked against the government registries.", 0.1),
    ("signed",     "Agreement signed",
     "Your service agreement is signed and filed.", 0.2),
    ("provisioning", "Provisioning your connection",
     "Your numbers and services are being created on our network.", 0.4),
    ("scheduled",  "Engineer visit scheduled",
     "An engineer is booked to install at your address.", 0.6),
    ("installed",  "Installed and tested",
     "Equipment is in and the line has been tested.", 0.9),
    ("activated",  "Live",
     "Everything is active. Welcome to Accel Business.", 1.0),
]

_SKIP_VISIT = {"confirmed", "kyc", "signed", "provisioning", "activated"}


def _order_id() -> str:
    return "UD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


@dataclass
class Order:
    order_id: str
    profile: dict
    bundle: dict
    consent: ConsentLedger
    kyc: Optional[dict] = None
    placed_on: date = field(default_factory=lambda: TODAY)
    stage: int = 0
    otp: str = field(default_factory=lambda: f"{random.randint(100000, 999999)}")
    contact_name: str = ""
    contact_mobile: str = ""

    # ------------------------------------------------------------------
    @property
    def install_days(self) -> int:
        return max(1, int(self.bundle.get("install_days", 5)))

    @property
    def expected_live(self) -> date:
        return self.placed_on + timedelta(days=self.install_days)

    def milestone_date(self, share: float) -> date:
        return self.placed_on + timedelta(days=round(self.install_days * share))

    def needs_site_visit(self) -> bool:
        """Cloud-only bundles activate without anyone coming out."""
        return any(i.get("unit") == "per site" for i in self.bundle.get("items", []))

    def timeline(self) -> list[dict]:
        steps = []
        index = 0
        for key, label, detail, share in MILESTONES:
            if key == "scheduled" and not self.needs_site_visit():
                continue
            if key == "installed" and not self.needs_site_visit():
                continue
            steps.append({
                "key": key,
                "label": label,
                "detail": detail,
                "on": self.milestone_date(share).isoformat(),
                "state": ("done" if index < self.stage
                          else "current" if index == self.stage
                          else "upcoming"),
            })
            index += 1
        return steps

    def advance(self) -> bool:
        total = len(self.timeline())
        if self.stage >= total - 1:
            return False
        self.stage += 1
        return True

    def status_label(self) -> str:
        steps = self.timeline()
        return steps[min(self.stage, len(steps) - 1)]["label"]

    def to_dict(self) -> dict:
        steps = self.timeline()
        current = steps[min(self.stage, len(steps) - 1)]
        return {
            "order_id": self.order_id,
            "placed_on": self.placed_on.isoformat(),
            "expected_live": self.expected_live.isoformat(),
            "days_remaining": max(0, (self.expected_live - TODAY).days),
            "status": current["label"],
            "status_detail": current["detail"],
            "progress_pct": round(self.stage / max(1, len(steps) - 1) * 100),
            "is_live": self.stage >= len(steps) - 1,
            "timeline": steps,
            "bundle": self.bundle,
            "monthly_inr": self.bundle.get("monthly_inr", 0),
            "setup_inr": self.bundle.get("setup_inr", 0),
            "contact_name": self.contact_name,
            "needs_site_visit": self.needs_site_visit(),
            "kyc_status": (self.kyc or {}).get("status"),
            "consent": self.consent.to_dict(),
        }


_ORDERS: dict[str, Order] = {}


def create_order(profile: dict, bundle: dict, consent: ConsentLedger,
                 kyc: Optional[dict] = None, contact_name: str = "",
                 contact_mobile: str = "") -> Order:
    order = Order(
        order_id=_order_id(), profile=profile, bundle=bundle,
        consent=consent, kyc=kyc,
        contact_name=contact_name, contact_mobile=contact_mobile,
    )
    # A verified KYC means the first two milestones are already behind us.
    if kyc and kyc.get("status") == "AUTO_APPROVED":
        order.stage = 1
    _ORDERS[order.order_id] = order
    return order


def get_order(order_id: str) -> Optional[Order]:
    return _ORDERS.get((order_id or "").strip().upper())


def all_orders() -> list[Order]:
    return list(_ORDERS.values())

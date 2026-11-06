"""The rules engine — deterministic bundle selection and pricing.

This module decides what a business can buy. The LLM never does. Given a
BusinessProfile it:

  1. filters the catalogue by the eligibility rules in product_catalogue.csv
  2. picks exactly one connectivity anchor
  3. adds need-driven products
  4. resolves `requires_one_of` dependencies
  5. trims to budget by priority
  6. prices the bundle and computes an activation ETA

Every decision carries a plain-language reason, so the advisor can explain
itself and a judge can audit it.
"""
from dataclasses import dataclass, field
from typing import Optional

from .data import get_store
from .profile import BusinessProfile

# ----------------------------------------------------------------------
# Need -> candidate products, in preference order.
# ----------------------------------------------------------------------
NEED_PRODUCTS: dict[str, list[str]] = {
    "phone_lines":        ["P08", "P07"],
    "customer_messaging": ["P15", "P14"],
    "email":              ["P16"],
    "backup":             ["P17"],
    "website":            ["P18"],
    "pos":                ["P19"],
    "security":           ["P21", "P20"],
    "cctv":               ["P23"],
    "guest_wifi":         ["P22"],
    "uptime":             ["P09"],
    "multi_site":         ["P10"],
    "telephony":          ["P12", "P13"],
    "static_ip":          ["P11"],
    "machine_iot":        ["P24"],
    "fleet":              ["P25"],
}

# Connectivity anchors in preference order per segment. The engine walks the
# list and takes the first one the business is actually eligible for.
ANCHOR_PREFERENCE: dict[str, list[str]] = {
    "micro": ["P01", "P04", "P02"],
    "prof":  ["P02", "P01", "P04", "P03"],
    "multi": ["P02", "P03", "P01", "P05"],
    "mfg":   ["P05", "P06", "P03", "P02"],
}

# Priority decides what gets dropped first when a budget bites.
PRIORITY = {
    "anchor": 100,
    "stated_need": 80,
    "phone_lines": 75,
    "implied_need": 50,
    "dependency": 95,  # never drop a prerequisite while its dependant is in
}

# Why each product earns its place, in words an owner understands.
PRODUCT_REASON = {
    "P01": "a reliable 100 Mbps business line so billing, UPI and day-to-day work never stall",
    "P02": "300 Mbps so several computers, card machines and cameras run together without slowing down",
    "P03": "a 1 Gbps priority line for a site that cannot afford to be slow",
    "P04": "5G wireless broadband — it installs in about 2 days, with no fibre digging or wait",
    "P05": "a dedicated 20 Mbps leased line with a 99.5% uptime guarantee, so production never waits on the network",
    "P06": "a dedicated 100 Mbps leased line with a 99.9% uptime guarantee for heavy plant use",
    "P07": "business mobile lines with unlimited calls, so the shop number is separate from your personal one",
    "P08": "business mobile lines with 150 GB, roaming and priority support for staff who travel",
    "P09": "an automatic backup connection that takes over the moment the main line drops",
    "P10": "central management across all your sites from one dashboard, instead of calling each location",
    "P11": "a fixed public IP so you can view cameras and systems from anywhere",
    "P12": "a business phone system with IVR and call recording, so no customer call is missed",
    "P13": "a 1800 toll-free number customers can call at no cost",
    "P14": "10,000 SMS a month for appointment reminders and offers",
    "P15": "a verified WhatsApp channel with your catalogue, where your customers already are",
    "P16": "business email on your own domain, instead of a personal Gmail for official work",
    "P17": "automatic 1 TB backup, so a stolen or broken computer never costs you your records",
    "P18": "a domain and simple website so customers can find you online",
    "P19": "GST-ready billing with inventory and UPI/card acceptance on a tablet",
    "P20": "antivirus and ransomware protection on every computer and phone",
    "P21": "a managed firewall watched by Accel's security team, so you do not need an IT person",
    "P22": "managed Wi-Fi with a separate guest network, so customer Wi-Fi never touches your billing system",
    "P23": "HD cameras with 30 days of cloud recording you can view from your phone",
    "P24": "sensors on your machines to track uptime and energy use",
    "P25": "GPS tracking for your vehicles with a live dashboard",
}


@dataclass
class LineItem:
    product_id: str
    name: str
    category: str
    unit: str
    quantity: int
    unit_price_inr: int
    monthly_inr: int
    setup_inr: int
    contract_months: int
    install_days: int
    reason: str
    priority: int
    is_addon: bool = True          # anchors and prerequisites are not toggleable

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "category": self.category,
            "unit": self.unit,
            "quantity": self.quantity,
            "unit_price_inr": self.unit_price_inr,
            "monthly_inr": self.monthly_inr,
            "setup_inr": self.setup_inr,
            "contract_months": self.contract_months,
            "install_days": self.install_days,
            "reason": self.reason,
            "is_addon": self.is_addon,
        }


@dataclass
class Bundle:
    items: list[LineItem] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- totals --------------------------------------------------------
    @property
    def monthly_inr(self) -> int:
        return sum(i.monthly_inr for i in self.items)

    @property
    def setup_inr(self) -> int:
        return sum(i.setup_inr for i in self.items)

    @property
    def contract_months(self) -> int:
        return max((i.contract_months for i in self.items), default=0)

    @property
    def install_days(self) -> int:
        return max((i.install_days for i in self.items), default=0)

    def to_dict(self, profile: Optional[BusinessProfile] = None) -> dict:
        data = {
            "items": [i.to_dict() for i in self.items],
            "monthly_inr": self.monthly_inr,
            "setup_inr": self.setup_inr,
            "contract_months": self.contract_months,
            "install_days": self.install_days,
            "excluded": self.excluded,
            "notes": self.notes,
        }
        if profile and profile.monthly_spend_inr:
            saving = profile.monthly_spend_inr - self.monthly_inr
            data["current_spend_inr"] = profile.monthly_spend_inr
            data["monthly_saving_inr"] = saving
            data["annual_saving_inr"] = saving * 12
        return data


# ----------------------------------------------------------------------
# Eligibility — the heart of the thing
# ----------------------------------------------------------------------
def check_eligibility(product: dict, profile: BusinessProfile) -> Optional[dict]:
    """Return None if eligible, else every catalogue rule the business fails.

    All blocking rules are reported, not just the first. A 2-person single-site
    shop asking for SD-WAN fails both the headcount and the site-count rule,
    and a partner looking at the back-office view deserves to see both.
    """
    pid = product["product_id"]
    name = product["product_name"]
    codes: list[str] = []
    reasons: list[str] = []

    if str(profile.city_tier) not in product["available_city_tiers"]:
        tiers = " and ".join(product["available_city_tiers"])
        codes.append("CITY_TIER")
        reasons.append(
            f"it is sold only in tier {tiers} cities and "
            f"{profile.city or 'this location'} is tier {profile.city_tier}"
        )

    if profile.employee_count < product["min_employees"]:
        codes.append("MIN_EMPLOYEES")
        reasons.append(
            f"it needs at least {product['min_employees']} employees "
            f"and this business has {profile.employee_count}"
        )

    if profile.num_sites < product["min_sites"]:
        codes.append("MIN_SITES")
        reasons.append(
            f"it needs at least {product['min_sites']} locations "
            f"and this business has {profile.num_sites}"
        )

    if product["requires_gstin"] and not (profile.has_gstin and profile.gstin_active):
        detail = ("the GSTIN on file is not active" if profile.has_gstin
                  else "this business has no GSTIN")
        codes.append("REQUIRES_GSTIN")
        reasons.append(
            f"it can only be sold against an active GST registration, and {detail}"
        )

    if not codes:
        return None

    if len(reasons) == 1:
        detail = reasons[0]
    else:
        detail = ", ".join(reasons[:-1]) + f", and {reasons[-1]}"
    return {
        "product_id": pid,
        "name": name,
        "code": codes[0],
        "codes": codes,
        "reason": f"{name} was not offered because {detail}.",
    }


def eligible_products(profile: BusinessProfile) -> tuple[list[dict], list[dict]]:
    """Split the whole catalogue into (eligible, excluded-with-reason)."""
    allowed, blocked = [], []
    for product in get_store().all_products():
        failure = check_eligibility(product, profile)
        (blocked if failure else allowed).append(failure or product)
    return allowed, blocked


# ----------------------------------------------------------------------
# Quantities — "per site", "per user", "per camera" all mean something
# ----------------------------------------------------------------------
def quantity_for(product: dict, profile: BusinessProfile) -> int:
    unit = product["unit"]
    pid = product["product_id"]
    staff = max(1, profile.employee_count)

    if unit == "per site":
        return profile.num_sites
    if unit == "per line":
        # Owner plus roughly one line per five staff, capped so we never
        # over-sell a small business.
        return max(1, min(10, 1 + staff // 5))
    if unit == "per user":
        return min(staff, 50)
    if unit == "per device":
        if pid == "P24":  # machine monitoring
            return max(1, profile.num_machines or 5)
        return min(staff, 50)
    if unit == "per camera":
        return profile.num_sites * 2
    if unit == "per counter":
        return profile.num_sites
    if unit == "per channel":
        return max(2, min(6, staff // 10 + 2))
    if unit == "per vehicle":
        return max(1, profile.num_vehicles or 3)
    # per account, per IP, per number, per pack
    return 1


def _line_item(product: dict, profile: BusinessProfile, priority: int,
               is_addon: bool = True) -> LineItem:
    qty = quantity_for(product, profile)
    # We quote the floor of the published range. A transparent, defensible
    # price beats a negotiable one for a self-serve journey.
    unit_price = product["price_min_inr_per_month"]
    return LineItem(
        product_id=product["product_id"],
        name=product["product_name"],
        category=product["category"],
        unit=product["unit"],
        quantity=qty,
        unit_price_inr=unit_price,
        monthly_inr=unit_price * qty,
        setup_inr=product["one_time_setup_inr"] * (qty if product["unit"] == "per site" else 1),
        contract_months=product["min_contract_months"],
        install_days=product["typical_install_days"],
        reason=PRODUCT_REASON.get(product["product_id"], product["description"]),
        priority=priority,
        is_addon=is_addon,
    )


# ----------------------------------------------------------------------
# Bundle construction
# ----------------------------------------------------------------------
def _pick_anchor(allowed_ids: set[str], profile: BusinessProfile) -> Optional[dict]:
    """Choose exactly one connectivity product to anchor the bundle."""
    store = get_store()
    segment = profile.normalised_segment()
    preference = list(ANCHOR_PREFERENCE.get(segment, ANCHOR_PREFERENCE["micro"]))

    # A business that explicitly wants fast installation, or has no fibre
    # today, is better served by 5G FWA (2-day install) than by fibre.
    wants_speed = "uptime" in profile.effective_needs() and segment == "micro"
    if profile.current_internet.lower() in {"mobile hotspot", "none", ""} or wants_speed:
        if "P04" in preference:
            preference.remove("P04")
            preference.insert(0, "P04")

    budget = profile.budget_inr
    fallback = None
    for pid in preference:
        if pid not in allowed_ids:
            continue
        product = store.product(pid)
        fallback = fallback or product
        cost = product["price_min_inr_per_month"] * quantity_for(product, profile)
        if budget is None or cost <= budget:
            return product
    return fallback


def build_bundle(profile: BusinessProfile) -> Bundle:
    """Produce a priced, dependency-complete, eligibility-safe bundle."""
    store = get_store()
    allowed, blocked = eligible_products(profile)
    allowed_ids = {p["product_id"] for p in allowed}
    bundle = Bundle(excluded=blocked)

    chosen: dict[str, LineItem] = {}

    # --- 1. the connectivity anchor ----------------------------------
    anchor = _pick_anchor(allowed_ids, profile)
    if anchor:
        chosen[anchor["product_id"]] = _line_item(
            anchor, profile, PRIORITY["anchor"], is_addon=False
        )
    else:
        bundle.notes.append(
            "No connectivity product is available for this location and business size. "
            "A field visit is needed to check feasibility."
        )

    # --- 2. need-driven products -------------------------------------
    stated = set(profile.needs)
    for need in profile.effective_needs():
        if need == "internet":
            continue
        priority = (PRIORITY["phone_lines"] if need == "phone_lines"
                    else PRIORITY["stated_need"] if need in stated
                    else PRIORITY["implied_need"])
        for pid in NEED_PRODUCTS.get(need, []):
            if pid in chosen:
                break
            if pid not in allowed_ids:
                continue
            chosen[pid] = _line_item(store.product(pid), profile, priority)
            break

    # --- 3. resolve requires_one_of dependencies ---------------------
    for pid in list(chosen):
        product = store.product(pid)
        prerequisites = product["requires_one_of"]
        if not prerequisites or any(p in chosen for p in prerequisites):
            continue
        # Add the cheapest eligible prerequisite rather than dropping the
        # product the owner actually asked for.
        candidates = [store.product(p) for p in prerequisites if p in allowed_ids]
        if candidates:
            cheapest = min(candidates, key=lambda p: p["price_min_inr_per_month"])
            chosen[cheapest["product_id"]] = _line_item(
                cheapest, profile, PRIORITY["dependency"], is_addon=False
            )
            bundle.notes.append(
                f"{product['product_name']} needs a main connection, so "
                f"{cheapest['product_name']} was added to the bundle."
            )
        else:
            del chosen[pid]
            bundle.excluded.append({
                "product_id": pid, "name": product["product_name"],
                "code": "DEPENDENCY_UNMET",
                "reason": f"{product['product_name']} needs a main internet connection "
                          f"that this location is not eligible for.",
            })

    # --- 4. trim to budget -------------------------------------------
    if profile.budget_inr:
        while chosen and sum(i.monthly_inr for i in chosen.values()) > profile.budget_inr:
            # Drop the lowest-priority, most expensive add-on first.
            droppable = [i for i in chosen.values() if i.is_addon]
            if not droppable:
                break
            victim = min(droppable, key=lambda i: (i.priority, -i.monthly_inr))
            del chosen[victim.product_id]
            bundle.excluded.append({
                "product_id": victim.product_id, "name": victim.name,
                "code": "OVER_BUDGET",
                "reason": f"{victim.name} was left out to stay within the "
                          f"₹{profile.budget_inr:,}/month budget. It can be added later.",
            })

    # --- 5. order the bundle for display -----------------------------
    order = {"Connectivity": 0, "Communication": 1, "Cloud & Productivity": 2,
             "Security": 3, "IoT & Physical": 4}
    bundle.items = sorted(
        chosen.values(),
        key=lambda i: (order.get(i.category, 9), -i.priority, i.product_id),
    )
    return bundle


def reprice(profile: BusinessProfile, product_ids: list[str]) -> dict:
    """Re-cost an owner-edited bundle, re-running every rule.

    The checkout screen lets people toggle add-ons, so the chosen set has to be
    validated again rather than trusted. Toggling must not be a way to put an
    ineligible product into a basket.
    """
    store = get_store()
    allowed, blocked = eligible_products(profile)
    allowed_ids = {p["product_id"] for p in allowed}
    blocked_by_id = {b["product_id"]: b for b in blocked}

    # Start from the full set of catalogue exclusions, exactly as the first
    # recommendation does. Editing the basket must not make the "why wasn't
    # this offered" explanation disappear — that transparency is the point.
    bundle = Bundle(excluded=list(blocked))
    chosen: dict[str, LineItem] = {}
    wanted = [pid.strip().upper() for pid in product_ids if pid]

    for pid in wanted:
        product = store.product(pid)
        if product is None:
            continue
        if pid not in allowed_ids:
            if pid not in blocked_by_id:        # already listed otherwise
                bundle.excluded.append({
                    "product_id": pid, "name": product["product_name"],
                    "code": "NOT_ELIGIBLE",
                    "reason": f"{product['product_name']} is not available for "
                              f"this business.",
                })
            continue
        is_anchor = product["category"] == "Connectivity" and not any(
            i.category == "Connectivity" for i in chosen.values())
        chosen[pid] = _line_item(
            product, profile,
            PRIORITY["anchor"] if is_anchor else PRIORITY["stated_need"],
            is_addon=not is_anchor,
        )

    # Dependencies still apply to a hand-edited basket.
    for pid in list(chosen):
        product = store.product(pid)
        prerequisites = product["requires_one_of"]
        if not prerequisites or any(p in chosen for p in prerequisites):
            continue
        candidates = [store.product(p) for p in prerequisites if p in allowed_ids]
        if candidates:
            cheapest = min(candidates, key=lambda p: p["price_min_inr_per_month"])
            chosen[cheapest["product_id"]] = _line_item(
                cheapest, profile, PRIORITY["dependency"], is_addon=False)
            bundle.notes.append(
                f"{product['product_name']} needs a main connection, so "
                f"{cheapest['product_name']} was added back.")
        else:
            del chosen[pid]
            bundle.excluded.append({
                "product_id": pid, "name": product["product_name"],
                "code": "DEPENDENCY_UNMET",
                "reason": f"{product['product_name']} needs a main internet "
                          f"connection, which is not available here.",
            })

    order = {"Connectivity": 0, "Communication": 1, "Cloud & Productivity": 2,
             "Security": 3, "IoT & Physical": 4}
    bundle.items = sorted(chosen.values(),
                          key=lambda i: (order.get(i.category, 9), i.product_id))
    return {"profile": profile.to_dict(), "bundle": bundle.to_dict(profile)}


def available_addons(profile: BusinessProfile, current: list[str]) -> list[dict]:
    """Eligible products not already in the basket, for the add-on picker."""
    store = get_store()
    allowed, _ = eligible_products(profile)
    have = {p.upper() for p in current}
    segment = profile.normalised_segment()
    out = []
    for product in allowed:
        pid = product["product_id"]
        if pid in have or segment not in product["target_segments"]:
            continue
        quantity = quantity_for(product, profile)
        out.append({
            "product_id": pid,
            "name": product["product_name"],
            "category": product["category"],
            "reason": PRODUCT_REASON.get(pid, product["description"]),
            "quantity": quantity,
            "monthly_inr": product["price_min_inr_per_month"] * quantity,
            "setup_inr": product["one_time_setup_inr"],
        })
    return sorted(out, key=lambda p: p["monthly_inr"])


def recommend(profile: BusinessProfile) -> dict:
    """Top-level entry point: profile in, priced recommendation out."""
    bundle = build_bundle(profile)
    return {
        "profile": profile.to_dict(),
        "bundle": bundle.to_dict(profile),
    }

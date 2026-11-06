"""Phase 4 acceptance tests — quote editing, DPDP consent, checkout, tracker."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import advisor as svc
from app.consent import PURPOSES, ConsentLedger, consent_notice
from app.data import get_store
from app.engine import available_addons, check_eligibility, reprice
from app.orders import create_order, get_order
from app.profile import BusinessProfile

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
clinic = BusinessProfile(
    business_type="dental clinic", city="Kochi", city_tier=2,
    employee_count=20, num_sites=1, has_gstin=True, gstin_active=True,
    current_internet="Business broadband", monthly_spend_inr=3050,
    needs=["email", "cctv"],
)

print("\n=== Re-pricing an owner-edited basket ===")
out = reprice(clinic, ["P02", "P16", "P23"])
ids = [i["product_id"] for i in out["bundle"]["items"]]
check("basket priced", ids, ["P02", "P16", "P23"])
check("total is the sum of the lines",
      out["bundle"]["monthly_inr"],
      sum(i["monthly_inr"] for i in out["bundle"]["items"]))
check("saving recomputed against current spend",
      out["bundle"]["monthly_saving_inr"],
      3050 - out["bundle"]["monthly_inr"])

print("\n=== Toggling cannot smuggle in an ineligible product ===")
# P03 is tier-1 only, P10 needs 3 sites. Neither may enter a Kochi basket.
out = reprice(clinic, ["P02", "P03", "P10", "P16"])
ids = [i["product_id"] for i in out["bundle"]["items"]]
check_true("P03 refused", "P03" not in ids)
check_true("P10 refused", "P10" not in ids)
check_true("the eligible items survived", {"P02", "P16"} <= set(ids))
check_true("refusals are explained",
           all(e.get("reason") for e in out["bundle"]["excluded"]))

print("\n=== Removing a prerequisite re-adds it ===")
# P23 CCTV needs a main connection; dropping every link must not leave it orphaned.
out = reprice(clinic, ["P23"])
ids = [i["product_id"] for i in out["bundle"]["items"]]
check_true("CCTV still present", "P23" in ids)
check_true("a connection was added back",
           any(p in ids for p in store.product("P23")["requires_one_of"]))
check_true("and the owner is told why",
           any("added back" in n for n in out["bundle"]["notes"]))

print("\n=== Every edited basket stays valid ===")
import itertools
violations = []
catalogue = [p["product_id"] for p in store.all_products()]
for combo in itertools.islice(itertools.combinations(catalogue, 3), 300):
    out = reprice(clinic, list(combo))
    chosen = {i["product_id"] for i in out["bundle"]["items"]}
    for item in out["bundle"]["items"]:
        product = store.product(item["product_id"])
        if check_eligibility(product, clinic):
            violations.append((combo, item["product_id"], "ELIGIBILITY"))
        prereqs = product["requires_one_of"]
        if prereqs and not (set(prereqs) & chosen):
            violations.append((combo, item["product_id"], "DEPENDENCY"))
check("300 arbitrary baskets, zero invalid", violations, [])

print("\n=== The transparency panel survives basket edits ===")
# Regression: reprice() used to list only products the owner had explicitly
# asked for and been refused, so the "why wasn't this offered" panel vanished
# the moment anything was added or removed.
first = reprice(clinic, ["P02", "P16", "P23"])
edited = reprice(clinic, ["P02", "P11", "P16"])
check_true("exclusions are listed on a fresh quote", len(first["bundle"]["excluded"]) > 0)
check_true("and still listed after editing", len(edited["bundle"]["excluded"]) > 0)
check("the same products are excluded either way",
      {e["product_id"] for e in first["bundle"]["excluded"]},
      {e["product_id"] for e in edited["bundle"]["excluded"]})
codes = {e["product_id"] for e in edited["bundle"]["excluded"]}
check_true("tier-1-only P03 is explained", "P03" in codes)
check_true("multi-site P10 is explained", "P10" in codes)
ids_e = {i["product_id"] for i in edited["bundle"]["items"]}
check_true("nothing excluded is also in the basket", not (codes & ids_e))
check("no duplicate entries",
      len(edited["bundle"]["excluded"]),
      len({e["product_id"] for e in edited["bundle"]["excluded"]}))

print("\n=== Add-on picker ===")
addons = available_addons(clinic, ["P02", "P16"])
keys = {a["product_id"] for a in addons}
check_true("already-chosen items are not offered again", not ({"P02", "P16"} & keys))
check_true("ineligible items are not offered", "P03" not in keys and "P10" not in keys)
check_true("every add-on carries a price", all(a["monthly_inr"] > 0 for a in addons))
check_true("every add-on explains itself", all(a["reason"] for a in addons))

print("\n=== DPDP consent ===")
notice = consent_notice()
check("four separate purposes", len(notice["purposes"]), 4)
required = [p for p in notice["purposes"] if p["required"]]
check("only two are required", len(required), 2)
check_true("marketing is optional",
           not PURPOSES["marketing"]["required"])
check_true("every purpose states its retention period",
           all(p["retention"] for p in notice["purposes"]))
check_true("every purpose lists the data it covers",
           all(p["data"] for p in notice["purposes"]))
check_true("data principal rights are listed", len(notice["rights"]) >= 4)

ledger = ConsentLedger()
ledger.record("identity_verification", True)
ledger.record("service_delivery", True)
ledger.record("service_updates", True)
ledger.record("marketing", False)
check("required consents satisfied", ledger.missing_required(), [])
check("refusing marketing is recorded", ledger.granted("marketing"), False)
check_true("the notice text is stored with the consent",
           all(r.notice_shown for r in ledger.records if r.granted))
check_true("every consent is timestamped",
           all(r.timestamp for r in ledger.records))

ledger.withdraw("service_updates")
check("withdrawal takes effect", ledger.granted("service_updates"), False)
check("the ledger is append-only — nothing deleted", len(ledger.records), 4)
check_true("withdrawal is timestamped too",
           any(r.withdrawn_at for r in ledger.records))

partial = ConsentLedger()
partial.record("identity_verification", True)
partial.record("service_delivery", False)
check("refusing a required purpose blocks processing",
      partial.missing_required(), ["service_delivery"])

print("\n=== Order and tracker ===")
quote = reprice(clinic, ["P02", "P16", "P23"])
full = ConsentLedger()
for purpose in PURPOSES:
    full.record(purpose, True)
order = create_order(quote["profile"], quote["bundle"], full,
                     contact_name="Dr Nair")
data = order.to_dict()
check_true("order id looks like UD-XXXXXX", data["order_id"].startswith("UD-"))
check("expected go-live uses the slowest install in the bundle",
      data["expected_live"],
      (order.placed_on + __import__("datetime").timedelta(
          days=quote["bundle"]["install_days"])).isoformat())
check_true("a timeline was built", len(data["timeline"]) >= 5)
check("first milestone is current", data["timeline"][0]["state"], "current")
check_true("every milestone carries a date",
           all(step["on"] for step in data["timeline"]))
check_true("every milestone explains itself in plain words",
           all(len(step["detail"]) > 20 for step in data["timeline"]))
check("fibre bundle needs a site visit", data["needs_site_visit"], True)

steps = len(data["timeline"])
for _ in range(steps):
    order.advance()
done = order.to_dict()
check("order reaches live", done["is_live"], True)
check("progress hits 100%", done["progress_pct"], 100)
check("status reads Live", done["status"], "Live")

print("\n=== Cloud-only bundles skip the engineer visit ===")
cloud = reprice(clinic, ["P16", "P20"])
cloud_order = create_order(cloud["profile"], cloud["bundle"], full)
labels = [s["label"] for s in cloud_order.to_dict()["timeline"]]
check("no site visit for a cloud-only bundle",
      cloud_order.to_dict()["needs_site_visit"], False)
check_true("so no engineer milestone", "Engineer visit scheduled" not in labels)
check_true("but it still reaches Live", "Live" in labels)

print("\n=== Lookups ===")
check_true("order retrievable by id", get_order(order.order_id) is not None)
check_true("lookup is case-insensitive",
           get_order(order.order_id.lower()) is not None)
check("unknown order returns nothing", get_order("UD-NOPE1"), None)

print("\n" + "=" * 54)
print(f"  {sum(results)}/{len(results)} checks passed")
print("=" * 54)
sys.exit(0 if all(results) else 1)

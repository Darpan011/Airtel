"""Phase 0 acceptance tests — data loading, validators, registry lookups."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data import get_store
from app.registry import gstin_lookup, udyam_lookup
from app.validators import gstin_checksum_ok, mask_gstin, validate_gstin_format

PASS, FAIL = "  PASS", "  FAIL"
results = []


def check(label: str, got, want) -> None:
    ok = got == want
    results.append(ok)
    print(f"{PASS if ok else FAIL}  {label}")
    if not ok:
        print(f"         expected {want!r}\n         got      {got!r}")


print("\n=== Data load ===")
store = get_store()
check("25 products loaded", len(store.products), 25)
check("3534 GSTIN records", len(store.gstin_registry), 3534)
check("2723 Udyam records", len(store.udyam_registry), 2723)
check("5000 leads", len(store.leads), 5000)

print("\n=== Catalogue parsing ===")
p03 = store.product("P03")
check("P03 is tier-1 only", p03["available_city_tiers"], ["1"])
check("P03 requires GSTIN", p03["requires_gstin"], True)
check("P03 min_employees = 5", p03["min_employees"], 5)
p10 = store.product("P10")
check("P10 needs >=3 sites", p10["min_sites"], 3)
check("P10 dependencies parsed", p10["requires_one_of"],
      ["P01", "P02", "P03", "P05", "P06"])
check("P23 install days = 4", store.product("P23")["typical_install_days"], 4)

print("\n=== GSTIN checksum ===")
check("valid GSTIN passes", gstin_checksum_ok("27XUIXG0767D1ZP"), True)
check("valid GSTIN passes (2)", gstin_checksum_ok("32TZAXB5481U1Z1"), True)
check("altered GSTIN fails (K01)", gstin_checksum_ok("37THKXV5167L1ZS"), False)
check("altered GSTIN fails (K06)", gstin_checksum_ok("08YKZXS8189L1Z0"), False)
check("short string fails", gstin_checksum_ok("27XUIXG"), False)
check("format: accepts X entity char",
      validate_gstin_format("27XUIXG0767D1ZP")["ok"], True)

print("\n=== Masking (DPDP) ===")
check("GSTIN masked", mask_gstin("27XUIXG0767D1ZP"), "27XU*******D1ZP")

print("\n=== GSTIN lookup ===")
r = gstin_lookup("27XUIXG0767D1ZP")
check("active GSTIN verified", r["verified"], True)
check("legal name returned", r["record"]["legal_name"],
      "Joseph Singh (Prop. Ganesh Woodcraft)")
check("raw GSTIN never echoed", "27XUIXG0767D1ZP" in str(r), False)

r = gstin_lookup("37THKXV5167L1ZS")
check("bad checksum rejected", r["code"], "GSTIN_CHECKSUM")

r = gstin_lookup("NOTAGSTIN")
check("malformed rejected", r["code"], "GSTIN_LENGTH")

# A GSTIN that is structurally valid but cancelled in the registry.
cancelled = next(
    (g for g, rec in store.gstin_registry.items() if rec["status"] == "Cancelled"),
    None,
)
r = gstin_lookup(cancelled)
check("cancelled GSTIN not verified", r["verified"], False)
check("cancelled GSTIN code", r["code"], "GSTIN_CANCELLED")

print("\n=== Udyam lookup ===")
r = udyam_lookup("UDYAM-MH-00-9516696")
check("udyam verified", r["verified"], True)
check("enterprise name", r["record"]["enterprise_name"], "Ganesh Woodcraft")
check("enterprise type", r["record"]["enterprise_type"], "Micro")

check("blank udyam rejected", udyam_lookup("")["code"], "UDYAM_MISSING")
check("bad format rejected", udyam_lookup("UDYAM-MH-9516696")["code"], "UDYAM_FORMAT")
check("unknown udyam rejected", udyam_lookup("UDYAM-MH-00-1111111")["code"],
      "UDYAM_NOT_FOUND")

print("\n" + "=" * 46)
print(f"  {sum(results)}/{len(results)} checks passed")
print("=" * 46)
sys.exit(0 if all(results) else 1)

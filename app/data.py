"""Loads the synthetic data pack once at startup and exposes lookup helpers.

The two mock registries stand in for the real GSTIN and Udyam lookup APIs.
Everything is loaded into dictionaries keyed by identifier so lookups are O(1)
and the prototype stays dependency-light.
"""
from functools import lru_cache
from typing import Optional

import pandas as pd

from .config import DATA_DIR


def _read(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / name, dtype=str, keep_default_na=False)


class DataStore:
    """In-memory store for the catalogue, registries and leads."""

    def __init__(self) -> None:
        # ---- Product catalogue -------------------------------------------
        cat = _read("product_catalogue.csv")
        numeric = [
            "price_min_inr_per_month", "price_max_inr_per_month",
            "one_time_setup_inr", "min_contract_months", "min_employees",
            "min_sites", "typical_install_days",
        ]
        for col in numeric:
            cat[col] = pd.to_numeric(cat[col], errors="coerce").fillna(0).astype(int)

        self.products: dict[str, dict] = {}
        for row in cat.to_dict("records"):
            row["available_city_tiers"] = [
                t for t in str(row["available_city_tiers"]).split(";") if t
            ]
            row["requires_one_of"] = [
                p for p in str(row["requires_one_of"]).split("|") if p
            ]
            row["target_segments"] = [
                s for s in str(row["target_segments"]).split(";") if s
            ]
            row["requires_gstin"] = row["requires_gstin"] == "Y"
            self.products[row["product_id"]] = row

        # ---- Mock GSTIN registry ------------------------------------------
        gst = _read("mock_gstin_registry.csv")
        self.gstin_registry: dict[str, dict] = {
            r["gstin"]: r for r in gst.to_dict("records")
        }

        # ---- Mock Udyam registry ------------------------------------------
        udyam = _read("mock_udyam_registry.csv")
        self.udyam_registry: dict[str, dict] = {
            r["udyam_number"]: r for r in udyam.to_dict("records")
        }

        # Reverse index: enterprise name -> udyam record. Lets the advisor
        # offer "we found your Udyam registration" from a business name alone.
        self.udyam_by_name: dict[str, dict] = {
            r["enterprise_name"].strip().lower(): r
            for r in self.udyam_registry.values()
        }

        # ---- Leads (used by the back-office view and propensity model) ----
        self.leads = _read("smb_leads.csv")

        # City -> tier, learned from the leads file so the advisor never has to
        # ask "is your city tier 1 or tier 2?" — a question no owner can answer.
        self.city_tiers: dict[str, int] = {}
        for row in self.leads[["city", "city_tier"]].to_dict("records"):
            city = row["city"].strip().lower()
            if city and city not in self.city_tiers:
                self.city_tiers[city] = int(row["city_tier"])

    # ------------------------------------------------------------------
    # Registry lookups — these back the mock GSTIN / Udyam APIs
    # ------------------------------------------------------------------
    def lookup_gstin(self, gstin: str) -> Optional[dict]:
        return self.gstin_registry.get((gstin or "").strip().upper())

    def lookup_udyam(self, number: str) -> Optional[dict]:
        return self.udyam_registry.get((number or "").strip().upper())

    def udyam_for_business(self, name: str) -> Optional[dict]:
        return self.udyam_by_name.get((name or "").strip().lower())

    def city_tier(self, city: str) -> int:
        """Resolve a city to its tier. Unknown cities default to tier 2, which
        is the conservative choice: it never offers a product the location
        cannot actually get."""
        return self.city_tiers.get((city or "").strip().lower(), 2)

    def product(self, product_id: str) -> Optional[dict]:
        return self.products.get((product_id or "").strip().upper())

    def all_products(self) -> list[dict]:
        return list(self.products.values())


@lru_cache(maxsize=1)
def get_store() -> DataStore:
    """Singleton accessor — the CSVs are parsed exactly once per process."""
    return DataStore()

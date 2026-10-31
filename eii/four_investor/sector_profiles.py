"""Sector profiles for the industry-level four-investor parameters.

Some parameters describe the industry more than the company (GDP sensitivity, regulatory
dependence, commodity exposure, disruption risk, market growth). For these the app uses a
profile of the company's NSE sector, rated 0–4 where higher is always better for the investor
(e.g. 4 = least GDP-sensitive, 4 = least government-dependent).

These are documented judgement calls, not measurements: they are labelled "Sector profile",
carry Low confidence, and a Claude Code research run (company-specific evidence) overrides them.
Grounding: the usual cyclical/defensive split of Indian sectors (metals, capital goods,
infrastructure, autos cyclical; FMCG, pharma, utilities defensive), sector regulation
(RBI, CERC/SERC, DPCO, TRAI) and import dependence (pharma APIs, electronics, crude).

The profile also says which parameters apply at all: patents and new-product revenue are not
meaningful for a power utility, brand is not for a steelmaker, dealer networks are not for an
EPC contractor. Those are excluded from coverage, as the master prompt allows ("document the
substitution or exclusion and disclose the revised denominator").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

PROFILE_PARAMS = (
    "munger.quality.understandable", "munger.quality.industry_durability", "munger.quality.complexity",
    "munger.protection.disruption", "dalio.cycle.gdp", "dalio.cycle.cycle_position", "dalio.macro.commodity",
    "dalio.macro.supply_chain", "dalio.macro.government", "dalio.macro.geopolitical", "fisher.growth.tam",
    "fisher.growth.penetration", "fisher.growth.demand_visibility", "fisher.competition.retention",
    "fisher.innovation.adaptability",
)
INNOVATION_PARAMS = ("fisher.innovation.rnd", "fisher.innovation.new_products", "fisher.innovation.patents",
                     "fisher.innovation.new_product_revenue")


@dataclass(frozen=True)
class SectorProfile:
    ratings: tuple[int, ...]  # aligned with PROFILE_PARAMS
    innovation: bool  # R&D, new products and patents drive competitiveness
    brand: bool  # consumer-facing brand matters
    distribution: bool  # dealer / retail network matters
    rationale: str


def _p(ratings: str, innovation: bool, brand: bool, distribution: bool, rationale: str) -> SectorProfile:
    vals = tuple(int(c) for c in ratings.split())
    assert len(vals) == len(PROFILE_PARAMS)
    return SectorProfile(vals, innovation, brand, distribution, rationale)


#               understand durab complex disrupt gdp cycle commod supply govt geopol tam penetr visib retain adapt
PROFILES: dict[str, SectorProfile] = {
    "Power": _p("3 4 2 3 3 3 1 2 1 3 3 2 4 4 2", False, False, False,
                "Regulated, contracted demand (long-term PPAs) gives high visibility; fuel (coal/gas) is a commodity "
                "input; tariffs and dues depend on state discoms and regulators; demand grows with the economy."),
    "Utilities": _p("4 4 3 3 4 3 2 3 1 3 3 3 4 4 2", False, False, False,
                    "City gas / water / utility networks: monopoly-like regulated franchises, defensive demand, "
                    "regulator-set returns."),
    "Oil Gas & Consumable Fuels": _p("3 3 2 1 2 2 0 1 1 1 2 2 2 3 2", False, False, True,
                                     "Commodity-priced (crude, gas, coal), import-dependent, government-controlled "
                                     "pricing and ownership, long-run energy-transition risk."),
    "Metals & Mining": _p("3 3 2 3 1 1 0 2 2 1 2 2 1 2 2", False, False, False,
                          "Deep-cyclical, price-taking commodity producers; earnings swing with global prices and "
                          "Chinese supply; mining licences are government-dependent."),
    "Capital Goods": _p("2 3 1 2 1 2 2 2 2 3 3 3 3 2 3", True, False, False,
                        "Investment-cycle exposure (private and government capex); order books give 2–3 years of "
                        "visibility; engineering know-how and product development matter."),
    "Financial Services": _p("2 4 1 2 2 2 4 4 1 3 4 4 2 3 2", False, True, True,
                             "Credit growth tracks nominal GDP; heavily regulated (RBI/SEBI/IRDAI); balance sheets "
                             "are complex and leveraged; long financial-deepening runway in India."),
    "Healthcare": _p("2 4 2 2 4 3 3 1 2 2 3 3 3 3 3", True, True, True,
                     "Defensive demand; pricing controls (DPCO) and USFDA compliance; dependence on imported APIs/KSMs; "
                     "R&D pipeline and patents drive returns."),
    "Automobile and Auto Components": _p("3 4 2 1 1 2 1 1 3 2 3 3 2 3 2", True, True, True,
                                         "Cyclical consumer and freight demand; metal input costs; electronics and "
                                         "chip supply chains; electric-vehicle transition is a disruption risk."),
    "Fast Moving Consumer Goods": _p("4 4 3 3 4 3 2 3 3 3 3 3 3 4 3", True, True, True,
                                     "Repeat-purchase staples with stable demand; agri-commodity input costs; brands "
                                     "and distribution reach are the moat."),
    "Chemicals": _p("2 3 2 3 2 2 1 1 3 1 3 3 2 3 3", True, False, False,
                    "Specialty and commodity chemicals: input prices follow crude, Chinese competition and dumping, "
                    "export-dependent, process R&D matters."),
    "Consumer Services": _p("3 3 2 2 2 3 3 3 3 3 4 4 2 3 3", False, True, True,
                            "Retail, travel, hotels, food services and platforms: discretionary demand with a long "
                            "consumption runway; brand and footprint matter."),
    "Information Technology": _p("3 3 2 1 3 3 4 4 3 1 3 3 3 4 3", True, True, False,
                                 "Export services with multi-year client relationships; revenue tied to US/EU tech "
                                 "budgets and the rupee; AI-driven automation is a disruption risk."),
    "Consumer Durables": _p("3 3 2 2 2 3 2 2 3 3 3 4 2 3 3", True, True, True,
                            "Discretionary demand with low penetration; metal and electronics inputs, part imported; "
                            "brands and dealer networks matter."),
    "Services": _p("3 3 2 2 2 3 2 3 3 3 3 3 2 3 2", False, False, False,
                   "Logistics, ports, staffing and business services: activity-linked demand, moderate cyclicality."),
    "Construction": _p("2 3 1 3 1 3 1 2 1 3 3 3 3 2 2", False, False, False,
                       "EPC and infrastructure contractors: order-book visibility, dependence on government spending "
                       "and payments, working-capital heavy, steel and cement input costs."),
    "Realty": _p("3 3 2 3 1 2 2 3 2 4 3 3 1 1 2", False, True, False,
                 "Interest-rate and income-cycle sensitive; one-time buyers; approvals are state-regulated; "
                 "domestic business."),
    "Construction Materials": _p("4 4 3 3 1 2 1 3 3 4 3 3 2 3 2", False, True, True,
                                 "Cement and building materials: simple, durable, regional businesses tied to "
                                 "construction cycles; fuel (coal/petcoke) and freight costs; dealer networks."),
    "Textiles": _p("3 3 2 3 2 2 1 2 3 1 2 2 2 2 2", False, False, False,
                   "Fragmented, cotton/yarn-price driven, export-exposed (US/EU), thin margins."),
    "Telecommunication": _p("3 4 2 2 4 3 3 2 1 3 3 2 4 3 2", True, True, True,
                            "Subscription revenue with high visibility; spectrum, tariffs and licences are "
                            "government-controlled; imported network equipment."),
    "Media Entertainment & Publication": _p("3 2 2 1 2 3 3 3 2 3 2 2 2 2 2", False, True, False,
                                            "Advertising-cyclical; structural shift to digital platforms threatens "
                                            "legacy print and broadcast models."),
    "Diversified": _p("1 3 0 2 2 2 2 2 2 2 2 2 2 2 2", False, False, False,
                      "Conglomerate: several unrelated businesses make the whole hard to understand and value."),
    "Forest Materials": _p("3 3 3 2 2 2 1 2 3 3 2 2 2 2 2", False, False, False,
                           "Paper and wood products: commodity-priced, cyclical, input (wood/pulp) cost driven."),
}

# Within innovation-driven sectors, patents are a competitive driver only where products are engineered or
# formulated; IT services, consumer staples and telecom operators compete on capability, brand and network.
PATENT_SECTORS = {"Healthcare", "Chemicals", "Capital Goods", "Automobile and Auto Components", "Consumer Durables"}

# NSE announcement industry tags (finer) -> profile sector. First match wins.
_KEYWORDS = [
    (r"software|computers|information tech|\bit\b", "Information Technology"),
    (r"bank|financ|nbfc|insurance|capital market|broking|asset management|housing finance", "Financial Services"),
    (r"pharma|hospital|healthcare|diagnostic|drug", "Healthcare"),
    (r"power|electricity", "Power"),
    (r"gas distribution|water|utilit", "Utilities"),
    (r"cement|ceramic|building material|tiles", "Construction Materials"),
    (r"steel|iron|metal|mining|aluminium|copper|zinc", "Metals & Mining"),
    (r"auto|tyre|tractor|two wheeler", "Automobile and Auto Components"),
    (r"chemical|fertili|pesticide|agrochem|paint", "Chemicals"),
    (r"realty|real estate|residential|commercial project", "Realty"),
    (r"construction|infrastructure|engineering - construction|epc", "Construction"),
    (r"textile|apparel|yarn|garment", "Textiles"),
    (r"telecom", "Telecommunication"),
    (r"media|entertainment|publishing|broadcast|film", "Media Entertainment & Publication"),
    (r"oil|gas|refiner|petro|lubricant|coal", "Oil Gas & Consumable Fuels"),
    (r"fmcg|food|beverage|personal care|tobacco|consumer food|tea|sugar|dairy", "Fast Moving Consumer Goods"),
    (r"consumer durable|electronic|appliance|jewel", "Consumer Durables"),
    (r"retail|hotel|restaurant|travel|tourism|e-commerce|leisure", "Consumer Services"),
    (r"paper|forest|wood", "Forest Materials"),
    (r"capital goods|electrical equipment|machinery|industrial|defence|cables|bearings", "Capital Goods"),
    (r"logistic|shipping|transport|port|courier|airline", "Services"),
    (r"diversified|conglomerate", "Diversified"),
]


def resolve_sector(raw: Optional[str]) -> Optional[str]:
    """Map an NSE industry string to a profile sector (exact Nifty name first, then keywords)."""
    if not raw:
        return None
    raw = raw.strip()
    if raw in PROFILES:
        return raw
    for pattern, sector in _KEYWORDS:
        if re.search(pattern, raw, re.I):
            return sector
    return None

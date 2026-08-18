# Region data — model your own market

Liberty Bee ships calibrated to Salem, Massachusetts. To run it on **your** market, you supply
a **Region bundle** — a small folder of your data — and the importer builds a fresh database
from it. This guide is the how-to; [`bundles/massachusetts/salem/`](bundles/massachusetts/salem/)
is a complete worked example.

## This tree

```
regiondata/
  README.md          ← this guide
  defaults.json      ← sourced US-national defaults (what you inherit when you omit a value)
  bundles/
    massachusetts/
      salem/         ← the reference bundle (MassGIS + ACS; the source of the published corpus)
      beverly/ danvers/ lynn/ marblehead/ peabody/ swampscott/
```

The `bundles/<state>/<town>/` nesting is **a convention, not a requirement** — the importer
takes any path (`--bundle <dir>`) and cares only that the folder contains `region.json`,
`buildings.csv`, `units.csv`. Organize your own data however you like.

### Provenance of the shipped bundles

Everything under `bundles/` is built from government/public data: **MassGIS**
`Massachusetts_Property_Tax_Parcels` (building shells, identified by the GIS parcel id —
never a street address) + **US Census ACS** (bedroom distributions, per-town renter income).
Snapshot: August 2026. MassGIS revises parcels continuously, so these are frozen snapshots —
the committed bundle, not a fresh pull, is what the published corpus was built from.

Honest caveats, per town:
- **Thin-sales ratio windows** — Marblehead (n=22, 2022), Danvers (n=20, 2023), Swampscott
  (n=21, 2023) use widened sale-year windows for their assessed-to-market ratios; a mild
  price bias is possible. Salem, Lynn, Beverly, and Peabody use robust 2024 windows.
- **Insurance** is a $1,400/unit Massachusetts small-building proxy on the six non-Salem towns.
- The **Salem** bundle is the source of the published corpus; the other towns ship as
  importable examples with sourced per-town economics.

> **One region per database.** A "region" is whatever market(s) you load. You *can* put
> multiple markets in one region — the engine has no geography, so it treats them as a single
> blended market: one income / tax / rent parameter set applies to the whole universe
> (per-town parameter overrides inside a bundle are not yet supported). To keep markets
> distinct, build one bundle per market and import each into its own database.

## What a bundle contains

```
my-region/
  region.json     ← settings + your region's parameters
  buildings.csv   ← one row per building you're modeling
  units.csv       ← one row per unit
```

### buildings.csv (required columns)
`property_id` (unique integer), `year_built` (integer, blank if unknown), `base_price`
(the building's value at simulation start, > 0). Optional: `address` (may be a street address,
a parcel id, or blank — a human label the engine never reads), `town`, `state`,
`property_style`, `total_units`.

### units.csv (required columns)
`property_id` (→ a building), `unit_number` (the unit's real label, **any format** — "Apt A1",
"Suite 2", "#3", "1"; unique within its building), `beds` (0 = studio), `adjusted_rent` (the
monthly market rent for the unit, > 0). Optional: `unit_id` (integer engine key — **generated
for you** when omitted), `baths`, `base_rent`.

The importer **rejects a bundle loudly** — with `file:line` — if a required field is blank or
mistyped, a rent/price is ≤ 0, or a unit points at a building that isn't there. Fix and re-run.

## region.json — your market's numbers

`region.json` carries metadata (name, locale, currency, start date) and a `parameters` map keyed
by the engine's own names (e.g. `OPEX.PropertyTaxPerUnit`). Parameters fall into three tiers:

- **You MUST supply these** (no honest generic exists): your local **median renter income**,
  **property tax per unit**, and **insurance per unit**. Property tax especially is
  municipality-specific — a national average would be meaningless.
- **You SHOULD supply these, but may omit them** (market-behavior values): vacancy rates,
  days-on-market, seasonality, inflation surge anchors. If you omit one, the importer fills a
  **generic US-national default and warns you** — the model still runs, but it's carrying a
  national placeholder, not your market. See *Generic defaults* below.
- **Everything else** ships with the engine (universal modeling assumptions — hold-period
  behavior, market-flow mechanics). You don't touch these.

## Sourcing your local values

| You need | A good source |
|---|---|
| building prices & rents | local listings / assessor records / a market data export |
| median renter income | US Census ACS table B25119 (renter-occupied) for your area |
| property tax per unit | your municipality's residential tax rate × assessed value |
| insurance per unit | a local small-building insurance quote |
| vacancy, seasonality, … | your metro's figures if you have them; else inherit the documented national defaults |

## Generic defaults (what you inherit if you omit a market-behavior value)

These are **honest US-national placeholders, not your market.** If your `region.json` omits one,
the importer applies the value below and **warns you by name**. Replace them with your metro's
figures when you can — start with the ones that most affect your market. Each carries its full
source, as-of window, and direction-of-effect in [`defaults.json`](defaults.json).

| Parameter | National default | Source (tier) | Which way it pushes |
|---|---|---|---|
| `PROP.VacancyRateBase` (your own vacancy) | 5% | RealPage managed-multifamily occupancy (reasoned-proxy) | higher → less rent collected |
| `RET.VacancyRefPct` (balanced-market vacancy) | 7% | US rental vacancy, Census/FRED RRVRUSQ156N (cited) | higher → more modeled scarcity → higher retention |
| `RET.MoverRegionalVacancyPct` (mover's metro vacancy) | 7% | = national reference (reasoned) | higher → weaker lock-in → more turnover |
| `MARKET.DaysOnMarket_Mean` | 56.1 days | FRED MEDDAYONMARUS, 2023–25 avg (cited) | higher → slower acquisition throughput |
| `INF.Surge_PropertyMean` (surge-regime property growth) | 12%/yr | Case-Shiller US, 2020–22 (cited) | higher → costlier surge-year acquisitions |
| `INF.Surge_OpExMean` (surge-regime OpEx growth) | 7%/yr | S&P insurance, 2020–25 (cited) | higher → faster margin erosion in surges |
| `MARKET.SeasonalMultiplier_*` (listing volume, 12 mo) | index, peaks Oct | FRED ACTLISCOUUS, 2023–25 (cited) | shifts *when* inventory appears |
| `MARKET.DaysOnMarket_SeasonalMultiplier_*` (12 mo) | index, slow winter | FRED MEDDAYONMARUS, 2023–25 (cited) | shifts *when* listings sit longer |
| `MARKET.PriceMultiplier_*` (12 mo) | index, peaks summer | FRED MEDLISPRIUS, 2023–25 (cited) | shifts *when* acquisitions cost more |

The 12-month seasonality indices were computed from the US-national FRED series over 2023–2025
(month-average ÷ overall-average — the same method as the shipped Salem calibration). Universal
model mechanics (hold periods, market-flow rates, clamps) are **not** region inputs and ship with
the engine unchanged.

## Running it

```
python region_importer.py --bundle regiondata/bundles/massachusetts/salem --label my-run
python app/src/simulation.py --env <the-created-db> --projection-id 200 --months 240 --seed 12345
```

Validate before importing with `--validate-only`. The importer provenance-stamps each ingest so
a database always knows which region it came from.

Bundles are also the **durable knob path**: copy a shipped bundle and add *any* registry
parameter to its `region.json` — not just the market set — to build a named variant of the
model under your own assumptions (REPRODUCE.md §6).

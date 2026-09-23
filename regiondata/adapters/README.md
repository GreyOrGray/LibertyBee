# Region adapters — how the non-Massachusetts universes were built

The Massachusetts bundles in `../bundles/massachusetts/` were built from MassGIS + ACS
public data (see `../README.md`). Tacoma and San Francisco went through the same
pipeline, but each required an **adapter**: a script that turns that jurisdiction's own
public-records format into the standard bundle shape (`region.json` + `buildings.csv` +
`units.csv`) the importer consumes.

These are the as-run scripts that produced the released bundles in
`../bundles/washington/tacoma/` and `../bundles/california/sanfrancisco/`. They are
published for provenance and as worked examples for anyone adapting their own city.

## What each adapter consumed (all public government data)

- **`tacoma/`** — Pierce County Assessor-Treasurer bulk data downloads
  (appraisal_account, improvement, improvement_builtas, improvement_detail, sale,
  tax_account, tax_description flat files; via the county's public GIS/data portal,
  subject to its published terms of use), filtered to the City of Tacoma boundary
  (`tacoma_boundary_tigerweb.json`, U.S. Census TIGERweb). `tacoma_ratio.py` and
  `tacoma_universe_count.py` are the validation companions (assessed-value ratio
  checks and universe-count reconciliation).
- **`sanfrancisco/`** — DataSF open data: the 2025 secured property tax roll
  (`datasf_rolls_2025.json` extract), filtered to small residential rental stock.

The raw source files are large (hundreds of MB) and are **not** in this repository —
each script's header documents exactly what to download and from where. Parcel
identifiers in the emitted bundles are anonymized locators (`PC_*`, `SF_*`), never
street addresses, matching the project's identifying-info rules.

## Regions evaluated and excluded

Santa Fe, Denton, and New Orleans were evaluated for the panel and **excluded on
composition validity**: the public data available for them was not sufficient to
populate a universe our validation gates would certify. Someone with MLS or other
non-public access could build their own bundle for those markets using the same
pipeline.

## The compensation-basis builders

`fw_build.py` and `fwrd_build.py` (repo root) are the scripts that created the
fair-wage (`fw`) and fair-wage-reduced-deal (`fwrd`) gold templates from the declared
baseline: they apply BLS OEWS May 2025 metro wages (each region's own metro; hire-in =
metro median, career cap = 75th percentile) to `reference.EmployeeRole`, and — for
`fwrd` — the leaner tenant deal. Every released corpus also carries its full parameter
record internally, so the dumps are self-describing without these scripts.

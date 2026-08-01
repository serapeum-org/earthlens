# EM-DAT disaster impacts — introduction

[EM-DAT](https://www.emdat.be/), the Emergency Events Database, is maintained by the Centre for Research on the
Epidemiology of Disasters (CRED) at UCLouvain. It is the reference record of disaster *impacts* — how many people
died, how many were affected, how much damage was done — for more than 26,000 events worldwide since 1900.

earthlens ships a single `emdat` backend covering the two routes that are actually programmatic. This page orients
it. For the hands-on walkthrough see [Usage](usage.md); the rendered API is the [Reference](emdat.md) page.

## The three EM-DAT products, and which one you get

CRED publishes three things. They are not interchangeable, and the differences matter more than usual here.

| Product | What it is | How earthlens reaches it |
|---|---|---|
| **EM-DAT Public Data** | The living database, updated weekly | **Not used** — the `public.emdat.be` portal is a manual XLSX download behind a login |
| **EM-DAT Archive** | A validated snapshot of the public table, re-cut roughly yearly | `variables=["emdat:events"]` — **this backend**, anonymously |
| **HDX country profiles** | Yearly per-country *summaries*, not events | The shipped `hdx` backend (see below) |

The Archive is the one to use. CRED's own documentation calls it *"the preferred choice for conducting research
with EM-DAT"*: it is on the [UCLouvain Dataverse](https://doi.org/10.14428/DVN/I0LTPH), it follows FAIR
principles, it has a DOI you can cite, and — unlike the portal — it needs no account. The trade-off is that it is
a snapshot: if you need last week's events, the portal is still the only route, and it is a manual one.

## Two sources, two shapes

`OUTPUT_KIND` is **per instance** here, resolved from the dataset id you pass in `variables`:

- **`emdat:events`** → `tabular`, a `pandas.DataFrame`. The impact record: one row per disaster, with deaths,
  affected, damage (nominal and CPI-adjusted), the country, and a coordinate pair.
- **`gdis:points`** / **`gdis:polygons`** → `vector`, a pyramids `FeatureCollection`. **GDIS** (Geocoded
  Disasters) is a separate, openly-licensed derivative that geocodes EM-DAT's *natural* disasters onto
  [GADM](https://gadm.org/) administrative units.

The two join on the disaster number: EM-DAT's `DisNo.` is `2009-0631-BGD`, GDIS's `disasterno` is `2009-0631` —
the same key with the ISO3 suffix dropped. The [notebook](../../examples/emdat/flood_events_and_footprints.ipynb)
does exactly that join.

## Know these four things before you use it

**1. The two sources cover different periods and different hazards.** The archive runs **1900 onwards** and
includes **technological** disasters (industrial and transport accidents) alongside natural ones. GDIS is frozen
at **1960–2018** and is **natural hazards only**. A GDIS query will never return a 2020 flood, and never returns
an industrial accident.

**2. `gdis:polygons` is a 2.2 GB download.** The GeoPackage of real admin-unit footprints is 2.2 GB compressed
and 6.3 GB on disk. `gdis:points` is the same 39,953 locations as centroids in **1.09 MB** — 2000× smaller — and
it is the only GDIS distribution carrying `year`, `latitude` and `longitude` outright. Start with the points and
reach for the polygons only when you genuinely need footprint geometry. The backend warns before it starts the
large download.

**3. The licences differ, and the archive's is restrictive.** GDIS is **CC-BY-4.0** — attribute it and you are
done. The EM-DAT archive is **CC-BY-NC-ND-4.0**, and the
[Terms of Use](https://doc.emdat.be/docs/legal/terms-of-use/) go further than the licence tag suggests: free use
is limited to **academic organisations, universities, non-profit research institutions, international public
organisations (UN agencies, multilateral banks, national governments) and media**, for research, teaching or
information purposes. Anything else is Commercial Use and needs a paid agreement with CRED. The terms also forbid
redistributing the database or building a derivative database from it. earthlens therefore fetches the archive
into *your* output directory and never caches or repacks it, and raises a `LicenseWarning` naming these
restrictions on every `emdat:events` download.

**4. Only GDIS needs credentials.** `emdat:events` is anonymous. The GDIS granules used to be served openly from
NASA SEDAC, but that host is gone and they now live in NASA Earthdata Cloud, so `gdis:*` needs an
[Earthdata Login](https://urs.earthdata.nasa.gov/users/new) — plus a one-time acceptance of the SEDAC data-use
agreement, without which the download returns a `401`. See [Usage](usage.md#authentication).

## What this backend deliberately does not do

- **Country summaries.** The EM-DAT country profiles on the Humanitarian Data Exchange are already reachable with
  the shipped `hdx` backend and are not re-implemented here:

    ```python
    from earthlens.core import EarthLens

    EarthLens("hdx", hdx_id="emdat-country-profiles", path="out").download()
    ```

- **The GraphQL API.** A GraphQL endpoint exists at `https://api.emdat.be/v1` and answers with
  `Missing API Key, please provide a value for the header: Authorization`. It is **not documented** anywhere on
  the current EM-DAT documentation site, and there is no published way for a new user to obtain a key, so
  earthlens does not build on it. If CRED documents it again, it would slot in as a third source.

- **The XLSX portal.** Superseded by the Archive for every programmatic purpose.

## Related derivatives

CRED's own [external resources](https://doc.emdat.be/docs/additional-resources-and-tutorials/external-resources/)
page lists two other geocoded derivatives that earthlens does not yet ship:

- **FLODIS** — flood disasters 2000–2018, joining EM-DAT fatalities and damage plus IDMC displacement to *satellite
  flood footprints* from the Global Flood Database
  ([Nature Sci Data 2023](https://www.nature.com/articles/s41597-023-02376-9)). Keyed on `disasterno`, so it joins
  straight onto what this backend returns.
- **Geo-Disasters** — Teber et al. (2025), geocoding climate-related EM-DAT events.

## Citation

Cite both sources when you use them together:

- EM-DAT, CRED / UCLouvain, Brussels, Belgium — <https://www.emdat.be>. Archive DOI `10.14428/DVN/I0LTPH`.
- Rosvold, E.L. & Buhaug, H. (2021). GDIS, a global dataset of geocoded disaster locations. *Scientific Data* 8,
  61. NASA SEDAC, DOI `10.7927/61jv-th84`.

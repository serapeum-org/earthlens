# IUCN Red List — introduction

The [IUCN Red List](https://www.iucnredlist.org/) is the world's authoritative
inventory of the conservation status of species — each assessment carries a
category (e.g. `EN`, `VU`, `CR`), the criteria behind it, a population trend,
threats, and a publication year. earthlens ships an `iucn` backend (alias
`redlist`) that fetches assessment records from the IUCN Red List **v4** API.

This page orients the backend. For the hands-on walkthrough see
[Usage](usage.md); the rendered API is the [Reference](iucn.md) page.

## A v4 REST shim

There is no mature Python client for the v4 API, so the backend talks to
`https://api.iucnredlist.org/api/v4` directly with `requests` (a core
dependency — no extra). The retired v3 API (`apiv3.iucnredlist.org`, with a
`?token=` query param) is gone; v4 authenticates with an
**`Authorization: Bearer <token>`** header.

## What it returns

`iucn` is the cluster's only `tabular` backend
(`IUCN.OUTPUT_KIND == "tabular"`): a query returns assessment **records**, not
geometry, so `download()` returns a `pandas.DataFrame` and the facade rejects
an `aggregate=` argument. Each entry in `variables` is a
`"species:<binomial>"` or a `"country:<ISO2>"` selector.

The species path is **two-step**: the binomial is split into genus + species,
looked up via `taxa/scientific_name?genus_name=&species_name=` (which returns
the assessment list), then the latest assessment is enriched from
`assessment/{id}` with its criteria and population trend. The country path
lists `countries/{code}` (ISO alpha-2). IUCN advises a ~2-second delay between
calls, which the backend honours.

## Authentication

v4 requires a token sent as a Bearer header. Resolve it from a `token=`
argument or the `IUCN_TOKEN` environment variable; a missing token raises
`AuthenticationError` naming `IUCN_TOKEN`. Sign up for a free token at
[api.iucnredlist.org/users/sign_up](https://api.iucnredlist.org/users/sign_up).

## Licensing

Red List data is **`CC-BY-NC`**, and redistribution requires a written IUCN
waiver. **Every** download raises a `LicenseWarning`, and Red List data is
**never** shipped as package data — the token stays user-supplied, so each
user fetches under their own agreement. See the
[IUCN Red List Terms of Use](https://www.iucnredlist.org/terms/terms-of-use).

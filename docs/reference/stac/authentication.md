# STAC backend — authentication

STAC **search** is anonymous on every supported endpoint; only **asset reads**
may need credentials. The signer for an endpoint is chosen from the catalog, so
you usually only supply credentials (when required) and pick the endpoint.

## Earth Search — anonymous

Nothing to configure. Earth Search assets are public COGs on AWS Open Data:

```python
EarthLens(data_source="earth-search",
          variables={"sentinel-2-l2a": ["red"]}, ...)
```

Requires only the `[stac]` extra (`pip install earthlens[stac]`).

## Microsoft Planetary Computer — SAS URL signing

No account needed. Install the `planetary-computer` SDK (part of the `[stac]`
extra) and the backend signs each asset URL with a short-lived Azure **SAS
token** in the query string (`MpcSasSigner`):

```python
EarthLens(data_source="planetary-computer",
          variables={"sentinel-2-l2a": ["B04", "B08"]}, ...)
```

The token rides in the URL, not in a header — this is deliberate, because GDAL
forwards bearer headers across the cross-host redirects MPC blob URLs use, which
would leak a bearer token.

## CDSE — S3 credentials

CDSE assets live on an S3-compatible store at `eodata.dataspace.copernicus.eu`
and need **S3 access-key/secret** credentials (`CdseS3Signer`). Generate keys in
the CDSE dashboard
(`https://eodata-s3keysmanager.dataspace.copernicus.eu`) and supply them by
env var or kwarg:

```bash
export CDSE_S3_ACCESS_KEY=...
export CDSE_S3_SECRET_KEY=...
```

```python
EarthLens(data_source="cdse",
          variables={"sentinel-1-grd": ["vv"]},
          access_key="...", secret_key="...",   # or rely on the env vars
          ...)
```

Resolution order is **kwarg → env var**. If neither supplies both halves, an
`AuthenticationError` is raised naming the dashboard URL. The credentials are
exposed to GDAL via the asset-read environment (`AWS_S3_ENDPOINT`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, …), never as an `Authorization`
header.

## Requester-pays collections — AWS credentials

A few collections store their assets on a **requester-pays** S3 bucket even
though their endpoint is otherwise anonymous — e.g. Earth Search's
`landsat-c2-l2` resolves to `s3://usgs-landsat`. The catalog marks these with a
per-collection `signer: aws-requester-pays`, so the backend reads them with the
requester-pays signer (sets `AWS_REQUEST_PAYER=requester` and rewrites the
`s3://` href to GDAL's `/vsis3/` path). **You must have valid AWS credentials**
in the environment (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, or an
instance role) — the requester (you) pays the egress. Without them the read
fails with an S3 `403`.

## CI secret pattern

Set the CDSE keys as repository secrets and export them in the job environment
before running the live STAC tests; the Earth Search and MPC e2e tests need no
secrets (MPC only needs the `planetary-computer` package installed).

## See also

* [Introduction](introduction.md) · [Usage](usage.md) ·
  [API reference](stac.md)

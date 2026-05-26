# EUMETSAT Data Store — Data Tailor (deferred)

**Data Tailor** is EUMETSAT's server-side customisation service
(`api.eumetsat.int/epcs/`) — the analog of NASA's Harmony. It subsets,
reprojects, and format-converts products **on the server** before
delivery, so it is the natural target for the `aggregate=` / bbox-crop /
reproject path.

It is **not part of the MVP.** Today the backend fetches whole native
products, and `download(aggregate=...)` raises `NotImplementedError`
naming this page:

```python
el.download(aggregate=some_config)
# NotImplementedError: EUMETSAT aggregate=/subset is the Data Tailor path
# (H4); it is not part of the MVP. Download native products without
# aggregate= and reduce NetCDF products client-side with pyramids.
```

## What it will look like

When the `datatailor.py` module lands, it will build a Data Tailor
`Chain` from the catalog row's `tailor_product_type`, submit a
customisation per product, poll it to completion, stream the output to
disk, and **delete the customisation** to free the account's Data Tailor
quota:

```python
import eumdac
chain = eumdac.tailor_models.Chain(
    product=collection.tailor_product_type,   # from the catalog row
    format="netcdf4",
    projection="geographic",
    roi={"NSWE": [north, south, west, east]},
)
cust = datatailor.new_customisation(product, chain)
# poll cust.status until "DONE"; stream cust.stream_output(...); cust.delete()
```

Every curated catalog row already carries a `tailor_product_type` so this
path can be wired in without a catalog change.

!!! warning "Data Tailor quota"
    Customisations must be `delete()`d after streaming, or the account's
    Data Tailor storage fills up and further requests fail.

## In the meantime

* **NetCDF products** (Sentinel-3 / -5P / -6, OSI SAF) are readable and
  croppable with `pyramids` today — download them whole, then read /
  reduce client-side.
* **Native SEVIRI / FCI** products need a satpy reader bridge in
  `pyramids` (a separate cross-repo follow-on) before they can be read
  client-side.

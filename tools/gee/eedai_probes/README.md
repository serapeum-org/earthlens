# EEDAI capability probes

Live probes against the GDAL Earth Engine drivers, written to answer specific questions about what the EEDAI
raster driver and the EEDA vector driver can do, before building on either. They are **diagnostics, not tests**:
each one talks to Earth Engine, prints a table, and leaves the conclusion to the reader. Nothing here runs in CI.

Every probe needs a service-account key:

```bash
export GEE_SERVICE_KEY=/path/to/service-account.json
python tools/gee/eedai_probes/<probe>.py
```

They import `pyramids` first, which activates the vendored `osgeo`, so no separate GDAL install is needed.
Measurements below were taken on GDAL 3.13.1, 2026-08-22.

## The gates, and what they found

| Probe | Question | Answer |
|-------|----------|--------|
| `gate_a2_block_size.py` | Is a `BLOCK_SIZE` above the pinned 256 still read-correct, and cheaper? | Identical pixels at 128-2048. Cost tracks round-trips: 1024 read a 1024 px window ~9x faster than 256. 2048 is slower again - past the window size you pay for pixels you discard. |
| `gate_a3_pixel_encoding.py` | Which `PIXEL_ENCODING` is fastest on float and multi-band assets? | All agree on pixels. `AUTO` is already within noise of the best; `GEO_TIFF` degrades 3.5x at seven bands. `PNG`/`JPEG` are refused unless every band is Byte, and `Int8` does not qualify. |
| `gate_a4_subdatasets.py` | How does EEDAI expose an asset whose bands differ in resolution? | Sentinel-2 opens as three resolution-grouped subdatasets. A plain open returns 6 of 24 bands. **A mixed-resolution band request returns one band with only a warning** - a silent drop. |
| `gate_a6_corruption_hunt.py` | Can the intermittent overview corruption be caught with the transport logged? | Not reproduced in 75 instrumented reads. Kept so the next sighting can be captured rather than recalled. |

## The A1 sequence - why there are eight of them

`probe_a1_chain.py` holds the eight passes as one function apiece, in order,
because each refuted the hypothesis the previous one raised and the refutations
*are* the result. Run the whole chain, or one pass:

```bash
python tools/gee/eedai_probes/probe_a1_chain.py        # every pass
python tools/gee/eedai_probes/probe_a1_chain.py v4     # one pass
```

| Pass | Hypothesis under test | Outcome |
|------|----------------------|---------|
| `v1` | Are overviews corrupt at all? | Inconclusive - the window centred on the asset grid, which for a global DEM is ocean nodata, so every correlation was degenerate. |
| `v2` | Same, over land with the fill masked | Levels 0-2 returned impossible elevations; levels 3-7 matched a native downsample exactly. |
| `v3` | Multi-block reads break on overviews, as they do natively | **Refuted** - block-by-block reads were equally corrupt. |
| `v4` | `PIXEL_ENCODING=AUTO` mangles Int16 through a byte-only codec | **Refuted** - `AUTO`, `NPY` and `GEO_TIFF` were all exact. |
| `v5` | A prior native read poisons the handle's overview state | **Refuted** - cold and warm handles agreed. |
| `v6` | Is the corruption intermittent? | A probe defect: the native leg built its `Dataset` inside a lambda, so GDAL collected it before the band was read. Kept as a warning, not a result. |
| `v7` | Same, with the lifetime defect fixed | Native stable; overviews unaffected by any preceding read. |
| `v8` | Does sustained load provoke it? | **Refuted** - repeated reads stayed clean. |

`probe_a5_native_soak.py` and `probe_a5_endtoend.py` then asked the question that
mattered for production - whether the same fault reaches the native path earthlens
ships - across several assets, regions and window sizes, and through
`from_earthengine` itself. 96 reads, zero anomalies.

## Shared mechanics

`_common.py` holds what every probe needs and none of them is testing: auth, an
asset opened the way pyramids-eo opens it, a block-by-block window read, and the
oracle. Each probe is then only the question it asks. Anything a probe *is*
testing stays in that probe - which is why the A1 chain keeps its own window
arithmetic and read calls.

## Writing another one

Three lessons are baked into the later probes and are worth keeping:

1. **Pick the window deliberately.** Centring on the asset grid finds ocean. A degenerate ground truth makes every
   correlation meaningless while looking like a clean run.
2. **Mask the fill, but do not expect the driver to name it.** EEDAI reports `GetNoDataValue() == None` and a
   `GMF_ALL_VALID` mask even for bands that plainly have a sentinel, so the value has to come from the Earth Engine
   catalog. Judging on physical bounds alone counts an entire legitimate fill as corruption.
3. **Check for degeneracy as well as bounds.** A constant raster passes every bounds test ever written; an
   all-zero elevation grid was scored healthy once before this was added.

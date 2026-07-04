# Cloud region affinity (`earthlens.base.region`)

`region_affinity(bucket_region)` answers one question: **is this process running in the same cloud region as the
target bucket?** A backend uses the answer to choose between a cheap **in-region** streaming read and a slow,
billed **cross-region egress** download.

It returns one of three verdicts:

| Verdict | Meaning |
|---------|---------|
| `"in-region"` | the caller region equals `bucket_region` — stream directly |
| `"egress"` | caller and bucket are in different (known) regions — expect billed, slower transfer |
| `"unknown"` | the caller region could not be determined — fall back to the safe (HTTPS) path |

## How the caller region is resolved

Resolution is **env-first**, then an optional metadata probe:

1. an explicit `caller_region=` argument, then
2. `AWS_REGION` / `AWS_DEFAULT_REGION`, then
3. (only when `probe=True`) an instance-metadata probe — **AWS** IMDSv2, **GCP**
   `metadata.google.internal`, or **Azure** IMDS — each behind a hard 1 s timeout and cached per process.

Detection **never raises and never blocks**: any failure yields `"unknown"`. Pass `probe=False` to skip all
network access entirely (tests, air-gapped runs). The earthdata backend deliberately uses `probe=False` so its
in-region decision never risks hanging an off-cloud caller.

## Warning before large egress

`warn_if_egress(bucket_region, *, size_bytes, threshold_bytes=1<<30)` emits a single `loguru` warning when the
hint is `"egress"` and the transfer exceeds the threshold (default 1 GiB), and returns the hint. The `s3`
backend calls it before a bulk pull (using the object sizes the listing already reported), so a cross-region
transfer is never a silent cost surprise.

```python
from earthlens.base.region import region_affinity, warn_if_egress

if region_affinity("us-west-2") == "in-region":
    ...  # stream from S3 in-region
else:
    warn_if_egress("us-west-2", size_bytes=total_bytes)  # advise, then HTTPS download
```

## API

::: earthlens.base.region.region_affinity

::: earthlens.base.region.warn_if_egress

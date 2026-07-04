# Sensor.Community — licence & access

Sensor.Community is **public and keyless** — there are no credentials,
no API key, and no login. Both the live JSON API
(`data.sensor.community`) and the archive (`archive.sensor.community`)
are served anonymously over HTTPS, and the backend uses only `requests`
+ `pandas` (core earthlens dependencies), so there is **no extra to
install**.

## Licence — ODbL

Sensor.Community data is licensed under the **Open Database License
(ODbL)**: you may use, share, and adapt it, but you must **attribute**
the source and keep derived databases under a **share-alike** licence.
The backend emits a `LicenseWarning` on every `download()` so the
obligation rides along with the data rather than being discovered
silently:

```python
import warnings
from earthlens.sensor_community import LicenseWarning

with warnings.catch_warnings():
    warnings.simplefilter("ignore", LicenseWarning)   # if you have acknowledged it
    df = EarthLens(data_source="sensor-community", ...).download()
```

## Data-quality caveat

The readings come from **low-cost, citizen-operated sensors**. They are
excellent for dense spatial coverage and relative trends but are **not
reference-grade** — do not treat them as regulatory measurements. For
reference-grade observations use `earthlens.airnow`,
`earthlens.eea_aq`, or `earthlens.openaq`.

The gated live e2e test (`tests/sensor_community/test_sensor_community_e2e.py`)
is marked `sensor_community` and fetches one recent day over a tight,
sensor-dense bbox.

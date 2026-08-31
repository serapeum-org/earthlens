# Core functions

The module-level surface of `earthlens.core` — one-shot helpers that wrap the
[`EarthLens`](earthlens.md) facade, plus the discovery functions for finding a dataset across all 61 providers.

```python
from earthlens.core import download, find, search, sources
```

For a task-oriented walkthrough of `find` / `search` / `sources`, see [Discovering datasets](../discovery.md).

## `download`

The one-shot equivalent of constructing an `EarthLens` and calling `.download()`. Takes the same arguments as the
facade constructor and returns the backend's result directly.

::: earthlens.core.download

## `sources`

::: earthlens.core.sources

## `find`

::: earthlens.core.find

## `search`

::: earthlens.core.search

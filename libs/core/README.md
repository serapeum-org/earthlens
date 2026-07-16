# earthlens-core

Core of [earthlens](https://github.com/serapeum-org/earthlens): the `EarthLens` facade, the
`AbstractDataSource` / `AbstractCatalog` abstractions every backend subclasses, the shared transport and
spatial helpers, and the `earthlens` command-line interface.

This distribution carries **no provider SDK**. Backends ship in the thematic provider distributions
(`earthlens-atmosphere`, `earthlens-ocean`, `earthlens-imagery`, `earthlens-land`, `earthlens-hazards`),
each registering its data-source keys with the facade through the `earthlens.backends` entry-point group.

Installing `earthlens` pulls in this package plus the provider distributions.

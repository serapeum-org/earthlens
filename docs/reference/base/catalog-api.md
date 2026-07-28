# Catalog & utility API

The shared plumbing every provider backend builds on: catalog loading, the strict YAML parser, the provider
registry, and the small filesystem helpers. See [Base contracts](contracts.md) for the rules these implement.

## Catalog loading

All 48 catalog loaders route through `load_catalog`, which owns the catalog glob, the `(path, mtime_ns)` cache
key, and the cache registry.

::: earthlens.base.catalog_source.load_catalog

## Strict YAML

The duplicate-key-rejecting loader every catalog parses through — a mapping that declares the same key twice
raises `ValueError` rather than silently keeping the last value.

::: earthlens.base.yaml_loader.load_yaml_strict

## Provider registry

Backends that populate the base `providers` field load it from a per-backend `providers.yaml`.

::: earthlens.base.providers.Provider

::: earthlens.base.providers.load_providers

## Filesystem helpers

::: earthlens.base.naming.safe_filename

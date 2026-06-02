"""AWS Open Data backend for earthlens (`earthlens.s3`).

Registry-driven multi-dataset reader over public AWS S3 buckets (ERA5 on
`nsf-ncar-era5`, and other AWS Open Data collections). The :class:`S3`
backend resolves a dataset key to its bucket layout and streams the
matching objects to disk; :class:`Catalog` maps dataset keys to their
per-dataset metadata, and :class:`S3Auth` / :class:`S3Credentials`
handle the (unsigned, public) access.
"""

from __future__ import annotations

from earthlens.s3.auth import S3Auth, S3Credentials
from earthlens.s3.catalog import Catalog, Dataset, Variable
from earthlens.s3.backend import S3

__all__ = ["S3", "Catalog", "Dataset", "Variable", "S3Auth", "S3Credentials"]

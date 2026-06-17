"""Custom warning classes raised by the NWP backend.

`RetentionWarning` is emitted at construction time when the requested
`start` precedes the cycle a model still keeps online — the most common
silent failure for short-retention providers (DWD keeps roughly one day,
Météo-France fourteen) where the download otherwise just returns nothing.
Exported from `earthlens.nwp` so callers can filter or escalate it via
the standard `warnings` machinery.
"""

from __future__ import annotations


class RetentionWarning(UserWarning):
    """A request asks for a cycle the provider has already rolled off.

    Emitted by `NWP.__init__` when the resolved model row carries a
    `retention_days` and the request `start` is older than
    `now - retention_days`. Subclasses `UserWarning` so it surfaces by
    default and can be promoted to an error with `warnings.simplefilter
    ('error', RetentionWarning)`.
    """

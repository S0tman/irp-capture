"""IRP integrity — deterministic snapshots and offline verification (PR2a).

MIT-licensed core. The format, canonicalisation, snapshot generation and the
verifier live here and stay permissively licensed: the verifier is neutral
evidence tooling that an outside party must be able to run without trusting or
buying anything. Advanced/managed anchoring tooling (PR2b+) lives in a separate,
source-available (BSL) module and must never be imported from here.
"""
from __future__ import annotations

SCHEMA_VERSION = "irp-integrity-snapshot/0.1"

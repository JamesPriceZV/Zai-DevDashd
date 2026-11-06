"""Safe projection of the Coordinator's frozen COMMON/B routing map.

Routing records are neither feature counts nor live acceptance decisions.
"""
from __future__ import annotations

import hashlib
import re
from zai_devdashd.executive_live import read_document, _clock, _text
from zai_devdashd import config as site

DIGEST = re.compile(r"^[0-9a-f]{64}$")


def read(path, expected_sha256, now, *, repository=None):
    repository = repository if repository is not None else (site.CURRENT.get("github") or {}).get("repo", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        return {"rows": [], "error": "Release repository is not configured"}
    if not path or not isinstance(expected_sha256, str) or not DIGEST.fullmatch(expected_sha256):
        return {"rows": [], "error": "Approved release workstreams are not connected"}
    try:
        raw, document = read_document(path, limit=131072)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_sha256 or document.get("kind") != "canonical-common-b-scope-mapping" or document.get("version") != 1:
            raise ValueError("Scope map binding changed")
        observed = _clock(document.get("prepared_at"), now)
        rows = document.get("rows")
        if observed is None or not isinstance(rows, list) or len(rows) > 64:
            raise ValueError("Scope map structure is invalid")
        safe, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or type(row.get("pr")) is not int or not 1 <= row["pr"] <= 1000000 or row["pr"] in seen:
                raise ValueError("Scope map identity is invalid")
            seen.add(row["pr"])
            safe.append({"pr": row["pr"], "url": "https://github.com/" + repository + "/pull/" + str(row["pr"]),
                         "title": _text(row.get("title"), 200), "benefit": _text(row.get("user_benefit"), 400),
                         "bucket": _text(row.get("bucket"), 20), "kind": _text(row.get("row_kind"), 40),
                         "feature_group_id": _text(row.get("feature_group_id"), 100),
                         "recorded_disposition": _text(row.get("disposition_raw"), 400),
                         "live_acceptance_verified": False})
        return {"rows": safe, "observed_at": observed, "sha256": actual, "error": None,
                "scope_complete": False, "feature_count": None,
                "basis": "Coordinator mapping of COMMON and Build B routing records; descriptive scope, not a feature denominator or live acceptance"}
    except (OSError, ValueError, TypeError, OverflowError):
        return {"rows": [], "error": "Approved workstream mapping could not be read or its frozen binding changed"}

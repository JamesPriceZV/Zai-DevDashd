"""Frozen scope binding tests without reading a journal or GitHub."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zai_devdashd import release_scope


class ScopeTests(unittest.TestCase):
    def test_mapping_projects_benefits_without_inventing_feature_totals_or_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "scope.json"
            p.write_text(json.dumps({"kind": "canonical-common-b-scope-mapping", "version": 1,
                "prepared_at": "2026-11-06T13:00:00-05:00", "rows": [{"pr": 42,
                "title": "Research", "user_benefit": "Check sources", "evidence": ["PRIVATE JOURNAL"],
                "url": "https://evil.example", "disposition_raw": "Awaiting review"}]}))
            p.chmod(0o600); digest = hashlib.sha256(p.read_bytes()).hexdigest()
            value = release_scope.read(p, digest, 1793989000, repository="example/project")
            self.assertIsNone(value["feature_count"]); self.assertFalse(value["scope_complete"])
            self.assertFalse(value["rows"][0]["live_acceptance_verified"])
            self.assertNotIn("PRIVATE JOURNAL", json.dumps(value))
            self.assertNotIn("evil.example", json.dumps(value))
            self.assertTrue(release_scope.read(p, "a" * 64, 1793989000, repository="example/project")["error"])
            p.chmod(0o644)
            self.assertTrue(release_scope.read(p, digest, 1793989000, repository="example/project")["error"])


if __name__ == "__main__": unittest.main()

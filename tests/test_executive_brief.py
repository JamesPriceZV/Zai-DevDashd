"""Pure interpretation/cache tests; the injected runner never invokes a model."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zai_devdashd import executive_brief as brief


def owner():
    return {"input_sha256": "a" * 64, "observed_at": 100, "stale": False,
            "features": [{"id": "research", "title": "Research", "benefit": "Check cited answers", "state": "in_progress"}],
            "operations": [{"id": "auto", "state": "blocked", "controller": {"new_valid": 0}}],
            "release_checks": [], "blockers": [], "raw_log": "SECRET"}


def reply():
    item = {"text": "Research work is in development.", "evidence_ids": ["feature:research"]}
    return {"summary": deepcopy(item), "next_action": deepcopy(item),
            "value_propositions": [dict(item, feature_id="research")],
            "swot": {key: [deepcopy(item)] for key in ("strengths", "weaknesses", "opportunities", "threats")}}


class BriefTests(unittest.TestCase):
    def inert_transport(self, output, model="system"):
        """Run the real pipe reader with supplied child lifecycle; no subprocess."""
        observed = []
        class InertGuard:
            def check(self, pid): pass
            def receipt(self): return {"kind": "TEST_INJECTED_RESOURCE_OBSERVER"}
        def client(command, **kwargs):
            import os
            read_fd, write_fd = os.pipe()
            os.write(write_fd, output.encode("utf-8")); os.close(write_fd)
            error_fd, error_write = os.pipe(); os.close(error_write)
            class InertChild:
                pid = 424242
                returncode = 0
                stdout = os.fdopen(read_fd, "rb")
                stderr = os.fdopen(error_fd, "rb")
                def poll(self): return 0
                def wait(self, timeout=None): return 0
                def terminate(self): raise AssertionError("completed fake child was signalled")
                def kill(self): raise AssertionError("completed fake child was signalled")
            child = InertChild()
            observed.append((command, kwargs["stdin"], kwargs["env"], child.pid))
            return child
        with tempfile.TemporaryDirectory() as folder:
            facts = brief.packet(owner(), [])
            with patch.object(brief, "Guard", InertGuard), patch.object(brief.subprocess, "Popen", side_effect=client):
                result = brief.run_model(facts, model, folder)
        return result, observed, facts

    def test_documented_guided_text_transport_keeps_closed_stdin_and_restricted_environment(self):
        result, observed, facts = self.inert_transport(json.dumps(reply()))
        command, stdin, environment, child_pid = observed[0]
        self.assertEqual(command[:5], ["/usr/bin/fm", "respond", "-m", "system", "-g"])
        self.assertEqual(Path(command[command.index("--schema") + 1]), brief.SCHEMA_PATH)
        self.assertTrue(brief.SCHEMA_PATH.is_file())
        self.assertEqual(command[command.index("-i") + 1], brief.INSTRUCTIONS)
        self.assertIn("--no-stream", command)
        self.assertEqual(json.loads(command[command.index("--text") + 1]), facts)
        self.assertEqual(stdin, subprocess.DEVNULL)
        self.assertEqual(set(environment), {"HOME", "PATH", "LANG", "TMPDIR"})
        self.assertEqual(result.reply, reply())
        self.assertEqual(result.execution["owned_client_pid"], child_pid)
        self.assertTrue(result.execution["owned_client_closed"])

    def test_guided_pcc_uses_the_same_schema_and_records_requested_provider(self):
        result, observed, _facts = self.inert_transport(json.dumps(reply()), model="pcc")
        command = observed[0][0]
        self.assertEqual(command[command.index("-m") + 1], "pcc")
        self.assertEqual(Path(command[command.index("--schema") + 1]), brief.SCHEMA_PATH)
        self.assertEqual(result.execution["requested_provider"], "pcc")

    def test_guidance_schema_requires_the_existing_nested_claim_shape(self):
        schema = json.loads(brief.SCHEMA_PATH.read_text())
        def resolve(node):
            return schema["$defs"][node["$ref"].split("/")[-1]] if "$ref" in node else node
        def closed_object(node, required):
            node = resolve(node)
            self.assertEqual(node["type"], "object")
            self.assertIs(node["additionalProperties"], False)
            self.assertEqual(set(node["required"]), set(required))
            self.assertEqual(set(node["properties"]), set(required))
            return node["properties"]
        def claim(node, feature=False):
            fields = closed_object(node, ["text", "evidence_ids"] + (["feature_id"] if feature else []))
            self.assertEqual(fields["text"]["type"], "string")
            ids = fields["evidence_ids"]
            self.assertEqual((ids["type"], ids["items"]["type"], ids["minItems"], ids["maxItems"]),
                             ("array", "string", 1, 8))
            if feature: self.assertEqual(fields["feature_id"]["type"], "string")
        root = closed_object(schema, ["summary", "next_action", "value_propositions", "swot"])
        claim(root["summary"]); claim(root["next_action"])
        claims = [root["value_propositions"]]
        claim(claims[0]["items"], feature=True)
        swot = closed_object(root["swot"], ["strengths", "weaknesses", "opportunities", "threats"])
        for items in swot.values():
            claim(items["items"]); claims.append(items)
        for items in claims:
            self.assertEqual((items["type"], items["minItems"], items["maxItems"]), ("array", 0, 2))

    def test_missing_guidance_resource_never_launches_an_unguided_client(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(brief, "SCHEMA_PATH", Path(folder) / "missing.json"), \
                    patch.object(brief, "Guard", side_effect=AssertionError("no resource observer")), \
                    patch.object(brief.subprocess, "Popen", side_effect=AssertionError("no client")):
                with self.assertRaisesRegex(ValueError, "schema is unavailable"):
                    brief.run_model(brief.packet(owner(), []), "system", folder)

    def test_two_adjacent_json_objects_are_not_salvaged(self):
        with self.assertRaises(json.JSONDecodeError):
            self.inert_transport(json.dumps(reply()) + json.dumps(reply()))

    def test_incomplete_swot_or_bare_strings_still_fail_strict_validation(self):
        facts = brief.packet(owner(), [])
        for mutate in (lambda value: value["swot"].pop("threats"),
                       lambda value: value["swot"].update(strengths=["Uncited synthetic string"]),
                       lambda value: value["summary"].update(evidence_ids=[]),
                       lambda value: value["summary"].update(text="x" * 301)):
            value = reply(); mutate(value)
            with self.assertRaises(ValueError): brief.validate(value, facts)

    def test_old_freeform_cache_is_preserved_but_not_reused_as_guided_content(self):
        old_facts = brief.packet(owner(), []); old_facts["version"] = "executive-brief-v1"
        old = {"version": "executive-brief-v1", "model": "system",
               "input_sha256": brief.input_hash(old_facts), "content": reply()}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "brief.json"
            path.write_text(json.dumps(old)); path.chmod(0o600)
            before = path.read_bytes()
            cache = brief.Cache(path, enabled=False, model="system")
            result = cache.get(owner(), 100)
            self.assertEqual(result["state"], "not_started")
            self.assertIsNone(result["content"])
            self.assertEqual(path.read_bytes(), before)

    def test_compact_packet_retains_controller_before_other_operation_rows(self):
        source = owner()
        source["features"] = [{"id": "f" + str(i), "title": "T" * 180, "benefit": "B" * 400} for i in range(24)]
        source["operations"] = [{"id": "step" + str(i), "title": "T" * 180, "next_action": "N" * 180} for i in range(24)] + source["operations"]
        result = brief.packet(source, [])
        self.assertLessEqual(len(json.dumps(result)), 6000)
        self.assertEqual(result["operations"][0]["id"], "auto")
        self.assertIn("not a complete", result["scope"])

    def test_long_other_categories_cannot_erase_controller_or_counter_changes(self):
        source = owner()
        source["blockers"] = [dict(id="long", **{key: "é" * 400 for key in ("title", "owner", "impact", "next_action")})]
        first = brief.packet(source, [])
        self.assertLessEqual(len(json.dumps(first)), 6000)
        self.assertEqual(first["operations"][0]["id"], "auto")
        source["operations"][0]["controller"]["new_valid"] = 1
        self.assertNotEqual(brief.input_hash(first), brief.input_hash(brief.packet(source, [])))

    def test_oversized_essential_controller_refuses_instead_of_silently_omitting(self):
        source = owner()
        source["operations"][0]["controller"]["benchmark_acceptance"] = "é" * 2000
        with self.assertRaises(ValueError): brief.packet(source, [])

    def test_worker_start_failure_releases_admission_and_can_retry(self):
        cache = brief.Cache("unused", enabled=True)
        with patch.object(brief.threading.Thread, "start", side_effect=RuntimeError("thread creation refused")):
            result = cache.get(owner(), 100)
        self.assertEqual(result["state"], "unavailable")
        self.assertFalse(cache.running)
        self.assertEqual(cache.attempted, 100)
        with patch.object(brief.threading.Thread, "start", side_effect=RuntimeError("refused again")) as start:
            cache.get(owner(), 101)
            start.assert_not_called()
            cache.get(owner(), 161)
            start.assert_called_once()

    def test_packet_ignores_reads_but_tracks_actual_controller_changes(self):
        first = owner(); second = deepcopy(first)
        second.update(observed_at=200, attempted_at=300, input_sha256="b" * 64)
        second["operations"][0]["controller"]["checked_at"] = 400
        a, b = brief.packet(first, []), brief.packet(second, [])
        self.assertEqual(brief.input_hash(a), brief.input_hash(b))
        self.assertNotIn("SECRET", json.dumps(a))
        second["operations"][0]["controller"]["new_valid"] = 1
        self.assertNotEqual(brief.input_hash(a), brief.input_hash(brief.packet(second, [])))

    def test_retained_controller_history_keeps_semantic_qualification_in_packet_and_hash(self):
        source = owner()
        source["operations"][0].update(state="queued", controller_applied=False,
            controller={"state": "running", "stale": True, "checked_at": 1, "publication_age_s": 5000})
        facts = brief.packet(source, [])
        controller = facts["operations"][0]["controller"]
        self.assertTrue(controller["stale"])
        self.assertFalse(controller["applied_to_operation"])
        self.assertIn("Historical", controller["authority"])
        source["operations"][0]["controller"].update(checked_at=100, publication_age_s=6000)
        self.assertEqual(brief.input_hash(facts), brief.input_hash(brief.packet(source, [])))
        source["operations"][0].update(controller_applied=True)
        source["operations"][0]["controller"]["stale"] = False
        self.assertNotEqual(brief.input_hash(facts), brief.input_hash(brief.packet(source, [])))

    def test_credentials_are_redacted_from_allowlisted_text(self):
        source = owner(); source["features"][0]["benefit"] = "api_key=secret-value sk-abcdefghijklmnop"
        value = json.dumps(brief.packet(source, []))
        self.assertNotIn("secret-value", value); self.assertNotIn("sk-abcdefghijklmnop", value)

    def test_unknown_citation_feature_and_unsupported_claim_fail_closed(self):
        facts = brief.packet(owner(), [])
        for mutate in (lambda r: r["summary"].update(evidence_ids=["market:invented"]),
                       lambda r: r["value_propositions"][0].update(feature_id="invented"),
                       lambda r: r["summary"].update(text="All tests pass.")):
            value = reply(); mutate(value)
            with self.assertRaises(ValueError): brief.validate(value, facts)
        self.assertEqual(brief.validate(reply(), facts)["summary"]["evidence_ids"], ["feature:research"])

    def test_paid_provider_is_rejected_before_execution(self):
        with self.assertRaises(ValueError): brief.Cache("unused", model="openai")
        with self.assertRaises(ValueError): brief.run_model({}, "openai", ".")

    def test_one_background_run_is_cached_with_actual_provider_and_private_custody(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def runner(facts, model, folder):
            calls.append(model); entered.set(); release.wait(2)
            return brief.ModelResponse(reply(), {"kind": "TEST_INJECTED_RUNNER"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cache" / "brief.json"
            cache = brief.Cache(path, enabled=True, model="system", runner=runner)
            self.assertEqual(cache.get(owner(), 100)["state"], "updating")
            self.assertTrue(entered.wait(1)); cache.get(owner(), 200)
            self.assertEqual(calls, ["system"]); release.set()
            for _ in range(100):
                if not cache.running: break
                time.sleep(.01)
            self.assertFalse(cache.running)
            self.assertEqual(cache.get(owner(), 300)["state"], "available")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            restored = brief.Cache(path, enabled=False, model="pcc")
            result = restored.get(dict(owner(), stale=True), 400)
            self.assertEqual(result["state"], "last_known")
            self.assertEqual(result["model"], "system")
            self.assertTrue(result["source_stale"]); self.assertIsNotNone(result["content"])
            self.assertEqual(calls, ["system"])

    def test_missing_or_invalid_owner_never_starts_inference(self):
        calls = []
        cache = brief.Cache("unused", enabled=True, runner=lambda *args: calls.append(args))
        self.assertEqual(cache.get({}, 100)["state"], "source_unavailable")
        self.assertEqual(cache.get(dict(owner(), error="read failed"), 200)["state"], "source_unavailable")
        self.assertEqual(calls, [])


if __name__ == "__main__": unittest.main()

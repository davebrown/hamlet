import json
import os
from pathlib import Path
import unittest
import uuid

from hamlet.grouping import group_corpus
from hamlet.normalized import algorithm_identity
from hamlet.splitting import build_split, sample_test
from hamlet.store import Store


class SplittingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f".split-test-{uuid.uuid4().hex}"
        self.store = Store(self.root)
        self.uid = 0

    def tearDown(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.root)

    def add(self, name, *, account="a", folder="INBOX", date="2026-01-01", raw=None, references=""):
        self.uid += 1
        if raw is None:
            raw = (f"Message-ID: <{name}@example.invalid>\r\n"
                   f"References: {references}\r\n\r\nprivate body {name}").encode()
        return self.store.add(account, folder, "7", self.uid, date, raw)

    def manifest(self, report):
        return json.loads(Path(report["manifest_path"]).read_text())

    def test_components_protect_groups_and_cross_account_effective_duplicates(self):
        first = self.add("first", references="<ancestor@example.invalid>")
        second = self.add("second", date="2026-06-01", references="<ancestor@example.invalid>")
        raw = self.store.message_path(second).read_bytes().replace(b"\r\n", b"\n")
        duplicate = self.add("duplicate", account="b", raw=raw, date="2026-06-02")
        fourth = self.add("fourth", account="b", date="2026-06-03",
                          references="<ancestor@example.invalid>")
        for index in range(8):
            self.add(f"other-{index}", date=f"2026-01-{index + 2:02}")
        grouped = group_corpus(self.store)
        report = build_split(self.store)
        records = self.manifest(report)["records"]
        related = [record for record in records if record["sha256"] in (first, second, duplicate, fourth)]
        self.assertEqual(len({record["group_id"] for record in related}), 1)
        self.assertEqual(len({record["partition"] for record in related}), 1)
        self.assertEqual(related[0]["partition"], "test")
        membership = {}
        identities = {}
        for record in records:
            for group_id in record["group_ids"]:
                self.assertEqual(membership.setdefault(group_id, record["partition"]), record["partition"])
            self.assertEqual(identities.setdefault(record["content_identity"], record["partition"]),
                             record["partition"])
        self.assertEqual(report["parameters"]["grouping_run_id"], grouped["run_id"])

    def test_equal_or_missing_dates_have_deterministic_component_ties(self):
        for index in range(10):
            self.add(f"tie-{index}", date="unknown" if index < 5 else "2026-01-01")
        group_corpus(self.store)
        first = build_split(self.store)
        records = self.manifest(first)["records"]
        tested = [record for record in records if record["partition"] == "test"]
        self.assertEqual(len(tested), 2)
        self.assertTrue(all(record["component_latest_internal_date"] for record in tested))
        self.assertEqual(first, build_split(self.store))

    def test_reproducible_private_body_free_and_chronological(self):
        for index in range(10):
            self.add(f"mail-{index}", date=f"2026-02-{index + 1:02}")
        group_corpus(self.store)
        first = build_split(self.store)
        content = Path(first["manifest_path"]).read_bytes()
        second = build_split(self.store)
        self.assertEqual(first, second)
        self.assertEqual(content, Path(second["manifest_path"]).read_bytes())
        self.assertNotIn(b"private body", content)
        self.assertEqual(os.stat(first["manifest_path"]).st_mode & 0o777, 0o600)
        self.assertEqual(first["counts"]["test"]["records"], 2)
        records = self.manifest(first)["records"]
        self.assertEqual({record["sources"][0]["internal_date"] for record in records
                          if record["partition"] == "test"}, {"2026-02-09", "2026-02-10"})
        self.assertTrue(all(record["label_provisional"] for record in records))

    def test_latest_review_unsafe_unknown_and_global_label_conflicts_excluded(self):
        reviewed = self.add("reviewed", folder="Junk")
        self.store.review("a", reviewed, "spam")
        self.store.review("a", reviewed, "ham")
        conflict = self.add("conflict")
        self.add("other-account", account="b", folder="Junk",
                 raw=self.store.message_path(conflict).read_bytes())
        unsafe = self.add("unsafe")
        unknown = self.add("unknown", folder="archive")
        group_corpus(self.store)
        with self.store.db:
            self.store.db.execute("""
                UPDATE normalized_messages SET status='ambiguous'
                WHERE source_sha256=? AND algorithm=?
            """, (unsafe, algorithm_identity()))
        # Unsafe records were previously groupable; split must re-check their view.
        report = build_split(self.store)
        manifest = self.manifest(report)
        self.assertEqual(len(manifest["records"]), 1)
        record = manifest["records"][0]
        self.assertEqual(record["sha256"], reviewed)
        self.assertEqual(record["label"], "ham")
        self.assertEqual(record["label_status"], "reviewed")
        excluded = {item["sha256"]: item for item in manifest["excluded_records"]}
        self.assertIn("unsafe_normalization", excluded[unsafe]["exclusion_reasons"])
        self.assertIn("unresolved_label", excluded[unknown]["exclusion_reasons"])
        self.assertIn("effective_content_label_conflict", excluded[conflict]["exclusion_reasons"])
        self.assertEqual(report["effective_content_label_conflicts"], 1)

    def test_sampling_is_seeded_balanced_test_only_and_identity_distinct(self):
        for folder in ("INBOX", "Junk"):
            for index in range(20):
                digest = self.add(f"{folder}-{index}", folder=folder,
                                  date=f"2026-01-{index + 1:02}")
                self.add("copy", account="b", folder=folder,
                         raw=self.store.message_path(digest).read_bytes(),
                         date=f"2026-01-{index + 1:02}")
        group_corpus(self.store)
        report = build_split(self.store, test_fraction=.5)
        first = sample_test(self.store, report, per_label=3, seed=42)
        second = sample_test(self.store, report, per_label=3, seed=42)
        self.assertEqual(first, second)
        self.assertEqual(first["selected"], {"ham": 3, "spam": 3})
        self.assertEqual(sum(row["entries"] for row in first["source_distribution"]), 6)
        self.assertEqual({row["folder"] for row in first["source_distribution"]}, {"INBOX", "Junk"})
        self.assertTrue(first["labels_provisional"])
        self.assertEqual(first["label_status"], {"provisional": 6})
        records = self.manifest(first)["records"]
        self.assertEqual(len({record["content_identity"] for record in records}), 6)
        self.assertEqual({record["partition"] for record in records}, {"test"})
        different = self.manifest(sample_test(self.store, report, per_label=3, seed=43))
        self.assertNotEqual(records, different["records"])
        insufficient = sample_test(self.store, report, per_label=11)
        self.assertEqual(insufficient["status"], "insufficient")
        self.assertIsNone(insufficient["manifest_path"])
        self.assertIsNone(report["benchmark_manifest_path"])

    def test_requires_fresh_grouping_and_valid_fraction_and_keeps_single_component_development(self):
        digest = self.add("only")
        with self.assertRaisesRegex(ValueError, "grouping"):
            build_split(self.store)
        group_corpus(self.store)
        for fraction in (0, 1, -1, float("nan"), True):
            with self.assertRaises(ValueError):
                build_split(self.store, fraction)
        report = build_split(self.store)
        self.assertEqual(report["counts"]["test"]["records"], 0)
        self.store.review("a", digest, "spam")
        with self.assertRaisesRegex(ValueError, "grouping"):
            build_split(self.store)


if __name__ == "__main__":
    unittest.main()

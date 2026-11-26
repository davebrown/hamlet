import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hamlet.cli import main
from hamlet.config import read_environment
from hamlet.evaluation import main as evaluation_main
from hamlet.exporter import credentials


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_env(self, values):
        path = self.root / ".env"
        path.write_text("".join(
            f"{name}={json.dumps(value, ensure_ascii=False)}\n"
            for name, value in values.items()
        ))
        return path

    def test_dotenv_quotes_and_literal_shell_characters_without_execution(self):
        password = "quotes'\" # $HOME ${SECRET} $(never-run) `never-run` \\ \n café"
        path = self.write_env({"HAMLET_IMAP_USERNAME": "synthetic",
                               "HAMLET_IMAP_PASSWORD": password})
        before = dict(os.environ)
        with patch.dict(os.environ, {"SECRET": "must-not-expand"}):
            values = read_environment(path, environ={})
        self.assertEqual(credentials(
            "HAMLET_IMAP_USERNAME", "HAMLET_IMAP_PASSWORD", environment=values),
            ("synthetic", password))
        self.assertEqual(dict(os.environ), before)

    def test_process_environment_overrides_dotenv_even_when_empty(self):
        path = self.write_env({"HAMLET_IMAP_PASSWORD": "file-secret", "ONLY_FILE": "file"})
        values = read_environment(path, environ={"HAMLET_IMAP_PASSWORD": "", "ONLY_PROCESS": "env"})
        self.assertEqual(values, {
            "HAMLET_IMAP_PASSWORD": "", "ONLY_FILE": "file", "ONLY_PROCESS": "env",
        })

    def test_missing_file_preserves_environment_and_does_not_search_parents(self):
        self.write_env({"PARENT_ONLY": "do-not-read"})
        child = self.root / "child"
        child.mkdir()
        with contextlib.chdir(child):
            self.assertEqual(read_environment(environ={"EXISTING": "value"}),
                             {"EXISTING": "value"})

    def test_corpus_export_uses_dotenv_without_explicit_credential_file(self):
        data_root = self.root / "private-data"
        self.write_env({
            "HAMLET_DATA_ROOT": str(data_root), "HAMLET_IMAP_HOST": "example.invalid",
            "HAMLET_IMAP_USERNAME": "synthetic", "HAMLET_IMAP_PASSWORD": "test-secret",
        })
        with contextlib.chdir(self.root), patch.dict(os.environ, {}, clear=True):
            with patch("hamlet.cli.export", return_value=[]) as export:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["corpus", "export", "--account", "a"]), 0)
        self.assertEqual(json.loads(output.getvalue()), [])
        self.assertEqual(export.call_args.args[1:5],
                         ("example.invalid", "synthetic", "test-secret", "a"))
        self.assertTrue((data_root / "corpus.sqlite").exists())

    def test_cli_flags_override_environment_and_dotenv(self):
        self.write_env({"HAMLET_DATA_ROOT": str(self.root / "dotenv-data"),
                        "HAMLET_IMAP_HOST": "dotenv.invalid",
                        "HAMLET_IMAP_USERNAME": "synthetic",
                        "HAMLET_IMAP_PASSWORD": "test-secret"})
        explicit_root = self.root / "explicit-data"
        with contextlib.chdir(self.root), patch.dict(
                os.environ, {"HAMLET_DATA_ROOT": str(self.root / "env-data"),
                             "HAMLET_IMAP_HOST": "env.invalid"}, clear=True):
            with patch("hamlet.cli.export", return_value=[]) as export:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main([
                        "--data-root", str(explicit_root), "corpus", "export",
                        "--account", "a", "--host", "flag.invalid",
                    ]), 0)
        self.assertEqual(export.call_args.args[1], "flag.invalid")
        self.assertTrue((explicit_root / "corpus.sqlite").exists())
        self.assertFalse((self.root / "env-data").exists())
        self.assertFalse((self.root / "dotenv-data").exists())

    def test_evaluation_honors_dotenv_data_root_and_explicit_override(self):
        self.write_env({"HAMLET_DATA_ROOT": str(self.root / "dotenv-data")})
        with contextlib.chdir(self.root), patch.dict(os.environ, {}, clear=True):
            with patch("hamlet.evaluation.evaluate", return_value={"complete": True}) as evaluate:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(evaluation_main(["benchmark.json"]), 0)
                self.assertEqual(evaluate.call_args.args[1], str(self.root / "dotenv-data"))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(evaluation_main([
                        "benchmark.json", "--data-root", str(self.root / "explicit"),
                    ]), 0)
                self.assertEqual(evaluate.call_args.args[1], self.root / "explicit")

    def test_configuration_errors_are_sanitized(self):
        with patch("hamlet.cli.read_environment", side_effect=ValueError("private secret")):
            with self.assertLogs("hamlet", level="ERROR") as logs:
                self.assertEqual(main(["corpus", "init"]), 1)
        self.assertNotIn("private secret", " ".join(logs.output))

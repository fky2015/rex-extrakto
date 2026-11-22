"""Check offline Git bundle installation, upgrades, rollback and refusals."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("git_support", ROOT / "scripts/git_support.py")
git_support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(git_support)

REPOSITORY = "fky2015/rex-extrakto"
CANONICAL_ORIGIN = "https://github.com/fky2015/rex-extrakto.git"


class GitInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex git install tests ")
        self.root = Path(self.temp.name)
        self.counter = 0
        self.environment = os.environ.copy()
        self.environment.update({
            "GIT_AUTHOR_NAME": "Rex Tests",
            "GIT_AUTHOR_EMAIL": "rex-tests@example.invalid",
            "GIT_COMMITTER_NAME": "Rex Tests",
            "GIT_COMMITTER_EMAIL": "rex-tests@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment = mock.patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(self.temp.cleanup)

    def git(self, *arguments, cwd=None, check=True):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
             "-c", "maintenance.auto=false", "-c", "gc.auto=0", *arguments],
            cwd=None if cwd is None else str(cwd),
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if check and result.returncode:
            self.fail("git " + " ".join(arguments) + " failed: " + result.stderr)
        return result

    def write_plugin(self, directory, version, content=None):
        path = directory / "plugin.json"
        if content is None:
            content = json.dumps({
                "schema_version": 1,
                "name": "rex-extrakto",
                "version": version,
            }, indent=2) + "\n"
        path.write_text(content)

    def test_install_does_not_spawn_detached_git_maintenance(self):
        source, commit = self.make_source()
        bundle = self.make_bundle(source)
        trace = self.root / "git-trace.json"
        with mock.patch.dict(os.environ, {
            "GIT_TRACE2_EVENT": str(trace), "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "true",
        }):
            transaction = git_support.install_repository(self.root / "installed", bundle, "v0.1.10",
                                                         REPOSITORY, commit)
            transaction.finish()
        if not trace.exists():
            self.skipTest("Git does not support Trace2 event output")
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        commands = [event.get("argv", []) for event in events if event.get("event") == "child_start"]
        self.assertFalse(any("maintenance" in argv and "--auto" in argv for argv in commands), commands)

    def make_source(self, version="v0.1.10", name=None, plugin=True, plugin_content=None,
                    ignore_build_directories=True, object_format=None):
        self.counter += 1
        source = self.root / (name or ("source-" + str(self.counter)))
        source.mkdir()
        init = ["init", "-q", "-b", "main"]
        if object_format:
            init.insert(1, "--object-format=" + object_format)
        self.git(*init, cwd=source)
        if plugin:
            self.write_plugin(source, version, plugin_content)
        (source / "extrakto.lua").write_text("-- fixture plugin " + version + "\n")
        ignored = "__pycache__/\n"
        if ignore_build_directories:
            ignored += "/bin/\n/deps/\n/target/\n"
        (source / ".gitignore").write_text(ignored)
        self.git("add", "-A", cwd=source)
        self.git("commit", "-q", "-m", "fixture " + version, cwd=source)
        commit = self.git("rev-parse", "HEAD", cwd=source).stdout.strip()
        self.git("tag", version, commit, cwd=source)
        return source, commit

    def make_bundle(self, source, source_ref="HEAD", name=None):
        self.counter += 1
        bundle = self.root / (name or ("bundle-" + str(self.counter) + ".bundle"))
        self.git("bundle", "create", str(bundle), source_ref, cwd=source)
        return bundle

    def make_existing(self, version="v0.1.10", origin="git@github.com:fky2015/rex-extrakto.git", name=None):
        self.counter += 1
        destination = self.root / (name or ("existing-" + str(self.counter)))
        destination.mkdir()
        self.git("init", "-q", "-b", "main", cwd=destination)
        self.write_plugin(destination, version)
        (destination / "extrakto.lua").write_text("-- existing plugin " + version + "\n")
        self.git("add", "-A", cwd=destination)
        self.git("commit", "-q", "-m", "existing " + version, cwd=destination)
        commit = self.git("rev-parse", "HEAD", cwd=destination).stdout.strip()
        if origin is not None:
            self.git("remote", "add", "origin", origin, cwd=destination)
        return destination, commit

    def install(self, destination, bundle, version, commit, source_ref="HEAD", repository=REPOSITORY):
        return git_support.install_repository(destination, bundle, version, repository, commit, source_ref)

    def head(self, repository):
        return self.git("rev-parse", "HEAD", cwd=repository).stdout.strip()

    def branch(self, repository):
        result = self.git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=repository, check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def origin(self, repository):
        return self.git("remote", "get-url", "origin", cwd=repository).stdout.strip()

    def plugin_version(self, repository):
        return json.loads((repository / "plugin.json").read_text())["version"]

    def status(self, repository):
        return self.git("status", "--porcelain", cwd=repository).stdout

    def installed_release(self, version="v0.1.10", name=None):
        source, commit = self.make_source(version, name=name)
        bundle = self.make_bundle(source)
        destination = self.root / "config/plugins/rex-extrakto"
        transaction = self.install(destination, bundle, version, commit)
        transaction.finish()
        return destination, source, bundle, commit

    def test_installs_new_checkout_from_head_bundle(self):
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source)
        destination = self.root / "config/plugins/rex-extrakto"

        transaction = self.install(destination, bundle, "v0.1.10", commit.upper())

        self.assertTrue(destination.is_dir())
        self.assertTrue((destination / ".git").is_dir())
        self.assertEqual(self.head(destination), commit)
        self.assertIsNone(self.branch(destination))
        self.assertEqual(self.origin(destination), CANONICAL_ORIGIN)
        self.assertEqual(self.plugin_version(destination), "v0.1.10")
        self.assertEqual(self.status(destination), "")
        self.assertTrue(transaction.created)
        self.assertEqual(transaction.commit, commit)
        self.assertEqual(sorted(path.name for path in destination.parent.iterdir()), ["rex-extrakto"])

        transaction.finish()
        self.assertTrue(destination.is_dir())
        self.assertEqual(self.head(destination), commit)

    def test_installs_new_checkout_from_tag_bundle(self):
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source, source_ref="refs/tags/v0.1.10")
        destination = self.root / "tagged/rex-extrakto"

        transaction = self.install(
            destination, bundle, "v0.1.10", commit, source_ref="refs/tags/v0.1.10",
        )

        self.assertEqual(self.head(destination), commit)
        self.assertIsNone(self.branch(destination))
        self.assertEqual(self.origin(destination), CANONICAL_ORIGIN)
        self.assertEqual(self.plugin_version(destination), "v0.1.10")
        transaction.finish()

    def test_accepts_annotated_tag_bundle_by_tag_or_peeled_commit(self):
        source, commit = self.make_source("v0.1.10")
        self.git("tag", "-d", "v0.1.10", cwd=source)
        self.git("tag", "-a", "v0.1.10", "-m", "release", commit, cwd=source)
        tag_object = self.git("rev-parse", "refs/tags/v0.1.10", cwd=source).stdout.strip()
        self.assertNotEqual(tag_object, commit)
        bundle = self.make_bundle(source, source_ref="refs/tags/v0.1.10")

        for index, expected in enumerate([commit, tag_object]):
            with self.subTest(expected=expected):
                destination = self.root / ("annotated-" + str(index)) / "rex-extrakto"
                transaction = self.install(
                    destination, bundle, "v0.1.10", expected, source_ref="refs/tags/v0.1.10",
                )
                self.assertEqual(self.head(destination), commit)
                self.assertEqual(transaction.commit, commit)
                self.assertEqual(self.plugin_version(destination), "v0.1.10")
                transaction.finish()

    def test_overrides_restrictive_file_transport_configuration(self):
        restrictive_config = self.root / "restrictive.gitconfig"
        restrictive_config.write_text(
            "[protocol]\n\tallow = never\n[protocol \"file\"]\n\tallow = never\n"
        )
        restricted_environment = dict(self.environment)
        restricted_environment.update({
            "GIT_CONFIG_GLOBAL": str(restrictive_config),
            "GIT_ALLOW_PROTOCOL": "https",
        })
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source)

        probe = self.root / "restricted-probe"
        blocked = subprocess.run(
            ["git", "clone", str(bundle), str(probe)],
            env=restricted_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("not allowed", blocked.stderr)
        self.assertFalse((probe / ".git").exists())

        destination = self.root / "restricted/rex-extrakto"
        with mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(restrictive_config),
            "GIT_ALLOW_PROTOCOL": "https",
        }):
            transaction = self.install(destination, bundle, "v0.1.10", commit)
            transaction.finish()
            next_source, next_commit = self.make_source("v0.1.11")
            next_bundle = self.make_bundle(next_source)
            update = self.install(destination, next_bundle, "v0.1.11", next_commit)
            update.finish()

        self.assertEqual(self.head(destination), next_commit)
        self.assertEqual(self.plugin_version(destination), "v0.1.11")
        self.assertEqual(self.origin(destination), CANONICAL_ORIGIN)
        self.assertEqual(self.status(destination), "")

    def test_new_checkout_rollback_removes_destination(self):
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source)
        destination = self.root / "rollback/rex-extrakto"

        transaction = self.install(destination, bundle, "v0.1.10", commit)
        self.assertTrue(destination.is_dir())
        transaction.rollback()
        self.assertFalse(os.path.lexists(destination))
        transaction.rollback()
        transaction.finish()
        self.assertFalse(os.path.lexists(destination))

    def test_update_then_rollback_restores_previous_detached_commit(self):
        destination, _, _, commit = self.installed_release("v0.1.10")
        source, next_commit = self.make_source("v0.1.11")
        bundle = self.make_bundle(source)

        transaction = self.install(destination, bundle, "v0.1.11", next_commit)

        self.assertEqual(self.head(destination), next_commit)
        self.assertIsNone(self.branch(destination))
        self.assertEqual(self.origin(destination), CANONICAL_ORIGIN)
        self.assertEqual(self.plugin_version(destination), "v0.1.11")

        transaction.rollback()
        self.assertEqual(self.head(destination), commit)
        self.assertIsNone(self.branch(destination))
        self.assertEqual(self.origin(destination), CANONICAL_ORIGIN)
        self.assertEqual(self.plugin_version(destination), "v0.1.10")
        self.assertEqual(self.status(destination), "")

        transaction.rollback()
        transaction.finish()
        self.assertEqual(self.head(destination), commit)

    def test_update_restores_branch_and_local_origin_on_rollback(self):
        source, _ = self.make_source("v0.1.10")
        destination, existing_commit = self.make_existing(origin=str(source))
        self.assertEqual(self.branch(destination), "main")

        next_source, next_commit = self.make_source("v0.1.11")
        bundle = self.make_bundle(next_source)

        transaction = self.install(destination, bundle, "v0.1.11", next_commit)

        self.assertEqual(self.head(destination), next_commit)
        self.assertIsNone(self.branch(destination))
        self.assertEqual(self.origin(destination), str(source))

        transaction.rollback()
        self.assertEqual(self.head(destination), existing_commit)
        self.assertEqual(self.branch(destination), "main")
        self.assertEqual(self.origin(destination), str(source))
        self.assertEqual(self.status(destination), "")

    def test_update_preserves_canonical_ssh_origin(self):
        ssh_origin = "git@github.com:fky2015/rex-extrakto.git"
        destination, _ = self.make_existing(origin=ssh_origin)
        source, commit = self.make_source("v0.1.11")
        bundle = self.make_bundle(source)

        transaction = self.install(destination, bundle, "v0.1.11", commit)

        self.assertEqual(self.head(destination), commit)
        self.assertEqual(self.origin(destination), ssh_origin)
        transaction.finish()

    def test_reinstalling_same_version_is_idempotent(self):
        destination, _, bundle, commit = self.installed_release("v0.1.10")

        transaction = self.install(destination, bundle, "v0.1.10", commit)

        self.assertEqual(self.head(destination), commit)
        self.assertEqual(self.origin(destination), CANONICAL_ORIGIN)
        self.assertEqual(self.plugin_version(destination), "v0.1.10")
        self.assertEqual(self.status(destination), "")
        transaction.finish()

    def test_refuses_dirty_tracked_file(self):
        destination, _, _, commit = self.installed_release("v0.1.10")
        (destination / "extrakto.lua").write_text("-- local modification\n")
        source, next_commit = self.make_source("v0.1.11")
        bundle = self.make_bundle(source)

        with self.assertRaises(ValueError) as raised:
            self.install(destination, bundle, "v0.1.11", next_commit)

        self.assertIn("dirty", str(raised.exception).lower())
        self.assertEqual(self.head(destination), commit)
        self.assertEqual(self.plugin_version(destination), "v0.1.10")
        self.assertEqual((destination / "extrakto.lua").read_text(), "-- local modification\n")

    def test_refuses_dirty_untracked_file(self):
        destination, _, _, commit = self.installed_release("v0.1.10")
        (destination / "notes.txt").write_text("user notes\n")
        source, next_commit = self.make_source("v0.1.11")
        bundle = self.make_bundle(source)

        with self.assertRaises(ValueError) as raised:
            self.install(destination, bundle, "v0.1.11", next_commit)

        self.assertIn("dirty", str(raised.exception).lower())
        self.assertEqual(self.head(destination), commit)
        self.assertTrue((destination / "notes.txt").is_file())

    def test_allows_gitignored_build_outputs(self):
        destination, _, _, _ = self.installed_release("v0.1.10")
        for relative, content in [
            ("bin/rex-extract", "binary\n"),
            ("deps/fzf", "binary\n"),
            ("target/artifact", "build\n"),
        ]:
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        source, commit = self.make_source("v0.1.11")
        bundle = self.make_bundle(source)

        transaction = self.install(destination, bundle, "v0.1.11", commit)

        self.assertEqual(self.head(destination), commit)
        for relative, content in [
            ("bin/rex-extract", "binary\n"),
            ("deps/fzf", "binary\n"),
            ("target/artifact", "build\n"),
        ]:
            self.assertEqual((destination / relative).read_text(), content)
        transaction.finish()

    def test_allows_untracked_build_outputs_without_ignore_rules(self):
        source, commit = self.make_source("v0.1.10", ignore_build_directories=False)
        bundle = self.make_bundle(source)
        destination = self.root / "build-outputs/rex-extrakto"
        self.install(destination, bundle, "v0.1.10", commit).finish()
        for relative in ["bin/tool", "deps/fzf", "target/artifact"]:
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("output\n")
        next_source, next_commit = self.make_source("v0.1.11", ignore_build_directories=False)
        next_bundle = self.make_bundle(next_source)

        transaction = self.install(destination, next_bundle, "v0.1.11", next_commit)

        self.assertEqual(self.head(destination), next_commit)
        self.assertTrue((destination / "bin/tool").is_file())
        transaction.finish()

    def test_refuses_plain_user_directory(self):
        destination = self.root / "user/rex-extrakto"
        destination.mkdir(parents=True)
        (destination / "notes.txt").write_text("keep me\n")
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source)

        with self.assertRaises(ValueError) as raised:
            self.install(destination, bundle, "v0.1.10", commit)

        self.assertIn("not a git repository", str(raised.exception).lower())
        self.assertEqual((destination / "notes.txt").read_text(), "keep me\n")
        self.assertFalse((destination / ".git").exists())

    def test_refuses_destination_that_is_a_file(self):
        destination = self.root / "user-file"
        destination.write_text("keep me\n")
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source)

        with self.assertRaises(ValueError) as raised:
            self.install(destination, bundle, "v0.1.10", commit)

        self.assertIn("not a directory", str(raised.exception).lower())
        self.assertEqual(destination.read_text(), "keep me\n")

    def test_refuses_destination_inside_parent_repository(self):
        parent = self.root / "parent"
        parent.mkdir()
        self.git("init", "-q", "-b", "main", cwd=parent)
        (parent / "README.md").write_text("parent\n")
        self.git("add", "-A", cwd=parent)
        self.git("commit", "-q", "-m", "parent", cwd=parent)
        destination = parent / "plugins/rex-extrakto"
        destination.mkdir(parents=True)
        (destination / "notes.txt").write_text("keep me\n")
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source)

        with self.assertRaises(ValueError) as raised:
            self.install(destination, bundle, "v0.1.10", commit)

        self.assertIn("another git repository", str(raised.exception).lower())
        self.assertEqual((destination / "notes.txt").read_text(), "keep me\n")
        self.assertFalse((destination / ".git").exists())

    def test_refuses_symlinked_destination(self):
        real, _, _, commit = self.installed_release("v0.1.10")
        link = self.root / "linked-rex-extrakto"
        link.symlink_to(real, target_is_directory=True)
        source, next_commit = self.make_source("v0.1.11")
        bundle = self.make_bundle(source)

        with self.assertRaises(ValueError) as raised:
            self.install(link, bundle, "v0.1.11", next_commit)

        self.assertIn("symlink", str(raised.exception).lower())
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.head(real), commit)
        self.assertEqual(self.plugin_version(real), "v0.1.10")

    def test_refuses_unrecognized_or_unrelated_origin(self):
        source, next_commit = self.make_source("v0.1.11")
        bundle = self.make_bundle(source)
        cases = [
            ("https://github.com/someone/else.git", "origin points to"),
            ("git@gitlab.com:someone/else.git", "unrecognized origin"),
            (None, "no origin remote"),
        ]
        for index, (origin, message) in enumerate(cases):
            with self.subTest(origin=origin):
                destination, existing_commit = self.make_existing(
                    origin=origin, name="unrelated-" + str(index),
                )
                with self.assertRaises(ValueError) as raised:
                    self.install(destination, bundle, "v0.1.11", next_commit)
                self.assertIn(message, str(raised.exception).lower())
                self.assertEqual(self.head(destination), existing_commit)
                self.assertEqual(self.plugin_version(destination), "v0.1.10")

    def test_refuses_version_mismatch_for_new_checkout(self):
        source, commit = self.make_source("v0.1.9")
        bundle = self.make_bundle(source)
        destination = self.root / "mismatch/rex-extrakto"

        with self.assertRaises(ValueError) as raised:
            self.install(destination, bundle, "v0.1.10", commit)

        self.assertIn("does not match requested", str(raised.exception))
        self.assertFalse(os.path.lexists(destination))

    def test_refuses_version_mismatch_for_existing_checkout(self):
        destination, _, _, commit = self.installed_release("v0.1.10")
        source, next_commit = self.make_source("v0.1.9")
        bundle = self.make_bundle(source)

        with self.assertRaises(ValueError) as raised:
            self.install(destination, bundle, "v0.1.10", next_commit)

        self.assertIn("does not match requested", str(raised.exception))
        self.assertEqual(self.head(destination), commit)
        self.assertEqual(self.plugin_version(destination), "v0.1.10")
        self.assertEqual(self.origin(destination), CANONICAL_ORIGIN)
        self.assertEqual(self.status(destination), "")

    def test_refuses_expected_commit_mismatch_without_touching_destination(self):
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source)
        wrong = "0" * 40
        destination = self.root / "commit-mismatch/rex-extrakto"

        with self.assertRaises(ValueError) as raised:
            self.install(destination, bundle, "v0.1.10", wrong)

        self.assertIn("expected", str(raised.exception))
        self.assertFalse(os.path.lexists(destination))

        installed, _, _, installed_commit = self.installed_release("v0.1.10")
        with self.assertRaises(ValueError):
            self.install(installed, bundle, "v0.1.10", wrong)
        self.assertEqual(self.head(installed), installed_commit)
        self.assertEqual(self.plugin_version(installed), "v0.1.10")

    def test_refuses_missing_bundle_reference(self):
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source)
        destination = self.root / "missing-ref/rex-extrakto"

        with self.assertRaises(ValueError) as raised:
            self.install(
                destination, bundle, "v0.1.10", commit, source_ref="refs/tags/v0.1.99",
            )

        self.assertIn("does not advertise", str(raised.exception))
        self.assertFalse(os.path.lexists(destination))

    def test_rejects_dangerous_arguments(self):
        source, commit = self.make_source("v0.1.10")
        bundle = self.make_bundle(source)
        attempts = [
            ("version", {"version": "--upload-pack=evil"}),
            ("version", {"version": "v1;rm -rf /"}),
            ("version", {"version": "v1/../../escape"}),
            ("version", {"version": ""}),
            ("source_ref", {"source_ref": "--upload-pack=evil"}),
            ("source_ref", {"source_ref": "HEAD;rm"}),
            ("source_ref", {"source_ref": "refs/heads/main"}),
            ("repository", {"repository": "../evil"}),
            ("repository", {"repository": "owner/repo/extra"}),
            ("repository", {"repository": "owner/.."}),
            ("expected_commit", {"commit": "not-a-commit"}),
            ("expected_commit", {"commit": "g" * 40}),
        ]
        for index, (label, values) in enumerate(attempts):
            with self.subTest(label=label, values=values):
                destination = self.root / ("dangerous-" + str(index)) / "rex-extrakto"
                arguments = {"version": "v0.1.10", "commit": commit, "source_ref": "HEAD"}
                arguments.update(values)
                with self.assertRaises(ValueError):
                    self.install(destination, bundle, arguments["version"], arguments["commit"],
                                 source_ref=arguments["source_ref"],
                                 repository=values.get("repository", REPOSITORY))
                self.assertFalse(os.path.lexists(destination))

    def test_refuses_invalid_or_missing_plugin_manifest(self):
        cases = [
            ("missing", {"plugin": False}),
            ("malformed", {"plugin_content": "{not json\n"}),
            ("array", {"plugin_content": "[]\n"}),
            ("numeric-version", {"plugin_content": json.dumps({"version": 1}) + "\n"}),
        ]
        for index, (label, options) in enumerate(cases):
            with self.subTest(label=label):
                source, commit = self.make_source("v0.1.10", name="manifest-" + str(index), **options)
                bundle = self.make_bundle(source)
                destination = self.root / ("manifest-" + str(index)) / "rex-extrakto"
                with self.assertRaises(ValueError):
                    self.install(destination, bundle, "v0.1.10", commit)
                self.assertFalse(os.path.lexists(destination))

    def test_does_not_copy_or_execute_git_hooks(self):
        source, commit = self.make_source("v0.1.10")
        source_marker = self.root / "source-hook-ran"
        source_hook = source / ".git/hooks/post-checkout"
        source_hook.write_text("#!/bin/sh\nprintf ran > " + shlex.quote(str(source_marker)) + "\n")
        source_hook.chmod(0o755)
        bundle = self.make_bundle(source)
        destination = self.root / "hooks/rex-extrakto"
        self.install(destination, bundle, "v0.1.10", commit).finish()
        self.assertFalse(source_marker.exists())
        installed_hook = destination / ".git/hooks/post-checkout"
        self.assertFalse(installed_hook.exists() and "source-hook-ran" in installed_hook.read_text())

        local_marker = self.root / "local-hook-ran"
        installed_hook.parent.mkdir(parents=True, exist_ok=True)
        installed_hook.write_text("#!/bin/sh\nprintf ran > " + shlex.quote(str(local_marker)) + "\n")
        installed_hook.chmod(0o755)
        next_source, next_commit = self.make_source("v0.1.11")
        next_bundle = self.make_bundle(next_source)

        transaction = self.install(destination, next_bundle, "v0.1.11", next_commit)

        self.assertEqual(self.head(destination), next_commit)
        self.assertFalse(local_marker.exists())
        self.assertTrue(installed_hook.is_file())
        transaction.finish()

    def test_supports_sha256_bundle(self):
        probe = self.root / "sha256-probe"
        result = self.git("init", "--object-format=sha256", "-q", str(probe), check=False)
        if result.returncode:
            self.skipTest("Git does not support SHA-256 repositories")
        source, commit = self.make_source("v0.1.10", object_format="sha256")
        self.assertEqual(len(commit), 64)
        bundle = self.make_bundle(source)
        destination = self.root / "sha256/rex-extrakto"

        transaction = self.install(destination, bundle, "v0.1.10", commit)

        self.assertEqual(self.head(destination), commit)
        self.assertEqual(self.plugin_version(destination), "v0.1.10")
        transaction.finish()


if __name__ == "__main__":
    unittest.main()

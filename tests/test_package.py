"""Check reproducible release archives, checksums and publication gating."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("packager", ROOT / "scripts/package.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)
installer = sys.modules["install"]

VERSION = "v0.1.0"
SOURCE_COMMIT = "a" * 40
HELPERS = ["rex-extract", "rex-collect", "rex-popup"]
EXECUTABLE_FILES = {"bin/rex-extract", "bin/rex-collect", "bin/rex-popup", "deps/fzf"}


def release_manifest(target, version=VERSION, **overrides):
    manifest = {
        "schema_version": 1,
        "version": version,
        "target": target,
        "picker": "rust",
        "helpers": list(HELPERS),
        "fzf_version": packager.FZF_VERSION,
        "source_commit": SOURCE_COMMIT,
        "source_ref": "refs/tags/" + version,
        "source_bundle": packager.SOURCE_BUNDLE,
    }
    manifest.update(overrides)
    return manifest


def archive_files(version=VERSION, target=None, manifest=None, plugin_version=None,
                  plugin_json=None, source_bundle=b"# v2 git bundle\nfixture bundle\n"):
    target = target or installer.host_target()
    manifest = manifest or release_manifest(target, version)
    if plugin_version is None:
        plugin_version = version
    if plugin_json is None:
        plugin_json = (json.dumps({
            "schema_version": 1, "name": "rex-extrakto", "version": plugin_version,
        }, indent=2) + "\n").encode()
    return {
        "README.md": b"readme\n",
        "LICENSE": b"license\n",
        "extrakto.lua": b"-- plugin\n",
        "init.lua": b"-- rex-extrakto module\nreturn {}\n",
        "plugin.json": plugin_json,
        "scripts/install.py": b"# installer\n",
        "scripts/install.sh": b"#!/bin/sh\n",
        "scripts/git_support.py": b"# git support\n",
        "docs/configuration.md": b"# configuration\n",
        "docs/development.md": b"# development\n",
        "docs/installation.md": b"# installation\n",
        "bin/rex-extract": b"fixture extract\n",
        "bin/rex-collect": b"fixture collect\n",
        "bin/rex-popup": b"fixture popup\n",
        "deps/fzf": b"fixture fzf\n",
        "deps/LICENSE.fzf": b"fzf license\n",
        "licenses/README.txt": b"Rust dependency licenses\n",
        "source.bundle": source_bundle,
        "release.json": (json.dumps(manifest, indent=2) + "\n").encode(),
    }


def write_archive(path, files, modes=None, extra_members=()):
    prefix = path.name[:-len(".tar.gz")]
    modes = modes or {}
    with tarfile.open(path, "w:gz") as archive:
        for relative, content in files.items():
            member = tarfile.TarInfo(prefix + "/" + relative)
            member.size = len(content)
            member.mode = modes.get(relative, 0o755 if relative in EXECUTABLE_FILES else 0o644)
            archive.addfile(member, io.BytesIO(content))
        for member, content in extra_members:
            member.name = prefix + "/" + member.name
            archive.addfile(member, io.BytesIO(content) if content is not None else None)


def write_release(directory, target, version=VERSION, manifest=None, omit=(), modes=None,
                  extra_members=(), plugin_version=None, plugin_json=None,
                  source_bundle=b"# v2 git bundle\nfixture bundle\n"):
    path = directory / packager.archive_name(version, target)
    files = archive_files(version, target, manifest, plugin_version=plugin_version,
                          plugin_json=plugin_json, source_bundle=source_bundle)
    for name in omit:
        files.pop(name)
    write_archive(path, files, modes=modes, extra_members=extra_members)
    return path


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex package tests ")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self):
        payload = self.root / "rex-extrakto-v0.1.0-aarch64-apple-darwin"
        (payload / "bin").mkdir(parents=True)
        (payload / "docs").mkdir()
        (payload / "empty").mkdir()
        (payload / "README.md").write_text("readme\n")
        (payload / "bin/rex-extract").write_text("#!/bin/sh\n")
        (payload / "bin/rex-extract").chmod(0o755)
        (payload / "docs/installation.md").write_text("# install\n")
        (payload / "restricted").write_text("secret\n")
        (payload / "restricted").chmod(0o600)
        return payload

    def test_archives_are_byte_reproducible_and_normalized(self):
        payload = self.payload()
        first, second = self.root / "first.tar.gz", self.root / "second.tar.gz"
        packager.build_archive(payload, first)
        packager.build_archive(payload, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first) as archive:
            members = {member.name: member for member in archive}
            self.assertEqual(set(members), {
                payload.name,
                payload.name + "/README.md",
                payload.name + "/bin",
                payload.name + "/bin/rex-extract",
                payload.name + "/docs",
                payload.name + "/docs/installation.md",
                payload.name + "/empty",
                payload.name + "/restricted",
            })
            for member in members.values():
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                self.assertEqual((member.uname, member.gname), ("", ""))
                self.assertFalse(member.pax_headers)
            self.assertEqual(members[payload.name].mode, 0o755)
            self.assertEqual(members[payload.name + "/bin"].mode, 0o755)
            self.assertEqual(members[payload.name + "/empty"].mode, 0o755)
            self.assertEqual(members[payload.name + "/bin/rex-extract"].mode, 0o755)
            self.assertEqual(members[payload.name + "/README.md"].mode, 0o644)
            self.assertEqual(members[payload.name + "/restricted"].mode, 0o644)
            self.assertEqual(archive.extractfile(members[payload.name + "/README.md"]).read(), b"readme\n")

    def test_build_archive_replaces_an_existing_destination(self):
        payload = self.payload()
        destination = self.root / "release.tar.gz"
        destination.write_bytes(b"stale")
        packager.build_archive(payload, destination)
        with tarfile.open(destination) as archive:
            self.assertIn(payload.name + "/README.md", archive.getnames())


class ChecksumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex checksum tests ")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_checksums_are_sorted_installer_compatible_and_ignore_directories(self):
        first = self.root / "rex-extrakto-v0.1.0-x86_64-unknown-linux-musl.tar.gz"
        second = self.root / "rex-extrakto-v0.1.0-aarch64-apple-darwin.tar.gz"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        (self.root / "unrelated.tar.gz").write_bytes(b"unrelated")
        (self.root / "rex-extrakto-v0.1.0-directory.tar.gz").mkdir()
        packager.checksums(self.root)
        lines = (self.root / "SHA256SUMS").read_text().splitlines()
        self.assertEqual([line.split("  ")[1] for line in lines], [second.name, first.name])
        for line in lines:
            digest, name = line.split("  ")
            self.assertEqual(digest, hashlib.sha256((self.root / name).read_bytes()).hexdigest())
            self.assertEqual(installer.checksum_for(line + "\n", name), digest)

    def test_checksums_require_at_least_one_archive(self):
        with self.assertRaisesRegex(ValueError, "No release archives"):
            packager.checksums(self.root)


class ReleaseGatingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex release tests ")
        self.root = Path(self.temp.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_release_set(self, extra_members=(), omit=(), version=VERSION, manifest_for=None,
                          modes=None, plugin_version=None, plugin_json=None,
                          source_bundle=b"# v2 git bundle\nfixture bundle\n"):
        targets = sorted(packager.TARGETS.values())
        for index, target in enumerate(targets):
            manifest = manifest_for(target) if manifest_for else None
            write_release(self.dist, target, version=version, manifest=manifest, omit=omit, modes=modes,
                          extra_members=extra_members if index == len(targets) - 1 else (),
                          plugin_version=plugin_version, plugin_json=plugin_json,
                          source_bundle=source_bundle)

    def test_four_distinct_targets_pass_and_checksums_cover_them(self):
        self.write_release_set()
        packager.verify_release(self.dist, VERSION)
        packager.checksums(self.dist)
        names = [line.split("  ")[1] for line in (self.dist / "SHA256SUMS").read_text().splitlines()]
        self.assertEqual(len(names), 4)
        self.assertEqual(set(names), {packager.archive_name(VERSION, target) for target in packager.TARGETS.values()})

    def test_missing_platform_is_rejected(self):
        for target in sorted(packager.TARGETS.values())[:-1]:
            write_release(self.dist, target)
        with self.assertRaisesRegex(ValueError, "four platform"):
            packager.verify_release(self.dist, VERSION)

    def test_extra_archive_is_rejected(self):
        self.write_release_set()
        (self.dist / "rex-extrakto-v0.1.0-extra.tar.gz").write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "four platform"):
            packager.verify_release(self.dist, VERSION)

    def test_duplicate_manifest_target_is_rejected(self):
        targets = sorted(packager.TARGETS.values())

        def manifest_for(target):
            if target == targets[0]:
                return release_manifest(targets[1])
            return None

        self.write_release_set(manifest_for=manifest_for)
        with self.assertRaisesRegex(ValueError, "version/target"):
            packager.verify_release(self.dist, VERSION)

    def test_version_must_match_the_release_tag(self):
        self.write_release_set(version="v0.2.0")
        with self.assertRaisesRegex(ValueError, "version/target"):
            packager.verify_release(self.dist, VERSION)

    def test_schema_version_is_rejected(self):
        def manifest_for(target):
            return release_manifest(target, schema_version=2)

        self.write_release_set(manifest_for=manifest_for)
        with self.assertRaisesRegex(ValueError, "schema"):
            packager.verify_release(self.dist, VERSION)

    def test_python_picker_manifest_is_rejected(self):
        self.write_release_set(manifest_for=lambda target: release_manifest(target, picker="python"))
        with self.assertRaisesRegex(ValueError, "picker"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_rust_picker_manifest_is_rejected(self):
        self.write_release_set(manifest_for=lambda target: release_manifest(target, picker=None))
        with self.assertRaisesRegex(ValueError, "picker"):
            packager.verify_release(self.dist, VERSION)

    def test_helper_list_must_match_all_three_helpers(self):
        self.write_release_set(manifest_for=lambda target: release_manifest(target, helpers=HELPERS[:2]))
        with self.assertRaisesRegex(ValueError, "helper list"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_pinned_fzf_is_rejected(self):
        def manifest_for(target):
            return release_manifest(target, fzf_version=None)

        self.write_release_set(manifest_for=manifest_for)
        with self.assertRaisesRegex(ValueError, "pinned fzf"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_required_file_is_rejected(self):
        self.write_release_set(omit=("deps/fzf",))
        with self.assertRaisesRegex(ValueError, "missing deps/fzf"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_rust_popup_helper_is_rejected(self):
        self.write_release_set(omit=("bin/rex-popup",))
        with self.assertRaisesRegex(ValueError, "missing bin/rex-popup"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_module_entry_is_rejected(self):
        self.write_release_set(omit=("init.lua",))
        with self.assertRaisesRegex(ValueError, "missing init.lua"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_plugin_manifest_is_rejected(self):
        self.write_release_set(omit=("plugin.json",))
        with self.assertRaisesRegex(ValueError, "missing plugin.json"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_git_support_is_rejected(self):
        self.write_release_set(omit=("scripts/git_support.py",))
        with self.assertRaisesRegex(ValueError, "missing scripts/git_support.py"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_source_bundle_is_rejected(self):
        self.write_release_set(omit=("source.bundle",))
        with self.assertRaisesRegex(ValueError, "missing source.bundle"):
            packager.verify_release(self.dist, VERSION)

    def test_empty_source_bundle_is_rejected(self):
        self.write_release_set(source_bundle=b"")
        with self.assertRaisesRegex(ValueError, "empty source bundle"):
            packager.verify_release(self.dist, VERSION)

    def test_source_commit_must_be_lowercase_hex(self):
        for value in ("A" * 40, "a" * 39, "a" * 41, "g" * 40, None, 123):
            with self.subTest(value=value):
                self.write_release_set(manifest_for=lambda target, value=value: release_manifest(target, source_commit=value))
                with self.assertRaisesRegex(ValueError, "source_commit"):
                    packager.verify_release(self.dist, VERSION)

    def test_source_commit_accepts_sha256_length(self):
        self.write_release_set(manifest_for=lambda target: release_manifest(target, source_commit="b" * 64))
        packager.verify_release(self.dist, VERSION)

    def test_head_source_ref_is_accepted(self):
        self.write_release_set(manifest_for=lambda target: release_manifest(target, source_ref="HEAD"))
        packager.verify_release(self.dist, VERSION)

    def test_source_ref_must_be_head_or_the_release_tag(self):
        for value in ("refs/heads/main", "refs/tags/v9.9.9", None):
            with self.subTest(value=value):
                self.write_release_set(manifest_for=lambda target, value=value: release_manifest(target, source_ref=value))
                with self.assertRaisesRegex(ValueError, "source_ref"):
                    packager.verify_release(self.dist, VERSION)

    def test_source_bundle_metadata_must_name_the_bundle(self):
        self.write_release_set(manifest_for=lambda target: release_manifest(target, source_bundle="plugin.bundle"))
        with self.assertRaisesRegex(ValueError, "source_bundle"):
            packager.verify_release(self.dist, VERSION)

    def test_source_revision_must_be_consistent_across_platforms(self):
        targets = sorted(packager.TARGETS.values())

        def manifest_for(target):
            if target == targets[0]:
                return release_manifest(target, source_commit="b" * 40)
            return None

        self.write_release_set(manifest_for=manifest_for)
        with self.assertRaisesRegex(ValueError, "source revision"):
            packager.verify_release(self.dist, VERSION)

    def test_plugin_version_must_match_the_release_tag(self):
        self.write_release_set(plugin_version="v9.9.9")
        with self.assertRaisesRegex(ValueError, "plugin.json version"):
            packager.verify_release(self.dist, VERSION)

    def test_plugin_manifest_must_be_valid_json(self):
        self.write_release_set(plugin_json=b"{not json\n")
        with self.assertRaisesRegex(ValueError, "plugin.json"):
            packager.verify_release(self.dist, VERSION)

    def test_plugin_manifest_must_be_an_object(self):
        self.write_release_set(plugin_json=b"[]\n")
        with self.assertRaisesRegex(ValueError, "plugin.json"):
            packager.verify_release(self.dist, VERSION)

    def test_legacy_python_picker_is_rejected(self):
        legacy = tarfile.TarInfo("scripts/popup.py")
        legacy.size = len(b"# popup\n")
        self.write_release_set(extra_members=[(legacy, b"# popup\n")])
        with self.assertRaisesRegex(ValueError, "legacy Python picker"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_manifest_is_rejected(self):
        self.write_release_set(omit=("release.json",))
        with self.assertRaisesRegex(ValueError, "no unique manifest"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_executable_bit_is_rejected(self):
        self.write_release_set(modes={"deps/fzf": 0o644})
        with self.assertRaisesRegex(ValueError, "executable bit"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_helper_executable_bit_is_rejected(self):
        for helper in HELPERS:
            with self.subTest(helper=helper):
                self.write_release_set(modes={"bin/" + helper: 0o644})
                with self.assertRaisesRegex(ValueError, "executable bit"):
                    packager.verify_release(self.dist, VERSION)

    def test_links_are_rejected(self):
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        self.write_release_set(extra_members=[(link, None)])
        with self.assertRaisesRegex(ValueError, "links and special files"):
            packager.verify_release(self.dist, VERSION)

    def test_traversal_is_rejected(self):
        traversal = tarfile.TarInfo("../escape")
        traversal.size = 1
        self.write_release_set(extra_members=[(traversal, b"x")])
        with self.assertRaisesRegex(ValueError, "Unsafe archive path"):
            packager.verify_release(self.dist, VERSION)


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex license tests ")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def package(self, name, version, files, license, license_file=None):
        directory = self.root / (name + "-" + version)
        directory.mkdir()
        (directory / "Cargo.toml").write_text("")
        for filename, content in files.items():
            (directory / filename).write_text(content)
        package = {"name": name, "version": version, "id": name + " " + version,
                   "manifest_path": str(directory / "Cargo.toml"), "license": license}
        if license_file:
            package["license_file"] = license_file
        return package

    def test_only_resolved_dependencies_are_bundled(self):
        packages = [
            self.package("rex-extrakto", "0.1.0", {"LICENSE": "project\n"}, "MIT"),
            self.package("alpha", "1.0.0", {"LICENSE-MIT": "mit\n", "COPYING": "copying\n"}, "MIT OR Apache-2.0"),
            self.package("beta", "2.0.0", {"LICENSE": "beta\n"}, None, license_file="LICENSE"),
            self.package("unused", "3.0.0", {"LICENSE": "unused\n"}, "MIT"),
        ]
        metadata = {
            "packages": packages,
            "resolve": {"nodes": [{"id": package["id"]} for package in packages if package["name"] != "unused"]},
        }
        payload = self.root / "payload"
        payload.mkdir()
        packager.dependency_licenses(metadata, payload)
        licenses = payload / "licenses"
        self.assertEqual((licenses / "alpha-1.0.0/LICENSE-MIT").read_text(), "mit\n")
        self.assertEqual((licenses / "alpha-1.0.0/COPYING").read_text(), "copying\n")
        self.assertEqual((licenses / "beta-2.0.0/LICENSE").read_text(), "beta\n")
        self.assertFalse((licenses / "unused-3.0.0").exists())
        self.assertFalse((licenses / "rex-extrakto-0.1.0").exists())
        text = (licenses / "README.txt").read_text()
        self.assertIn("Rust dependency licenses\n", text)
        self.assertIn("alpha 1.0.0 — MIT OR Apache-2.0", text)
        self.assertIn("beta 2.0.0 — see included license", text)
        self.assertNotIn("unused", text)
        self.assertNotIn("rex-extrakto", text)


class SourceBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex source bundle tests ")
        self.root = Path(self.temp.name)
        self.environment = mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Rex Tests",
            "GIT_AUTHOR_EMAIL": "rex-tests@example.invalid",
            "GIT_COMMITTER_NAME": "Rex Tests",
            "GIT_COMMITTER_EMAIL": "rex-tests@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temp.cleanup)

    def git(self, *arguments, cwd=None):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
             "-c", "tag.gpgsign=false", *arguments],
            cwd=None if cwd is None else str(cwd), env=os.environ,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
        )
        if result.returncode:
            self.fail("git " + " ".join(arguments) + " failed: " + result.stderr)
        return result.stdout.strip()

    def source(self, version="v0.1.10", name="source", tag=True, annotated=False):
        root = self.root / name
        root.mkdir()
        self.git("init", "-q", "-b", "main", cwd=root)
        (root / "Cargo.toml").write_text('[package]\nname = "rex-extrakto"\nversion = "' + version[1:] + '"\n')
        (root / "plugin.json").write_text(json.dumps({
            "schema_version": 1, "name": "rex-extrakto", "version": version,
        }, indent=2) + "\n")
        (root / "init.lua").write_text("-- rex-extrakto module " + name + "\nreturn {}\n")
        self.git("add", "-A", cwd=root)
        self.git("commit", "-q", "-m", "fixture " + version, cwd=root)
        commit = self.git("rev-parse", "HEAD", cwd=root)
        if tag:
            arguments = ["tag", "-a", "-m", "release", version, commit] if annotated else ["tag", version, commit]
            self.git(*arguments, cwd=root)
        return root, commit

    def test_resolves_the_release_tag_when_it_points_at_head(self):
        root, commit = self.source()
        self.assertEqual(packager.resolve_source(root, "v0.1.10"), ("refs/tags/v0.1.10", commit))

    def test_falls_back_to_head_before_the_tag_exists(self):
        root, commit = self.source(tag=False)
        self.assertEqual(packager.resolve_source(root, "v0.1.10"), ("HEAD", commit))

    def test_release_tag_must_match_the_checked_out_source(self):
        root, commit = self.source(tag=False)
        self.git("tag", "v0.1.10", commit, cwd=root)
        (root / "init.lua").write_text("-- newer source\n")
        self.git("commit", "-qam", "newer", cwd=root)
        with self.assertRaisesRegex(ValueError, "checked out source"):
            packager.resolve_source(root, "v0.1.10")

    def test_requires_a_clean_tracked_source_tree(self):
        root, _ = self.source()
        (root / "init.lua").write_text("-- modified\n")
        with self.assertRaisesRegex(ValueError, "uncommitted tracked changes"):
            packager.require_clean_source(root)

    def test_untracked_files_do_not_dirty_the_source_tree(self):
        root, _ = self.source()
        (root / "notes.txt").write_text("local notes\n")
        packager.require_clean_source(root)

    def test_plugin_version_must_match_cargo(self):
        root, _ = self.source()
        self.assertEqual(packager.require_plugin_version(root, "v0.1.10"), "v0.1.10")
        (root / "plugin.json").write_text(json.dumps({"version": "v0.1.9"}) + "\n")
        with self.assertRaisesRegex(ValueError, "plugin.json version"):
            packager.require_plugin_version(root, "v0.1.10")

    def test_plugin_manifest_must_be_valid(self):
        root, _ = self.source()
        for content in ("{not json\n", "[]\n", json.dumps({"version": 1}) + "\n", json.dumps({}) + "\n"):
            with self.subTest(content=content):
                (root / "plugin.json").write_text(content)
                with self.assertRaisesRegex(ValueError, "plugin.json"):
                    packager.require_plugin_version(root, "v0.1.10")

    def test_missing_plugin_manifest_is_rejected(self):
        root, _ = self.source()
        (root / "plugin.json").unlink()
        with self.assertRaisesRegex(ValueError, "plugin.json"):
            packager.require_plugin_version(root, "v0.1.10")

    def test_creates_head_bundle_with_matching_provenance(self):
        root, commit = self.source(tag=False)
        bundle = self.root / "source.bundle"
        packager.create_source_bundle(root, "HEAD", commit, bundle)
        self.assertTrue(bundle.is_file())
        self.assertGreater(bundle.stat().st_size, 0)
        self.assertEqual(packager.bundle_heads(bundle, root), [(commit, "HEAD")])
        packager.verify_source_bundle(bundle, root, "HEAD", commit)

    def test_creates_annotated_tag_bundle_and_accepts_the_tag_object(self):
        root, commit = self.source(annotated=True)
        tag_object = self.git("rev-parse", "refs/tags/v0.1.10", cwd=root)
        self.assertNotEqual(tag_object, commit)
        bundle = self.root / "source.bundle"
        packager.create_source_bundle(root, "refs/tags/v0.1.10", commit, bundle)
        self.assertEqual(packager.bundle_heads(bundle, root), [(tag_object, "refs/tags/v0.1.10")])
        packager.verify_source_bundle(bundle, root, "refs/tags/v0.1.10", commit)

    def test_rejects_a_bundle_that_advertises_another_commit(self):
        root, commit = self.source(tag=False)
        other, _ = self.source(tag=False, name="other")
        bundle = self.root / "other.bundle"
        self.git("bundle", "create", str(bundle), "HEAD", cwd=other)
        with self.assertRaisesRegex(ValueError, "expected"):
            packager.verify_source_bundle(bundle, root, "HEAD", commit)

    def test_rejects_a_bundle_without_the_requested_ref(self):
        root, commit = self.source(tag=False)
        bundle = self.root / "source.bundle"
        self.git("bundle", "create", str(bundle), "HEAD", cwd=root)
        with self.assertRaisesRegex(ValueError, "does not advertise"):
            packager.verify_source_bundle(bundle, root, "refs/tags/v0.1.10", commit)

    def test_rejects_missing_and_empty_bundles(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            packager.verify_source_bundle(self.root / "absent.bundle", self.root, "HEAD", SOURCE_COMMIT)
        empty = self.root / "empty.bundle"
        empty.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            packager.verify_source_bundle(empty, self.root, "HEAD", SOURCE_COMMIT)

    def test_rejects_files_without_git_bundle_provenance(self):
        path = self.root / "fake.bundle"
        path.write_text("not a bundle\n")
        with self.assertRaisesRegex(ValueError, "not a Git bundle"):
            packager.verify_source_bundle(path, self.root, "HEAD", SOURCE_COMMIT)


class PackagingLayoutTests(unittest.TestCase):
    def test_release_layout_uses_rust_picker_and_three_helpers(self):
        self.assertEqual(packager.PICKER, "rust")
        self.assertEqual(packager.HELPERS, HELPERS)
        self.assertNotIn("scripts/popup.py", packager.REQUIRED_FILES)
        for helper in HELPERS:
            self.assertIn("bin/" + helper, packager.REQUIRED_FILES)
            self.assertIn("bin/" + helper, packager.EXECUTABLES)
        self.assertIn("deps/fzf", packager.EXECUTABLES)

    def test_release_layout_includes_module_git_support_and_source_bundle(self):
        for required in ("init.lua", "plugin.json", "scripts/git_support.py", "source.bundle"):
            self.assertIn(required, packager.REQUIRED_FILES)
        self.assertEqual(packager.SOURCE_BUNDLE, "source.bundle")
        self.assertNotIn("source.bundle", packager.EXECUTABLES)


if __name__ == "__main__":
    unittest.main()

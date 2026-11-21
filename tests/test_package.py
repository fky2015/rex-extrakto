"""Check reproducible release archives, checksums and publication gating."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("packager", ROOT / "scripts/package.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)
installer = sys.modules["install"]

VERSION = "v0.1.0"
EXECUTABLE_FILES = {"bin/rex-extract", "bin/rex-collect", "deps/fzf"}


def archive_files(version=VERSION, target=None, manifest=None):
    target = target or installer.host_target()
    manifest = manifest or {
        "schema_version": 1,
        "version": version,
        "target": target,
        "fzf_version": packager.FZF_VERSION,
    }
    return {
        "README.md": b"readme\n",
        "LICENSE": b"license\n",
        "extrakto.lua": b"-- plugin\n",
        "scripts/install.py": b"# installer\n",
        "scripts/install.sh": b"#!/bin/sh\n",
        "scripts/popup.py": b"# popup\n",
        "docs/configuration.md": b"# configuration\n",
        "docs/development.md": b"# development\n",
        "docs/installation.md": b"# installation\n",
        "bin/rex-extract": b"fixture extract\n",
        "bin/rex-collect": b"fixture collect\n",
        "deps/fzf": b"fixture fzf\n",
        "deps/LICENSE.fzf": b"fzf license\n",
        "licenses/README.txt": b"Rust dependency licenses\n",
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


def write_release(directory, target, version=VERSION, manifest=None, omit=(), modes=None, extra_members=()):
    path = directory / packager.archive_name(version, target)
    files = archive_files(version, target, manifest)
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

    def write_release_set(self, extra_members=(), omit=(), version=VERSION, manifest_for=None, modes=None):
        targets = sorted(packager.TARGETS.values())
        for index, target in enumerate(targets):
            manifest = manifest_for(target) if manifest_for else None
            write_release(self.dist, target, version=version, manifest=manifest, omit=omit, modes=modes,
                          extra_members=extra_members if index == len(targets) - 1 else ())

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
                return {"schema_version": 1, "version": VERSION, "target": targets[1],
                        "fzf_version": packager.FZF_VERSION}
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
            return {"schema_version": 2, "version": VERSION, "target": target,
                    "fzf_version": packager.FZF_VERSION}

        self.write_release_set(manifest_for=manifest_for)
        with self.assertRaisesRegex(ValueError, "schema"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_pinned_fzf_is_rejected(self):
        def manifest_for(target):
            return {"schema_version": 1, "version": VERSION, "target": target, "fzf_version": None}

        self.write_release_set(manifest_for=manifest_for)
        with self.assertRaisesRegex(ValueError, "pinned fzf"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_required_file_is_rejected(self):
        self.write_release_set(omit=("deps/fzf",))
        with self.assertRaisesRegex(ValueError, "missing deps/fzf"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_manifest_is_rejected(self):
        self.write_release_set(omit=("release.json",))
        with self.assertRaisesRegex(ValueError, "no unique manifest"):
            packager.verify_release(self.dist, VERSION)

    def test_missing_executable_bit_is_rejected(self):
        self.write_release_set(modes={"deps/fzf": 0o644})
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


if __name__ == "__main__":
    unittest.main()

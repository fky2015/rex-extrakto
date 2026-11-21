"""Check offline installation, immutable releases and configuration preservation."""
import argparse
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
spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class RedirectResponse:
    def __init__(self, url):
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self.url


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex install tests ")
        self.root = Path(self.temp.name)
        self.install_dir = self.root / "installed plugin"
        self.config = self.root / "config/init.lua"
        self.rex = self.root / "fake rex"
        self.rex.write_text("#!" + sys.executable + '\nimport json,sys\nprint(json.dumps({"diagnostics": []}))\n')
        self.rex.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, version="v0.1.0", target=None, extra=None, plugin=b"-- fixture plugin\n", fzf_version=None):
        target = target or installer.host_target()
        name = installer.archive_name(version, target)
        prefix = name[:-len(".tar.gz")]
        manifest = {"schema_version": 1, "version": version, "target": target}
        if fzf_version:
            manifest["fzf_version"] = fzf_version
        files = {
            "release.json": json.dumps(manifest).encode(),
            "extrakto.lua": plugin,
            "scripts/popup.py": b"# fixture popup\n",
        }
        for helper in ["rex-extract", "rex-collect"]:
            files["bin/" + helper] = ('#!/bin/sh\nprintf "%s\\n" "' + helper + " " + version[1:] + '"\n').encode()
        path = self.root / name
        with tarfile.open(path, "w:gz") as archive:
            for relative, content in files.items():
                member = tarfile.TarInfo(prefix + "/" + relative)
                member.size = len(content)
                member.mode = 0o755 if relative.startswith("bin/") else 0o644
                archive.addfile(member, io.BytesIO(content))
            if extra:
                member, content = extra
                archive.addfile(member, io.BytesIO(content) if content else None)
        checksum = self.root / "SHA256SUMS"
        checksum.write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n")
        return path, checksum

    def args(self, archive, checksum, **options):
        defaults = dict(version="latest", repository=installer.REPOSITORY, install_dir=self.install_dir,
                        config=self.config, archive=archive, checksum_file=checksum,
                        no_config=False, no_reload=True, no_deps=True)
        defaults.update(options)
        return argparse.Namespace(**defaults)

    def install(self, archive, checksum, **options):
        with mock.patch.object(installer, "find_rex", return_value=str(self.rex)), mock.patch.object(installer, "open_url", side_effect=AssertionError("offline install accessed the network")):
            installer.install(self.args(archive, checksum, **options))

    def test_first_install_is_idempotent_and_loads_versioned_files(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        text = self.config.read_text()
        self.assertIn("scope = \"tab\"", text)
        self.assertEqual(text.count(installer.BEGIN), 1)
        self.assertIn("releases/v0.1.0/extrakto.lua", text)
        self.assertEqual((self.install_dir / "current").resolve(), (self.install_dir / "releases/v0.1.0").resolve())
        self.install(archive, checksum)
        self.assertEqual(self.config.read_text(), text)

    def test_upgrade_preserves_custom_bindings_and_old_release(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        original = self.config.read_text().replace('scope = "tab"', 'scope = "session", jobs = 2')
        original += 'rex.bind("cmd+j", "another_action")\n'
        self.config.write_text(original)
        archive, checksum = self.archive("v0.2.0")
        self.install(archive, checksum)
        updated = self.config.read_text()
        self.assertIn('scope = "session", jobs = 2', updated)
        self.assertIn('rex.bind("cmd+j", "another_action")', updated)
        self.assertIn("releases/v0.2.0/extrakto.lua", updated)
        self.assertTrue((self.install_dir / "releases/v0.1.0/bin/rex-collect").exists())

    def test_manual_loader_migrates_without_changing_preferences(self):
        self.config.parent.mkdir()
        original = 'dofile("/old project/extrakto.lua")\nrex.bind("cmd+shift+f", "rex_extrakto", { scope = "pane", jobs = 1 })\n'
        self.config.write_text(original)
        archive, checksum = self.archive()
        self.install(archive, checksum)
        self.assertNotIn("/old project/", self.config.read_text())
        self.assertIn('scope = "pane", jobs = 1', self.config.read_text())
        self.assertEqual(self.config.with_name("init.lua.rex-extrakto.bak").read_text(), original)

    def test_existing_shortcut_is_not_overwritten(self):
        text = 'rex.bind("cmd+shift+f", "other_feature")\n'
        updated = installer.configured_text(text, Path("/installed/extrakto.lua"))
        self.assertEqual(updated.count('"cmd+shift+f"'), 1)
        self.assertIn("other_feature", updated)

    def test_checksum_failure_leaves_config_unchanged(self):
        archive, checksum = self.archive()
        self.config.parent.mkdir()
        self.config.write_text("-- existing config\n")
        checksum.write_text("0" * 64 + "  " + archive.name + "\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.install(archive, checksum)
        self.assertEqual(self.config.read_text(), "-- existing config\n")

    def test_traversal_and_links_are_rejected(self):
        traversal = tarfile.TarInfo("../outside")
        traversal.size = 1
        symbolic = tarfile.TarInfo("root/link")
        symbolic.type = tarfile.SYMTYPE
        symbolic.linkname = "../../outside"
        for member, content in [(traversal, b"x"), (symbolic, b"")]:
            with self.subTest(member=member.name):
                archive, checksum = self.archive(extra=(member, content))
                with self.assertRaises(ValueError):
                    self.install(archive, checksum)
                self.assertFalse(self.config.exists())

    def test_wrong_platform_and_version_are_rejected(self):
        other = next(target for target in installer.TARGETS.values() if target != installer.host_target())
        archive, checksum = self.archive(target=other)
        with self.assertRaisesRegex(ValueError, "platform"):
            self.install(archive, checksum)
        archive, checksum = self.archive()
        with self.assertRaisesRegex(ValueError, "requested version"):
            self.install(archive, checksum, version="v0.2.0")

    def test_failed_config_check_restores_original_and_keeps_current(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        original = self.config.read_text()
        old = os.readlink(self.install_dir / "current")
        self.rex.write_text('#!/bin/sh\nprintf "%s\\n" "bad config" >&2\nexit 1\n')
        self.rex.chmod(0o755)
        archive, checksum = self.archive("v0.2.0")
        with self.assertRaisesRegex(RuntimeError, "bad config"):
            self.install(archive, checksum)
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(os.readlink(self.install_dir / "current"), old)

    def test_incomplete_managed_loader_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            installer.configured_text(installer.BEGIN + "\n", Path("/installed/extrakto.lua"))

    def test_cli_installs_without_rex_or_network(self):
        archive, checksum = self.archive()
        process = subprocess.run([sys.executable, str(ROOT / "scripts/install.py"),
                                  "--archive", str(archive), "--checksum-file", str(checksum),
                                  "--install-dir", str(self.install_dir), "--no-config", "--no-deps"],
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue((self.install_dir / "current/bin/rex-extract").exists())
        self.assertFalse(self.config.exists())

    def backups(self):
        releases = self.install_dir / "releases"
        return sorted(path for path in releases.iterdir() if path.name.startswith("v0.1.0.bak"))

    def test_same_version_install_is_repaired_with_backup(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        release = self.install_dir / "releases/v0.1.0"
        config = self.config.read_text()
        (release / "bin/rex-extract").unlink()
        (release / "extrakto.lua").write_text("-- modified plugin\n")
        (release / "bin/rex-collect").chmod(0o644)
        self.install(archive, checksum)
        self.assertEqual((release / "extrakto.lua").read_text(), "-- fixture plugin\n")
        self.assertEqual((release / "bin/rex-extract").read_text(),
                         '#!/bin/sh\nprintf "%s\\n" "rex-extract 0.1.0"\n')
        self.assertTrue(os.access(release / "bin/rex-collect", os.X_OK))
        self.assertEqual(self.config.read_text(), config)
        self.assertEqual((self.install_dir / "current").resolve(), release.resolve())
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "extrakto.lua").read_text(), "-- modified plugin\n")
        self.assertFalse((backups[0] / "bin/rex-extract").exists())
        self.install(archive, checksum)
        self.assertEqual(len(self.backups()), 1)
        (release / "scripts/popup.py").write_text("# broken again\n")
        self.install(archive, checksum)
        backups = self.backups()
        self.assertEqual(len(backups), 2)
        self.assertEqual((backups[1] / "scripts/popup.py").read_text(), "# broken again\n")

    def test_same_version_with_different_archive_is_rejected(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        config = self.config.read_text()
        other, other_checksum = self.archive(plugin=b"-- rebuilt plugin\n")
        with self.assertRaisesRegex(RuntimeError, "different content"):
            self.install(other, other_checksum)
        self.assertEqual(self.config.read_text(), config)
        self.assertEqual((self.install_dir / "releases/v0.1.0/extrakto.lua").read_text(), "-- fixture plugin\n")
        self.assertEqual(self.backups(), [])

    def test_missing_release_marker_is_restored_without_repair(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        release = self.install_dir / "releases/v0.1.0"
        (release / ".archive-sha256").unlink()
        self.install(archive, checksum)
        self.assertEqual((release / ".archive-sha256").read_text().strip(), hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(self.backups(), [])

    def test_default_keys_respect_case_and_modifier_order(self):
        original = ('rex.bind("Shift+Cmd+F", "custom_action")\n'
                    'rex.bind("shift+alt+cmd+e", "second_action")\n')
        updated = installer.configured_text(original, Path("/installed/extrakto.lua"))
        self.assertNotIn('rex.bind("cmd+shift+f"', updated)
        self.assertNotIn('rex.bind("alt+cmd+shift+e"', updated)
        self.assertEqual(updated.count('"custom_action"'), 1)
        self.assertEqual(updated.count('"second_action"'), 1)
        self.assertEqual(installer.configured_text(updated, Path("/installed/extrakto.lua")), updated)

    def test_reversed_managed_loader_is_rejected(self):
        text = installer.END + "\n" + installer.BEGIN + "\n"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            installer.configured_text(text, Path("/installed/extrakto.lua"))

    def test_manifest_requiring_fzf_without_bundled_fzf_is_rejected(self):
        archive, checksum = self.archive(fzf_version="v0.75.0")
        with self.assertRaisesRegex(ValueError, "deps/fzf"):
            self.install(archive, checksum)
        self.assertFalse(self.config.exists())

    def test_activation_failure_leaves_config_untouched(self):
        archive, checksum = self.archive()
        with mock.patch.object(installer, "activate", side_effect=RuntimeError("activation failed")):
            with self.assertRaisesRegex(RuntimeError, "activation failed"):
                self.install(archive, checksum)
        self.assertFalse(self.config.exists())

    def test_activation_failure_keeps_existing_config_and_current(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        config = self.config.read_text()
        old = os.readlink(self.install_dir / "current")
        archive, checksum = self.archive("v0.2.0")
        with mock.patch.object(installer, "activate", side_effect=RuntimeError("activation failed")):
            with self.assertRaisesRegex(RuntimeError, "activation failed"):
                self.install(archive, checksum)
        self.assertEqual(self.config.read_text(), config)
        self.assertEqual(os.readlink(self.install_dir / "current"), old)

    def test_failed_config_reload_restores_original_and_keeps_current(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        original = self.config.read_text()
        old = os.readlink(self.install_dir / "current")
        self.rex.write_text("#!" + sys.executable + "\nimport json, sys\n"
                            "if 'reload' in sys.argv:\n"
                            "    print('reload failed', file=sys.stderr)\n"
                            "    sys.exit(1)\n"
                            "print(json.dumps({'diagnostics': []}))\n")
        self.rex.chmod(0o755)
        archive, checksum = self.archive("v0.2.0")
        with self.assertRaisesRegex(RuntimeError, "reload failed"):
            self.install(archive, checksum, no_reload=False)
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(os.readlink(self.install_dir / "current"), old)

    def test_failed_first_config_check_removes_current(self):
        archive, checksum = self.archive()
        self.rex.write_text('#!/bin/sh\nprintf "%s\\n" "bad config" >&2\nexit 1\n')
        self.rex.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "bad config"):
            self.install(archive, checksum)
        self.assertFalse((self.install_dir / "current").exists())
        self.assertFalse((self.install_dir / "current").is_symlink())
        self.assertFalse(self.config.exists())
        self.assertTrue((self.install_dir / "releases/v0.1.0/bin/rex-extract").exists())

    def test_reload_diagnostics_restore_config_and_reload_previous(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        original = self.config.read_text()
        old = os.readlink(self.install_dir / "current")
        calls = self.root / "rex-calls.log"
        self.rex.write_text("#!" + sys.executable + "\nimport json, sys\n"
                            "with open(" + repr(str(calls)) + ", 'a') as log:\n"
                            "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
                            "if 'reload' in sys.argv:\n"
                            "    print(json.dumps({'diagnostics': ['new config broken']}))\n"
                            "else:\n"
                            "    print(json.dumps({'diagnostics': []}))\n")
        self.rex.chmod(0o755)
        archive, checksum = self.archive("v0.2.0")
        with self.assertRaisesRegex(RuntimeError, "reload returned diagnostics"):
            self.install(archive, checksum, no_reload=False)
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(os.readlink(self.install_dir / "current"), old)
        self.assertEqual(sum("reload" in line for line in calls.read_text().splitlines()), 2)

    def test_symlinked_config_is_updated_in_place(self):
        target = self.root / "dotfiles/rex.lua"
        target.parent.mkdir()
        original = 'dofile("/old project/extrakto.lua")\nrex.bind("cmd+shift+f", "rex_extrakto", { scope = "session" })\n'
        target.write_text(original)
        self.config.parent.mkdir()
        self.config.symlink_to(target)
        archive, checksum = self.archive()
        self.install(archive, checksum)
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(self.config.resolve(), target.resolve())
        self.assertNotIn("/old project/", target.read_text())
        self.assertIn('scope = "session"', target.read_text())
        self.assertEqual(self.config.with_name("init.lua.rex-extrakto.bak").read_text(), original)


    def test_latest_release_metadata_follows_github_redirect(self):
        calls = []

        def fake_open(url):
            calls.append(url)
            return RedirectResponse("https://github.com/fky2015/rex-extrakto/releases/tag/v0.2.0")

        with mock.patch.object(installer, "open_url", side_effect=fake_open):
            release = installer.release_metadata("fky2015/rex-extrakto", "latest")
        self.assertEqual(calls, ["https://github.com/fky2015/rex-extrakto/releases/latest"])
        self.assertEqual(release["tag_name"], "v0.2.0")
        expected = {installer.archive_name("v0.2.0", target) for target in installer.TARGETS.values()} | {"SHA256SUMS"}
        self.assertEqual({asset["name"] for asset in release["assets"]}, expected)
        for asset in release["assets"]:
            self.assertEqual(asset["browser_download_url"],
                             "https://github.com/fky2015/rex-extrakto/releases/download/v0.2.0/" + asset["name"])

    def test_latest_release_metadata_rejects_untrusted_redirects(self):
        redirects = [
            "http://github.com/fky2015/rex-extrakto/releases/tag/v1.0.0",
            "https://github.com.evil.example/fky2015/rex-extrakto/releases/tag/v1.0.0",
            "https://evil.example/github.com/fky2015/rex-extrakto/releases/tag/v1.0.0",
            "https://github.com@evil.example/fky2015/rex-extrakto/releases/tag/v1.0.0",
            "https://github.com/other/repo/releases/tag/v1.0.0",
            "https://github.com/fky2015/rex-extrakto/releases/tag/v1.0.0/extra",
            "https://github.com/fky2015/rex-extrakto/releases/tag/not-a-version",
            "https://github.com/fky2015/rex-extrakto/releases/tag/v1.0.0?next=evil",
            "https://github.com/fky2015/rex-extrakto/releases/tag/v1.0.0#fragment",
            "https://github.com/fky2015/rex-extrakto/releases/latest",
        ]
        for final in redirects:
            with self.subTest(final=final):
                with mock.patch.object(installer, "open_url", return_value=RedirectResponse(final)):
                    with self.assertRaisesRegex(RuntimeError, "redirect"):
                        installer.release_metadata("fky2015/rex-extrakto", "latest")

    def test_release_metadata_rejects_invalid_repository(self):
        for repository in ["fky2015", "fky2015/rex-extrakto/extra", "../evil", "owner/..",
                           "owner/repo with space", ""]:
            with self.subTest(repository=repository):
                with mock.patch.object(installer, "open_url", side_effect=AssertionError("unexpected network")):
                    with self.assertRaises(ValueError):
                        installer.release_metadata(repository, "v0.1.0")

    def test_fixed_version_metadata_needs_no_network(self):
        with mock.patch.object(installer, "open_url", side_effect=AssertionError("unexpected network")):
            release = installer.release_metadata("fky2015/rex-extrakto", "v0.1.0")
        self.assertEqual(release["tag_name"], "v0.1.0")
        base = "https://github.com/fky2015/rex-extrakto/releases/download/v0.1.0/"
        for target in installer.TARGETS.values():
            name = installer.archive_name("v0.1.0", target)
            self.assertIn({"name": name, "browser_download_url": base + name}, release["assets"])
        self.assertIn({"name": "SHA256SUMS", "browser_download_url": base + "SHA256SUMS"}, release["assets"])

    def test_online_fixed_version_install_uses_direct_release_urls(self):
        archive, checksum = self.archive()
        base = "https://github.com/fky2015/rex-extrakto/releases/download/v0.1.0/"
        archive_url, sums_url = base + archive.name, base + "SHA256SUMS"
        calls = []

        def fake_open(url):
            calls.append(url)
            if url == archive_url:
                return io.BytesIO(archive.read_bytes())
            if url == sums_url:
                return io.BytesIO(checksum.read_bytes())
            raise AssertionError("unexpected URL: " + url)

        with mock.patch.object(installer, "find_rex", return_value=None), \
                mock.patch.object(installer, "open_url", side_effect=fake_open):
            installer.install(self.args(None, None, version="v0.1.0", no_config=True, no_deps=True))
        self.assertEqual(calls, [archive_url, sums_url])
        self.assertTrue((self.install_dir / "current/bin/rex-extract").exists())

    def test_online_latest_install_follows_redirect_without_api(self):
        archive, checksum = self.archive()
        latest = "https://github.com/fky2015/rex-extrakto/releases/latest"
        base = "https://github.com/fky2015/rex-extrakto/releases/download/v0.1.0/"
        archive_url, sums_url = base + archive.name, base + "SHA256SUMS"
        calls = []

        def fake_open(url):
            calls.append(url)
            if url == latest:
                return RedirectResponse("https://github.com/fky2015/rex-extrakto/releases/tag/v0.1.0")
            if url == archive_url:
                return io.BytesIO(archive.read_bytes())
            if url == sums_url:
                return io.BytesIO(checksum.read_bytes())
            raise AssertionError("unexpected URL: " + url)

        with mock.patch.object(installer, "find_rex", return_value=None), \
                mock.patch.object(installer, "open_url", side_effect=fake_open):
            installer.install(self.args(None, None, version="latest", no_config=True, no_deps=True))
        self.assertEqual(calls, [latest, archive_url, sums_url])
        self.assertTrue((self.install_dir / "current/bin/rex-extract").exists())


if __name__ == "__main__":
    unittest.main()

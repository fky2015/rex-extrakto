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

    def archive(self, version="v0.1.0", target=None, extra=None, plugin=b"-- fixture plugin\n",
                fzf_version=None, picker=None, helpers=None, popup=None, binaries=None,
                versions=None, modes=None, extra_files=None, metadata=None):
        target = target or installer.host_target()
        name = installer.archive_name(version, target)
        prefix = name[:-len(".tar.gz")]
        manifest = {"schema_version": 1, "version": version, "target": target}
        manifest.update(metadata or {})
        if fzf_version:
            manifest["fzf_version"] = fzf_version
        if picker is not None:
            manifest["picker"] = picker
        if helpers is not None:
            manifest["helpers"] = helpers
        files = {
            "release.json": json.dumps(manifest).encode(),
            "extrakto.lua": plugin,
        }
        files.update(extra_files or {})
        if popup is None:
            popup = picker != "rust"
        if popup:
            files["scripts/popup.py"] = b"# fixture popup\n"
        if binaries is None:
            binaries = (["rex-extract", "rex-collect", "rex-popup"] if picker == "rust"
                        else ["rex-extract", "rex-collect"])
        for helper in binaries:
            reported = (versions or {}).get(helper, version[1:])
            files["bin/" + helper] = ('#!/bin/sh\nprintf "%s\\n" "' + helper + " " + reported + '"\n').encode()
        modes = modes or {}
        path = self.root / name
        with tarfile.open(path, "w:gz") as archive:
            for relative, content in files.items():
                member = tarfile.TarInfo(prefix + "/" + relative)
                member.size = len(content)
                member.mode = modes.get(relative, 0o755 if relative.startswith("bin/") else 0o644)
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

    def plugin_entry(self):
        entry = self.root / "plugin/extrakto.lua"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("-- fixture plugin\n")
        return entry

    def generated_text(self, original):
        entry = self.plugin_entry()
        text = installer.configured_text(original, entry)
        self.assertEqual(text.count(installer.SHORTCUT_BEGIN), 1)
        self.assertEqual(text.count(installer.SHORTCUT_END), 1)
        self.assertLess(text.index(installer.SHORTCUT_BEGIN), text.index(installer.SHORTCUT_END))
        self.assertEqual(installer.configured_text(text, entry), text)
        return text

    @staticmethod
    def bindings_probe(path):
        return (
            'local probe = assert(io.open(' + json.dumps(str(path)) + ', "w"))\n'
            'for _, binding in ipairs(rex.bindings()) do\n'
            '  probe:write(tostring(binding.key or ""), "\\t", tostring(binding.mode or ""), "\\t",\n'
            '              tostring(binding.action or ""), "\\t", tostring(binding.fn ~= nil), "\\n")\n'
            'end\n'
            'probe:close()\n'
        )

    @staticmethod
    def parse_bindings(content):
        bindings = []
        for line in content.splitlines():
            key, mode, action, fn = line.split("\t")
            bindings.append({"key": key, "mode": mode, "action": action, "fn": fn == "true"})
        return bindings

    @staticmethod
    def normalize_key(key):
        return "+".join(sorted(part.strip().lower() for part in key.split("+")))

    def global_bindings(self, bindings, key):
        return [binding for binding in bindings
                if binding["mode"] == "" and self.normalize_key(binding["key"]) == self.normalize_key(key)]

    def global_actions(self, bindings, key):
        return [binding["action"] for binding in self.global_bindings(bindings, key)]

    @staticmethod
    def has_action(bindings, action):
        return any(binding["action"] == action for binding in bindings)

    def effective_bindings(self, original):
        text = self.generated_text(original)
        rex = installer.find_rex()
        if not rex:
            return None
        probe = self.root / "effective/bindings.txt"
        config = self.root / "effective/init.lua"
        config.parent.mkdir(parents=True, exist_ok=True)
        probe.unlink(missing_ok=True)
        config.write_text(text + "\n" + self.bindings_probe(probe) + "\n")
        home = self.root / "home"
        xdg = self.root / "xdg"
        home.mkdir(exist_ok=True)
        xdg.mkdir(exist_ok=True)
        environment = {key: value for key, value in os.environ.items() if key != "REX_CONFIG"}
        environment["HOME"] = str(home)
        environment["XDG_CONFIG_HOME"] = str(xdg)
        result = subprocess.run([rex, "config", "check", str(config), "--json"],
                                capture_output=True, text=True, timeout=30, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["diagnostics"], [])
        self.assertTrue(probe.is_file(), "Rex did not write binding metadata")
        return self.parse_bindings(probe.read_text())

    def require_effective_bindings(self, original):
        bindings = self.effective_bindings(original)
        if bindings is None:
            self.skipTest("Rex CLI is not installed; managed text and idempotence were checked")
        return bindings

    def test_first_install_is_idempotent_and_loads_versioned_files(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        text = self.config.read_text()
        self.assertIn("scope = \"tab\"", text)
        self.assertEqual(text.count(installer.BEGIN), 1)
        self.assertEqual(text.count(installer.SHORTCUT_BEGIN), 1)
        self.assertTrue(text.rstrip().endswith(installer.SHORTCUT_END))
        self.assertIn("releases/v0.1.0/extrakto.lua", text)
        self.assertEqual((self.install_dir / "current").resolve(), (self.install_dir / "releases/v0.1.0").resolve())
        self.install(archive, checksum)
        self.assertEqual(self.config.read_text(), text)

    def test_upgrade_preserves_custom_bindings_and_old_release(self):
        archive, checksum = self.archive()
        self.install(archive, checksum)
        text = self.config.read_text()
        original = text.replace(installer.SHORTCUT_BEGIN,
                                'rex.bind("cmd+j", "another_action")\n\n' + installer.SHORTCUT_BEGIN)
        self.assertNotEqual(original, text)
        self.config.write_text(original)
        archive, checksum = self.archive("v0.2.0")
        self.install(archive, checksum)
        updated = self.config.read_text()
        self.assertIn('rex.bind("cmd+j", "another_action")', updated)
        self.assertIn("releases/v0.2.0/extrakto.lua", updated)
        self.assertTrue(updated.rstrip().endswith(installer.SHORTCUT_END))
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
        original = 'rex.bind("cmd+shift+f", "other_feature")\n'
        updated = self.generated_text(original)
        self.assertIn('rex.bind("cmd+shift+f", "other_feature")', updated)
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["other_feature"])
        self.assertFalse(self.has_action(bindings, "rex_extrakto"))
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

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
        self.generated_text(original)
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["custom_action"])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["second_action"])
        self.assertFalse(self.has_action(bindings, "rex_extrakto"))
        self.assertFalse(self.has_action(bindings, "rex_extrakto_lines"))

    def test_callback_bindings_occupy_keys(self):
        original = ('rex.bind("Shift+Cmd+F", function(ctx) return true, false end)\n'
                    'rex.bind("Alt+Cmd+Shift+E", function() return false end)\n')
        bindings = self.require_effective_bindings(original)
        for key in ["cmd+shift+f", "alt+cmd+shift+e"]:
            with self.subTest(key=key):
                global_bindings = self.global_bindings(bindings, key)
                self.assertEqual(len(global_bindings), 1)
                self.assertTrue(global_bindings[0]["fn"])
        self.assertFalse(self.has_action(bindings, "rex_extrakto"))
        self.assertFalse(self.has_action(bindings, "rex_extrakto_lines"))

    def test_callback_on_another_key_does_not_suppress_defaults(self):
        original = 'rex.bind("cmd+j", function() return true end)\n'
        bindings = self.require_effective_bindings(original)
        self.assertTrue(self.global_bindings(bindings, "cmd+j")[0]["fn"])
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["rex_extrakto"])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

    def test_variable_key_and_action_bindings_are_resolved(self):
        original = ('local key = "Shift+Cmd+F"\n'
                    'local action = "rex_" .. "extrakto"\n'
                    'rex.bind(key, action, { scope = "pane" })\n')
        updated = self.generated_text(original)
        self.assertIn('local action = "rex_" .. "extrakto"', updated)
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["rex_extrakto"])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

    def test_variable_action_on_another_key_avoids_default(self):
        original = 'local action = "rex_extrakto"\nrex.bind("cmd+j", action)\n'
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+j"), ["rex_extrakto"])
        self.assertEqual(self.global_bindings(bindings, "cmd+shift+f"), [])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

    def test_reassigned_key_uses_its_final_value(self):
        original = ('local key = "cmd+shift+f"\n'
                    'key = "cmd+j"\n'
                    'rex.bind(key, "custom_action")\n')
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+j"), ["custom_action"])
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["rex_extrakto"])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

    def test_action_binding_on_another_key_avoids_default(self):
        original = 'rex.bind("cmd+j", "rex_extrakto")\n'
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+j"), ["rex_extrakto"])
        self.assertEqual(self.global_bindings(bindings, "cmd+shift+f"), [])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

    def test_commented_bindings_are_ignored(self):
        original = ('-- rex.bind("cmd+shift+f", "commented_action")\n'
                    '--[==[ rex.bind("alt+cmd+shift+e", "long_commented_action") ]==]\n')
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["rex_extrakto"])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])
        self.assertFalse(self.has_action(bindings, "commented_action"))
        self.assertFalse(self.has_action(bindings, "long_commented_action"))

    def test_bind_lookalikes_in_strings_are_ignored(self):
        original = ('local quoted = "rex.bind(\'cmd+shift+f\', \'rex_extrakto\')"\n'
                    'local long = [=[ rex.bind("alt+cmd+shift+e", "rex_extrakto_lines") ]=]\n')
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["rex_extrakto"])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

    def test_single_quoted_existing_bindings_are_preserved(self):
        original = "rex.bind('cmd+shift+f', 'rex_extrakto')\n"
        updated = self.generated_text(original)
        self.assertIn("rex.bind('cmd+shift+f', 'rex_extrakto')", updated)
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["rex_extrakto"])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

    def test_unrelated_rex_bind_lookalikes_are_ignored(self):
        original = ('local myrex = { bind = function() end }\n'
                    'myrex.bind("cmd+shift+f", "rex_extrakto")\n'
                    'local helper = { rex = { bind = function() end } }\n'
                    'helper.rex.bind("alt+cmd+shift+e", "rex_extrakto_lines")\n')
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["rex_extrakto"])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

    def test_conditional_bindings_are_evaluated_at_runtime(self):
        cases = {
            "false branch": ('if false then\n  rex.bind("cmd+shift+f", "custom_action")\nend\n',
                             "rex_extrakto"),
            "true branch": ('if true then\n  rex.bind("cmd+shift+f", "custom_action")\nend\n',
                            "custom_action"),
        }
        for label, (original, expected) in cases.items():
            with self.subTest(case=label):
                bindings = self.require_effective_bindings(original)
                self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), [expected])

    def test_mode_only_bindings_do_not_suppress_global_defaults(self):
        original = 'rex.mode("resize")\nrex.bind("resize/cmd+shift+f", "pane.zoom")\n'
        bindings = self.require_effective_bindings(original)
        self.assertTrue(any(binding["mode"] == "resize" for binding in bindings))
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["rex_extrakto"])
        self.assertEqual(self.global_actions(bindings, "alt+cmd+shift+e"), ["rex_extrakto_lines"])

    def test_existing_unmanaged_defaults_are_not_duplicated(self):
        original = ('rex.bind("cmd+shift+f", "rex_extrakto", { scope = "pane" })\n'
                    'rex.bind("alt+cmd+shift+e", "rex_extrakto_lines")\n')
        updated = self.generated_text(original)
        self.assertIn('rex.bind("cmd+shift+f", "rex_extrakto", { scope = "pane" })', updated)
        bindings = self.require_effective_bindings(original)
        self.assertEqual(self.global_actions(bindings, "cmd+shift+f"), ["rex_extrakto"])
        self.assertEqual(len(self.global_bindings(bindings, "alt+cmd+shift+e")), 1)

    def test_managed_shortcuts_block_is_replaced_not_duplicated(self):
        entry = self.plugin_entry()
        original = 'rex.bind("cmd+j", "custom_action")\n'
        first = installer.configured_text(original, entry)
        edited = first.replace('{ scope = "tab" }', '{ scope = "session" }')
        self.assertNotEqual(edited, first)
        self.assertEqual(installer.configured_text(edited, entry), first)
        self.assertEqual(installer.configured_text(first, entry), first)

    def test_managed_shortcuts_stay_last_after_user_edits(self):
        entry = self.plugin_entry()
        first = installer.configured_text('rex.bind("cmd+j", "custom_action")\n', entry)
        self.assertLess(first.index(installer.BEGIN), first.index(installer.SHORTCUT_BEGIN))
        self.assertTrue(first.rstrip().endswith(installer.SHORTCUT_END))
        appended = first + 'rex.bind("cmd+k", "later_action")\n'
        updated = installer.configured_text(appended, entry)
        self.assertIn('rex.bind("cmd+k", "later_action")', updated)
        self.assertEqual(updated.count(installer.SHORTCUT_BEGIN), 1)
        self.assertTrue(updated.rstrip().endswith(installer.SHORTCUT_END))

    def test_incomplete_managed_shortcuts_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "incomplete or duplicate managed shortcuts"):
            installer.configured_text(installer.SHORTCUT_BEGIN + "\n", Path("/installed/extrakto.lua"))

    def test_reversed_managed_shortcuts_is_rejected(self):
        text = installer.SHORTCUT_END + "\n" + installer.SHORTCUT_BEGIN + "\n"
        with self.assertRaisesRegex(ValueError, "incomplete or duplicate managed shortcuts"):
            installer.configured_text(text, Path("/installed/extrakto.lua"))

    def test_duplicate_managed_shortcuts_is_rejected(self):
        block = installer.SHORTCUT_BEGIN + "\n" + installer.SHORTCUT_END + "\n"
        with self.assertRaisesRegex(ValueError, "incomplete or duplicate managed shortcuts"):
            installer.configured_text(block + block, Path("/installed/extrakto.lua"))

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

    def test_rust_release_installs_with_three_helpers_and_no_python_popup(self):
        archive, checksum = self.archive(picker="rust", helpers=["rex-popup", "rex-extract", "rex-collect"])
        self.install(archive, checksum)
        release = self.install_dir / "releases/v0.1.0"
        self.assertEqual((self.install_dir / "current").resolve(), release.resolve())
        for helper in ["rex-extract", "rex-collect", "rex-popup"]:
            self.assertTrue(os.access(release / "bin" / helper, os.X_OK))
        self.assertFalse((release / "scripts/popup.py").exists())
        self.assertIn("releases/v0.1.0/extrakto.lua", self.config.read_text())

    def test_rust_release_requires_popup_helper(self):
        archive, checksum = self.archive(picker="rust", binaries=["rex-extract", "rex-collect"])
        with self.assertRaisesRegex(ValueError, "bin/rex-popup"):
            self.install(archive, checksum)
        self.assertFalse(self.config.exists())
        self.assertFalse((self.install_dir / "current").exists())

    def test_rust_popup_version_mismatch_is_rejected(self):
        archive, checksum = self.archive(picker="rust", versions={"rex-popup": "9.9.9"})
        with self.assertRaisesRegex(ValueError, "version mismatch: rex-popup"):
            self.install(archive, checksum)
        self.assertFalse(self.config.exists())

    def test_rust_release_requires_executable_helpers(self):
        archive, checksum = self.archive(picker="rust", modes={"bin/rex-popup": 0o644})
        with self.assertRaisesRegex(ValueError, "not executable: bin/rex-popup"):
            self.install(archive, checksum)
        self.assertFalse(self.config.exists())

    def test_legacy_release_without_picker_defaults_to_python(self):
        self.assertEqual(installer.release_picker({}), "python")
        self.assertEqual(installer.release_helpers({}, "python"), {"rex-extract", "rex-collect"})
        archive, checksum = self.archive()
        self.install(archive, checksum)
        release = self.install_dir / "releases/v0.1.0"
        self.assertTrue((release / "scripts/popup.py").is_file())
        self.assertFalse((release / "bin/rex-popup").exists())
        self.assertEqual((self.install_dir / "current").resolve(), release.resolve())

    def test_legacy_release_with_declared_python_helpers_is_accepted(self):
        archive, checksum = self.archive(helpers=["rex-collect", "rex-extract"])
        self.install(archive, checksum)
        self.assertTrue((self.install_dir / "releases/v0.1.0/scripts/popup.py").is_file())

    def test_legacy_release_requires_python_popup(self):
        archive, checksum = self.archive(popup=False)
        with self.assertRaisesRegex(ValueError, "scripts/popup.py"):
            self.install(archive, checksum)
        self.assertFalse(self.config.exists())

    def test_invalid_picker_is_rejected_before_any_helper_runs(self):
        for picker in ["go", "Python", "", 1, True]:
            with self.subTest(picker=picker):
                archive, checksum = self.archive(picker=picker)
                with mock.patch.object(installer.subprocess, "check_output",
                                       side_effect=AssertionError("helper executed")):
                    with self.assertRaisesRegex(ValueError, "picker"):
                        self.install(archive, checksum)
                self.assertFalse(self.config.exists())

    def test_invalid_helper_lists_are_rejected(self):
        cases = {
            "duplicate": ["rex-extract", "rex-extract"],
            "extra helper": ["rex-extract", "rex-collect", "rex-popup"],
            "missing helper": ["rex-extract"],
            "empty": [],
            "illegal path": ["rex-extract", "../evil"],
            "nested path": ["rex-extract", "nested/rex-collect"],
            "not a list": "rex-extract",
            "not strings": ["rex-extract", 1],
        }
        for label, helpers in cases.items():
            with self.subTest(case=label):
                archive, checksum = self.archive(helpers=helpers)
                with mock.patch.object(installer.subprocess, "check_output",
                                       side_effect=AssertionError("helper executed")):
                    with self.assertRaises(ValueError):
                        self.install(archive, checksum)
                self.assertFalse(self.config.exists())

    def test_helpers_must_match_the_declared_picker(self):
        with self.assertRaisesRegex(ValueError, "do not match the rust picker"):
            installer.release_helpers({"helpers": ["rex-extract", "rex-collect"]}, "rust")
        with self.assertRaisesRegex(ValueError, "do not match the python picker"):
            installer.release_helpers({"helpers": ["rex-extract", "rex-collect", "rex-popup"]}, "python")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            installer.release_helpers({"helpers": ["rex-extract", "rex-extract", "rex-collect"]}, "python")

    def test_declared_helper_names_are_never_executed(self):
        archive, checksum = self.archive(helpers=["rex-extract", "rex-collect", "evil"])
        with mock.patch.object(installer.subprocess, "check_output",
                               side_effect=AssertionError("helper executed")):
            with self.assertRaises(ValueError):
                self.install(archive, checksum)
        self.assertFalse((self.install_dir / "current").exists())
        self.assertFalse(self.config.exists())

    def default_config(self, **environment):
        with mock.patch.dict(os.environ, environment, clear=True):
            return installer.parser().parse_args([]).config

    def test_parser_prefers_non_empty_rex_config(self):
        config = self.default_config(REX_CONFIG="/tmp/isolated rex/config.lua",
                                     XDG_CONFIG_HOME="/tmp/xdg", HOME="/tmp/home")
        self.assertEqual(config, Path("/tmp/isolated rex/config.lua"))

    def test_parser_empty_rex_config_falls_back_to_xdg(self):
        config = self.default_config(REX_CONFIG="", XDG_CONFIG_HOME="/tmp/xdg", HOME="/tmp/home")
        self.assertEqual(config, Path("/tmp/xdg/rex/init.lua"))

    def test_parser_without_environment_uses_home_config(self):
        config = self.default_config(HOME="/tmp/home")
        self.assertEqual(config, Path("/tmp/home/.config/rex/init.lua"))

    def test_parser_empty_xdg_config_home_uses_home_config(self):
        config = self.default_config(REX_CONFIG="", XDG_CONFIG_HOME="", HOME="/tmp/home")
        self.assertEqual(config, Path("/tmp/home/.config/rex/init.lua"))

    def test_parser_explicit_config_overrides_environment(self):
        with mock.patch.dict(os.environ, {"REX_CONFIG": "/tmp/isolated rex/config.lua"}, clear=True):
            config = installer.parser().parse_args(["--config", "/tmp/explicit.lua"]).config
        self.assertEqual(config, Path("/tmp/explicit.lua"))

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


class ModuleInstallationTests(unittest.TestCase):
    archive = InstallationTests.archive
    args = InstallationTests.args
    install = InstallationTests.install

    def setUp(self):
        InstallationTests.setUp(self)
        self.state = self.root / "state"
        self.environment = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        self.environment.start()
        self.plugin = self.config.parent / "plugins/rex-extrakto"

    def tearDown(self):
        self.environment.stop()
        InstallationTests.tearDown(self)

    def module_archive(self, version="v0.1.0", source=False):
        files = {"init.lua": b"-- rex-extrakto module\nreturn {}\n"}
        metadata = {}
        if source:
            checkout = self.root / ("source-" + version)
            checkout.mkdir()
            (checkout / "init.lua").write_bytes(files["init.lua"])
            (checkout / "plugin.json").write_text(json.dumps({"schema_version": 1, "name": "rex-extrakto", "version": version}))
            self.git("init", str(checkout))
            self.git("add", ".", cwd=checkout)
            self.git("-c", "user.name=Installer Test", "-c", "user.email=installer@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "-m", "Module fixture", cwd=checkout)
            commit = self.git("rev-parse", "HEAD", cwd=checkout)
            bundle = self.root / (version + ".bundle")
            self.git("bundle", "create", str(bundle), "HEAD", cwd=checkout)
            files["source.bundle"] = bundle.read_bytes()
            files["scripts/git_support.py"] = (ROOT / "scripts/git_support.py").read_bytes()
            metadata = {"source_commit": commit, "source_ref": "HEAD", "source_bundle": "source.bundle"}
        return self.archive(version, picker="rust", extra_files=files, metadata=metadata)

    def git(self, *arguments, cwd=None):
        if not installer.shutil.which("git"):
            self.skipTest("Git is not installed")
        result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, *arguments],
                                cwd=cwd, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_release_module_uses_portable_require_and_immutable_registry(self):
        archive, checksum = self.module_archive()
        self.install(archive, checksum, mode="release")
        text = self.config.read_text()
        self.assertIn('require("plugins.rex-extrakto").setup({ bind = false })', text)
        self.assertIn('require("plugins.rex-extrakto").bind_defaults()', text)
        self.assertNotIn(str(self.install_dir), text)
        registry = json.loads(installer.installation_registry().read_text())
        self.assertEqual(registry["installations"][str(self.plugin)],
                         {"kind": "release", "version": "v0.1.0", "directory": str(self.install_dir / "releases/v0.1.0")})
        self.install(archive, checksum, mode="release")
        self.assertEqual(self.config.read_text(), text)

    def test_auto_without_bundle_installs_release_module(self):
        archive, checksum = self.module_archive()
        self.install(archive, checksum)
        self.assertTrue((self.plugin / "init.lua").is_file())
        self.assertFalse((self.plugin / ".git").exists())

    def test_module_install_migrates_legacy_loader_and_preserves_custom_bindings(self):
        self.config.parent.mkdir()
        self.config.write_text('dofile("/old path/extrakto.lua")\nrex.bind("cmd+shift+f", "rex_extrakto", { scope = "pane", jobs = 1 })\n')
        archive, checksum = self.module_archive()
        self.install(archive, checksum, mode="release")
        text = self.config.read_text()
        self.assertNotIn("/old path/", text)
        self.assertIn('scope = "pane", jobs = 1', text)
        self.assertLess(text.index('scope = "pane"'), text.index(installer.SHORTCUT_BEGIN))

    def test_user_owned_entry_is_preserved_and_current_is_restored(self):
        self.plugin.mkdir(parents=True)
        (self.plugin / "init.lua").write_text("-- my custom entry\n")
        archive, checksum = self.module_archive()
        with self.assertRaisesRegex(ValueError, "user-owned"):
            self.install(archive, checksum, mode="release")
        self.assertEqual((self.plugin / "init.lua").read_text(), "-- my custom entry\n")
        self.assertFalse((self.install_dir / "current").exists())
        self.assertFalse(installer.installation_registry().exists())
        self.assertFalse(self.config.exists())

    def test_failed_check_restores_module_registry_config_and_current(self):
        archive, checksum = self.module_archive()
        self.install(archive, checksum, mode="release")
        entry = (self.plugin / "init.lua").read_bytes()
        registry = installer.installation_registry().read_bytes()
        config = self.config.read_bytes()
        archive, checksum = self.module_archive("v0.2.0")
        with mock.patch.object(installer, "initialize_config", side_effect=RuntimeError("check failed")):
            with self.assertRaisesRegex(RuntimeError, "check failed"):
                self.install(archive, checksum, mode="release")
        self.assertEqual((self.plugin / "init.lua").read_bytes(), entry)
        self.assertEqual(installer.installation_registry().read_bytes(), registry)
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual((self.install_dir / "current").resolve(), (self.install_dir / "releases/v0.1.0").resolve())

    def test_first_failed_check_removes_module_and_registry(self):
        archive, checksum = self.module_archive()
        with mock.patch.object(installer, "initialize_config", side_effect=RuntimeError("check failed")):
            with self.assertRaisesRegex(RuntimeError, "check failed"):
                self.install(archive, checksum, mode="release")
        self.assertFalse((self.plugin / "init.lua").exists())
        self.assertFalse(installer.installation_registry().exists())
        self.assertFalse((self.install_dir / "current").exists())

    def test_registry_failure_restores_module_and_current(self):
        installer.atomic_write(installer.installation_registry(), '{"schema_version": 99}\n')
        archive, checksum = self.module_archive()
        with self.assertRaisesRegex(ValueError, "registry"):
            self.install(archive, checksum, mode="release")
        self.assertFalse((self.plugin / "init.lua").exists())
        self.assertFalse((self.install_dir / "current").exists())
        self.assertEqual(installer.installation_registry().read_text(), '{"schema_version": 99}\n')

    def test_plugin_directory_must_be_importable_under_config_directory(self):
        archive, checksum = self.module_archive()
        for location in [self.root / "outside", self.config.parent / "with.dot", self.config.parent]:
            with self.subTest(location=location):
                with self.assertRaisesRegex(ValueError, "plugin-dir"):
                    self.install(archive, checksum, plugin_dir=location)
        self.assertFalse(self.install_dir.exists())

    def test_auto_git_install_uses_bundled_source_and_matching_prebuilt_runtime(self):
        archive, checksum = self.module_archive(source=True)
        self.install(archive, checksum)
        self.assertTrue((self.plugin / ".git").is_dir())
        self.assertEqual(self.git("remote", "get-url", "origin", cwd=self.plugin),
                         "https://github.com/fky2015/rex-extrakto.git")
        self.assertFalse((self.plugin / "bin").exists())
        self.assertTrue((self.install_dir / "current/bin/rex-popup").is_file())
        first = self.git("rev-parse", "HEAD", cwd=self.plugin)
        self.install(archive, checksum)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.plugin), first)

    def test_failed_first_git_install_removes_checkout_and_registration(self):
        archive, checksum = self.module_archive(source=True)
        with mock.patch.object(installer, "initialize_config", side_effect=RuntimeError("check failed")):
            with self.assertRaisesRegex(RuntimeError, "check failed"):
                self.install(archive, checksum)
        self.assertFalse(self.plugin.exists())
        self.assertFalse(installer.installation_registry().exists())
        self.assertFalse((self.install_dir / "current").exists())

    def test_failed_git_upgrade_restores_previous_checkout_and_registry(self):
        archive, checksum = self.module_archive(source=True)
        self.install(archive, checksum)
        previous = self.git("rev-parse", "HEAD", cwd=self.plugin)
        registry = installer.installation_registry().read_bytes()
        archive, checksum = self.module_archive("v0.2.0", source=True)
        with mock.patch.object(installer, "initialize_config", side_effect=RuntimeError("check failed")):
            with self.assertRaisesRegex(RuntimeError, "check failed"):
                self.install(archive, checksum)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.plugin), previous)
        self.assertEqual(installer.installation_registry().read_bytes(), registry)
        self.assertEqual((self.install_dir / "current").resolve(), (self.install_dir / "releases/v0.1.0").resolve())

    def test_explicit_git_mode_rejects_legacy_release_without_changing_current(self):
        archive, checksum = self.archive()
        with self.assertRaisesRegex(ValueError, "source.bundle"):
            self.install(archive, checksum, mode="git")
        self.assertFalse((self.install_dir / "current").exists())


if __name__ == "__main__":
    unittest.main()

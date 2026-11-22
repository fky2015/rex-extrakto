"""Check the native Rex module entry: runtime resolution, validation and bindings."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "init.lua"


def find_rex():
    """Locate the Rex CLI without importing installer helpers."""
    candidate = shutil.which("rex")
    if candidate:
        return candidate
    fallback = Path.home() / ".local/bin/rex"
    if fallback.is_file():
        return str(fallback)
    return None


def find_lua():
    for name in ("luajit", "lua", "lua5.4", "lua5.1"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    return None


# Loads the real extrakto.lua through the module entry with a stubbed Rex table.
# The fake executor records every helper invocation so the tests can verify that
# setup() options become the action's defaults.
LUA_HARNESS = """\
local actions, execs = {}, {}
local extract_result = { items = {}, values = {} }
local next_decode = "extract"
rex = {
  action = function(spec) actions[spec.name] = spec end,
  json = {
    encode = function(value)
      if type(value) == "table" and value.jobs then
        print("COLLECT\\t" .. tostring(value.jobs) .. "\\t" .. tostring(value.extract_jobs)
          .. "\\t" .. tostring(value.options and value.options.mode)
          .. "\\t" .. tostring(value.options and value.options.max_lines)
          .. "\\t" .. tostring(value.options and value.options.max_items)
          .. "\\t" .. tostring(value.options and value.options.min_length))
      end
      return "{}"
    end,
    decode = function()
      if next_decode == "view" then
        return {
          windows = {
            {
              window_id = "window",
              layers = {
                {
                  blocks = {
                    { block_id = "block", creator_name = "com.superlogical.terminal", label = "Terminal" },
                  },
                },
              },
            },
          },
        }
      elseif next_decode == "batch" then
        return { panes = { { block_id = "block",
          candidates = { items = {}, values = {}, source_lines = 0, limited = false } } } }
      end
      return extract_result
    end,
  },
  exec = function(argv)
    execs[#execs + 1] = argv
    if argv[2] == "session" and argv[3] == "inspect" then
      next_decode = "view"
    elseif tostring(argv[1]):match("bin/rex%-collect$") then
      next_decode = "batch"
    else
      next_decode = "extract"
    end
    return { ok = true, stdout = "payload" }
  end,
  call = function() return {} end,
}
local module = assert(loadfile(INIT_PATH))()
module.setup(SETUP)
local action = assert(actions.rex_extrakto, "rex_extrakto action was not registered")
local ok, result = pcall(action.run, { session_id = "session", block_id = "block" }, { preview = true })
if not ok then
  print("ERROR\\t" .. tostring(result))
  os.exit(0)
end
print("SCOPE\\t" .. tostring(result and result.scope))
for _, argv in ipairs(execs) do print("EXEC\\t" .. table.concat(argv, "\\t")) end
"""


def runtime_chunk(marker, extra=""):
    """A fake runtime that records the options passed by the module entry."""
    return (
        "local options = ...\n"
        "local log = os.getenv(\"REX_EXTRAKTO_TEST_LOG\")\n"
        "if log then\n"
        "  local file = assert(io.open(log, \"w\"))\n"
        "  file:write(rex.json.encode({ marker = " + json.dumps(marker) + ", options = options }))\n"
        "  file:close()\n"
        "end\n"
        "rex.action{ name = \"rex_extrakto\", title = \"Test Extract\", run = function() return { status = \"test\" } end }\n"
        "rex.action{ name = \"rex_extrakto_lines\", title = \"Test Lines\", run = function() return { status = \"test\" } end }\n"
        + extra
    )


class ModuleEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rex = find_rex()
        if not cls.rex:
            raise unittest.SkipTest("Rex CLI is not installed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex module tests ")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.config_dir = self.root / "config"
        self.plugin = self.config_dir / "plugins/rex-extrakto"
        self.plugin.mkdir(parents=True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.xdg = self.root / "xdg"
        self.xdg.mkdir()
        self.config = self.config_dir / "init.lua"
        self.log = self.root / "options.json"
        self.binding_log = self.root / "bindings.txt"
        shutil.copyfile(MODULE, self.plugin / "init.lua")

    # -- fixtures ---------------------------------------------------------

    def write_runtime(self, directory, marker, extra=""):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "extrakto.lua").write_text(runtime_chunk(marker, extra))
        return directory

    def write_registry(self, installations, schema_version=1):
        path = self.state / "rex-extrakto/installations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": schema_version, "installations": installations}))
        return path

    def write_manifest(self, version, directory=None, schema_version=1):
        current = self.data / "rex-extrakto/current"
        current.mkdir(parents=True, exist_ok=True)
        (current / "release.json").write_text(
            json.dumps({"schema_version": schema_version, "version": version}))
        release = directory or (self.data / "rex-extrakto/releases" / version)
        self.write_runtime(release, "manifest")
        return release

    def binding_probe(self):
        path = json.dumps(str(self.binding_log))
        return (
            "local probe = assert(io.open(" + path + ", \"w\"))\n"
            "for _, binding in ipairs(rex.bindings()) do\n"
            "  probe:write(tostring(binding.key or \"\"), \"\\t\", tostring(binding.mode or \"\"), \"\\t\",\n"
            "              tostring(binding.action or \"\"), \"\\t\",\n"
            "              tostring(binding.args and binding.args.scope or \"\"), \"\\n\")\n"
            "end\n"
            "probe:close()\n"
        )

    def environment(self):
        environment = {key: value for key, value in os.environ.items() if key != "REX_CONFIG"}
        environment.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg),
            "XDG_STATE_HOME": str(self.state),
            "XDG_DATA_HOME": str(self.data),
            "REX_EXTRAKTO_TEST_LOG": str(self.log),
        })
        return environment

    # -- helpers ----------------------------------------------------------

    def run_config(self, body, probe=False):
        text = body.rstrip("\n") + "\n"
        if probe:
            text += self.binding_probe()
        self.config.write_text(text)
        self.log.unlink(missing_ok=True)
        self.binding_log.unlink(missing_ok=True)
        return subprocess.run(
            [self.rex, "--autostart=false", "config", "check", str(self.config), "--json"],
            capture_output=True, text=True, timeout=60, env=self.environment())

    def checked(self, body, probe=False):
        result = self.run_config(body, probe)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["diagnostics"], [], report)
        return report

    def failed(self, body):
        result = self.run_config(body)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Error: the configuration has errors", result.stderr)
        report = json.loads(result.stdout)
        return [entry.get("message", "") for entry in report["diagnostics"]]

    def assert_failure(self, body, fragment):
        messages = self.failed(body)
        self.assertTrue(any(fragment in message for message in messages),
                        "expected %r in diagnostics: %r" % (fragment, messages))

    def logged(self):
        return json.loads(self.log.read_text())

    def bindings(self):
        entries = []
        for line in self.binding_log.read_text().splitlines():
            key, mode, action, scope = line.split("\t")
            entries.append({"key": key, "mode": mode, "action": action, "scope": scope})
        return entries

    @staticmethod
    def normalize_key(key):
        return "+".join(sorted(part.strip().lower() for part in key.split("+")))

    def actions(self, report):
        return sorted(action["name"] for action in report["actions"])

    # -- loading and options ---------------------------------------------

    def test_require_alone_has_no_side_effects(self):
        report = self.checked('require("plugins.rex-extrakto")', probe=True)
        self.assertEqual(report["actions"], [])
        self.assertEqual(self.bindings(), [])
        self.assertFalse(self.log.exists(), "module load must not touch a runtime")

    def test_dev_mode_loads_checkout_and_passes_merged_options(self):
        self.write_runtime(self.plugin, "checkout")
        report = self.checked(
            "local module = require(\"plugins.rex-extrakto\")\n"
            "local returned = module.setup({ mode = \"dev\", bind = false, jobs = 8 })\n"
            "assert(returned == module, \"setup must return the module\")\n"
            "assert(module.runtime.kind == \"dev\", \"dev mode must use the checkout\")\n"
            "assert(module.runtime.entry == " + json.dumps(str(self.plugin / "extrakto.lua")) + ")\n")
        self.assertEqual(self.actions(report), ["rex_extrakto", "rex_extrakto_lines"])
        logged = self.logged()
        self.assertEqual(logged["marker"], "checkout")
        self.assertEqual(logged["options"], {
            "mode": "dev", "bind": False, "scope": "tab", "jobs": 8})

    def test_dev_mode_requires_checkout_runtime(self):
        self.assert_failure(
            'require("plugins.rex-extrakto").setup({ mode = "dev", bind = false })',
            "dev mode requires " + str(self.plugin / "extrakto.lua"))

    def test_unknown_setup_options_are_rejected(self):
        for name in ("colour", "path", "install"):
            with self.subTest(option=name):
                self.assert_failure(
                    'require("plugins.rex-extrakto").setup({ mode = "dev", bind = false, '
                    + name + ' = "value" })',
                    'unknown setup option "' + name + '"')

    def test_invalid_option_values_are_rejected(self):
        cases = [
            ('{ mode = "production", bind = false }', "mode must be"),
            ('{ mode = "dev", bind = "yes" }', "bind must be a boolean"),
            ('{ mode = "dev", bind = false, scope = "window" }', "scope must be"),
            ('{ mode = "dev", bind = false, jobs = 0 }', "jobs must be between 1 and 32"),
            ('{ mode = "dev", bind = false, jobs = 33 }', "jobs must be between 1 and 32"),
            ('{ mode = "dev", bind = false, jobs = 2.5 }', "jobs must be an integer"),
            ('{ mode = "dev", bind = false, extract_jobs = 5 }', "extract_jobs must be between 1 and 4"),
            ('{ mode = "dev", bind = false, jobs = 8, extract_jobs = 9 }',
             "extract_jobs must be between 1 and 8"),
            ('{ mode = "dev", bind = false, max_lines = 0 }', "max_lines must be at least 1"),
            ('{ mode = "dev", bind = false, max_items = 2001 }', "max_items must be between 1 and 2000"),
            ('{ mode = "dev", bind = false, min_length = -1 }', "min_length must be at least 1"),
            ('{ mode = "dev", bind = false, clipboard = "xclip" }', "clipboard must be"),
            ('"dev"', "setup() options must be a table"),
        ]
        for options, fragment in cases:
            with self.subTest(options=options):
                self.assert_failure(
                    'require("plugins.rex-extrakto").setup(' + options + ')', fragment)

    # -- runtime resolution ----------------------------------------------

    def test_auto_mode_prefers_registry_release(self):
        release = self.write_runtime(self.root / "installed/v1.2.3", "registry")
        self.write_runtime(self.plugin, "checkout")
        self.write_registry({
            str(self.plugin) + "/": {
                "kind": "release", "version": "v1.2.3", "directory": str(release)},
        })
        self.checked('require("plugins.rex-extrakto").setup({ bind = false })')
        logged = self.logged()
        self.assertEqual(logged["marker"], "registry")
        self.assertEqual(logged["options"]["mode"], "auto")
        self.assertEqual(logged["options"]["scope"], "tab")

    def test_auto_mode_uses_release_manifest_without_registry(self):
        self.write_manifest("v2.0.0")
        self.write_runtime(self.data / "rex-extrakto/current", "current")
        self.write_runtime(self.plugin, "checkout")
        self.checked('require("plugins.rex-extrakto").setup({ bind = false })')
        self.assertEqual(self.logged()["marker"], "manifest")

    def test_auto_mode_falls_back_to_checkout(self):
        self.write_runtime(self.plugin, "checkout")
        self.checked('require("plugins.rex-extrakto").setup({ bind = false })')
        self.assertEqual(self.logged()["marker"], "checkout")

    def test_auto_mode_ignores_unusable_registry_entries(self):
        checkout = self.write_runtime(self.plugin, "checkout")
        missing = str(self.root / "missing/release")
        invalid_version = str(self.write_runtime(self.root / "invalid-version", "registry"))
        cases = [
            ("malformed json", None),
            ("schema version", {"schema_version": 2}),
            ("installations", {"installations": []}),
            ("other plugin", {str(self.root / "elsewhere"): {
                "kind": "release", "version": "v1.0.0", "directory": str(self.write_runtime(
                    self.root / "elsewhere-release", "registry"))}}),
            ("unknown kind", {str(self.plugin): {
                "kind": "archive", "version": "v1.0.0", "directory": str(checkout)}}),
            ("bad version", {str(self.plugin): {
                "kind": "release", "version": "1.0.0", "directory": invalid_version}}),
            ("relative directory", {str(self.plugin): {
                "kind": "release", "version": "v1.0.0", "directory": "relative/release"}}),
            ("missing runtime", {str(self.plugin): {
                "kind": "release", "version": "v1.0.0", "directory": missing}}),
        ]
        for label, installations in cases:
            with self.subTest(case=label):
                path = self.state / "rex-extrakto/installations.json"
                if installations is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{not json")
                elif "schema_version" in installations or "installations" in installations:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({
                        "schema_version": installations.get("schema_version", 1),
                        "installations": installations.get("installations", {}),
                    }))
                else:
                    self.write_registry(installations)
                self.checked('require("plugins.rex-extrakto").setup({ bind = false })')
                self.assertEqual(self.logged()["marker"], "checkout")

    def test_auto_mode_ignores_manifest_with_invalid_version(self):
        self.write_runtime(self.plugin, "checkout")
        current = self.data / "rex-extrakto/current"
        current.mkdir(parents=True)
        (current / "release.json").write_text(json.dumps({"schema_version": 1, "version": "2.0.0"}))
        self.write_runtime(self.data / "rex-extrakto/releases/2.0.0", "manifest")
        self.write_runtime(current, "current")
        self.checked('require("plugins.rex-extrakto").setup({ bind = false })')
        self.assertEqual(self.logged()["marker"], "checkout")

    def test_release_mode_requires_a_release_installation(self):
        self.write_runtime(self.plugin, "checkout")
        self.assert_failure(
            'require("plugins.rex-extrakto").setup({ mode = "release", bind = false })',
            "no release installation was found")

    def test_release_mode_ignores_dev_registry_entries(self):
        self.write_runtime(self.plugin, "checkout")
        self.write_registry({str(self.plugin): {
            "kind": "dev", "version": "v1.0.0", "directory": str(self.plugin)}})
        self.assert_failure(
            'require("plugins.rex-extrakto").setup({ mode = "release", bind = false })',
            "no release installation was found")

    def test_release_mode_uses_registry_release(self):
        release = self.write_runtime(self.root / "installed/v3.1.4", "registry")
        self.write_runtime(self.plugin, "checkout")
        self.write_registry({str(self.plugin): {
            "kind": "release", "version": "v3.1.4", "directory": str(release)}})
        self.checked('require("plugins.rex-extrakto").setup({ mode = "release", bind = false })')
        self.assertEqual(self.logged()["marker"], "registry")

    def test_runtime_load_errors_are_reported(self):
        cases = [
            ("syntax", "this is not a Lua chunk", "could not load runtime at"),
            ("initialization", "error(\"boom\")", "could not initialize runtime at"),
        ]
        for label, content, fragment in cases:
            with self.subTest(case=label):
                (self.plugin / "extrakto.lua").write_text(content + "\n")
                self.assert_failure(
                    'require("plugins.rex-extrakto").setup({ mode = "dev", bind = false })',
                    fragment)
                self.assert_failure(
                    'require("plugins.rex-extrakto").setup({ mode = "dev", bind = false })',
                    str(self.plugin / "extrakto.lua"))

    # -- real runtime defaults -------------------------------------------

    def write_real_checkout(self):
        """Copy the real runtime and fake helper binaries into the checkout."""
        shutil.copyfile(ROOT / "extrakto.lua", self.plugin / "extrakto.lua")
        for name in ("rex-extract", "rex-collect", "rex-popup"):
            helper = self.plugin / "bin" / name
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("")
        deps = self.plugin / "deps"
        deps.mkdir()
        (deps / "rex").write_text("")

    def run_lua_harness(self, setup):
        lua = find_lua()
        if not lua:
            self.skipTest("Lua interpreter is not installed")
        script = LUA_HARNESS.replace("INIT_PATH", json.dumps(str(self.plugin / "init.lua")))
        script = script.replace("SETUP", setup)
        result = subprocess.run([lua, "-e", script], capture_output=True, text=True,
                                timeout=60, env=self.environment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [line.split("\t") for line in result.stdout.splitlines() if line]

    def test_options_become_action_defaults_for_pane_extraction(self):
        self.write_real_checkout()
        rows = self.run_lua_harness(
            '{ mode = "dev", bind = false, scope = "pane", jobs = 8, extract_jobs = 2, '
            'max_lines = 99, max_items = 7, min_length = 5, clipboard = "pbcopy" }')
        self.assertEqual([row[1] for row in rows if row[0] == "SCOPE"], ["pane"])
        commands = [row[1:] for row in rows if row[0] == "EXEC"]
        extract = next((argv for argv in commands if argv[0].endswith("bin/rex-extract")), None)
        self.assertIsNotNone(extract, rows)
        self.assertEqual(extract[extract.index("--mode") + 1], "word")
        self.assertEqual(extract[extract.index("--max-lines") + 1], "99")
        self.assertEqual(extract[extract.index("--max-items") + 1], "7")
        self.assertEqual(extract[extract.index("--min-length") + 1], "5")

    def test_options_become_action_defaults_for_session_collection(self):
        self.write_real_checkout()
        rows = self.run_lua_harness(
            '{ mode = "dev", bind = false, scope = "session", jobs = 8, extract_jobs = 2, '
            'max_lines = 99, max_items = 7, min_length = 5 }')
        self.assertEqual([row[1] for row in rows if row[0] == "SCOPE"], ["session"])
        collect = [row[1:] for row in rows if row[0] == "COLLECT"]
        self.assertEqual(len(collect), 1, rows)
        self.assertEqual(collect[0], ["8", "2", "word", "99", "7", "5"])

    # -- default bindings ------------------------------------------------

    def test_bind_defaults_are_idempotent(self):
        self.write_runtime(self.plugin, "checkout")
        self.checked(
            "local module = require(\"plugins.rex-extrakto\")\n"
            "module.setup({ mode = \"dev\", bind = false })\n"
            "module.bind_defaults()\n"
            "module.bind_defaults()\n", probe=True)
        bindings = self.bindings()
        main = [entry for entry in bindings if entry["action"] == "rex_extrakto"]
        lines = [entry for entry in bindings if entry["action"] == "rex_extrakto_lines"]
        self.assertEqual(len(main), 1, bindings)
        self.assertEqual(len(lines), 1, bindings)
        self.assertEqual(self.normalize_key(main[0]["key"]), self.normalize_key("cmd+shift+f"))
        self.assertEqual(main[0]["scope"], "tab")
        self.assertEqual(self.normalize_key(lines[0]["key"]), self.normalize_key("alt+cmd+shift+e"))
        self.assertEqual(lines[0]["scope"], "")

    def test_setup_binds_by_default(self):
        self.write_runtime(self.plugin, "checkout")
        self.checked('require("plugins.rex-extrakto").setup({ mode = "dev" })', probe=True)
        actions = sorted(entry["action"] for entry in self.bindings())
        self.assertEqual(actions, ["rex_extrakto", "rex_extrakto_lines"])

    def test_setup_with_bind_false_leaves_bindings_alone(self):
        self.write_runtime(self.plugin, "checkout")
        self.checked('require("plugins.rex-extrakto").setup({ mode = "dev", bind = false })',
                     probe=True)
        self.assertEqual(self.bindings(), [])

    def test_bind_defaults_without_setup_uses_tab_scope(self):
        self.checked('require("plugins.rex-extrakto").bind_defaults()', probe=True)
        main = [entry for entry in self.bindings() if entry["action"] == "rex_extrakto"]
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0]["scope"], "tab")

    def test_custom_scope_is_used_for_default_binding(self):
        self.write_runtime(self.plugin, "checkout")
        self.checked(
            "local module = require(\"plugins.rex-extrakto\")\n"
            "module.setup({ mode = \"dev\", bind = false, scope = \"session\" })\n"
            "module.bind_defaults()\n", probe=True)
        main = [entry for entry in self.bindings() if entry["action"] == "rex_extrakto"]
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0]["scope"], "session")
        self.assertEqual(self.logged()["options"]["scope"], "session")

    def test_existing_key_binding_is_preserved(self):
        self.write_runtime(self.plugin, "checkout")
        self.checked(
            "rex.action{ name = \"other_action\", title = \"Other\", run = function() return {} end }\n"
            "rex.bind(\"cmd+shift+f\", \"other_action\")\n"
            "local module = require(\"plugins.rex-extrakto\")\n"
            "module.setup({ mode = \"dev\", bind = false })\n"
            "module.bind_defaults()\n", probe=True)
        on_key = [entry for entry in self.bindings()
                  if self.normalize_key(entry["key"]) == self.normalize_key("cmd+shift+f")]
        self.assertEqual([entry["action"] for entry in on_key], ["other_action"])
        self.assertTrue(any(entry["action"] == "rex_extrakto_lines" for entry in self.bindings()))

    def test_existing_action_binding_suppresses_default_key(self):
        self.write_runtime(self.plugin, "checkout")
        self.checked(
            "rex.bind(\"cmd+j\", \"rex_extrakto\")\n"
            "local module = require(\"plugins.rex-extrakto\")\n"
            "module.setup({ mode = \"dev\", bind = false })\n"
            "module.bind_defaults()\n", probe=True)
        main = [entry for entry in self.bindings() if entry["action"] == "rex_extrakto"]
        self.assertEqual([self.normalize_key(entry["key"]) for entry in main], ["cmd+j"])

    # -- native rex do ----------------------------------------------------

    def test_rex_do_exposes_module_state(self):
        self.write_runtime(self.plugin, "checkout")
        package_path = "%s/?.lua;%s/?/init.lua;" % (self.config_dir, self.config_dir)
        script = (
            "package.path = " + json.dumps(package_path) + " .. package.path\n"
            "local module = require(\"plugins.rex-extrakto\")\n"
            "local returned = module.setup({ mode = \"dev\", bind = false, max_lines = 99 })\n"
            "return { same = returned == module, kind = module.runtime.kind,\n"
            "         max_lines = module.options.max_lines, scope = module.options.scope }\n")
        result = subprocess.run(
            [self.rex, "--autostart=false", "do", "-e", script],
            capture_output=True, text=True, timeout=60, env=self.environment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state, {
            "same": True, "kind": "dev", "max_lines": 99, "scope": "tab"})
        self.assertEqual(self.logged()["marker"], "checkout")


if __name__ == "__main__":
    unittest.main()

"""Verify first-use behavior when a source checkout has no native helpers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("rex"), "Rex CLI is not installed")
class SourceRuntimeTests(unittest.TestCase):
    def test_missing_helpers_use_source_version_and_bootstrap_on_same_server(self):
        with tempfile.TemporaryDirectory(prefix="rex source runtime ") as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            plugin.mkdir()
            shutil.copy2(ROOT / "extrakto.lua", plugin)
            (plugin / "plugin.json").write_text(json.dumps({"version": "v9.8.7"}))
            report = root / "report.json"
            config = root / "init.lua"
            script = r'''
local real, specs, launches = rex, {}, {}
local view = { active_window_id = "window:source", windows = {{
  window_id = "window:source", focused_block_id = "block:source",
  layers = {{blocks = {{block_id = "block:source", creator_name = "com.superlogical.terminal", label = "Source"}}}}
}}}
rex = {
  json = real.json,
  action = function(spec) specs[spec.name] = spec end,
  call = function(method)
    if method == "server.status" then return {listeners = {{primary = true, endpoint = "unix:///fixture.sock"}}} end
    if method == "client.action" then return {result = specs.rex_extrakto_runtime.run()} end
    error("unexpected API: " .. method)
  end,
  session = {
    view = function() return view end,
    new_layer = function(request)
      launches[#launches + 1] = request
      return {block_ids = {"block:installer"}, layer_id = "layer:installer"}
    end,
  },
}
assert(loadfile(PLUGIN))({scope = "session", jobs = 7})
local metadata = specs.rex_extrakto_runtime.run()
assert(metadata.version == "v9.8.7" and metadata.installed == false)
local context = {session_id = "session:source", block_id = "block:source"}
local ok, message = pcall(specs.rex_extrakto.run, context, {preview = true})
assert(not ok and tostring(message):find("Install rex%-extrakto v9.8.7"))
assert(#launches == 0)
local opened = specs.rex_extrakto.run(context, {query = "extko", jobs = 2})
assert(opened.status == "installing" and #launches == 1)
local arguments = real.json.decode(launches[1].layout.block.options.command[9])
assert(arguments.scope == "session" and arguments.jobs == 2 and arguments.query == "extko")
assert(loadfile(PLUGIN))({mode = "dev", scope = "tab", bind = false})
local dev_ok, dev_message = pcall(specs.rex_extrakto.run, context, {preview = true})
assert(not dev_ok and tostring(dev_message):find("scripts/build.sh", 1, true))
assert(#launches == 1)
local output = assert(io.open(REPORT, "w"))
output:write(real.json.encode({metadata = metadata, arguments = arguments, status = opened.status}))
output:close()
rex = real
'''
            config.write_text("local PLUGIN = " + json.dumps(str(plugin / "extrakto.lua"))
                              + "\nlocal REPORT = " + json.dumps(str(report)) + "\n" + script)
            env = dict(os.environ)
            env.pop("REX_CONFIG", None)
            env["HOME"] = str(root / "home")
            env["XDG_CONFIG_HOME"] = str(root / "config")
            result = subprocess.run([shutil.which("rex"), "config", "check", str(config), "--json"],
                                    capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["diagnostics"], [])
            self.assertEqual(json.loads(report.read_text())["status"], "installing")


if __name__ == "__main__":
    unittest.main()

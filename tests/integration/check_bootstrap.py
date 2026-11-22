"""A pending remote bootstrap is reused without launching another installer."""
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "target/test-results/bootstrap"


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    # Calls execute the production action, but the target API only records launch
    # requests. No installer, network download, or remote configuration is run.
    script = r'''
local real = rex
local specs, launches = {}, {}
local title_ready, failed = false, false
local reentrant
local window = { window_id = "window:source", focused_block_id = "block:source", zoomed_block_id = "block:source",
  layers = { { blocks = { { block_id = "block:source", creator_name = "com.superlogical.terminal", label = "Source" } } } } }
local caller = { window_id = "window:caller", layers = { { blocks = {
  { block_id = "block:caller", creator_name = "com.superlogical.terminal", label = "Caller" } } } } }
local target = {
  call = function(method)
    if method == "client.action" then return nil, "not_found" end
    if method == "server.status" then return {listeners={{primary=true, endpoint="unix:///review-only.sock"}}} end
  end,
  block = { call = function(_, method)
    if method == "title" then return {title=title_ready and "rex-extrakto" or ""} end
    if method == "process" then return failed and {last_exit={code=1}} or {child={name="sh"}} end
  end },
  session = {
    view = function() return {active_window_id=window.window_id, windows={window,caller}} end,
    new_layer = function(request)
      assert(request.window_id == window.window_id)
      if #launches == 0 then
        reentrant = specs.rex_extrakto.run({server="review-target",session_id="session:review",block_id="block:source"}, {scope="tab"})
        assert(reentrant.status == "installing" and #launches == 0)
      end
      local id = "block:installer-" .. tostring(#launches + 1)
      launches[#launches + 1] = {label=request.layout.block.label, resume_block=request.layout.block.options.command[8]}
      window.layers[#window.layers + 1] = {blocks={{block_id=id, creator_name="com.superlogical.terminal",label=request.layout.block.label}}}
      window.focused_block_id = id
      window.zoomed_block_id = nil
      return {block_ids={id},layer_id="layer:installer",revision=#launches}
    end,
  },
}
rex = {action=function(spec) specs[spec.name]=spec end, server=function() return target end, exec=real.exec, json=real.json}
local function load() dofile(PLUGIN) end
load()
local args = {scope="tab"}
local ctx = {server="review-target",session_id="session:review",block_id="block:source"}
local first = specs.rex_extrakto.run(ctx,args)
local second = specs.rex_extrakto.run({server=ctx.server,session_id=ctx.session_id,block_id=first.block_ids[1]},args)
assert(first.status == "installing" and second.status == "installing")
assert(first.block_ids[1] == second.block_ids[1] and #launches == 1)
assert(launches[1].resume_block == "block:source")
-- An independent script/config load recognizes the running install by its title.
title_ready = true
load()
local third = specs.rex_extrakto.run({server=ctx.server,session_id=ctx.session_id,block_id="block:caller"},
  {scope="tab",source_block="block:source"})
assert(third.status == "installing" and third.block_ids[1] == first.block_ids[1] and #launches == 1)
failed = true
local diagnostic = specs.rex_extrakto.run(ctx,args)
assert(diagnostic.status == "installation_failed" and #launches == 1)
return {launch_count=#launches,original_source=launches[1].resume_block,
  rapid_repeat=second.status,independent_load=third.status,failed_install=diagnostic.status,
  concurrent_repeat=reentrant.status}
'''
    with tempfile.TemporaryDirectory(prefix="rex bootstrap test ") as temporary:
        path = Path(temporary) / "bootstrap.lua"
        path.write_text("local PLUGIN = " + json.dumps(str(ROOT / "extrakto.lua")) + "\n" + script)
        output = subprocess.run(["rex", "do", str(path), "--json"], capture_output=True, timeout=30, check=True)
        result = json.loads(output.stdout)
    (ARTIFACTS / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print("PASS: pending bootstrap is reused across repeated actions and Lua loads; original source preserved", flush=True)


if __name__ == "__main__":
    main()

-- rex-extrakto module: native Rex entry loaded with require("plugins.rex-extrakto").
--
-- scripts/install.py copies this file to <config>/plugins/rex-extrakto/init.lua and
-- manages the configuration:
--   require("plugins.rex-extrakto").setup({ bind = false })
--   ...
--   require("plugins.rex-extrakto").bind_defaults()
--
-- A local checkout can be used directly:
--   require("plugins.rex-extrakto").setup({ mode = "dev", bind = false })
--   require("plugins.rex-extrakto").bind_defaults()
--
-- Runtime selection:
--   mode = "auto"    (default) pinned installation registry, then the current
--                    release manifest, then extrakto.lua next to this module
--   mode = "release" pinned installation registry or current release manifest
--   mode = "dev"     extrakto.lua next to this module
--
-- The module performs no file access and no process execution while it is
-- loaded; every lookup happens inside setup().

local M = {}

local defaults = {
  mode = "auto",
  bind = true,
  scope = "tab",
}

local allowed = {
  mode = true, bind = true, scope = true,
  jobs = true, extract_jobs = true, max_lines = true,
  max_items = true, min_length = true, clipboard = true,
}

local modes = { auto = true, release = true, dev = true }
local scopes = { session = true, tab = true, pane = true }
local clipboards = { auto = true, pbcopy = true, osc52 = true }

local function module_directory()
  local source = debug.getinfo(1, "S").source or ""
  local path = source:gsub("^@", ""):gsub("\\", "/")
  local directory = path:match("^(.*)/[^/]+$")
  if directory == nil then return "." end
  directory = directory:gsub("/+$", "")
  if directory == "" then return "/" end
  return directory
end

local directory = module_directory()

local function environment(name)
  local value = os.getenv(name)
  if value == nil or value == "" then return nil end
  return value
end

local function normalize(path)
  return (path:gsub("\\", "/"):gsub("/+$", ""))
end

local function join(parent, name)
  if parent == "/" then return "/" .. name end
  return parent .. "/" .. name
end

local function read_file(path)
  local file = io.open(path, "rb")
  if not file then return nil end
  local data = file:read("*a")
  file:close()
  return data
end

-- io.open() accepts directories on some platforms; only regular files are runtimes.
local function readable_file(path)
  local file = io.open(path, "rb")
  if not file then return false end
  local data = file:read(1)
  file:close()
  return data ~= nil
end

local function decode(text)
  if type(rex) ~= "table" or type(rex.json) ~= "table" or type(rex.json.decode) ~= "function" then
    return nil
  end
  local ok, value = pcall(rex.json.decode, text)
  if ok and type(value) == "table" then return value end
  return nil
end

local function valid_version(value)
  return type(value) == "string" and value:match("^v%d[%w%.%+%-_]*$") ~= nil
end

local function state_registry()
  local root = environment("XDG_STATE_HOME")
  if not root then
    local home = environment("HOME")
    if not home then return nil end
    root = join(home, ".local/state")
  end
  return join(normalize(root), "rex-extrakto/installations.json")
end

local function data_manifest()
  local root = environment("XDG_DATA_HOME")
  if not root then
    local home = environment("HOME")
    if not home then return nil end
    root = join(home, ".local/share")
  end
  return join(normalize(root), "rex-extrakto/current/release.json")
end

local function candidate(location, kind, version, source)
  if type(location) ~= "string" then return nil end
  local path = normalize(location)
  if path == "" then return nil end
  local entry = join(path, "extrakto.lua")
  if not readable_file(entry) then return nil end
  return { directory = path, entry = entry, kind = kind, version = version, source = source }
end

-- The registry is written by scripts/install.py under both the absolute and the
-- resolved plugin directory. Compare normalized keys so trailing slashes and
-- separators do not matter.
local function registry_runtime(mode)
  local path = state_registry()
  if not path then return nil end
  local text = read_file(path)
  if not text then return nil end
  local registry = decode(text)
  if type(registry) ~= "table" or registry.schema_version ~= 1 then return nil end
  local installations = registry.installations
  if type(installations) ~= "table" then return nil end
  local target = normalize(directory)
  local fallback
  for key, entry in pairs(installations) do
    if type(key) == "string" and type(entry) == "table" and normalize(key) == target then
      local kind = entry.kind
      local accepted = kind == "release" or (kind == "dev" and mode ~= "release")
      if accepted and (kind ~= "release" or valid_version(entry.version)) then
        local location = entry.directory
        if type(location) == "string" and location:sub(1, 1) == "/" then
          local runtime = candidate(location, kind, entry.version, "registry")
          if runtime then
            if kind == "release" then return runtime end
            fallback = fallback or runtime
          end
        end
      end
    end
  end
  return fallback
end

local function manifest_runtime()
  local path = data_manifest()
  if not path then return nil end
  local text = read_file(path)
  if not text then return nil end
  local manifest = decode(text)
  if type(manifest) ~= "table" then return nil end
  if manifest.schema_version ~= nil and manifest.schema_version ~= 1 then return nil end
  if not valid_version(manifest.version) then return nil end
  local root = path:match("^(.*)/rex%-extrakto/current/release%.json$")
  if not root then return nil end
  return candidate(join(root, "rex-extrakto/releases/" .. manifest.version),
    "release", manifest.version, "manifest")
end

local function checkout_runtime()
  return candidate(directory, "dev", nil, "checkout")
end

local function resolve_runtime(mode)
  if mode == "dev" then
    local runtime = checkout_runtime()
    if not runtime then
      error("rex-extrakto: dev mode requires " .. join(directory, "extrakto.lua"), 0)
    end
    return runtime
  end
  local runtime = registry_runtime(mode)
  if runtime then return runtime end
  runtime = manifest_runtime()
  if runtime then return runtime end
  if mode == "release" then
    error("rex-extrakto: no release installation was found in "
      .. (state_registry() or "<state>/rex-extrakto/installations.json")
      .. " or the current release manifest", 0)
  end
  runtime = checkout_runtime()
  if runtime then return runtime end
  error("rex-extrakto: no rex-extrakto runtime was found; install a release or use setup({ mode = \"dev\" })", 0)
end

local function integer(value, name, minimum, maximum)
  if type(value) ~= "number" or value % 1 ~= 0 then
    error("rex-extrakto: " .. name .. " must be an integer", 0)
  end
  if value < minimum or (maximum ~= nil and value > maximum) then
    if maximum == nil then
      error("rex-extrakto: " .. name .. " must be at least " .. minimum, 0)
    end
    error("rex-extrakto: " .. name .. " must be between " .. minimum .. " and " .. maximum, 0)
  end
end

local function validated(options)
  if options == nil then options = {} end
  if type(options) ~= "table" then
    error("rex-extrakto: setup() options must be a table", 0)
  end
  for name in pairs(options) do
    if not allowed[name] then
      error("rex-extrakto: unknown setup option " .. string.format("%q", tostring(name)), 0)
    end
  end
  local merged = {}
  for name, value in pairs(defaults) do merged[name] = value end
  for name, value in pairs(options) do merged[name] = value end
  if not modes[merged.mode] then
    error('rex-extrakto: mode must be "auto", "release", or "dev"', 0)
  end
  if type(merged.bind) ~= "boolean" then
    error("rex-extrakto: bind must be a boolean", 0)
  end
  if not scopes[merged.scope] then
    error('rex-extrakto: scope must be "session", "tab", or "pane"', 0)
  end
  if merged.jobs ~= nil then integer(merged.jobs, "jobs", 1, 32) end
  if merged.extract_jobs ~= nil then
    integer(merged.extract_jobs, "extract_jobs", 1, merged.jobs or 4)
  end
  if merged.max_lines ~= nil then integer(merged.max_lines, "max_lines", 1) end
  if merged.max_items ~= nil then integer(merged.max_items, "max_items", 1, 2000) end
  if merged.min_length ~= nil then integer(merged.min_length, "min_length", 1) end
  if merged.clipboard ~= nil and not clipboards[merged.clipboard] then
    error('rex-extrakto: clipboard must be "auto", "pbcopy", or "osc52"', 0)
  end
  return merged
end

local function compile(path)
  if type(loadfile) == "function" then return loadfile(path) end
  local text = read_file(path)
  if not text then return nil, "could not read " .. path end
  local loader = loadstring or load
  if type(loader) ~= "function" then return nil, "no Lua chunk loader is available" end
  return loader(text, "@" .. path)
end

local function load_runtime(runtime, options)
  local chunk, message = compile(runtime.entry)
  if not chunk then
    error("rex-extrakto: could not load runtime at " .. runtime.entry .. ": " .. tostring(message), 0)
  end
  local copy = {}
  for name, value in pairs(options) do copy[name] = value end
  local ok, result = pcall(chunk, copy)
  if not ok then
    error("rex-extrakto: could not initialize runtime at " .. runtime.entry .. ": " .. tostring(result), 0)
  end
  return result
end

local function bound(key, action)
  for _, binding in ipairs(rex.bindings({ key = key })) do
    if binding.mode == nil or binding.mode == "" then return true end
  end
  return #rex.bindings({ action = action }) > 0
end

function M.bind_defaults()
  if type(rex) ~= "table" or type(rex.bind) ~= "function" or type(rex.bindings) ~= "function" then
    error("rex-extrakto: bind_defaults() requires the Rex runtime", 0)
  end
  local scope = M.scope or defaults.scope
  if not bound("cmd+shift+f", "rex_extrakto") then
    rex.bind("cmd+shift+f", "rex_extrakto", { scope = scope })
  end
  if not bound("alt+cmd+shift+e", "rex_extrakto_lines") then
    rex.bind("alt+cmd+shift+e", "rex_extrakto_lines")
  end
end

function M.setup(options)
  local merged = validated(options)
  local runtime = resolve_runtime(merged.mode)
  load_runtime(runtime, merged)
  M.scope = merged.scope
  M.options = merged
  M.runtime = runtime
  if merged.bind then M.bind_defaults() end
  return M
end

return M

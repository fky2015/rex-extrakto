-- CLI: rex do ./extrakto.lua --action rex_extrakto -s SESSION -b BLOCK
-- Config: dofile("/absolute/path/extrakto.lua"); rex.bind("cmd+shift+f", "rex_extrakto", { scope = "tab" })
local source = debug.getinfo(1, "S").source
local directory = source:gsub("^@", ""):match("^(.*/)")
assert(directory, "Load extrakto.lua from a file, so the Rust helper can be located")
local terminal = "com.superlogical.terminal"
local picker_script = directory .. "scripts/popup.py"
local picker_blocks = {}

local function executable(name)
  if name:find("/", 1, true) then return name end
  local paths = { directory .. "deps" }
  for path in ((os.getenv("PATH") or "") .. ":"):gmatch("(.-):") do
    if path ~= "" then paths[#paths + 1] = path end
  end
  local home = os.getenv("HOME")
  if home then paths[#paths + 1] = home .. "/.local/bin" end
  for _, path in ipairs({ "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin" }) do
    paths[#paths + 1] = path
  end
  for _, path in ipairs(paths) do
    local candidate = path .. "/" .. name
    local file = io.open(candidate, "r")
    if file then file:close(); return candidate end
  end
  error("Could not locate " .. name .. "; install it or add its directory to PATH", 0)
end

local function execute(argv, options)
  argv[1] = executable(argv[1])
  local result, err = rex.exec(argv, options)
  if not result then return nil, err or "Command could not start" end
  if not result.ok then return nil, result.stderr ~= "" and result.stderr or "Command failed" end
  return result.stdout
end

local function command(argv, options)
  local output, err = execute(argv, options)
  if not output then error(err, 0) end
  return output
end

local function rex_argv(ctx, args)
  local argv = { "rex" }
  local server = args.source_server or ctx.server
  if server and server ~= "" then argv[#argv + 1] = "-S"; argv[#argv + 1] = server end
  return argv
end

local function capture(ctx, args, selected_block)
  local session = args.source_session or ctx.session_id
  local block = selected_block or args.source_block or ctx.block_id
  if not session or not block then error("Select a terminal pane, or pass -s SESSION -b BLOCK", 0) end
  -- The CLI handles format/blob_read pagination and unwraps soft-wrapped lines.
  -- An explicit source_server also lets a newer server host the prompt for an older source.
  local argv = rex_argv(ctx, args)
  for _, value in ipairs({ "capture", "-s", session, "-b", block, "--trim", "--unwrap" }) do
    argv[#argv + 1] = value
  end
  return execute(argv, { timeout = 15 })
end

local function extract(text, mode, args)
  local output = command({ directory .. "bin/rex-extract",
    "--mode", mode,
    "--max-lines", tostring(args.max_lines or 3000),
    "--max-items", tostring(args.max_items or 2000),
    "--min-length", tostring(args.min_length or 3),
  }, { stdin = text, timeout = 10 })
  local candidates, err = rex.json.decode(output)
  if not candidates then error(err or "Could not decode extraction results", 0) end
  return candidates
end

local function is_picker(ctx, args, block)
  if picker_blocks[block.block_id] then return true end
  if block.label ~= "Extract text" then return false end
  local session = args.source_session or ctx.session_id
  local argv = rex_argv(ctx, args)
  for _, value in ipairs({ "block", "call", "-s", session, "-b", block.block_id, "title" }) do
    argv[#argv + 1] = value
  end
  local title_output = execute(argv, { timeout = 5 })
  local title = title_output and rex.json.decode(title_output)
  if title and title.title == "rex-extrakto" then return true end
  -- Older, unmarked pickers can still be recognized when process details are available.
  argv = rex_argv(ctx, args)
  for _, value in ipairs({ "block", "call", "-s", session, "-b", block.block_id, "process" }) do
    argv[#argv + 1] = value
  end
  local output = execute(argv, { timeout = 5 })
  if not output then return false end
  local process = rex.json.decode(output)
  if not process then return false end
  local child, foreground = process.child or {}, process.foreground or {}
  local script = child.script or foreground.script
  if script then return script == picker_script end
  local name = (child.name or ""):lower()
  local cwd = child.cwd or foreground.cwd or ""
  return name:find("python", 1, true) and foreground.name == "fzf"
    and cwd:gsub("/+$", "") == directory:gsub("/+$", "")
end

local function scope_panes(ctx, args, scope)
  local session = args.source_session or ctx.session_id
  local origin = args.source_block or ctx.block_id
  if not session or not origin then error("Select a terminal pane, or pass -s SESSION -b BLOCK", 0) end
  local argv = rex_argv(ctx, args)
  for _, value in ipairs({ "session", "inspect", session, "--json" }) do argv[#argv + 1] = value end
  local view, err = rex.json.decode(command(argv, { timeout = 10 }))
  if not view then error(err, 0) end
  local window, window_index
  local live = {}
  for index, candidate in ipairs(view.windows or {}) do
    for _, layer in ipairs(candidate.layers or {}) do
      for _, block in ipairs(layer.blocks or {}) do
        live[block.block_id] = true
        if block.block_id == origin then window, window_index = candidate, index end
      end
    end
  end
  for block, owner_session in pairs(picker_blocks) do
    if owner_session == session and not live[block] then picker_blocks[block] = nil end
  end
  if not window then error("The source pane is not in a tab", 0) end
  local windows = { { window = window, index = window_index } }
  if scope == "session" then
    for index, candidate in ipairs(view.windows or {}) do
      if candidate.window_id ~= window.window_id then
        windows[#windows + 1] = { window = candidate, index = index }
      end
    end
  end
  local panes, first = {}, nil
  for _, entry in ipairs(windows) do
    local index = 0
    for _, layer in ipairs(entry.window.layers or {}) do
      for _, block in ipairs(layer.blocks or {}) do
        local picker = block.creator_name == terminal and is_picker(ctx, args, block)
        if block.block_id == origin and picker then error("Close the current extractor before opening another", 0) end
        if block.creator_name == terminal and not picker then
          index = index + 1
          local pane = { block_id = block.block_id, label = block.label or "Terminal",
            window_id = entry.window.window_id, window_label = entry.window.label or "Tab",
            display = tostring(index) .. ": " .. (block.label or "Terminal") }
          if scope == "session" then
            pane.display = tostring(entry.index) .. ": " .. pane.window_label .. " / " .. pane.display
          end
          if block.block_id == origin then first = pane else panes[#panes + 1] = pane end
        end
      end
    end
  end
  if not first then error("The source pane is not a terminal", 0) end
  table.insert(panes, 1, first)
  return panes, window.window_id
end

local function collect(ctx, args, mode, scope)
  if scope == "pane" then
    local text, err = capture(ctx, args)
    if not text then error(err, 0) end
    local candidates = extract(text, mode, args)
    candidates.scope = "pane"
    return candidates
  end
  local panes, window = scope_panes(ctx, args, scope)
  local requested = {}
  for _, pane in ipairs(panes) do
    requested[#requested + 1] = { block_id = pane.block_id, display = pane.display }
  end
  local output = command({ directory .. "bin/rex-collect" }, {
    stdin = rex.json.encode{
      rex = executable("rex"), server = args.source_server or ctx.server,
      session_id = args.source_session or ctx.session_id, panes = requested,
      jobs = args.jobs or 4, extract_jobs = args.extract_jobs or 1,
      options = { mode = mode, max_lines = args.max_lines or 3000,
        max_items = args.max_items or 2000, min_length = args.min_length or 3 },
    },
    timeout = math.max(30, math.ceil(#panes / (args.jobs or 4)) * 15
      + math.ceil(#panes / (args.extract_jobs or 1)) * 10 + 10),
  })
  local batch, batch_err = rex.json.decode(output)
  if not batch then error(batch_err or "Could not decode collected panes", 0) end
  if not batch.panes or #batch.panes ~= #panes then error("Collector returned an incomplete pane list", 0) end
  local groups, skipped = {}, {}
  local merged = { items = {}, values = {}, source_lines = 0, limited = false,
    scope = scope, window_id = window, panes = {}, skipped_panes = skipped }
  local longest = 0
  for index, pane in ipairs(panes) do
    local collected = batch.panes[index]
    if collected.block_id ~= pane.block_id then error("Collector returned panes out of order", 0) end
    if collected.error then
      skipped[#skipped + 1] = pane
    else
      local candidates = collected.candidates
      if not candidates then error("Collector returned no candidates for " .. pane.display, 0) end
      groups[#groups + 1] = { pane = pane, candidates = candidates }
      longest = math.max(longest, #candidates.items)
      merged.source_lines = merged.source_lines + candidates.source_lines
      merged.limited = merged.limited or candidates.limited
      merged.panes[#merged.panes + 1] = { block_id = pane.block_id, label = pane.label,
        window_id = pane.window_id, window_label = pane.window_label,
        source_lines = candidates.source_lines, count = #candidates.items, limited = candidates.limited }
    end
  end
  local seen, origins = {}, {}
  local cap = args.max_items or 2000
  -- Interleave each pane's recent candidates so one large pane cannot consume the list first.
  for slot = 1, longest do
    for _, group in ipairs(groups) do
      local candidate = group.candidates.items[slot]
      if candidate then
        local value = group.candidates.values[candidate.id]
        local existing = seen[value]
        if existing then
          origins[existing][#origins[existing] + 1] = group.pane.display
        elseif #merged.items < cap then
          local id = tostring(#merged.items + 1)
          merged.items[#merged.items + 1] = { id = id, title = candidate.title, detail = candidate.detail }
          merged.values[id] = value
          seen[value] = #merged.items
          origins[#merged.items] = { group.pane.display }
        else
          merged.limited = true
        end
      end
    end
  end
  for index, item in ipairs(merged.items) do
    local names = origins[index]
    item.subtitle = names[1]
    if #names > 1 then item.subtitle = item.subtitle .. ", " .. names[2] end
    if #names > 2 then item.subtitle = item.subtitle .. " +" .. tostring(#names - 2) end
  end
  return merged
end

local function popup(ctx, args, candidates)
  local fzf = executable("fzf")
  local python = executable("python3")
  command({ fzf, "--version" }, { timeout = 5 })
  local temp = command({ "mktemp", "-d", "/tmp/rex-extrakto.XXXXXX" }, { timeout = 5 }):gsub("%s+$", "")
  local path = temp .. "/state.json"
  candidates.query = args.query or ""
  candidates.result_file = args.result_file
  candidates.fzf = fzf
  candidates.clipboard = args.clipboard or "auto"
  candidates.rex = executable("rex")
  candidates.target = {
    server = args.source_server or ctx.server,
    session_id = args.source_session or ctx.session_id,
    block_id = args.source_block or ctx.block_id,
  }
  local file, err = io.open(path, "w")
  if not file then error(err, 0) end
  file:write(rex.json.encode(candidates))
  file:close()
  local result, layer_err = rex.session.new_layer{
    session_id = ctx.session_id,
    window_id = (args.source_session or ctx.session_id) == ctx.session_id
      and candidates.window_id or nil,
    bounds = { 0.1, 0.15, 0.8, 0.7 },
    focus = true,
    layout = { block = {
      flavor = "com.superlogical.terminal.shell",
      label = "Extract text",
      options = { command = { python, picker_script, path },
        cwd = directory, shell = "none", exit = { on_completion = not args.keep_open } },
    } },
  }
  if not result then
    os.remove(path)
    command({ "rmdir", temp }, { timeout = 5 })
    error(layer_err or "Could not open extraction popup", 0)
  end
  result.status = "popup"
  result.count = #candidates.items
  result.scope = candidates.scope or "pane"
  result.pane_count = candidates.panes and #candidates.panes or 1
  for _, block in ipairs(result.block_ids or {}) do picker_blocks[block] = ctx.session_id end
  return result
end

local function run(default_mode, default_scope)
  return function(ctx, args)
    local mode = args.mode or default_mode
    local scope = args.scope or default_scope or "pane"
    if scope ~= "pane" and scope ~= "tab" and scope ~= "session" then
      error("scope must be session, tab, or pane", 0)
    end
    local jobs, extract_jobs = args.jobs or 4, args.extract_jobs or 1
    if jobs < 1 or jobs > 32 then error("jobs must be 1..32", 0) end
    if extract_jobs < 1 or extract_jobs > jobs then error("extract_jobs must be 1..jobs", 0) end
    if args.clipboard and args.clipboard ~= "auto" and args.clipboard ~= "pbcopy" and args.clipboard ~= "osc52" then
      error("clipboard must be auto, pbcopy, or osc52", 0)
    end
    local helper = directory .. "bin/rex-extract"
    local file = io.open(helper, "rb")
    if not file then error("Rust helper is missing; run " .. directory .. "scripts/build.sh", 0) end
    file:close()
    local candidates = collect(ctx, args, mode, scope)
    if args.preview then return candidates end
    if #candidates.items == 0 then return { status = "empty", count = 0 } end

    local ui = args.ui or "fzf"
    if ui ~= "auto" and ui ~= "native" and ui ~= "fzf" then error("ui must be auto, native, or fzf", 0) end
    if args.operation and args.operation ~= "copy" and args.operation ~= "insert" then
      error("operation must be copy or insert", 0)
    end
    -- Separate Tab/Enter operations need fzf; rex.ui.pick returns only an item ID.
    if not args.select and (ui == "fzf" or ui == "auto") then return popup(ctx, args, candidates) end

    local chosen
    if args.select then
      for id, value in pairs(candidates.values) do
        if value == args.select then chosen = id; break end
      end
      if not chosen then error("The requested text is not a candidate in the selected scope", 0) end
    else
      if not rex.ui or not rex.ui.pick then
        error("rex.ui.pick requires a newer Rex server", 0)
      end
      local why, detail
      chosen, why, detail = rex.ui.pick{
        title = "Extract text · " .. mode,
        placeholder = "Search terminal text",
        value = args.query or "",
        items = candidates.items,
        timeout = 180,
      }
      if chosen == nil then
        if why == "cancelled" then return { status = "cancelled" } end
        error("Picker failed: " .. tostring(why) .. " " .. (detail or "")
          .. ". Native prompts require server API revision 17+ and a compatible Rex app.", 0)
      end
    end

    local value = candidates.values[chosen]
    if not value then error("Picker returned an unknown candidate", 0) end
    local operation = args.operation or (args.select and "copy" or "insert")
    local status
    if operation == "copy" then
      if args.clipboard == "osc52" then
        error("OSC52 copying uses the fzf picker; direct select/native copy requires pbcopy", 0)
      end
      -- In `rex do`, commands run on the CLI's machine; init.lua runs on the server.
      command({ "/usr/bin/pbcopy" }, { stdin = value, timeout = 5 })
      status = "copied"
    else
      local argv = { "rex" }
      local server = args.source_server or ctx.server
      if server and server ~= "" then argv[#argv + 1] = "-S"; argv[#argv + 1] = server end
      for _, arg in ipairs({ "api", "call", "block.event", rex.json.encode{
        session_id = args.source_session or ctx.session_id,
        block_id = args.source_block or ctx.block_id,
        event = { type = "paste", payload = { text = value } },
      } }) do argv[#argv + 1] = arg end
      command(argv, { timeout = 10 })
      status = "inserted"
    end
    return { status = status, text = value, count = #candidates.items, limited = candidates.limited }
  end
end

local args = {
  mode = "string?", query = "string?", select = "string?", preview = "boolean?",
  source_server = "string?", source_session = "string?", source_block = "string?",
  max_lines = "integer?", max_items = "integer?", min_length = "integer?",
  ui = "string?", result_file = "string?", keep_open = "boolean?",
  operation = "string?",
  scope = "string?",
  jobs = "integer?", extract_jobs = "integer?",
  clipboard = "string?",
}

rex.action{
  name = "rex_extrakto",
  title = "Extract Terminal Text…",
  description = "Search words, paths and URLs; Tab copies, Enter inserts into the original pane.",
  category = "Terminal", keywords = "extrakto fuzzy copy clipboard paste insert word path url",
  args = args, run = run("word"),
}
rex.action{
  name = "rex_extrakto_lines",
  title = "Extract Terminal Line…",
  description = "Search complete lines; Tab copies, Enter inserts into the original pane.",
  category = "Terminal", keywords = "extrakto fuzzy copy clipboard paste insert line",
  args = args, run = run("line"),
}

rex.action{
  name = "rex_extrakto_tab",
  title = "Extract Tab Text…",
  description = "Search all terminal panes in the source tab; Enter inserts into the invoking pane.",
  category = "Terminal", keywords = "extrakto tab panes fuzzy copy paste",
  args = args, run = run("word", "tab"),
}

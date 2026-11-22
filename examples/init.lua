-- Clone the repository to ~/.config/rex/plugins/rex-extrakto.
-- Add these lines to ~/.config/rex/init.lua.
-- A Rex floating pane with fzf: Tab copies, Enter inserts into the source pane.
local extrakto = require("plugins.rex-extrakto")
extrakto.setup({ scope = "tab", bind = false })
-- Put custom bindings here. Defaults only claim unused keys and actions.
extrakto.bind_defaults()

-- Add these lines to ~/.config/rex/init.lua on macOS.
-- A Rex floating pane with fzf: Tab copies, Enter inserts into the source pane.
-- Adjust this path if the project is checked out elsewhere.
dofile(os.getenv("HOME") .. "/code/rex-extrakto/extrakto.lua")
-- scope: "session" (all tabs), "tab" (this tab), or "pane" (this pane).
rex.bind("cmd+shift+f", "rex_extrakto", { scope = "tab" })
rex.bind("alt+cmd+shift+e", "rex_extrakto_lines")

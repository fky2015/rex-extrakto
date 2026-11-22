# Configuration and commands

## Actions and scopes

| Action | Default extraction type | Default scope |
| --- | --- | --- |
| `rex_extrakto` | Words, paths and URLs | Source pane |
| `rex_extrakto_lines` | Complete lines | Source pane |
| `rex_extrakto_tab` | Words, paths and URLs | Tab containing the source pane |

All three actions accept `scope=session/tab/pane`:

| scope | Extraction range |
| --- | --- |
| `session` | Terminal panes in all tabs of the source session |
| `tab` | All terminal panes in the tab containing the source pane |
| `pane` | The source pane only |

The default scope for Cmd + Shift + F remains tab, set by the binding arguments:

```lua
rex.bind("cmd+shift+f", "rex_extrakto", { scope = "tab" })
```

Change it to `scope = "session"` to search the entire session, or `scope = "pane"` to search only the current pane. The complete-lines action supports the same three scopes; for example, passing `scope = "session"` to `rex_extrakto_lines` searches complete lines across all tabs.

The session and tab scopes include both tiled and floating terminal panes in the relevant tabs and exclude the plugin's own picker. The session scope does not read other sessions and does not include detached blocks that are not placed in a tab. The tab scope is determined by the tab containing the pane that starts the search.

Results are deduplicated across panes, with the source shown next to each result; the session scope also shows the tab and pane, making same-named panes in different tabs distinguishable. Enter always inserts into the pane that started the search. For an unzoomed source, the floating picker opens in the tab containing the source pane, even when the session's current focus is elsewhere. A zoomed source uses a temporary picker tab to preserve the original tab's zoom and layout. Normal copy, insert and cancel return to the original tab and remove the temporary tab; an intentional focus change is respected. `keep_open=true` retains the picker for debugging and does not return automatically.

## Extraction parameters

| Parameter | Meaning |
| --- | --- |
| `mode=word/line/path/url/quote` | Extraction type; when omitted, the action's default type is used |
| `scope=session/tab/pane` | All tabs of the source session, the source tab, or only the source pane |
| `query=extko` | Initial search term; empty by default |
| `min_length=3` | Minimum number of characters; must be greater than 0 |
| `max_lines=3000` | Number of most recent lines per pane; must be greater than 0 |
| `max_items=2000` | Candidate limit per pane and for the final merged list; supports 1–2,000 |
| `jobs=4` | Maximum number of pane tasks processed concurrently for session/tab; supports 1–32; 1 means sequential processing |
| `extract_jobs=1` | Maximum CPU extraction concurrency; supports 1–jobs; runs through the Tokio blocking pool |
| `clipboard=auto/pbcopy/osc52` | macOS uses pbcopy by default, Linux uses OSC 52; remote macOS can specify osc52 |

For example, adjust the number of lines per pane and the merged candidate limit:

```lua
rex.bind("cmd+shift+f", "rex_extrakto", {
  scope = "tab",
  max_lines = 10000,
  max_items = 2000,
  jobs = 4,
  extract_jobs = 1,
})
```

Within each pane, the most recently appearing candidates are kept first; when merging across panes, samples are taken round-robin starting from the pane that started the search. The line limit only bounds which candidates are processed; the plugin still captures all text retained by Rex first. Increasing `max_lines` increases extraction work, and history in multiple panes also increases capture time.

Session/tab capture runs concurrently through Tokio asynchronous I/O, while extraction is throttled separately through `spawn_blocking` and a Semaphore. Completion order does not change candidate order; the overall pane-task concurrency limit also bounds how many raw history copies are retained at once. The pane scope has only one source and does not use batch scheduling.

Scrollback that Rex has already discarded cannot be recovered. When a full-screen program uses the alternate screen, the captured content is determined by the screen currently available in that terminal.

## Calling from the CLI

Run from the project directory; the session and block IDs must still exist:

```sh
rex do ./extrakto.lua --action rex_extrakto_tab -s "$REX_SESSION" -b "$REX_BLOCK"
rex do ./extrakto.lua --action rex_extrakto -s "$REX_SESSION" -b "$REX_BLOCK" scope=session
rex do ./extrakto.lua --action rex_extrakto_lines -s "$REX_SESSION" -b "$REX_BLOCK"
rex do ./extrakto.lua --action rex_extrakto -s "$REX_SESSION" -b "$REX_BLOCK" scope=pane query=extko
```

Rex IDs from an old shell may be stale; use `rex whoami --json`, `rex ls --json` and `rex block ls -s SESSION --json` to find the current IDs.

## Debug parameters

| Parameter | Meaning |
| --- | --- |
| `preview=true` | Return candidate JSON without opening the picker |
| `select=TEXT` | Operate directly on a candidate that actually exists; used for automated checks |
| `operation=copy/insert` | Operation for `select` or the native UI; `select` defaults to copy, native defaults to insert |
| `ui=fzf/auto/native` | fzf by default; auto also uses fzf; native is an experimental entry point |
| `source_server=HOST` | Read the source terminal from the specified Rex host |
| `source_session=SESSION source_block=BLOCK` | Specify the source pane, overriding the calling context |
| `result_file=PATH` | Write the picker operation result to a file; only for explicit experiments |
| `keep_open=true` | Keep the picker terminal after exit for troubleshooting |

Example: inspect candidates without opening the picker.

```sh
rex do ./extrakto.lua --action rex_extrakto_tab -s "$REX_SESSION" -b "$REX_BLOCK" preview=true --json
```

The result's `scope` reports the actual scope. Session and tab scopes also include `window_id`, `panes` and `skipped_panes`; `window_id` refers to the tab containing the source pane, and each pane's `window_id` and `window_label` identify its own source. `source_lines` is the sum of the lines actually processed for extraction in each pane; `limited=true` means candidates were truncated. Candidate display fields and exact text are stored in `items` and `values`, respectively.

Normal keyboard-shortcut invocations do not write `result_file`. Temporary candidate data is stored in the system temporary directory and deleted after the picker exits.

## UI and remote sources

fzf distinguishes Tab copy from Enter insert. The native `rex.ui.pick` only returns a candidate ID and requires a compatible Rex server and app, so `ui=native` is kept only as a single-operation experimental entry point.

The plugin routes cross-server invocation to the registered action on the target server, where helper programs, fzf and temporary state share the same filesystem. This also applies to a local `rex do` script selecting a remote server. A missing or different target version starts installation from the calling plugin's release tag and resumes the search; preview requires an existing matching installation. See [remote server setup](installation.md#remote-server-setup). The Linux fzf picker hands copies to the connected Rex client via OSC 52; when viewing a remote macOS server, you can specify `clipboard=osc52`.

Direct copy through `select` and the native UI still depends on `pbcopy`; OSC 52 copy uses the fzf picker. Real Rex integration tests cover the local machine and the OSC 52 protocol; cross-platform builds and installation are verified by CI.

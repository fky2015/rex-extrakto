# Development and verification

## File responsibilities

`extrakto.lua` is the stable plugin entry point. It locates the three Rust programs relative to its own location and does not depend on the working directory at invocation time. It determines the source terminal from `scope=session/tab/pane`, merges candidates and creates a floating picker and records any source zoom for restoration.

`src/extract.rs` contains the shared candidate rules; `src/main.rs` provides the `rex-extract` CLI. `src/collect.rs` uses Tokio asynchronous capture, and `src/bin/rex-collect.rs` provides the batch JSON CLI. Both programs reuse the same extraction logic.

Batch capture reuses the Rex CLI's pagination and soft-wrapping capabilities; `futures-util::buffer_unordered` limits the number of in-flight pane tasks. CPU work is throttled separately through Tokio `spawn_blocking` and a Semaphore, without blocking the I/O runtime. Results are restored in input order; a timeout or failure cancels the remaining captures, and child processes use `kill_on_drop`. Each batch capture creates only one collector process and compiles the Regex once; the pane scope does not use the collector.

`src/popup.rs` runs fzf and, depending on the confirmation key, performs `pbcopy`, OSC 52 or a Rex paste event. `src/bin/rex-popup.rs` provides its CLI. Tokio supplies NUL-separated records and drains fzf output concurrently, including when fzf selects before consuming the full input. Display text is separate from exact candidate values. The picker clears fzf's default-options environment variables and writes an operation result only when explicitly requested. A detached Rust watcher reads only lifecycle metadata and subscribes through `rex events` before the floating pane opens. It handles direct pane closure, restores saved focus or zoom when appropriate, removes temporary state and reaps its event-stream process. A completion marker distinguishes ordinary picker exits from PTY closure; the watcher respects observed focus changes and newer layout revisions.

Cross-server invocation uses Rex's `server.status.host_id` to determine whether CLI and target share files, and `client.action` to run the target's registered plugin. Capture and paste use the selected server's primary listener. A missing or different target version opens a target-side installation terminal using the calling runtime's release tag; preview remains read-only. Repeated invocations reuse an existing installation pane and retain the original resume target. API forwarding never assumes that a local file path exists on another machine.

Rex clears a window's zoom when focus moves to another block in that window. Lua records the original zoomed block before `session.new_layer`. After copy, insert, cancellation or failure, Rust restores saved zoom with `session.zoom_block` or ordinary source focus with `session.focus_block`, provided the picker still owns focus and the source block still exists in the original tab. It targets the picker server separately from the insertion server. First-use installation restores the saved zoom before invoking the original search, allowing that search to capture and restore it in turn. Pickers and installers are excluded from extraction scopes.

Capture, floating placement and cancellation resolve the same source tab, including local `source_block` overrides. A repeated interactive action identifies that tab's picker and queues Escape through the terminal API before capturing candidates. The existing Rust process performs cancellation and restoration. A completed diagnostic picker is closed through `block.close`. Preview and direct selection bypass toggling.

The picker identifies itself through the terminal title, so Lua can continue excluding already-open pickers after independent loading or a configuration reload. An ordinary pane with the same name is not excluded based on its label alone; process queries are used to support older pickers that lack the title marker.

`tests/reference/extract.py` preserves the candidate rules from before the migration as a reference for the Rust output. Test data and the raw input receiver live in `tests/helpers/`; checks for actual Rex operations live in `tests/integration/`.

## Build

```sh
make build
make build CARGO_FLAGS=--offline
```

You can also run `scripts/build.sh` from any working directory. The script uses `cargo build --release --locked` and atomically replaces `bin/rex-extract`, `bin/rex-collect` and `bin/rex-popup` one at a time. Dependencies are locked by `Cargo.lock`; offline builds require the dependencies to be cached. Building from source requires Rust 1.85 or newer.

Local build artifacts target the native architecture of the current machine. The release workflow builds four platforms, using musl on Linux; the picker uses pbcopy by default on macOS and hands copies to the Rex client via OSC 52 on Linux.

Manually check the extraction program:

```sh
printf '%s\n' 'https://github.com/laktak/extrakto' | bin/rex-extract --mode url
bin/rex-extract --help
```

## Tests without Rex

```sh
make test
make check
```

`make test` first builds the programs, then runs Rust unit tests, CLI, asynchronous capture, installation/configuration initialization and clipboard protocol tests, plus behavior comparisons of 240 JSON cases and 24 NUL-record cases. Asynchronous tests use controllable capture processes to verify concurrency limits, ordering, timeouts, cancellation on failure and stdout/stderr draining, without requiring a Rex server.

`make check` runs `cargo fmt --check` and Clippy, treating Clippy warnings as failures. Cargo commands can be given `CARGO_FLAGS=--offline`; the test Python can be selected with `PYTHON=/path/to/python3`.

GitHub CI runs for code, tests, build scripts and workflow changes. Changes confined to Markdown files or `docs/` skip CI. Documentation language is not a CI check.

When running tests directly, use the following entry points:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/check_equivalence.py
```

## Rex integration tests

The local machine must have a running Rex server and must be able to find Python 3, fzf, `pbcopy` and `pbpaste`:

```sh
make test-integration
```

Integration tests load the current project's `extrakto.lua` directly and do not require modifying the user's `init.lua` in advance. Each test creates an independent session, operates using the specified pane ID, and closes that test session when finished. The input receiver only records raw bytes and never executes the content it receives.

| Check | Coverage |
| --- | --- |
| `tests/integration/check_picker.py` | Real capture, clipboard, Tab copy, Enter inserting exact UTF-8 text, Esc cancellation |
| `tests/integration/check_tab.py` | Four source panes (including a floating pane), exclusion of other tabs, picker exclusion, global deduplication, round-robin candidate sampling, insertion target |
| `tests/integration/check_session.py` | Boundaries of the three scopes, cross-tab deduplication, exclusion of other sessions, source overrides, cross-tab picker exclusion, picker and insertion target fixed to the source tab/pane |
| `tests/integration/check_remote.py` | Local Lua script routed to remote Rust/fzf, remote tab capture, fuzzy OSC 52 copy, exact insertion and cancellation; optional first-use installation |
| `tests/integration/check_toggle.py` | Repeated and rapid invocations across scopes, independent Lua loads, cross-tab source overrides, focus/zoom restoration, other-tab picker and preview preservation, completed diagnostic picker dismissal |
| `tests/integration/check_zoom.py` | Floating picker stays in the source tab; zoom restored after copy/insert/cancel and direct pane closure, temporary state cleanup and intentional focus changes |
| `tests/integration/check_bootstrap.py` | Production Lua action with a simulated uninstalled target; pending installation reuse, original resume target, independent Lua loads and failed-install diagnostics |
| `tests/integration/check_osc52.py` | Real fzf picker emits OSC 52, and Rex receives the exact UTF-8 text |

`check_picker.py` invokes `check_clipboard.py` to verify URL soft-wrapping and exact copying of paths and Unicode (Chinese) text. You can also pass it a dedicated demo pane:

```sh
python3 tests/integration/check_clipboard.py SESSION DEMO_BLOCK
```

Remote verification is optional and runs against a separately started disposable server. Reserve an empty directory on the remote machine for artifacts, and pass the test server's SSH endpoint (including its socket):

```sh
python3 tests/integration/check_remote.py \
  --server ssh://user@host/tmp/rex-extrakto-check/server.sock \
  --ssh user@host --remote-dir /tmp/rex-extrakto-check/artifacts
```

Add `--zoom` to verify a zoomed source, including restoration after the floating picker closes. Add `--bootstrap` when the test server has no plugin, after the matching version has been published to GitHub Releases. It first verifies that preview does not install, then checks installation and automatic resumption. The test closes its own session; the caller remains responsible for stopping the disposable server and removing its reserved directory.

At the end of the test, the original clipboard value is restored only if the clipboard still contains the content written by that test. Integration tests should run in sequence to avoid concurrent clipboard use.

Test results are generated under `target/test-results/`:

```text
target/test-results/
├── equivalence.json
├── picker/              copy, insert and cancel results plus received bytes
├── tab/                 multi-pane fixtures, received bytes and check results
└── session/             cross-tab fixtures, received bytes and check results
```

Build caches, binaries, logs and test results are not included in source control.

## Behavior to preserve when making changes

- Rust output matches the Python reference rules, preserving complete URLs, Unicode and internal whitespace exactly.
- fzf matches only candidate text; source labels never enter copied or inserted content.
- Enter sends a literal paste to the pane that started the search, without an extra newline.
- The tab scope is determined by the window containing the source pane and excludes the plugin's own picker.
- The session scope reads only terminal panes in all tabs of the source session; all scopes insert into the source pane.
- Large output from a single pane must not fill all candidates due to concatenation order.

## Release

GitHub Actions CI verifies the Rust 1.85 minimum version, tests and Clippy. Pushing a new `vX.Y.Z` tag that matches the Cargo version starts the release workflow. It builds and verifies four platforms, packages the Rust programs, pinned fzf and dependency licenses, and generates `SHA256SUMS`. After all builds succeed, it creates a draft release, uploads all assets, and then publishes it. The workflow skips tag updates and deletions.

To validate all four platform packages from the latest workflow without publishing a release, dispatch it on `main`:

```sh
gh workflow run release.yml --ref main
```

On a local macOS ARM64 machine, you can check the packaging flow:

```sh
make build
python3 scripts/package.py --target aarch64-apple-darwin
```

`scripts/package.py` uses the already-built programs. Linux releases must first build for the corresponding musl target and specify the directory with `--binary-dir`; the script checks the architecture and static linking. Archive timestamps, permissions and directory order are normalized.

The release package is initialized by `scripts/install.py`, which supports SHA-256 verification, versioned directories, configuration preservation and offline installation. Default shortcuts are guarded at config load through `rex.bindings`, so callbacks, dynamic Lua values and mode-specific bindings use Rex's own binding semantics. See [Installation](installation.md) for normal usage.

Rex prompts must be invoked from an action's `run`. Use `rex do FILE --action ACTION` for standalone experiments, and bind new shortcuts to action names. For interface details, refer to the currently installed `rex --help`, `rex api describe` and the official documentation.

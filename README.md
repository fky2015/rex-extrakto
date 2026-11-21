# rex-extrakto

Extract text from Rex terminals, search with fzf, then copy or insert a result. Cmd + Shift + F searches all terminal panes in the current tab, including floating terminals. Rust handles extraction, concurrent capture, fzf, copying and insertion; Lua connects the plugin to Rex.

Choose the extraction scope with `scope = "session"`, `"tab"` or `"pane"`: all tabs in the source session, the source tab, or the source pane.

## Installation

Run this on the macOS or Linux machine hosting the Rex server:

```sh
curl -fsSL https://raw.githubusercontent.com/fky2015/rex-extrakto/main/scripts/install.sh | sh
```

The installer downloads the matching package from [GitHub Releases](https://github.com/fky2015/rex-extrakto/releases), verifies SHA-256 and initializes Rex config. Packages include all three Rust programs and fzf. Installation requires Rex CLI, curl and Python 3.8 or newer; normal extraction and picker operations do not use Python. Releases support x86_64 and ARM64 on both macOS and Linux.

When you invoke the plugin on another Rex server, it runs the picker and creates temporary files on that server. If the matching version is missing, the first invocation opens an installation terminal, installs that version from GitHub Releases and resumes the requested search. See [remote server setup](docs/installation.md#remote-server-setup).

See the [installation guide](docs/installation.md) for upgrades, fixed versions and offline installation. Existing bindings and configuration are preserved.

### Build from source

Source builds require Rust 1.85 or newer:

```sh
cd ~/code/rex-extrakto
make build
```

Add the configuration from [examples/init.lua](examples/init.lua) to `~/.config/rex/init.lua`. The example assumes the project is checked out at `~/code/rex-extrakto`; adjust the loader path for a different location.

```lua
dofile(os.getenv("HOME") .. "/code/rex-extrakto/extrakto.lua")
rex.bind("cmd+shift+f", "rex_extrakto", { scope = "tab" })
rex.bind("alt+cmd+shift+e", "rex_extrakto_lines")
```

Check and reload the configuration:

```sh
rex config check --json
rex config reload --json
```

## Usage

| Key | Action |
| --- | --- |
| Cmd + Shift + F | Search words, paths and URLs from all terminal panes in the current tab |
| Option + Cmd + Shift + E | Search complete lines from the source pane |
| Tab | Copy the selected result and close the picker |
| Enter | Insert the selected result and close the picker |
| Esc | Cancel and close the picker |

fzf matches nonconsecutive characters: `extko` matches `https://github.com/laktak/extrakto`. Candidates show their source pane, while search, copy and insertion use only the candidate text. Enter always inserts into the pane that opened the picker, without an extra newline.

By default, each pane contributes candidates from its 3,000 most recent lines. The merged list contains at most 2,000 unique candidates. Candidates are taken from each pane in turn so that a busy pane cannot fill the entire list. Session results show both the source tab and pane. See the [configuration guide](docs/configuration.md) for scopes, extraction modes and limits.

Tab and session scopes use bounded Tokio concurrency, with `jobs=4` and `extract_jobs=1` by default. Set `jobs=1` for sequential processing. Capture and CPU extraction have separate concurrency limits, and results are merged in source pane order. On Linux, OSC 52 delegates copying to the connected Rex client; macOS uses `pbcopy` by default.

## Development

```sh
make test
make check
make test-integration
```

`make test` runs CLI tests and compares Rust output with the Python reference. `make test-integration` uses a local Rex server and fzf to verify copy, insert, cancel and all three extraction scopes in disposable sessions. Each test closes its sessions when finished.

Add `CARGO_FLAGS=--offline` for offline builds and tests. See the [development guide](docs/development.md) for commands, test coverage and implementation details.

## Project layout

```text
rex-extrakto/
├── Cargo.toml / Cargo.lock     Rust package and locked dependencies
├── Makefile                    Build and verification commands
├── extrakto.lua                Rex plugin entry point
├── src/
│   ├── extract.rs              Shared candidate extraction
│   ├── collect.rs              Tokio capture and CPU concurrency limits
│   ├── popup.rs                fzf, clipboard and insertion operations
│   ├── main.rs                 rex-extract CLI
│   └── bin/                    rex-collect and rex-popup CLIs
├── scripts/
│   ├── build.sh                Build and install helpers into bin/
│   ├── install.sh / install.py Release download and initialization
│   └── package.py              Release archives and checksums
├── examples/init.lua           Rex configuration example
├── docs/                       Installation, configuration and development guides
└── tests/
    ├── test_*.py               CLI, collector, installer, package and clipboard tests
    ├── check_equivalence.py    Python reference comparison
    ├── reference/              Python extraction reference for tests
    ├── helpers/                Terminal fixtures and raw input receivers
    └── integration/            Tests against a real Rex server
```

`bin/` contains locally built helpers. `target/` contains Cargo caches and test results. Both are generated and excluded by `.gitignore`. The Python extraction reference is used only in tests.

Inspired by [laktak/extrakto](https://github.com/laktak/extrakto). Rex interfaces are documented in the [official guide](https://www.superlogical.com/rex/docs).

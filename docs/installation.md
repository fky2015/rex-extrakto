# Installation and upgrades

Run this on the machine hosting the Rex server where you want to use the plugin:

```sh
curl -fsSL https://raw.githubusercontent.com/fky2015/rex-extrakto/main/scripts/install.sh | sh
```

The installer detects macOS/Linux and x86_64/ARM64, downloads the matching GitHub Release archive and verifies `SHA256SUMS`. Public releases use the Release page and direct download links, so installation does not require GitHub API access or a token. Each package includes `rex-extract`, `rex-collect`, `rex-popup`, fzf and the Lua plugin. Installation requires Rex CLI, curl and Python 3.8 or newer. Normal extraction, searching, copying and insertion run in Rust and fzf; Rust/Cargo and administrator access are not required.

The default module repository is `~/.config/rex/plugins/rex-extrakto/`, and prebuilt runtime releases live in `~/.local/share/rex-extrakto/`. The default configuration file is `~/.config/rex/init.lua`. `XDG_DATA_HOME` and `XDG_CONFIG_HOME` override their respective base directories. A nonempty `REX_CONFIG` selects the config file; explicit `--config` takes precedence over it.

With Git available, `--mode auto` installs the complete repository from `source.bundle` inside the verified archive, at the exact source commit used to build the release. The checkout's origin is the public GitHub repository; the installer checks out the release commit without contacting GitHub again. Dirty repositories are rejected without resetting or cleaning them. Without Git, auto mode installs the portable module entry. Use `--mode git` to require a repository or `--mode release` to select the module-only installation.

Rex config uses its native module search path, with no absolute loader path or `package.path` changes:

```lua
require("plugins.rex-extrakto").setup({ bind = false })
-- Custom bindings can go here.
require("plugins.rex-extrakto").bind_defaults()
```

Machine-specific runtime locations are recorded in `$XDG_STATE_HOME/rex-extrakto/installations.json`, defaulting to `~/.local/state/rex-extrakto/installations.json`. This registry selects an immutable release directory for each module installation. Updating the Git checkout alone does not switch active helpers; rerun the installer to hydrate the corresponding release.

The installer initializes the plugin loader, adds missing default bindings, checks the configuration and reloads Rex. Existing scope, concurrency settings and custom bindings are preserved. Default shortcuts already assigned to another action or a Lua function are left intact. Defaults are registered in a separate managed shortcuts block, after querying Rex's effective bindings. Put custom bindings outside that block so upgrades preserve them. An existing configuration is backed up to `init.lua.rex-extrakto.bak` before its first modification.

## Configuration

Set module defaults through `setup`. The scope defaults to `"tab"`; explicit action or binding arguments override these defaults:

```lua
require("plugins.rex-extrakto").setup({ scope = "session", jobs = 4, bind = false })
```

Default bindings:

```lua
rex.bind("cmd+shift+f", "rex_extrakto", { scope = "tab" })
rex.bind("alt+cmd+shift+e", "rex_extrakto_lines")
```

Adjust the binding arguments in `init.lua`:

```lua
rex.bind("cmd+shift+f", "rex_extrakto", {
  scope = "session",  -- session / tab / pane
  jobs = 4,
  extract_jobs = 1,
})
```

Then run `rex config reload --json`. See the [configuration guide](configuration.md) for all available options.

## Upgrades and fixed versions

Run the installation command again to install the latest release. To select a specific version:

```sh
curl -fsSL https://raw.githubusercontent.com/fky2015/rex-extrakto/main/scripts/install.sh | sh -s -- --version v0.1.11
```

Versioned files live in `releases/vX.Y.Z/`, and `current` points to the active version. The module resolves a specific release directory before registering actions, so actions already running continue using their helpers while the source repository updates. Upgrades retain older versions. Reinstalling the same version is idempotent and does not duplicate configuration or bindings.

If files in an installed version are missing, modified or no longer executable, the installer repairs them from the verified archive and backs up the old directory as `releases/vX.Y.Z.bak`. A failed configuration check or reload restores the previous configuration, module entry, registry, Git checkout and active version. Existing configuration symlinks are preserved.

## Machines with restricted network access

On a machine that can reach GitHub, download the target platform's `.tar.gz` archive and `SHA256SUMS` from [Releases](https://github.com/fky2015/rex-extrakto/releases). Transfer them and `scripts/install.py` to the target machine.

For example, on Linux x86_64:

```sh
python3 install.py \
  --archive rex-extrakto-v0.1.11-x86_64-unknown-linux-musl.tar.gz \
  --checksum-file SHA256SUMS
```

The archive includes fzf, so this command runs offline. You can also use `scripts/install.py` from inside the extracted archive. Linux helpers are built with musl to avoid depending on the target machine's glibc version.

## Remote server setup

Rex forwards API calls between servers, but it does not distribute this plugin's files or binaries. rex-extrakto checks the target server's registered runtime before opening a picker. The target runs its own Lua action, uses its own native programs and stores candidate state in its own temporary directory.

If the target has no plugin or a different version, the first invocation opens an installation terminal there. It downloads the bootstrap from the calling plugin's release tag, installs the corresponding package with SHA-256 verification, checks and reloads the target's config, and resumes the original action with its source pane and arguments. The target needs the same installation prerequisites and access to GitHub raw files and release downloads. Failed setup leaves terminal output available for inspection; use the offline instructions above if downloads are blocked.

`preview=true` never installs software. It returns an error when the matching target runtime is missing. To prepare a server explicitly, run the normal installer on that machine before requesting a preview.

Old version directories can still contain the Python picker used by releases before v0.1.3. New packages contain `bin/rex-popup` and no `scripts/popup.py`; the installer retains support for older package layouts.

## Installer options

| Option | Purpose |
| --- | --- |
| `--version vX.Y.Z` | Select a release; defaults to latest |
| `--install-dir PATH` | Set the installation directory |
| `--config PATH` | Set the Rex init.lua path |
| `--plugin-dir PATH` | Set the module directory inside the Rex config directory |
| `--mode auto\|git\|release` | Choose Git source plus prebuilt runtime, or the module-only installation |
| `--no-reload` | Write and check config; reload Rex later |
| `--no-config` | Install files without editing Rex config |
| `--archive PATH --checksum-file PATH` | Install from a local release archive |

macOS uses `pbcopy` by default. Linux uses OSC 52 to copy through the connected Rex client. When viewing a remote macOS server, set `clipboard = "osc52"` to send copied text to the client.

## Removal

Remove the marked loader and managed shortcuts blocks, and any plugin bindings you no longer need from Rex config, then run `rex config reload --json`. Once the plugin is no longer in use, delete its module directory and runtime installation directory, and remove its entry from the state registry. Preserve the rest of your Rex configuration.

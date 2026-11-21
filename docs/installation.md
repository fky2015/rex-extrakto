# 安装与升级

在需要使用插件的 Rex server 所在机器上运行：

```sh
curl -fsSL https://raw.githubusercontent.com/fky2015/rex-extrakto/main/scripts/install.sh | sh
```

安装器识别 macOS/Linux 和 x86_64/ARM64，从 GitHub Release 下载对应包并核对 `SHA256SUMS`。发行包包含两个已编译的 Rust 程序、fzf、Lua 插件和选框脚本；机器只需已有 Rex CLI、Python 3.8 或更新版本。无需 Rust/Cargo，也无需管理员权限。

默认安装在 `~/.local/share/rex-extrakto/`，配置在 `~/.config/rex/init.lua`。设置了 `XDG_DATA_HOME` 或 `XDG_CONFIG_HOME` 时使用相应目录。

安装器会初始化插件 loader 和缺少的默认绑定，校验配置并 reload Rex。已有的 scope、jobs 和自定义按键绑定会保留；被其他功能占用的默认快捷键不会被覆盖。配置首次改动前保存在 `init.lua.rex-extrakto.bak`。

## 配置

安装后的默认绑定为：

```lua
rex.bind("cmd+shift+f", "rex_extrakto", { scope = "tab" })
rex.bind("alt+cmd+shift+e", "rex_extrakto_lines")
```

可以在 `init.lua` 调整绑定参数：

```lua
rex.bind("cmd+shift+f", "rex_extrakto", {
  scope = "session",  -- session / tab / pane
  jobs = 4,
  extract_jobs = 1,
})
```

然后执行 `rex config reload --json`。详细参数见 [配置说明](configuration.md)。

## 升级与固定版本

再次执行安装命令会安装最新 release。固定版本可以使用：

```sh
curl -fsSL https://raw.githubusercontent.com/fky2015/rex-extrakto/main/scripts/install.sh | sh -s -- --version v0.1.0
```

版本文件保存在 `releases/vX.Y.Z/`，`current` 指向当前安装版本。配置 loader 指向具体版本，正在运行的旧 action 可以继续使用对应程序。升级保留旧版本；重新安装相同版本不会重复添加配置或快捷键。

如果同版本的文件缺失、被修改或失去执行权限，安装器会从校验后的发行包修复，原目录保存在 `releases/vX.Y.Z.bak`。配置检查或 reload 失败会恢复原配置和当前版本；已有的配置符号链接会保留。

## 网络受限的机器

在能访问 GitHub 的机器上，从 [Releases](https://github.com/fky2015/rex-extrakto/releases) 下载目标平台的 `.tar.gz` 和 `SHA256SUMS`，连同 `scripts/install.py` 传到目标机器。

例如，Linux x86_64：

```sh
python3 install.py \
  --archive rex-extrakto-v0.1.0-x86_64-unknown-linux-musl.tar.gz \
  --checksum-file SHA256SUMS
```

发行包已经包含 fzf，这条命令不需要访问 GitHub。也可以从压缩包中的 `scripts/install.py` 运行初始化。Linux 程序使用 musl 构建，避免依赖目标机器的 glibc 版本。

GitHub API 限流时，可通过已有的 `GITHUB_TOKEN` 或 `GH_TOKEN` 环境变量运行安装器，也可改用本地发行包。

## 安装选项

| 参数 | 用途 |
| --- | --- |
| `--version vX.Y.Z` | 固定 release，默认 latest |
| `--install-dir PATH` | 指定安装目录 |
| `--config PATH` | 指定 Rex init.lua |
| `--no-reload` | 写入并校验配置，稍后自行 reload |
| `--no-config` | 只安装文件，不修改 Rex 配置 |
| `--archive PATH --checksum-file PATH` | 从本地发行包安装 |

macOS 默认用 `pbcopy`；Linux 默认通过 OSC 52 让 Rex 客户端复制。查看远程 macOS server 时，也可配置 `clipboard = "osc52"`，将复制交给当前客户端。

## 移除

从 Rex 配置中移除标记的 loader 和需要移除的 `rex_extrakto` 绑定，再运行 `rex config reload --json`。确认不再使用后可删除安装目录。其他 Rex 配置不需要改动。

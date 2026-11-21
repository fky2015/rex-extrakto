# rex-extrakto

为 Rex 提取终端文本，用 fzf 模糊搜索后复制或插入。Cmd + Shift + F 默认搜索当前 tab 的所有终端 pane，包括浮动终端。候选由 Rust 提取，Lua 负责 Rex 操作，Python 负责运行 fzf 选框。

提取范围可配置为 `scope = "session" | "tab" | "pane"`，分别读取整个 session 的所有 tab、当前 tab 或当前 pane。

## 安装

在运行 Rex server 的 macOS 或 Linux 机器上执行：

```sh
curl -fsSL https://raw.githubusercontent.com/fky2015/rex-extrakto/main/scripts/install.sh | sh
```

安装器从 [GitHub Releases](https://github.com/fky2015/rex-extrakto/releases) 下载匹配平台的发行包并校验 SHA-256，初始化 Rex 配置。发行包自带 Rust 程序和 fzf；只需已有 Rex CLI 和 Python 3.8 或更新版本，无需 Rust 工具链。支持 macOS/Linux 的 x86_64 和 ARM64。

升级、固定版本和从本地下载包安装见 [安装说明](docs/installation.md)。安装器保留已有快捷键与配置。

从源码开发时需要 Rust 1.85 或更新版本：

```sh
cd ~/code/rex-extrakto
make build
```

将 [examples/init.lua](examples/init.lua) 中的配置加入 `~/.config/rex/init.lua`。下面假设项目位于 `~/code/rex-extrakto`；如果放在其他目录，请调整 `dofile` 的路径。

```lua
dofile(os.getenv("HOME") .. "/code/rex-extrakto/extrakto.lua")
rex.bind("cmd+shift+f", "rex_extrakto", { scope = "tab" })
rex.bind("alt+cmd+shift+e", "rex_extrakto_lines")
```

检查并加载配置：

```sh
rex config check --json
rex config reload --json
```

## 使用

| 按键 | 操作 |
| --- | --- |
| Cmd + Shift + F | 搜索当前 tab 所有终端 pane 的单词、路径和 URL |
| Option + Cmd + Shift + E | 搜索当前 pane 的完整行 |
| Tab | 复制选中结果，关闭选框 |
| Enter | 插入选中结果，关闭选框 |
| Esc | 取消，关闭选框 |

可以用不连续的字母搜索，例如 `extko` 能匹配 `https://github.com/laktak/extrakto`。候选旁显示来源 pane，搜索和复制只使用候选正文。Enter 始终向发起搜索的 pane 插入文本，不附加回车。

默认从每个 pane 最近 3,000 行中提取，跨 pane 去重后保留最多 2,000 个候选。合并时轮流取各 pane 的结果，避免一个大量输出的 pane 先占满列表。session 范围的结果会同时标明 tab 和 pane 来源。范围、提取类型和上限的设置见 [配置说明](docs/configuration.md)。

session/tab 范围使用 Tokio 并发捕获，默认 `jobs=4`、`extract_jobs=1`；`jobs=1` 可顺序处理。异步 I/O 和 CPU 提取分别限流，结果按原 pane 顺序合并。Linux 通过 OSC 52 让当前 Rex 客户端复制，macOS 默认使用 `pbcopy`。

## 开发

```sh
make test
make check
make test-integration
```

`make test` 运行 Rust CLI 测试和 Python 行为对照，不需要 Rex server。`make test-integration` 需要本机 Rex server 和 fzf，会创建独立测试 session，验证复制、插入、取消，以及 pane、tab、session 三种提取范围，结束后关闭测试 session。

离线构建和测试可加 `CARGO_FLAGS=--offline`。完整命令、测试范围和实现说明见 [开发说明](docs/development.md)。

## 目录

```text
rex-extrakto/
├── Cargo.toml / Cargo.lock   Rust 包和锁定依赖
├── Makefile                  构建与验证命令
├── extrakto.lua              Rex 插件入口
├── src/
│   ├── extract.rs            共享的候选提取逻辑
│   ├── collect.rs            Tokio 异步批量捕获与 CPU 限流
│   ├── main.rs               rex-extract CLI
│   └── bin/rex-collect.rs    rex-collect CLI
├── scripts/
│   ├── build.sh              构建并安装到 bin/
│   ├── popup.py              fzf、剪贴板和插入操作
│   ├── install.sh / install.py 发行包下载安装与初始化
│   └── package.py            跨平台打包与校验文件
├── examples/init.lua         Rex 配置示例
├── docs/                     配置与开发说明
└── tests/
    ├── test_extract.py       Rust CLI 测试
    ├── check_equivalence.py  Python 行为对照检查
    ├── reference/            仅供测试的 Python 实现
    ├── helpers/              终端数据和原始输入接收器
    └── integration/          真实 Rex 集成测试
```

`bin/` 存放本机构建的运行程序；`target/` 存放 Cargo 缓存和测试结果，均由工具生成并通过 `.gitignore` 排除。Python 对照实现不参与插件运行。

项目参考了 [laktak/extrakto](https://github.com/laktak/extrakto)。Rex 接口见 [官方文档](https://www.superlogical.com/rex/docs)。

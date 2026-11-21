# 开发与验证

## 文件职责

`extrakto.lua` 是稳定的插件入口，按自身位置查找程序和选框脚本，不依赖调用时的工作目录。它根据 `scope=session/tab/pane` 确定源终端，合并候选并创建浮动选框。

`src/extract.rs` 保存共享的候选规则；`src/main.rs` 提供 `rex-extract` CLI。`src/collect.rs` 使用 Tokio 异步捕获，`src/bin/rex-collect.rs` 提供批量 JSON CLI。两个程序复用同一份提取逻辑。

批量捕获复用 Rex CLI 的分页和软折行能力；`futures-util::buffer_unordered` 限制在途 pane 任务数。CPU 工作通过 Tokio `spawn_blocking` 和 Semaphore 单独限流，不阻塞 I/O runtime。结果按输入顺序还原；超时或失败会取消剩余捕获，子进程使用 `kill_on_drop`。每次提取只创建一个 collector 进程并编译一次 Regex。

`scripts/popup.py` 运行 fzf，并根据确认按键执行 `pbcopy`、OSC 52 或 Rex paste 事件。Python 不承担候选提取或并发调度。

选框通过终端标题标识自身，便于 Lua 独立加载或配置 reload 后继续排除已打开的选框。普通同名 pane 不会仅凭标签被排除；进程查询用于兼容没有标题标记的旧选框。

`tests/reference/extract.py` 保留迁移前的候选规则，作为 Rust 输出的对照。测试数据和原始输入接收器放在 `tests/helpers/`；实际 Rex 操作的检查放在 `tests/integration/`。

## 构建

```sh
make build
make build CARGO_FLAGS=--offline
```

也可从任意工作目录执行 `scripts/build.sh`。脚本使用 `cargo build --release --locked`，逐个原子替换 `bin/rex-extract` 和 `bin/rex-collect`。依赖由 `Cargo.lock` 锁定；离线构建要求依赖已缓存。源码构建需要 Rust 1.85 或更新版本。

本地构建产物用于当前机器的原生架构。release workflow 构建四个平台，Linux 使用 musl；选框在 macOS 默认用 pbcopy，在 Linux 通过 OSC 52 将复制交给 Rex 客户端。

手动检查提取程序：

```sh
printf '%s\n' 'https://github.com/laktak/extrakto' | bin/rex-extract --mode url
bin/rex-extract --help
```

## 不依赖 Rex 的测试

```sh
make test
make check
```

`make test` 先构建运行程序，再运行 CLI、异步捕获、初始化安装和剪贴板协议测试，以及 240 组 JSON、24 组 NUL 记录的行为对照。异步测试使用可控的捕获进程，验证并发上限、顺序、超时、失败取消和 stdout/stderr 排空，不需要 Rex server。

`make check` 运行 `cargo fmt --check` 和 Clippy，并将 Clippy warning 视为失败。Cargo 命令可加 `CARGO_FLAGS=--offline`；测试 Python 可通过 `PYTHON=/path/to/python3` 指定。

直接执行测试时使用以下入口：

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/check_equivalence.py
```

## Rex 集成测试

本机需要运行 Rex server，并能找到 Python 3、fzf、`pbcopy` 和 `pbpaste`：

```sh
make test-integration
```

集成测试直接加载当前项目的 `extrakto.lua`，不要求预先修改用户的 `init.lua`。每项测试创建独立 session，使用指定 pane ID 操作，结束后关闭该测试 session。输入接收器只记录原始字节，不执行收到的内容。

| 检查 | 范围 |
| --- | --- |
| `tests/integration/check_picker.py` | 真实 capture、剪贴板、Tab 复制、Enter 插入准确 UTF-8 文本、Esc 取消 |
| `tests/integration/check_tab.py` | 四个源 pane（含浮动 pane）、其他 tab 排除、选框排除、全局去重、候选轮流取样、插入目标 |
| `tests/integration/check_session.py` | 三种范围边界、跨 tab 去重、其他 session 排除、来源覆盖、跨 tab 选框排除、选框与插入目标固定在源 tab/pane |
| `tests/integration/check_osc52.py` | 真实 fzf 选框发出 OSC 52，Rex 接收到准确 UTF-8 文本 |

`check_picker.py` 会调用 `check_clipboard.py`，验证 URL 软折行、路径和中文的准确复制。也可向它传入专用 demo pane：

```sh
python3 tests/integration/check_clipboard.py SESSION DEMO_BLOCK
```

测试结束时，仅在剪贴板仍是该测试写入的内容时恢复原值。集成测试应按顺序运行，避免同时使用剪贴板。

测试结果生成于 `target/test-results/`：

```text
target/test-results/
├── equivalence.json
├── picker/              复制、插入、取消结果与接收到的字节
├── tab/                 多 pane 夹具、接收字节与检查结果
└── session/             跨 tab 夹具、接收字节与检查结果
```

构建缓存、二进制、日志和测试结果均不纳入源码。旧实验记录和整理前备份已移到项目外归档。

## 修改时需要保持的行为

- Rust 输出与 Python 对照规则一致，完整 URL、Unicode 和内部空白保持准确。
- fzf 只匹配候选正文，来源标注不进入复制或插入内容。
- Enter 向发起搜索的 pane 发送 literal paste，不附加回车。
- tab 范围按源 pane 所属 window 确定，并排除插件自己的选框。
- session 范围只读取源 session 所有 tab 的终端 pane，所有范围都向源 pane 插入。
- 单个 pane 的大量输出不能因拼接顺序占满所有候选。

## 发布

GitHub Actions 的 CI 校验 Rust 1.85 最低版本、测试和 Clippy。推送匹配 Cargo 版本的 `vX.Y.Z` tag 后，release workflow 构建并验证四个平台，打包 Rust 程序、固定版本 fzf 和依赖许可证，生成 `SHA256SUMS`。所有构建成功后先创建 draft release，上传全部资产，再发布。

本机 macOS ARM64 可检查打包流程：

```sh
make build
python3 scripts/package.py --target aarch64-apple-darwin
```

`scripts/package.py` 使用已构建的程序。Linux release 必须先为对应 musl target 构建，并通过 `--binary-dir` 指定目录；脚本检查架构和静态链接。归档的时间戳、权限和目录顺序会标准化。

release 包由 `scripts/install.py` 初始化，支持 SHA-256 校验、版本目录、配置保留和离线安装。正常使用说明见 [安装说明](installation.md)。

Rex prompt 必须从 action 的 `run` 调用。独立试验使用 `rex do FILE --action ACTION`，新增快捷键绑定到 action 名称。接口细节以当前安装的 `rex --help`、`rex api describe` 和官方文档为准。

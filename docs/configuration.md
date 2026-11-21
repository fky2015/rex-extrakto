# 配置与命令

## Action 与范围

| Action | 默认提取类型 | 默认范围 |
| --- | --- | --- |
| `rex_extrakto` | 单词、路径和 URL | 源 pane |
| `rex_extrakto_lines` | 完整行 | 源 pane |
| `rex_extrakto_tab` | 单词、路径和 URL | 源 pane 所属 tab |

三个 action 都接受 `scope=session/tab/pane`：

| scope | 提取范围 |
| --- | --- |
| `session` | 源 session 所有 tab 的终端 pane |
| `tab` | 源 pane 所属 tab 的所有终端 pane |
| `pane` | 仅源 pane |

Cmd + Shift + F 的默认范围仍为 tab，由绑定参数设置：

```lua
rex.bind("cmd+shift+f", "rex_extrakto", { scope = "tab" })
```

改成 `scope = "session"` 可搜索整个 session，改成 `scope = "pane"` 可只搜索当前 pane。完整行 action 同样支持三种范围，例如给 `rex_extrakto_lines` 传入 `scope = "session"` 可搜索所有 tab 的完整行。

session 和 tab 范围包括相应 tab 中平铺和浮动的终端 pane，并排除本插件的选框。session 范围不会读取其他 session，也不包含未放入 tab 的 detached block。tab 范围按发起搜索的 pane 所属 tab 确定。

结果跨 pane 去重，旁边显示来源；session 范围同时标明 tab 和 pane，便于区分不同 tab 的同名 pane。Enter 的插入目标始终是发起搜索的 pane。即使 session 当前焦点在另一个 tab，选框仍在源 pane 所属 tab 打开。

## 提取参数

| 参数 | 含义 |
| --- | --- |
| `mode=word/line/path/url/quote` | 提取类型；省略时使用 action 的默认类型 |
| `scope=session/tab/pane` | 源 session 的所有 tab、源 tab，或仅源 pane |
| `query=extko` | 初始搜索词；默认留空 |
| `min_length=3` | 最小字符数，必须大于 0 |
| `max_lines=3000` | 每个 pane 最近多少行，必须大于 0 |
| `max_items=2000` | 每个 pane 和最终合并列表的候选上限，支持 1–2,000 |
| `jobs=4` | session/tab 同时处理的 pane 任务上限，支持 1–32；1 为顺序处理 |
| `extract_jobs=1` | CPU 提取并发上限，支持 1–jobs；通过 Tokio blocking pool 执行 |
| `clipboard=auto/pbcopy/osc52` | 默认 macOS 用 pbcopy，Linux 用 OSC 52；远程 macOS 可指定 osc52 |

例如，调整每个 pane 的行数和合并候选上限：

```lua
rex.bind("cmd+shift+f", "rex_extrakto", {
  scope = "tab",
  max_lines = 10000,
  max_items = 2000,
  jobs = 4,
  extract_jobs = 1,
})
```

每个 pane 内优先保留最近出现的候选；跨 pane 合并时，从发起搜索的 pane 开始轮流取样。提取行数只限制候选处理范围，插件仍会先 capture Rex 保留的全部文本。增大 `max_lines` 会增加提取工作量，多个 pane 的历史也会增加捕获耗时。

session/tab 的捕获由 Tokio 异步 I/O 并发执行，提取通过 `spawn_blocking` 和 Semaphore 单独限流。完成顺序不会改变候选顺序；整个 pane 任务的并发上限也限制同时保留的原始历史份数。pane 范围只有一个来源，不使用批量调度。

Rex 已经丢弃的 scrollback 无法恢复。全屏程序使用 alternate screen 时，捕获内容由该终端当前可用的屏幕决定。

## 从 CLI 调用

从项目目录运行；session 和 block ID 必须仍然存在：

```sh
rex do ./extrakto.lua --action rex_extrakto_tab -s "$REX_SESSION" -b "$REX_BLOCK"
rex do ./extrakto.lua --action rex_extrakto -s "$REX_SESSION" -b "$REX_BLOCK" scope=session
rex do ./extrakto.lua --action rex_extrakto_lines -s "$REX_SESSION" -b "$REX_BLOCK"
rex do ./extrakto.lua --action rex_extrakto -s "$REX_SESSION" -b "$REX_BLOCK" scope=pane query=extko
```

旧 shell 中的 Rex ID 可能过期，可用 `rex whoami --json`、`rex ls --json` 和 `rex block ls -s SESSION --json` 查找当前 ID。

## 调试参数

| 参数 | 含义 |
| --- | --- |
| `preview=true` | 返回候选 JSON，不打开选框 |
| `select=TEXT` | 直接操作一个确实存在的候选，用于自动化检查 |
| `operation=copy/insert` | `select` 或原生界面的操作；`select` 默认 copy，原生默认 insert |
| `ui=fzf/auto/native` | 默认 fzf；auto 也使用 fzf；native 为实验入口 |
| `source_server=HOST` | 从指定 Rex host 读取源终端 |
| `source_session=SESSION source_block=BLOCK` | 指定源 pane，覆盖调用上下文 |
| `result_file=PATH` | 将选框操作结果写入文件，仅用于显式试验 |
| `keep_open=true` | 保留退出后的选框终端，用于排错 |

示例：查看候选而不打开选框。

```sh
rex do ./extrakto.lua --action rex_extrakto_tab -s "$REX_SESSION" -b "$REX_BLOCK" preview=true --json
```

结果的 `scope` 表示实际范围。session 和 tab 范围还含 `window_id`、`panes` 和 `skipped_panes`；`window_id` 指源 pane 所属 tab，各 pane 的 `window_id` 和 `window_label` 标明各自来源。`source_lines` 是各 pane 实际参与提取的行数之和；`limited=true` 表示候选发生截断。候选显示字段和准确文本分别保存在 `items` 和 `values` 中。

正常快捷键调用不会写 `result_file`。临时候选数据存于系统临时目录，选框退出后删除。

## 界面与远程来源

fzf 支持区分 Tab 复制和 Enter 插入。原生 `rex.ui.pick` 只返回候选 ID，并要求兼容的 Rex server 和 app，因此 `ui=native` 只作为单操作试验入口保留。

通过 `rex do` 调用时，辅助命令运行在 CLI 所在机器；通过 `init.lua` 调用时，辅助命令运行在 server 所在机器。发行包在每个 server 上安装脚本和对应架构的程序。Linux 的 fzf 选框通过 OSC 52 将复制交给连接的 Rex 客户端；查看远程 macOS server 时可指定 `clipboard=osc52`。

`select` 和原生界面的直接复制仍依赖 `pbcopy`，OSC 52 复制使用 fzf 选框。当前真实 Rex 集成测试覆盖本机和 OSC 52 协议，跨平台构建与安装由 CI 验证。

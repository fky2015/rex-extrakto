use anyhow::{bail, Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use futures_util::future::join;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Output, Stdio};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

#[derive(Default, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Clipboard {
    #[default]
    Auto,
    Pbcopy,
    Osc52,
}

#[derive(Deserialize)]
struct Item {
    id: String,
    title: String,
    subtitle: Option<String>,
}

#[derive(Deserialize)]
struct Target {
    server: Option<String>,
    session_id: String,
    block_id: String,
}

#[derive(Deserialize)]
struct RestoreTarget {
    server: Option<String>,
    session_id: String,
    window_id: String,
    block_id: String,
}

#[derive(Deserialize)]
struct WatchTarget {
    server: Option<String>,
    session_id: String,
    window_id: String,
}

#[derive(Deserialize)]
struct WatchState {
    rex: PathBuf,
    watch: WatchTarget,
    result_file: Option<PathBuf>,
    restore_zoom: Option<RestoreTarget>,
    restore_focus: Option<RestoreTarget>,
}

#[derive(Deserialize)]
struct State {
    items: Vec<Item>,
    values: HashMap<String, String>,
    fzf: PathBuf,
    rex: PathBuf,
    target: Target,
    #[serde(default)]
    query: String,
    scope: Option<String>,
    #[serde(default)]
    clipboard: Clipboard,
    result_file: Option<PathBuf>,
    restore_zoom: Option<RestoreTarget>,
    restore_focus: Option<RestoreTarget>,
    watch: Option<WatchTarget>,
}

#[derive(Serialize)]
struct Outcome<'a> {
    status: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    text: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    count: Option<usize>,
}

struct Cleanup<'a>(&'a Path);

impl Drop for Cleanup<'_> {
    fn drop(&mut self) {
        let _ = fs::remove_file(self.0);
        if let Some(parent) = self.0.parent() {
            // Remove only an empty directory; never delete another file.
            let _ = fs::remove_dir(parent);
        }
    }
}

struct WatchCleanup<'a>(&'a Path);

impl Drop for WatchCleanup<'_> {
    fn drop(&mut self) {
        for extension in ["ready", "block", "finished"] {
            let _ = fs::remove_file(self.0.with_extension(extension));
        }
        drop(Cleanup(self.0));
    }
}

fn display(value: &str) -> String {
    value
        .replace('\t', " ⇥ ")
        .replace(['\n', '\r'], " ⏎ ")
        .replace('\0', "␀")
}

fn records(state: &State) -> Result<Vec<u8>> {
    let mut input = Vec::new();
    for item in &state.items {
        if item.id.is_empty() || item.id.contains(['\t', '\0', '\n', '\r']) {
            bail!("candidate ID contains a record separator");
        }
        if !state.values.contains_key(&item.id) {
            bail!("candidate ID has no exact value");
        }
        input.extend_from_slice(item.id.as_bytes());
        input.push(b'\t');
        input.extend_from_slice(display(&item.title).as_bytes());
        if let Some(subtitle) = item.subtitle.as_ref().filter(|value| !value.is_empty()) {
            input.extend_from_slice(b"\t[");
            input.extend_from_slice(display(subtitle).as_bytes());
            input.push(b']');
        }
        input.push(0);
    }
    Ok(input)
}

async fn communicate(command: &mut Command, input: &[u8]) -> Result<(Output, io::Result<()>)> {
    command.stdin(Stdio::piped()).stdout(Stdio::piped());
    command.kill_on_drop(true);
    let mut child = command.spawn().context("could not start command")?;
    let mut stdin = child.stdin.take().context("command has no stdin pipe")?;
    let write = async {
        let result = stdin.write_all(input).await;
        drop(stdin);
        result
    };
    // Drain output while supplying input, including when fzf selects early.
    let (written, output) = join(write, child.wait_with_output()).await;
    Ok((output.context("could not wait for command")?, written))
}

async fn copy(value: &str, backend: &Clipboard) -> Result<()> {
    let use_pbcopy = matches!(backend, Clipboard::Pbcopy)
        || (matches!(backend, Clipboard::Auto) && cfg!(target_os = "macos"));
    if use_pbcopy {
        let mut command = Command::new("/usr/bin/pbcopy");
        let (output, written) = communicate(&mut command, value.as_bytes()).await?;
        written.context("could not write clipboard text")?;
        if !output.status.success() {
            bail!("pbcopy exited with {}", output.status);
        }
    } else {
        let mut stdout = io::stdout().lock();
        write!(
            stdout,
            "\x1b]52;c;{}\x07",
            STANDARD.encode(value.as_bytes())
        )?;
        stdout.flush()?;
    }
    Ok(())
}

async fn rex_call(
    rex: &Path,
    server: Option<&str>,
    method: &str,
    request: serde_json::Value,
) -> Result<Vec<u8>> {
    let mut command = Command::new(rex);
    if let Some(server) = server.filter(|value| !value.is_empty()) {
        command.args(["-S", server]);
    }
    command.args(["api", "call", method, &serde_json::to_string(&request)?]);
    command.kill_on_drop(true);
    let output = tokio::time::timeout(Duration::from_secs(30), command.output())
        .await
        .with_context(|| format!("Rex {method} timed out"))?
        .with_context(|| format!("could not start Rex {method}"))?;
    if !output.status.success() {
        bail!(
            "Rex {method} failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(output.stdout)
}

async fn insert(value: &str, state: &State) -> Result<()> {
    rex_call(
        &state.rex,
        state.target.server.as_deref(),
        "block.event",
        serde_json::json!({
            "session_id": state.target.session_id,
            "block_id": state.target.block_id,
            "event": { "type": "paste", "payload": { "text": value } },
        }),
    )
    .await?;
    Ok(())
}

async fn restore_source(state: &State) -> Result<bool> {
    let Some(target) = state.restore_zoom.as_ref().or(state.restore_focus.as_ref()) else {
        return Ok(true);
    };
    let Ok(block) = std::env::var("REX_BLOCK") else {
        return Ok(true);
    };
    let output = rex_call(
        &state.rex,
        target.server.as_deref(),
        "session.view",
        serde_json::json!({ "session_id": target.session_id }),
    )
    .await?;
    let view: serde_json::Value = serde_json::from_slice(&output)?;
    let picker_exists = view["windows"].as_array().is_some_and(|windows| {
        windows
            .iter()
            .any(|window| window_has_block(window, &block))
    });
    restore_in_view(
        &state.rex,
        state.restore_zoom.as_ref(),
        state.restore_focus.as_ref(),
        &view,
        &block,
        false,
    )
    .await?;
    Ok(picker_exists)
}

async fn restore_in_view(
    rex: &Path,
    zoom: Option<&RestoreTarget>,
    focus: Option<&RestoreTarget>,
    view: &serde_json::Value,
    picker: &str,
    focused_before_close: bool,
) -> Result<()> {
    let Some(target) = zoom.or(focus) else {
        return Ok(());
    };
    let Some(windows) = view["windows"].as_array() else {
        return Ok(());
    };
    let Some(window) = windows
        .iter()
        .find(|window| window["window_id"].as_str() == Some(&target.window_id))
    else {
        return Ok(());
    };
    let source_exists = window_has_block(window, &target.block_id);
    let picker_exists = window_has_block(window, picker);
    let owns_focus = window["window_id"] == view["active_window_id"]
        && (window["focused_block_id"].as_str() == Some(picker)
            || (focused_before_close && !picker_exists));
    // Respect a focus change, closed source, or source moved to another tab.
    if source_exists && owns_focus {
        let mut request = serde_json::json!({
            "session_id": target.session_id,
            "block_id": target.block_id,
        });
        let method = if zoom.is_some() {
            request["zoom"] = serde_json::Value::Bool(true);
            "session.zoom_block"
        } else {
            "session.focus_block"
        };
        rex_call(rex, target.server.as_deref(), method, request).await?;
    }
    Ok(())
}

fn window_has_block(window: &serde_json::Value, block: &str) -> bool {
    window["layers"].as_array().is_some_and(|layers| {
        layers.iter().any(|layer| {
            layer["blocks"].as_array().is_some_and(|blocks| {
                blocks
                    .iter()
                    .any(|candidate| candidate["block_id"].as_str() == Some(block))
            })
        })
    })
}

pub async fn wait_ready(path: &Path) -> Result<()> {
    tokio::time::timeout(Duration::from_secs(10), async {
        while !path.with_extension("ready").is_file() {
            if !path.is_file() {
                bail!("picker lifecycle watcher exited before it was ready");
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        Ok(())
    })
    .await
    .context("picker lifecycle watcher did not become ready")?
}

/// Outlive the terminal so closing its pane cannot bypass cleanup and restoration.
pub async fn watch(path: &Path) -> Result<()> {
    let _cleanup = WatchCleanup(path);
    // Ignore candidate arrays and values: only the picker retains that text.
    let state: WatchState = serde_json::from_reader(fs::File::open(path)?)?;
    let target = &state.watch;
    let mut command = Command::new(&state.rex);
    if let Some(server) = target.server.as_deref().filter(|value| !value.is_empty()) {
        command.args(["-S", server]);
    }
    command
        .args([
            "--autostart=false",
            "events",
            "-s",
            &target.session_id,
            "--json",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let mut child = command
        .spawn()
        .context("could not start Rex event stream")?;
    let mut events =
        BufReader::new(child.stdout.take().context("event stream has no stdout")?).lines();
    let result = watch_events(&state, path, &mut events).await;
    // Reap the streaming CLI on every exit, including errors and pane closure.
    let _ = child.kill().await;
    result
}

async fn watch_events(
    state: &WatchState,
    path: &Path,
    events: &mut tokio::io::Lines<impl tokio::io::AsyncBufRead + Unpin>,
) -> Result<()> {
    let target = &state.watch;
    // Receiving a session event proves subscription started before the picker opens.
    tokio::time::timeout(Duration::from_secs(10), async {
        while let Some(line) = events.next_line().await? {
            let event: serde_json::Value = serde_json::from_str(&line)?;
            if event["type"] == "session_event"
                && event["payload"]["session_id"].as_str() == Some(&target.session_id)
            {
                return Ok(());
            }
        }
        bail!("Rex event stream ended before subscription")
    })
    .await
    .context("Rex event subscription timed out")??;
    fs::write(path.with_extension("ready"), b"ready\n")?;
    let picker = tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            if let Ok(block) = fs::read_to_string(path.with_extension("block")) {
                return block;
            }
            if !path.is_file() {
                return String::new();
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .context("picker block was not assigned")?;
    if picker.is_empty() {
        return Ok(());
    }
    let mut owns_focus = true; // The creation request focuses this picker.
    let mut closed = false;
    while let Some(line) = events.next_line().await? {
        if path.with_extension("finished").is_file() {
            return Ok(()); // A normal exit already checked and restored focus.
        }
        let event: serde_json::Value = serde_json::from_str(&line)?;
        if event["type"] != "session_event"
            || event["payload"]["session_id"].as_str() != Some(&target.session_id)
        {
            continue;
        }
        let event = &event["payload"]["event"];
        let payload = &event["payload"];
        match event["type"].as_str() {
            Some("block_closed") if payload["block_id"].as_str() == Some(&picker) => {
                closed = true;
            }
            Some("active_window_changed")
                if payload["window_id"].as_str() != Some(&target.window_id) =>
            {
                owns_focus = false;
            }
            Some("session_view_changed") => {
                let view: serde_json::Value = serde_json::from_slice(
                    &rex_call(
                        &state.rex,
                        target.server.as_deref(),
                        "session.view",
                        serde_json::json!({"session_id": target.session_id}),
                    )
                    .await?,
                )?;
                if closed {
                    // Do not undo a newer focus/layout change made after the close.
                    if view["revision"] == payload["revision"] {
                        restore_in_view(
                            &state.rex,
                            state.restore_zoom.as_ref(),
                            state.restore_focus.as_ref(),
                            &view,
                            &picker,
                            owns_focus,
                        )
                        .await?;
                    }
                    if let Some(result) = &state.result_file {
                        fs::write(result, b"{\"status\":\"cancelled\"}\n")?;
                    }
                    return Ok(());
                }
                if let Some(window) = view["windows"].as_array().and_then(|windows| {
                    windows
                        .iter()
                        .find(|window| window["window_id"].as_str() == Some(&target.window_id))
                }) {
                    // A later snapshot can already contain the close. Preserve the
                    // last observed focus until its block_closed event is handled.
                    if window_has_block(window, &picker) {
                        owns_focus = view["active_window_id"] == window["window_id"]
                            && window["focused_block_id"].as_str() == Some(&picker);
                    }
                }
            }
            _ => {}
        }
    }
    Ok(())
}

pub async fn run(path: &Path) -> Result<()> {
    let _cleanup = Cleanup(path);
    // The terminal title identifies pickers across config reloads.
    {
        let mut stdout = io::stdout().lock();
        stdout.write_all(b"\x1b]2;rex-extrakto\x07")?;
        stdout.flush()?;
    }
    let state: State =
        serde_json::from_reader(fs::File::open(path).context("could not open picker state")?)
            .context("could not decode picker state")?;
    let outcome = pick(&state).await;
    match restore_source(&state).await {
        Ok(true) if state.watch.is_some() => {
            // Removing state alone is insufficient: PTY closure can make fzf
            // fail and run Drop after the picker has already left the layout.
            fs::write(path.with_extension("finished"), b"finished\n")?;
        }
        Err(error) => eprintln!("rex-popup: could not restore the source pane: {error:#}"),
        _ => {}
    }
    let outcome = outcome?;
    if let Some(result) = &state.result_file {
        let mut output = fs::File::create(result).context("could not create picker result file")?;
        serde_json::to_writer(&mut output, &outcome)?;
        output.write_all(b"\n")?;
    }
    Ok(())
}

async fn pick(state: &State) -> Result<Outcome<'_>> {
    let input = records(state)?;
    let prompt = match state.scope.as_deref() {
        Some("tab") => "Tab extract> ",
        Some("session") => "Session extract> ",
        _ => "Extract> ",
    };
    let mut command = Command::new(&state.fzf);
    command.args([
        "--read0",
        "--print0",
        "--layout=reverse",
        "--border=rounded",
        "--no-multi",
        "--delimiter=\t",
        "--with-nth=2..",
        "--nth=1",
        "--expect=tab,enter",
        "--header=Tab: copy   Enter: insert   Esc: cancel",
    ]);
    command.arg(format!("--prompt={prompt}"));
    command.arg(format!("--query={}", state.query));
    command
        .env_remove("FZF_DEFAULT_OPTS")
        .env_remove("FZF_DEFAULT_OPTS_FILE");
    command.stderr(Stdio::inherit());
    let (selected, written) = communicate(&mut command, &input).await?;
    if let Err(error) = written {
        if error.kind() != io::ErrorKind::BrokenPipe {
            return Err(error).context("could not write fzf candidates");
        }
    }
    let outcome = if matches!(selected.status.code(), Some(1 | 130)) {
        Outcome {
            status: "cancelled",
            text: None,
            count: None,
        }
    } else {
        if !selected.status.success() {
            bail!("fzf exited with {}", selected.status);
        }
        let mut fields = selected.stdout.split(|byte| *byte == 0);
        let key = fields.next().context("fzf returned no selection key")?;
        let record = fields.next().context("fzf returned no selected record")?;
        let id = std::str::from_utf8(
            record
                .split(|byte| *byte == b'\t')
                .next()
                .unwrap_or_default(),
        )?;
        let value = state
            .values
            .get(id)
            .context("fzf returned an unknown candidate ID")?;
        let status = match key {
            b"tab" => {
                copy(value, &state.clipboard).await?;
                "copied"
            }
            b"enter" => {
                insert(value, state).await?;
                "inserted"
            }
            _ => bail!("fzf returned an unexpected selection key"),
        };
        Outcome {
            status,
            text: Some(value),
            count: Some(state.items.len()),
        }
    };
    Ok(outcome)
}

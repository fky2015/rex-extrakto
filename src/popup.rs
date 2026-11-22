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
use tokio::io::AsyncWriteExt;
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
struct RestoreZoom {
    server: Option<String>,
    session_id: String,
    window_id: String,
    block_id: String,
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
    restore_zoom: Option<RestoreZoom>,
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

async fn restore_zoom(state: &State) -> Result<()> {
    let Some(target) = &state.restore_zoom else {
        return Ok(());
    };
    let Ok(block) = std::env::var("REX_BLOCK") else {
        return Ok(());
    };
    let output = rex_call(
        &state.rex,
        target.server.as_deref(),
        "session.view",
        serde_json::json!({ "session_id": target.session_id }),
    )
    .await?;
    let view: serde_json::Value = serde_json::from_slice(&output)?;
    let Some(windows) = view["windows"].as_array() else {
        return Ok(());
    };
    let Some(window) = windows
        .iter()
        .find(|window| window["window_id"].as_str() == Some(&target.window_id))
    else {
        return Ok(());
    };
    let source_exists = window["layers"].as_array().is_some_and(|layers| {
        layers.iter().any(|layer| {
            layer["blocks"].as_array().is_some_and(|blocks| {
                blocks
                    .iter()
                    .any(|block| block["block_id"].as_str() == Some(&target.block_id))
            })
        })
    });
    let owns_focus = window["window_id"] == view["active_window_id"]
        && window["focused_block_id"].as_str() == Some(&block);
    // Respect a focus change, closed source, or source moved to another tab.
    if source_exists && owns_focus {
        rex_call(
            &state.rex,
            target.server.as_deref(),
            "session.zoom_block",
            serde_json::json!({
                "session_id": target.session_id,
                "block_id": target.block_id,
                "zoom": true,
            }),
        )
        .await?;
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
    if let Err(error) = restore_zoom(&state).await {
        eprintln!("rex-popup: could not restore the source pane zoom: {error:#}");
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

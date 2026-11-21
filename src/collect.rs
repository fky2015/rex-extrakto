use crate::extract::{Extractor, Options, Output};
use anyhow::{bail, Context, Result};
use futures_util::{stream, StreamExt, TryStreamExt};
use serde::{Deserialize, Serialize};
use std::{process::Stdio, sync::Arc, time::Duration};
use tokio::{process::Command, sync::Semaphore, task, time::timeout};

pub const DEFAULT_JOBS: usize = 4;
pub const DEFAULT_EXTRACT_JOBS: usize = 1;
pub const MAX_JOBS: usize = 32;

fn default_jobs() -> usize {
    DEFAULT_JOBS
}

fn default_extract_jobs() -> usize {
    DEFAULT_EXTRACT_JOBS
}

fn default_capture_timeout_ms() -> u64 {
    15000
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pane {
    pub block_id: String,
    pub display: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub rex: String,
    pub session_id: String,
    #[serde(default)]
    pub server: Option<String>,
    pub panes: Vec<Pane>,
    #[serde(default)]
    pub options: Options,
    #[serde(default = "default_jobs")]
    pub jobs: usize,
    #[serde(default = "default_extract_jobs")]
    pub extract_jobs: usize,
    #[serde(default = "default_capture_timeout_ms")]
    pub capture_timeout_ms: u64,
}

impl Request {
    pub fn validate(&self) -> Result<()> {
        if !(1..=MAX_JOBS).contains(&self.jobs) {
            bail!("jobs must be 1..{MAX_JOBS}");
        }
        if !(1..=self.jobs).contains(&self.extract_jobs) {
            bail!("extract_jobs must be 1..jobs");
        }
        if self.capture_timeout_ms == 0 {
            bail!("capture_timeout_ms must be positive");
        }
        if self.rex.is_empty() || self.session_id.is_empty() || self.panes.is_empty() {
            bail!("rex, session_id and panes must be nonempty");
        }
        if self.panes.iter().any(|pane| pane.block_id.is_empty()) {
            bail!("block_id must be nonempty");
        }
        self.options.validate().map_err(anyhow::Error::msg)
    }
}

#[derive(Debug, Serialize)]
pub struct PaneOutput {
    pub block_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub candidates: Option<Output>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct BatchOutput {
    pub panes: Vec<PaneOutput>,
}

struct CaptureConfig {
    rex: String,
    server: Option<String>,
    session_id: String,
    options: Options,
    timeout: Duration,
}

enum Capture {
    Text(String),
    Missing(String),
}

async fn capture(config: &CaptureConfig, pane: &Pane) -> Result<Capture> {
    let mut command = Command::new(&config.rex);
    if let Some(server) = config.server.as_deref().filter(|server| !server.is_empty()) {
        command.arg("-S").arg(server);
    }
    command
        .arg("capture")
        .arg("-s")
        .arg(&config.session_id)
        .arg("-b")
        .arg(&pane.block_id)
        .args(["--trim", "--unwrap"])
        .stdin(Stdio::null())
        .kill_on_drop(true);
    // Dropping the timed-out/cancelled output future terminates its child.
    let output = timeout(config.timeout, command.output())
        .await
        .context("Rex capture timed out")?
        .context("Rex capture could not complete")?;
    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        if message.contains("not found") || message.contains("not_found") {
            return Ok(Capture::Missing(message));
        }
        if message.is_empty() {
            bail!("Rex capture exited with {}", output.status);
        }
        bail!("{message}");
    }
    Ok(Capture::Text(
        String::from_utf8(output.stdout).context("Rex capture returned invalid UTF-8")?,
    ))
}

pub async fn collect(request: Request) -> Result<BatchOutput> {
    request.validate()?;
    let jobs = request.jobs.min(request.panes.len());
    let cpu_slots = Arc::new(Semaphore::new(request.extract_jobs));
    let extractor = Arc::new(Extractor::default());
    let config = Arc::new(CaptureConfig {
        rex: request.rex,
        server: request.server,
        session_id: request.session_id,
        options: request.options,
        timeout: Duration::from_millis(request.capture_timeout_ms),
    });
    let tasks = request.panes.into_iter().enumerate().map(|(index, pane)| {
        let config = Arc::clone(&config);
        let extractor = Arc::clone(&extractor);
        let cpu_slots = Arc::clone(&cpu_slots);
        async move {
            let captured = capture(&config, &pane)
                .await
                .with_context(|| format!("Could not read {}", pane.display))?;
            let candidates = match captured {
                Capture::Missing(error) if index != 0 => {
                    return Ok((
                        index,
                        PaneOutput {
                            block_id: pane.block_id,
                            candidates: None,
                            error: Some(error),
                        },
                    ));
                }
                Capture::Missing(error) => bail!("Could not read {}: {error}", pane.display),
                Capture::Text(text) => {
                    let permit = cpu_slots.acquire_owned().await?;
                    let options = config.options.clone();
                    task::spawn_blocking(move || {
                        let _permit = permit;
                        extractor
                            .extract(&text, &options)
                            .map_err(anyhow::Error::msg)
                    })
                    .await
                    .context("Extractor task failed")??
                }
            };
            Ok((
                index,
                PaneOutput {
                    block_id: pane.block_id,
                    candidates: Some(candidates),
                    error: None,
                },
            ))
        }
    });
    // Bound the entire pane task, including captured text waiting for CPU work.
    // Failures drop the remaining futures, cancelling any active captures.
    let mut completed: Vec<(usize, PaneOutput)> = stream::iter(tasks)
        .buffer_unordered(jobs)
        .try_collect()
        .await?;
    completed.sort_unstable_by_key(|(index, _)| *index);
    Ok(BatchOutput {
        panes: completed.into_iter().map(|(_, output)| output).collect(),
    })
}

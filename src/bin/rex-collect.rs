use anyhow::{bail, Result};
use rex_extrakto::collect::{collect, Request};
use std::io::{self, BufWriter, Write};

fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--help"] || args == ["-h"] {
        println!(
            "rex-collect: capture and extract panes from a JSON request on stdin\n\
Required: rex, session_id, panes=[{{block_id, display}}]\n\
Optional: server, options, jobs=4, extract_jobs=1, capture_timeout_ms=15000"
        );
        return Ok(());
    }
    if args == ["--version"] {
        println!("rex-collect {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if !args.is_empty() {
        bail!("rex-collect reads its request from stdin; use --help for the format");
    }
    let request: Request = serde_json::from_reader(io::stdin().lock())?;
    request.validate()?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(request.extract_jobs)
        .build()?;
    let output = runtime.block_on(collect(request))?;
    let mut stdout = BufWriter::new(io::stdout().lock());
    serde_json::to_writer(&mut stdout, &output)?;
    stdout.flush()?;
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("rex-collect: {error:#}");
        std::process::exit(1);
    }
}

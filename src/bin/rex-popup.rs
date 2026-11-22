use anyhow::{bail, Result};
use std::path::Path;

fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--help"] || args == ["-h"] {
        println!("rex-popup: search candidates with fzf; Tab copies, Enter inserts\nUsage: rex-popup STATE.json\n       rex-popup --watch STATE.json\n       rex-popup --wait-ready STATE.json");
        return Ok(());
    }
    if args == ["--version"] {
        println!("rex-popup {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    match args.as_slice() {
        [path] => runtime.block_on(rex_extrakto::popup::run(Path::new(path))),
        [flag, path] if flag == "--watch" => {
            runtime.block_on(rex_extrakto::popup::watch(Path::new(path)))
        }
        [flag, path] if flag == "--wait-ready" => {
            runtime.block_on(rex_extrakto::popup::wait_ready(Path::new(path)))
        }
        _ => bail!("expected a state file; use --help for the format"),
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("rex-popup: {error:#}");
        std::process::exit(1);
    }
}

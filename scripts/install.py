#!/usr/bin/env python3
"""Install a verified GitHub release and initialize Rex without Cargo."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

REPOSITORY = "fky2015/rex-extrakto"
BEGIN = "-- BEGIN rex-extrakto (managed loader)"
END = "-- END rex-extrakto (managed loader)"
TARGETS = {
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("Linux", "aarch64"): "aarch64-unknown-linux-musl",
}


def host_target():
    system, machine = platform.system(), platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "arm64" if system == "Darwin" else "aarch64"}.get(machine, machine)
    try:
        return TARGETS[(system, machine)]
    except KeyError:
        raise RuntimeError("Unsupported platform: " + system + " " + machine)


def archive_name(version, target):
    return "rex-extrakto-" + version + "-" + target + ".tar.gz"


def validate_version(version):
    if not re.fullmatch(r"v[0-9][A-Za-z0-9.+_-]*", version):
        raise ValueError("Invalid release version: " + version)
    return version


def normalize_version(version):
    if version == "latest":
        return version
    return validate_version(version if version.startswith("v") else "v" + version)


def open_url(url):
    headers = {"User-Agent": "rex-extrakto-installer"}
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = "Bearer " + token
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)


def release_metadata(repository, version):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be OWNER/REPO")
    endpoint = "latest" if version == "latest" else "tags/" + urllib.parse.quote(version, safe="")
    with open_url("https://api.github.com/repos/" + repository + "/releases/" + endpoint) as response:
        return json.load(response)


def download(url, destination):
    print("Downloading " + destination.name, flush=True)
    with open_url(url) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, 1024 * 1024)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_for(content, name):
    for line in content.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise ValueError("No SHA-256 checksum for " + name)


def extract_package(archive, destination):
    roots = set()
    with tarfile.open(archive, "r:gz") as package:
        for member in package:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or "\\" in member.name or not name.parts:
                raise ValueError("Unsafe archive path: " + member.name)
            if not (member.isdir() or member.isfile()):
                raise ValueError("Archive links and special files are not supported: " + member.name)
            roots.add(name.parts[0])
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    if len(roots) != 1:
        raise ValueError("Release archive must have one top-level directory")
    return destination / roots.pop()


def find_rex():
    return shutil.which("rex") or next((str(path) for path in [Path.home() / ".local/bin/rex"] if os.access(path, os.X_OK)), None)


def install_fzf(payload, target, previous):
    if (payload / "deps/fzf").is_file():
        return
    if shutil.which("fzf"):
        return
    cache = previous / "deps/fzf"
    dependencies = payload / "deps"
    dependencies.mkdir(exist_ok=True)
    if cache.is_file() and os.access(cache, os.X_OK):
        shutil.copy2(cache, dependencies / "fzf")
        if (previous / "deps/LICENSE.fzf").exists():
            shutil.copy2(previous / "deps/LICENSE.fzf", dependencies / "LICENSE.fzf")
        return
    download_fzf(payload, target)


def download_fzf(payload, target, version="latest"):
    dependencies = payload / "deps"
    dependencies.mkdir(exist_ok=True)
    release = release_metadata("junegunn/fzf", version)
    system = "darwin" if "apple-darwin" in target else "linux"
    arch = "arm64" if target.startswith("aarch64") else "amd64"
    asset = next((asset for asset in release["assets"] if asset["name"].endswith(system + "_" + arch + ".tar.gz")), None)
    if not asset:
        raise RuntimeError("No fzf release for " + target)
    with tempfile.TemporaryDirectory(prefix="rex-fzf-") as temporary:
        archive = Path(temporary) / asset["name"]
        download(asset["browser_download_url"], archive)
        digest = asset.get("digest", "") or ""
        if digest.startswith("sha256:"):
            expected = digest[7:]
        else:
            checksums = next(asset for asset in release["assets"] if asset["name"].endswith("checksums.txt"))
            with open_url(checksums["browser_download_url"]) as response:
                expected = checksum_for(response.read().decode(), archive.name)
        if digest_file(archive) != expected:
            raise ValueError("fzf SHA-256 checksum mismatch")
        with tarfile.open(archive, "r:gz") as package:
            members = [member for member in package if member.isfile() and PurePosixPath(member.name).name == "fzf"]
            if len(members) != 1:
                raise ValueError("fzf archive contains no unique executable")
            with package.extractfile(members[0]) as source, (dependencies / "fzf").open("wb") as output:
                shutil.copyfileobj(source, output)
        (dependencies / "fzf").chmod(0o755)
        tag = urllib.parse.quote(release["tag_name"], safe="")
        with open_url("https://raw.githubusercontent.com/junegunn/fzf/" + tag + "/LICENSE") as response:
            (dependencies / "LICENSE.fzf").write_bytes(response.read())


def normalize_key(key):
    """Compare shortcuts without depending on key case or modifier order."""
    return "+".join(sorted(part.strip().lower() for part in key.split("+")))


def configured_text(original, entry):
    path = str(entry)
    if any(ord(character) < 32 for character in path):
        raise ValueError("Install path must not contain control characters")
    loader = BEGIN + "\ndofile(" + json.dumps(path, ensure_ascii=False) + ")\n" + END
    starts = [match.start() for match in re.finditer(re.escape(BEGIN), original)]
    ends = [match.start() for match in re.finditer(re.escape(END), original)]
    if len(starts) != len(ends) or len(starts) > 1 or (starts and starts[0] > ends[0]):
        raise ValueError("Rex configuration contains an incomplete or duplicate managed loader")
    if starts:
        pattern = re.escape(BEGIN) + r".*?" + re.escape(END)
        text = re.sub(pattern, lambda match: loader, original, flags=re.DOTALL)
    else:
        manual = r'(?m)^[ \t]*dofile\([^\n]*[/\\]extrakto\.lua[^\n]*\)[ \t]*$'
        matches = list(re.finditer(manual, original))
        if len(matches) > 1:
            raise ValueError("Rex configuration contains multiple extrakto loaders")
        text = re.sub(manual, lambda match: loader, original) if matches else original.rstrip("\n") + ("\n\n" if original else "") + loader
    bindings = re.findall(r'''rex\.bind\(\s*["']([^"']+)["']\s*,\s*["']([^"']+)["']''', text)
    keys, actions = {normalize_key(key) for key, action in bindings}, {action for key, action in bindings}
    defaults = [("cmd+shift+f", "rex_extrakto", ', { scope = "tab" }'),
                ("alt+cmd+shift+e", "rex_extrakto_lines", "")]
    for key, action, arguments in defaults:
        if normalize_key(key) not in keys and action not in actions:
            text = text.rstrip("\n") + '\nrex.bind("' + key + '", "' + action + '"' + arguments + ")\n"
    return text.rstrip("\n") + "\n"


def atomic_write(path, content):
    if path.is_symlink():
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(content)
    try:
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def release_differences(payload, destination):
    """List required files that are missing, modified or have the wrong mode."""
    differences = []
    for source in sorted(payload.rglob("*")):
        relative = source.relative_to(payload)
        if source.is_dir() or source.is_symlink() or not source.is_file() or str(relative) == ".archive-sha256":
            continue
        target = destination / relative
        if target.is_symlink() or not target.is_file():
            differences.append(str(relative))
            continue
        try:
            if target.stat().st_size != source.stat().st_size or digest_file(target) != digest_file(source):
                differences.append(str(relative))
            elif bool(target.stat().st_mode & 0o111) != bool(source.stat().st_mode & 0o111):
                differences.append(str(relative))
        except OSError:
            differences.append(str(relative))
    return differences


def backup_release(destination):
    candidate = destination.with_name(destination.name + ".bak")
    index = 1
    while candidate.exists() or candidate.is_symlink():
        index += 1
        candidate = destination.with_name(destination.name + ".bak-" + str(index))
    return candidate


def replace_release(payload, destination):
    """Move the damaged release aside, install the verified copy, keep the backup."""
    backup = backup_release(destination)
    os.replace(destination, backup)
    try:
        os.replace(payload, destination)
    except BaseException:
        try:
            os.replace(backup, destination)
        except OSError as error:
            print("Warning: could not restore " + str(destination) + ": " + str(error), file=sys.stderr)
        raise
    return backup


def initialize_config(config, entry, rex, reload):
    path = config.resolve() if config.is_symlink() else config
    original = path.read_text() if path.exists() else None
    updated = configured_text(original or "", entry)
    changed = updated != original
    if changed:
        backup = config.with_name(config.name + ".rex-extrakto.bak")
        if original is not None and not backup.exists():
            atomic_write(backup, original)
        atomic_write(path, updated)
    reloaded = False
    try:
        checked = subprocess.run([rex, "config", "check", str(config), "--json"], capture_output=True, text=True, timeout=30)
        if checked.returncode:
            raise RuntimeError(checked.stderr.strip() or "Rex configuration check failed")
        try:
            diagnostics = json.loads(checked.stdout).get("diagnostics")
        except (ValueError, AttributeError):
            raise RuntimeError("Rex configuration check returned invalid JSON: " + checked.stdout.strip())
        if diagnostics:
            raise RuntimeError("Rex configuration check returned diagnostics: " + checked.stdout.strip())
        if reload:
            loaded = subprocess.run([rex, "config", "reload", "--json"], capture_output=True, text=True, timeout=30)
            reloaded = loaded.returncode == 0
            if loaded.returncode:
                raise RuntimeError(loaded.stderr.strip() or "Rex configuration reload failed")
            try:
                diagnostics = json.loads(loaded.stdout).get("diagnostics")
            except (ValueError, AttributeError):
                raise RuntimeError("Rex configuration reload returned invalid JSON: " + loaded.stdout.strip())
            if diagnostics:
                raise RuntimeError("Rex configuration reload returned diagnostics: " + loaded.stdout.strip())
    except Exception:
        if changed and path.exists() and path.read_text() == updated:
            if original is None:
                path.unlink()
            else:
                atomic_write(path, original)
            if reloaded:
                try:
                    subprocess.run([rex, "config", "reload", "--json"], capture_output=True, text=True, timeout=30)
                except Exception as error:
                    print("Warning: could not reload the previous Rex configuration: " + str(error), file=sys.stderr)
        raise


def set_current(root, target):
    current = root / "current"
    fd, name = tempfile.mkstemp(prefix=".current-", dir=root)
    os.close(fd)
    temporary = Path(name)
    temporary.unlink()
    try:
        temporary.symlink_to(target)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def activate(root, release):
    current = root / "current"
    if current.exists() and not current.is_symlink():
        raise ValueError("Install current path already exists and is not a symlink")
    set_current(root, release.relative_to(root))


def restore_current(root, previous):
    current = root / "current"
    if previous is None:
        if current.is_symlink():
            current.unlink()
    else:
        set_current(root, previous)


def install(args):
    target = host_target()
    requested = normalize_version(args.version)
    root = args.install_dir.expanduser().absolute()
    config = args.config.expanduser().absolute()
    rex = find_rex()
    if not args.no_config and not rex:
        raise RuntimeError("Rex CLI was not found. Install Rex first, or use --no-config to install files only.")
    root.mkdir(parents=True, exist_ok=True)
    if (root / "current").exists() and not (root / "current").is_symlink():
        raise ValueError("Install current path already exists and is not a symlink")
    with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as temporary:
        scratch = Path(temporary)
        if args.archive:
            archive = args.archive.expanduser().absolute()
            checksums = args.checksum_file or archive.parent / "SHA256SUMS"
        else:
            release = release_metadata(args.repository, requested)
            requested = validate_version(release["tag_name"])
            name = archive_name(requested, target)
            assets = {asset["name"]: asset for asset in release["assets"]}
            if name not in assets or "SHA256SUMS" not in assets:
                raise RuntimeError("Release has no verified package for " + target)
            archive, checksums = scratch / name, scratch / "SHA256SUMS"
            download(assets[name]["browser_download_url"], archive)
            download(assets["SHA256SUMS"]["browser_download_url"], checksums)
        expected = checksum_for(checksums.read_text(), archive.name)
        actual = digest_file(archive)
        if actual != expected:
            raise ValueError("Release SHA-256 checksum mismatch")
        payload = extract_package(archive, scratch / "unpacked")
        manifest = json.loads((payload / "release.json").read_text())
        version = validate_version(manifest["version"])
        if manifest.get("schema_version") != 1 or manifest.get("target") != target:
            raise ValueError("Release manifest does not match this platform")
        if requested != "latest" and version != requested:
            raise ValueError("Release manifest version does not match the requested version")
        for name in ["extrakto.lua", "scripts/popup.py", "bin/rex-extract", "bin/rex-collect"]:
            if not (payload / name).is_file():
                raise ValueError("Release is missing " + name)
        for name in ["rex-extract", "rex-collect"]:
            output = subprocess.check_output([str(payload / "bin" / name), "--version"], text=True, timeout=10).strip()
            if output != name + " " + version[1:]:
                raise ValueError("Release binary version mismatch: " + name)
        if manifest.get("fzf_version"):
            bundled = payload / "deps/fzf"
            if not bundled.is_file():
                raise ValueError("Release is missing deps/fzf")
            output = subprocess.check_output([str(bundled), "--version"], text=True, timeout=10).split()
            if not output or output[0] != manifest["fzf_version"].lstrip("v"):
                raise ValueError("Bundled fzf version mismatch")
        if not args.no_deps:
            install_fzf(payload, target, root / "current")
        releases = root / "releases"
        releases.mkdir(exist_ok=True)
        destination = releases / version
        (payload / ".archive-sha256").write_text(actual + "\n")
        backup = None
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            backup = replace_release(payload, destination)
        elif destination.exists():
            marker = destination / ".archive-sha256"
            marker_regular = marker.is_file() and not marker.is_symlink()
            recorded = marker.read_text().strip() if marker_regular else None
            if recorded is not None and recorded != actual:
                raise RuntimeError("This release version is already installed with different content: " + str(destination))
            differences = release_differences(payload, destination)
            if differences or ((marker.exists() or marker.is_symlink()) and not marker_regular):
                backup = replace_release(payload, destination)
            elif recorded is None:
                atomic_write(marker, actual + "\n")
        else:
            os.replace(payload, destination)
        if backup is not None:
            print("Repaired " + str(destination) + " (backup: " + str(backup) + ")")
        previous = os.readlink(root / "current") if (root / "current").is_symlink() else None
        activate(root, destination)
        if not args.no_config:
            try:
                initialize_config(config, destination / "extrakto.lua", rex, not args.no_reload)
            except Exception:
                try:
                    restore_current(root, previous)
                except OSError as error:
                    print("Warning: could not restore " + str(root / "current") + ": " + str(error), file=sys.stderr)
                raise
        print("Installed rex-extrakto " + version + " for " + target)
        print("Location: " + str(root / "current"))
        if not args.no_config:
            print("Rex config: " + str(config))


def parser():
    data = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local/share")
    config = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--version", default="latest", help="release tag, default: latest")
    arguments.add_argument("--repository", default=REPOSITORY)
    arguments.add_argument("--install-dir", type=Path, default=data / "rex-extrakto")
    arguments.add_argument("--config", type=Path, default=config / "rex/init.lua")
    arguments.add_argument("--archive", type=Path, help="use a locally downloaded release archive")
    arguments.add_argument("--checksum-file", type=Path, help="local SHA256SUMS, defaults to archive's directory")
    arguments.add_argument("--no-config", action="store_true", help="install release files without editing Rex config")
    arguments.add_argument("--no-reload", action="store_true", help="write and check config without reloading the server")
    arguments.add_argument("--no-deps", action="store_true", help="skip optional fzf installation")
    return arguments


def main():
    try:
        install(parser().parse_args())
        return 0
    except Exception as error:
        print("Install failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

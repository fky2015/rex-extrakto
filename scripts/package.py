#!/usr/bin/env python3
"""Package prebuilt helpers, the plugin and pinned fzf into a release archive."""
import argparse
import gzip
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile

from install import TARGETS, archive_name, digest_file, download_fzf

ROOT = Path(__file__).resolve().parents[1]
FZF_VERSION = "v0.75.0"
PICKER = "rust"
HELPERS = ["rex-extract", "rex-collect", "rex-popup"]
REQUIRED_FILES = [
    "README.md", "LICENSE", "extrakto.lua",
    "scripts/install.py", "scripts/install.sh",
    "docs/configuration.md", "docs/development.md", "docs/installation.md",
    "bin/rex-extract", "bin/rex-collect", "bin/rex-popup", "deps/fzf", "deps/LICENSE.fzf",
    "licenses/README.txt", "release.json",
]
EXECUTABLES = ["bin/rex-extract", "bin/rex-collect", "bin/rex-popup", "deps/fzf"]


def checksums(directory):
    archives = sorted(path for path in directory.glob("rex-extrakto-*.tar.gz") if path.is_file())
    if not archives:
        raise ValueError("No release archives found")
    (directory / "SHA256SUMS").write_text("".join(digest_file(path) + "  " + path.name + "\n" for path in archives))


def verify_release(directory, version):
    archives = sorted(directory.glob("rex-extrakto-*.tar.gz"))
    expected = set(TARGETS.values())
    actual = set()
    if len(archives) != len(expected):
        raise ValueError("Release must contain all four platform archives")
    for path in archives:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            roots = set()
            contents = {}
            for member in members:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or "\\" in member.name or not name.parts:
                    raise ValueError("Unsafe archive path: " + member.name)
                if not (member.isdir() or member.isfile()):
                    raise ValueError("Archive links and special files are not supported: " + member.name)
                roots.add(name.parts[0])
                if len(name.parts) == 1:
                    continue
                relative = PurePosixPath(*name.parts[1:]).as_posix()
                if relative in contents:
                    raise ValueError("Duplicate archive member: " + member.name)
                contents[relative] = member
            if len(roots) != 1 or roots.pop() != path.name[:-len(".tar.gz")]:
                raise ValueError("Release archive must have one matching top-level directory: " + path.name)
            manifest_member = contents.get("release.json")
            if manifest_member is None or not manifest_member.isfile():
                raise ValueError("Release archive has no unique manifest: " + path.name)
            manifest = json.load(archive.extractfile(manifest_member))
        target = manifest.get("target")
        if manifest.get("schema_version") != 1:
            raise ValueError("Release manifest schema mismatch: " + path.name)
        if manifest.get("picker") != PICKER:
            raise ValueError("Release manifest picker mismatch: " + path.name)
        if manifest.get("helpers") != HELPERS:
            raise ValueError("Release manifest helper list mismatch: " + path.name)
        if not isinstance(target, str) or manifest.get("version") != version or target not in expected or path.name != archive_name(version, target):
            raise ValueError("Release archive version/target mismatch: " + path.name)
        if manifest.get("fzf_version") != FZF_VERSION:
            raise ValueError("Release archive does not bundle pinned fzf: " + path.name)
        if "scripts/popup.py" in contents:
            raise ValueError("Release archive still contains the legacy Python picker: " + path.name)
        for required in REQUIRED_FILES:
            member = contents.get(required)
            if member is None or not member.isfile():
                raise ValueError("Release archive is missing " + required + ": " + path.name)
        for executable in EXECUTABLES:
            if not contents[executable].mode & 0o111:
                raise ValueError("Release archive executable bit is missing: " + executable + " in " + path.name)
        actual.add(target)
    if actual != expected:
        raise ValueError("Release platform set is incomplete")


def dependency_licenses(metadata, payload):
    destination = payload / "licenses"
    destination.mkdir()
    selected = {node["id"] for node in metadata["resolve"]["nodes"]}
    index = []
    for package in sorted(metadata["packages"], key=lambda package: package["name"]):
        if package["id"] not in selected or package["name"] == "rex-extrakto":
            continue
        source = Path(package["manifest_path"]).parent
        files = set()
        for pattern in ["LICENSE*", "COPYING*", "NOTICE*"]:
            files.update(path for path in source.glob(pattern) if path.is_file())
        if package.get("license_file"):
            files.add(source / package["license_file"])
        folder = destination / (package["name"] + "-" + package["version"])
        folder.mkdir()
        for path in sorted(files):
            shutil.copyfile(path, folder / path.name)
        index.append(package["name"] + " " + package["version"] + " — " + (package.get("license") or "see included license"))
    (destination / "README.txt").write_text("Rust dependency licenses\n\n" + "\n".join(index) + "\n")


def build_archive(payload, destination):
    # Normalize archive metadata so repeated packaging produces identical bytes.
    with destination.open("wb") as output, gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in [payload, *sorted(payload.rglob("*"))]:
                name = path.relative_to(payload.parent).as_posix()
                info = archive.gettarinfo(str(path), arcname=name)
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.pax_headers = {}
                info.mode = 0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644
                if path.is_file():
                    with path.open("rb") as source:
                        archive.addfile(info, source)
                else:
                    archive.addfile(info)


def package(args):
    target = args.target
    metadata = json.loads(subprocess.check_output([
        "cargo", "metadata", "--locked", "--format-version", "1", "--filter-platform", target,
    ], cwd=ROOT))
    project = next(package for package in metadata["packages"] if package["name"] == "rex-extrakto")
    version = "v" + project["version"]
    binary_dir = args.binary_dir.absolute()
    args.dist.mkdir(parents=True, exist_ok=True)
    name = archive_name(version, target)
    with tempfile.TemporaryDirectory(prefix="rex-package-") as temporary:
        payload = Path(temporary) / name[:-len(".tar.gz")]
        payload.mkdir()
        paths = ["extrakto.lua", "scripts/install.py", "scripts/install.sh",
                 "examples/init.lua", "README.md", "LICENSE", "docs/configuration.md", "docs/development.md", "docs/installation.md"]
        for relative in paths:
            source, destination = ROOT / relative, payload / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        (payload / "bin").mkdir()
        for helper in HELPERS:
            source = binary_dir / helper
            if not source.is_file():
                raise ValueError("Missing built helper: " + str(source))
            description = subprocess.check_output(["file", "-b", str(source)], text=True)
            architecture = "arm64" if target.startswith("aarch64") and "darwin" in target else "aarch64" if target.startswith("aarch64") else "x86-64" if "linux" in target else "x86_64"
            if architecture not in description or ("linux" in target and "ELF" not in description) or ("darwin" in target and "Mach-O" not in description):
                raise ValueError("Helper architecture does not match " + target + ": " + description.strip())
            if "linux" in target and not any(kind in description for kind in ["statically linked", "static-pie linked"]):
                raise ValueError("Linux release helper must be linked with musl statically: " + helper)
            expected_version = helper + " " + version[1:]
            try:
                reported = subprocess.check_output([str(source), "--version"], text=True, timeout=10).strip()
            except (OSError, subprocess.SubprocessError) as error:
                raise ValueError("Cannot execute built helper " + helper + ": " + str(error)) from error
            if reported != expected_version:
                raise ValueError("Built helper version mismatch: " + helper + " reported " + reported + ", expected " + expected_version)
            shutil.copyfile(source, payload / "bin" / helper)
            (payload / "bin" / helper).chmod(0o755)
        if not args.no_bundle_fzf:
            download_fzf(payload, target, FZF_VERSION)
            try:
                reported = subprocess.check_output([str(payload / "deps/fzf"), "--version"], text=True, timeout=10).split()
            except (OSError, subprocess.SubprocessError) as error:
                raise ValueError("Cannot execute bundled fzf: " + str(error)) from error
            if not reported or reported[0] != FZF_VERSION.lstrip("v"):
                raise ValueError("Bundled fzf version mismatch: " + " ".join(reported))
        dependency_licenses(metadata, payload)
        (payload / "release.json").write_text(json.dumps({
            "schema_version": 1, "version": version, "target": target,
            "picker": PICKER, "helpers": HELPERS,
            "fzf_version": None if args.no_bundle_fzf else FZF_VERSION,
        }, indent=2) + "\n")
        destination = args.dist / name
        build_archive(payload, destination)
    checksums(args.dist)
    print(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=sorted(set(TARGETS.values())))
    parser.add_argument("--binary-dir", type=Path, default=ROOT / "bin")
    parser.add_argument("--dist", type=Path, default=ROOT / "target/dist")
    parser.add_argument("--no-bundle-fzf", action="store_true", help="development packages only")
    parser.add_argument("--checksums-only", action="store_true")
    parser.add_argument("--verify-version", help="verify all four archives against this release tag")
    args = parser.parse_args()
    if args.checksums_only:
        if args.verify_version:
            verify_release(args.dist, args.verify_version)
        checksums(args.dist)
    elif args.target:
        package(args)
    else:
        parser.error("--target is required when building a package")


if __name__ == "__main__":
    main()

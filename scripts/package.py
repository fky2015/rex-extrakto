#!/usr/bin/env python3
"""Package prebuilt helpers, the plugin and pinned fzf into a release archive."""
import argparse
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from install import TARGETS, archive_name, digest_file, download_fzf

ROOT = Path(__file__).resolve().parents[1]
FZF_VERSION = "v0.75.0"
PICKER = "rust"
HELPERS = ["rex-extract", "rex-collect", "rex-popup"]
SOURCE_BUNDLE = "source.bundle"
SOURCE_REF_HEAD = "HEAD"
HEX_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
BUNDLE_SIGNATURES = (b"# v2 git bundle", b"# v3 git bundle")
REQUIRED_FILES = [
    "README.md", "LICENSE", "extrakto.lua", "init.lua", "plugin.json",
    "scripts/install.py", "scripts/install.sh", "scripts/git_support.py",
    "docs/configuration.md", "docs/development.md", "docs/installation.md",
    "bin/rex-extract", "bin/rex-collect", "bin/rex-popup", "deps/fzf", "deps/LICENSE.fzf",
    "licenses/README.txt", SOURCE_BUNDLE, "release.json",
]
EXECUTABLES = ["bin/rex-extract", "bin/rex-collect", "bin/rex-popup", "deps/fzf"]


def checksums(directory):
    archives = sorted(path for path in directory.glob("rex-extrakto-*.tar.gz") if path.is_file())
    if not archives:
        raise ValueError("No release archives found")
    (directory / "SHA256SUMS").write_text("".join(digest_file(path) + "  " + path.name + "\n" for path in archives))


def git_run(root, *arguments, check=True):
    """Run a local Git command without prompts or user configuration surprises."""
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", *arguments], cwd=root, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "exit code " + str(result.returncode)
        raise RuntimeError("git " + " ".join(arguments) + " failed: " + detail)
    return result


def git_output(root, *arguments):
    return git_run(root, *arguments).stdout.strip()


def require_clean_source(root):
    """Reject packaging when tracked files differ from the recorded source revision."""
    output = git_output(root, "status", "--porcelain=v1", "--untracked-files=no")
    if output:
        changed = [line[3:] for line in output.splitlines()[:5]]
        raise ValueError("Source working tree has uncommitted tracked changes: " + ", ".join(changed))


def plugin_manifest_version(root):
    path = root / "plugin.json"
    if not path.is_file():
        raise ValueError("Missing plugin.json")
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError("plugin.json is not valid JSON: " + str(error)) from error
    if not isinstance(manifest, dict):
        raise ValueError("plugin.json must contain a JSON object")
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("plugin.json must contain a nonempty version string")
    return version


def require_plugin_version(root, release_version):
    """Require plugin.json to declare the release tag that Cargo will build."""
    declared = plugin_manifest_version(root)
    cargo_version = release_version[1:] if release_version.startswith("v") else release_version
    if declared != release_version:
        raise ValueError(
            "plugin.json version " + repr(declared) + " does not match Cargo version "
            + repr(cargo_version) + " (expected " + repr(release_version) + ")"
        )
    return declared


def resolve_source(root, version):
    """Return the source ref and commit that match the checked out build.

    A release tag is preferred when it points at the checked out commit.  HEAD is
    used before the tag exists, which keeps local packaging possible while still
    guaranteeing that the bundled source matches the built source.
    """
    tag = "refs/tags/" + version
    tag_exists = git_run(root, "show-ref", "--verify", "--quiet", tag, check=False).returncode == 0
    head = git_output(root, "rev-parse", "--verify", "HEAD^{commit}").lower()
    if not HEX_COMMIT.fullmatch(head):
        raise ValueError("HEAD does not resolve to a valid commit")
    if not tag_exists:
        return SOURCE_REF_HEAD, head
    commit = git_output(root, "rev-parse", "--verify", tag + "^{commit}").lower()
    if not HEX_COMMIT.fullmatch(commit):
        raise ValueError("Release tag " + version + " does not resolve to a valid commit")
    if commit != head:
        raise ValueError(
            "Release tag " + version + " does not point at the checked out source; "
            "check out the tag before packaging"
        )
    return tag, commit


def source_objects(root, source_ref, source_commit):
    """Objects a bundle may advertise for a ref: its commit or annotated tag object."""
    objects = {source_commit}
    raw = git_run(root, "rev-parse", "--verify", source_ref, check=False)
    if raw.returncode == 0:
        value = raw.stdout.strip().lower()
        if HEX_COMMIT.fullmatch(value):
            objects.add(value)
    return objects


def bundle_heads(path, root=ROOT):
    heads = []
    for line in git_output(root, "bundle", "list-heads", str(path)).splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            raise ValueError("Source bundle has an invalid head: " + line)
        commit, name = fields[0].lower(), fields[1]
        if not HEX_COMMIT.fullmatch(commit):
            raise ValueError("Source bundle advertises an invalid object: " + line)
        heads.append((commit, name))
    return heads


def verify_source_bundle(path, root, source_ref, source_commit):
    """Validate a local Git bundle's provenance and advertised source ref."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("Source bundle is missing: " + str(path))
    if path.stat().st_size == 0:
        raise ValueError("Source bundle is empty: " + str(path))
    with path.open("rb") as stream:
        signature = stream.readline(64)
    if not any(signature.startswith(prefix) for prefix in BUNDLE_SIGNATURES):
        raise ValueError("Source bundle is not a Git bundle: " + str(path))
    verified = git_run(root, "bundle", "verify", str(path), check=False)
    if verified.returncode:
        detail = verified.stderr.strip() or verified.stdout.strip() or "invalid bundle"
        raise ValueError("Source bundle verification failed: " + detail)
    advertised = [commit for commit, name in bundle_heads(path, root) if name == source_ref]
    if len(advertised) != 1:
        raise ValueError("Source bundle does not advertise " + source_ref)
    expected = source_objects(root, source_ref, source_commit)
    if advertised[0] not in expected:
        raise ValueError(
            "Source bundle advertises " + advertised[0] + " for " + source_ref
            + ", expected " + source_commit
        )
    return advertised[0]


def create_source_bundle(root, source_ref, source_commit, destination):
    """Write a complete bundle for the built source revision using the Git CLI."""
    destination = Path(destination)
    if destination.exists():
        destination.unlink()
    git_run(root, "bundle", "create", str(destination), source_ref)
    verify_source_bundle(destination, root, source_ref, source_commit)
    return destination


def verify_release(directory, version):
    archives = sorted(directory.glob("rex-extrakto-*.tar.gz"))
    expected = set(TARGETS.values())
    actual = set()
    if len(archives) != len(expected):
        raise ValueError("Release must contain all four platform archives")
    source_revisions = set()
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
            plugin_member = contents.get("plugin.json")
            plugin_manifest = None
            if plugin_member is not None and plugin_member.isfile():
                try:
                    plugin_manifest = json.load(archive.extractfile(plugin_member))
                except ValueError as error:
                    raise ValueError("Release plugin.json is not valid JSON: " + path.name + ": " + str(error))
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
        source_commit = manifest.get("source_commit")
        if not isinstance(source_commit, str) or not HEX_COMMIT.fullmatch(source_commit):
            raise ValueError("Release manifest source_commit must be 40 or 64 lowercase hexadecimal characters: " + path.name)
        source_ref = manifest.get("source_ref")
        if source_ref not in (SOURCE_REF_HEAD, "refs/tags/" + version):
            raise ValueError("Release manifest source_ref must be HEAD or refs/tags/" + version + ": " + path.name)
        if manifest.get("source_bundle") != SOURCE_BUNDLE:
            raise ValueError("Release manifest source_bundle must be " + SOURCE_BUNDLE + ": " + path.name)
        if contents[SOURCE_BUNDLE].size <= 0:
            raise ValueError("Release archive has an empty source bundle: " + path.name)
        if plugin_manifest is None:
            raise ValueError("Release archive is missing plugin.json: " + path.name)
        if not isinstance(plugin_manifest, dict):
            raise ValueError("Release plugin.json must contain a JSON object: " + path.name)
        declared = plugin_manifest.get("version")
        if not isinstance(declared, str) or declared != version:
            raise ValueError("Release plugin.json version " + repr(declared) + " does not match " + version + ": " + path.name)
        source_revisions.add((source_commit, source_ref))
        actual.add(target)
    if actual != expected:
        raise ValueError("Release platform set is incomplete")
    if len(source_revisions) != 1:
        raise ValueError("Release archives do not share one source revision")


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
    require_clean_source(ROOT)
    require_plugin_version(ROOT, version)
    source_ref, source_commit = resolve_source(ROOT, version)
    provided_bundle = None
    if getattr(args, "source_bundle", None):
        provided_bundle = args.source_bundle.expanduser().absolute()
        verify_source_bundle(provided_bundle, ROOT, source_ref, source_commit)
    binary_dir = args.binary_dir.absolute()
    args.dist.mkdir(parents=True, exist_ok=True)
    name = archive_name(version, target)
    with tempfile.TemporaryDirectory(prefix="rex-package-") as temporary:
        payload = Path(temporary) / name[:-len(".tar.gz")]
        payload.mkdir()
        paths = ["extrakto.lua", "init.lua", "plugin.json",
                 "scripts/install.py", "scripts/install.sh", "scripts/git_support.py",
                 "examples/init.lua", "README.md", "LICENSE",
                 "docs/configuration.md", "docs/development.md", "docs/installation.md"]
        for relative in paths:
            source, destination = ROOT / relative, payload / relative
            if not source.is_file():
                raise ValueError("Missing source file: " + relative)
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
        if provided_bundle is None:
            create_source_bundle(ROOT, source_ref, source_commit, payload / SOURCE_BUNDLE)
        else:
            shutil.copyfile(provided_bundle, payload / SOURCE_BUNDLE)
            verify_source_bundle(payload / SOURCE_BUNDLE, ROOT, source_ref, source_commit)
        (payload / SOURCE_BUNDLE).chmod(0o644)
        dependency_licenses(metadata, payload)
        (payload / "release.json").write_text(json.dumps({
            "schema_version": 1, "version": version, "target": target,
            "picker": PICKER, "helpers": HELPERS,
            "fzf_version": None if args.no_bundle_fzf else FZF_VERSION,
            "source_commit": source_commit, "source_ref": source_ref,
            "source_bundle": SOURCE_BUNDLE,
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
    parser.add_argument("--source-bundle", type=Path, help="use an existing Git bundle for explicit smoke fixtures")
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

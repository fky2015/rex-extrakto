#!/usr/bin/env python3
"""Install or update a rex-extrakto checkout from a verified local Git bundle.

The release archive is checked against SHA256SUMS by the caller before its Git
bundle is passed here.  This module never contacts a network: it only clones or
fetches from that local bundle, checks out the expected detached commit and
keeps the canonical GitHub URL in ``origin`` for future upgrades.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.parse

GIT_TIMEOUT = 120

_HEX_COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_RELEASE_VERSION = re.compile(r"v[0-9][A-Za-z0-9.+_-]*")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_TAG_REF = re.compile(r"refs/tags/v[0-9][A-Za-z0-9.+_-]*")
_UNTRACKED_BUILD_DIRECTORIES = frozenset({"bin", "deps", "target"})


class GitInstallTransaction:
    """State needed to finish an installation or restore the previous checkout."""

    def __init__(self, destination, commit, created, old_head=None, old_branch=None, old_origin=None):
        self.destination = destination
        self.commit = commit
        self.created = created
        self._old_head = old_head
        self._old_branch = old_branch
        self._old_origin = old_origin
        self._finished = False
        self._rolled_back = False

    def finish(self):
        """Keep the installed checkout."""
        self._finished = True

    def rollback(self):
        """Remove a new checkout or restore the previous HEAD, branch and origin."""
        if self._finished or self._rolled_back:
            return
        try:
            if self.created:
                self._remove_created()
            else:
                self._restore_existing()
        except Exception as error:
            raise RuntimeError("Could not roll back repository at " + str(self.destination) + ": " + str(error)) from error
        self._rolled_back = True

    def _remove_created(self):
        destination = self.destination
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise RuntimeError("Refusing to remove unexpected path: " + str(destination))
        if destination.exists():
            shutil.rmtree(destination)

    def _restore_existing(self):
        destination = self.destination
        if not destination.is_dir():
            raise RuntimeError("Repository is missing: " + str(destination))
        if self._old_branch:
            _git(["checkout", self._old_branch], cwd=destination)
        elif self._old_head:
            _git(["checkout", "--detach", self._old_head], cwd=destination)
        else:
            raise RuntimeError("No previous checkout was recorded")
        _verify_head(destination, self._old_head)
        if self._old_origin is not None and _origin_url_or_none(destination) != self._old_origin:
            _set_origin_url(destination, self._old_origin)


def install_repository(destination, bundle, version, repository, expected_commit, source_ref="HEAD"):
    """Install or update a plugin checkout from a local bundle.

    ``source_ref`` is either ``HEAD`` or a full ``refs/tags/vX.Y.Z`` reference.
    The bundle must advertise that exact reference at ``expected_commit`` and
    the checked out ``plugin.json`` must declare ``version``.  The returned
    transaction keeps the previous state until the caller calls ``finish()``;
    ``rollback()`` removes a new checkout or restores an existing one.
    """
    destination = _absolute_path(destination, "destination")
    bundle = _absolute_path(bundle, "bundle")
    version = _validate_version(version)
    repository = _validate_repository(repository)
    expected_commit = _validate_commit(expected_commit)
    source_ref = _validate_source_ref(source_ref)

    if destination.is_symlink():
        raise ValueError("Refusing to install into a symlinked destination: " + str(destination))
    if not bundle.is_file():
        raise ValueError("Bundle is not a file: " + str(bundle))
    advertised = _verify_bundle_ref(bundle, source_ref)

    if destination.exists():
        if not destination.is_dir():
            raise ValueError("Destination exists and is not a directory: " + str(destination))
        return _update_repository(destination, bundle, version, repository, expected_commit, source_ref, advertised)
    return _create_repository(destination, bundle, version, repository, expected_commit, source_ref, advertised)


def _absolute_path(value, label):
    if isinstance(value, str) and not value:
        raise ValueError(label + " must not be empty")
    try:
        path = Path(value).expanduser()
    except TypeError as error:
        raise ValueError(label + " must be a filesystem path") from error
    return Path(os.path.abspath(os.fspath(path)))


def _validate_version(version):
    if not isinstance(version, str) or not _RELEASE_VERSION.fullmatch(version):
        raise ValueError("Invalid release version: " + repr(version))
    return version


def _validate_repository(repository):
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be OWNER/REPO")
    owner, name = repository.split("/")
    if owner in (".", "..") or name in (".", ".."):
        raise ValueError("repository must be OWNER/REPO")
    return repository


def _validate_commit(expected_commit):
    if not isinstance(expected_commit, str) or not _HEX_COMMIT.fullmatch(expected_commit):
        raise ValueError("expected_commit must be a 40 or 64 character hexadecimal commit")
    return expected_commit.lower()


def _validate_source_ref(source_ref):
    if not isinstance(source_ref, str):
        raise ValueError("source_ref must be HEAD or a refs/tags/vX.Y.Z reference")
    if source_ref == "HEAD" or _TAG_REF.fullmatch(source_ref):
        return source_ref
    raise ValueError("source_ref must be HEAD or a refs/tags/vX.Y.Z reference")


def _execute(arguments, cwd=None, timeout=None, allow_file_protocol=False):
    if timeout is None:
        timeout = GIT_TIMEOUT
    command = [
        "git",
        "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false",
    ]
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if allow_file_protocol:
        # Only bundle transport sets this.  Cloning or fetching from the local
        # bundle is the whole point of the helper, so override a restrictive
        # protocol.file.allow (or protocol.allow) from the user's Git config.
        command += ["-c", "protocol.file.allow=always"]
        # GIT_ALLOW_PROTOCOL takes precedence over configuration, so drop it
        # for this one subprocess; the caller already verified the bundle.
        environment.pop("GIT_ALLOW_PROTOCOL", None)
    command += list(arguments)
    try:
        return subprocess.run(
            command,
            cwd=None if cwd is None else os.fspath(cwd),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Git is not available on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Git command timed out after " + str(timeout) + " seconds: git " + " ".join(arguments)) from error


def _git(arguments, cwd=None, timeout=None, allow_file_protocol=False):
    result = _execute(arguments, cwd=cwd, timeout=timeout, allow_file_protocol=allow_file_protocol)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = result.stdout.decode("utf-8", "replace").strip()
        if not detail:
            detail = "exit code " + str(result.returncode)
        raise RuntimeError("Git command failed: git " + " ".join(arguments) + ": " + detail)
    return result.stdout.decode("utf-8", "surrogateescape")


def _attempt(arguments, cwd=None, timeout=None):
    return _execute(arguments, cwd=cwd, timeout=timeout)


def _verify_bundle_ref(bundle, source_ref):
    output = _git(["bundle", "list-heads", str(bundle)])
    for line in output.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2 or fields[1] != source_ref:
            continue
        advertised = fields[0]
        if not _HEX_COMMIT.fullmatch(advertised):
            raise ValueError("Bundle advertises an invalid commit for " + source_ref)
        return advertised.lower()
    raise ValueError("Bundle does not advertise " + source_ref)


def _fetch_bundle_ref(repository_path, bundle, source_ref, advertised, expected_commit):
    """Fetch source_ref and return the commit, accepting annotated tag objects."""
    _git(["fetch", "--no-tags", str(bundle), source_ref], cwd=repository_path, allow_file_protocol=True)
    fetched = _git(["rev-parse", "--verify", "FETCH_HEAD"], cwd=repository_path).strip().lower()
    if fetched != advertised:
        raise ValueError("Bundle fetch produced " + fetched + ", expected ref object " + advertised)
    peeled = _attempt(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=repository_path)
    if peeled.returncode:
        raise ValueError("Bundle " + source_ref + " does not point to a commit")
    commit = peeled.stdout.decode("utf-8", "surrogateescape").strip().lower()
    if not _HEX_COMMIT.fullmatch(commit):
        raise ValueError("Bundle " + source_ref + " does not point to a commit")
    if expected_commit not in (fetched, commit):
        raise ValueError("Bundle " + source_ref + " points to " + commit + ", expected " + expected_commit)
    return commit


def _create_repository(destination, bundle, version, repository, expected_commit, source_ref, advertised):
    destination.parent.mkdir(parents=True, exist_ok=True)
    container = Path(tempfile.mkdtemp(prefix=".rex-extrakto-", dir=os.fspath(destination.parent)))
    staging = container / "repository"
    try:
        _git(
            ["clone", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(bundle), str(staging)],
            allow_file_protocol=True,
        )
        commit = _fetch_bundle_ref(staging, bundle, source_ref, advertised, expected_commit)
        _verify_plugin_version(staging, commit, version)
        _git(["checkout", "--detach", commit], cwd=staging)
        _verify_head(staging, commit)
        _set_origin(staging, repository)
        if os.path.lexists(destination):
            raise ValueError("Destination appeared while installing: " + str(destination))
        transaction = GitInstallTransaction(destination, commit, created=True)
        os.replace(staging, destination)
        return transaction
    finally:
        shutil.rmtree(container, ignore_errors=True)


def _update_repository(destination, bundle, version, repository, expected_commit, source_ref, advertised):
    _require_repository(destination)
    _require_clean(destination)
    old_origin = _origin_url(destination)
    _validate_origin(old_origin, repository, destination)
    old_head = _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=destination).strip().lower()
    if not _HEX_COMMIT.fullmatch(old_head):
        raise ValueError("Existing repository has no valid HEAD commit: " + str(destination))
    branch = _attempt(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=destination)
    old_branch = branch.stdout.decode("utf-8", "surrogateescape").strip() if branch.returncode == 0 else None
    transaction = GitInstallTransaction(
        destination, expected_commit, created=False,
        old_head=old_head, old_branch=old_branch, old_origin=old_origin,
    )
    try:
        commit = _fetch_bundle_ref(destination, bundle, source_ref, advertised, expected_commit)
        _verify_plugin_version(destination, commit, version)
        _git(["checkout", "--detach", commit], cwd=destination)
        _verify_head(destination, commit)
        transaction.commit = commit
    except BaseException as error:
        try:
            transaction.rollback()
        except Exception as rollback_error:
            raise RuntimeError(
                "Repository update failed (" + str(error) + ") and rollback failed (" + str(rollback_error) + ")"
            ) from error
        raise
    return transaction


def _require_repository(destination):
    result = _attempt(["rev-parse", "--show-toplevel"], cwd=destination)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError("Destination is not a Git repository: " + str(destination) + (": " + detail if detail else ""))
    root = result.stdout.decode("utf-8", "surrogateescape").strip()
    if not root:
        raise ValueError("Could not determine the Git repository root for " + str(destination))
    try:
        root_path = Path(root).resolve()
    except OSError as error:
        raise ValueError("Could not resolve the Git repository root for " + str(destination)) from error
    if root_path != destination.resolve():
        raise ValueError("Destination is inside another Git repository, not a plugin checkout: " + str(destination))


def _require_clean(destination):
    output = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"], cwd=destination)
    dirty = []
    for entry in output.split("\0"):
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if status == "??" and path.split("/", 1)[0] in _UNTRACKED_BUILD_DIRECTORIES:
            continue
        dirty.append(path)
    if dirty:
        shown = ", ".join(dirty[:5])
        if len(dirty) > 5:
            shown += ", ..."
        raise ValueError("Refusing to update a dirty repository at " + str(destination) + ": " + shown)


def _origin_url_or_none(destination):
    result = _attempt(["remote", "get-url", "origin"], cwd=destination)
    if result.returncode:
        return None
    return result.stdout.decode("utf-8", "surrogateescape").strip() or None


def _origin_url(destination):
    origin = _origin_url_or_none(destination)
    if origin is None:
        raise ValueError("Existing repository has no origin remote: " + str(destination))
    if any(ord(character) < 32 for character in origin):
        raise ValueError("Existing repository origin contains control characters")
    return origin


def _validate_origin(origin, repository, destination):
    github = _github_repository(origin)
    if github is not None:
        if github.lower() != repository.lower():
            raise ValueError("Existing repository origin points to " + github + ", expected " + repository)
        return
    if _is_local_origin(origin, destination):
        return
    raise ValueError("Refusing to update a repository with an unrecognized origin: " + origin)


def _github_repository(url):
    if "://" in url:
        try:
            parsed = urllib.parse.urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme not in ("https", "ssh") or hostname is None:
            return None
        if hostname.lower() != "github.com" or parsed.query or parsed.fragment:
            return None
        if parsed.scheme == "https" and (parsed.username is not None or port not in (None, 443)):
            return None
        if parsed.scheme == "ssh" and port not in (None, 22):
            return None
        path = parsed.path.strip("/")
    else:
        match = re.fullmatch(r"(?:[^@/\\]+@)?github\.com:(.+)", url)
        if not match:
            return None
        path = match.group(1).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0] + "/" + parts[1]


def _is_local_origin(url, destination):
    if any(ord(character) < 32 for character in url) or url.startswith("-"):
        return False
    if url.startswith("file://"):
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        if parsed.netloc not in ("", "localhost"):
            return False
        return Path(urllib.parse.unquote(parsed.path)).exists()
    if "://" in url:
        return False
    if re.fullmatch(r"(?:[^@/\\]+@)?[^/\\:]+:.+", url):
        return False
    path = Path(url).expanduser()
    if not path.is_absolute():
        path = destination / path
    return path.exists()


def _verify_plugin_version(repository_path, commit, version):
    result = _attempt(["cat-file", "blob", commit + ":plugin.json"], cwd=repository_path)
    if result.returncode:
        raise ValueError("Commit " + commit + " does not contain plugin.json")
    try:
        manifest = json.loads(result.stdout.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError("plugin.json is not valid UTF-8") from error
    except ValueError as error:
        raise ValueError("plugin.json is not valid JSON: " + str(error)) from error
    if not isinstance(manifest, dict):
        raise ValueError("plugin.json must contain a JSON object")
    actual = manifest.get("version")
    if not isinstance(actual, str) or actual != version:
        raise ValueError("plugin.json version " + repr(actual) + " does not match requested " + version)


def _verify_head(repository_path, expected_commit):
    head = _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=repository_path).strip().lower()
    if head != expected_commit:
        raise RuntimeError("Repository HEAD is " + head + ", expected " + expected_commit)


def _set_origin_url(repository_path, url):
    if _attempt(["remote", "get-url", "origin"], cwd=repository_path).returncode:
        _git(["remote", "add", "origin", url], cwd=repository_path)
    else:
        _git(["remote", "set-url", "origin", url], cwd=repository_path)


def _set_origin(repository_path, repository):
    _set_origin_url(repository_path, "https://github.com/" + repository + ".git")

#!/usr/bin/env python3

import argparse
from datetime import datetime
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


HOME_URL = "http://huma.ysepan.com"
DOWNLOAD_SPACE = "03 虎码输入法下载"
CHANGELOG_SPACE = "05 虎码测评 更新日志"
ARCHIVE_PREFIX = "虎码秃版 鼠须管 （Mac）"
CHANGELOG_PREFIX = "虎码更新日志 "
TIMEOUT = 30
RELAY_TIMEOUT = 190
HOMEPAGE_ATTEMPTS = 3
HOMEPAGE_BODY_LOG_LIMIT = 4096
USER_AGENT = "huma-rime-http-crawler/1"
IGNORED_NAMES = {
    ".DS_Store",
    ".git",
    ".github",
    ".gitignore",
    ".venv",
    "README.md",
    "__pycache__",
    "crawler.py",
    "crawler_http.py",
    "huma.recipe.yaml",
    "scf_relay",
    "test_crawler_http.py",
}


def log(message):
    print(message, file=sys.stderr, flush=True)


def log_transport_error(stage, url, error, started):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = sorted(
            {
                result[4][0]
                for result in socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
            }
        )
    except OSError as dns_error:
        addresses = [f"DNS error: {type(dns_error).__name__}: {dns_error}"]
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    log(
        f"[{stage}] transport error after {time.monotonic() - started:.3f}s: "
        f"host={host!r} port={port} addresses={addresses!r} "
        f"reason_type={type(reason).__name__} reason={reason!r}"
    )


def open_url(opener, request, stage, read_limit=None, timeout=TIMEOUT):
    log(f"[{stage}] request: {request.full_url}")
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read() if read_limit is None else response.read(read_limit)
            log(
                f"[{stage}] response: status={response.status} "
                f"type={response.headers.get('Content-Type', '')!r} bytes={len(body)} "
                f"elapsed={time.monotonic() - started:.3f}s"
            )
            return response.status, response.headers, body
    except urllib.error.HTTPError as error:
        detail = error.read(1024).decode("utf-8", "replace")
        log(f"[{stage}] HTTPError: status={error.code} reason={error.reason!r}")
        if detail:
            log(f"[{stage}] body: {detail!r}")
        raise
    except urllib.error.URLError as error:
        log_transport_error(stage, request.full_url, error, started)
        raise
    except TimeoutError as error:
        log_transport_error(stage, request.full_url, error, started)
        raise
    except Exception as error:
        log(f"[{stage}] {type(error).__name__}: {error}")
        raise


def load_json(
    opener,
    url,
    stage,
    token,
    payload,
    timeout=TIMEOUT,
    log_body_on_error=True,
):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    _, _, body = open_url(opener, request, stage, timeout=timeout)
    try:
        result = json.loads(body)
    except json.JSONDecodeError as error:
        detail = f"; body={body[:1024]!r}" if log_body_on_error else ""
        log(f"[{stage}] JSONDecodeError: {error}{detail}")
        raise
    log(f"[{stage}] JSON keys: {sorted(result)}")
    return result


def find_one(items, description, predicate):
    matches = [item for item in items if predicate(item)]
    if len(matches) != 1:
        names = [item.get("wjm") or item.get("bt") for item in matches]
        raise RuntimeError(
            f"expected one {description}, found {len(matches)}: {names!r}"
        )
    return matches[0]


def download_url(site, space, item):
    server = item["fwq"]
    return (
        f"https://ys-{server}.{site['ym']}/wap/{site['dlmc']}/"
        f"{urllib.parse.quote(space['xzpz'], safe='')}/"
        f"{urllib.parse.quote(item['pz'], safe='')}/"
        f"{urllib.parse.quote(item['wjm'])}"
    )


def open_homepage():
    for attempt in range(1, HOMEPAGE_ATTEMPTS + 1):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(
            HOME_URL, headers={"User-Agent": USER_AGENT}
        )
        _, headers, home = open_url(opener, request, "homepage")
        match = re.search(rb"window\.htxx\s*=\s*(\{.*?\});", home, re.DOTALL)
        if match:
            return opener, jar, json.loads(match.group(1))
        diagnostic_headers = {
            name: headers.get(name)
            for name in ("Content-Type", "Content-Length", "Date", "Server", "Via")
            if headers.get(name) is not None
        }
        log(
            f"[homepage] unusable response: attempt={attempt}/{HOMEPAGE_ATTEMPTS} "
            f"sha256={hashlib.sha256(home).hexdigest()} "
            f"headers={diagnostic_headers!r} "
            f"cookies={sorted(cookie.name for cookie in jar)!r}"
        )
        excerpt = home[:HOMEPAGE_BODY_LOG_LIMIT].decode("utf-8", "backslashreplace")
        log(f"[homepage] body excerpt: {excerpt!r}")
        if attempt == HOMEPAGE_ATTEMPTS:
            break
        delay = 2 ** (attempt - 1)
        log(
            f"[homepage] window.htxx was not found; "
            f"retrying with a new session in {delay}s"
        )
        time.sleep(delay)
    raise RuntimeError(
        f"[homepage] window.htxx was not found after {HOMEPAGE_ATTEMPTS} attempts"
    )


def probe_download(opener, url, stage):
    request = urllib.request.Request(
        url,
        headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT},
    )
    status, headers, body = open_url(opener, request, stage, read_limit=1)
    if status not in (200, 206) or len(body) != 1:
        raise RuntimeError(
            f"[{stage}] download probe returned status={status}, bytes={len(body)}"
        )
    log(
        f"[{stage}] download reachable: content-length="
        f"{headers.get('Content-Length', '')!r} content-range="
        f"{headers.get('Content-Range', '')!r}"
    )


def discover():
    opener, jar, site = open_homepage()
    token_cookie = next(
        (cookie for cookie in jar if cookie.name == f"jwttk_{site['dlmc']}"),
        None,
    )
    if token_cookie is None:
        raise RuntimeError("[homepage] authentication cookie was not set")
    log(
        f"[homepage] site={site['dlmc']!r} api={site['qqdz']!r} "
        f"cookie={token_cookie.name!r}"
    )

    api = site["qqdz"].rstrip("/") + "/"
    spaces = load_json(
        opener,
        urllib.parse.urljoin(api, "ml/mldq"),
        "space-list",
        token_cookie.value,
        {},
    )["lb"]
    download_space = find_one(
        spaces, DOWNLOAD_SPACE, lambda item: item.get("bt") == DOWNLOAD_SPACE
    )
    changelog_space = find_one(
        spaces, CHANGELOG_SPACE, lambda item: item.get("bt") == CHANGELOG_SPACE
    )

    def load_space(space, stage):
        return load_json(
            opener,
            urllib.parse.urljoin(api, "wj/wjdq"),
            stage,
            token_cookie.value,
            {"mlbh": space["bh"], "kqmm": "", "wjbh": 0, "ip1": ""},
        )

    downloads = load_space(download_space, "download-list")
    archive = find_one(
        downloads["lb"],
        ARCHIVE_PREFIX,
        lambda item: (item.get("wjm") or "").startswith(ARCHIVE_PREFIX)
        and item["wjm"].endswith(".zip"),
    )
    changelogs = load_space(changelog_space, "changelog-list")
    changelog = find_one(
        changelogs["lb"],
        CHANGELOG_PREFIX,
        lambda item: (item.get("wjm") or "").startswith(CHANGELOG_PREFIX)
        and item["wjm"].endswith(".txt"),
    )

    log(f"[archive] found: {archive['wjm']!r} size={archive['dx']}")
    log(f"[changelog] found: {changelog['wjm']!r} size={changelog['dx']}")
    return opener, site, downloads, archive, changelogs, changelog


def probe():
    opener, site, downloads, archive, changelogs, changelog = discover()
    probe_download(
        opener,
        download_url(site, downloads["ml"], archive),
        "archive-download",
    )
    probe_download(
        opener,
        download_url(site, changelogs["ml"], changelog),
        "changelog-download",
    )
    log("[probe] all connectivity checks passed")


def download_file(
    opener, url, destination, stage, expected_size, redact_url=False
):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    log(f"[{stage}] request: {'<redacted>' if redact_url else url}")
    started = time.monotonic()
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            log(
                f"[{stage}] response: status={response.status} "
                f"type={response.headers.get('Content-Type', '')!r} "
                f"bytes={destination.stat().st_size} "
                f"elapsed={time.monotonic() - started:.3f}s"
            )
    except urllib.error.HTTPError as error:
        detail = error.read(1024).decode("utf-8", "replace")
        log(f"[{stage}] HTTPError: status={error.code} reason={error.reason!r}")
        if detail:
            log(f"[{stage}] body: {detail!r}")
        raise
    except urllib.error.URLError as error:
        log_transport_error(stage, url, error, started)
        raise
    except TimeoutError as error:
        log_transport_error(stage, url, error, started)
        raise
    except Exception as error:
        detail = "<redacted>" if redact_url else str(error)
        log(f"[{stage}] {type(error).__name__}: {detail}")
        raise
    actual_size = destination.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(
            f"[{stage}] size mismatch: expected={expected_size}, actual={actual_size}"
        )


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_via_relay(source_url, destination, cache_key, expected_size):
    relay_url = os.environ.get("RELAY_URL")
    relay_token = os.environ.get("RELAY_TOKEN")
    if not relay_url or not relay_token:
        raise RuntimeError("RELAY_URL and RELAY_TOKEN are required")

    opener = urllib.request.build_opener()
    result = load_json(
        opener,
        relay_url,
        "changelog-relay",
        relay_token,
        {
            "url": source_url,
            "expected_size": expected_size,
            "cache_key": cache_key,
        },
        timeout=RELAY_TIMEOUT,
        log_body_on_error=False,
    )
    required = {"url", "size", "sha256", "cached"}
    if not isinstance(result, dict) or set(result) != required:
        raise RuntimeError("[changelog-relay] invalid response fields")
    if type(result["size"]) is not int or result["size"] != expected_size:
        raise RuntimeError("[changelog-relay] invalid response size")
    if type(result["cached"]) is not bool:
        raise RuntimeError("[changelog-relay] invalid cache status")
    if not isinstance(result["sha256"], str) or re.fullmatch(
        r"[0-9a-f]{64}", result["sha256"]
    ) is None:
        raise RuntimeError("[changelog-relay] invalid SHA-256")
    if not isinstance(result["url"], str):
        raise RuntimeError("[changelog-relay] invalid download URL")
    signed_url = urllib.parse.urlsplit(result["url"])
    if signed_url.scheme != "https" or not signed_url.hostname:
        raise RuntimeError("[changelog-relay] invalid download URL")

    log(
        f"[changelog-relay] ready: cached={result['cached']} "
        f"size={result['size']} sha256={result['sha256']}"
    )
    download_file(
        opener,
        result["url"],
        destination,
        "changelog-relay-download",
        expected_size,
        redact_url=True,
    )
    actual_sha256 = file_sha256(destination)
    if actual_sha256 != result["sha256"]:
        raise RuntimeError(
            f"[changelog-relay-download] SHA-256 mismatch: "
            f"expected={result['sha256']}, actual={actual_sha256}"
        )


def extract_archive(archive, destination):
    with zipfile.ZipFile(archive, metadata_encoding="cp936") as source:
        roots = set()
        for member in source.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsafe archive path: {member.filename!r}")
            if path.parts:
                roots.add(path.parts[0])
        if len(roots) != 1:
            raise RuntimeError(f"expected one archive root, found: {sorted(roots)!r}")
        source.extractall(destination)
    root = destination / roots.pop()
    if not root.is_dir():
        raise RuntimeError(f"archive root is not a directory: {root}")
    log(f"[archive] extracted root: {root.name!r}")
    return root


def remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def sync_tree(source, destination):
    source_names = {path.name for path in source.iterdir()}
    for target in destination.iterdir():
        if target.name not in IGNORED_NAMES and target.name not in source_names:
            log(f"[sync] remove: {target.relative_to(Path.cwd())}")
            remove_path(target)

    for item in source.iterdir():
        if item.name in IGNORED_NAMES:
            continue
        target = destination / item.name
        if item.is_dir():
            if target.exists() and (not target.is_dir() or target.is_symlink()):
                remove_path(target)
            target.mkdir(exist_ok=True)
            sync_tree(item, target)
        else:
            if target.exists() and target.is_dir():
                remove_path(target)
            shutil.copy2(item, target)


def version_from_name(filename, prefix, suffix):
    pattern = rf"{re.escape(prefix)}(?P<version>.+){re.escape(suffix)}"
    match = re.fullmatch(pattern, filename)
    if not match:
        raise RuntimeError(f"could not extract version from {filename!r}")
    version = match.group("version")
    if "\n" in version or "\r" in version:
        raise RuntimeError(f"invalid version: {version!r}")
    if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", version):
        return f"v{version}"
    return version


def parse_date(tag):
    value = tag.removeprefix("v")
    for pattern in ("%Y.%m.%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    return None


def choose_tag(archive_name, changelog_name):
    archive_tag = version_from_name(archive_name, ARCHIVE_PREFIX, ".zip")
    changelog_tag = version_from_name(changelog_name, CHANGELOG_PREFIX, ".txt")
    archive_date = parse_date(archive_tag)
    changelog_date = parse_date(changelog_tag)
    if archive_date is None:
        return archive_tag
    if changelog_date is None:
        raise RuntimeError(
            f"archive tag {archive_tag!r} is dated, changelog tag {changelog_tag!r} is not"
        )
    if archive_date == changelog_date:
        return archive_tag
    if archive_date > changelog_date:
        return f"{archive_tag}-pre"
    raise RuntimeError(
        f"archive tag {archive_tag!r} predates changelog tag {changelog_tag!r}"
    )


def update():
    repository = Path.cwd()
    if not (repository / ".git").exists():
        raise RuntimeError("run the updater from the repository root")
    opener, site, downloads, archive, changelogs, changelog = discover()
    with tempfile.TemporaryDirectory(prefix="huma-rime-") as temporary:
        temporary_path = Path(temporary)
        archive_path = temporary_path / "huma.zip"
        changelog_path = temporary_path / "changelog.txt"
        download_file(
            opener,
            download_url(site, downloads["ml"], archive),
            archive_path,
            "archive-download",
            archive["dx"],
        )
        changelog_url = download_url(site, changelogs["ml"], changelog)
        download_via_relay(
            changelog_url,
            changelog_path,
            f"changelog:{changelog_url}",
            changelog["dx"],
        )
        archive_root = extract_archive(archive_path, temporary_path / "extracted")
        sync_tree(archive_root, repository)
        shutil.copy2(changelog_path, repository / "changelog.txt")

    tag = choose_tag(archive["wjm"], changelog["wjm"])
    log(f"[update] selected tag: {tag}")
    print(f"tag={tag}")


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true")
    mode.add_argument("--update", action="store_true")
    args = parser.parse_args()
    if args.probe:
        probe()
    else:
        update()


if __name__ == "__main__":
    main()

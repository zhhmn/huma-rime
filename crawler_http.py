#!/usr/bin/env python3

import argparse
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


HOME_URL = "http://huma.ysepan.com"
DOWNLOAD_SPACE = "03 虎码输入法下载"
CHANGELOG_SPACE = "05 虎码测评 更新日志"
ARCHIVE_PREFIX = "虎码秃版 鼠须管 （Mac）"
CHANGELOG_PREFIX = "虎码更新日志 "
TIMEOUT = 30
USER_AGENT = "huma-rime-http-crawler/1"


def log(message):
    print(message, file=sys.stderr, flush=True)


def open_url(opener, request, stage, read_limit=None):
    log(f"[{stage}] request: {request.full_url}")
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            body = response.read() if read_limit is None else response.read(read_limit)
            log(
                f"[{stage}] response: status={response.status} "
                f"type={response.headers.get('Content-Type', '')!r} bytes={len(body)}"
            )
            return response.status, response.headers, body
    except urllib.error.HTTPError as error:
        detail = error.read(1024).decode("utf-8", "replace")
        log(f"[{stage}] HTTPError: status={error.code} reason={error.reason!r}")
        if detail:
            log(f"[{stage}] body: {detail!r}")
        raise
    except urllib.error.URLError as error:
        log(f"[{stage}] URLError: reason={error.reason!r}")
        raise
    except Exception as error:
        log(f"[{stage}] {type(error).__name__}: {error}")
        raise


def load_json(opener, url, stage, token, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    _, _, body = open_url(opener, request, stage)
    try:
        result = json.loads(body)
    except json.JSONDecodeError as error:
        log(f"[{stage}] JSONDecodeError: {error}; body={body[:1024]!r}")
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


def probe_download(opener, url, stage):
    request = urllib.request.Request(
        url,
        headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT},
    )
    status, headers, body = open_url(opener, request, stage, read_limit=1)
    if status not in (200, 206) or len(body) != 1:
        raise RuntimeError(f"[{stage}] download probe returned status={status}, bytes={len(body)}")
    log(
        f"[{stage}] download reachable: content-length="
        f"{headers.get('Content-Length', '')!r} content-range="
        f"{headers.get('Content-Range', '')!r}"
    )


def probe():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    home_request = urllib.request.Request(HOME_URL, headers={"User-Agent": USER_AGENT})
    _, _, home = open_url(opener, home_request, "homepage")

    match = re.search(rb"window\.htxx\s*=\s*(\{.*?\});", home, re.DOTALL)
    if not match:
        raise RuntimeError("[homepage] window.htxx was not found")
    site = json.loads(match.group(1))
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", required=True)
    parser.parse_args()
    probe()


if __name__ == "__main__":
    main()

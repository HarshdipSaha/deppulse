#!/usr/bin/env python3
"""
DepPulse -- "is it me, or is a provider down?"

A zero-credential, read-only tool that scans the current repo's lockfiles,
maps the dependencies you actually use to a pinned provider->status-page table,
checks each matched provider's live incident state (Atlassian Statuspage
summary.json), and prints ONE worst-first traffic-light board with a single
top verdict plus an honest coverage line.

Standard library only. No credentials. No writes to your repo.

This file exposes five subcommands that mirror the DepPulse rote Play's steps,
so each can be captured as its own rote @unit:

    scan     (rote proc)     -> detect dependencies from manifests/lockfiles
    map      (rote extract)  -> join detected deps against the pinned table
    probe    (rote probe)    -> credential-free GET of each provider's summary.json
    compose  (rote extract)  -> merge into the worst-first board
    run                      -> do all of the above in one process (for humans)

Each JSON-producing subcommand reads its input from a file or stdin ('-') and
writes JSON to stdout, so they compose like rote @units:

    deppulse scan  --dir .            > u1.json
    deppulse map   --in u1.json       > u2.json
    deppulse probe --in u2.json       > u3.json
    deppulse compose --map u2.json --status u3.json --format board
"""

import argparse
import concurrent.futures
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TABLE = os.path.join(HERE, "adapters", "dep-providers.table.json")
DEFAULT_ALLOWLIST = os.path.join(HERE, "adapters", "status-allowlist.txt")
USER_AGENT = "DepPulse/1.0 (+https://play.modiqo.ai; read-only status check)"

# ----------------------------------------------------------------------------
# Severity model. Lower index == worse. Ordering is fixed so the board is
# byte-stable for a given set of provider states (the determinism guarantee).
# ----------------------------------------------------------------------------
SEVERITY_ORDER = [
    "major_outage",
    "partial_outage",
    "degraded",
    "maintenance",
    "unknown",
    "operational",
]
SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITY_ORDER)}
PROBLEM_STATES = {"major_outage", "partial_outage", "degraded", "maintenance"}

LIGHT = {
    "major_outage": "R",   # rendered with an emoji below; ASCII fallback kept in comments
    "partial_outage": "O",
    "degraded": "Y",
    "maintenance": "M",
    "unknown": "?",
    "operational": "G",
}
EMOJI = {
    "major_outage": "\U0001F534",    # red circle
    "partial_outage": "\U0001F7E0",  # orange circle
    "degraded": "\U0001F7E1",        # yellow circle
    "maintenance": "\U0001F527",     # wrench
    "unknown": "⚪",             # white circle
    "operational": "\U0001F7E2",     # green circle
}
STATUS_TEXT = {
    "major_outage": "major outage",
    "partial_outage": "partial outage",
    "degraded": "degraded",
    "maintenance": "maintenance",
    "unknown": "unknown",
    "operational": "operational",
}

# Statuspage status.indicator -> DepPulse severity
INDICATOR_SEV = {
    "none": "operational",
    "minor": "degraded",
    "major": "partial_outage",
    "critical": "major_outage",
}
# Statuspage component.status -> DepPulse severity
COMPONENT_SEV = {
    "operational": "operational",
    "degraded_performance": "degraded",
    "partial_outage": "partial_outage",
    "major_outage": "major_outage",
    "under_maintenance": "maintenance",
}


def worst(a, b):
    return a if SEVERITY_RANK[a] <= SEVERITY_RANK[b] else b


def read_input(path):
    """Read JSON from a file path, or from stdin when path is '-'."""
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------------------------
# scan  (rote proc)  -- read-only, cwd-confined manifest/lockfile scan
# ----------------------------------------------------------------------------
MANIFESTS = [
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "go.mod",
    "Dockerfile",
]


def _npm_names_from_package_json(text):
    names = []
    try:
        data = json.loads(text)
    except ValueError:
        return names
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        block = data.get(field)
        if isinstance(block, dict):
            names.extend(block.keys())
    return names


def _pypi_names_from_requirements(text):
    names = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # strip environment markers and version specifiers
        line = line.split(";", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m:
            names.append(m.group(1).lower())
    return names


def _pypi_names_from_pyproject(text):
    names = []
    # [project] dependencies = ["pkg>=1", ...]  (PEP 621)
    for block in re.findall(r"dependencies\s*=\s*\[(.*?)\]", text, flags=re.S):
        for item in re.findall(r"[\"']([^\"']+)[\"']", block):
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", item.strip())
            if m:
                names.append(m.group(1).lower())
    # [tool.poetry.dependencies]\n pkg = "^1.0"
    pm = re.search(r"\[tool\.poetry\.dependencies\](.*?)(\n\[|\Z)", text, flags=re.S)
    if pm:
        for line in pm.group(1).splitlines():
            m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", line)
            if m and m.group(1).lower() != "python":
                names.append(m.group(1).lower())
    return names


def _go_names_from_gomod(text):
    names = []
    for m in re.finditer(r"^\s*(?:require\s+)?([a-z0-9][\w./-]+/[\w./-]+)\s+v", text, flags=re.M):
        names.append(m.group(1))
    return names


def _docker_images_from_dockerfile(text):
    images = []
    for m in re.finditer(r"^\s*FROM\s+(\S+)", text, flags=re.M | re.I):
        img = m.group(1)
        if img.lower() == "scratch" or img.startswith("$"):
            continue
        images.append(img)
    return images


def _is_dockerhub_image(image):
    """Docker Hub is the implied registry when there is no explicit registry host."""
    ref = image.split("@", 1)[0]
    first = ref.split("/", 1)[0]
    # An explicit registry host contains a '.' or ':' (e.g. ghcr.io, gcr.io:443)
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return False
    return True


def cmd_scan(repo_path):
    root = os.path.abspath(repo_path)
    deps = []
    files_scanned = []
    context = []
    seen = set()

    def add_dep(name, ecosystem, source):
        key = (ecosystem, name.lower())
        if key in seen:
            return
        seen.add(key)
        deps.append({"name": name, "ecosystem": ecosystem, "source_file": source})

    def read(rel):
        path = os.path.join(root, rel)
        # cwd-confinement guard: never read outside the target directory.
        if os.path.commonpath([root, os.path.abspath(path)]) != root:
            return None
        if not os.path.isfile(path):
            return None
        files_scanned.append(rel)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    # npm ecosystem
    pkg = read("package.json")
    if pkg is not None:
        for n in _npm_names_from_package_json(pkg):
            add_dep(n, "npm", "package.json")
    for lock in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
        if read(lock) is not None:
            context.append({"type": "npm_lockfile", "value": lock, "source_file": lock})

    # pypi ecosystem
    req = read("requirements.txt")
    if req is not None:
        for n in _pypi_names_from_requirements(req):
            add_dep(n, "pypi", "requirements.txt")
    pyp = read("pyproject.toml")
    if pyp is not None:
        for n in _pypi_names_from_pyproject(pyp):
            add_dep(n, "pypi", "pyproject.toml")
    if read("poetry.lock") is not None:
        context.append({"type": "pypi_lockfile", "value": "poetry.lock", "source_file": "poetry.lock"})

    # go ecosystem
    gomod = read("go.mod")
    if gomod is not None:
        for n in _go_names_from_gomod(gomod):
            add_dep(n, "go", "go.mod")

    # Dockerfile base images
    dockerfile = read("Dockerfile")
    if dockerfile is not None:
        for img in _docker_images_from_dockerfile(dockerfile):
            if _is_dockerhub_image(img):
                context.append({"type": "dockerhub_image", "value": img, "source_file": "Dockerfile"})

    # GitHub Actions
    wf_dir = os.path.join(root, ".github", "workflows")
    if os.path.isdir(wf_dir):
        wf_files = sorted(f for f in os.listdir(wf_dir) if f.endswith((".yml", ".yaml")))
        if wf_files:
            context.append({
                "type": "github_actions",
                "value": ".github/workflows",
                "source_file": ".github/workflows/" + wf_files[0],
            })

    ecosystems = sorted({d["ecosystem"] for d in deps})
    if any(c["type"] == "npm_lockfile" for c in context) and "npm" not in ecosystems:
        ecosystems.append("npm")
    if any(c["type"] == "pypi_lockfile" for c in context) and "pypi" not in ecosystems:
        ecosystems.append("pypi")

    return {
        "repo_path": repo_path,
        "deps": sorted(deps, key=lambda d: (d["ecosystem"], d["name"].lower())),
        "ecosystems": sorted(set(ecosystems)),
        "context": context,
        "scanned_files": sorted(set(files_scanned)),
    }


# ----------------------------------------------------------------------------
# map  (rote extract)  -- deterministic join of scan output against the table
# ----------------------------------------------------------------------------
def _ecosystem_source(scan, ecosystem):
    for d in scan.get("deps", []):
        if d["ecosystem"] == ecosystem:
            return d["source_file"]
    for c in scan.get("context", []):
        if c["type"] == ecosystem + "_lockfile":
            return c["value"]
    return ecosystem + " dependencies"


def _match_provider(provider, scan):
    """Return a human 'reason' string if this provider is implied by the repo, else None.
    Priority: explicit dependency > context signal > ecosystem."""
    match = provider.get("match", {})
    deps = scan.get("deps", [])
    context = scan.get("context", [])
    ecosystems = scan.get("ecosystems", [])

    for pattern in match.get("deps", []):
        pat = pattern.lower()
        for d in deps:
            if fnmatch.fnmatch(d["name"].lower(), pat):
                return "%s (%s)" % (d["name"], d["source_file"])

    for want in match.get("context", []):
        for c in context:
            if c["type"] == want:
                return "%s (%s)" % (c["value"], c["source_file"])

    for eco in match.get("ecosystems", []):
        if eco in ecosystems:
            return _ecosystem_source(scan, eco)

    return None


def cmd_map(scan, table, providers_filter=None, use_browser=False):
    allow = None
    if providers_filter:
        allow = {p.strip().lower() for p in providers_filter.split(",") if p.strip()}

    checked = []   # providers we will actually probe/browse
    manual = []    # providers with no live source right now (shown as manual links)

    for provider in table.get("providers", []):
        if allow is not None and provider["id"].lower() not in allow:
            continue
        reason = _match_provider(provider, scan)
        if reason is None:
            continue
        entry = {
            "id": provider["id"],
            "name": provider["name"],
            "leg": provider["leg"],
            "summary_json_url": provider.get("summary_json_url"),
            "status_url": provider["status_url"],
            "reason": reason,
        }
        if provider["leg"] == "json":
            checked.append(entry)
        elif provider["leg"] == "browser" and use_browser:
            checked.append(entry)
        else:
            manual.append(entry)

    checked.sort(key=lambda e: e["id"])
    manual.sort(key=lambda e: e["id"])
    detected = len(checked) + len(manual)
    return {
        "matched": checked,
        "manual": manual,
        "counts": {
            "detected": detected,
            "matched": len(checked),
            "no_status_page": len(manual),
        },
        "scanned_files": scan.get("scanned_files", []),
        "n_dependencies": len(scan.get("deps", [])),
        "table_version": table.get("version", "unknown"),
    }


# ----------------------------------------------------------------------------
# probe  (rote probe)  -- credential-free public GET of summary.json
# ----------------------------------------------------------------------------
def load_allowlist(path):
    hosts = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    hosts.add(line.lower())
    except OSError:
        pass
    return hosts


def _host_of(url):
    m = re.match(r"^https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def summarize_summary_json(summary):
    """Reduce a Statuspage summary.json into a single provider severity + incident text."""
    status = summary.get("status") or {}
    indicator = str(status.get("indicator") or "none").lower()
    severity = INDICATOR_SEV.get(indicator, "unknown")

    worst_comp = "operational"
    degraded_names = []
    for c in summary.get("components") or []:
        if c.get("group"):
            continue
        cs = str(c.get("status") or "operational").lower()
        sev = COMPONENT_SEV.get(cs, "operational")
        if sev != "operational":
            degraded_names.append(c.get("name"))
        worst_comp = worst(worst_comp, sev)
    severity = worst(severity, worst_comp)

    incidents = summary.get("incidents") or []  # summary.json lists UNRESOLVED incidents only
    incident_titles = [i.get("name") for i in incidents if i.get("name")]

    if incident_titles:
        detail = incident_titles[0]
    elif degraded_names:
        detail = "degraded: " + ", ".join(n for n in degraded_names[:2] if n)
    else:
        detail = status.get("description") or ""
    return severity, detail, incident_titles


def _fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def probe_one(entry, timeout, retries, allow_hosts):
    url = entry.get("summary_json_url")
    result = {
        "id": entry["id"],
        "name": entry["name"],
        "status_url": entry["status_url"],
        "reason": entry["reason"],
        "leg": entry["leg"],
    }
    host = _host_of(url)
    if not url or (allow_hosts and host not in allow_hosts):
        result.update(severity="unknown", detail="host not on allowlist", incidents=[])
        return result

    last_err = ""
    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            summary = _fetch(url, timeout)
            severity, detail, incidents = summarize_summary_json(summary)
            result.update(
                severity=severity,
                detail=detail,
                incidents=incidents,
                latency_ms=int((time.time() - t0) * 1000),
            )
            return result
        except (urllib.error.URLError, ValueError, OSError) as exc:
            last_err = type(exc).__name__
            continue
    # every attempt failed -> degrade this ONE row to unknown; never abort the run
    result.update(severity="unknown", detail="no response (%s)" % last_err, incidents=[])
    return result


def cmd_probe(mapped, timeout, retries, allow_hosts, max_workers=8):
    entries = [e for e in mapped.get("matched", []) if e.get("leg") == "json"]
    browser_entries = [e for e in mapped.get("matched", []) if e.get("leg") == "browser"]
    results = []
    if entries:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(probe_one, e, timeout, retries, allow_hosts): e for e in entries}
            for fut in concurrent.futures.as_completed(futs):
                results.append(fut.result())
    # Browser-leg providers with --browser on are best-effort; without a headless
    # browser dependency here we mark them 'unknown' with a manual link so replay
    # never breaks. (The rote Play uses the browse adapter for a coarse read.)
    for e in browser_entries:
        results.append({
            "id": e["id"], "name": e["name"], "status_url": e["status_url"],
            "reason": e["reason"], "leg": "browser",
            "severity": "unknown", "detail": "browser leg (best-effort) — check manually", "incidents": [],
        })
    results.sort(key=lambda r: r["id"])
    return {"statuses": results}


def probe_self(url, timeout):
    result = {"url": url}
    try:
        t0 = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
        result.update(http_status=code, latency_ms=int((time.time() - t0) * 1000),
                      reachable=200 <= code < 400)
    except Exception as exc:  # noqa: BLE001 - liveness check must never raise
        result.update(http_status=None, reachable=False, error=type(exc).__name__)
    return result


# ----------------------------------------------------------------------------
# compose  (rote extract)  -- merge into the worst-first board
# ----------------------------------------------------------------------------
def build_board(mapped, probed, self_status=None):
    statuses = list(probed.get("statuses", []))
    statuses.sort(key=lambda r: (SEVERITY_RANK.get(r.get("severity", "unknown"), 99), r["id"]))

    problems = [r for r in statuses if r.get("severity") in PROBLEM_STATES]
    unknowns = [r for r in statuses if r.get("severity") == "unknown"]
    checked = mapped.get("counts", {}).get("matched", len(statuses))
    detected = mapped.get("counts", {}).get("detected", len(statuses))
    no_status_page = mapped.get("counts", {}).get("no_status_page", 0)

    if problems:
        verdict_text = "%d provider(s) degraded" % len(problems)
        verdict_level = "degraded"
    elif unknowns:
        verdict_text = "all known providers operational — %d unknown" % len(unknowns)
        verdict_level = "unknown"
    else:
        verdict_text = "all systems green (%d checked)" % checked
        verdict_level = "operational"

    rows = []
    for r in statuses:
        rows.append({
            "id": r["id"],
            "provider": r["name"],
            "severity": r.get("severity", "unknown"),
            "status_text": STATUS_TEXT.get(r.get("severity", "unknown"), "unknown"),
            "detail": r.get("detail", ""),
            "status_url": r["status_url"],
            "reason": r["reason"],
        })

    return {
        "verdict": {"level": verdict_level, "text": verdict_text,
                    "degraded": len(problems), "unknown": len(unknowns), "checked": checked},
        "rows": rows,
        "manual": mapped.get("manual", []),
        "self": self_status,
        "coverage": {"checked": checked, "detected": detected,
                     "no_status_page": no_status_page,
                     "files": len(mapped.get("scanned_files", [])),
                     "dependencies": mapped.get("n_dependencies", 0)},
        "table_version": mapped.get("table_version", "unknown"),
    }


def render_board(board, use_emoji=True):
    lights = EMOJI if use_emoji else {k: "[%s]" % v for k, v in LIGHT.items()}
    v = board["verdict"]
    head_light = lights.get(v["level"], "")
    out = []
    out.append("DepPulse - is it me, or is a provider down?")
    out.append("%s %s" % (head_light, v["text"]))
    out.append("")
    if not board["rows"]:
        out.append("  (no providers detected from this repo's manifests)")
    for r in board["rows"]:
        light = lights.get(r["severity"], "")
        line = "%s %-13s %-16s" % (light, r["provider"], r["status_text"])
        if r["detail"] and r["severity"] != "operational":
            line += ' "%s"' % r["detail"]
        out.append(line.rstrip())
        out.append("      %s  <- included because: %s" % (r["status_url"], r["reason"]))
    if board.get("self"):
        s = board["self"]
        state = ("reachable, HTTP %s" % s.get("http_status")) if s.get("reachable") else "UNREACHABLE"
        out.append("")
        out.append("your endpoint: %s (%s)" % (s.get("url"), state))
    if board["manual"]:
        out.append("")
        out.append("no JSON status page (check manually):")
        for m in board["manual"]:
            out.append("  %-13s %s  <- included because: %s" % (m["name"], m["status_url"], m["reason"]))
    c = board["coverage"]
    out.append("")
    out.append("Coverage: checked %d of %d detected providers; %d had no JSON status page."
               % (c["checked"], c["detected"], c["no_status_page"]))
    out.append("Scanned %d manifest file(s), %d dependencies. Table %s."
               % (c["files"], c["dependencies"], board["table_version"]))
    return "\n".join(out)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _load_table(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="deppulse", description="Is it me, or is a provider down?")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="detect dependencies from manifests (rote proc)")
    sp.add_argument("--dir", default=".")

    mp = sub.add_parser("map", help="join detected deps against the pinned table (rote extract)")
    mp.add_argument("--in", dest="infile", default="-")
    mp.add_argument("--table", default=DEFAULT_TABLE)
    mp.add_argument("--providers", default=None)
    mp.add_argument("--browser", action="store_true")

    pp = sub.add_parser("probe", help="GET each provider's summary.json (rote probe)")
    pp.add_argument("--in", dest="infile", default="-")
    pp.add_argument("--timeout", type=float, default=8.0)
    pp.add_argument("--retries", type=int, default=2)
    pp.add_argument("--allowlist", default=DEFAULT_ALLOWLIST)

    cp = sub.add_parser("compose", help="merge into the worst-first board (rote extract)")
    cp.add_argument("--map", dest="mapfile", required=True)
    cp.add_argument("--status", dest="statusfile", required=True)
    cp.add_argument("--self", dest="selffile", default=None)
    cp.add_argument("--format", choices=["board", "json"], default="board")
    cp.add_argument("--no-emoji", action="store_true")

    rp = sub.add_parser("run", help="scan+map+probe+compose in one process")
    rp.add_argument("--dir", default=".")
    rp.add_argument("--table", default=DEFAULT_TABLE)
    rp.add_argument("--allowlist", default=DEFAULT_ALLOWLIST)
    rp.add_argument("--providers", default=None)
    rp.add_argument("--url", default=None, help="also liveness-check YOUR endpoint")
    rp.add_argument("--browser", action="store_true", help="enable best-effort browser leg (off by default)")
    rp.add_argument("--timeout", type=float, default=8.0)
    rp.add_argument("--retries", type=int, default=2)
    rp.add_argument("--format", choices=["board", "json"], default="board")
    rp.add_argument("--no-emoji", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "scan":
        print(json.dumps(cmd_scan(args.dir), indent=2))
        return 0

    if args.cmd == "map":
        scan = read_input(args.infile)
        table = _load_table(args.table)
        print(json.dumps(cmd_map(scan, table, args.providers, args.browser), indent=2))
        return 0

    if args.cmd == "probe":
        mapped = read_input(args.infile)
        allow = load_allowlist(args.allowlist)
        print(json.dumps(cmd_probe(mapped, args.timeout, args.retries, allow), indent=2))
        return 0

    if args.cmd == "compose":
        mapped = read_input(args.mapfile)
        probed = read_input(args.statusfile)
        self_status = read_input(args.selffile) if args.selffile else None
        board = build_board(mapped, probed, self_status)
        if args.format == "json":
            print(json.dumps(board, indent=2))
        else:
            print(render_board(board, use_emoji=not args.no_emoji))
        return _exit_code(board)

    if args.cmd == "run":
        scan = cmd_scan(args.dir)
        table = _load_table(args.table)
        mapped = cmd_map(scan, table, args.providers, args.browser)
        allow = load_allowlist(args.allowlist)
        probed = cmd_probe(mapped, args.timeout, args.retries, allow)
        self_status = probe_self(args.url, args.timeout) if args.url else None
        board = build_board(mapped, probed, self_status)
        if args.format == "json":
            print(json.dumps(board, indent=2))
        else:
            print(render_board(board, use_emoji=not args.no_emoji))
        return _exit_code(board)

    p.print_help()
    return 2


def _exit_code(board):
    """0 = all green/unknown-only, 1 = at least one provider in a problem state.
    Exit stays 0 on unknowns so a flaky status page never fails your pipeline."""
    return 1 if board["verdict"]["degraded"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

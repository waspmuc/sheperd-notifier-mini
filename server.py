#!/usr/bin/env python3
"""Minimal HTTP→Telegram notification sidecar for Shepherd."""
import html
import json
import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_TOKEN = os.environ.get("GITHUB_READ_TOKEN", "")
PORT = int(os.environ.get("PORT", "8000"))
STATE_FILE = "/tmp/shepherd_state.json"

# App-relevant path prefixes — commits touching only other paths are "CI-only"
APP_PATHS = ("src/", "build.gradle", "settings.gradle", "gradlew", "Dockerfile")

# {svc_full: {"digest": "abc123", "commit_sha": "deadbeef..."}}
_state: dict = {}


def load_state() -> None:
    global _state
    try:
        with open(STATE_FILE) as f:
            _state = json.load(f)
    except Exception:
        _state = {}


def save_state() -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_state, f)
    except Exception as e:
        print(f"State save error: {e}", flush=True)


def _gh_request(url: str) -> object:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "shepherd-notifier",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get_commits_since(owner: str, repo: str, base_sha: str, head: str) -> list[str]:
    """Fetch commits between base_sha and head (branch or SHA, up to 10)."""
    try:
        data = _gh_request(
            f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head}"
        )
        commits = data.get("commits", [])
        return [c["commit"]["message"].split("\n")[0] for c in reversed(commits[-10:])]
    except Exception as e:
        print(f"GitHub compare error: {e}", flush=True)
        return []


def get_recent_commits(owner: str, repo: str, head: str, n: int = 3) -> list[str]:
    """Fetch last n commits from head (branch or SHA)."""
    try:
        commits = _gh_request(
            f"https://api.github.com/repos/{owner}/{repo}/commits?sha={head}&per_page={n}"
        )
        return [c["commit"]["message"].split("\n")[0] for c in commits]
    except Exception as e:
        print(f"GitHub commits error: {e}", flush=True)
        return []


def is_app_relevant(owner: str, repo: str, base_sha: str, head: str) -> bool:
    """Return True if any commit between base_sha and head touches app source files."""
    try:
        data = _gh_request(
            f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head}"
        )
        files = [f["filename"] for f in data.get("files", [])]
        return any(f.startswith(APP_PATHS) for f in files)
    except Exception as e:
        print(f"GitHub files error: {e}", flush=True)
        return True  # assume relevant on error


def get_ghcr_token(owner: str, repo: str) -> str:
    """Exchange GitHub token for a GHCR pull token."""
    if not GITHUB_TOKEN:
        return ""
    try:
        req = urllib.request.Request(
            f"https://ghcr.io/token?scope=repository:{owner}/{repo}:pull&service=ghcr.io",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("token", "")
    except Exception as e:
        print(f"GHCR token error: {e}", flush=True)
        return ""


def get_sha_from_ghcr(digest: str, owner: str, repo: str) -> str:
    """Read org.opencontainers.image.revision label from GHCR manifest."""
    if not digest:
        return ""
    try:
        token = get_ghcr_token(owner, repo)
        if not token:
            return ""
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": (
                "application/vnd.oci.image.manifest.v1+json,"
                "application/vnd.docker.distribution.manifest.v2+json"
            ),
        }
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{owner}/{repo}/manifests/{digest}", headers=headers
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            manifest = json.loads(resp.read())

        config_digest = manifest.get("config", {}).get("digest", "")
        if not config_digest:
            return ""

        req = urllib.request.Request(
            f"https://ghcr.io/v2/{owner}/{repo}/blobs/{config_digest}", headers=headers
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            config = json.loads(resp.read())

        labels = config.get("config", {}).get("Labels") or {}
        return labels.get("org.opencontainers.image.revision", "")
    except Exception as e:
        print(f"GHCR revision error: {e}", flush=True)
        return ""


def get_latest_commit_sha(owner: str, repo: str, branch: str) -> str:
    try:
        commits = _gh_request(
            f"https://api.github.com/repos/{owner}/{repo}/commits?sha={branch}&per_page=1"
        )
        return commits[0]["sha"] if commits else ""
    except Exception as e:
        print(f"GitHub sha error: {e}", flush=True)
        return ""


def parse_body(body: str) -> tuple[str, str]:
    """Return (from_image_ref, to_image_ref) from shepherd body."""
    to_m = re.search(r"\bto (ghcr\.io/\S+)", body)
    from_m = re.search(r"\bfrom (ghcr\.io/\S+)\s+to\b", body)
    return (
        from_m.group(1) if from_m else "",
        to_m.group(1) if to_m else "",
    )


def short_digest(image_ref: str) -> str:
    m = re.search(r"sha256:([a-f0-9]{8})", image_ref)
    return m.group(1) if m else ""


def full_digest(image_ref: str) -> str:
    m = re.search(r"(sha256:[a-f0-9]{64})", image_ref)
    return m.group(1) if m else ""


def owner_repo(image_ref: str) -> tuple[str, str]:
    m = re.match(r"ghcr\.io/([^/]+)/([^:@\s]+)", image_ref)
    return (m.group(1), m.group(2)) if m else ("", "")


def format_message(title: str, body: str, notify_type: str) -> str | None:
    """Return formatted Telegram message, or None to skip."""
    print(f"title={title!r}", flush=True)
    print(f"body={body!r}", flush=True)

    is_failure = notify_type == "failure"
    is_staging = "staging" in title.lower()

    svc_m = re.search(r"Service (\S+) (?:updated|update failed)", title)
    svc_full = svc_m.group(1) if svc_m else title.strip()
    svc = svc_full.rsplit("-", 1)[-1]

    from_ref, to_ref = parse_body(body)
    new_digest = short_digest(to_ref)
    old_digest = short_digest(from_ref)

    # Skip restart: same digest, no failure
    prev = _state.get(svc_full, {})
    prev_digest = prev.get("digest", "")
    if not is_failure and new_digest and prev_digest == new_digest:
        print(f"Skip restart for {svc_full} (digest unchanged: {new_digest})", flush=True)
        return None

    icon = "❌" if is_failure else ("🚧" if is_staging else "🚀")
    env = "Staging" if is_staging else "Prod"

    if is_failure:
        lines = [
            f"{icon} <b>{env} — {html.escape(svc)} fehlgeschlagen</b>",
            f"<code>{html.escape(svc_full)}</code>",
            "Rollback wurde eingeleitet.",
        ]
    else:
        lines = [f"{icon} <b>{env} — {html.escape(svc)} aktualisiert</b>"]

        if old_digest and old_digest != new_digest:
            lines.append(f"<code>{old_digest}</code> → <code>{new_digest}</code>")
        elif new_digest:
            lines.append(f"ID: <code>{new_digest}</code>")

        if GITHUB_TOKEN and to_ref:
            owner, repo = owner_repo(to_ref)
            if owner and repo:
                branch = "staging" if is_staging else "main"
                prev_sha = prev.get("commit_sha", "")

                # Get the actual deployed commit SHA from the image label
                digest = full_digest(to_ref)
                new_sha = (get_sha_from_ghcr(digest, owner, repo) or get_latest_commit_sha(owner, repo, branch)).strip()
                print(f"new_sha={new_sha!r}", flush=True)

                if new_sha:
                    lines.append(f"v1.0.{new_sha[:7]}")

                if prev_sha and prev_sha != new_sha:
                    commits = get_commits_since(owner, repo, prev_sha, new_sha)
                    app_changed = is_app_relevant(owner, repo, prev_sha, new_sha)
                else:
                    # try with sha first, fall back to branch name
                    commits = get_recent_commits(owner, repo, new_sha, 3) if new_sha else []
                    if not commits:
                        commits = get_recent_commits(owner, repo, branch, 3)
                    app_changed = True

                if commits:
                    lines.append("")
                    if not app_changed:
                        lines.append("ℹ️ <i>Keine App-Änderungen</i>")
                    for msg in commits:
                        lines.append(f"• {html.escape(msg)}")

                _state[svc_full] = {"digest": new_digest, "commit_sha": new_sha}
                save_state()

    return "\n".join(lines)


def send_telegram(text: str) -> None:
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"Telegram: {resp.status}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/notify":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            title = data.get("title", "")
            body = data.get("body", "")
            notify_type = data.get("notify_type", "success")
            msg = format_message(title, body, notify_type)
            if msg:
                send_telegram(msg)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            print(f"Error: {e}", flush=True)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


if __name__ == "__main__":
    load_state()
    print(f"shepherd-notifier listening on :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

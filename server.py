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
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
PORT = int(os.environ.get("PORT", "8000"))


def get_commits(image: str, tag: str) -> list[str]:
    """Fetch last 3 commit messages for the deployed image repo."""
    if not GITHUB_TOKEN:
        return []
    match = re.match(r"ghcr\.io/([^/]+)/([^:@\s]+)", image)
    if not match:
        return []
    owner, repo = match.group(1), match.group(2)
    branch = "staging" if tag == "staging" else "main"
    url = f"https://api.github.com/repos/{owner}/{repo}/commits?sha={branch}&per_page=3"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "shepherd-notifier",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            commits = json.loads(resp.read())
            return [c["commit"]["message"].split("\n")[0] for c in commits]
    except Exception as e:
        print(f"GitHub API error: {e}", flush=True)
        return []


def format_message(title: str, body: str, notify_type: str) -> str:
    is_staging = "staging" in title.lower()
    is_failure = notify_type == "failure"

    if is_failure:
        icon = "❌"
    elif is_staging:
        icon = "🚧"
    else:
        icon = "🚀"

    env = "Staging" if is_staging else "Prod"

    # Service-Kurzname
    svc_match = re.search(r"Service (\S+) (?:updated|update failed)", title)
    svc_full = svc_match.group(1) if svc_match else "unknown"
    svc = svc_full.rsplit("-", 1)[-1]

    # Image-Info aus Body
    to_part = body.split(" to ")[-1] if " to " in body else ""
    image_match = re.match(r"(ghcr\.io/\S+?)(?:@|$)", to_part)
    image = image_match.group(1) if image_match else ""
    tag_match = re.search(r":(\w+)(?:@|$)", to_part)
    tag = tag_match.group(1) if tag_match else ""
    digest_match = re.search(r"sha256:([a-f0-9]{8})", to_part)
    digest = digest_match.group(1) if digest_match else ""

    if is_failure:
        lines = [
            f"{icon} <b>{env} — {html.escape(svc)} fehlgeschlagen</b>",
            f"<code>{html.escape(svc_full)}</code>",
            "Rollback wurde eingeleitet.",
        ]
    else:
        lines = [f"{icon} <b>{env} — {html.escape(svc)} aktualisiert</b>"]
        if tag:
            lines.append(f"Tag: <code>{html.escape(tag)}</code>")
        if digest:
            lines.append(f"ID: <code>{digest}</code>")

        commits = get_commits(image, tag)
        if commits:
            lines.append("")
            lines.append("Commits:")
            for msg in commits:
                lines.append(f"• {html.escape(msg)}")

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
            print(f"Notify: {title}", flush=True)
            send_telegram(format_message(title, body, notify_type))
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
    print(f"shepherd-notifier listening on :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

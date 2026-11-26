#!/usr/bin/env python3
"""Keep DumbPad's configured origin aligned with its Cloudflare Quick Tunnel."""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

UNIT = "dumbpad-cloudflared.service"
APP_UNIT = "dumbpad.service"
JOURNALCTL = "/usr/bin/journalctl"
SYSTEMCTL = "/usr/bin/systemctl"
REPO = Path("/workspace/DumbPad")
ENV_FILE = REPO / ".env"
STATE_FILE = Path("/var/lib/dumbpad-monitor/tunnel-url")
APEX_ORIGIN = "https://rahmatarasy.my.id"
URL_PATTERN = re.compile(r"https://[A-Za-z0-9-]+\.trycloudflare\.com")


def log(message):
    print(message, flush=True)


def valid_url(value):
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname) and parsed.hostname.endswith(".trycloudflare.com") and URL_PATTERN.fullmatch(value) is not None


def env_values():
    result = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip()
    return result


def write_state(value):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, STATE_FILE)


def update_app_origin(value):
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    output = []
    seen = set()
    for line in lines:
        if line.startswith("BASE_URL="):
            output.append(f"BASE_URL={value}")
            seen.add("BASE_URL")
        elif line.startswith("ALLOWED_ORIGINS="):
            output.append(f"ALLOWED_ORIGINS={value},{APEX_ORIGIN}")
            seen.add("ALLOWED_ORIGINS")
        else:
            output.append(line)
    if "BASE_URL" not in seen:
        output.append(f"BASE_URL={value}")
    if "ALLOWED_ORIGINS" not in seen:
        output.append(f"ALLOWED_ORIGINS={value}")
    temporary = ENV_FILE.with_name(".env.tunnel-update")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chown(temporary, 1000, 1000)
    os.chmod(temporary, 0o640)
    os.replace(temporary, ENV_FILE)


def verify_app_origin(value):
    host = urlparse(value).netloc
    request = Request(
        "http://127.0.0.1:3001/api/pin-required",
        headers={
            "Host": host,
            "X-Forwarded-Proto": "https",
            "Origin": value,
            "Referer": value + "/",
        },
    )
    for _ in range(30):
        try:
            with urlopen(request, timeout=4) as response:
                return response.status == 200
        except Exception:
            time.sleep(2)
    return False


def notify_change(old_url, new_url):
    webhook = os.environ.get("DUMBPAD_NOTIFY_WEBHOOK_URL", "").strip()
    if not webhook:
        log("URL changed, but no Cue webhook is configured.")
        return
    body = json.dumps({
        "event": "dumbpad_tunnel_url_changed",
        "old_url": old_url,
        "new_url": new_url,
        "changed_at": datetime.now(timezone.utc).isoformat(),
    }).encode("utf-8")
    request = Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                log(f"Cue link-change notification accepted (HTTP {response.status}).")
                return
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            if attempt == 2:
                log(f"Cue link-change notification failed ({type(error).__name__}); the URL is saved locally.")
                return
            time.sleep(3 * (attempt + 1))


def handle_url(value):
    value = value.rstrip(".,;)")
    if not valid_url(value):
        return
    saved = STATE_FILE.read_text(encoding="utf-8").strip() if STATE_FILE.exists() else ""
    current_env = env_values().get("BASE_URL", "")
    old_url = saved or current_env
    changed = bool(old_url and old_url != value)

    if current_env != value:
        update_app_origin(value)
        subprocess.run([SYSTEMCTL, "restart", APP_UNIT], check=True, timeout=60)
        if not verify_app_origin(value):
            raise RuntimeError("DumbPad did not pass the new-origin health check")
        log("DumbPad origin updated and service restarted for the current tunnel.")

    write_state(value)
    if changed:
        notify_change(old_url, value)


def journal_snapshot():
    result = subprocess.run(
        [JOURNALCTL, "-u", UNIT, "-n", "100", "--no-pager", "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    latest_url = ""
    cursor = ""
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        cursor = entry.get("__CURSOR", cursor)
        message = entry.get("MESSAGE", "")
        matches = URL_PATTERN.findall(message)
        if matches:
            latest_url = matches[-1]
    return latest_url, cursor


def follow_journal(cursor):
    command = [JOURNALCTL, "-u", UNIT, "--follow", "--no-pager", "-o", "cat"]
    if cursor:
        command.append(f"--after-cursor={cursor}")
    else:
        command.extend(["-n", "0"])
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        for line in process.stdout:
            for value in URL_PATTERN.findall(line):
                handle_url(value)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def main():
    latest_url, cursor = journal_snapshot()
    if latest_url:
        handle_url(latest_url)
    if "--check-once" in sys.argv:
        log("Current Cloudflare URL state checked.")
        return
    log("Watching DumbPad's Cloudflare Tunnel journal for URL changes.")
    follow_journal(cursor)


if __name__ == "__main__":
    main()

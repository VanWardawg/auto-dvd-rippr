"""
Tell someone a job needs them, without making them watch the app.

Across the first 36 recorded jobs the pipeline spent 21 hours ripping and 58
hours parked: errored and waiting for Resume, or finished ripping and waiting
for a TMDB choice. Nobody was looking at the window. A notification is what
turns those hours back into minutes.

The transport is one HTTP POST of a small JSON document to a URL the user
supplies. That is deliberately generic: a Home Assistant webhook trigger, ntfy,
Node-RED and most automation tools accept exactly that, and the app needs to
know nothing about any of them. Standard library only.

The URL is a secret -- anyone who has it can trigger the automation behind it
-- so it lives only in the user's config and is never written to a log, a job
record, or an error message.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import AppConfig
from .logger import get_logger

log = get_logger("notify")

# A notification is a courtesy. It must never hold up the pipeline for longer
# than this, however unreachable the receiver is.
NOTIFY_TIMEOUT_SECONDS = 5.0

EVENT_NEEDS_ATTENTION = "needs_attention"
EVENT_FAILED = "failed"
EVENT_FINISHED = "finished"
EVENT_TEST = "test"


def webhook_problem(url: str) -> str | None:
    """Why this URL cannot be used, or None if it can."""
    parsed = urllib.parse.urlsplit(url.strip())
    # urllib will happily open file:// and ftp:// URLs; a notification target
    # is only ever a web endpoint.
    if parsed.scheme not in ("http", "https"):
        return "The webhook URL must start with http:// or https://."
    if not parsed.netloc:
        return "The webhook URL has no host."
    return None


def send_notification(
    cfg: AppConfig,
    event: str,
    title: str,
    message: str,
    *,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    POST one notification. Never raises, and never reports the URL back.

    Returns {"sent": bool, "detail": str} so the Settings test button can say
    what happened; pipeline callers ignore the result.
    """
    url = (cfg.notify_webhook_url or "").strip()
    if not url:
        return {"sent": False, "detail": "No webhook URL is configured."}
    problem = webhook_problem(url)
    if problem:
        return {"sent": False, "detail": problem}

    payload = {
        "app": "Auto-Ripper",
        "event": event,
        "title": title,
        "message": message,
        "job_id": (job or {}).get("id"),
        "disc_label": (job or {}).get("disc_label"),
        "status": (job or {}).get("status"),
    }
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Auto-Ripper"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=NOTIFY_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        detail = f"The webhook answered HTTP {exc.code}."
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # str(exc) on these never contains the URL, only the reason.
        reason = getattr(exc, "reason", exc)
        detail = f"Could not reach the webhook: {reason}"
    else:
        return {"sent": True, "detail": f"The webhook answered HTTP {status}."}

    log.warning("notification not delivered", extra={"event": event, "detail": detail})
    return {"sent": False, "detail": detail}

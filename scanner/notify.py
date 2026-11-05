"""Push notifications through ntfy (https://ntfy.sh), which has free iPhone and Android apps."""
import json
import os
import urllib.request


def configured():
    return bool(os.environ.get("NTFY_TOPIC", "").strip())


def send(title, message, click=None, tags=None, priority=3):
    """Publish one notification. Raises on failure so the caller does not mark it as sent."""
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {
        "topic": os.environ["NTFY_TOPIC"].strip(),
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or [],
    }
    if click:
        payload["click"] = click
    request = urllib.request.Request(
        server,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def signal_message(signal):
    lines = [f"${signal['price']:.2f}. " + "; ".join(signal["reasons"][:3])]
    if signal["flags"]:
        lines.append("Risk: " + "; ".join(signal["flags"][:2]))
    lines.append("Research signal, not advice. Prices may be delayed.")
    return "\n".join(lines)


def signal_title(signal):
    return f"{signal['symbol']}: score {signal['score']}, {signal['day_change_pct']:+.1f}% today"

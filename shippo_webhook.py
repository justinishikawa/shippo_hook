#!/usr/bin/env python3
"""
Shippo Webhook Receiver → Telegram Forwarder
Receives Shippo tracking webhook events and forwards them to your Telegram chat.

Setup:
1. Run this server somewhere with a public HTTPS URL
2. Register the URL at https://app.goshippo.com/settings/tokens
3. Set env vars before running (see below)

Usage:
    SHIPPO_API_KEY=your_shippo_api_key \
    SHIPPO_REGISTRATION_API_KEY=your-registration-proxy-key \
    TELEGRAM_BOT_TOKEN=your_bot_token \
    TELEGRAM_CHAT_ID=your_chat_id \
    python3 shippo_webhook.py

Endpoints:
  POST /webhook/shippo      — Shippo webhook receiver (Telegram forwarder)
  POST /register/tracking   — Register tracking (protected by X-API-Key)
  GET  /health              — Health check
"""

import os
import re
import json
import threading
from flask import Flask, request, abort
import requests

app = Flask(__name__)

TELEGRAM_API = "https://api.telegram.org/bot"

def telegram_send(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False
    url = f"{TELEGRAM_API}{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"❌ Telegram send failed: {e}")
        return False


def format_tracking_message(payload: dict) -> str:
    """Format a Shippo webhook payload into a readable Telegram message."""

    # Extract tracking info
    data = payload.get("data", {})
    tracking_num = data.get("tracking_number", "Unknown")
    carrier = data.get("carrier", "Unknown").upper()
    servicelevel = data.get("servicelevel", {}).get("name", "")

    # Status info
    tracking_status = data.get("tracking_status", {})
    status = tracking_status.get("status", "UNKNOWN")
    status_details = tracking_status.get("status_details", "")
    location = tracking_status.get("location", {})
    location_str = f"{location.get('city', '')}, {location.get('state', '')} {location.get('zip', '')} {location.get('country', '')}".strip().rstrip(",").replace("  ", " ")

    # Status display name + emoji
    status_map = {
        "UNKNOWN":       ("Unknown",          "❓"),
        "PRE_TRANSIT":   ("Pre-Transit",       "📦"),
        "TRANSIT":       ("In Transit",        "🚚"),
        "DELIVERED":     ("Delivered",         "✅"),
        "RETURNED":      ("Returned",          "↩️"),
        "FAILURE":       ("Failed",            "❌"),
    }
    status_label, status_emoji = status_map.get(status, (status, "📍"))

    lines = [
        f"{'─' * 30}",
        f"📬 <b>Shippo Tracking Update</b>",
        f"{'─' * 30}",
        f"<b>Carrier:</b> {carrier}",
        f"<b>Tracking #:</b> <code>{tracking_num}</code>",
        f"<b>Service:</b> {servicelevel or 'Standard'}",
        f"",
        f"{status_emoji} <b>Status:</b> {status_label}",
    ]

    if status_details:
        lines.append(f"   └ {status_details}")

    if location_str:
        lines.append(f"   └ 📍 {location_str}")

    # ETA if present
    eta = data.get("eta")
    if eta:
        lines.append(f"   └ ⏰ ETA: {eta}")

    # Show tracking history link
    lines.append(f"")
    lines.append(f"🔗 Track: https://track.goshippo.com/results/{tracking_num}")

    return "\n".join(lines)


# ─── Tracking Registration ────────────────────────────────────────────────────

SHIPPO_API_URL = "https://api.goshippo.com/tracks/"

KNOWN_CARRIERS = ["ups", "fedex", "usps", "dhl", "amazon", "dhl_express", "lasership", "ontrac"]


def infer_carrier(tracking_number: str) -> str | None:
    """Infer Shippo carrier token from tracking number format."""
    tn = tracking_number.strip().upper()

    # UPS: 1Z prefix (e.g., 1Z999AA10123456784)
    if re.match(r"^1Z[A-Z0-9]{16}$", tn):
        return "ups"
    # FedEx: 12-15 digits, commonly starts with 7/8/9
    if re.match(r"^(7|8|9)\d{11,14}$", tn):
        return "fedex"
    # USPS: 20-30 digits, starts with 91
    if re.match(r"^91\d{18,28}$", tn):
        return "usps"
    # DHL: 10 digits starting with 1-5
    if re.match(r"^[1-5]\d{9}$", tn):
        return "dhl"
    # Amazon: TBA... or AMZN prefixes
    if tn.startswith("TBA") or tn.startswith("AMZN"):
        return "amazon"

    return None


def register_tracking(tracking_number: str, carrier: str = None, metadata: str = "") -> dict:
    """Call Shippo tracks.create API."""
    shippo_api_key = os.getenv("SHIPPO_API_KEY")
    if not shippo_api_key:
        raise ValueError("SHIPPO_API_KEY not set")

    if not carrier:
        carrier = infer_carrier(tracking_number)
    if not carrier:
        carriers_list = ", ".join(KNOWN_CARRIERS)
        raise ValueError(
            f"Could not infer carrier from tracking number: {tracking_number}\n"
            f"Known carriers: {carriers_list}"
        )

    headers = {
        "Authorization": f"ShippoToken {shippo_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "carrier": carrier,
        "tracking_number": tracking_number,
    }
    if metadata:
        payload["metadata"] = metadata

    resp = requests.post(SHIPPO_API_URL, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.route("/webhook/shippo", methods=["POST"])
def shippo_webhook():
    """
    Shippo sends POST to this endpoint on tracking events.
    Validate the webhook signature if SHIPPO_WEBHOOK_SECRET is set.
    """

    # Optional: verify Shippo webhook signature
    secret = os.getenv("SHIPPO_WEBHOOK_SECRET")
    if secret:
        sig = request.headers.get("Shippo-Webhook-Signature", "")
        # Shippo signs with HMAC-SHA256 — in production use a proper verify function
        # For simplicity, just check secret presence here
        if sig != secret:
            abort(403, "Invalid webhook signature")

    if request.method == "POST":
        try:
            payload = request.get_json()
            if not payload:
                return {"error": "No JSON body"}, 400

            # Log raw payload for debugging
            print(f"📦 Shippo webhook received: {json.dumps(payload, indent=2)}")

            # Only forward relevant status updates (skip TEST webhooks)
            data = payload.get("data", {})
            status = data.get("tracking_status", {}).get("status", "")
            if status in ("UNKNOWN", "TEST"):
                return {"ok": True}, 200

            # Format and send to Telegram
            msg = format_tracking_message(payload)
            success = telegram_send(msg)

            return {"ok": success}, 200 if success else 500

        except Exception as e:
            print(f"❌ Webhook error: {e}")
            return {"error": str(e)}, 500

    return {"error": "Method not allowed"}, 405


@app.route("/register/tracking", methods=["POST"])
def register_tracking_endpoint():
    """
    Register a tracking number with Shippo.
    Protected by X-API-Key header — must match SHIPPO_REGISTRATION_API_KEY.
    """
    # Auth check
    api_key = os.getenv("SHIPPO_REGISTRATION_API_KEY")
    if not api_key:
        return {"error": "Registration not configured"}, 500

    provided_key = request.headers.get("X-API-Key", "")
    if provided_key != api_key:
        return {"error": "Unauthorized"}, 401

    # Parse body
    try:
        body = request.get_json()
        if not body:
            return {"error": "No JSON body"}, 400
    except Exception:
        return {"error": "Invalid JSON"}, 400

    tracking_number = body.get("tracking_number")
    if not tracking_number:
        return {"error": "tracking_number is required"}, 400

    carrier = body.get("carrier") or None
    metadata = body.get("metadata", "")

    try:
        result = register_tracking(tracking_number, carrier=carrier, metadata=metadata)
        return result, 200
    except ValueError as e:
        return {"error": str(e)}, 400
    except Exception as e:
        print(f"❌ Shippo registration failed: {e}")
        return {"error": "Shippo API error"}, 502


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Shippo → Telegram webhook forwarder")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    args = parser.parse_args()

    print("🚀 Shippo Webhook Forwarder running on :{}".format(args.port))
    print("📬 Set your webhook URL in Shippo settings to: https://your-domain.com/webhook/shippo")
    app.run(host="0.0.0.0", port=args.port, debug=False)

#!/usr/bin/env python3
"""
Mux webhook server to handle asset status updates.
Updates videos and logod_videos tables when assets become ready.

To run locally: python3 mux_webhook_server.py
For production, deploy to a server with a public URL and configure Mux webhook.

Mux webhook setup:
1. Go to https://dashboard.mux.com/settings/webhooks
2. Add a new webhook with URL: https://your-domain.com/mux-webhook
3. Select events: video.asset.ready, video.asset.errored
4. Copy the signing secret to MUX_WEBHOOK_SECRET in .env
"""

import hashlib
import hmac
import json
import os
import time
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
MUX_WEBHOOK_SECRET = os.getenv("MUX_WEBHOOK_SECRET", "")

app = Flask(__name__)
supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def verify_mux_signature(payload: bytes, signature: str) -> bool:
    """Verify the Mux webhook signature."""
    if not MUX_WEBHOOK_SECRET:
        print("Warning: MUX_WEBHOOK_SECRET not set, skipping signature verification")
        return True
    
    # Mux signature format: t=timestamp,v1=signature
    parts = dict(part.split("=", 1) for part in signature.split(","))
    timestamp = parts.get("t", "")
    expected_sig = parts.get("v1", "")
    
    # Check timestamp is within 5 minutes
    if abs(time.time() - int(timestamp)) > 300:
        return False
    
    # Compute expected signature
    signed_payload = f"{timestamp}.{payload.decode()}"
    computed_sig = hmac.new(
        MUX_WEBHOOK_SECRET.encode(),
        signed_payload.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(computed_sig, expected_sig)


def update_video_status(asset_id: str, status: str, playback_id: str = None):
    """Update video status in videos table."""
    update_data = {"mux_status": status}
    if playback_id:
        update_data["mux_playback_id"] = playback_id
    if status == "ready":
        update_data["status"] = "ready"
    elif status == "errored":
        update_data["status"] = "failed"
    
    result = supabase.table("videos").update(update_data).eq("mux_asset_id", asset_id).execute()
    return len(result.data) > 0


def update_logod_video_status(asset_id: str, status: str, playback_id: str = None):
    """Update video status in logod_videos table."""
    update_data = {"mux_status": status}
    if playback_id:
        update_data["mux_playback_id"] = playback_id
    if status == "ready":
        update_data["status"] = "ready"
    elif status == "errored":
        update_data["status"] = "failed"
    
    result = supabase.table("logod_videos").update(update_data).eq("mux_asset_id", asset_id).execute()
    return len(result.data) > 0


@app.route("/mux-webhook", methods=["POST"])
def mux_webhook():
    """Handle Mux webhook events."""
    # Verify signature
    signature = request.headers.get("Mux-Signature", "")
    if MUX_WEBHOOK_SECRET and not verify_mux_signature(request.data, signature):
        return jsonify({"error": "Invalid signature"}), 401
    
    try:
        event = request.json
        event_type = event.get("type")
        data = event.get("data", {})
        asset_id = data.get("id")
        
        print(f"Received event: {event_type} for asset {asset_id}")
        
        if event_type == "video.asset.ready":
            playback_ids = data.get("playback_ids", [])
            playback_id = playback_ids[0]["id"] if playback_ids else None
            
            # Try updating both tables (asset could be in either)
            updated_video = update_video_status(asset_id, "ready", playback_id)
            updated_logod = update_logod_video_status(asset_id, "ready", playback_id)
            
            if updated_video:
                print(f"  Updated videos table for {asset_id}")
            if updated_logod:
                print(f"  Updated logod_videos table for {asset_id}")
            
            return jsonify({"status": "ok", "updated_video": updated_video, "updated_logod": updated_logod})
        
        elif event_type == "video.asset.errored":
            error_msg = data.get("errors", {}).get("messages", ["Unknown error"])
            
            updated_video = update_video_status(asset_id, "errored")
            updated_logod = update_logod_video_status(asset_id, "errored")
            
            if updated_video:
                print(f"  Updated videos table for {asset_id} (errored)")
            if updated_logod:
                print(f"  Updated logod_videos table for {asset_id} (errored)")
            
            return jsonify({"status": "ok", "error": error_msg})
        
        return jsonify({"status": "ignored", "event_type": event_type})
    
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"Starting Mux webhook server on port {port}")
    print(f"Webhook URL: http://localhost:{port}/mux-webhook")
    print(f"Health check: http://localhost:{port}/health")
    print()
    print("To expose locally for testing, use ngrok:")
    print(f"  ngrok http {port}")
    print()
    app.run(host="0.0.0.0", port=port, debug=True)

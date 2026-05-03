#!/usr/bin/env python3
"""Mux direct upload with retries for transient TLS / connection errors (SSL EOF, reset, etc.)."""

from __future__ import annotations

import errno
import os
import threading
import time

import requests
from requests import exceptions as req_exc
from requests.adapters import HTTPAdapter
from urllib3.exceptions import MaxRetryError, ProtocolError

_tls = threading.local()


def _mux_session() -> requests.Session:
    """Thread-local Session (requests sessions are not safe to share across threads)."""
    s = getattr(_tls, "mux_session", None)
    if s is None:
        s = requests.Session()
        # Wider pool helps many workers talking to api.mux.com + occasional direct-upload hosts.
        adapter = HTTPAdapter(pool_connections=12, pool_maxsize=12, max_retries=0)
        s.mount("https://", adapter)
        _tls.mux_session = s
    return s


def _transient_request_error(exc: BaseException) -> bool:
    if isinstance(exc, (req_exc.Timeout, req_exc.ConnectionError, req_exc.ChunkedEncodingError)):
        return True
    if isinstance(exc, req_exc.HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in (408, 425, 429, 500, 502, 503, 504):
            return True
    if isinstance(exc, req_exc.SSLError):
        return True
    if isinstance(exc, MaxRetryError):
        reason = getattr(exc, "reason", None)
        if reason is not None and reason is not exc and _transient_request_error(reason):
            return True
    if isinstance(exc, ProtocolError):
        return True
    if isinstance(exc, OSError):
        en = getattr(exc, "errno", None)
        if en in (errno.EPIPE, errno.ECONNRESET, errno.ETIMEDOUT, errno.ECONNABORTED):
            return True
    msg = str(exc).lower()
    if any(
        s in msg
        for s in (
            "eof occurred",
            "connection reset",
            "broken pipe",
            "timed out",
            "temporarily unavailable",
            "nodename nor servname",
            "max retries exceeded",
            "ssl",
            "decryption failed",
        )
    ):
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _transient_request_error(cause)
    return False


def upload_to_mux_direct(file_path: str, passthrough: str | None = None) -> dict:
    """
    Create Mux direct upload, PUT the file, poll until ``asset_created``.
    Retries PUT on transient errors; obtains a new direct upload if PUT keeps failing
    (partial uploads must not be resumed on the same URL).
    """
    mux_token_id = os.getenv("MUX_TOKEN_ID")
    mux_token_secret = os.getenv("MUX_TOKEN_SECRET")
    create_url = "https://api.mux.com/video/v1/uploads"
    headers = {"Content-Type": "application/json"}
    payload = {
        "new_asset_settings": {
            "playback_policy": ["public"],
            "mp4_support": "capped-1080p",
        },
        "cors_origin": "*",
    }
    if passthrough:
        payload["new_asset_settings"]["passthrough"] = passthrough

    size = os.path.getsize(file_path)
    put_timeout = max(600, min(7200, size // (256 * 1024) + 300))

    session = _mux_session()
    last_exc: BaseException | None = None
    for outer in range(8):
        try:
            response = session.post(
                create_url,
                json=payload,
                headers=headers,
                auth=(mux_token_id, mux_token_secret),
                timeout=120,
            )
            response.raise_for_status()
            upload_data = response.json()
            upload_url = upload_data["data"]["url"]
            upload_id = upload_data["data"]["id"]
        except Exception as e:
            last_exc = e
            if outer < 7 and _transient_request_error(e):
                time.sleep(min(45.0, 2.0**min(outer, 6)))
                continue
            raise

        put_ok = False
        for put_try in range(12):
            try:
                with open(file_path, "rb") as fh:
                    upload_response = session.put(
                        upload_url,
                        data=fh,
                        headers={"Content-Type": "video/mp4"},
                        timeout=put_timeout,
                    )
                upload_response.raise_for_status()
                put_ok = True
                break
            except Exception as e:
                last_exc = e
                if put_try < 11 and _transient_request_error(e):
                    time.sleep(min(90.0, 2.0**min(put_try, 6)))
                    continue
                break

        if not put_ok:
            if outer < 7:
                print(
                    f"Mux direct-upload PUT failed (retry {outer + 1}/8 with new upload URL): {last_exc!r}"
                )
                time.sleep(min(45.0, 2.0**min(outer, 6)))
                continue
            assert last_exc is not None
            raise last_exc

        for poll in range(30):
            try:
                check_response = session.get(
                    f"https://api.mux.com/video/v1/uploads/{upload_id}",
                    auth=(mux_token_id, mux_token_secret),
                    timeout=60,
                )
                check_response.raise_for_status()
            except Exception as e:
                if poll < 29 and _transient_request_error(e):
                    time.sleep(1 + poll * 0.2)
                    continue
                raise
            check_data = check_response.json()
            status = check_data["data"]["status"]

            if status == "asset_created":
                asset_id = check_data["data"]["asset_id"]
                asset_response = session.get(
                    f"https://api.mux.com/video/v1/assets/{asset_id}",
                    auth=(mux_token_id, mux_token_secret),
                    timeout=60,
                )
                asset_response.raise_for_status()
                return asset_response.json()
            if status == "errored":
                raise Exception(
                    f"Upload failed: {check_data['data'].get('error', {}).get('message', 'Unknown error')}"
                )

            time.sleep(1)

        return {"data": {"id": check_data["data"].get("asset_id"), "status": "preparing"}}

    raise RuntimeError("Mux upload: unexpected retry exhaustion")

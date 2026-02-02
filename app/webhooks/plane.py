"""Plane webhook endpoint for receiving work item events."""

import hashlib
import hmac
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import get_settings
from app.models.plane import PlaneWebhookEvent
from app.services.sync_service import SyncOutcome, SyncResult, SyncService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def verify_plane_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify Plane webhook signature.

    Plane uses HMAC-SHA256; the header is the hex digest (with or without "sha256=").
    Doc: https://developers.plane.so/dev-tools/intro-webhooks
    """
    if not signature:
        return False
    # Plane sends raw hex; accept both "sha256=hex" and "hex"
    expected_hex = signature[7:] if signature.startswith("sha256=") else signature.strip()
    if not expected_hex:
        return False
    # Strip secret (trailing newline in .env is common)
    secret = (secret or "").strip()
    if not secret:
        return False

    secret_bytes = secret.encode("utf-8")
    # Verify against raw body (what we received)
    computed = hmac.new(
        key=secret_bytes,
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(computed, expected_hex):
        logger.debug("Plane webhook signature verified (raw body, len=%d)", len(body))
        return True
    logger.debug(
        "Plane signature mismatch on raw body (len=%d), trying canonical JSON variants",
        len(body),
    )
    # If behind a proxy, body may differ; try verifying with re-serialized JSON (Plane doc)
    try:
        payload = json.loads(body.decode("utf-8"))
        for separators, sort_keys in [
            ((",", ":"), False),
            ((",", ":"), True),
            (None, True),  # default separators (with spaces)
        ]:
            kwargs = {"sort_keys": sort_keys}
            if separators is not None:
                kwargs["separators"] = separators
            canonical = json.dumps(payload, **kwargs).encode("utf-8")
            computed_canonical = hmac.new(
                key=secret_bytes,
                msg=canonical,
                digestmod=hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(computed_canonical, expected_hex):
                logger.debug(
                    "Plane webhook signature verified (canonical JSON, sort_keys=%s)",
                    sort_keys,
                )
                return True
    except Exception as e:
        logger.debug("Plane canonical verification failed: %s", e)
    return False


async def get_sync_service() -> SyncService:
    """Dependency to get the sync service instance."""
    # This will be overridden by the app's dependency injection
    raise NotImplementedError("Sync service not configured")


@router.post("/plane", status_code=status.HTTP_200_OK)
async def plane_webhook(
    request: Request,
    x_plane_signature: Annotated[str | None, Header()] = None,
    sync_service: SyncService = Depends(get_sync_service),
) -> dict[str, str | dict[str, Any] | None]:
    """
    Handle Plane webhook events.

    This endpoint receives webhook events from Plane when work item
    statuses are changed, and syncs the status to GitHub.

    Headers:
        X-Plane-Signature: HMAC signature for payload verification (sha256=<hex>)

    Plane webhook payload structure:
    {
        "event": "work_item.updated",
        "action": "updated",
        "webhook_id": "...",
        "workspace_id": "...",
        "project_id": "...",
        "data": {
            "id": "work_item_id",
            "state": "new_state_id",
            ...
        }
    }
    """
    settings = get_settings()
    body = await request.body()

    # Verify webhook signature if secret is configured (STRONGLY RECOMMENDED)
    if settings.plane_webhook_secret:
        # Read signature from headers (proxy may alter casing)
        signature = (
            x_plane_signature
            or request.headers.get("x-plane-signature")
            or request.headers.get("X-Plane-Signature")
        )
        if not signature:
            logger.warning("Missing Plane webhook signature")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-Plane-Signature header",
            )

        if not verify_plane_signature(body, signature, settings.plane_webhook_secret):
            logger.warning(
                "Invalid Plane webhook signature (body_len=%d). "
                "Check PLANE_WEBHOOK_SECRET matches the secret in Plane webhook settings.",
                len(body),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature",
            )
    else:
        logger.warning(
            "PLANE_WEBHOOK_SECRET not configured - webhook signature verification DISABLED. "
            "This is a security risk!"
        )

    # Parse the raw payload
    try:
        payload = json.loads(body)
    except Exception as e:
        logger.error(f"Failed to parse Plane webhook JSON: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON: {e}",
        )

    event_type = payload.get("event", "unknown")
    logger.info(f"Received Plane webhook: event={event_type}")
    logger.debug(
        "Plane webhook body: %s",
        json.dumps(
            {
                k: v
                for k, v in payload.items()
                if k in ("event", "action", "webhook_id", "workspace_id", "project_id", "data")
            },
            default=str,
        ),
    )

    # Only process work item events
    if not event_type.startswith("work_item."):
        logger.debug(f"Ignoring event type: {event_type}")
        return {
            "status": "ignored",
            "reason": f"Event type '{event_type}' not handled",
        }

    # Parse into our model
    try:
        event = PlaneWebhookEvent(**payload)
    except Exception as e:
        logger.error(f"Failed to parse Plane webhook payload: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payload structure: {e}",
        )

    # Check if this is a status change
    if not event.is_status_change:
        logger.debug("Event is not a status change, ignoring")
        return {
            "status": "ignored",
            "reason": "Not a status change event",
        }

    # Perform the sync
    try:
        outcome: SyncOutcome = await sync_service.sync_plane_to_github(event)

        response: dict[str, str | dict[str, Any] | None] = {
            "status": outcome.result.value,
            "message": outcome.message,
        }
        if outcome.details:
            response["details"] = outcome.details

        if outcome.result == SyncResult.ERROR:
            logger.error(f"Sync failed: {outcome.message}")

        return response

    except Exception as e:
        logger.exception(f"Unexpected error processing Plane webhook: {e}")
        return {
            "status": "error",
            "message": str(e),
        }


@router.get("/plane/health")
async def plane_webhook_health() -> dict[str, str]:
    """Health check endpoint for the Plane webhook."""
    return {"status": "ok", "endpoint": "plane"}

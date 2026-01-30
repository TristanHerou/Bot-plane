"""Plane webhook endpoint for receiving work item events."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.models.plane import PlaneWebhookEvent
from app.services.sync_service import SyncOutcome, SyncResult, SyncService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def get_sync_service() -> SyncService:
    """Dependency to get the sync service instance."""
    # This will be overridden by the app's dependency injection
    raise NotImplementedError("Sync service not configured")


@router.post("/plane", status_code=status.HTTP_200_OK)
async def plane_webhook(
    request: Request,
    sync_service: SyncService = Depends(get_sync_service),
) -> dict[str, str | dict[str, Any] | None]:
    """
    Handle Plane webhook events.

    This endpoint receives webhook events from Plane when work item
    statuses are changed, and syncs the status to GitHub.

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
    # Parse the raw payload first for logging
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse Plane webhook JSON: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON: {e}",
        )

    event_type = payload.get("event", "unknown")
    logger.info(f"Received Plane webhook: event={event_type}")

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

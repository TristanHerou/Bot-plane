"""GitHub webhook endpoint for receiving issue events."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.models.github import GitHubIssueEvent
from app.services.github_service import GitHubService
from app.services.sync_service import SyncOutcome, SyncResult, SyncService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def get_sync_service() -> SyncService:
    """Dependency to get the sync service instance."""
    # This will be overridden by the app's dependency injection
    raise NotImplementedError("Sync service not configured")


async def get_github_service() -> GitHubService:
    """Dependency to get the GitHub service instance."""
    raise NotImplementedError("GitHub service not configured")


@router.post("/github", status_code=status.HTTP_200_OK)
async def github_webhook(
    request: Request,
    x_github_event: Annotated[str, Header()],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
    sync_service: SyncService = Depends(get_sync_service),
    github_service: GitHubService = Depends(get_github_service),
) -> dict[str, str | dict[str, str | list[str] | None] | None]:
    """
    Handle GitHub webhook events.

    This endpoint receives webhook events from GitHub when issues are
    closed or reopened, and syncs the status to Plane.

    Headers:
        X-GitHub-Event: Event type (e.g., 'issues')
        X-Hub-Signature-256: HMAC signature for payload verification
        X-GitHub-Delivery: Unique delivery ID
    """
    delivery_id = x_github_delivery or "unknown"
    logger.info(f"Received GitHub webhook: event={x_github_event}, delivery={delivery_id}")

    # Verify webhook signature
    if x_hub_signature_256:
        body = await request.body()
        if not github_service.verify_webhook_signature(body, x_hub_signature_256):
            logger.warning(f"Invalid webhook signature for delivery {delivery_id}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature",
            )

    # Only process issue events
    if x_github_event != "issues":
        logger.debug(f"Ignoring event type: {x_github_event}")
        return {
            "status": "ignored",
            "reason": f"Event type '{x_github_event}' not handled",
        }

    # Parse the payload
    try:
        body = await request.body()
        # Re-parse as JSON since we already read the body
        import json

        payload = json.loads(body)
        event = GitHubIssueEvent(**payload)
    except Exception as e:
        logger.error(f"Failed to parse webhook payload: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payload: {e}",
        )

    # Check if this is a relevant action
    if not event.is_sync_relevant:
        logger.debug(f"Ignoring action: {event.action}")
        return {
            "status": "ignored",
            "reason": f"Action '{event.action}' not relevant for sync",
        }

    # Perform the sync
    try:
        outcome: SyncOutcome = await sync_service.sync_github_to_plane(event)

        response: dict[str, str | dict[str, str | list[str] | None] | None] = {
            "status": outcome.result.value,
            "message": outcome.message,
        }
        if outcome.details:
            response["details"] = outcome.details

        if outcome.result == SyncResult.ERROR:
            logger.error(f"Sync failed: {outcome.message}")
            # Don't raise HTTP error - acknowledge webhook was received
            # but log the error for debugging

        return response

    except Exception as e:
        logger.exception(f"Unexpected error processing webhook: {e}")
        # Return 200 to acknowledge receipt, but indicate error
        return {
            "status": "error",
            "message": str(e),
        }


@router.get("/github/health")
async def github_webhook_health() -> dict[str, str]:
    """Health check endpoint for the GitHub webhook."""
    return {"status": "ok", "endpoint": "github"}

"""Synchronization service for bidirectional GitHub-Plane sync."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.config import StatusMapping
from app.models.github import SYNC_LABEL, GitHubIssueEvent
from app.models.plane import GitHubIssueRef, PlaneWebhookEvent
from app.services.github_service import GitHubService
from app.services.plane_service import PlaneService

logger = logging.getLogger(__name__)


class SyncDirection(str, Enum):
    """Direction of synchronization."""

    GITHUB_TO_PLANE = "github_to_plane"
    PLANE_TO_GITHUB = "plane_to_github"


class SyncResult(str, Enum):
    """Result of a sync operation."""

    SUCCESS = "success"
    SKIPPED = "skipped"
    NO_MAPPING = "no_mapping"
    NO_LINKED_ITEM = "no_linked_item"
    LOOP_PREVENTED = "loop_prevented"
    ERROR = "error"


@dataclass
class SyncOutcome:
    """Outcome of a sync operation."""

    result: SyncResult
    direction: SyncDirection
    message: str
    details: dict[str, Any] | None = None


class SyncService:
    """
    Orchestrates bidirectional synchronization between GitHub and Plane.

    Handles:
    - GitHub issue closed/reopened -> Update Plane work item state
    - Plane work item state changed -> Update GitHub issue state

    Implements loop prevention to avoid infinite sync cycles.
    """

    def __init__(
        self,
        github_service: GitHubService,
        plane_service: PlaneService,
        status_mapping: StatusMapping,
    ) -> None:
        self.github = github_service
        self.plane = plane_service
        self.mapping = status_mapping

        # Track recent syncs to prevent loops
        # Key: "{work_item_id}:{state_id}" or "{owner}/{repo}#{issue_number}:{state}"
        self._recent_syncs: dict[str, float] = {}
        self._sync_cooldown_seconds = 30  # Ignore duplicate syncs within this window

    def _is_duplicate_sync(self, sync_key: str) -> bool:
        """Check if this sync was recently performed."""
        import time

        now = time.time()
        # Clean old entries
        self._recent_syncs = {
            k: v
            for k, v in self._recent_syncs.items()
            if now - v < self._sync_cooldown_seconds
        }

        if sync_key in self._recent_syncs:
            return True

        self._recent_syncs[sync_key] = now
        return False

    async def sync_github_to_plane(
        self, event: GitHubIssueEvent
    ) -> SyncOutcome:
        """
        Sync a GitHub issue state change to Plane.

        Called when a GitHub issue is closed or reopened.
        """
        direction = SyncDirection.GITHUB_TO_PLANE
        issue = event.issue
        repo = event.repository

        logger.info(
            f"Processing GitHub event: {event.action} for "
            f"{repo.full_name}#{issue.number}"
        )

        # Check if this is a sync-relevant event
        if not event.is_sync_relevant:
            return SyncOutcome(
                result=SyncResult.SKIPPED,
                direction=direction,
                message=f"Event action '{event.action}' not relevant for sync",
            )

        # Check for loop prevention - if issue has our sync label and was
        # recently modified, this might be a loop
        if issue.has_label(SYNC_LABEL):
            sync_key = f"gh:{repo.full_name}#{issue.number}:{issue.state}"
            if self._is_duplicate_sync(sync_key):
                logger.info(f"Skipping duplicate sync for {repo.full_name}#{issue.number}")
                return SyncOutcome(
                    result=SyncResult.LOOP_PREVENTED,
                    direction=direction,
                    message="Duplicate sync prevented",
                )

        # Determine the target Plane status
        plane_status_name = self.mapping.get_plane_status_for_github_action(event.action)
        if not plane_status_name:
            return SyncOutcome(
                result=SyncResult.NO_MAPPING,
                direction=direction,
                message=f"No Plane status mapping for GitHub action '{event.action}'",
            )

        # Get the Plane state ID
        state_id = await self.plane.get_state_id_for_name(plane_status_name)
        if not state_id:
            # Try from pre-configured mapping
            state_id = self.mapping.get_plane_state_id(plane_status_name)

        if not state_id:
            return SyncOutcome(
                result=SyncResult.NO_MAPPING,
                direction=direction,
                message=f"Plane state ID not found for status '{plane_status_name}'",
            )

        # Find the linked Plane work item
        work_item = await self.plane.find_work_item_by_github_issue(
            repo.owner_name, repo.name, issue.number
        )

        if not work_item:
            logger.warning(
                f"No Plane work item found linked to {repo.full_name}#{issue.number}"
            )
            return SyncOutcome(
                result=SyncResult.NO_LINKED_ITEM,
                direction=direction,
                message=f"No Plane work item linked to {repo.full_name}#{issue.number}",
            )

        # Check if state already matches
        if work_item.state == state_id:
            logger.info(
                f"Plane work item {work_item.id} already has state '{plane_status_name}'"
            )
            return SyncOutcome(
                result=SyncResult.SKIPPED,
                direction=direction,
                message="State already matches",
            )

        # Update the Plane work item
        try:
            await self.plane.update_work_item_state(work_item.id, state_id)

            # Record this sync to prevent loops
            self._recent_syncs[f"plane:{work_item.id}:{state_id}"] = __import__(
                "time"
            ).time()

            logger.info(
                f"Synced GitHub {repo.full_name}#{issue.number} ({event.action}) "
                f"-> Plane work item {work_item.id} ({plane_status_name})"
            )

            return SyncOutcome(
                result=SyncResult.SUCCESS,
                direction=direction,
                message=f"Updated Plane work item to '{plane_status_name}'",
                details={
                    "work_item_id": work_item.id,
                    "new_state": plane_status_name,
                    "github_issue": f"{repo.full_name}#{issue.number}",
                },
            )

        except Exception as e:
            logger.error(f"Failed to update Plane work item: {e}")
            return SyncOutcome(
                result=SyncResult.ERROR,
                direction=direction,
                message=f"Failed to update Plane: {e}",
            )

    async def sync_plane_to_github(
        self, event: PlaneWebhookEvent
    ) -> SyncOutcome:
        """
        Sync a Plane work item state change to GitHub.

        Called when a Plane work item status is changed.
        """
        direction = SyncDirection.PLANE_TO_GITHUB
        work_item_id = event.work_item_id

        logger.info(f"Processing Plane event for work item {work_item_id}")

        # Check if this is a status change
        if not event.is_status_change:
            return SyncOutcome(
                result=SyncResult.SKIPPED,
                direction=direction,
                message="Event is not a status change",
            )

        new_state_id = event.new_state_id
        if not new_state_id:
            return SyncOutcome(
                result=SyncResult.SKIPPED,
                direction=direction,
                message="No new state ID in event",
            )

        # Check for duplicate sync (loop prevention)
        sync_key = f"plane:{work_item_id}:{new_state_id}"
        if self._is_duplicate_sync(sync_key):
            logger.info(f"Skipping duplicate sync for work item {work_item_id}")
            return SyncOutcome(
                result=SyncResult.LOOP_PREVENTED,
                direction=direction,
                message="Duplicate sync prevented",
            )

        # Get the state name
        state = await self.plane.get_state_by_id(new_state_id)
        if not state:
            return SyncOutcome(
                result=SyncResult.NO_MAPPING,
                direction=direction,
                message=f"Unknown state ID: {new_state_id}",
            )

        # Determine the target GitHub state
        github_state = self.mapping.get_github_state_for_plane_status(state.name)
        if not github_state:
            return SyncOutcome(
                result=SyncResult.NO_MAPPING,
                direction=direction,
                message=f"No GitHub state mapping for Plane status '{state.name}'",
            )

        # Get linked GitHub issues
        try:
            links = await self.plane.get_work_item_links(work_item_id)
        except Exception as e:
            logger.error(f"Failed to get work item links: {e}")
            return SyncOutcome(
                result=SyncResult.ERROR,
                direction=direction,
                message=f"Failed to get work item links: {e}",
            )

        github_refs: list[GitHubIssueRef] = []
        for link in links:
            if link.is_github_issue and link.github_issue_ref:
                github_refs.append(link.github_issue_ref)

        if not github_refs:
            return SyncOutcome(
                result=SyncResult.NO_LINKED_ITEM,
                direction=direction,
                message="No GitHub issues linked to this work item",
            )

        # Update all linked GitHub issues
        updated_issues: list[str] = []
        errors: list[str] = []

        for ref in github_refs:
            try:
                # Check current state to avoid unnecessary updates
                current_issue = await self.github.get_issue(
                    ref.owner, ref.repo, ref.issue_number
                )
                current_state = current_issue.get("state")

                if current_state == github_state:
                    logger.info(
                        f"GitHub issue {ref.full_name}#{ref.issue_number} "
                        f"already has state '{github_state}'"
                    )
                    continue

                # Update the issue
                await self.github.update_issue_state(
                    ref.owner, ref.repo, ref.issue_number, github_state
                )

                # Add sync label to prevent loop
                await self.github.add_label(
                    ref.owner, ref.repo, ref.issue_number, SYNC_LABEL
                )

                # Record this sync
                self._recent_syncs[
                    f"gh:{ref.full_name}#{ref.issue_number}:{github_state}"
                ] = __import__("time").time()

                updated_issues.append(f"{ref.full_name}#{ref.issue_number}")
                logger.info(
                    f"Synced Plane work item {work_item_id} ({state.name}) "
                    f"-> GitHub {ref.full_name}#{ref.issue_number} ({github_state})"
                )

            except Exception as e:
                error_msg = f"{ref.full_name}#{ref.issue_number}: {e}"
                errors.append(error_msg)
                logger.error(f"Failed to update GitHub issue: {error_msg}")

        if updated_issues:
            return SyncOutcome(
                result=SyncResult.SUCCESS,
                direction=direction,
                message=f"Updated {len(updated_issues)} GitHub issue(s)",
                details={
                    "work_item_id": work_item_id,
                    "plane_state": state.name,
                    "github_state": github_state,
                    "updated_issues": updated_issues,
                    "errors": errors if errors else None,
                },
            )
        elif errors:
            return SyncOutcome(
                result=SyncResult.ERROR,
                direction=direction,
                message=f"Failed to update GitHub issues: {', '.join(errors)}",
            )
        else:
            return SyncOutcome(
                result=SyncResult.SKIPPED,
                direction=direction,
                message="All linked issues already have the correct state",
            )

    async def initialize(self) -> None:
        """
        Initialize the sync service.

        Loads Plane states and updates the status mapping with actual state IDs.
        """
        logger.info("Initializing sync service...")
        await self.plane.get_project_states()
        self.plane.update_status_mapping_with_state_ids(self.mapping)
        logger.info("Sync service initialized")

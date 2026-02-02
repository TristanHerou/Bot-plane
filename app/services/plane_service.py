"""Plane API service for interacting with Plane.so."""

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import Settings, StatusMapping
from app.models.plane import PlaneLink, PlaneState, PlaneWorkItem

logger = logging.getLogger(__name__)


class PlaneAPIError(Exception):
    """Raised when Plane API call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PlaneRateLimitError(PlaneAPIError):
    """Raised when Plane API rate limit is exceeded."""

    def __init__(self, retry_after: int = 60) -> None:
        super().__init__(f"Rate limit exceeded. Retry after {retry_after}s", 429)
        self.retry_after = retry_after


class PlaneService:
    """Service for interacting with Plane API."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.plane_base_url.rstrip("/")
        self.workspace_slug = settings.plane_workspace_slug
        self.project_id = settings.plane_project_id

        # Rate limiting
        self._request_times: list[float] = []
        self._rate_limit = settings.plane_rate_limit
        self._rate_window = 60  # 1 minute

        # Cache for state mappings
        self._states_cache: dict[str, PlaneState] | None = None
        self._states_by_name: dict[str, PlaneState] | None = None

    def _get_headers(self) -> dict[str, str]:
        """Get headers for Plane API requests."""
        return {
            "X-API-Key": self.settings.plane_api_key,
            "Content-Type": "application/json",
        }

    async def _check_rate_limit(self) -> None:
        """Check and enforce rate limiting."""
        now = time.time()
        # Remove requests older than the rate window
        self._request_times = [
            t for t in self._request_times if now - t < self._rate_window
        ]

        if len(self._request_times) >= self._rate_limit:
            # Calculate wait time
            oldest = min(self._request_times)
            wait_time = self._rate_window - (now - oldest) + 1
            logger.warning(f"Rate limit reached, waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            # Clear old requests after waiting
            self._request_times = []

        self._request_times.append(now)

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        """Make an authenticated request to the Plane API."""
        await self._check_rate_limit()

        url = f"{self.base_url}{endpoint}"

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                url,
                headers=self._get_headers(),
                json=json_data,
                timeout=30.0,
            )

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                raise PlaneRateLimitError(retry_after)

            if response.status_code >= 400:
                logger.error(
                    f"Plane API error: {method} {endpoint} - "
                    f"{response.status_code} - {response.text}"
                )
                raise PlaneAPIError(
                    f"Plane API error: {response.status_code}",
                    status_code=response.status_code,
                )

            if response.status_code == 204:
                return {}

            text = response.text
            if not text or not text.strip():
                return {}

            try:
                return response.json()
            except ValueError as e:
                logger.error(
                    "Plane API returned non-JSON: %s %s - body: %s",
                    method,
                    endpoint,
                    text[:200] if len(text) > 200 else text,
                )
                raise PlaneAPIError(f"Invalid JSON response: {e}") from e

    def _work_item_endpoint(self, work_item_id: str, suffix: str = "") -> str:
        """Build the endpoint URL for a work item."""
        base = (
            f"/api/v1/workspaces/{self.workspace_slug}"
            f"/projects/{self.project_id}"
            f"/work-items/{work_item_id}"
        )
        return f"{base}/{suffix}" if suffix else base

    async def get_work_item(self, work_item_id: str) -> PlaneWorkItem:
        """Get a work item by ID."""
        data = await self._make_request("GET", self._work_item_endpoint(work_item_id))
        if isinstance(data, dict):
            return PlaneWorkItem(**data)
        raise PlaneAPIError("Unexpected response format")

    async def get_work_item_links(self, work_item_id: str) -> list[PlaneLink]:
        """
        Get all links attached to a work item.

        This is used to find GitHub issues linked to Plane work items.
        """
        data = await self._make_request(
            "GET", self._work_item_endpoint(work_item_id, "links")
        )
        if isinstance(data, list):
            return [PlaneLink(**link) for link in data]
        # Handle paginated response
        if isinstance(data, dict) and "results" in data:
            return [PlaneLink(**link) for link in data["results"]]
        return []

    async def update_work_item_state(
        self, work_item_id: str, state_id: str
    ) -> PlaneWorkItem:
        """
        Update the state of a work item.

        Args:
            work_item_id: The work item UUID
            state_id: The new state UUID

        Returns:
            Updated work item
        """
        data = await self._make_request(
            "PATCH",
            self._work_item_endpoint(work_item_id),
            json_data={"state": state_id},
        )
        if isinstance(data, dict):
            logger.info(f"Updated Plane work item {work_item_id} state to {state_id}")
            return PlaneWorkItem(**data)
        raise PlaneAPIError("Unexpected response format")

    async def get_project_states(self) -> list[PlaneState]:
        """
        Get all states for the configured project.

        Returns cached results if available.
        """
        if self._states_cache is not None:
            return list(self._states_cache.values())

        endpoint = (
            f"/api/v1/workspaces/{self.workspace_slug}"
            f"/projects/{self.project_id}/states/"
        )
        data = await self._make_request("GET", endpoint)

        states: list[PlaneState] = []
        if isinstance(data, list):
            states = [PlaneState(**s) for s in data]
        elif isinstance(data, dict):
            if "results" in data:
                states = [PlaneState(**s) for s in data["results"]]
            elif "data" in data and isinstance(data["data"], list):
                states = [PlaneState(**s) for s in data["data"]]

        # Cache the states
        self._states_cache = {s.id: s for s in states}
        self._states_by_name = {s.name: s for s in states}

        if not states:
            logger.warning(
                "Loaded 0 project states. Check PLANE_WORKSPACE_SLUG, PLANE_PROJECT_ID "
                "and PLANE_API_KEY (workspace: %s, project: %s)",
                self.workspace_slug,
                self.project_id,
            )
        else:
            logger.info(f"Loaded {len(states)} project states: {[s.name for s in states]}")
        return states

    async def get_state_by_name(self, name: str) -> PlaneState | None:
        """Get a state by its name."""
        if self._states_by_name is None:
            await self.get_project_states()
        return self._states_by_name.get(name) if self._states_by_name else None

    async def get_state_by_id(self, state_id: str) -> PlaneState | None:
        """Get a state by its ID."""
        if self._states_cache is None:
            await self.get_project_states()
        return self._states_cache.get(state_id) if self._states_cache else None

    async def get_state_id_for_name(self, name: str) -> str | None:
        """Get the state ID for a given state name."""
        state = await self.get_state_by_name(name)
        return state.id if state else None

    async def find_work_item_by_github_issue(
        self, owner: str, repo: str, issue_number: int
    ) -> PlaneWorkItem | None:
        """
        Find a Plane work item that links to a specific GitHub issue.

        This performs a search through work items. For better performance,
        consider implementing a cache or using Plane's search API if available.

        Note: This is a potentially expensive operation as it may need to
        iterate through many work items. In production, consider maintaining
        a mapping database.
        """
        github_url = f"https://github.com/{owner}/{repo}/issues/{issue_number}"

        # List work items and check their links
        # Note: This is a simplified implementation. In production,
        # you might want to use Plane's search functionality or maintain
        # your own mapping database.
        endpoint = (
            f"/api/v1/workspaces/{self.workspace_slug}"
            f"/projects/{self.project_id}/work-items"
        )

        data = await self._make_request("GET", endpoint)
        work_items: list[dict[str, Any]] = []

        if isinstance(data, list):
            work_items = data
        elif isinstance(data, dict) and "results" in data:
            work_items = data["results"]

        for item_data in work_items:
            work_item_id = item_data.get("id")
            if not work_item_id:
                continue

            try:
                links = await self.get_work_item_links(work_item_id)
                for link in links:
                    if link.url == github_url or link.url == github_url.rstrip("/"):
                        return PlaneWorkItem(**item_data)
            except PlaneAPIError:
                continue

        return None

    def update_status_mapping_with_state_ids(
        self, mapping: StatusMapping
    ) -> None:
        """
        Update the status mapping with actual state IDs from Plane.

        This should be called after loading project states.
        """
        if self._states_by_name is None:
            logger.warning("States not loaded, cannot update mapping")
            return

        if not self._states_by_name:
            logger.warning(
                "No states in project; mapping will use names only. "
                "Create states in Plane (e.g. Backlog, Todo, In Progress, Done) or check project/workspace."
            )
            return

        for name in mapping.plane_to_github:
            if name not in mapping.plane_state_ids:
                state = self._states_by_name.get(name)
                if state:
                    mapping.plane_state_ids[name] = state.id
                    logger.debug(f"Mapped state '{name}' to ID '{state.id}'")
                else:
                    logger.warning(f"State '{name}' not found in project")

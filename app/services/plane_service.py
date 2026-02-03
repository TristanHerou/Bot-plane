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
    """Service for interacting with Plane API. Supports a single project or all projects in the workspace."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.plane_base_url.rstrip("/")
        self.workspace_slug = settings.plane_workspace_slug
        # Single project from config, or None to use all workspace projects (resolved in ensure_project_ids)
        self._project_ids: list[str] | None = (
            [settings.plane_project_id] if settings.plane_project_id else None
        )

        # Rate limiting
        self._request_times: list[float] = []
        self._rate_limit = settings.plane_rate_limit
        self._rate_window = 60  # 1 minute

        # Cache for state mappings: per project and global (state_id -> state for get_state_by_id)
        self._states_cache_by_project: dict[str, dict[str, PlaneState]] = {}
        self._states_by_name_by_project: dict[str, dict[str, PlaneState]] = {}
        self._states_cache_global: dict[str, PlaneState] = {}
        self._projects_loaded = False

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
        params: dict[str, Any] | None = None,
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
                params=params,
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

    async def ensure_project_ids(self) -> list[str]:
        """Resolve and return the list of project IDs to use (single from config or all in workspace)."""
        if self._project_ids is not None:
            return self._project_ids
        projects = await self.list_workspace_projects()
        self._project_ids = [p["id"] for p in projects if p.get("id")]
        self._projects_loaded = True
        logger.info(
            "Using all projects in workspace %s: %s",
            self.workspace_slug,
            self._project_ids,
        )
        return self._project_ids

    async def list_workspace_projects(self) -> list[dict[str, Any]]:
        """List all projects in the workspace."""
        endpoint = f"/api/v1/workspaces/{self.workspace_slug}/projects/"
        data = await self._make_request("GET", endpoint)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            return data["data"]
        return []

    def _work_item_endpoint(
        self, work_item_id: str, project_id: str, suffix: str = ""
    ) -> str:
        """Build the endpoint URL for a work item in a given project."""
        base = (
            f"/api/v1/workspaces/{self.workspace_slug}"
            f"/projects/{project_id}"
            f"/work-items/{work_item_id}"
        )
        return f"{base}/{suffix}" if suffix else base

    async def get_work_item(self, work_item_id: str, project_id: str) -> PlaneWorkItem:
        """Get a work item by ID (Plane API expects trailing slash)."""
        endpoint = self._work_item_endpoint(work_item_id, project_id) + "/"
        data = await self._make_request("GET", endpoint)
        if not isinstance(data, dict) or not data or "id" not in data:
            raise PlaneAPIError(
                "Empty or invalid response for work item (GET may need trailing slash or API returned empty)"
            )
        try:
            return PlaneWorkItem(**data)
        except Exception as e:
            raise PlaneAPIError(f"Invalid work item data: {e}") from e

    async def get_work_item_links(
        self, work_item_id: str, project_id: str
    ) -> list[PlaneLink]:
        """
        Get all links attached to a work item.

        This is used to find GitHub issues linked to Plane work items.
        """
        data = await self._make_request(
            "GET", self._work_item_endpoint(work_item_id, project_id, "links/")
        )
        if isinstance(data, list):
            return [PlaneLink(**link) for link in data]
        # Handle paginated response
        if isinstance(data, dict) and "results" in data:
            return [PlaneLink(**link) for link in data["results"]]
        return []

    async def update_work_item_state(
        self, work_item_id: str, state_id: str, project_id: str
    ) -> PlaneWorkItem | None:
        """
        Update the state of a work item.

        Args:
            work_item_id: The work item UUID
            state_id: The new state UUID
            project_id: The project UUID this work item belongs to

        Returns:
            Updated work item, or None if PATCH succeeded but re-fetch returned empty.
        """
        endpoint = self._work_item_endpoint(work_item_id, project_id) + "/"
        data = await self._make_request(
            "PATCH",
            endpoint,
            json_data={"state": state_id},
        )
        logger.info(f"Updated Plane work item {work_item_id} state to {state_id}")
        if isinstance(data, dict) and data and "id" in data:
            try:
                return PlaneWorkItem(**data)
            except Exception:
                pass
        # Plane API may return empty body on PATCH; re-fetch the work item
        try:
            return await self.get_work_item(work_item_id, project_id)
        except PlaneAPIError:
            logger.warning(
                "Plane PATCH succeeded but re-fetch of work item %s returned empty; state was updated",
                work_item_id,
            )
            return None

    async def add_work_item_comment(
        self, work_item_id: str, comment_html: str, project_id: str
    ) -> None:
        """
        Add a comment on a Plane work item (appears in activity).

        Used to notify users when the bot syncs from GitHub.
        """
        endpoint = (
            f"/api/v1/workspaces/{self.workspace_slug}"
            f"/projects/{project_id}"
            f"/work-items/{work_item_id}/comments/"
        )
        body = {"comment_html": f"<p>{comment_html}</p>", "access": "EXTERNAL"}
        try:
            await self._make_request("POST", endpoint, json_data=body)
        except PlaneAPIError as e:
            logger.warning(
                "Failed to add comment on Plane work item %s: %s",
                work_item_id,
                e,
            )

    async def _get_project_states_for_project(
        self, project_id: str
    ) -> list[PlaneState]:
        """Load states for a single project (no cache check)."""
        endpoint = (
            f"/api/v1/workspaces/{self.workspace_slug}"
            f"/projects/{project_id}/states/"
        )
        try:
            data = await self._make_request("GET", endpoint)
        except PlaneAPIError as e:
            if e.status_code == 403:
                logger.warning(
                    "Plane API 403 listing states for project %s. Continuing with 0 states.",
                    project_id,
                )
                return []
            raise

        states: list[PlaneState] = []
        if isinstance(data, list):
            states = [PlaneState(**s) for s in data]
        elif isinstance(data, dict):
            if "results" in data:
                states = [PlaneState(**s) for s in data["results"]]
            elif "data" in data and isinstance(data["data"], list):
                states = [PlaneState(**s) for s in data["data"]]
        return states

    async def get_project_states(self) -> list[PlaneState]:
        """
        Get all states for all configured projects (single or whole workspace).

        Returns cached results if available. Caches per project and globally by state_id.
        """
        project_ids = await self.ensure_project_ids()
        if not project_ids:
            logger.warning("No project IDs available")
            return []

        all_states: list[PlaneState] = []
        for project_id in project_ids:
            if project_id in self._states_cache_by_project:
                all_states.extend(self._states_cache_by_project[project_id].values())
                continue
            states = await self._get_project_states_for_project(project_id)
            by_id = {s.id: s for s in states}
            by_name = {s.name: s for s in states}
            self._states_cache_by_project[project_id] = by_id
            self._states_by_name_by_project[project_id] = by_name
            for s in states:
                self._states_cache_global[s.id] = s
            all_states.extend(states)
            if states:
                logger.info(
                    "Loaded %d states for project %s: %s",
                    len(states),
                    project_id,
                    [s.name for s in states],
                )
            else:
                logger.warning("Loaded 0 states for project %s", project_id)

        if not all_states:
            logger.warning(
                "Loaded 0 project states. Check PLANE_WORKSPACE_SLUG, PLANE_PROJECT_ID (or no projects in workspace) "
                "and PLANE_API_KEY (workspace: %s)",
                self.workspace_slug,
            )
        return all_states

    async def get_state_by_name(
        self, name: str, project_id: str | None = None
    ) -> PlaneState | None:
        """Get a state by its name, optionally for a specific project."""
        await self.get_project_states()
        if project_id and project_id in self._states_by_name_by_project:
            return self._states_by_name_by_project[project_id].get(name)
        # Fallback: first project that has this state name
        for by_name in self._states_by_name_by_project.values():
            if name in by_name:
                return by_name[name]
        return None

    async def get_state_by_id(self, state_id: str) -> PlaneState | None:
        """Get a state by its ID (from global cache)."""
        await self.get_project_states()
        return self._states_cache_global.get(state_id)

    async def get_state_by_id_fetch(self, state_id: str) -> PlaneState | None:
        """
        Fetch a state by ID from the API (when not in cache).

        Tries each project until the state is found.
        """
        if state_id in self._states_cache_global:
            return self._states_cache_global[state_id]
        project_ids = await self.ensure_project_ids()
        for project_id in project_ids:
            endpoint = (
                f"/api/v1/workspaces/{self.workspace_slug}"
                f"/projects/{project_id}/states/{state_id}/"
            )
            try:
                data = await self._make_request("GET", endpoint)
            except PlaneAPIError:
                continue
            if not isinstance(data, dict) or not data or "id" not in data:
                continue
            try:
                state = PlaneState(**data)
                self._states_cache_global[state.id] = state
                if project_id not in self._states_cache_by_project:
                    self._states_cache_by_project[project_id] = {}
                    self._states_by_name_by_project[project_id] = {}
                self._states_cache_by_project[project_id][state.id] = state
                self._states_by_name_by_project[project_id][state.name] = state
                return state
            except Exception:
                continue
        return None

    async def get_state_id_for_name(
        self, name: str, project_id: str | None = None
    ) -> str | None:
        """Get the state ID for a given state name, optionally for a specific project."""
        state = await self.get_state_by_name(name, project_id)
        return state.id if state else None

    async def find_work_item_by_github_issue(
        self, owner: str, repo: str, issue_number: int
    ) -> PlaneWorkItem | None:
        """
        Find a Plane work item that links to a specific GitHub issue.

        Searches across all configured projects (single or whole workspace).
        Matches links by owner/repo/issue_number (insensitive to URL format).
        """
        project_ids = await self.ensure_project_ids()
        if not project_ids:
            return None

        for project_id in project_ids:
            endpoint = (
                f"/api/v1/workspaces/{self.workspace_slug}"
                f"/projects/{project_id}/work-items/"
            )
            limit = 100
            offset = 0

            while True:
                data = await self._make_request(
                    "GET", endpoint, params={"limit": limit, "offset": offset}
                )
                page: list[dict[str, Any]] = []
                if isinstance(data, list):
                    page = data
                elif isinstance(data, dict) and "results" in data:
                    page = data["results"]
                elif isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                    page = data["data"]
                if not page:
                    break
                for item_data in page:
                    work_item_id = item_data.get("id")
                    if not work_item_id:
                        continue
                    try:
                        links = await self.get_work_item_links(work_item_id, project_id)
                        for link in links:
                            ref = link.github_issue_ref
                            if ref is None:
                                continue
                            if (
                                ref.owner.lower() == owner.lower()
                                and ref.repo.lower() == repo.lower()
                                and ref.issue_number == issue_number
                            ):
                                # Ensure project is set for callers that need it
                                if "project" not in item_data or not item_data["project"]:
                                    item_data = {**item_data, "project": project_id}
                                work_item = PlaneWorkItem(**item_data)
                                logger.info(
                                    "Found Plane work item %s (project %s) linked to %s/%s#%s",
                                    work_item.id,
                                    project_id,
                                    owner,
                                    repo,
                                    issue_number,
                                )
                                return work_item
                    except PlaneAPIError:
                        continue
                if len(page) < limit:
                    break
                offset += limit

        logger.info(
            "No Plane work item found linked to %s/%s#%s in %d project(s)",
            owner,
            repo,
            issue_number,
            len(project_ids),
        )
        return None

    def update_status_mapping_with_state_ids(
        self, mapping: StatusMapping
    ) -> None:
        """
        Update the status mapping with actual state IDs from Plane.

        Uses the first available project's states (same names are assumed across projects).
        """
        if not self._states_by_name_by_project:
            logger.warning("States not loaded, cannot update mapping")
            return

        # Use first project that has states for name lookup
        combined_by_name: dict[str, PlaneState] = {}
        for by_name in self._states_by_name_by_project.values():
            combined_by_name.update(by_name)
        if not combined_by_name:
            logger.warning(
                "No states in any project; mapping will use names only. "
                "Create states in Plane (e.g. Backlog, Todo, In Progress, Done) or check project/workspace."
            )
            return

        for name in mapping.plane_to_github:
            if name not in mapping.plane_state_ids:
                state = combined_by_name.get(name)
                if state:
                    mapping.plane_state_ids[name] = state.id
                    logger.debug(f"Mapped state '{name}' to ID '{state.id}'")
                else:
                    logger.warning(f"State '{name}' not found in any project")

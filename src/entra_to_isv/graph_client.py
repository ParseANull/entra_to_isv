from __future__ import annotations

import logging
from typing import Dict, List, Optional

import httpx
import msal

logger = logging.getLogger(__name__)


class GraphClient:
    """Client for fetching users from Microsoft Graph API.
    
    We wrap the Microsoft Graph API to pull user data from Azure AD. Uses client
    credentials flow (app permissions) because we're a background service, not an
    interactive app. Think of this as our dedicated hotline to Microsoft's user
    directory.
    
    The client handles OAuth token acquisition and pagination automatically, so you
    just call list_users() and get back all the users without worrying about the
    plumbing underneath.
    """
    
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scope: str = "https://graph.microsoft.com/.default",
        session: Optional[httpx.Client] = None,
    ) -> None:
        """Initialize Graph API client with Azure AD credentials.
        
        Args:
            tenant_id: Azure AD tenant UUID for your organization.
            client_id: App registration client ID with User.Read.All permission.
            client_secret: The secret password for the app registration.
            scope: OAuth scope to request; defaults to full Graph API access.
            session: Optional httpx.Client for HTTP requests. If None, we create one.
                Useful for testing or custom timeout/proxy configs.
        """
        # authority: str - Initialized with tenant-specific Azure AD endpoint.
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        # self._app: MSAL app - Initialized for client credentials flow.
        # This handles OAuth token acquisition and caching for us.
        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            authority=authority,
            client_credential=client_secret,
        )
        # self._scope: str - Stored for token requests.
        self._scope = scope
        # self._http: httpx.Client - Initialized from param or created with reasonable timeouts.
        # 20s connect, 60s read because sometimes Azure's a bit slow to respond.
        self._http = session or httpx.Client(timeout=httpx.Timeout(20.0, read=60.0))

    def _get_token(self) -> str:
        """Get a valid OAuth access token for Graph API, with caching.
        
        We try the token cache first (silent acquisition) because why waste time
        getting a new token if we already have a valid one? If cache is empty or
        expired, we acquire a fresh token using client credentials flow.
        
        Returns:
            Valid access token string ready to stick in Authorization header.
        
        Raises:
            RuntimeError: When token acquisition fails. This usually means bad
                credentials or network issues.
        """
        # result: Optional[Dict] - Try to get cached token first, initialized from MSAL cache.
        result = self._app.acquire_token_silent(scopes=[self._scope], account=None)
        if not result:  # Evaluated: cache miss or token expired.
            # result re-initialized by acquiring fresh token from Azure AD.
            result = self._app.acquire_token_for_client(scopes=[self._scope])
        if "access_token" not in result:  # Evaluated: sanity check for token presence.
            raise RuntimeError(f"Failed to obtain Graph token: {result}")
        # Return the token string, extracted from result dict.
        return result["access_token"]

    def _headers(self) -> Dict[str, str]:
        """Build HTTP headers with fresh OAuth token.
        
        Returns:
            Dict with Authorization header containing Bearer token.
        """
        # Return dict initialized with current valid token from _get_token().
        return {"Authorization": f"Bearer {self._get_token()}"}

    def list_users(self) -> List[Dict]:
        """Fetch all users from Azure AD with selected attributes.
        
        We paginate through the entire user directory, fetching 200 users at a time
        (because Microsoft imposes limits). The selected fields are the ones we need
        for SCIM mapping - names, email, phone, status, etc. We don't fetch *everything*
        because that'd be wasteful and slow.
        
        Returns:
            List of user dicts, each containing the fields we selected. Could be empty
            if your Azure AD is lonely and has no users (but that'd be weird).
        
        Raises:
            RuntimeError: When Graph API returns an error response (4xx/5xx status).
        """
        # url: str - Initialized with Graph API users endpoint.
        url = "https://graph.microsoft.com/v1.0/users"
        # params: Dict - Initialized with OData query params to limit fields and page size.
        params = {
            "$select": "id,userPrincipalName,givenName,surname,displayName,mail,mobilePhone,accountEnabled",
            "$top": 200,  # Fetch 200 per page - good balance between speed and memory.
        }

        # users: List[Dict] - Initialized empty, we'll accumulate all users here.
        users: List[Dict] = []
        # Loop through all pages until we've fetched everyone.
        while True:
            # resp: httpx.Response - Initialized from HTTP GET request.
            resp = self._http.get(url, headers=self._headers(), params=params)
            if resp.status_code >= 400:  # Evaluated: check for error status.
                raise RuntimeError(f"Graph request failed: {resp.status_code} {resp.text}")

            # data: Dict - Initialized by parsing JSON response body.
            data = resp.json()
            # batch: List[Dict] - Current page of users, extracted from 'value' field.
            batch = data.get("value", [])
            users.extend(batch)  # users updated with new batch.
            logger.debug("Fetched %s users (total=%s)", len(batch), len(users))

            # next_link: Optional[str] - OData continuation URL for next page, or None if done.
            next_link = data.get("@odata.nextLink")
            if not next_link:  # Evaluated: check if there are more pages.
                break  # No more pages - we're done!
            # url updated to next page URL; params set to None (next_link has everything).
            url = next_link
            params = None

        # users: complete list of all users from all pages, ready to return.
        return users

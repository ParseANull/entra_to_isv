from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class VerifyClient:
    """Client for managing users in IBM Security Verify via SCIM 2.0.
    
    We talk to IBM's SCIM API to create, update, find, and deactivate users. SCIM
    (System for Cross-domain Identity Management) is the standard REST API for
    identity operations - think of it as the universal language for user provisioning.
    
    This client uses bearer token authentication and expects JSON payloads that
    conform to SCIM 2.0 User schema. We handle the HTTP plumbing so you can focus
    on the business logic of user sync.
    """
    
    def __init__(self, base_url: str, api_token: str, session: Optional[httpx.Client] = None) -> None:
        """Initialize Verify SCIM client with endpoint and credentials.
        
        Args:
            base_url: SCIM v2 base URL (e.g., https://tenant.verify.ibm.com/v2.0/scim/v2).
                We'll strip trailing slashes to avoid double-slash issues.
            api_token: Bearer token for authenticating SCIM requests.
            session: Optional httpx.Client for HTTP requests. If None, we create one
                with reasonable timeouts. Useful for testing or custom configs.
        """
        # self._base_url: str - Initialized with trailing slash removed for clean URL joins.
        self._base_url = base_url.rstrip("/")
        # self._http: httpx.Client - Initialized from param or created with 20s connect, 60s read.
        self._http = session or httpx.Client(timeout=httpx.Timeout(20.0, read=60.0))
        # self._api_token: str - Stored for building Authorization headers.
        self._api_token = api_token

    def _headers(self) -> Dict[str, str]:
        """Build HTTP headers for SCIM requests.
        
        Returns:
            Dict with Authorization (bearer token) and SCIM-specific Content-Type.
        """
        # Return dict initialized with auth token and SCIM content type.
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/scim+json",
        }

    def _url(self, path: str) -> str:
        """Build full URL by joining base URL with path.
        
        Args:
            path: Relative path like '/Users' or '/Users/{id}'.
        
        Returns:
            Complete URL ready for HTTP request.
        """
        # Return full URL string initialized by concatenating base and path.
        return f"{self._base_url}{path}"

    def find_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Search for a user in Verify by their userName field.
        
        We use SCIM filtering (similar to SQL WHERE clause) to search for exact
        username match. This is how we check if a user already exists before deciding
        whether to create or update.
        
        Args:
            username: The userName value to search for (usually email/UPN).
        
        Returns:
            User dict if found (with 'id' and all other SCIM fields), or None if
            no match. We only return the first match because userName should be unique.
        
        Raises:
            RuntimeError: When the search request fails (4xx/5xx status).
        """
        # params: Dict - Initialized with SCIM filter query for exact username match.
        params = {"filter": f'userName eq "{username}"'}
        # resp: httpx.Response - Initialized from GET request with filter.
        resp = self._http.get(self._url("/Users"), headers=self._headers(), params=params)
        if resp.status_code >= 400:  # Evaluated: check for error status.
            raise RuntimeError(f"Verify lookup failed: {resp.status_code} {resp.text}")
        # data: Dict - Initialized by parsing JSON response.
        data = resp.json()
        # resources: List[Dict] - Extracted from response, initialized as list of matching users.
        resources = data.get("Resources") or []
        # Return first match if exists, else None. Evaluated immediately.
        return resources[0] if resources else None

    def create_user(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new user in Verify via SCIM POST.
        
        Args:
            payload: SCIM 2.0 User dict with required fields (schemas, userName, etc.).
                This should come from our mapper, already in SCIM format.
        
        Returns:
            Created user dict from Verify, including the assigned 'id' and any
            server-generated fields.
        
        Raises:
            RuntimeError: When creation fails. Common causes: duplicate userName,
                invalid payload format, or permission issues.
        """
        # resp: httpx.Response - Initialized from POST request creating new user.
        resp = self._http.post(self._url("/Users"), headers=self._headers(), json=payload)
        if resp.status_code >= 400:  # Evaluated: check for error status.
            raise RuntimeError(f"Verify create failed: {resp.status_code} {resp.text}")
        # Return created user dict, initialized by parsing JSON response.
        return resp.json()

    def update_user(self, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Update existing user in Verify via SCIM PUT.
        
        PUT is full replacement - we send the complete user representation and Verify
        replaces the existing user. Make sure payload includes all fields you want to
        keep, not just the changes.
        
        Args:
            user_id: The SCIM 'id' field from Verify (not userName - the actual UUID).
            payload: Complete SCIM 2.0 User dict with updated values.
        
        Returns:
            Updated user dict from Verify with all current field values.
        
        Raises:
            RuntimeError: When update fails. Could be 404 (user not found) or 400
                (invalid payload).
        """
        # resp: httpx.Response - Initialized from PUT request replacing user.
        resp = self._http.put(self._url(f"/Users/{user_id}"), headers=self._headers(), json=payload)
        if resp.status_code >= 400:  # Evaluated: check for error status.
            raise RuntimeError(f"Verify update failed: {resp.status_code} {resp.text}")
        # Return updated user dict, initialized by parsing JSON response.
        return resp.json()

    def deactivate_user(self, user_id: str) -> None:
        """Mark user as inactive in Verify via SCIM PATCH.
        
        We use PATCH instead of PUT because we only want to change the 'active' field
        without touching anything else. It's more surgical than PUT's full replacement.
        This is the soft-delete approach - user still exists but can't log in.
        
        Args:
            user_id: The SCIM 'id' field from Verify.
        
        Raises:
            RuntimeError: When deactivation fails (usually 404 if user doesn't exist).
        """
        # patch: Dict - Initialized with SCIM PatchOp payload to set active=False.
        patch = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {"op": "Replace", "path": "active", "value": False},
            ],
        }
        # resp: httpx.Response - Initialized from PATCH request modifying user.
        resp = self._http.patch(self._url(f"/Users/{user_id}"), headers=self._headers(), json=patch)
        if resp.status_code >= 400:  # Evaluated: check for error status.
            raise RuntimeError(f"Verify deactivate failed: {resp.status_code} {resp.text}")
        # resp disposed after check, user_id logged for audit trail.
        logger.info("Deactivated Verify user %s", user_id)

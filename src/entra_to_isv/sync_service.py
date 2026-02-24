from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Tuple

from .config import AppConfig, get_attribute_mapping
from .graph_client import GraphClient
from .mapper import map_user
from .verify_client import VerifyClient

logger = logging.getLogger(__name__)


def _compare_simple(lhs: Dict, rhs: Dict, keys: Iterable[str]) -> bool:
    """Compare specific keys between two dicts for equality.
    
    Simple equality checker that only looks at the keys we care about. We use this
    to detect if a user's attributes changed without comparing *everything* (because
    SCIM servers add metadata fields we don't control).
    
    Args:
        lhs: Left-hand side dict (usually our mapped/new data).
        rhs: Right-hand side dict (usually existing Verify data).
        keys: Which keys to compare between the dicts.
    
    Returns:
        True if all specified keys have equal values (or are both missing), False otherwise.
    """
    # Return boolean evaluated by checking if all keys match between dicts.
    # Each k evaluated in turn: get value from both dicts and compare.
    return all(lhs.get(k) == rhs.get(k) for k in keys)


def _emails_equal(lhs: List[Dict], rhs: List[Dict]) -> bool:
    """Compare two email arrays for equality, ignoring order.
    
    Emails might be in different order but still represent the same set. We sort
    both lists by (value, type) before comparing so order doesn't matter. Because
    nobody wants to update a user just because the emails got shuffled.
    
    Args:
        lhs: Left-hand side email list (our mapped data).
        rhs: Right-hand side email list (existing Verify data).
    
    Returns:
        True if both lists contain the same emails (regardless of order), False otherwise.
    """
    # Return boolean evaluated by sorting both lists and comparing.
    # Each list sorted by tuple of (value, type) to normalize order.
    return sorted(lhs, key=lambda e: (e.get("value"), e.get("type"))) == sorted(
        rhs, key=lambda e: (e.get("value"), e.get("type"))
    )


def _needs_update(mapped: Dict, existing: Dict) -> bool:
    """Determine if existing Verify user needs updating based on mapped data.
    
    We compare the important fields between our freshly-mapped Azure data and what's
    currently in Verify. If anything meaningful changed, we return True to trigger
    an update. We ignore SCIM metadata fields that the server adds (like 'meta',
    'id', etc.) because we don't control those.
    
    This prevents unnecessary updates when nothing actually changed, which keeps
    the logs cleaner and reduces API traffic. We're efficient like that.
    
    Args:
        mapped: Our freshly-mapped SCIM user dict from Azure AD data.
        existing: Current user dict from Verify (includes all SCIM fields).
    
    Returns:
        True if user needs updating (something changed), False if they're identical.
    """
    # Compare core top-level fields first. Evaluated immediately.
    if not _compare_simple(mapped, existing, ["userName", "displayName", "active"]):
        return True  # At least one core field differs - we need to update.

    # mapped_name: Dict - Initialized from nested name dict, empty if missing.
    mapped_name = mapped.get("name", {})
    # existing_name: Dict - Initialized from existing nested name dict.
    existing_name = existing.get("name", {})
    # Evaluate name fields comparison.
    if not _compare_simple(mapped_name, existing_name, ["givenName", "familyName"]):
        return True  # Name changed - update needed.

    # Check if emails changed (only if we have emails in mapped data).
    if "emails" in mapped:  # Evaluated: do we care about emails?
        # Evaluate email equality using order-independent comparison.
        if not _emails_equal(mapped.get("emails", []), existing.get("emails", [])):
            return True  # Emails differ - update needed.

    # Check if phone numbers changed (only if we have them in mapped data).
    if "phoneNumbers" in mapped:  # Evaluated: do we care about phone numbers?
        # Evaluate phone number list equality (order matters here, unlike emails).
        if mapped.get("phoneNumbers", []) != existing.get("phoneNumbers", []):
            return True  # Phones differ - update needed.

    # All checks passed - no meaningful differences detected.
    return False


def _compute_diffs(mapped: Dict, existing: Dict) -> Dict[str, Dict[str, object]]:
    """Compute a diff between two SCIM user dicts focused on mapped fields.

    We walk through every field we care about and compare the incoming Azure-mapped
    value against what's currently sitting in Verify. Any mismatch becomes an entry
    in the returned dict with a 'current' (what Verify has) and 'new' (what Azure
    says it should be) pair. This is the paper trail behind every update we'd make.

    Returns a dict keyed by field path with {"current": X, "new": Y} entries
    for any field that would change if we updated the user.
    """
    # diffs: Dict - Initialized empty; we'll add entries for each field that changed.
    diffs: Dict[str, Dict[str, object]] = {}

    def set_diff(key: str, a, b):
        # Inner helper so we don't repeat the 'if a != b' pattern everywhere.
        # a: current value (from existing), b: new value (from mapped).
        # key evaluated; if values differ, an entry is added to diffs.
        if a != b:
            diffs[key] = {"current": a, "new": b}  # diffs updated with the change.

    # Compare the core top-level SCIM fields first - these are the most common changes.
    set_diff("userName", existing.get("userName"), mapped.get("userName"))
    set_diff("displayName", existing.get("displayName"), mapped.get("displayName"))
    set_diff("active", existing.get("active"), mapped.get("active"))

    # Pull out the nested name dicts so we can compare their sub-fields cleanly.
    # ex_name, mp_name: Dicts - Initialized from each user's name block.
    ex_name = existing.get("name", {})
    mp_name = mapped.get("name", {})
    set_diff("name.givenName", ex_name.get("givenName"), mp_name.get("givenName"))
    set_diff("name.familyName", ex_name.get("familyName"), mp_name.get("familyName"))

    # Only check emails if our mapped data includes them - we don't want to flag
    # a missing email as a diff when we simply didn't map it.
    if "emails" in mapped:
        if not _emails_equal(mapped.get("emails", []), existing.get("emails", [])):
            # Emails differ - record the full before/after arrays for visibility.
            diffs["emails"] = {"current": existing.get("emails", []), "new": mapped.get("emails", [])}

    # Same logic for phone numbers - only compare if we mapped them.
    if "phoneNumbers" in mapped:
        if mapped.get("phoneNumbers", []) != existing.get("phoneNumbers", []):
            # phoneNumbers differ - record the full before/after arrays.
            diffs["phoneNumbers"] = {
                "current": existing.get("phoneNumbers", []),
                "new": mapped.get("phoneNumbers", []),
            }

    return diffs


def _partition_changes(
    mapped_users: List[Tuple[Dict, Dict]],
    verify_client: VerifyClient,
) -> Tuple[List[Tuple[Dict, Dict]], List[Tuple[Dict, Dict]]]:
    """Split mapped users into 'create' and 'update' buckets.
    
    We check each user against Verify to see if they already exist. New users go
    in the 'create' pile, existing ones go in the 'update' pile. It's like sorting
    mail - some letters need new folders, others go in existing ones.
    
    This involves making a Verify API call for each user, so it can take a bit if
    you have thousands of users. But it's necessary to avoid duplicate creates or
    missing updates.
    
    Args:
        mapped_users: List of (source_dict, mapped_dict) tuples. Source is the raw
            Azure AD user, mapped is the SCIM-formatted version ready for Verify.
        verify_client: Client to search for existing users in Verify.
    
    Returns:
        Tuple of (to_create, to_update) where:
        - to_create: List of (source, mapped) for users not in Verify yet.
        - to_update: List of (mapped, existing) for users already in Verify.
    """
    # to_create: List - Initialized empty, will accumulate users to create.
    to_create: List[Tuple[Dict, Dict]] = []
    # to_update: List - Initialized empty, will accumulate users to update.
    to_update: List[Tuple[Dict, Dict]] = []

    # Process each user to determine if they exist in Verify.
    for source_user, mapped in mapped_users:
        # source_user: Azure AD raw data dict, evaluated on each iteration.
        # mapped: SCIM-formatted dict, evaluated on each iteration.
        
        # existing: Optional[Dict] - Initialized from Verify lookup by userName.
        existing = verify_client.find_user_by_username(mapped["userName"])
        if existing:  # Evaluated: user exists in Verify.
            # Add to update list with (new_data, old_data) tuple.
            to_update.append((mapped, existing))  # to_update updated.
        else:  # Evaluated: user doesn't exist in Verify yet.
            # Add to create list with (source, mapped) tuple.
            to_create.append((source_user, mapped))  # to_create updated.

    # Both lists now fully populated and ready to return.
    return to_create, to_update


def sync_users(config: AppConfig, dry_run: bool = False) -> None:
    """Execute a complete user sync from Azure AD to IBM Security Verify.
    
    This is the main orchestration function that ties everything together. We:
    1. Fetch all users from Azure AD via Graph API.
    2. Map each user to SCIM format using our configured mappings.
    3. Check which users exist in Verify vs need to be created.
    4. Create new users and update existing ones (unless dry_run=True).
    
    The sync is unidirectional (Azure → Verify) and handles disabled users according
    to config. We don't delete users - at worst we mark them inactive.
    
    Args:
        config: Application config with credentials, mappings, and sync options.
        dry_run: If True, we plan the changes but don't actually send them to Verify.
            Great for testing or seeing what would happen. Defaults to False (do it for real).
    
    Raises:
        RuntimeError: If Graph or Verify API calls fail. We let exceptions bubble up
            rather than swallowing them, because failures are important.
    """
    # graph: GraphClient - Initialized with Azure AD credentials from config.
    # This client will handle OAuth and pagination for us.
    graph = GraphClient(
        tenant_id=config.graph.tenant_id,
        client_id=config.graph.client_id,
        client_secret=config.graph.client_secret,
        scope=config.graph.scope,
    )
    # verify: VerifyClient - Initialized with Verify SCIM endpoint and token.
    # This handles all our SCIM operations.
    verify = VerifyClient(
        base_url=config.verify.base_url,
        api_token=config.verify.api_token,
    )

    # mapping: Dict - Initialized from config, defines how to translate Azure → SCIM.
    mapping = get_attribute_mapping(config)
    # source_users: List[Dict] - Initialized by fetching all users from Azure AD.
    # This might take a while if you have thousands of users.
    source_users = graph.list_users()

    # mapped_users: List[Tuple] - Initialized empty, will hold (source, mapped) pairs.
    mapped_users: List[Tuple[Dict, Dict]] = []
    # Transform each Azure user into SCIM format.
    for user in source_users:  # user: Dict evaluated on each iteration.
        # mapped: Dict - Initialized by transforming user according to mapping rules.
        mapped = map_user(user, mapping, include_mobile=config.sync.include_mobile_phone)

        # Special handling for disabled Azure users (if configured).
        # user.accountEnabled evaluated to check account status.
        if not user.get("accountEnabled") and config.sync.deactivate_disabled:
            # Override active field to False - mark them inactive in Verify too.
            mapped["active"] = False  # mapped updated to reflect disabled status.

        # Accumulate the (source, mapped) tuple for later processing.
        mapped_users.append((user, mapped))  # mapped_users updated with new pair.

    # to_create, to_update: Tuples of lists - Initialized by partitioning users.
    # This step involves Verify API calls to check each user's existence.
    to_create, to_update = _partition_changes(mapped_users, verify)

    # Log summary of planned changes for visibility.
    logger.info("Users fetched from Graph: %s", len(source_users))
    logger.info("To create: %s; to update: %s", len(to_create), len(to_update))

    # dry_run evaluated: if True, we stop here without making changes.
    if dry_run:
        logger.info("Dry-run enabled; no changes will be sent")
        return  # Exit early - nothing actually sent to Verify.

    # Execute creates: process each user in to_create list.
    for source_user, payload in to_create:
        # source_user: original Azure dict (for logging if needed).
        # payload: SCIM dict ready to POST, evaluated on each iteration.
        logger.info("Creating Verify user %s", payload.get("userName"))
        verify.create_user(payload)  # Side effect: user created in Verify.

    # Execute updates: process each user in to_update list.
    for payload, existing in to_update:
        # payload: our new SCIM data from Azure.
        # existing: current SCIM data from Verify (evaluated on each iteration).
        
        # Check if update is actually needed by comparing the data.
        if _needs_update(payload, existing):  # Evaluated for each user.
            logger.info("Updating Verify user %s", payload.get("userName"))
            # Side effect: user updated in Verify with new data.
            verify.update_user(existing["id"], payload)
        else:
            # No meaningful changes detected - skip the update to save API calls.
            logger.debug("No change for Verify user %s", payload.get("userName"))
    
    # All creates and updates complete. mapped_users, to_create, to_update disposed.
    # Function exits, sync cycle complete.


def plan_sync(config: AppConfig) -> Tuple[List[Tuple[Dict, Dict]], List[Tuple[Dict, Dict]], int]:
    """Plan the synchronization without applying changes.

    We go through all the same steps as a real sync - fetching users from Azure,
    mapping their attributes, and checking what exists in Verify - but we stop
    before creating or updating anything. This gives callers a full picture of what
    *would* happen so they can preview, report, or present it to the user.

    Returns lists of users to create and to update, plus total source user count.
    """
    # graph: GraphClient - Initialized with Azure AD credentials from config.
    # We need this to fetch the full user directory from Microsoft's side.
    graph = GraphClient(
        tenant_id=config.graph.tenant_id,
        client_id=config.graph.client_id,
        client_secret=config.graph.client_secret,
        scope=config.graph.scope,
    )
    # verify: VerifyClient - Initialized with Verify SCIM endpoint and token.
    # We need this to check which users already exist on IBM's side.
    verify = VerifyClient(
        base_url=config.verify.base_url,
        api_token=config.verify.api_token,
    )

    # mapping: Dict - Initialized from config, defines how to translate Azure → SCIM.
    mapping = get_attribute_mapping(config)
    # source_users: List[Dict] - Initialized by fetching all users from Azure AD.
    # This is the full universe of users we're planning to sync.
    source_users = graph.list_users()

    # mapped_users: List[Tuple] - Initialized empty, will hold (source, mapped) pairs.
    mapped_users: List[Tuple[Dict, Dict]] = []
    # Transform each Azure user into its SCIM representation using our mapping rules.
    for user in source_users:  # user: Dict evaluated on each iteration.
        # mapped: Dict - Initialized by converting user through our attribute mapping.
        mapped = map_user(user, mapping, include_mobile=config.sync.include_mobile_phone)
        # If the user is disabled in Azure and we're configured to reflect that,
        # make sure the mapped payload marks them inactive in Verify too.
        if not user.get("accountEnabled") and config.sync.deactivate_disabled:
            mapped["active"] = False  # mapped updated to reflect disabled status.
        mapped_users.append((user, mapped))  # mapped_users updated with new pair.

    # Split users into create/update buckets by checking Verify for existing records.
    # to_create, to_update: Lists - Initialized by partitioning mapped_users.
    to_create, to_update = _partition_changes(mapped_users, verify)
    # Return the two action lists plus total count for caller to use as needed.
    return to_create, to_update, len(source_users)


def compare_account(config: AppConfig, username: str) -> Dict[str, object]:
    """Compare a single account between Azure AD and Verify.

    We look up one user by UPN in Azure, translate them through our mapping rules,
    then search for them in Verify. The result tells us one of three stories:
    the user doesn't exist in Azure, they exist in Azure but not yet in Verify
    (candidate for creation), or they exist in both (candidate for update if anything
    has changed). We return all the raw data too so callers can do their own analysis.

    Looks up the Azure AD user by UPN, maps to SCIM, looks up Verify user by
    userName, and returns a structured comparison with diffs.
    """
    # graph: GraphClient - Initialized with Azure AD credentials from config.
    graph = GraphClient(
        tenant_id=config.graph.tenant_id,
        client_id=config.graph.client_id,
        client_secret=config.graph.client_secret,
        scope=config.graph.scope,
    )
    # verify: VerifyClient - Initialized with Verify SCIM endpoint and token.
    verify = VerifyClient(
        base_url=config.verify.base_url,
        api_token=config.verify.api_token,
    )

    # source: Optional[Dict] - Initialized by fetching the user directly by UPN.
    # If Azure doesn't know this user, we can stop right here.
    source = graph.get_user_by_upn(username)
    if not source:
        # source evaluated as falsy - user not found in Azure, nothing more to do.
        return {"status": "not_found_in_azure", "username": username}

    # mapping: Dict - Initialized from config to translate Azure fields → SCIM.
    mapping = get_attribute_mapping(config)
    # mapped: Dict - Initialized by converting source through the attribute mapping.
    mapped = map_user(source, mapping, include_mobile=config.sync.include_mobile_phone)
    # Respect the disabled-user policy: if the account is disabled in Azure and
    # we're configured to deactivate, reflect that in the mapped payload.
    if not source.get("accountEnabled") and config.sync.deactivate_disabled:
        mapped["active"] = False  # mapped updated to reflect disabled status.

    # existing: Optional[Dict] - Initialized by searching Verify for this user.
    # If they're not in Verify yet, we return the would-be creation payload.
    existing = verify.find_user_by_username(mapped["userName"])
    if not existing:
        # existing evaluated as falsy - user not yet in Verify.
        # Return the full would-be creation payload so the caller can see what we'd send.
        return {
            "status": "not_found_in_verify",
            "username": mapped["userName"],
            "azure_source": source,
            "scim_mapped": mapped,
        }

    # User exists in both systems - compute the field-level diff to see what's changed.
    # diffs: Dict - Initialized by comparing the mapped data against what Verify has.
    diffs = _compute_diffs(mapped, existing)
    # Return the full comparison including raw data from both sides so callers
    # have everything they need to audit, report, or decide what to do next.
    return {
        "status": "found_both",
        "username": mapped["userName"],
        "diffs": diffs,
        "azure_source": source,
        "scim_mapped": mapped,
        "verify_current": existing,
    }

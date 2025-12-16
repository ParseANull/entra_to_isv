from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List


def _set_nested(target: Dict[str, Any], path: str, value: Any) -> None:
    """Set a value in a nested dict using dot-notation path.
    
    We use this to handle SCIM's nested structure like 'name.givenName' without
    manually checking if 'name' dict exists. It's basically autocomplete for dicts.
    
    Args:
        target: The dict we're modifying (mutated in place).
        path: Dot-separated key path like 'name.familyName' or just 'userName'.
        value: The value to stuff at the end of the path.
    
    Example:
        _set_nested({}, 'name.givenName', 'Alice') → {'name': {'givenName': 'Alice'}}
    """
    # parts: List[str] - Path split into segments, initialized from dot-separated string.
    parts = path.split(".")
    # current: Dict - Initialized as reference to target, will navigate through nested dicts.
    current = target
    # Loop through all segments except the last one, creating nested dicts as needed.
    for segment in parts[:-1]:  # segment evaluated on each iteration.
        # setdefault: if segment key missing, creates empty dict; then we navigate into it.
        current = current.setdefault(segment, {})  # current updated to point deeper.
    # Final assignment: current now points to the parent dict of our target key.
    current[parts[-1]] = value  # Set the final key to our value.


def _map_emails(source: Dict[str, Any], email_rules: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Azure AD email fields to SCIM email array format.
    
    SCIM wants emails as an array of objects with 'value', 'type', and 'primary' fields.
    We take mapping rules that specify which source field to use and what metadata
    to attach. It's like translating between two people who both speak 'email' but
    with different accents.
    
    Args:
        source: Azure AD user dict containing raw email field(s).
        email_rules: List of dicts, each with 'source' field name plus optional
            'type' (e.g., 'work') and 'primary' (bool) metadata.
    
    Returns:
        List of SCIM-formatted email objects. Empty list if no emails found.
    """
    # emails: List[Dict] - Initialized empty, we'll accumulate results here.
    emails: List[Dict[str, Any]] = []
    # Process each mapping rule to extract and format email addresses.
    for rule in email_rules:  # rule: Dict evaluated on each iteration.
        # source_key: Optional[str] - Which Azure field to read from.
        source_key = rule.get("source")
        if not source_key:  # Evaluated: skip rules without source field.
            continue
        # value: Optional[str] - The actual email address from source.
        value = source.get(source_key)
        if not value:  # Evaluated: skip if email field is missing/empty.
            continue
        # entry: Dict - Initialized with required 'value', will add optional fields.
        entry = {"value": value}
        if "type" in rule:  # Evaluated: add email type if specified.
            entry["type"] = rule["type"]  # entry updated with type.
        if "primary" in rule:  # Evaluated: add primary flag if specified.
            entry["primary"] = bool(rule["primary"])  # entry updated with primary flag.
        emails.append(entry)  # emails updated with new entry.
    return emails  # emails: final list of formatted email objects.


def map_user(source: Dict[str, Any], mapping: Dict[str, Any], include_mobile: bool = False) -> Dict[str, Any]:
    """Transform Azure AD user dict into SCIM 2.0 user representation.
    
    This is where the magic happens - we take Microsoft's Graph API user format and
    convert it to IBM's SCIM format. It's like being a translator at the UN, except
    for user attributes instead of heated diplomatic debates.
    
    We follow the mapping rules provided (or defaults) to copy fields from source to
    target structure, handling special cases like emails (array format), nested names,
    and the active boolean. Missing fields are gracefully skipped because not every
    user has every attribute.
    
    Args:
        source: Azure AD user dict from Graph API with fields like userPrincipalName,
            givenName, surname, mail, accountEnabled, etc.
        mapping: Rules dict mapping SCIM target paths to Azure source field names.
            Special case: 'emails' maps to a list of email rules.
        include_mobile: If True, we also copy mobilePhone to phoneNumbers array.
            Defaults to False for privacy-conscious orgs.
    
    Returns:
        SCIM 2.0 compliant user dict ready to POST/PUT to Verify. Includes required
        'schemas' field and all mapped attributes that had values in source.
    """
    # mapped: Dict - Initialized with required SCIM schema declaration.
    mapped: Dict[str, Any] = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    }

    # rules: Dict - Deep copy of mapping to avoid mutating original (we're polite like that).
    rules = deepcopy(mapping)

    # Process each mapping rule to copy attributes from source to mapped output.
    for target_key, source_key in list(rules.items()):
        # target_key: SCIM field path being mapped to (e.g., 'userName', 'name.givenName').
        # source_key: Azure field name or list of rules (for emails).
        
        # Special case: emails need array-of-objects format.
        if target_key == "emails" and isinstance(source_key, list):
            # source_key evaluated as list - it's email mapping rules.
            emails = _map_emails(source, source_key)  # emails: List[Dict] initialized.
            if emails:  # Evaluated: only include if we found actual email addresses.
                mapped["emails"] = emails  # mapped updated with emails array.
            continue

        # Skip non-string source keys (shouldn't happen, but let's be defensive).
        if not isinstance(source_key, str):  # source_key evaluated for type.
            continue

        # value: Any - Extract value from source, initialized as None if missing.
        value = source.get(source_key)
        if value is None:  # Evaluated: skip if source field doesn't exist.
            continue

        # Special case: 'active' field needs to be explicit boolean for SCIM.
        if target_key == "active":  # target_key evaluated for special handling.
            mapped["active"] = bool(value)  # mapped updated with boolean.
            continue

        # Regular field: use nested dict setter for paths like 'name.givenName'.
        _set_nested(mapped, target_key, value)  # mapped updated via side effect.

    # Optional: add mobile phone if requested and available.
    if include_mobile:  # include_mobile evaluated for conditional processing.
        # mobile: Optional[str] - Initialized from source, might be None.
        mobile = source.get("mobilePhone")
        if mobile:  # Evaluated: only add if phone number exists.
            # Get or create phoneNumbers array, then append mobile entry.
            # mapped updated: phoneNumbers array created if needed, entry appended.
            mapped.setdefault("phoneNumbers", []).append(
                {"value": mobile, "type": "mobile", "primary": True}
            )

    # mapped: final SCIM user dict, fully populated from source via mapping rules.
    return mapped

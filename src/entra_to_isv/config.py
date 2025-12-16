from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv


@dataclass
class GraphSettings:
    """Azure AD (Graph API) connection credentials.
    
    We store the keys to Microsoft's kingdom here. These credentials let us talk
    to Azure AD via the Graph API and fetch user data. Think of it as our VIP
    backstage pass to the Azure concert.
    
    Attributes:
        tenant_id: Your Azure AD tenant UUID - basically your org's unique ID in Microsoft-land.
        client_id: The app registration ID we're using to authenticate.
        client_secret: The password/secret for the app (shhh, don't commit this!).
        scope: OAuth scope we're requesting; defaults to full Graph API access.
    """
    tenant_id: str
    client_id: str
    client_secret: str
    scope: str = "https://graph.microsoft.com/.default"


@dataclass
class VerifySettings:
    """IBM Security Verify SCIM endpoint configuration.
    
    The other half of our identity bridge - connection details for IBM's side.
    We'll use these to create and update users in the Verify system.
    
    Attributes:
        base_url: SCIM v2 endpoint URL (e.g., https://tenant.verify.ibm.com/v2.0/scim/v2).
        api_token: Bearer token for authenticating SCIM requests.
    """
    base_url: str
    api_token: str


@dataclass
class SyncOptions:
    """Behavioral flags controlling how we sync users.
    
    Because one size doesn't fit all - these knobs let you tune the sync behavior
    to match your organization's policies and preferences.
    
    Attributes:
        deactivate_disabled: If True, we mark disabled Azure users as inactive in Verify
            (instead of pretending they don't exist). Initialized to True because it's
            usually what you want.
        include_mobile_phone: If True, we'll copy over mobile phone numbers too. Defaults
            to False in case your users value their privacy.
        treat_missing_as_inactive: If True, users who vanish from Azure get marked inactive
            in Verify. Currently unused but here for future expansion.
    """
    deactivate_disabled: bool = True
    include_mobile_phone: bool = False
    treat_missing_as_inactive: bool = False


@dataclass
class AppConfig:
    """Top-level application configuration container.
    
    We bundle everything together here - connection details for both sides plus
    the rules for how to sync. It's like a recipe card for the sync operation.
    
    Attributes:
        graph: Azure AD Graph API credentials (initialized from env/config).
        verify: IBM Security Verify SCIM endpoint details (also from env/config).
        sync: Behavioral options controlling sync logic (initialized with defaults).
        attribute_mapping: Dict mapping Verify SCIM fields to Azure AD Graph fields.
            Starts empty, gets populated from config file or falls back to DEFAULT_MAPPING.
    """
    graph: GraphSettings
    verify: VerifySettings
    sync: SyncOptions = field(default_factory=SyncOptions)
    attribute_mapping: Dict[str, Any] = field(default_factory=dict)


class ConfigError(Exception):
    """Raised when configuration is incomplete or invalid."""


DEFAULT_MAPPING: Dict[str, Any] = {
    "userName": "userPrincipalName",
    "name.givenName": "givenName",
    "name.familyName": "surname",
    "displayName": "displayName",
    "emails": [
        {"source": "mail", "type": "work", "primary": True},
    ],
    "active": "accountEnabled",
}


def _load_yaml_config(path: str) -> Dict[str, Any]:
    """Load YAML configuration file safely.
    
    Args:
        path: Filesystem path to the YAML file we're loading.
    
    Returns:
        Parsed YAML content as a dict, or empty dict if file is empty/null.
        We return empty dict instead of None to make downstream code simpler.
    """
    with open(path, "r", encoding="utf-8") as handle:
        # handle: File object opened for reading, will auto-close on exit.
        # We use safe_load because we don't trust random YAML to execute Python code.
        return yaml.safe_load(handle) or {}


def _require_env(name: str) -> str:
    """Fetch an environment variable or explode dramatically if it's missing.
    
    We use this helper to enforce required config - better to fail fast at startup
    than mysteriously blow up mid-sync.
    
    Args:
        name: Environment variable name to look up.
    
    Returns:
        The env var value (guaranteed to be non-empty).
    
    Raises:
        ConfigError: When the variable is missing or empty. We go boom.
    """
    value = os.getenv(name)  # value: str | None - might be missing!
    if not value:  # Evaluated: checking for None or empty string.
        raise ConfigError(f"Missing required environment variable: {name}")
    return value  # value: str - now guaranteed to exist and be non-empty.


def load_config(config_path: str, env_file: Optional[str] = None) -> AppConfig:
    """Load application config from YAML plus environment variables.
    
    We merge settings from multiple sources: YAML file for structure/defaults and
    environment variables for secrets. This lets you keep credentials out of your
    config files (because nobody wants their secrets in git, right?).
    
    Args:
        config_path: Path to YAML config file with mappings and options.
        env_file: Optional path to .env file to load before reading env vars.
            If None, we just use whatever's already in the environment.
    
    Returns:
        Fully initialized AppConfig with all settings ready to roll.
    
    Raises:
        ConfigError: If required environment variables are missing.
    """
    # env_file: Optional[str] - If provided, we load it first to populate env vars.
    if env_file:
        load_dotenv(env_file)  # Side effect: updates os.environ with file contents.

    # raw: Dict[str, Any] - Initialized from YAML or empty dict if file doesn't exist.
    raw = _load_yaml_config(config_path) if os.path.exists(config_path) else {}

    # Extracting subsections from raw config (or empty dicts if missing).
    # These get evaluated immediately to extract nested config blocks.
    graph_cfg = raw.get("graph", {})
    verify_cfg = raw.get("verify", {})
    sync_cfg = raw.get("sync", {})

    graph = GraphSettings(
        tenant_id=graph_cfg.get("tenant_id") or _require_env("GRAPH_TENANT_ID"),
        client_id=graph_cfg.get("client_id") or _require_env("GRAPH_CLIENT_ID"),
        client_secret=graph_cfg.get("client_secret")
        or _require_env("GRAPH_CLIENT_SECRET"),
        scope=graph_cfg.get("scope") or "https://graph.microsoft.com/.default",
    )

    verify = VerifySettings(
        base_url=(verify_cfg.get("base_url") or _require_env("VERIFY_BASE_URL")).rstrip("/"),
        api_token=verify_cfg.get("api_token") or _require_env("VERIFY_API_TOKEN"),
    )

    mapping = raw.get("attribute_mapping") or DEFAULT_MAPPING

    sync = SyncOptions(
        deactivate_disabled=bool(sync_cfg.get("deactivate_disabled", True)),
        include_mobile_phone=bool(sync_cfg.get("include_mobile_phone", False)),
        treat_missing_as_inactive=bool(sync_cfg.get("treat_missing_as_inactive", False)),
    )

    # AppConfig initialized with all subsections now fully populated.
    # Each sub-config has been validated (via _require_env) and is ready for use.
    return AppConfig(graph=graph, verify=verify, sync=sync, attribute_mapping=mapping)


def get_attribute_mapping(config: AppConfig) -> Dict[str, Any]:
    """Extract attribute mapping from config with fallback to defaults.
    
    Convenience function that returns the user's custom mapping if they provided one,
    or falls back to our sensible defaults. Because we're helpful like that.
    
    Args:
        config: Application configuration to extract mapping from.
    
    Returns:
        Attribute mapping dict (custom or DEFAULT_MAPPING). This dict maps SCIM
        field paths to Azure AD source field names.
    """
    # Evaluated: checking if user provided custom mapping or if we use defaults.
    return config.attribute_mapping or DEFAULT_MAPPING

import json
import logging
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# Per-request/task context for current domain (avoids global shared state)
_CURRENT_DOMAIN_CTX: ContextVar[Optional[str]] = ContextVar("_current_domain_ctx", default=None)


def set_current_domain(domain: Optional[str]) -> None:
    """Record the active module/domain for background validations (per-request)."""
    value = (domain or "").strip() or None
    _CURRENT_DOMAIN_CTX.set(value)
    logger.info(f"🔧 OnboardingConfig: current domain set to '{value}'")


def get_current_domain() -> Optional[str]:
    return _CURRENT_DOMAIN_CTX.get()


def _candidate_paths() -> List[str]:
    """
    Generate candidate paths to locate onboardingConfiguration.json robustly across
    different execution contexts.
    """
    paths: List[str] = []
    # This file: src/services/integration_layer/mcp_integration/onboarding_config.py
    this_dir = os.path.dirname(os.path.abspath(__file__))

    # Directory traversal (corrected names):
    integration_layer_dir = os.path.dirname(this_dir)              # .../integration_layer
    services_dir = os.path.dirname(integration_layer_dir)          # .../services
    src_dir = os.path.dirname(services_dir)                        # .../src
    # project_root would be os.path.dirname(src_dir)               # repo root (unused here)

    # Canonical expected location
    paths.append(os.path.join(src_dir, "onboardingConfiguration.json"))

    # Optional legacy fallback: src/services/onboardingConfiguration.json
    # Keep low priority to avoid masking misplacements
    paths.append(os.path.join(services_dir, "onboardingConfiguration.json"))

    # Environment override
    env_path = os.getenv("ONBOARDING_CONFIGURATION_JSON")
    if env_path:
        paths.insert(0, env_path)

    # De-duplicate while preserving order
    seen = set()
    uniq: List[str] = []
    for p in paths:
        if p not in seen:
            uniq.append(p)
            seen.add(p)

    # Debug visibility for troubleshooting
    try:
        logger.debug("🔍 OnboardingConfig: candidate search order: %s", " -> ".join(uniq))
    except Exception:
        pass
    return uniq


@lru_cache(maxsize=1)
def _load_onboarding_config() -> List[Dict[str, Any]]:
    """
    Load and cache onboardingConfiguration.json content.
    Returns a list of module dicts; empty list if not found or invalid.
    """
    for p in _candidate_paths():
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    logger.info(f"✅ OnboardingConfig: loaded from {p} ({len(data)} modules)")
                    return data
                else:
                    logger.warning(f"⚠️ OnboardingConfig: file at {p} is not a list, ignoring")
        except Exception as e:
            logger.warning(f"⚠️ OnboardingConfig: failed to read {p}: {e}")
    logger.error("❌ OnboardingConfig: onboardingConfiguration.json not found in candidate paths")
    return []


def get_module_config(domain: str) -> Optional[Dict[str, Any]]:
    """Return the module config dict for the given domain (case-insensitive)."""
    if not domain:
        return None
    modules = _load_onboarding_config()
    for m in modules:
        if isinstance(m, dict) and str(m.get("domain", "")).lower() == domain.lower():
            return m
    return None


def get_allowed_hosts_for_current_domain() -> Set[str]:
    """
    Returns the set of allowed base hostnames (lowercased) for the current domain.
    Empty set means no hosts are allowed (validation should block navigation).
    """
    domain = get_current_domain()
    if not domain:
        return set()

    cfg = get_module_config(domain)
    if not cfg:
        return set()

    hosts = cfg.get("applicationBaseURL")
    if not isinstance(hosts, list):
        return set()

    normalized: Set[str] = set()
    for h in hosts:
        try:
            if isinstance(h, str) and h.strip():
                normalized.add(h.strip().lower())
        except Exception:
            continue
    return normalized



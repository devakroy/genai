# locator_reuse.py
import re
import ast
import time
import asyncio
import logging
from typing import Optional, Tuple, List, Union, Dict, Tuple as _Tuple
from playwright.async_api import Page, Locator, FrameLocator
from utils.db_utils import get_playwright_locator_by_uid_prefix, update_locator_validity, get_fallback_locator_by_unique_id
from utils.dynamic_select import ensure_selected
from utils.constants import PLATFORM_CONFIG
from dataclasses import dataclass
from typing import NamedTuple

# Setup logger for locator reuse operations
logger = logging.getLogger(__name__)

# ---- TTL Cache for DB lookups with memory management ----
@dataclass
class CacheEntry:
    value: Optional[str]
    timestamp: float
    access_count: int = 0

class CacheConfig(NamedTuple):
    ttl_seconds: int
    max_size: int
    cleanup_threshold: int

# Load cache configuration from constants.py
_CACHE_CONFIG = CacheConfig(
    ttl_seconds=PLATFORM_CONFIG.get("CACHE_TTL_SECONDS", 1800),
    max_size=PLATFORM_CONFIG.get("CACHE_MAX_SIZE", 1000),
    cleanup_threshold=PLATFORM_CONFIG.get("CACHE_CLEANUP_THRESHOLD", 1200)
)
_LOCATOR_CACHE: Dict[str, CacheEntry] = {}
_LAST_CLEANUP: float = 0

def _is_cache_entry_expired(entry: CacheEntry) -> bool:
    """Check if a cache entry has expired based on TTL."""
    return (time.time() - entry.timestamp) > _CACHE_CONFIG.ttl_seconds

def _cleanup_expired_cache_entries() -> None:
    """Remove expired entries and enforce size limits."""
    global _LAST_CLEANUP
    current_time = time.time()
    
    # Only cleanup if it's been a while since last cleanup
    if current_time - _LAST_CLEANUP < 300:  # 5 minutes
        return
    
    initial_size = len(_LOCATOR_CACHE)
    
    # Remove expired entries
    expired_keys = [
        key for key, entry in _LOCATOR_CACHE.items() 
        if _is_cache_entry_expired(entry)
    ]
    
    for key in expired_keys:
        del _LOCATOR_CACHE[key]
    
    # If still over max size, remove least recently accessed entries
    if len(_LOCATOR_CACHE) > _CACHE_CONFIG.max_size:
        # Sort by access_count (ascending) then by timestamp (ascending)
        sorted_items = sorted(
            _LOCATOR_CACHE.items(), 
            key=lambda x: (x[1].access_count, x[1].timestamp)
        )
        
        # Keep only the most recently/frequently accessed entries
        entries_to_keep = sorted_items[-_CACHE_CONFIG.max_size:]
        _LOCATOR_CACHE.clear()
        _LOCATOR_CACHE.update(entries_to_keep)
    
    _LAST_CLEANUP = current_time
    cleaned_count = initial_size - len(_LOCATOR_CACHE)
    
    if cleaned_count > 0:
        logger.debug(f"🧹 CACHE-CLEANUP: Removed {cleaned_count} entries | {len(_LOCATOR_CACHE)} remaining")

def _should_trigger_cleanup() -> bool:
    """Determine if cache cleanup should be triggered."""
    return len(_LOCATOR_CACHE) > _CACHE_CONFIG.cleanup_threshold

def get_cache_stats() -> dict:
    """Get cache statistics for monitoring."""
    current_time = time.time()
    expired_count = sum(1 for entry in _LOCATOR_CACHE.values() if _is_cache_entry_expired(entry))
    
    return {
        'total_entries': len(_LOCATOR_CACHE),
        'expired_entries': expired_count,
        'active_entries': len(_LOCATOR_CACHE) - expired_count,
        'cache_size_mb': sum(len(str(k) + str(v.value or '')) for k, v in _LOCATOR_CACHE.items()) / 1024 / 1024,
        'last_cleanup': _LAST_CLEANUP,
        'ttl_seconds': _CACHE_CONFIG.ttl_seconds,
        'max_size': _CACHE_CONFIG.max_size
    }

def clear_locator_cache() -> int:
    """Clear all entries from the locator cache and return the number of entries cleared."""
    if not _LOCATOR_CACHE:
        logger.info("🧹 CACHE-CLEAR: ALREADY EMPTY | No entries to clear")
        return 0
    
    cleared_count = len(_LOCATOR_CACHE)
    _LOCATOR_CACHE.clear()
    logger.info(f"🧹 CACHE-CLEAR: SUCCESS ✅ | Cleared {cleared_count} cache entries")
    return cleared_count

def invalidate_cache_entry(unique_id: str) -> bool:
    """Invalidate a specific cache entry by removing it from the cache.
    
    Args:
        unique_id: The unique identifier for the cache entry to invalidate
        
    Returns:
        True if an entry was removed, False if no entry existed
    """
    if unique_id in _LOCATOR_CACHE:
        del _LOCATOR_CACHE[unique_id]
        logger.info(f"🧹 CACHE-INVALIDATE: SUCCESS ✅ | Removed stale entry for {unique_id}")
        return True
    else:
        logger.debug(f"🧹 CACHE-INVALIDATE: NO-OP | No entry found for {unique_id}")
        return False

_CAMEL_SPLIT = re.compile(r'(?<!^)(?=[A-Z])')

def _camel_to_snake(name: str) -> str:
    if name in ("nth", "first", "frameLocator"):
        # Keep frameLocator as frame_locator for Python method name
        if name == "frameLocator":
            return "frame_locator"
        return name
    return _CAMEL_SPLIT.sub('_', name).lower()

def make_uid_base(original_bdd_step: str, page_name: str) -> str:
    """Mirror UID generation used for DB rows with dynamic content normalization."""
    step_text = (original_bdd_step or "").strip()
    
    # Normalize dynamic content patterns before generating UID
    normalized_step = _normalize_dynamic_content_for_uid(step_text)
    
    cleaned = re.sub(r'\s+', '_', normalized_step)
    cleaned = cleaned.replace('"', '').replace("'", '')
    return f"chewy_web_storefront_{cleaned}_{page_name}"

def _normalize_dynamic_content_for_uid(step_text: str) -> str:
    """
    Normalize dynamic content in step text for consistent UID generation.
    This replaces actual resolved values with placeholder patterns so that
    steps with dynamic content generate the same UID across test runs.
    """
    if not step_text:
        return step_text
    
    normalized = step_text
    
    # Replace email addresses with generic pattern
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    normalized = re.sub(email_pattern, 'DYNAMIC_EMAIL', normalized)
    
    # Replace common dynamic patterns like {variableName} or {{variableName}}
    normalized = re.sub(r'\{\{?[^}]+\}\}?', 'DYNAMIC_PLACEHOLDER', normalized)
    
    # Replace phone numbers
    phone_pattern = r'\b\(?[\d]{3}\)?[-.\s]?[\d]{3}[-.\s]?[\d]{4}\b'
    normalized = re.sub(phone_pattern, 'DYNAMIC_PHONE', normalized)
    
    # Replace timestamps and dates
    timestamp_pattern = r'\b\d{4}[-/]\d{2}[-/]\d{2}(?:[T\s]\d{2}:\d{2}:\d{2}(?:\.\d{3})?(?:Z|[+-]\d{2}:\d{2})?)?\b'
    normalized = re.sub(timestamp_pattern, 'DYNAMIC_TIMESTAMP', normalized)
    
    # Replace UUIDs and long numeric IDs
    uuid_pattern = r'\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b'
    normalized = re.sub(uuid_pattern, 'DYNAMIC_UUID', normalized, flags=re.IGNORECASE)
    
    # Replace numeric IDs (6+ digits)
    numeric_id_pattern = r'\b\d{6,}\b'
    normalized = re.sub(numeric_id_pattern, 'DYNAMIC_NUMERIC_ID', normalized)
    
    # Replace credit card numbers (basic pattern)
    cc_pattern = r'\b(?:\d{4}[-\s]?){3}\d{4}\b'
    normalized = re.sub(cc_pattern, 'DYNAMIC_CC_NUMBER', normalized)
    
    # Replace resolved placeholder patterns (clicks on numeric IDs)
    resolved_click_pattern = r'(clicks? on ")(\d+)("?)'
    normalized = re.sub(resolved_click_pattern, r'\1DYNAMIC_ID\3', normalized)
    
    # Replace enters field with numeric values
    resolved_enter_pattern = r'(enters? "[^"]*" as ")(\d+)("?)'
    normalized = re.sub(resolved_enter_pattern, r'\1DYNAMIC_ID\3', normalized)
    
    # Replace any remaining 4+ digit numbers (potential IDs)
    remaining_numeric_pattern = r'\b\d{4,}\b'
    normalized = re.sub(remaining_numeric_pattern, 'DYNAMIC_ID', normalized)
    
    logger.debug(f"🔧 UID-NORMALIZE: Original: {step_text[:60]}...")
    logger.debug(f"🔧 UID-NORMALIZE: Normalized: {normalized[:60]}...")
    
    return normalized

# ---------- translator (generic, no hard-coded map) ----------
def _safe_eval_simple(value: str):
    v = value.strip()
    v = re.sub(r"\btrue\b", "True", v, flags=re.I)
    v = re.sub(r"\bfalse\b", "False", v, flags=re.I)
    v = re.sub(r"\bnull\b", "None", v, flags=re.I)
    try:
        return ast.literal_eval(v)
    except Exception:
        return v

def _split_top_level_commas(s: str) -> List[str]:
    parts, cur, depth, in_s, in_d = [], [], 0, False, False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == '{' and not in_s and not in_d:
            depth += 1
        elif ch == '}' and not in_s and not in_d:
            depth = max(0, depth - 1)
        elif ch == ',' and not in_s and not in_d and depth == 0:
            parts.append(''.join(cur).strip()); cur = []; i += 1; continue
        cur.append(ch); i += 1
    if cur:
        parts.append(''.join(cur).strip())
    return [p for p in parts if p]

def _parse_js_args_and_opts(argstr: str) -> Tuple[List, dict]:
    s = argstr.strip()
    
    # Special handling for xpath expressions that might contain complex patterns
    if s.startswith("'xpath=") or s.startswith('"xpath='):
        logger.debug(f"🔧 PARSE-ARGS: XPATH DETECTED | Length: {len(s)}")
        # Simple xpath case - extract the xpath string directly
        try:
            # Find the closing quote
            quote_char = s[0]
            end_idx = s.rfind(quote_char)
            if end_idx > 0:
                xpath_arg = s[:end_idx + 1]
                xpath_value = _safe_eval_simple(xpath_arg)
                logger.debug(f"🔧 PARSE-ARGS: XPATH EXTRACTED | Value: {str(xpath_value)[:100]}...")
                return [xpath_value], {}
        except Exception as e:
            logger.warning(f"🔧 PARSE-ARGS: XPATH PARSING FAILED | Error: {str(e)[:100]}")
    
    brace = 0
    last_start = -1
    opts = ""
    for i, ch in enumerate(s):
        if ch == "{" and brace == 0:
            last_start = i; brace = 1; j = i + 1
            while j < len(s) and brace:
                if s[j] == "{": brace += 1
                elif s[j] == "}": brace -= 1
                j += 1
            if brace == 0 and j == len(s):
                opts = s[last_start:]; s = s[:last_start].rstrip().rstrip(",")
            break

    args: List = []
    kwargs: dict = {}

    if s:
        for a in _split_top_level_commas(s):
            args.append(_safe_eval_simple(a))

    if opts:
        body = opts.strip()[1:-1].strip()
        for kv in _split_top_level_commas(body):
            if ":" not in kv:
                continue
            k, v = kv.split(":", 1)
            k = re.sub(r"[^0-9a-zA-Z_]", "_", k.strip())
            kwargs[k] = _safe_eval_simple(v.strip())
    
    logger.debug(f"🔧 PARSE-ARGS: RESULT | Args: {len(args)} | Kwargs: {len(kwargs)}")
    return args, kwargs

def _parse_chain(chain: str) -> List[Tuple[str, str]]:
    segments = [p for p in re.split(r"\.(?=[A-Za-z_])", chain.strip()) if p]
    calls: List[Tuple[str, str]] = []
    
    logger.debug(f"🔧 PARSE-CHAIN: STARTING | Segments: {len(segments)}")
    
    for i, seg in enumerate(segments):
        seg = seg.strip()
        logger.debug(f"🔧 PARSE-CHAIN: SEGMENT {i+1}/{len(segments)} | Content: {seg[:100]}...")
        
        if seg in ("first", "first()"):
            calls.append(("first", "")); continue
            
        m_nth = re.fullmatch(r"nth\(\s*(-?\d+)\s*\)", seg)
        if m_nth:
            calls.append(("nth", m_nth.group(1))); continue
            
        # Handle current_frame reference (no-op, refers to current frame context)
        if seg == "current_frame":
            logger.debug(f"🔧 PARSE-CHAIN: CURRENT-FRAME | Skipping as implicit context")
            continue  # Skip this segment as it's implicit in frameLocator context
        
        # Enhanced frameLocator parsing for complex xpath expressions
        if seg.startswith("frameLocator("):
            try:
                # Extract everything between the parentheses for frameLocator
                paren_start = seg.find("(")
                paren_end = seg.rfind(")")
                if paren_start != -1 and paren_end != -1 and paren_end > paren_start:
                    args_content = seg[paren_start + 1:paren_end]
                    logger.debug(f"🔧 PARSE-CHAIN: FRAME-LOCATOR | Args: {args_content[:100]}...")
                    calls.append(("frameLocator", args_content))
                    continue
            except Exception as e:
                logger.warning(f"🔧 PARSE-CHAIN: FRAME-LOCATOR FAILED | Error: {str(e)[:50]}")
        
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*\((.*)\)\s*", seg)
        if m:
            method_name = m.group(1)
            args_content = m.group(2)
            logger.debug(f"🔧 PARSE-CHAIN: METHOD | Name: {method_name} | Args: {args_content[:50]}...")
            calls.append((method_name, args_content))
        else:
            logger.debug(f"🔧 PARSE-CHAIN: SIMPLE | Name: {seg}")
            calls.append((seg, ""))
    
    logger.debug(f"🔧 PARSE-CHAIN: RESULT | Total calls: {len(calls)}")
    return calls

def _invoke(loc: Union[Page, Locator, FrameLocator], method_js: str, argstr: str) -> Optional[Union[Locator, Page, FrameLocator]]:
    method_py = _camel_to_snake(method_js)

    # Handle current_frame as no-op (returns the current frame context)
    if method_py == "current_frame":
        logger.debug(f"🔧 INVOKE: current_frame - Returning current context (no-op)")
        return loc  # Return the current frame locator as-is

    if method_py == "first":
        return getattr(loc, "first", None)

    if method_py == "nth":
        try:
            n = int((argstr or "0").strip())
        except Exception:
            return None
        return loc.nth(n) if isinstance(loc, Locator) else None

    # Handle frameLocator for iframe support
    if method_py == "frame_locator":
        if not isinstance(loc, (Page, FrameLocator)):
            logger.debug(f"🔧 INVOKE: frameLocator - Invalid object type: {type(loc).__name__}")
            return None
        args, kwargs = _parse_js_args_and_opts(argstr)
        logger.debug(f"🔧 INVOKE: frameLocator - Args: {args[:2] if args else []} | Kwargs: {list(kwargs.keys())}")
        try:
            result = loc.frame_locator(*args, **kwargs)
            logger.debug(f"🔧 INVOKE: frameLocator - Success ✅ | Result type: {type(result).__name__}")
            return result
        except Exception as e:
            logger.warning(f"🔧 INVOKE: frameLocator - Failed ❌ | Error: {str(e)[:100]}")
            return None

    if not hasattr(loc, method_py):
        return None

    call = getattr(loc, method_py)
    args, kwargs = _parse_js_args_and_opts(argstr)
    try:
        return call(*args, **kwargs)
    except TypeError:
        try:
            return call(*args)
        except Exception:
            return None
    except Exception:
        return None

def translate_locator_to_python(page: Page, locator_str: str) -> Optional[Locator]:
    if not locator_str:
        return None
    s = locator_str.strip()
    
    logger.debug(f"🔍 TRANSLATE: STARTING | Locator: {s[:150]}...")

    # Fix malformed iframe selector syntax: page.iframe[...] -> page.locator('iframe[...]')
    iframe_selector_pattern = r'page\.iframe\[([^\]]+)\]'
    iframe_match = re.search(iframe_selector_pattern, s)
    if iframe_match:
        iframe_selector = iframe_match.group(1)
        # Convert to proper locator syntax
        normalized_locator = f"page.locator('iframe[{iframe_selector}]')"
        s = re.sub(iframe_selector_pattern, normalized_locator, s)
        logger.debug(f"🔍 TRANSLATE: NORMALIZED IFRAME SELECTOR | Before: page.iframe[{iframe_selector}] | After: {normalized_locator}")

    # Parse iframe chain if present
    iframe_chain = _extract_iframe_chain(s)
    if iframe_chain:
        logger.debug(f"🔍 TRANSLATE: IFRAME CHAIN FOUND | Count: {len(iframe_chain)}")
        # Remove iframe comment from locator string
        s = re.sub(r'/\*\s*iframe_chain:[^*]+\*/', '', s).strip()
        # If locator starts with 'current_frame.' remove it as we'll build frame context
        s = re.sub(r'^current_frame\.', '', s).strip()
        logger.debug(f"🔍 TRANSLATE: AFTER IFRAME CLEANUP | Locator: {s[:100]}...")

    # raw selector
    if s.startswith(("css=", "xpath=", "id=", "text=", "data-testid=")):
        try:
            logger.debug(f"🔍 TRANSLATE: RAW SELECTOR DETECTED | Type: {s.split('=')[0]}")
            if iframe_chain:
                logger.debug(f"🔍 TRANSLATE: BUILDING FRAME CHAIN | Count: {len(iframe_chain)}")
                obj = page
                for i, iframe_xpath in enumerate(iframe_chain):
                    frame_selector = f"xpath={iframe_xpath}"
                    logger.debug(f"🔍 TRANSLATE: FRAME {i+1}/{len(iframe_chain)} | Selector: {frame_selector[:100]}...")
                    obj = obj.frame_locator(frame_selector)
                logger.debug(f"🔍 TRANSLATE: APPLYING FINAL LOCATOR | Selector: {s[:100]}...")
                return obj.locator(s)
            else:
                return page.locator(s)
        except Exception as e:
            logger.warning(f"🔍 TRANSLATE: RAW SELECTOR FAILED | Error: {str(e)[:100]}")
            return None

    # Handle special case of frameLocator chain that starts with page.frameLocator
    if s.startswith('page.frameLocator('):
        logger.debug(f"🔍 TRANSLATE: FRAME LOCATOR CHAIN DETECTED")
        try:
            # Try to parse and execute the frameLocator chain directly
            # Remove 'page.' prefix and parse the chain
            chain_str = s[5:]  # Remove 'page.'
            calls = _parse_chain(chain_str)
            logger.debug(f"🔍 TRANSLATE: PARSED FRAME CHAIN | Calls: {len(calls)}")
            
            obj: Union[Page, Locator, FrameLocator] = page
            for i, (method_js, argstr) in enumerate(calls):
                logger.debug(f"🔍 TRANSLATE: EXECUTING CALL {i+1}/{len(calls)} | Method: {method_js} | Args: {argstr[:50]}...")
                obj = _invoke(obj, method_js, argstr)
                if obj is None:
                    logger.warning(f"🔍 TRANSLATE: CALL FAILED | Method: {method_js} | Step: {i+1}")
                    return None
            return obj if isinstance(obj, Locator) else None
        except Exception as e:
            logger.warning(f"🔍 TRANSLATE: FRAME CHAIN FAILED | Error: {str(e)[:100]}")
            return None
    
    # strip page. and trailing actions
    s = re.sub(r"^\s*page\.", "", s)
    s = re.sub(
        r"\)\s*\.\s*(click|fill|press|type|check|uncheck|selectOption|hover|focus|dblclick|tap)\s*\(.*\)\s*$",
        ")",
        s, flags=re.I,
    )

    calls = _parse_chain(s)
    if not calls:
        logger.warning(f"🔍 TRANSLATE: NO CALLS PARSED | String: {s[:100]}...")
        return None

    obj: Union[Page, Locator, FrameLocator] = page
    
    # Handle iframe chain first
    if iframe_chain:
        logger.debug(f"🔍 TRANSLATE: PROCESSING IFRAME CHAIN | Count: {len(iframe_chain)}")
        for i, iframe_xpath in enumerate(iframe_chain):
            frame_selector = f"xpath={iframe_xpath}"
            logger.debug(f"🔍 TRANSLATE: IFRAME {i+1}/{len(iframe_chain)} | Selector: {frame_selector[:100]}...")
            obj = obj.frame_locator(frame_selector)
    
    logger.debug(f"🔍 TRANSLATE: EXECUTING METHOD CHAIN | Count: {len(calls)}")
    for i, (method_js, argstr) in enumerate(calls):
        logger.debug(f"🔍 TRANSLATE: METHOD {i+1}/{len(calls)} | Name: {method_js} | Args: {argstr[:50]}...")
        obj = _invoke(obj, method_js, argstr)
        if obj is None:
            logger.warning(f"🔍 TRANSLATE: METHOD FAILED | Name: {method_js} | Step: {i+1}/{len(calls)}")
            return None

    final_result = obj if isinstance(obj, Locator) else None
    if final_result:
        logger.debug(f"🔍 TRANSLATE: SUCCESS ✅ | Final type: {type(obj).__name__}")
    else:
        logger.warning(f"🔍 TRANSLATE: FAILED ❌ | Final type: {type(obj).__name__}")
    
    return final_result

# ---------- TTL-based DB reuse (string fetch + translate) ----------
def get_cached_db_locator_with_fallback(uid_base: str, current_iframe_chain: Optional[List[str]] = None, 
                          step_action: str = '', step_object: str = '') -> Optional[str]:
    """
    Enhanced locator retrieval with fallback mechanism:
    1. First check fallback_locators table for the unique_id
    2. If found, try to use the playwrightLocator
    3. If fallback succeeds, insert to t_locators; if fails, proceed with existing flow
    4. If no fallback locator found, use existing flow (cache + t_locators)
    """
    try:
        current_time = time.time()
        
        # Trigger cleanup if cache is getting large
        if _should_trigger_cleanup():
            _cleanup_expired_cache_entries()
        
        # STEP 1: Check fallback_locators table first
        logger.info(f"🔄 FALLBACK-FLOW: CHECKING FALLBACK TABLE | {uid_base} | Checking fallback_locators table first")
        fallback_locator = get_fallback_locator_by_unique_id(uid_base)
        
        if fallback_locator:
            logger.info(f"🔄 FALLBACK-FLOW: FOUND FALLBACK LOCATOR ✅ | {uid_base} | Locator: {fallback_locator[:100]}...")
            
            # Skip invalid fallback locators
            if "/* INVALID_LOCATOR */" in fallback_locator:
                logger.info(f"🔄 FALLBACK-FLOW: SKIP INVALID FALLBACK | {uid_base} | Fallback locator marked as invalid")
                return _proceed_with_existing_flow(uid_base, current_iframe_chain, step_action, step_object, current_time)
            
            # Cache the fallback locator temporarily for validation
            _LOCATOR_CACHE[f"{uid_base}_fallback"] = CacheEntry(value=fallback_locator, timestamp=current_time, access_count=1)
            logger.info(f"🔄 FALLBACK-FLOW: RETURNING FALLBACK LOCATOR | {uid_base} | Will be validated by caller")
            
            # Mark this as a fallback locator for the caller to know
            return f"/* FALLBACK_SOURCE */ {fallback_locator}"
        else:
            logger.info(f"🔄 FALLBACK-FLOW: NO FALLBACK FOUND | {uid_base} | Proceeding with existing flow")
        
        # STEP 2: No fallback locator found - proceed with existing flow
        return _proceed_with_existing_flow(uid_base, current_iframe_chain, step_action, step_object, current_time)
        
    except Exception as e:
        logger.warning(f"🔄 FALLBACK-FLOW: ERROR | {uid_base} | {str(e)[:100]} | Proceeding with existing flow")
        # Cache the exception/miss to avoid repeated failed queries
        _LOCATOR_CACHE[uid_base] = CacheEntry(value=None, timestamp=time.time(), access_count=1)
        logger.debug(f"💾 CACHE-STORE: ERROR | {uid_base} | Cached error for {_CACHE_CONFIG.ttl_seconds}s")
        return None

def _proceed_with_existing_flow(uid_base: str, current_iframe_chain: Optional[List[str]], 
                               step_action: str, step_object: str, current_time: float) -> Optional[str]:
    """
    Existing locator retrieval flow when no fallback is found.
    """
    # EXACT UID MATCHING ONLY in cache
    if uid_base in _LOCATOR_CACHE:
        entry = _LOCATOR_CACHE[uid_base]
        
        # Check if entry is expired
        if _is_cache_entry_expired(entry):
            logger.debug(f"🕐 CACHE-TTL: EXPIRED | {uid_base} | Entry expired after {_CACHE_CONFIG.ttl_seconds}s")
            del _LOCATOR_CACHE[uid_base]
        else:
            # Entry is valid - return it (no context filtering)
            entry.access_count += 1
            cache_age = int(current_time - entry.timestamp)
            logger.info(f"🎯 LOCATOR-SOURCE: CACHE-HIT ✅ | {uid_base} | Age: {cache_age}s | Access: {entry.access_count} | Locator: {entry.value[:100] if entry.value else 'None'}...")
            return entry.value
    
    # Cache miss - query database with EXACT UID matching only
    logger.info(f"🔍 LOCATOR-SOURCE: DATABASE-QUERY 🗄️ | {uid_base} | Cache miss - querying database")
    s = get_playwright_locator_by_uid_prefix(uid_base)  # Exact UID match only
    
    # Skip locators marked as invalid (DON'T cache - allow re-validation)
    if s and "/* INVALID_LOCATOR */" in s:
        logger.info(f"🔍 DB-REUSE: SKIP INVALID | {uid_base} | Locator marked as invalid (not caching - allow retry)")
        return None
    
    # Cache the result (exact UID match or None) with TTL
    _LOCATOR_CACHE[uid_base] = CacheEntry(value=s, timestamp=current_time, access_count=1)
    
    if s:
        logger.info(f"🎯 LOCATOR-SOURCE: DATABASE-SUCCESS ✅ | {uid_base} | Retrieved and cached | Locator: {s[:100]}...")
        logger.debug(f"💾 CACHE-STORE: SUCCESS | {uid_base} | Cached for {_CACHE_CONFIG.ttl_seconds}s")
    else:
        logger.warning(f"🎯 LOCATOR-SOURCE: DATABASE-NOT-FOUND ⚠️ | {uid_base} | No locator found in database")
        logger.debug(f"💾 CACHE-STORE: MISS | {uid_base} | Cached miss for {_CACHE_CONFIG.ttl_seconds}s")
        
    return s

def get_cached_db_locator(uid_base: str, current_iframe_chain: Optional[List[str]] = None, 
                          step_action: str = '', step_object: str = '') -> Optional[str]:
    """
    Get locator from cache with EXACT UID matching only.
    No context-aware filtering - pure exact UID lookup.
    
    **DEPRECATED**: Use get_cached_db_locator_with_fallback instead for enhanced fallback support.
    """
    return get_cached_db_locator_with_fallback(uid_base, current_iframe_chain, step_action, step_object)

# ---------- helpers to improve CTA reliability ----------
def _extract_visible_text_hint(locator_str: str) -> Optional[str]:
    """Pull text hint from stored chain (getByText(...) or { name: '...' })."""
    s = locator_str or ""
    m = re.search(r"""\{\s*name\s*:\s*(['"])(.+?)\1\s*\}""", s, flags=re.I)
    if m:
        return m.group(2).strip()
    m = re.search(r"""getByText\s*\(\s*(['"])(.+?)\1""", s, flags=re.I)
    if m:
        return m.group(2).strip()
    return None

def _extract_container_testid(locator_str: str) -> Optional[str]:
    """Return the last getByTestId('...') in the chain to keep its container visible."""
    if not locator_str:
        return None
    last = None
    for m in re.finditer(r"getByTestId\s*\(\s*(['\"])(.+?)\1\s*\)", locator_str):
        last = m.group(2)
    return last

def _extract_iframe_chain(locator_str: str) -> Optional[List[str]]:
    """Extract iframe chain from locator comment /* iframe_chain:xpath1|xpath2 */"""
    if not locator_str:
        return None
    
    match = re.search(r'/\*\s*iframe_chain:([^*]+)\*/', locator_str)
    if not match:
        return None
    
    iframe_chain_str = match.group(1).strip()
    if not iframe_chain_str:
        return None
    
    # Split by | to get individual iframe xpaths
    iframe_xpaths = [xpath.strip() for xpath in iframe_chain_str.split('|') if xpath.strip()]
    return iframe_xpaths if iframe_xpaths else None

def _is_iframe_context(locator_str: str) -> bool:
    """Check if a locator is intended for iframe context based on iframe_chain comment."""
    if not locator_str:
        return False
    return "/* iframe_chain:" in locator_str

def _has_iframe_context_mismatch(stored_locator: str, current_iframe_chain: Optional[List[str]]) -> bool:
    """
    Check if stored locator context mismatches current execution context.
    
    Enhanced logic to reduce false positives and allow more locator reuse.
    """
    stored_has_iframe = _is_iframe_context(stored_locator)
    current_has_iframe = current_iframe_chain is not None and len(current_iframe_chain) > 0
    
    # ✅ LIBERAL APPROACH: If current context is unclear (no iframe_chain), 
    # be more permissive and try to infer context compatibility
    if not current_has_iframe:
        # If stored locator has iframe context but current doesn't, 
        # try to validate if it could still work in main page context
        if stored_has_iframe:
            logger.debug(f"🔍 CONTEXT LIBERAL | Stored has iframe context, current unclear - allowing with validation")
            return False  # Don't block - let validation decide
        # Both main page context - definitely compatible
        return False
    
    # ✅ If current execution IS in iframe context
    if current_has_iframe:
        # If stored locator is also iframe-based, compare chains if possible
        if stored_has_iframe:
            stored_chain = _extract_iframe_chain(stored_locator)
            logger.debug(f"🔍 CONTEXT IFRAME | Stored chain: {stored_chain}, Current: {current_iframe_chain}")
            # For now, allow any iframe-to-iframe usage (let validation handle specifics)
            return False
        else:
            # Stored is main page, current is iframe - this is a mismatch
            logger.debug(f"🔍 CONTEXT MISMATCH | Stored: main-page, Current: iframe")
            return True
    
    # Default: no mismatch
    return False

# --- wait helpers for "fast enough"
    """Extract iframe chain from locator comment /* iframe_chain:xpath1|xpath2 */"""
    if not locator_str:
        return None
    
    match = re.search(r'/\*\s*iframe_chain:([^\*/]+)\*/', locator_str)
    if not match:
        return None
    
    iframe_chain_str = match.group(1).strip()
    if not iframe_chain_str:
        return None
    
    # Split by | to get individual iframe xpaths
    iframe_xpaths = [xpath.strip() for xpath in iframe_chain_str.split('|') if xpath.strip()]
    return iframe_xpaths if iframe_xpaths else None

async def _last_visible(loc: Locator) -> Optional[Locator]:
    """Return the last visible match from a locator list."""
    try:
        count = await loc.count()
        last = None
        for i in range(count):
            item = loc.nth(i)
            try:
                if await item.is_visible():
                    last = item
            except Exception:
                pass
        return last
    except Exception:
        return None

async def _await_container_visible_for(page: Page, locator_str: str, timeout_ms: int = 6000) -> Optional[Locator]:
    tid = _extract_container_testid(locator_str)
    if not tid:
        return None
    try:
        cont = page.get_by_test_id(tid)
        await cont.first.wait_for(state="attached", timeout=timeout_ms)
        vis = await _last_visible(cont)
        if vis:
            try:
                await vis.wait_for(state="visible", timeout=timeout_ms)
            except Exception:
                pass
            return vis
    except Exception:
        pass
    return None

async def _wait_stable(loc: Locator) -> None:
    await loc.wait_for(state="attached", timeout=3500)
    try:
        await loc.wait_for(state="visible", timeout=3000)
    except Exception:
        pass
    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass
    # enabled & box hints
    try:
        if hasattr(loc, "is_enabled") and not await loc.is_enabled():
            await asyncio.sleep(0.15)
    except Exception:
        pass
    try:
        _ = await loc.bounding_box()
    except Exception:
        pass

# MUI elements: wait for transitions/ripple effects
    try:
        class_attr = await loc.get_attribute('class')
        if class_attr and ('Mui' in class_attr or 'jss' in class_attr):
            await asyncio.sleep(0.3)
    except Exception:
        pass

async def _click_label_fallback(page: Page, loc: Locator) -> bool:
    """For radios/checkboxes that flip back, try clicking wrapping/for= label."""
    try:
        wrap = loc.locator("xpath=ancestor::label[1]")
        if await wrap.count() > 0:
            try:
                await wrap.first.click(timeout=3000); return True
            except Exception:
                try:
                    await wrap.first.click(force=True, timeout=3000); return True
                except Exception:
                    pass
        input_id = await loc.evaluate("el => el.getAttribute && el.getAttribute('id')")
        if input_id:
            lab = page.locator(f'label[for="{input_id}"]')
            if await lab.count() > 0:
                try:
                    await lab.first.click(timeout=3000); return True
                except Exception:
                    await lab.first.click(force=True, timeout=3000); return True
    except Exception:
        pass
    return False

# -------- Post-click stabilization (generic, sequential, awaited) --------
async def _await_post_click_progress(page: Page, anchor: Optional[Locator] = None, hint: Optional[str] = None) -> None:
    """
    Generic stabilizer after a click:
      - any URL change
      - clicked anchor disappears or hides
      - load-state settling (domcontentloaded / networkidle)
    Never raises.
    """
    # 1) Any URL change (SPA or full nav)
    try:
        old_url = page.url
    except Exception:
        old_url = None
    if old_url:
        try:
            await page.wait_for_url(lambda url: url != old_url, timeout=8000)
            return
        except Exception:
            pass

    # 2) Anchor becomes detached/hidden (e.g., drawer closes, re-render)
    if anchor is not None:
        try:
            await anchor.wait_for(state="detached", timeout=3000)
            return
        except Exception:
            try:
                await anchor.wait_for(state="hidden", timeout=3000)
                return
            except Exception:
                pass

    # 3) Load-state settling (does not require 'load/complete')
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass

# ---------- Robust click (generic & layered fallbacks) ----------
async def robust_click_from_db_locator(
    page: Page,
    locator_str: str,
    *,
    retries: int = 3,
    delay_ms: int = 250,
) -> None:
    """
    Robust CTA click that prefers clicking INSIDE the most-recent visible container
    (last getByTestId in the stored chain), then falls back to global resolution.
    Also waits for post-click progress (nav/disappear/url change).
    Enhanced with better error handling and retry logic for checkout elements.
    """
    last_err: Optional[Exception] = None
    hint = _extract_visible_text_hint(locator_str)
    
    # Detect checkout/payment elements for enhanced handling
    checkout_keywords = ['checkout', 'proceed', 'payment', 'paypal', 'autoship', 'cart', 'order']
    is_checkout_element = any(keyword in locator_str.lower() for keyword in checkout_keywords)
    
     # Detect MUI/Material-UI components
    mui_keywords = ['mui', 'jss', 'material']
    is_mui_element = any(keyword in locator_str.lower() for keyword in mui_keywords)

    # Increase retries for checkout elements
    if is_checkout_element:
        retries = max(retries, 5)
        logger.debug(f"🔧 ROBUST-CLICK: CHECKOUT ELEMENT | Extended retries: {retries}")
        # Wait for MUI overlays to clear
    if is_mui_element:
        try:
            await page.wait_for_selector('.MuiBackdrop-root, [role="progressbar"], .MuiCircularProgress-root', state='hidden', timeout=2000)
        except Exception:
            pass
    for retry_attempt in range(max(1, retries)):
        try:
            logger.debug(f"🔧 ROBUST-CLICK: ATTEMPT {retry_attempt + 1}/{retries} | Locator: {locator_str[:50]}...")
            
            # 1) If chain includes a container testid, try inside it first
            cont = await _await_container_visible_for(page, locator_str)
            if cont and hint:
                try:
                    btn = cont.get_by_role("button", name=hint)
                    inside = await _last_visible(btn) or btn.first
                    await _wait_stable(inside)
                    
                    # Enhanced timeout for checkout and MUI elements
                    click_timeout = 6000 if (is_checkout_element or is_mui_element) else 4000
                    await inside.click(timeout=click_timeout)
                    await _await_post_click_progress(page, inside, hint)
                    logger.debug(f"🔧 ROBUST-CLICK: CONTAINER SUCCESS ✅ | Attempt: {retry_attempt + 1}")
                    return
                except Exception as container_error:
                    logger.debug(f"🔧 ROBUST-CLICK: CONTAINER FAILED | Attempt: {retry_attempt + 1} | {str(container_error)[:50]}")
                    pass

                # Try text-based fallback within container
                try:
                    tloc = cont.get_by_text(hint, exact=True)
                    inside = await _last_visible(tloc) or tloc.first
                    await _wait_stable(inside)
                    await inside.click(timeout=4000)
                    await _await_post_click_progress(page, inside, hint)
                    logger.debug(f"🔧 ROBUST-CLICK: CONTAINER TEXT SUCCESS ✅ | Attempt: {retry_attempt + 1}")
                    return
                except Exception as e:
                        last_err = e  # continue to global path

            # 2) Global: re-translate full stored chain
            logger.debug(f"🔧 ROBUST-CLICK: TRANSLATING LOCATOR | Attempt: {retry_attempt + 1} | Locator: {locator_str[:100]}...")
            loc = translate_locator_to_python(page, locator_str)
            if not loc:
                error_msg = f"Could not translate stored locator on attempt {retry_attempt + 1}"
                logger.warning(f"🔧 ROBUST-CLICK: TRANSLATE FAILED | {error_msg}")
                if retry_attempt == retries - 1:  # Last attempt
                    raise RuntimeError(error_msg)
                await asyncio.sleep(delay_ms / 1000)
                continue

            logger.debug(f"🔧 ROBUST-CLICK: LOCATOR TRANSLATED ✅ | Attempt: {retry_attempt + 1}")
            await _wait_stable(loc)
            logger.debug(f"🔧 ROBUST-CLICK: LOCATOR STABLE ✅ | Attempt: {retry_attempt + 1}")
            
            # Enhanced timeout for checkout and MUI elements
            click_timeout = 6000 if (is_checkout_element or is_mui_element) else 4000
            logger.debug(f"🔧 ROBUST-CLICK: USING TIMEOUT | {click_timeout}ms | Special element: {is_checkout_element}")

            try:
                logger.debug(f"🔧 ROBUST-CLICK: ATTEMPTING CLICK | Timeout: {click_timeout}ms")
                await loc.click(timeout=click_timeout)
                logger.debug(f"🔧 ROBUST-CLICK: CLICK EXECUTED ✅ | Attempt: {retry_attempt + 1}")
                await _await_post_click_progress(page, loc, hint)
                logger.debug(f"🔧 ROBUST-CLICK: GLOBAL SUCCESS ✅ | Attempt: {retry_attempt + 1}")
                return
            except Exception as click_error:
                logger.warning(f"🔧 ROBUST-CLICK: CLICK FAILED | Attempt: {retry_attempt + 1} | Error: {str(click_error)[:100]}")
                try:
                    logger.debug(f"🔧 ROBUST-CLICK: TRYING TRIAL CLICK | Attempt: {retry_attempt + 1}")
                    await loc.click(trial=True, timeout=1200)
                    await loc.click(timeout=click_timeout)
                    logger.debug(f"🔧 ROBUST-CLICK: TRIAL+CLICK EXECUTED ✅ | Attempt: {retry_attempt + 1}")
                    await _await_post_click_progress(page, loc, hint)
                    logger.debug(f"🔧 ROBUST-CLICK: TRIAL+CLICK SUCCESS ✅ | Attempt: {retry_attempt + 1}")
                    return
                except Exception as trial_error:
                    logger.warning(f"🔧 ROBUST-CLICK: TRIAL CLICK FAILED | Attempt: {retry_attempt + 1} | Error: {str(trial_error)[:100]}")
                    try:
                        logger.debug(f"🔧 ROBUST-CLICK: TRYING FORCE CLICK | Attempt: {retry_attempt + 1}")
                        await loc.click(force=True, timeout=click_timeout)
                        logger.debug(f"🔧 ROBUST-CLICK: FORCE CLICK EXECUTED ✅ | Attempt: {retry_attempt + 1}")
                        await _await_post_click_progress(page, loc, hint)
                        logger.debug(f"🔧 ROBUST-CLICK: FORCE SUCCESS ✅ | Attempt: {retry_attempt + 1}")
                        return
                    except Exception as force_error:
                        logger.warning(f"🔧 ROBUST-CLICK: FORCE CLICK FAILED | Attempt: {retry_attempt + 1} | Error: {str(force_error)[:100]}")
                        try:
                            logger.debug(f"🔧 ROBUST-CLICK: TRYING DISPATCH EVENT | Attempt: {retry_attempt + 1}")
                            await loc.dispatch_event("click")
                            logger.debug(f"🔧 ROBUST-CLICK: DISPATCH EXECUTED ✅ | Attempt: {retry_attempt + 1}")
                            await _await_post_click_progress(page, loc, hint)
                            logger.debug(f"🔧 ROBUST-CLICK: DISPATCH SUCCESS ✅ | Attempt: {retry_attempt + 1}")
                            return
                        except Exception as dispatch_error:
                            logger.warning(f"🔧 ROBUST-CLICK: DISPATCH FAILED | Attempt: {retry_attempt + 1} | Error: {str(dispatch_error)[:50]}")
                            logger.debug(f"🔧 ROBUST-CLICK: ALL METHODS FAILED | Attempt: {retry_attempt + 1}")
                            last_err = dispatch_error

        except Exception as e:
            logger.debug(f"🔧 ROBUST-CLICK: ATTEMPT FAILED | Attempt: {retry_attempt + 1} | {str(e)[:50]}")
            last_err = e

        # Wait before retry (except on last attempt)
        if retry_attempt < retries - 1:
            await asyncio.sleep(delay_ms / 1000)

    # 3) Final text/name fallbacks (global) - only if we have hint text
    if hint:
        logger.debug(f"🔧 ROBUST-CLICK: TRYING FALLBACKS | Hint: {hint}")
        try:
            btn = page.get_by_role("button", name=hint)
            inside = await _last_visible(btn) or btn.first
            await _wait_stable(inside)
            click_timeout = 6000 if is_checkout_element else 4000
            await inside.click(timeout=click_timeout)
            await _await_post_click_progress(page, inside, hint)
            logger.debug(f"🔧 ROBUST-CLICK: BUTTON FALLBACK SUCCESS ✅")
            return
        except Exception as fallback_error:
            logger.debug(f"🔧 ROBUST-CLICK: BUTTON FALLBACK FAILED | {str(fallback_error)[:50]}")
            pass
        try:
            tloc = page.get_by_text(hint, exact=True)
            inside = await _last_visible(tloc) or tloc.first
            await _wait_stable(inside)
            click_timeout = 6000 if is_checkout_element else 4000
            await inside.click(timeout=click_timeout)
            await _await_post_click_progress(page, inside, hint)
            logger.debug(f"🔧 ROBUST-CLICK: TEXT FALLBACK SUCCESS ✅")
            return
        except Exception as text_error:
            logger.debug(f"🔧 ROBUST-CLICK: TEXT FALLBACK FAILED | {str(text_error)[:50]}")
            pass

    # Final error with detailed information
    error_msg = f"robust_click_from_db_locator failed after {retries} attempts"
    if last_err:
        error_msg += f" | Last error: {str(last_err)[:100]}"
    if is_checkout_element:
        error_msg += f" | Checkout element detected"
    logger.error(f"🔧 ROBUST-CLICK: FINAL FAILURE ❌ | {error_msg}")
    raise last_err or RuntimeError(error_msg)

def _should_skip_locator_reuse(step_text: str) -> bool:
    """
    Check if step should skip locator reuse - only for navigation/API actions OR steps containing 'dynamic' keyword.
    
    IMPORTANT: Uses context-aware matching to avoid false positives where navigation keywords 
    appear in button text (e.g., 'clicks on "Schedule a visit" button' should NOT be skipped).
    """
    if not step_text:
        return False
    
    step_lower = step_text.lower()
    
    # 🔧 PRIORITY 1: Don't skip if this is clearly a click/interaction action
    # This takes priority even over 'dynamic' keyword because click actions should always use locator reuse
    if any(action_word in step_lower for action_word in ['clicks', 'click on', 'press', 'tap', 'select', 'enters', 'enter', 'type', 'fill', 'input']):
        # This is clearly an interaction step - don't skip regardless of other keywords
        return False
    
    # 🔧 PRIORITY 2: Skip if step contains 'dynamic' keyword (but only for non-interaction steps)
    if 'dynamic' in step_lower:
        return True
    
    # 🔧 PRIORITY 3: Skip locator reuse for NAVIGATION actions
    # Use context-aware matching to avoid false positives with button text
    import re
    
    # More specific navigation patterns that require context
    navigation_patterns = [
        r'\bnavigates?\s+to\b',  # "navigates to" or "navigate to"
        r'\bgoes?\s+to\b',       # "goes to" or "go to" 
        r'\buser\s+navigates?\s+to\b',  # "user navigates to" or "user navigate to"
        r'\buser\s+goes?\s+to\b',       # "user goes to" or "user go to"
        r'\buser\s+visits?\b',          # "user visits" or "user visit" (with word boundary)
        r'\buser\s+opens?\b'            # "user opens" or "user open" (with word boundary)
    ]
    
    # Only match when these patterns appear as actual verbs, not within button text or quotes
    for pattern in navigation_patterns:
        if re.search(pattern, step_lower):
            # Additional check: skip if the matched word is within quotes (button text)
            matches = re.finditer(pattern, step_lower)
            for match in matches:
                match_start = match.start()
                # Check if the match is within quotes by looking for quotes before and after
                text_before = step_lower[:match_start]
                
                # Count quotes before to see if we're inside quoted text
                quotes_before = text_before.count('"') + text_before.count("'")
                
                # If odd number of quotes before, we're inside quoted text - don't skip
                if quotes_before % 2 == 0:  # Even quotes = we're outside quoted text
                    return True
    
    # 🔧 PRIORITY 3: Skip locator reuse for API actions
    api_patterns = [
        'api call', 'api request', 
        'stores response', 'store response',
        'makes get', 'makes post', 'make get', 'make post',
        'calls api', 'call api',
        'sends request', 'send request',
        'posts to', 'post to', 'gets from', 'get from'
    ]
    
    for pattern in api_patterns:
        if pattern in step_lower:
            return True
    
    return False


def _contains_dynamic_content(step_text: str) -> bool:
    """
    Check if step text contains dynamic content that shouldn't be cached.
    
    Simplified: Only check for 'dynamic' keyword.
    Everything else (form fields, dates, numbers, etc.) should be cached.
    """
    if not step_text:
        return False
    
    step_lower = step_text.lower()
    
    # Only skip if step explicitly contains 'dynamic' keyword
    if "dynamic" in step_lower:
        return True
    
    return False

# ---------- High-level fast-path helper ----------
async def reuse_locator_if_possible(
    page: Page,
    test_step: dict,
    current_df,
    new_window_detected,
    extract_page_name,
    record_page_object_details,
    *,
    max_seconds: float = 6.0,   # small budget so we never block the discovery path
) -> _Tuple[bool, object]:
    """
    Try to reuse a stored locator. Returns (handled, current_df).
    If handled=True, the caller should skip discovery.
    Fail-open on *any* error/timeout and let the caller do JS crawl.
    """

    # ✅ Guard against closed/stale page reference
    try:
        if page.is_closed() or not page.url or page.url.strip() == "" or page.url == "about:blank":
            logger.warning("🔍 DB-REUSE: SKIP | Page is closed, blank, or invalid")
            return False, current_df
    except Exception:
        # if page object is in a bad state, also fail open
        logger.warning("🔍 DB-REUSE: SKIP | Invalid page reference")
        return False, current_df

    start = time.monotonic()

    try:
        step_text_raw = str(test_step.get('step', '')).strip()
        action = step_text_raw.split('-', 1)[0].strip().lower()

        if action not in {"click", "enter", "populate", "type", "fill", "select", "verify", "press", "tap"}:
            return False, current_df
        if not getattr(page, "url", None):
            return False, current_df

        original_bdd_step = str(test_step.get('original_bdd_step', '')).strip()
        
        # ✅ Skip locator reuse for navigation and API actions (but NOT for dynamic content)
        if _should_skip_locator_reuse(original_bdd_step):
            logger.info(f"🔍 DB-REUSE: SKIP NAVIGATION/API 🚫 | {original_bdd_step[:60]}... | Skipping locator reuse for navigation/API action")
            return False, current_df
        
        page_name = extract_page_name(page.url) if page.url else "landing_page"
        uid_base = make_uid_base(original_bdd_step, page_name)

        # never let unique_id be NULL downstream
        test_step['unique_id'] = uid_base

        # ✅ Detect current iframe context from test_step or page state
        current_iframe_chain = test_step.get('iframe_chain', None)
        
        # Also try to detect iframe context from current page state or action type
        if not current_iframe_chain and test_step.get('action') == 'switch_to_iframe':
            # If this is an iframe switch action, we're likely in iframe context
            current_iframe_chain = ['detected']
            logger.debug(f"🔍 DB-REUSE: IFRAME CONTEXT DETECTED | {uid_base} | Action: switch_to_iframe")
        
        if current_iframe_chain:
            logger.debug(f"🔍 DB-REUSE: IFRAME CONTEXT | {uid_base} | Current chain: {current_iframe_chain}")

        ## DB / cache – enhanced cache-then-validate approach with fallback support
        db_loc_str = get_cached_db_locator(uid_base, current_iframe_chain, 
                                          test_step.get('action', ''), test_step.get('object', ''))
        
        # If no locator found, skip to discovery
        if not db_loc_str:
            logger.debug(f"🔍 DB-REUSE: MISS | {uid_base} | No cached locator found (context-aware)")
            return False, current_df

        # Check if this is a fallback locator
        is_fallback_locator = db_loc_str.startswith("/* FALLBACK_SOURCE */")
        actual_locator_str = db_loc_str.replace("/* FALLBACK_SOURCE */ ", "") if is_fallback_locator else db_loc_str
        
        if is_fallback_locator:
            logger.info(f"🔄 FALLBACK-FLOW: PROCESSING FALLBACK LOCATOR | {uid_base} | Starting validation of fallback locator")
        
        logger.info(f"🔍 DB-REUSE: FOUND LOCATOR | {uid_base} | Starting validation process | Locator: {actual_locator_str[:100]}...")
        logger.info(f"🔍 DB-REUSE: HIT ✅ | {uid_base} | Action: {action} | Object: {test_step.get('object', 'unknown')} | Found: {actual_locator_str[:100]}...")
        
        # Check for iframe support
        iframe_chain = _extract_iframe_chain(actual_locator_str)
        if iframe_chain:
            logger.info(f"🔍 DB-REUSE: IFRAME 📱 | {uid_base} | Chain: {iframe_chain}")
        else:
            logger.debug(f"🔍 DB-REUSE: NO-IFRAME | {uid_base}")

        # ✅ ENHANCED CACHE-THEN-VALIDATE APPROACH WITH FALLBACK SUPPORT
        # Step 1: Always validate locator (whether from cache, regular DB, or fallback)
        validation_timeout = 1500 if iframe_chain else 700
        
        # ✅ Special handling for PayPal and payment widgets
        if any(keyword in actual_locator_str.lower() for keyword in ['paypal', 'payment', 'wallet', 'autoship']):
            validation_timeout = max(validation_timeout, 2000)  # Min 2 seconds for PayPal/dynamic widgets
            logger.debug(f"🔍 PAYMENT WIDGET | Extended timeout: {validation_timeout}ms for payment widget")

        loc = await _validate_locator_candidate(page, actual_locator_str, action, timeout_ms=validation_timeout)
        
        # Step 2: Handle fallback locator validation result
        if is_fallback_locator:
            if loc:
                logger.info(f"🔄 FALLBACK-FLOW: VALIDATION SUCCESS ✅ | {uid_base} | Fallback locator works - will insert to t_locators")
                # Try to perform the action to confirm it works
                action_success = await _perform_action_with_locator(page, loc, action, test_step, actual_locator_str)
                
                if action_success:
                    logger.info(f"🔄 FALLBACK-FLOW: ACTION SUCCESS ✅ | {uid_base} | Inserting fallback locator to t_locators")
                    # Insert the successful fallback locator to t_locators
                    try:
                        from utils.db_utils import update_locator_validity
                        success = update_locator_validity(uid_base, actual_locator_str, valid=True, table="t_locators")
                        if success:
                            logger.info(f"🔄 FALLBACK-FLOW: INSERTED TO T_LOCATORS ✅ | {uid_base} | Fallback locator now available for future use")
                        else:
                            logger.warning(f"🔄 FALLBACK-FLOW: INSERT FAILED ❌ | {uid_base} | Could not insert to t_locators")
                    except Exception as e:
                        logger.warning(f"🔄 FALLBACK-FLOW: INSERT ERROR | {uid_base} | {str(e)[:50]}")
                    
                    # Mark action as already performed to avoid duplicate execution
                    setattr(loc, '_action_already_performed', True)
                else:
                    logger.warning(f"🔄 FALLBACK-FLOW: ACTION FAILED ❌ | {uid_base} | Fallback locator validation succeeded but action failed")
                    # Continue with existing fallback logic below
                    loc = None
            else:
                logger.warning(f"🔄 FALLBACK-FLOW: VALIDATION FAILED ❌ | {uid_base} | Fallback locator validation failed - proceeding with existing flow")
        
        # Step 3: If validation fails (for both regular and fallback locators), try fresh fetch from DB
        if not loc and not is_fallback_locator:
            logger.warning(f"🔍 DB-REUSE: VALIDATION FAILED ❌ | {uid_base} | Trying fresh DB fetch...")
            
            # Remove from cache to force fresh DB query
            if uid_base in _LOCATOR_CACHE:
                del _LOCATOR_CACHE[uid_base]
                logger.debug(f"🔄 CACHE-REMOVE: VALIDATION-FAILED | {uid_base} | Removed for fresh DB fetch")
            
            # Try fresh fetch from DB
            try:
                fresh_db_loc_str = get_cached_db_locator(uid_base, current_iframe_chain, 
                                                        test_step.get('action', ''), test_step.get('object', ''))
                
                if fresh_db_loc_str and fresh_db_loc_str != db_loc_str:
                    # Check if fresh result is also a fallback
                    fresh_is_fallback = fresh_db_loc_str.startswith("/* FALLBACK_SOURCE */")
                    fresh_actual_locator = fresh_db_loc_str.replace("/* FALLBACK_SOURCE */ ", "") if fresh_is_fallback else fresh_db_loc_str
                    
                    logger.info(f"🔍 DB-REUSE: FRESH FETCH ✨ | {uid_base} | Got different locator, re-validating...")
                    
                    # Re-validate fresh locator
                    loc = await _validate_locator_candidate(page, fresh_actual_locator, action, timeout_ms=validation_timeout)
                    if loc:
                        logger.info(f"🔍 DB-REUSE: FRESH VALIDATION SUCCESS ✅ | {uid_base}")
                        actual_locator_str = fresh_actual_locator
                        is_fallback_locator = fresh_is_fallback
                    else:
                        logger.warning(f"🔍 DB-REUSE: FRESH VALIDATION ALSO FAILED ❌ | {uid_base}")
                
            except Exception as e:
                logger.debug(f"🔍 DB-REUSE: FRESH FETCH ERROR | {uid_base} | {str(e)[:50]}")
        
        # Step 4: ✅ ENHANCED: Try performing action even if validation failed
        # Sometimes validation fails due to timing but the locator still works
        if not loc:
            logger.warning(f"🔍 DB-REUSE: FINAL VALIDATION FAILED ❌ | {uid_base} | Attempting action anyway...")
            
            # Try to create locator directly and test action
            try:
                test_loc = translate_locator_to_python(page, actual_locator_str)
                if test_loc:
                    logger.info(f"🔍 DB-REUSE: ATTEMPTING DIRECT ACTION | {uid_base} | Testing if locator actually works...")
                    
                    # Attempt the action to see if locator actually works
                    action_success = await _perform_action_with_locator(page, test_loc, action, test_step, actual_locator_str)
                    
                    if action_success:
                        logger.info(f"🔍 DB-REUSE: ACTION SUCCESS DESPITE VALIDATION FAILURE! ✅ | {uid_base} | Locator works - validation was wrong")
                        
                        # If this was a fallback locator that worked, insert it to t_locators
                        if is_fallback_locator:
                            try:
                                from utils.db_utils import update_locator_validity
                                success = update_locator_validity(uid_base, actual_locator_str, valid=True, table="t_locators")
                                if success:
                                    logger.info(f"🔄 FALLBACK-FLOW: INSERTED TO T_LOCATORS (DELAYED) ✅ | {uid_base} | Fallback locator worked and inserted")
                            except Exception as e:
                                logger.warning(f"🔄 FALLBACK-FLOW: DELAYED INSERT ERROR | {uid_base} | {str(e)[:50]}")
                        
                        loc = test_loc  # Use the working locator
                        # Mark that action was already performed
                        setattr(loc, '_action_already_performed', True)
                    else:
                        logger.warning(f"🔍 DB-REUSE: ACTION ALSO FAILED ❌ | {uid_base} | Locator timeout - falling back to discovery")
                        # Clear from cache to allow fresh lookup next time
                        if uid_base in _LOCATOR_CACHE:
                            del _LOCATOR_CACHE[uid_base]
                            logger.info(f"🧹 CACHE-CLEAR: FAILED LOCATOR | {uid_base} | Cleared to allow fresh discovery")
                        return False, current_df
                else:
                    logger.warning(f"🔍 DB-REUSE: TRANSLATE FAILED ❌ | {uid_base} | Cannot create locator - falling back to discovery")
                    return False, current_df
                    
            except Exception as e:
                logger.warning(f"🔍 DB-REUSE: DIRECT ACTION TEST ERROR | {uid_base} | {str(e)[:100]} - falling back to discovery")
                # Clear from cache if there was an unexpected error
                if uid_base in _LOCATOR_CACHE:
                    del _LOCATOR_CACHE[uid_base]
                    logger.info(f"🧹 CACHE-CLEAR: ERROR RECOVERY | {uid_base} | Cleared after error for fresh discovery")
                return False, current_df

        # budget guard (still keep)
        if time.monotonic() - start > max_seconds:
            return False, current_df

        # ✅ If we reach here, we have a working locator (either validated or action-tested)
        if loc:
            logger.info(f"🔍 DB-REUSE: VALIDATION SUCCESS ✅ | {uid_base} | Action: {action} | Preparing smart waits")
            
            # ✅ Add smart pre-action stabilization
            try:
                # Wait for any pending network activity before action
                await page.wait_for_load_state('networkidle', timeout=1000)
            except Exception:
                pass  # Continue if network idle times out
            
            # Small buffer for UI stabilization
            await asyncio.sleep(0.1)
            
            # ✅ Enhanced post-action smart waits - only if we haven't already performed action
            if not hasattr(loc, '_action_already_performed'):
                action_success = await _perform_action_with_locator(page, loc, action, test_step, actual_locator_str)
                if not action_success:
                    logger.warning(f"🔍 DB-REUSE: ACTION FAILED ❌ | {uid_base} | Action: {action} | Skipping invalid marking - let discovery update with correct locator")
                    # Don't mark as invalid here - let discovery find the correct locator and update the DB
                    return False, current_df  # fallback to discovery
            else:
                # Action was already performed during validation testing
                logger.debug(f"🔍 DB-REUSE: ACTION ALREADY PERFORMED | {uid_base} | Skipping duplicate action")
        
        # ✅ Post-action smart stabilization
        try:
            logger.debug(f"🔍 DB-REUSE: POST-ACTION WAIT | {action} | Waiting for UI stabilization...")
            
            # Wait for potential navigation or UI changes
            if action in {"click", "press", "tap", "select"}:
                try:
                    # Check if URL changed (navigation)
                    original_url = page.url
                    await page.wait_for_load_state('networkidle', timeout=2000)
                    
                    # If URL changed, wait for DOM to stabilize
                    if page.url != original_url:
                        logger.debug(f"🔍 DB-REUSE: POST-ACTION WAIT | Navigation detected | {original_url} -> {page.url}")
                        await page.wait_for_load_state('domcontentloaded', timeout=3000)
                    else:
                        # No navigation, but wait for dynamic content
                        await page.wait_for_load_state('networkidle', timeout=1500)
                        
                except Exception:
                    # Fallback wait for UI updates
                    await asyncio.sleep(0.5)
            
            # Additional wait for form submissions or AJAX requests
            elif action in {"enter", "populate", "type", "fill"}:
                try:
                    await page.wait_for_load_state('networkidle', timeout=1000)
                except Exception:
                    await asyncio.sleep(0.3)
            
            logger.debug(f"🔍 DB-REUSE: POST-ACTION WAIT | {action} | Stabilization complete")
            
        except Exception as e:
            logger.warning(f"🔍 DB-REUSE: POST-ACTION WARN | {action} | {str(e)[:50]}")

        # record row (pass Page in both args to avoid Locator serialization issues)
        page_name = extract_page_name(page.url) if page.url else ''
        try:
            # 👇 provenance marker
            test_step["generated_by"] = "db_reuse"
            current_df = await record_page_object_details(
                page_name, action.upper(),
                test_step.get('condition', ''),
                test_step.get('test_data', ''), '', '',
                page, page, None, '',
                test_step.get('condition', ''), '', '',
                current_df, test_step, new_window_detected
            )
            if 'playwrightLocator' in current_df.columns:
                current_df.loc[current_df.index[-1], 'playwrightLocator'] = actual_locator_str
            if 'unique_id' in current_df.columns:
                current_df.loc[current_df.index[-1], 'unique_id'] = uid_base
        except Exception:
            # recording shouldn’t block success
            pass

        logger.info(f"🔍 DB-REUSE: SUCCESS 🎯 | {uid_base} | Action: {action} | Locator reused + smart waits complete")
        return True, current_df

    except Exception as e:
        logger.error(f"🔍 DB-REUSE: ERROR ❌ | {test_step.get('unique_id', 'unknown')} | Error: {str(e)[:100]}")
        return False, current_df


async def _perform_action_with_locator(page: Page, loc: Locator, action: str, test_step: dict, db_loc_str: str) -> bool:
    """Perform the specified action with the validated locator. Returns True on success."""
    start_time = time.time()
    try:
        # Log action details for debugging
        uid_base = test_step.get('unique_id', 'unknown')
        logger.info(f"🔧 DB-ACTION: STARTING | {uid_base} | Action: {action} | Locator: {db_loc_str[:100]}...")
        
        await _wait_stable(loc)

        # element hints
        el_tag, el_type, is_checked = None, None, None
        try:
            el_tag = await loc.evaluate("el => el.tagName?.toLowerCase?.()")
            if el_tag == "input":
                el_type = await loc.evaluate("el => el.type?.toLowerCase?.()")
                if el_type in {"radio", "checkbox"}:
                    is_checked = await loc.evaluate("el => !!el.checked")
                    
            logger.debug(f"🔧 DB-ACTION: ELEMENT INFO | {uid_base} | Tag: {el_tag}, Type: {el_type}, Checked: {is_checked}")
        except Exception as e:
            logger.warning(f"🔧 DB-ACTION: ELEMENT INFO FAILED | {uid_base} | {str(e)[:50]}")
            pass

        td = str(test_step.get('test_data', '') or '')
        
        # Resolve placeholders in test_data
        from services.integration_layer.playwright_script_generation.processing_layer.playwright_main import resolve_placeholder_in_test_data
        td = resolve_placeholder_in_test_data(td)

        # perform action based on type
        if action in {"enter", "populate", "type", "fill"}:
            await loc.fill(td, timeout=6000)

        elif action == "select":
            try:
                await loc.select_option(td, timeout=4000)
            except Exception:
                await ensure_selected(page, loc)
            
            # ✅ Enhanced post-select stabilization with async waits
            try:
                logger.debug(f"🔍 DB-REUSE: POST-SELECT WAIT | Waiting for UI changes...")
                
                # Wait for potential form submissions or dynamic updates
                try:
                    await page.wait_for_load_state('networkidle', timeout=2000)
                    logger.debug(f"🔍 DB-REUSE: POST-SELECT WAIT | Network idle achieved")
                except Exception:
                    pass
                    
                # Wait for DOM updates
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=1500)
                except Exception:
                    pass
                    
                # Small buffer for UI updates
                await asyncio.sleep(0.3)
                
            except Exception as e:
                logger.warning(f"🔍 DB-REUSE: POST-SELECT WARN | {str(e)[:50]}")
                pass

        elif action in {"click", "press", "tap"}:
            logger.debug(f"🔧 DB-ACTION: CLICK ACTION | Element: {el_tag}, Type: {el_type}, Checked: {is_checked}")
            
            if el_tag == "input" and el_type in {"radio", "checkbox"}:
                logger.debug(f"🔧 DB-ACTION: RADIO/CHECKBOX DETECTED | Attempting ensure_selected")
                try:
                    await ensure_selected(page, loc)
                    logger.debug(f"🔧 DB-ACTION: ENSURE_SELECTED SUCCESS ✅")
                except Exception as ensure_error:
                    logger.warning(f"🔧 DB-ACTION: ENSURE_SELECTED FAILED | {str(ensure_error)[:50]} | Trying label fallback")
                    if not await _click_label_fallback(page, loc):
                        logger.debug(f"🔧 DB-ACTION: LABEL FALLBACK FAILED | Using robust click")
                        await robust_click_from_db_locator(page, db_loc_str)
                    else:
                        logger.debug(f"🔧 DB-ACTION: LABEL FALLBACK SUCCESS ✅")
            else:
                logger.debug(f"🔧 DB-ACTION: REGULAR CLICK | Using robust_click_from_db_locator")
                await robust_click_from_db_locator(page, db_loc_str)

            # ✅ Enhanced post-click stabilization with async waits
            try:
                hint = _extract_visible_text_hint(db_loc_str)
                await _await_post_click_progress(page, loc, hint)
                
                # Additional async waits for click actions
                logger.debug(f"🔍 DB-REUSE: POST-CLICK WAIT | Waiting for UI changes...")
                
                # Wait for network idle (handles AJAX requests)
                try:
                    await page.wait_for_load_state('networkidle', timeout=3000)
                    logger.debug(f"🔍 DB-REUSE: POST-CLICK WAIT | Network idle achieved")
                except Exception:
                    pass  # Continue if network idle times out
                
                # Wait for DOM to stabilize (handles dynamic content)
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=2000)
                except Exception:
                    pass
                    
                # Small buffer for any remaining UI animations
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.warning(f"🔍 DB-REUSE: POST-CLICK WARN | {str(e)[:50]}")
                pass

        elif action == "verify":
            await loc.wait_for(state="visible", timeout=4000)

        execution_time = round((time.time() - start_time) * 1000, 1)
        logger.info(f"🔧 DB-ACTION: SUCCESS ✅ | {uid_base} | Action: {action} | Time: {execution_time}ms")
        return True

    except Exception as e:
        execution_time = round((time.time() - start_time) * 1000, 1)
        error_details = f"Action: {action}, Error: {str(e)}"
        if "Timeout" in str(e):
            error_details += f" | Element may not be interactable"
        if "detached" in str(e).lower():
            error_details += f" | Element may have been removed from DOM"
            
        logger.error(f"🔧 DB-ACTION: FAILED ❌ | {uid_base} | {error_details} | Time: {execution_time}ms")
        logger.debug(f"🔧 DB-ACTION: FAILED LOCATOR | {uid_base} | Locator: {db_loc_str}")
        return False

async def _update_locator_with_correct(uid_base: str, old_locator: str, correct_locator: str) -> None:
    """Update the database with the correct locator when we find one that works."""
    try:
        # Update the locator in the database with the correct working locator
        success = update_locator_validity(uid_base, correct_locator, valid=True)
        if success:
            logger.info(f"🔄 DB-UPDATE: CORRECT LOCATOR ✅ | {uid_base} | Updated with working locator")
            # Update cache with the correct locator
            if uid_base in _LOCATOR_CACHE:
                _LOCATOR_CACHE[uid_base] = CacheEntry(value=correct_locator, timestamp=time.time())
                logger.debug(f"🔄 CACHE-UPDATE: CORRECT | {uid_base} | Updated cache with working locator")
        else:
            logger.error(f"🔄 DB-UPDATE: CORRECT LOCATOR FAILED ❌ | {uid_base}")
    except Exception as e:
        logger.error(f"🔄 DB-UPDATE: ERROR ❌ | {uid_base} | {str(e)[:100]}")

async def _mark_locator_invalid(uid_base: str, db_loc_str: str) -> None:
    """Mark a locator as invalid in the database for future reference."""
    try:
        # Update the locator in the database to mark it as invalid
        success = update_locator_validity(uid_base, db_loc_str, valid=False)
        if success:
            logger.info(f"🔄 DB-UPDATE: MARK INVALID ✅ | {uid_base} | Marked as invalid in DB")
            # Remove from cache to force re-validation next time
            if uid_base in _LOCATOR_CACHE:
                del _LOCATOR_CACHE[uid_base]
                logger.debug(f"🔄 CACHE-REMOVE: INVALID | {uid_base} | Removed from cache for future re-validation")
        else:
            logger.error(f"🔄 DB-UPDATE: MARK INVALID FAILED ❌ | {uid_base}")
    except Exception as e:
        logger.error(f"🔄 DB-UPDATE: ERROR ❌ | {uid_base} | {str(e)[:100]}")


# ---quick candidate validation (cheap & action-aware) ---
async def _validate_locator_candidate(
    page: Page,
    locator_str: str,
    action: str,
    timeout_ms: int = 700,   # tiny budget; don't block JS crawl
) -> Optional[Locator]:
    """Translate → require fast attach/visibility → light action sanity. Return Locator or None."""
    try:
        logger.debug(f"🔍 VALIDATION: STARTING | Locator: {locator_str[:100]}... | Action: {action} | Timeout: {timeout_ms}ms")
        
        loc = translate_locator_to_python(page, locator_str)
        if not loc:
            logger.debug(f"🔍 VALIDATION: TRANSLATE FAILED ❌ | {locator_str[:50]}...")
            return None

        logger.debug(f"🔍 VALIDATION: TRANSLATE SUCCESS ✅ | {locator_str[:50]}...")

        # For iframe-based locators, allow slightly more time
        iframe_chain = _extract_iframe_chain(locator_str)
        validation_timeout = timeout_ms
        if iframe_chain:
            validation_timeout = min(timeout_ms * 2, 1500)  # Max 1.5 seconds for iframe validation
            logger.debug(f"🔍 VALIDATION: IFRAME CONTEXT | Extended timeout: {validation_timeout}ms")
            
        # ✅ Enhanced timeout for checkout/payment elements that may load dynamically
        checkout_keywords = ['checkout', 'proceed', 'payment', 'paypal', 'autoship', 'cart', 'order']
        is_checkout_element = any(keyword in locator_str.lower() for keyword in checkout_keywords)
        if is_checkout_element and action in {"click", "press", "tap"}:
            validation_timeout = max(validation_timeout, 1500)  # Min 1.5 seconds for checkout elements
            logger.debug(f"🔍 VALIDATION: CHECKOUT ELEMENT | Extended timeout: {validation_timeout}ms")

        # must attach quickly
        try:
            logger.debug(f"🔍 VALIDATION: CHECKING ATTACHED | Timeout: {validation_timeout}ms")
            await loc.wait_for(state="attached", timeout=validation_timeout)
            logger.debug(f"🔍 VALIDATION: ATTACHED ✅ | {locator_str[:50]}...")
        except Exception as e:
            logger.warning(f"🔍 VALIDATION: ATTACH FAILED ❌ | {locator_str[:50]}... | {str(e)[:100]}")
            return None

        # prefer visible; otherwise accept if it at least has a box
        try:
            logger.debug(f"🔍 VALIDATION: CHECKING VISIBLE | Timeout: {timeout_ms}ms")
            await loc.wait_for(state="visible", timeout=timeout_ms)
            logger.debug(f"🔍 VALIDATION: VISIBLE ✅ | {locator_str[:50]}...")
        except Exception as e:
            logger.warning(f"🔍 VALIDATION: VISIBILITY FAILED | {locator_str[:50]}... | {str(e)[:100]} | Checking bounding box...")
            try:
                logger.debug(f"🔍 VALIDATION: CHECKING BOUNDING BOX")
                box = await loc.bounding_box()
                if box:
                    logger.debug(f"🔍 VALIDATION: BOUNDING BOX ✅ | {locator_str[:50]}... | Width: {box.get('width', 0)}, Height: {box.get('height', 0)}")
                else:
                    logger.warning(f"🔍 VALIDATION: NO BOUNDING BOX ❌ | {locator_str[:50]}...")
                    return None
            except Exception as box_error:
                logger.warning(f"🔍 VALIDATION: BOUNDING BOX FAILED ❌ | {locator_str[:50]}... | {str(box_error)[:100]}")
                return None

        # action-aware sanity (fully generic)
        tag = None
        try:
            tag = await loc.evaluate("el => el && el.tagName && el.tagName.toLowerCase()")
            logger.debug(f"🔍 VALIDATION: TAG | {locator_str[:50]}... | tag: {tag}")
        except Exception as e:
            logger.debug(f"🔍 VALIDATION: TAG FAILED | {locator_str[:50]}... | {str(e)[:50]}")
            pass

        if action in {"enter", "populate", "type", "fill"}:
            try:
                is_ce = await loc.evaluate("el => !!(el && el.isContentEditable)")
                inp_type = await loc.evaluate("el => el && el.type && el.type.toLowerCase && el.type.toLowerCase()")
                logger.debug(f"🔍 VALIDATION: INPUT CHECK | {locator_str[:50]}... | tag: {tag}, type: {inp_type}, contentEditable: {is_ce}")
            except Exception:
                is_ce, inp_type = False, None
            if not (tag in {"input", "textarea"} or is_ce):
                logger.debug(f"🔍 VALIDATION: NOT INPUT ELEMENT ❌ | {locator_str[:50]}... | Expected input/textarea for {action}")
                return None
            if tag == "input" and inp_type in {"button", "submit", "radio", "checkbox"}:
                logger.debug(f"🔍 VALIDATION: WRONG INPUT TYPE ❌ | {locator_str[:50]}... | type: {inp_type}")
                return None

        if action in {"click", "press", "tap"}:
            # avoid choosing text fields as click targets
            try:
                inp_type = await loc.evaluate("el => el && el.type && el.type.toLowerCase && el.type.toLowerCase()")
                logger.debug(f"🔍 VALIDATION: CLICK CHECK | {locator_str[:50]}... | tag: {tag}, type: {inp_type}")
            except Exception:
                inp_type = None
            if tag in {"input", "textarea"} and (inp_type not in {"radio", "checkbox"}):
                logger.debug(f"🔍 VALIDATION: TEXT FIELD CLICK ❌ | {locator_str[:50]}... | Avoiding click on text field")
                return None

        logger.debug(f"🔍 VALIDATION: SUCCESS ✅ | {locator_str[:50]}... | All checks passed")
        return loc
    
    except Exception as e:
        logger.error(f"🔍 VALIDATION: UNEXPECTED ERROR ❌ | {locator_str[:50]}... | {str(e)[:100]}")
        return None
import json
import logging
import uuid
import math
import re
import pandas as pd
import time
import ftfy
import subprocess
import os
from playwright.async_api import async_playwright, Page  # Changed to async_api
from utils.locator_reuse import reuse_locator_if_possible
from services.agent_layer.playwright_html_agent import \
    inject_mutation_observer, get_iframe_details, inject_html_context_retrieval
from services.agent_layer.playwright_agent import \
    perform, handle_gift_card_flow
import datetime
import platform
from services.prompt_layer.step_def_generation_playwright import StepDefGenerationPlaywright
from services.llm_layer.LLM import LLM
from services.prompt_layer.prompt_storage import \
    STEPS_DETECTION_PROMPT as steps_detection_prompt
from services.integration_layer.playwright_script_generation.processing_layer.InputProcessor import \
    generate
from services.agent_layer.playwright_agent import record_page_object_details, extract_page_name
from services.embedding_layer.chromadb_connector import ChromaDBConnector
from services.prompt_layer.step_def_template import StepDefTemplate
from services.api_client.post_api_client import PostAPIClient, PostRequest
from services.mock_api.mock_interceptor import MockAPIInterceptor
import Levenshtein as lev
import shutil
import asyncio
from pathlib import Path
from utils.recording_cleanup import (
    _setup_failure_recording_dir,
    _build_download_url,
    _cleanup_success_recording_dir,
    register_video_run_dir,
    get_video_run_dir,
    set_last_recording_url,
    clear_video_run_dir_registration,
    cleanup_orphan_recording_dir,
)
from services.integration_layer.playwright_script_generation.processing_layer.request_bodies_manager import \
    get_request_bodies
from typing import List, Tuple, Dict, Set, Optional
from utils.s3_utils import redact_presigned_url_for_log, upload_file_to_s3_and_get_urls

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from services.interface_layer.aws_secret import get_secret_raw

# Initialize logger
logger = logging.getLogger(__name__)
llm = LLM()
chroma_connector = ChromaDBConnector()
post_client = PostAPIClient()
mock_interceptor = MockAPIInterceptor()  # Global instance for route management


# Enterprise-level in-memory response storage
_stored_responses = {}
_stored_tokens = {}
_pending_api_configs = {}  # Store pending API configurations by service name
_deferred_api_calls = {}  # Store API calls that need to be executed after config steps
_aws_secret_cache = {}

def _cleanup_global_state(preserve_aws_cache: bool = False):
    """Clear global variables to prevent data leakage between test runs.
    
    Args:
        preserve_aws_cache: If True, preserve AWS secret cache across scenarios (they don't change within a test run).
                           If False, clear all state including AWS cache (full test run reset).
    """
    global _stored_responses, _stored_tokens, _pending_api_configs, _deferred_api_calls, _aws_secret_cache
    try:
        _stored_responses.clear()
        _stored_tokens.clear()
        _pending_api_configs.clear()
        _deferred_api_calls.clear()
        
        # AWS secrets don't change between scenarios in the same test run, so preserve cache for efficiency
        if not preserve_aws_cache:
            _aws_secret_cache.clear()
            logger.info("✅ Global state cleaned including AWS cache (test run level)")
        else:
            logger.info("✅ Per-scenario state cleaned (AWS cache preserved for efficiency)")
    except Exception as e:
        logger.warning(f"⚠️ Error cleaning global state: {e}")

def reset_in_memory_api_state_for_run() -> None:
    """
    Clear per-run API response/token buffers so a long-lived worker does not retain
    prior crawl data in module-level dicts.
    """
    _stored_responses.clear()
    _stored_tokens.clear()
    _pending_api_configs.clear()
    _deferred_api_calls.clear()


async def _update_working_locator_in_db(test_step: dict, current_df: pd.DataFrame) -> None:
    """
    Update the database with the working locator when normal discovery succeeds
    after locator reuse failed. This prevents marking locators as invalid.
    """
    def _should_skip_dynamic_content_for_db(step_text: str) -> bool:
        """
        Enhanced check for dynamic content that shouldn't be stored in DB.
        This function detects various patterns of dynamic content to prevent
        caching locators that won't be reusable due to changing values.
        """
        if not step_text:
            return False

        step_lower = str(step_text).lower()

        # Skip if step explicitly contains 'dynamic' keyword
        if "dynamic" in step_lower:
            return True
        
        # Check for common dynamic content patterns
        dynamic_patterns = [
            # Email addresses
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            # Placeholder patterns like {variableName} or {{variableName}}
            r'\{\{?[^}]+\}\}?',
            # Phone numbers
            r'\b\(?[\d]{3}\)?[-.\s]?[\d]{3}[-.\s]?[\d]{4}\b',
            # Timestamps and dates
            r'\b\d{4}[-/]\d{2}[-/]\d{2}(?:[T\s]\d{2}:\d{2}:\d{2}(?:\.\d{3})?(?:Z|[+-]\d{2}:\d{2})?)?\b',
            # UUIDs
            r'\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b',
            # Long numeric IDs (6+ digits)
            r'\b\d{6,}\b',
            # Credit card numbers
            r'\b(?:\d{4}[-\s]?){3}\d{4}\b'
        ]
        
        # Check if any pattern matches
        for pattern in dynamic_patterns:
            if re.search(pattern, step_text, re.IGNORECASE):
                logger.debug(f"🔧 DB-SKIP: DYNAMIC CONTENT DETECTED | Pattern matched: {pattern[:30]}... | Step: {step_text[:50]}...")
                return True
        
        # Additional heuristic checks
        # Check for common dynamic keywords in step text
        dynamic_keywords = [
            'random', 'generate', 'timestamp', 'uuid', 'guid', 
            'current', 'now', 'today', 'temp', 'temporary'
        ]
        
        for keyword in dynamic_keywords:
            if keyword in step_lower:
                logger.debug(f"🔧 DB-SKIP: DYNAMIC KEYWORD DETECTED | Keyword: {keyword} | Step: {step_text[:50]}...")
                return True
        
        # Specific check for resolved placeholder patterns
        resolved_placeholder_patterns = [
            # Clicks on pure numeric values (likely resolved userIds, orderIds)
            r'clicks on "\d+"',
            r'click on "\d+"',
            # Enters field with numeric value (likely resolved IDs)
            r'enters "[^"]*" as "\d+"',
            r'enter "[^"]*" as "\d+"',
            # Navigate to URLs with long numeric IDs
            r'navigate.*\/\d{6,}',
            # Select options with numeric IDs
            r'select.*"\d{6,}"'
        ]
        
        for pattern in resolved_placeholder_patterns:
            if re.search(pattern, step_text, re.IGNORECASE):
                logger.debug(f"🔧 DB-SKIP: RESOLVED PLACEHOLDER DETECTED | Pattern: {pattern[:30]}... | Step: {step_text[:50]}...")
                return True

        return False

    try:
        uid_base = test_step.get('unique_id')
        logger.info(f"🔧 DB-UPSERT: STARTING | {uid_base} | Attempting to insert/update working locator")

        if not uid_base:
            logger.warning(f"🔧 DB-UPSERT: NO UID ❌ | Cannot save - no unique_id")
            return

        # Get the latest locator from current_df (last row should be the current step)
        if current_df.empty:
            logger.warning(f"🔧 DB-UPSERT: EMPTY DF ❌ | {uid_base} | No data to extract locator from")
            return

        # Get the last row which should contain the working locator
        last_row = current_df.iloc[-1]
        new_locator = last_row.get('playwrightLocator', '')

        logger.info(f"🔧 DB-UPSERT: LOCATOR EXTRACTED | {uid_base} | New locator: {new_locator[:100] if new_locator else 'NONE'}...")

        if new_locator and new_locator.strip():
            # ✅ Check if this step contains dynamic content that shouldn't be stored
            original_bdd_step = test_step.get('original_bdd_step', '')
            if _should_skip_dynamic_content_for_db(original_bdd_step):
                logger.info(f"🚫 DB-UPSERT: SKIP DYNAMIC | {uid_base} | Contains dynamic content - not storing in DB")
                logger.debug(f"🚫 DB-UPSERT: DYNAMIC STEP | {original_bdd_step[:80]}...")
                return

            # Import here to avoid circular imports
            from utils.db_utils import update_locator_validity
            from utils.locator_reuse import _LOCATOR_CACHE, CacheEntry
            import time

            # Insert or update the database with the new working locator
            logger.info(f"🔧 DB-UPSERT: CALLING UPSERT | {uid_base} | Saving working locator to database")
            success = update_locator_validity(uid_base, new_locator, valid=True)
            if success:
                logger.info(f"🔄 DB-UPSERT: WORKING LOCATOR ✅ | {uid_base} | Saved new locator: {new_locator[:100]}...")

                # 🔧 ALSO UPDATE CACHE: Replace invalid/old cache entry with new working locator
                current_time = time.time()
                _LOCATOR_CACHE[uid_base] = CacheEntry(
                    value=new_locator,
                    timestamp=current_time,
                    access_count=1
                )
                logger.info(f"🔄 CACHE-UPDATE: WORKING LOCATOR ✅ | {uid_base} | Updated cache with new locator")

            else:
                logger.warning(f"🔄 DB-UPSERT: FAILED ❌ | {uid_base} | Could not save locator")
        else:
            logger.warning(f"🔧 DB-UPSERT: NO LOCATOR ❌ | {uid_base} | No valid locator found in current_df")
            logger.debug(f"🔄 DB-UPDATE: NO LOCATOR | {uid_base} | No valid locator found to update")

    except Exception as e:
        logger.error(f"🔄 DB-UPSERT: ERROR ❌ | {test_step.get('unique_id', 'unknown')} | {str(e)[:100]}")


async def log_http_error_response(response, page_type="Unknown"):
    """
    Comprehensive logging for HTTP error responses (4xx and 5xx status codes)

    This function captures complete request/response details for debugging browser resource loading issues.
    It logs CSS, JavaScript, API calls, and any other HTTP requests that return error status codes.

    Args:
        response: Playwright Response object containing the HTTP response
        page_type: String identifying the source page ("Main Page", "New Page", "Popup", etc.)

    Logs:
        - Request method, headers, and User-Agent verification
        - Response status, headers, and body content
        - Security-related headers (Kasada, Cloudflare, etc.)
        - Error keywords in response body
        - Resource type and timestamp for correlation
    """
    try:
        status_code = response.status

        # Only log if it's a 4xx or 5xx error
        if status_code < 400:
            return

        # Determine error type for better categorization
        error_type = "CLIENT_ERROR" if 400 <= status_code < 500 else "SERVER_ERROR"

        logger.error(f"🚨 HTTP {error_type} ({page_type}) - Status: {status_code}, URL: {response.url}")
        print(f"🚨 HTTP {error_type} ({page_type}) - Status: {status_code}, URL: {response.url}")

        # Log request details
        request = response.request
        request_headers = dict(request.headers)
        logger.error(f"🚨 {status_code} Request Method: {request.method}")
        logger.error(f"🚨 {status_code} Request Headers: {request_headers}")

        # Special handling for User-Agent to check Kasada bypass
        sent_ua = request_headers.get('user-agent', '')
        kasada_bypass = "LowerEnvString/9a045ab84c4610ffb459247df65a6377"
        if kasada_bypass in sent_ua:
            logger.error(f"✅ {status_code} Kasada bypass WAS sent in request")
        else:
            logger.error(f"❌ {status_code} Kasada bypass NOT found in User-Agent")
            logger.error(f"🔍 {status_code} Sent User-Agent: {sent_ua}")

        # Log response headers
        response_headers = dict(response.headers)
        logger.error(f"🚨 {status_code} Response Headers: {response_headers}")

        # Check for security/blocking related headers
        security_keywords = ['kasada', 'cloudflare', 'incapsula', 'bot', 'blocked', 'challenge', 'security', 'protection', 'akamai']
        security_headers = {}
        for header, value in response_headers.items():
            if any(keyword in header.lower() or keyword in str(value).lower() for keyword in security_keywords):
                security_headers[header] = value

        if security_headers:
            logger.error(f"🚨 {status_code} Security Headers Detected: {security_headers}")

        # Log response body with size limits
        try:
            response_text = await response.text()
            if response_text:
                # Log full body for smaller responses, truncated for larger ones
                if len(response_text) <= 1000:
                    logger.error(f"🚨 {status_code} Response Body: {response_text}")
                else:
                    logger.error(f"🚨 {status_code} Response Body (first 500 chars): {response_text[:500]}...")
                    logger.error(f"🚨 {status_code} Response Body (last 200 chars): ...{response_text[-200:]}")
                    logger.error(f"🚨 {status_code} Total Response Size: {len(response_text)} characters")

                # Check for common error keywords in response body
                error_keywords = ['access denied', 'forbidden', 'unauthorized', 'blocked', 'kasada', 'bot detected',
                                'challenge', 'security violation', 'not found', 'server error', 'service unavailable']
                found_keywords = [keyword for keyword in error_keywords if keyword in response_text.lower()]
                if found_keywords:
                    logger.error(f"🚨 {status_code} Error Keywords Found: {found_keywords}")
            else:
                logger.error(f"🚨 {status_code} Response Body: [EMPTY]")

        except Exception as body_error:
            logger.error(f"🚨 {status_code} Could not read response body: {body_error}")

        # Log additional context for debugging
        import datetime
        logger.error(f"🚨 {status_code} Timestamp: {datetime.datetime.now().isoformat()}")
        logger.error(f"🚨 {status_code} Resource Type: {response.request.resource_type}")
        logger.error(f"🚨 {status_code} {'='*80}")

    except Exception as e:
        logger.error(f"Error in HTTP error logging function: {e}")


def _get_aws_secret_dict(secret_name=None):
    cache_key = secret_name or "__default__"
    if cache_key in _aws_secret_cache:
        return _aws_secret_cache[cache_key]
    secret_dict = get_secret_raw(secret_name)
    _aws_secret_cache[cache_key] = secret_dict or {}
    return _aws_secret_cache[cache_key]


def resolve_aws_secret_placeholder(field_pattern):
    prefix = "awssecretmanager."
    if not field_pattern.startswith(prefix):
        return None
    remainder = field_pattern[len(prefix):]
    if not remainder:
        return None
    parts = remainder.split('.')
    if len(parts) == 1:
        secret_name = None
        path_parts = parts
    else:
        secret_name = parts[0]
        path_parts = parts[1:]

    secret_dict = _get_aws_secret_dict(secret_name)
    value = secret_dict
    for part in path_parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return str(value) if value is not None else None


def replace_awssecret_placeholders_in_text(text):
    if not isinstance(text, str) or ('{' not in text and '}' not in text):
        return text
    matches = re.findall(r'\{\{([^}]+)\}\}|\{([^}]+)\}', text)
    for m in matches:
        field_pattern = m[0] if m[0] else m[1]
        if field_pattern.startswith('awssecretmanager.'):
            resolved = resolve_aws_secret_placeholder(field_pattern)
            if resolved is not None:
                text = text.replace(f'{{{{{field_pattern}}}}}', resolved)
                text = text.replace(f'{{{field_pattern}}}', resolved)
    return text


def resolve_placeholder_in_test_data(text):
    """Resolve placeholders in test_data including AWS secrets and stored response fields"""
    if not isinstance(text, str) or ('{' not in text and '}' not in text):
        return text

    # First handle AWS secrets
    text = replace_awssecret_placeholders_in_text(text)

    # Then handle stored response fields
    matches = re.findall(r'\{\{([^}]+)\}\}|\{([^}]+)\}', text)
    for m in matches:
        field_pattern = m[0] if m[0] else m[1]

        # Handle dot notation: accountResponse.email
        if '.' in field_pattern:
            parts = field_pattern.split('.')
            response_name = parts[0]

            # Case-insensitive lookup
            found_response = None
            for stored_key in _stored_responses.keys():
                if stored_key.lower() == response_name.lower():
                    found_response = stored_key
                    break

            if found_response:
                value = _stored_responses[found_response]
                for part in parts[1:]:
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        value = None
                        break

                if value is not None:
                    text = text.replace(f'{{{{{field_pattern}}}}}', str(value))
                    text = text.replace(f'{{{field_pattern}}}', str(value))

        # Handle simple token references
        else:
            for token_key in [field_pattern, field_pattern.lower(), field_pattern.upper()]:
                if token_key in _stored_tokens:
                    value = _stored_tokens[token_key]
                    text = text.replace(f'{{{{{field_pattern}}}}}', str(value))
                    text = text.replace(f'{{{field_pattern}}}', str(value))
                    break

    return text


def _replace_aws_in_body(obj):
    if isinstance(obj, dict):
        return {k: _replace_aws_in_body(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_aws_in_body(v) for v in obj]
    if isinstance(obj, str):
        return replace_awssecret_placeholders_in_text(obj)
    return obj


async def handle_navigation_step(test_step: dict, page, already_window_maximized: bool, current_df,
                                 new_window_detected, explicit_viewport_applied: bool = False) -> tuple:
    """
    Handle navigation/launch related steps like 'open', 'navigate', 'go to', 'launch'

    Args:
        test_step: Dictionary containing step information
        page: Playwright page object
        already_window_maximized: Boolean indicating if window is already maximized
        current_df: Current DataFrame with test steps
        new_window_detected: Window detection state object

    Returns:
        tuple: (page, already_window_maximized, current_df)
    """
    test_data = test_step['test_data']

    # Check for mobile view condition (skip if an explicit viewport has already been applied)
    if not explicit_viewport_applied and any(word in test_step['condition'].lower() for word in ['mobile view']):
        # Fixed mobile viewport size (iPhone 12 Pro)
        await asyncio.sleep(2)
        await page.set_viewport_size({"width": 390, "height": 844})
        response = await page.goto(test_step['test_data'], wait_until='domcontentloaded')
        logger.info(f"*******Response status code received: {response.status}")
    else:
        if not already_window_maximized and not explicit_viewport_applied:
            # Fixed desktop viewport size (standard 1080p)
            await page.set_viewport_size({"width": 1920, "height": 1080})
            response = await page.goto(test_step['test_data'], wait_until='domcontentloaded')
            logger.info(f"*******Response status code received: {response.status}")
            already_window_maximized = True
        else:
            response = await page.goto(test_step['test_data'], wait_until='domcontentloaded')
            logger.info(f"*******Response status code received: {response.status}")

    await page.wait_for_load_state('domcontentloaded')

    # Record navigation details
    page_name = extract_page_name(page.url)
    window_handle = page
    content_for_recording = page
    current_df = await record_page_object_details(
        page_name, 'Navigate', '',
        test_data, '', '', content_for_recording,
        page, None, '',
        test_step['condition'], '', '',
        current_df, test_step, new_window_detected
    )

    await asyncio.sleep(3)

    return page, already_window_maximized, current_df

async def wait_for_page_stable(page: Page, timeout: int = 10000) -> None:
    """
    A forgiving 'page is ready enough' wait:
    1) try networkidle, 2) fall back to domcontentloaded.
    We do NOT require document.readyState === 'complete' (brittle for SPAs).
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
        return
    except Exception:
        pass
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        # swallow — we don't want this to break the flow
        pass


async def execute_get_api_call(test_step, original_bdd_step, parsed_step, page, current_df,new_window_detected):
    """Execute GET API call using enterprise client"""
    try:
        # Extract URL from step
        url_match = re.search(r'"([^"]+)"', original_bdd_step)
        if url_match:
            url = url_match.group(1)
            logger.info(f"🔍 DEBUG: Extracted URL: {url}")

            # Apply pending headers for GET API
            headers = {}
            auth_token = None
            service_name = 'get_api'
            pending_config = _pending_api_configs.get(service_name)

            if pending_config:
                pending_headers = pending_config.get('headers', {})
                # Apply field injection to header values
                for key, value in pending_headers.items():
                    if '{' in value and '}' in value:
                        field_matches = re.findall(r'\{([^}]+)\}', value)
                        for field_pattern in field_matches:
                            if field_pattern in _stored_tokens:
                                token_value = _stored_tokens[field_pattern]
                                value = value.replace(f'{{{field_pattern}}}', str(token_value))

                    # Extract auth token if it's Authorization header
                    if key.lower() == 'authorization' and value.startswith('Bearer '):
                        auth_token = value.replace('Bearer ', '')
                    else:
                        headers[key] = value

                logger.info(f"✅ Applied GET headers: {headers}")
                logger.info(f"🔑 Auth token: {auth_token[:20] if auth_token else 'None'}...")

                # Clear pending config
                del _pending_api_configs[service_name]

            # Use enterprise GET API client
            from services.api_client.get_api_client import get_api_call
            result = get_api_call(url, auth_token, headers, None, "bearer")

            logger.info(f"✅ GET REQUEST SUCCESS: {url}")
            logger.info(f"✅ STATUS: {result['status_code']}")
            logger.info(f"📋 RESPONSE HEADERS: {result.get('response_headers', 'Not available')}")
            logger.info(f"📊 RESPONSE DATA: {result['data']}")

            # Store response for later use
            _stored_responses['last_response'] = result['data']
            _stored_responses['getResponse'] = result['data']

            logger.info(f"✅ ENTERPRISE STORAGE: Response stored as getResponse and last_response")
            logger.info(f"✅ Available responses: {list(_stored_responses.keys())}")

        # Record for TypeScript generation
        current_df = await record_page_object_details("API_Call", 'GET-API',
                                                      test_step.get('test_data', ''),
                                                      f"GET: {parsed_step}", '', '',
                                                      page, page, None, '',
                                                      test_step.get('condition', ''), '', '', current_df, test_step,
                                                      new_window_detected)

    except Exception as e:
        logger.info(f"❌ ENTERPRISE GET API FAILED: {e}")

    return current_df


async def execute_api_call(test_step, original_bdd_step, parsed_step, service_name, page, current_df,new_window_detected):
    """Execute API call with current pending configurations"""
    try:
        # Extract config file and service name dynamically
        logger.info(f"🔍 DEBUG: Parsed step: {parsed_step}")
        logger.info(f"🔍 DEBUG: Original BDD step: {original_bdd_step}")

        # Use original BDD step for regex matching
        step_for_matching = original_bdd_step if original_bdd_step else parsed_step
        logger.info(f"🔍 DEBUG: Step for matching: {step_for_matching}")

        # Look for config file in multiple patterns
        config_match = re.search(r'using configuration from "([^"]+)"', step_for_matching)
        service_with_config_match = re.search(r'with "([^"]+\.json)"', step_for_matching)
        simple_config_match = re.search(r'"([^"]+\.json)"', step_for_matching)
        auth_token_match = re.search(r'with auth token "\{([^}]+)\}"', step_for_matching)

        logger.info(f"🔍 DEBUG: Config match: {config_match}")
        logger.info(f"🔍 DEBUG: Service with config match: {service_with_config_match}")
        logger.info(f"🔍 DEBUG: Simple config match: {simple_config_match}")

        # Get config file from step (try multiple patterns)
        config_file = None
        if config_match:
            config_file = config_match.group(1)
            logger.info(f"🔍 DEBUG: Extracted config file from 'using configuration': {config_file}")
        elif service_with_config_match:
            config_file = service_with_config_match.group(1)
            logger.info(f"🔍 DEBUG: Extracted config file from service call: {config_file}")
        elif simple_config_match:
            config_file = simple_config_match.group(1)
            logger.info(f"🔍 DEBUG: Extracted config file from simple pattern: {config_file}")

        if config_file:
            # Get request bodies from upload or file system
            request_bodies = get_request_bodies()
            config_key = config_file.replace('.json', '')

            logger.info(f"🔍 DEBUG: Request bodies: {request_bodies}")
            logger.info(f"🔍 DEBUG: Config key: {config_key}")
            logger.info(f"🔍 DEBUG: Available keys: {list(request_bodies.keys()) if request_bodies else 'None'}")

            if request_bodies and config_key in request_bodies:
                config = request_bodies[config_key]
                logger.info(f"📁 LOADED CONFIG FROM UPLOAD: {config_file}")
                logger.info(f"📊 CONFIG CONTENT: {config}")
            else:
                logger.info(f"⚠️ Config file not found: {config_file}")
                return current_df
        else:
            logger.info(f"⚠️ Could not determine config file for step: {parsed_step}")
            return current_df

        # Generate timestamp for replacements
        import time
        current_timestamp = str(int(time.time()))

        # Prepare headers with auth token injection
        headers = config.get('headers', {}).copy()

        # Apply pending headers for this service
        logger.info(f"🔍 DEBUG: Looking for pending config for service: '{service_name}'")
        logger.info(f"🔍 DEBUG: Available pending configs: {list(_pending_api_configs.keys())}")

        # Apply pending headers for this service (exact match only)
        pending_config = _pending_api_configs.get(service_name)

        if pending_config:
            pending_headers = pending_config.get('headers', {})
            # Apply field injection to header values
            for key, value in pending_headers.items():
                if isinstance(value, str):
                    value = replace_awssecret_placeholders_in_text(value)
                if '{' in value and '}' in value:
                    field_matches = re.findall(r'\{([^}]+)\}', value)
                    for field_pattern in field_matches:
                        if field_pattern in _stored_tokens:
                            token_value = _stored_tokens[field_pattern]
                            value = value.replace(f'{{{field_pattern}}}', str(token_value))
                    headers[key] = value
                else:
                    headers[key] = value
            logger.info(f"✅ Applied pending headers: {pending_headers}")
        else:
            logger.info(f"🔍 No pending config found for service: {service_name}")

        # Handle URL field injection and timestamp replacement
        url = config.get('url', '')
        url = replace_awssecret_placeholders_in_text(url)
        if '{timestamp}' in url:
            url = url.replace('{timestamp}', current_timestamp)
            logger.info(f"✅ Replaced timestamp in URL: {current_timestamp}")

        if '{' in url and '}' in url:
            field_matches = re.findall(r'\{([^}]+)\}', url)
            for field_pattern in field_matches:
                if '.' in field_pattern:
                    parts = field_pattern.split('.')
                    response_name = parts[0]
                    if response_name in _stored_responses:
                        value = _stored_responses[response_name]
                        for part in parts[1:]:
                            if isinstance(value, dict) and part in value:
                                value = value[part]
                            else:
                                value = None
                                break
                        if value is not None:
                            url = url.replace(f'{{{field_pattern}}}', str(value))

        # Prepare request body with pending updates
        body = config.get('body', config.get('data', {})).copy() if config.get('body', config.get('data', {})) else {}
        body = _replace_aws_in_body(body)
        if pending_config:
            body_updates = pending_config.get('body_updates', {})
            logger.info(f"🔍 DEBUG: Original body: {body}")
            logger.info(f"🔍 DEBUG: Body updates to apply: {body_updates}")
            body.update(body_updates)
            logger.info(f"✅ Applied pending body updates: {body_updates}")
            logger.info(f"🔍 DEBUG: Final body after updates: {body}")
        else:
            logger.info(f"🔍 No pending body updates for service: {service_name}")
            logger.info(f"🔍 DEBUG: Final body (no updates): {body}")

        # Replace timestamp placeholders in body
        body_str = json.dumps(body)
        if '{timestamp}' in body_str:
            body_str = body_str.replace('{timestamp}', current_timestamp)
            body = json.loads(body_str)
            logger.info(f"✅ Replaced timestamp placeholders: {current_timestamp}")

        # Prepare POST request from any config structure
        post_request = PostRequest(
            url=url,
            headers=headers,
            body=body,
            content_type=headers.get('Content-Type', config.get('content_type', 'application/json')),
            method=config.get('method')  # Use method from config if provided
        )

        # Execute POST request
        logger.info(f"🚀 MAKING API CALL TO: {post_request.url}")
        logger.info(f"🚀 WITH HEADERS: {post_request.headers}")
        logger.info(f"🚀 AUTHORIZATION HEADER: {post_request.headers.get('Authorization', 'NOT FOUND')}")
        response = await post_client.universal_post(post_request)

        # Dynamic service name from config file
        if config_file:
            # Convert config file to camelCase response key: "any_file.json" -> "anyFileResponse"
            base_name = config_file.replace('.json', '')
            parts = base_name.split('_')
            service_name_key = parts[0] + ''.join(word.capitalize() for word in parts[1:]) + 'Response'
        else:
            service_name_key = 'apiResponse'

        # Store response with dynamic key
        _stored_responses[service_name_key] = response.response_data
        _stored_responses['last_response'] = response.response_data

        # Store common response patterns
        if "auth" in service_name_key.lower():
            _stored_responses['authresponse'] = response.response_data
            _stored_responses['authResponse'] = response.response_data
        elif "gift" in service_name_key.lower():
            _stored_responses['giftcardresponse'] = response.response_data
            _stored_responses['giftCardResponse'] = response.response_data

        # Store tokens if present
        if isinstance(response.response_data, dict):
            if 'access_token' in response.response_data:
                _stored_tokens['authToken'] = response.response_data['access_token']
                _stored_tokens['authtoken'] = response.response_data['access_token']
                logger.info(f"🔑 STORED AUTH TOKEN: {response.response_data['access_token'][:10]}...")

        logger.info(f"✅ ENTERPRISE STORAGE: Response stored as {service_name_key} and last_response")
        logger.info(f"✅ Available responses: {list(_stored_responses.keys())}")
        logger.info(f"✅ Available tokens: {list(_stored_tokens.keys())}")
        logger.info(f"✅ STATUS: {response.status_code}")
        logger.info(f"📋 RESPONSE HEADERS: {response.headers}")
        logger.info(f"📄 CONFIG FILE: {config_file}")
        logger.info(f"📊 RESPONSE DATA: {response.response_data}")
        logger.info(f"📊 STORED RESPONSES NOW: {list(_stored_responses.keys())}")

        # Clear pending config for this service after successful request
        if pending_config and service_name in _pending_api_configs:
            del _pending_api_configs[service_name]
            logger.info(f"✅ Cleared pending config for service: {service_name}")

        # Remove from deferred calls if it was there
        if service_name in _deferred_api_calls:
            del _deferred_api_calls[service_name]
            logger.info(f"✅ Removed deferred API call for service: {service_name}")

        logger.info(f"🔍 Final request headers sent: {post_request.headers}")
        logger.info(f"🔍 Final request body sent: {post_request.body}")

        # Record for TypeScript generation
        current_df = await record_page_object_details("API_Call", 'POST-API',
                                                      test_step.get('test_data', ''),
                                                      f"POST: {parsed_step}", '', '',
                                                      page, page, None, '',
                                                      test_step.get('condition', ''), '', '', current_df, test_step,
                                                      new_window_detected)

    except Exception as e:
        logger.info(f"❌ ENTERPRISE POST API FAILED: {e}")
        logger.info(f"💡 Ensure config file has valid URL and is uploaded with the feature file")

    return current_df


def levenshtein_similarity(str1, str2):
    distance = lev.distance(str1, str2)
    max_len = max(len(str1), len(str2))
    if max_len == 0:
        return 1.0
    return 1 - (distance / max_len)


# Helper for intent-based element matching
def _matches_intent_pattern(element_name: str, response_element: str) -> bool:
    """
    Determine if a generic intent in a BDD step (such as 'numeric link' or 'number in the table') matches
    the element text returned by the LLM.  This helper allows loose matching
    against patterns rather than strict string similarity.

    Currently supported intents:
      * numeric link / numeric / number link / id link / number: matches any string of
        digits (with optional spaces or commas between digits).  This allows a
        step like "click-number in the table" to match a concrete ID such as
        "332025256" without relying solely on Levenshtein similarity.
      * spatial relationships: "X beside Y", "X near Y", "X next to Y" matches if
        the response element contains the first part (X) when describing spatial layout.

    Args:
        element_name: The element portion of the BDD step (after the action).
        response_element: The element returned by the LLM (e.g. a visible text).

    Returns:
        bool: True if the element matches an intent-based pattern, False otherwise.
    """
    try:
        # Normalize the element name for intent detection
        name_norm = (element_name or '').strip().lower().replace('-', ' ').replace('_', ' ')
        response_norm = (response_element or '').strip().lower()
        
        # Detect numeric-related intents; include 'number' to match phrases like 'number in the table'
        # Also include 'id' to match patterns like 'User ID', 'Customer ID', etc.
        # Use word boundaries to avoid false matches (e.g., 'id' in 'beside')
        numeric_patterns = [
            r'\bnumeric\s+link\b', r'\bnumeric\b', r'\bnumber\s+link\b', 
            r'\bid\s+link\b', r'\bnumber\b', r'\bid\b', r'\buser\s+id\b', r'\bcustomer\s+id\b'
        ]
        if any(re.search(pattern, name_norm) for pattern in numeric_patterns):
            # Use fullmatch to ensure the entire response element consists of digits
            # Optionally separated by whitespace or commas
            return re.fullmatch(r'\d[\d\s,]*', (response_element or '').strip()) is not None
        
        # Detect spatial relationship patterns like "Change beside Payment Method"
        spatial_keywords = ['beside', 'near', 'next to', 'close to', 'adjacent to']
        for keyword in spatial_keywords:
            if keyword in name_norm:
                # Extract the element name before the spatial keyword
                # e.g., "Change beside Payment Method" -> "change"
                element_part = name_norm.split(keyword)[0].strip()
                if element_part and (element_part in response_norm or response_norm in element_part):
                    return True
                # Also check if the response element contains the target action word
                if element_part and any(word in response_norm for word in element_part.split()):
                    return True
                
    except Exception:
        # In the unlikely event of malformed input, do not block execution
        pass
    return False


async def check_and_execute_deferred_api(service_name, test_steps, current_step_index, page, current_df,new_window_detected):
    """Check if current step is 'stores response as' to execute deferred API call"""
    if current_step_index >= len(test_steps):
        return current_df

    current_step = test_steps[current_step_index]
    original_bdd_step = str(current_step.get('original_bdd_step', '')).lower()

    # Check if current step is "stores response as"
    if "stores response as" in original_bdd_step:
        # Extract service name from the step (e.g., "sessionResponse" -> "session", "userResponse" -> "user")
        match = re.search(r'stores response as "(\w+)Response"', original_bdd_step)
        if match:
            response_name = match.group(1).lower()  # e.g., "session", "user"

            # Find matching deferred API call
            for stored_service in list(_deferred_api_calls.keys()):
                if response_name in stored_service.lower() or stored_service.lower() in response_name or stored_service == 'get_api':
                    logger.info(
                        f"🚀 EXECUTING DEFERRED API CALL for service: {stored_service} (matched with {response_name})")
                    deferred_call = _deferred_api_calls[stored_service]

                    # Execute based on API type
                    if deferred_call.get('api_type') == 'GET':
                        current_df = await execute_get_api_call(deferred_call['step'],
                                                                deferred_call['original_bdd_step'],
                                                                deferred_call['parsed_step'], page, current_df,new_window_detected)
                    else:
                        current_df = await execute_api_call(deferred_call['step'], deferred_call['original_bdd_step'],
                                                            deferred_call['parsed_step'], stored_service, page,
                                                            current_df,new_window_detected)

                    # Remove from deferred calls
                    del _deferred_api_calls[stored_service]
                    break
        else:
            # Fallback: execute all deferred calls if pattern doesn't match
            for stored_service, deferred_call in list(_deferred_api_calls.items()):
                logger.info(f"🚀 EXECUTING DEFERRED API CALL (fallback) for service: {stored_service}")

                if deferred_call.get('api_type') == 'GET':
                    current_df = await execute_get_api_call(deferred_call['step'], deferred_call['original_bdd_step'],
                                                            deferred_call['parsed_step'], page, current_df,new_window_detected)
                else:
                    current_df = await execute_api_call(deferred_call['step'], deferred_call['original_bdd_step'],
                                                        deferred_call['parsed_step'], stored_service, page, current_df,new_window_detected)

            # Clear all deferred calls
            _deferred_api_calls.clear()

    return current_df


async def find_nearest_first_xpath(xpath_lists, page):  # Made async
    def remove_trailing_slash(xpath):
        return xpath.rstrip('/')

    # Remove trailing slashes from all XPaths in the dictionary
    for key in xpath_lists:
        xpath_lists[key] = [remove_trailing_slash(xpath) for xpath in xpath_lists[key]]

    async def get_element_location(xpath):
        try:
            # Use JavaScript to directly get the first element's position
            position = await page.evaluate(f'''
                () => {{
                    const element = document.evaluate('{xpath}', document, null,
                                  XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                    if (!element) return null;

                    const rect = element.getBoundingClientRect();
                    return {{ x: rect.x, y: rect.y }};
                }}
            ''')
            return position
        except Exception as e:
            logger.info(f"Error locating element for XPath {xpath}: {e}")
            return None

    def calculate_distance(loc1, loc2):
        if loc1 and loc2:
            x1, y1 = loc1['x'], loc1['y']
            x2, y2 = loc2['x'], loc2['y']
            distance = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
            return distance
        return float('inf')

    first_obj = list(xpath_lists.keys())[0]
    other_objs = list(xpath_lists.keys())[1:]

    nearest_first_xpath = None
    min_average_distance = float('inf')

    for first_xpath in xpath_lists[first_obj]:
        first_location = await get_element_location(first_xpath)  # Added await
        if not first_location:
            continue

        total_distance = 0
        valid_distances = 0

        for other_obj in other_objs:
            nearest_distance = float('inf')
            for other_xpath in xpath_lists[other_obj]:
                other_location = await get_element_location(other_xpath)  # Added await
                if other_location:
                    distance = calculate_distance(first_location, other_location)
                    nearest_distance = min(nearest_distance, distance)

            if nearest_distance != float('inf'):
                total_distance += nearest_distance
                valid_distances += 1

        if valid_distances > 0:
            average_distance = total_distance / valid_distances

            if average_distance < min_average_distance:
                min_average_distance = average_distance
                nearest_first_xpath = first_xpath

    logger.info(f"Nearest XPath: {nearest_first_xpath}")
    return nearest_first_xpath


def clean_content(content):
    # Remove '''json from the beginning and the last three ''' from the end
    return content[6:] if content.startswith("'''json") else content[:-3] if content.endswith("'''") else content


def create_timestamp_filename(folder_path, extn):
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{folder_path}/data_{timestamp}{extn}"
    return filename


async def comprehensive_wait(page, timeout=30000):

    await page.wait_for_load_state('domcontentloaded', timeout=30000)
    await page.wait_for_timeout(5000)
    # Wait for content
    await page.wait_for_selector('body', timeout=20000)

    content_selectors = ['main', '.content', '.dashboard', 'h1', '.page-title']
    for selector in content_selectors:
        try:
            await page.wait_for_selector(selector, state='visible', timeout=3000)
            break
        except:
            continue
    # Final document ready check
    await page.wait_for_load_state("domcontentloaded", timeout=10000)
    try:
        await page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass




async def window_switch_handling(context, current_page, current_df, test_step, new_window_detected ,max_retries=3):
    """Improved version that handles changing pages better (SPA/drawers-safe) with window type detection."""

    for attempt in range(max_retries):
        try:
            # Step 1: Wait for pages list to stabilize
            stable_pages = []
            stable_count = 0

            for _ in range(5):  # up to 5 checks
                candidate_pages = [p for p in context.pages if not p.is_closed()]
                if len(candidate_pages) == len(stable_pages):
                    stable_count += 1
                    if stable_count >= 2:  # same count twice in a row
                        break
                else:
                    stable_count = 0
                    stable_pages = candidate_pages
                await asyncio.sleep(0.2)  # ~200ms between checks

            if not stable_pages:
                logger.info(f"❌ No stable pages found on attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)
                    continue
                raise Exception(
                    message="Browser window handling failed",
                    error_code="BROWSER_ERROR",
                    context={"issue": "no_stable_windows", "attempt": attempt}
                )

            logger.info(f"📊 Found {len(stable_pages)} stable pages")

            # Detect window type based on page characteristics
            window_type = None
            if len(stable_pages) > 1:
                new_page = stable_pages[-1]  # Most recently opened

                try:
                    # Check if it's likely a popup vs new tab based on URL patterns and viewport
                    new_url = new_page.url
                    viewport_size = new_page.viewport_size

                    # Common popup indicators
                    popup_indicators = [
                        'popup', 'modal', 'dialog', 'checkout', 'payment',
                        'paypal', 'auth', 'login', 'oauth', 'about:blank'
                    ]

                    # Check URL patterns for popup indicators
                    is_popup_url = any(indicator in new_url.lower() for indicator in popup_indicators)

                    # Small viewport typically indicates popup
                    is_small_viewport = viewport_size and (
                        viewport_size['width'] < 800 or viewport_size['height'] < 600
                    )

                    if is_popup_url or is_small_viewport:
                        window_type = 'popup'
                        new_window_detected.mark_popup_detected()
                        logger.info(f"🪟 Popup detected: {new_url} (viewport: {viewport_size})")
                    else:
                        window_type = 'new_tab'
                        new_window_detected.mark_new_tab_detected()
                        logger.info(f"🗂️ New tab detected: {new_url}")

                except Exception as detection_error:
                    logger.warning(f"⚠️ Window type detection failed: {detection_error}")
                    window_type = 'unknown'

            new_window_detected.update_state(len(stable_pages), window_type)

            # Heuristic: last page in the list is usually the most recently opened/focused
            new_handle = stable_pages[-1]

            # ✅ flag when multiple windows/tabs exist
            logger.info(f"🏁 {new_window_detected}")

            # Step 2: Validate current page state
            current_valid = current_page and (not current_page.is_closed())

            # Step 3: Decide if switch is needed
            if (not current_valid) or (new_handle != current_page):
                # Step 4: Pre-switch validation
                if new_handle.is_closed():
                    logger.info(f"⚠️ Target page closed during switch prep (attempt {attempt + 1})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.5)
                        continue
                    raise Exception(
                        message="Browser window closed during switch",
                        error_code="BROWSER_ERROR",
                        context={"issue": "target_page_closed", "attempt": attempt}
                    )

                # Step 5: Perform switch
                page = new_handle
                try:
                    await page.bring_to_front()
                except Exception:
                    # Some drivers/environments do not require this; continue.
                    pass

                # Step 6: Verify switch success using SPA-friendly wait
                try:
                    await wait_for_page_stable(page, timeout=10000)
                except Exception as eval_error:
                    logger.info(f"⚠️ Page not responsive after switch (attempt {attempt + 1}): {eval_error}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.5)
                        continue
                    raise

                # Step 7: Record successful switch
                page_name = extract_page_name(page.url)
                current_df = await record_page_object_details(
                    page_name, 'switch to window', '', '', '', '',
                    page, page, None, '', '', '',
                    str(page), current_df, test_step, new_window_detected
                )

                logger.info(f"✅ Switch successful: {page_name}")
                return page, current_df

            # No switch needed; still ensure page is stable enough
            try:
                await wait_for_page_stable(current_page, timeout=5000)
            except Exception:
                pass
            return current_page, current_df

        except Exception as e:
            logger.info(f"❌ Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
                continue
            else:
                # Final fallback
                logger.info(f"🆘 Using emergency fallback")
                available_pages = [p for p in context.pages if not p.is_closed()]
                if available_pages:
                    # Try to stabilize whichever we pick
                    try:
                        await wait_for_page_stable(available_pages[0], timeout=5000)
                    except Exception:
                        pass
                    return available_pages[0], current_df
                raise Exception(
                    message="No browser windows available",
                    error_code="BROWSER_ERROR",
                    context={"issue": "no_pages_available"}
                )

    return current_page, current_df

def is_ecs():
    return (
        "ECS_CONTAINER_METADATA_URI" in os.environ or
        "ECS_CONTAINER_METADATA_URI_V4" in os.environ or
        "AWS_EXECUTION_ENV" in os.environ
    )
# async def get_full_page_source(page):
#     """Get the complete HTML source of the current page and log it"""
#     try:
#         # Get the full HTML content
#         html_content = await page.content()
#
#         # Log the HTML content
#         logger.info("=== BEGIN PAGE SOURCE ===")
#         logger.info(html_content)
#         logger.info("=== END PAGE SOURCE ===")
#
#         return html_content
#
#     except Exception as e:
#         logger.error(f"Error getting page source: {e}")
#         return None

def is_local():
    return os.path.isfile("src/.env")

success_recording_zip = None

def _parse_resolution_string(res_str: str) -> "Optional[Tuple[int, int]]":
    """
    Parse resolution strings like '1280x720', '1280 X 720', '1280*720', '1280,720', or with spaces.
    Returns (width, height) as ints if valid; otherwise None.
    """
    try:
        if not isinstance(res_str, str):
            return None
        text = res_str.strip()
        # Extract two integers separated by x, X, *, comma, or whitespace
        import re as _re
        match = _re.search(r'(\d+)\s*[xX\*,\s]\s*(\d+)', text)
        if not match:
            return None
        width = int(match.group(1))
        height = int(match.group(2))
        # Sanity bounds
        if 300 <= width <= 5000 and 300 <= height <= 5000:
            return width, height
        return None
    except Exception:
        return None

async def crawl(file_content, i, jira_id, test_lst, scenario_dict,new_window_detected,current_script="" ,record_video: bool = False,generate_step_definitions=True, headless: bool = False, datasets=None):  # Made async

    # ✅ FIX: Preserve AWS secret cache between scenarios - they don't change within a test run
    # Only clear per-scenario state (responses, tokens, API configs) to prevent data leakage
    _cleanup_global_state(preserve_aws_cache=True)

    window_size_as_per_ratio = False
    current_df = pd.DataFrame()
    already_window_maximized = False
    explicit_viewport_applied = False
    stepDefTemplate = StepDefTemplate()
    chroma_connector.cleanup_old_contexts(2)
    reset_in_memory_api_state_for_run()
    # Track the current BDD step being executed for error reporting
    current_bdd_step = "Initialization"
    output = generate(file_content)
    if output.startswith("```json") and output.endswith("```"):
        output = output[7:-3]
    logger.info(output)
    output_json = json.loads(output)
    test_url = output_json['app_url']
    test_steps = output_json['steps']
    #subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1920x1080x24'])
    #os.environ['DISPLAY'] = ':99'

    # --- VIDEO (controlled by flag) ---
    __run_failed = False
    __video_dir = None
    __video_path = None
    __video_url = None
    session_id = f"{jira_id or i}-{uuid.uuid4().hex}"
    if record_video:
        __video_dir = _setup_failure_recording_dir("playwright-test", str(jira_id or i))
        folder_name = __video_dir.name
        __video_path = __video_dir / f"{folder_name}.webm"
        __video_url = _build_download_url(__video_path)
        # ✅ Register this video directory for screenshots
        register_video_run_dir(__video_dir)
        logger.info(f"📹 Registered video run directory for screenshots: {__video_dir}")

    if is_ecs() and not is_local():
        logger.info("Running in ECS container")
    else:
        logger.info("Not running in ECS container")

    # Only start Xvfb if headless is False (non-headless mode requires display)
    if is_ecs() and not is_local() and platform.system() == "Linux" and not headless:
        # Start Xvfb if not already running
        logger.info("Initiating videoDriver for non-headless mode***************************************")
        xvfb_process = subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1920x1080x24'])
        os.environ['DISPLAY'] = ':99'
        time.sleep(5)  # Give Xvfb some time to start
        logger.info("videoDriver initiation successful for non-headless mode***************************************")
    else:
        xvfb_process = None  # Initialize to None for headless mode or non-ECS environments
        if not headless:
            logger.info("Non-headless mode requested but not in ECS Linux environment - running without Xvfb")
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-notifications",
                    "--disable-popup-blocking",
                    "--disable-extensions",
                    "--disable-features=VizDisplayCompositor",
                    "--disable-web-security",
                    "--disable-features=TranslateUI",
                    "--disable-ipc-flooding-protection",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-default-apps",
                    "--disable-component-extensions-with-background-pages",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-client-side-phishing-detection",
                    "--disable-hang-monitor",
                    "--disable-prompt-on-repost",
                    "--disable-domain-reliability",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-field-trial-config"
                    "--disable-field-trial-config",
                    "--disable-infobars",
                    "--disable-default-browser-check",
                    "--disable-translate"
                ]
            )
            context = await browser.new_context(  # Added await
                viewport={"width": 1920, "height": 1080},
                permissions=[]  # Block notifications
            )

            temp_page = await context.new_page()
            default_ua = await temp_page.evaluate("() => navigator.userAgent")
            logger.info(f"Default User-Agent******: {default_ua}")
            await temp_page.close()
            await context.close()
            #custom_ua = default_ua + " 00a1dd6d90df844f5c7157c66a515f23"
            custom_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 LowerEnvString/9a045ab84c4610ffb459247df65a6377"
            logger.info(f"Custom User-Agent******: {custom_ua}")
            common_kwargs = dict(
                viewport={"width": 1920, "height": 1080},
                permissions=[],
                user_agent=custom_ua,
                extra_http_headers={
                    'DNT': '1',
                    'Sec-GPC': '1'
                },
                java_script_enabled=True,
                bypass_csp=True,
                ignore_https_errors=True
            )

            if record_video and __video_dir:
                common_kwargs.update({
                    "record_video_dir": str(__video_dir),
                    "record_video_size": {"width": 1920, "height": 1080},
                })
            context = await browser.new_context(**common_kwargs)
            context.set_default_timeout(60000)  # equivalent to driver.implicitly_wait(60)

            # Set up global handler for any new pages created in this context
            async def handle_new_page_creation(new_page):
                try:
                    logger.info(f"🆕 New page created: {new_page.url}")

                    # Wait a bit for page to initialize
                    await asyncio.sleep(0.5)

                    # Try to detect if this is a popup or new tab
                    window_type = 'unknown'
                    try:
                        # Get viewport size to help distinguish popup from new tab
                        viewport = new_page.viewport_size
                        page_url = new_page.url

                        # Common popup indicators
                        popup_indicators = [
                            'popup', 'modal', 'dialog', 'checkout', 'payment',
                            'paypal', 'auth', 'login', 'oauth', 'about:blank'
                        ]

                        # Check URL patterns for popup indicators
                        is_popup_url = any(indicator in page_url.lower() for indicator in popup_indicators)

                        # Small viewport typically indicates popup
                        is_small_viewport = viewport and (
                            viewport['width'] < 800 or viewport['height'] < 600
                        )

                        if is_popup_url or is_small_viewport:
                            window_type = 'popup'
                            logger.info(f"🪟 Detected popup window: {page_url} (viewport: {viewport})")
                        else:
                            window_type = 'new_tab'
                            logger.info(f"🗂️ Detected new tab: {page_url}")

                        # Update window state manager if available
                        if 'new_window_detected' in locals() or 'new_window_detected' in globals():
                            if window_type == 'popup':
                                new_window_detected.mark_popup_detected()
                            elif window_type == 'new_tab':
                                new_window_detected.mark_new_tab_detected()

                    except Exception as detection_error:
                        logger.warning(f"⚠️ Window type detection failed in handler: {detection_error}")
                        window_type = 'unknown'

                except Exception as handler_error:
                    logger.warning(f"⚠️ New page handler error: {handler_error}")

                # HTTP response logging commented out to reduce terminal clutter
                # async def handle_new_page_response(response):
                #     await log_http_error_response(response, "Page")
                #
                # new_page.on("response", handle_new_page_response)

            context.on("page", handle_new_page_creation)

            # Block cookie consent dialogs
            await context.add_init_script("""
                // Block OneTrust and similar cookie consent managers
                Object.defineProperty(window, 'OneTrust', { value: undefined, writable: false });
                Object.defineProperty(window, 'OptanonWrapper', { value: undefined, writable: false });
                Object.defineProperty(window, 'Optanon', { value: undefined, writable: false });
                
                // Hide common cookie banner selectors
                const style = document.createElement('style');
                style.textContent = `
                    #onetrust-consent-sdk,
                    #onetrust-banner-sdk,
                    .ot-sdk-container,
                    .cookie-banner,
                    .cookie-consent,
                    .gdpr-banner,
                    [id*="cookie"][id*="banner"],
                    [class*="cookie"][class*="banner"],
                    [id*="consent"][id*="banner"],
                    [class*="consent"][class*="banner"],
                    [class*="browser"][class*="recommendation"],
                    [class*="switch"][class*="chrome"],
                    div:has-text("For the best experience, please switch to Chrome"),
                    div:has-text("switch to Chrome"),
                    *:has-text("Chrome >") {
                        display: none !important;
                        visibility: hidden !important;
                        opacity: 0 !important;
                        height: 0 !important;
                        width: 0 !important;
                        z-index: -9999 !important;
                    }
                `;
                document.head.appendChild(style);
                
                // Remove elements containing Chrome recommendation text
                setTimeout(() => {
                    const elements = document.querySelectorAll('*');
                    elements.forEach(el => {
                        if (el.textContent && el.textContent.includes('For the best experience, please switch to Chrome')) {
                            el.style.display = 'none';
                        }
                    });
                }, 1000);
            """)

            page = await context.new_page()  # Added await

            # Main page response logging is handled by the global new page handler above

            # page.goto(test_url)  # commented out like original
            custom_ua_actual = await page.evaluate("() => navigator.userAgent")
            logger.info(f"Custom User-Agent******: {custom_ua_actual}")
            window_handle = None

            # ✅ Wait for DOM content to load
            await page.wait_for_load_state("domcontentloaded", timeout=10000)

            # ✅ Then wait for network to settle (best effort, non-blocking if it fails)
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass

            # Inject MutationObserver script
            await inject_mutation_observer(page)  # Added await

            context_key = str(uuid.uuid4())

            for k, test_step in enumerate(test_steps, 0):
                # Reset window detection for this step
                new_window_detected.reset_detection()

                time.sleep(3)  # Changed from time.sleep
                status = 0
                locator_reuse_failed = False  # Initialize for each step
                test_step['original_bdd_step'] = test_lst[k]
                # Update current step for error reporting
                current_bdd_step = test_lst[k] if k < len(test_lst) else f"Step {k+1}: {test_step.get('step', 'Unknown')}"
                page, current_df = await window_switch_handling(context, page, current_df, test_step,new_window_detected)

                # html_content = await get_full_page_source(page)
                # if html_content:
                #     logger.info("Full page source captured successfully")

                # Enterprise field injection from stored responses - MUST happen before locator reuse
                test_step['test_data'] = resolve_placeholder_in_test_data(test_step.get('test_data', ''))
                test_step['original_bdd_step'] = resolve_placeholder_in_test_data(test_step.get('original_bdd_step', ''))
                logger.info(f"🔍 DEBUG: Resolved test_data: {test_step['test_data']}")
                logger.info(f"🔍 DEBUG: Resolved original_bdd_step: {test_step['original_bdd_step']}")
                logger.info(f"🔍 DEBUG: Available stored responses: {list(_stored_responses.keys())}")
                logger.info(f"🔍 DEBUG: Available stored tokens: {list(_stored_tokens.keys())}")

                # Handle explicit viewport resolution step: 'browser is launched with "<resolution>" resolution'
                if not explicit_viewport_applied:
                    try:
                        bdd_lower = (test_step.get('original_bdd_step') or '').lower()
                        if 'launched with' in bdd_lower and 'resolution' in bdd_lower:
                            # Try extracting the resolution value either from quotes or inline
                            import re as _re
                            # Prefer quoted content if present
                            quoted = _re.search(r'"([^"]+)"', test_step.get('original_bdd_step') or '')
                            res_src = quoted.group(1) if quoted else (test_step.get('test_data') or '')
                            parsed = _parse_resolution_string(res_src)
                            if parsed:
                                width, height = parsed
                                await page.set_viewport_size({"width": width, "height": height})
                                explicit_viewport_applied = True
                                already_window_maximized = True  # Prevent default reset later
                                logger.info(f"✅ Applied explicit viewport from step: {width}x{height}")
                            else:
                                logger.info(f"⚠️ Could not parse resolution from step: {test_step.get('original_bdd_step')}")
                            # This step is viewport-only; continue to next step
                            continue
                    except Exception as _e:
                        logger.info(f"⚠️ Error applying explicit viewport: {_e}")
                        # Proceed without failing the run

                handled, current_df = await reuse_locator_if_possible(
                    page, test_step, current_df, new_window_detected,
                    extract_page_name, record_page_object_details
                )

                # 🔧 Track if locator reuse failed so we can update DB with new working locator
                locator_reuse_failed = not handled and test_step.get('unique_id')
                logger.info(f"🔧 REUSE-TRACKING | handled: {handled}, unique_id: {test_step.get('unique_id')}, locator_reuse_failed: {locator_reuse_failed}")

                if handled:
                    logger.info(f"🔍 DB-REUSE: SUCCESS ✅ | Skipping discovery")
                    continue  # skip discovery

                # 👇 provenance marker for all rows produced by the JS crawl path of THIS step
                test_step["generated_by"] = "js_crawl"

                step_text = str(test_step['step']).lower().strip()
                original_bdd_step = str(test_step.get('original_bdd_step', ''))

                # Handle mock API steps - use existing locator system
                if step_text == 'mock-api':
                    logger.info(f"🎭 MOCK API STEP: {original_bdd_step}")

                    button_text = test_step.get('test_data', '')
                    mock_file = test_step.get('condition', '')

                    # Get mock config
                    request_bodies = get_request_bodies()
                    mock_key = mock_file.replace('.json', '')
                    mock_config = {}
                    if request_bodies and isinstance(request_bodies, dict):
                        mock_config = request_bodies.get(mock_key, {})

                    if not mock_config:
                        logger.info(f"❌ No mock configuration found for: {mock_file}")
                        continue

                    try:
                        # Set up method-specific API mocking using MockAPIInterceptor
                        mock_interceptor = MockAPIInterceptor()

                        # Get all route configurations (supports multiple routes)
                        route_configs = mock_interceptor.get_route_configs(mock_config)

                        # Register handler for each route pattern
                        for route_pattern, method_configs in route_configs:
                            handler = mock_interceptor.create_route_handler(method_configs)
                            await page.route(route_pattern, handler)

                            methods = list(method_configs.keys())
                            logger.info(f"✅ Mock route handler set up for pattern: {route_pattern} {methods}")

                        # Transform to regular click step and let existing system handle it
                        test_step['step'] = f'click-{button_text}'
                        step_text = 'click'
                        test_step['mock_delay'] = True  # Flag for post-click delay
                        logger.info(f"🎭 Transformed to click step: {step_text}")

                        # Fall through to existing system for button click processing

                    except Exception as e:
                        logger.info(f"❌ Mock setup failed: {e}")
                        import traceback
                        logger.info(f"📋 Traceback: {traceback.format_exc()}")
                        continue

                # Handle API-related steps (skip if this is a transformed mock step)
                if step_text in ['add-header', 'add-requestbody'] and test_step.get('generated_by') != 'mock_api':
                    service_name = test_step.get('condition', '').lower().strip()

                    # Initialize service config if not exists
                    if service_name not in _pending_api_configs:
                        _pending_api_configs[service_name] = {'headers': {}, 'body_updates': {}}

                    if step_text == 'add-header':
                        header_data = test_step.get('test_data', '')

                        # Handle different header formats
                        if ':' in header_data:
                            # "Authorization: Bearer {token}" format
                            key, value = header_data.split(':', 1)
                            header_key = key.strip()
                            header_value = value.strip()
                        elif header_data.startswith('Bearer '):
                            # "Bearer {token}" format
                            header_key = 'Authorization'
                            header_value = header_data
                        else:
                            # Plain token "{token}" format - add Authorization Bearer prefix
                            header_key = 'Authorization'
                            header_value = f'Bearer {header_data}'

                        _pending_api_configs[service_name]['headers'][header_key] = header_value
                        logger.info(f"✅ Stored header: {header_key}: {header_value} for service: {service_name}")

                    elif step_text == 'add-requestbody':
                        # Parse body: "loginId: {accountResponse.loginId}" -> {"loginId": "{accountResponse.loginId}"}
                        body_data = test_step.get('test_data', '')
                        if ':' in body_data:
                            key, value = body_data.split(':', 1)
                            _pending_api_configs[service_name]['body_updates'][key.strip()] = value.strip()
                        logger.info(f"✅ Stored request body: {body_data} for service: {service_name}")
                        logger.info(f"🔍 DEBUG: Parsed body key='{key.strip()}', value='{value.strip()}'")
                        logger.info(f"🔍 DEBUG: Body updates now: {_pending_api_configs[service_name]['body_updates']}")
                        logger.info(f"🔍 DEBUG: Config will be applied when API executes on STORE-RESPONSE step")

                    # Record the API configuration step
                    page_name = extract_page_name(page.url) if page.url else 'API'
                    current_df = await record_page_object_details(page_name, step_text.upper(),
                                                                  test_step.get('condition', ''),
                                                                  test_step.get('test_data', ''), '', '',
                                                                  page, page, None, '', '', '', '', current_df, test_step,
                                                                  new_window_detected)
                    continue

                # Handle gift card step - check step text, test_data, and parsed step
                step_combined = step_text + ' ' + str(test_step.get('test_data', ''))
                parsed_step = str(test_step.get('step', '')).lower()
                test_data_str = str(test_step.get('test_data', ''))
                original_bdd_step = str(test_step.get('original_bdd_step', '')).lower()
                logger.info(
                    f"DEBUG: step_text='{step_text}', parsed_step='{parsed_step}', test_data='{test_data_str}', step_combined='{step_combined}'")
                logger.info(f"DEBUG: original_bdd_step='{original_bdd_step}'")

                # Check for gift card patterns - case insensitive
                step_combined_lower = step_combined.lower()
                parsed_step_lower = parsed_step.lower()
                test_data_lower = test_data_str.lower()

                is_gift_card_step = (
                    ('giftcard' in parsed_step.lower() and '$' in test_data_str)
                )

                # Enterprise POST API processing
                post_api_patterns = [
                    'posts to', 'user posts to', 'using configuration from'
                ]
                if any(pattern in parsed_step.lower() for pattern in post_api_patterns) or 'post-api' in step_text:
                    logger.info(f"🚀 ENTERPRISE POST API: {parsed_step}")
                    logger.info(f"🚀 STEP TEXT: {step_text}")
                    logger.info(f"🚀 TEST DATA: {test_step.get('test_data', '')}")

                    service_name = test_step.get('test_data', '').lower().strip()

                    # Check if this is followed by ADD-HEADER or ADD-REQUESTBODY steps
                    next_steps_are_config = False
                    for next_idx in range(k + 1, min(k + 3, len(test_steps))):
                        next_step = test_steps[next_idx]
                        next_step_text = str(next_step.get('step', '')).lower()
                        if next_step_text in ['add-header', 'add-requestbody']:
                            next_condition = next_step.get('condition', '').lower().strip()
                            if service_name in next_condition or next_condition in service_name:
                                next_steps_are_config = True
                                break
                        elif next_step_text not in ['store-response', 'store-field']:
                            break  # Stop checking if we hit a non-config step

                    if next_steps_are_config:
                        logger.info(f"🔄 DEFERRING API CALL: Found config steps following POST-API for {service_name}")
                        # Store the API call for later execution
                        _deferred_api_calls[service_name] = {
                            'step': test_step,
                            'original_bdd_step': original_bdd_step,
                            'parsed_step': parsed_step,
                            'k': k
                        }
                        continue

                    # Execute immediately if no config steps follow
                    current_df = await execute_api_call(test_step, original_bdd_step, parsed_step, service_name, page,
                                                        current_df,new_window_detected)
                    continue

                # Handle STORE-RESPONSE step and storage patterns
                if step_text == 'store-response' or 'stores response as' in original_bdd_step:
                    logger.info(f"🔍 STORE RESPONSE STEP: {original_bdd_step}")

                    # Check and execute any deferred API calls first
                    current_df = await check_and_execute_deferred_api('', test_steps, k, page, current_df,new_window_detected)

                    # Store the response with the variable name
                    if step_text == 'store-response':
                        var_name = test_step.get('test_data', '')
                    else:
                        response_match = re.search(r'stores response as "([^"]+)"', original_bdd_step, re.IGNORECASE)
                        var_name = response_match.group(1) if response_match else ''

                    if var_name and 'last_response' in _stored_responses:
                        _stored_responses[var_name] = _stored_responses['last_response']
                        logger.info(f"✅ STORED RESPONSE AS: {var_name}")
                        logger.info(f"📊 ALL STORED RESPONSES: {list(_stored_responses.keys())}")

                    # Record STORE-RESPONSE step for TypeScript generation
                    page_name = extract_page_name(page.url) if page.url else 'API'
                    current_df = await record_page_object_details(page_name, 'STORE-RESPONSE',
                                                                  var_name or 'response',
                                                                  f"STORE: {original_bdd_step}", '', '',
                                                                  page, page, None, '',
                                                                  test_step.get('condition', ''), '', '', current_df, test_step,
                                                                  new_window_detected)
                    continue

                # Handle field verification patterns (only for stored response fields with dot notation)
                if 'verifies "' in original_bdd_step and 'contains' in original_bdd_step:
                    field_match = re.search(r'verifies "([^"]+)" contains "([^"]+)"', original_bdd_step, re.IGNORECASE)
                    if field_match:
                        field_path, expected_value = field_match.groups()
                        # Only process if it's a stored response field (contains dot notation)
                        if '.' in field_path:
                            logger.info(f"🔍 FIELD VERIFICATION: {original_bdd_step}")
                            parts = field_path.split('.')
                            response_name = parts[0]

                            # Case-insensitive response lookup
                            found_response = None
                            for stored_key in _stored_responses.keys():
                                if stored_key.lower() == response_name.lower():
                                    found_response = stored_key
                                    break

                            if found_response:
                                value = _stored_responses[found_response]
                                for part in parts[1:]:
                                    if isinstance(value, dict) and part in value:
                                        value = value[part]
                                    else:
                                        value = None
                                        break
                                if value is not None and expected_value in str(value):
                                    logger.info(f"✅ FIELD VERIFICATION PASSED: {field_path} contains {expected_value}")
                                else:
                                    logger.info(
                                        f"❌ FIELD VERIFICATION FAILED: {field_path} = {value}, expected to contain {expected_value}")
                            else:
                                logger.info(f"❌ RESPONSE NOT FOUND: {response_name}")
                            continue

                elif 'stores "' in original_bdd_step and 'from response as' in original_bdd_step:
                    logger.info(f"🔍 DYNAMIC STORE FIELD: {original_bdd_step}")
                    field_match = re.search(r'stores "([^"]+)" from response as "([^"]+)"', original_bdd_step,
                                            re.IGNORECASE)
                    if field_match and 'last_response' in _stored_responses:
                        field_path, var_name = field_match.groups()
                        last_response = _stored_responses['last_response']
                        value = last_response
                        for part in field_path.split('.'):
                            if isinstance(value, dict) and part in value:
                                value = value[part]
                            else:
                                value = None
                                break
                        if value is not None:
                            _stored_tokens[var_name] = value
                            logger.info(f"✅ STORED FIELD {field_path} AS: {var_name} = {value}")

                        # Record STORE-FIELD step for TypeScript generation
                        page_name = extract_page_name(page.url) if page.url else 'API'
                        current_df = await record_page_object_details(page_name, 'STORE-FIELD',
                                                                      f"{field_path} → {var_name}",
                                                                      f"STORE: {original_bdd_step}", '', '',
                                                                      page, page, None, '',
                                                                      test_step.get('condition', ''), '', '', current_df, test_step,
                                                                      new_window_detected)
                    continue

                # Enterprise GET API processing - BEFORE navigation to avoid timeouts
                get_api_patterns = [
                    'get-api', 'get api', 'send a get request', 'i send a get request'
                ]
                if any(pattern in parsed_step.lower() for pattern in get_api_patterns):
                    logger.info(f"🚀 ENTERPRISE GET API: {parsed_step}")

                    service_name = 'get_api'  # Default service name for GET APIs

                    # Check if next step is "stores response as" - if so, defer execution
                    next_steps_have_store = False
                    for next_idx in range(k + 1, min(k + 3, len(test_steps))):
                        next_step = test_steps[next_idx]
                        next_original_bdd = str(next_step.get('original_bdd_step', '')).lower()
                        if "stores response as" in next_original_bdd:
                            next_steps_have_store = True
                            break
                        elif next_original_bdd.strip() and "stores" not in next_original_bdd:
                            break  # Stop checking if we hit a non-store step

                    if next_steps_have_store:
                        logger.info(f"🔄 DEFERRING GET API CALL: Found stores response step following GET API")
                        # Store the API call for later execution
                        _deferred_api_calls[service_name] = {
                            'step': test_step,
                            'original_bdd_step': original_bdd_step,
                            'parsed_step': parsed_step,
                            'k': k,
                            'api_type': 'GET'
                        }
                        continue

                    # Execute immediately if no store step follows
                    current_df = await execute_get_api_call(test_step, original_bdd_step, parsed_step, page, current_df,new_window_detected)
                    continue

                # Handle status validation for GET API
                if 'response status should be' in original_bdd_step or 'the response status should be' in original_bdd_step:
                    logger.info(f"🔍 GET API STATUS VALIDATION: {original_bdd_step}")
                    # Just record for TypeScript generation - actual validation happens at runtime
                    current_df = await record_page_object_details("API_Call", 'VALIDATE-STATUS',
                                                                  test_step.get('test_data', ''),
                                                                  f"Status Validation: {parsed_step}", '', '',
                                                                  page, page, None, '',
                                                                  test_step.get('condition', ''), '', '', current_df,
                                                                  test_step, new_window_detected)
                    continue
                validation_patterns = [
                    'verifies response status', 'response status is'
                ]
                if any(pattern in parsed_step.lower() for pattern in validation_patterns) or 'validate-status' in step_text:
                    logger.info(f"🔍 ENTERPRISE VALIDATION: {parsed_step}")

                    # Legacy storage patterns are now handled by InputProcessor as STORE-RESPONSE and STORE-FIELD steps

                    # Record validation step
                    current_df = await record_page_object_details("API_Validation", 'VALIDATE',
                                                                  parsed_step, f"Validation: {parsed_step}", '', '',
                                                                  page, page, None, '', '', '', '', current_df, test_step,
                                                                  new_window_detected)
                    continue

                if is_gift_card_step:
                    logger.info(f"DEBUG: Gift card step detected!")
                    try:
                        # Extract amount from combined text or test data
                        amount_match = re.search(r'\$([0-9]+(?:\.[0-9]{2})?)', step_combined + test_data_str)
                        amount = float(amount_match.group(1)) if amount_match else 20.0
                        logger.info(f"Executing gift card flow for amount: ${amount}")

                        # Execute the gift card flow with dynamic amount
                        page = await handle_gift_card_flow(page, amount=amount)

                        # Record the action
                        content_for_recording=page
                        page_name = extract_page_name(page.url)
                        current_df = await record_page_object_details(page_name, 'create', 'new user',
                                                                      '', '', '', content_for_recording,
                                                                      page,
                                                                      None, '',
                                                                      '', '', '', current_df, test_step,
                                                                      new_window_detected)
                        continue  # Skip to next step after handling gift card
                    except Exception as e:
                        logger.info(f"Failed to execute gift card flow: {e}")
                        continue

                # Handle cookie setting step
                if (step_text == 'load-cookies-json'):
                    logger.info(f"🍪 Setting authentication cookies...")

                    # Extract JSON filename from step text
                    json_match = re.search(r'(\w+\.json)', original_bdd_step)
                    json_file_path = test_step.get('test_data', '').strip()

                    # Check if it's a stored response (like sessionResponse)
                    response_match = re.search(r'with (\w+Response|\w+) as cookie', original_bdd_step)

                    if json_match:
                        json_filename = json_match.group(1)
                        config_key = json_filename.replace('.json', '')

                        # Get cookie data from request_bodies (already loaded in memory)
                        from .request_bodies_manager import get_request_bodies
                        request_bodies = get_request_bodies()
                        logger.info(f"🔍 DEBUG: request_bodies type: {type(request_bodies)}")
                        logger.info(f"🔍 DEBUG: request_bodies is None: {request_bodies is None}")
                        logger.info(f"🔍 DEBUG: request_bodies keys: {list(request_bodies.keys()) if request_bodies else 'None'}")

                        # Case-insensitive lookup for config key
                        found_key = None
                        if request_bodies:
                            for key in request_bodies.keys():
                                if key.lower() == config_key.lower():
                                    found_key = key
                                    break

                        if found_key:
                            cookie_data = request_bodies[found_key]
                            logger.info(f"🔍 Loading cookies from request_bodies: {found_key}")
                            logger.info(f"🔍 Cookie data type: {type(cookie_data)}")
                            logger.info(f"🔍 Cookie data keys: {list(cookie_data.keys()) if isinstance(cookie_data, dict) else 'Not a dict'}")

                            # Navigate to a base domain first to set cookies
                            await page.goto('https://cph-stg.chewy.net', wait_until='domcontentloaded')

                            # Load cookies directly from the data
                            try:
                                from .cookie_handler import load_cookies_from_data
                                success = await load_cookies_from_data(context, cookie_data)
                            except ImportError as e:
                                logger.error(f"❌ Cookie handler import failed: {e}")
                                success = False
                            except Exception as e:
                                logger.error(f"❌ Cookie loading failed: {e}")
                                success = False

                            if success:
                                logger.info(f"✅ Cookies loaded successfully from {found_key}")
                                page_name = extract_page_name(page.url) if page.url else 'Authentication'
                                current_df = await record_page_object_details(page_name, 'LOAD-COOKIES-JSON', found_key,
                                                                              '', '', '', page, page, None, '', '', '', '',
                                                                              current_df, test_step, new_window_detected)
                            else:
                                logger.info(f"❌ Failed to load cookies from {found_key}")
                        else:
                            logger.info(f"❌ Cookie data not found in request_bodies: {config_key}")
                            logger.info(f"💡 Available keys: {list(request_bodies.keys()) if request_bodies else 'None'}")
                    elif response_match:
                        # Handle API response cookies (sessionResponse, etc.)
                        response_name = response_match.group(1)
                        session_data = None

                        # Case-insensitive lookup for stored responses
                        for key in _stored_responses.keys():
                            if key.lower() == response_name.lower():
                                session_data = _stored_responses[key]
                                break

                        if session_data and isinstance(session_data, dict) and 'idToken' in session_data:
                            await page.goto('https://www-stg.chewy.net', wait_until='domcontentloaded')
                            await context.add_cookies([
                                {'name': 'ticket', 'value': session_data['idToken'], 'domain': '.www-stg.chewy.net', 'path': '/'},
                                {'name': 'asid', 'value': session_data['sessionId'], 'domain': '.www-stg.chewy.net', 'path': '/'},
                                {'name': 'at', 'value': session_data['authToken'], 'domain': '.www-stg.chewy.net', 'path': '/'},
                                {'name': 'lbGroup', 'value': 'use2', 'domain': '.www-stg.chewy.net', 'path': '/'}
                            ])
                            logger.info(f"✅ Authentication cookies set from {response_name}")

                            page_name = extract_page_name(page.url) if page.url else 'Authentication'
                            current_df = await record_page_object_details(page_name, 'CREATE-USER-COOKIE', response_name,
                                                                          '', '', '', page, page, None, '', '', '', '',
                                                                          current_df, test_step, new_window_detected)
                        else:
                            logger.info(f"❌ Response {response_name} invalid or missing required fields")
                    continue

                if str(test_step['step']).lower().strip().startswith(("open", "navigate", "go to", "launch")):

                    page, already_window_maximized, current_df = await handle_navigation_step(
                        test_step,
                        page,
                        already_window_maximized,
                        current_df,
                        new_window_detected,
                        explicit_viewport_applied
                    )
                    await asyncio.sleep(3)  # Changed from time.sleep
                    continue

                max_attempts = 2
                attempt = 0
                steps_implemented = False
                while not steps_implemented and attempt < max_attempts:
                    stored_exception = None
                    current_url = page.url
                    ready = False
                    await comprehensive_wait(page)
                    try:
                        try:
                            iframe_details = await get_iframe_details(page)  # Added await
                            if iframe_details is None:
                                logger.info(f"Details extraction returned None")
                        except Exception as e:
                            logger.info(f"An error occurred during iframe_details extraction: {e}")
                        try:
                            logger.info("🔍 DEBUG: About to call inject_html_context_retrieval")
                            context_details = await inject_html_context_retrieval(page)  # Added await
                            if context_details is None:
                                logger.info(f"Details extraction returned None during main frame context")
                        except Exception as e:
                            logger.info(f"An error occurred during details extraction: {e}")
                        combined_context = iframe_details + context_details
                        existing_context = chroma_connector.get_context_by_id(context_key)
                        if existing_context:
                            if json.dumps(existing_context) != json.dumps(combined_context):   # Compare existing context with the new context
                                logger.info(f"Updating context for : {context_key} ")
                                chroma_connector.update_context_in_chromadb(combined_context, context_key)  # Update the context in ChromaDB
                        else:
                            logger.info(f"Storing context for ID: {context_key}")
                            chroma_connector.store_context_in_chromadb(combined_context, context_key)  # Store context in ChromaDB
                        import re as regex_module
                        pattern = regex_module.compile(r'â€¢+')
                        if pattern.search(test_step['step']):
                            test_step['step'] = ftfy.fix_text(test_step['step'])
                        if "Under" in test_step['step']:
                            await asyncio.sleep(3)  # Changed from time.sleep
                            act_obj = test_step['step']
                            words = act_obj.split("-", 1)
                            under_obj = words[1]
                            # Split the string based on "Under" keyword
                            parts = under_obj.split("Under")
                            # Remove leading/trailing whitespace from each part
                            object_list = [part.strip() for part in parts]
                            xpath_lists = {}

                            for u_obj in object_list:
                                query = words[0] + u_obj + ": " + str(test_step['test_data'])
                                k = 1  # Number of results to retrieve
                                filtered_elements, docs_with_similarity, threshold = chroma_connector.retrieval_html_context(
                                    query=query,
                                    context_key=context_key)
                                elements = json.loads(filtered_elements)
                                xpaths = [element["xpath"] for element in elements if u_obj in element["element"]]
                                logger.info(xpaths)
                                xpath_lists[u_obj] = xpaths
                            final_xpath = await find_nearest_first_xpath(xpath_lists, page)  # Added await
                            filtered_elements = str({test_step['step']: [final_xpath]})
                            if words[0].lower() == 'select':
                                query = words[0]
                                test_data_filtered_elements, docs_with_similarity, threshold = chroma_connector.retrieval_html_context(
                                    query=query,
                                    context_key=context_key)
                                filtered_elements = test_data_filtered_elements + filtered_elements

                            # logger.info(f"under element filtered_elements:", filtered_elements)
                            response = llm.send_request(
                                steps_detection_prompt,
                                ["test_step", "context"],
                                {"test_step": test_step['step'] + ": " + str(test_step['test_data']),
                                "context": json.dumps(filtered_elements)},
                                0.25
                            )
                            if response.startswith("```json") and response.endswith("```"):
                                response = response[7:-3]  # Remove the '''json prefix and the ending '''
                            # logger.info(fresponse)
                            response = json.loads(response)
                            logger.info(json.dumps(response, indent=4))
                            # Enterprise field injection into response steps
                            for resp_step in response["steps"]:
                                if 'text' in resp_step:
                                    # Only remove specific patterns that indicate LLM error, not legitimate text
                                    original_text = resp_step['text']

                                    # Pattern 1: "test_data:" prefix (LLM formatting error)
                                    if original_text.startswith('test_data:'):
                                        resp_step['text'] = original_text[10:].strip()
                                        logger.info(f"🔧 Removed 'test_data:' prefix: '{original_text}' → '{resp_step['text']}'")

                                    # Pattern 2: "test_data " prefix (LLM formatting error with space, but not followed by =)
                                    elif original_text.startswith('test_data ') and not original_text.startswith('test_data ='):
                                        resp_step['text'] = original_text[10:].strip()
                                        logger.info(f"🔧 Removed 'test_data ' prefix: '{original_text}' → '{resp_step['text']}'")

                                    # Pattern 3: "test_data=" prefix (LLM assignment-style error, no space before =)
                                    elif original_text.startswith('test_data='):
                                        resp_step['text'] = original_text[10:].strip()
                                        logger.info(f"🔧 Removed 'test_data=' prefix: '{original_text}' → '{resp_step['text']}'")

                                    # Pattern 4: Standalone "test_data" (entire field is just "test_data")
                                    elif original_text.strip() == 'test_data':
                                        resp_step['text'] = ''
                                        logger.info(f"🔧 Cleared standalone 'test_data': '{original_text}' → empty string")

                                    # Do NOT modify text that contains "test_data" as part of legitimate content
                                    # e.g., "test_data_validation", "my_test_data_file.txt", "test_data =value" (space before =)

                                    resp_step['text'] = resolve_placeholder_in_test_data(resp_step['text'])
                            status, steps_performed, steps_not_performed, current_df = await perform(page,
                                                                                                    response["steps"],
                                                                                                    chroma_connector,
                                                                                                    context_key, current_df,
                                                                                                    test_step,
                                                                                                    new_window_detected)
                        else:
                            # Resolve variables in step before creating query
                            resolved_step = resolve_placeholder_in_test_data(test_step['step'])
                            query = resolved_step + ": " + str(test_step['test_data'])
                            logger.info(f"🔍 DEBUG: Original step: {test_step['step']}")
                            logger.info(f"🔍 DEBUG: Resolved step: {resolved_step}")
                            logger.info(f"🔍 DEBUG: Query for ChromaDB retrieval: {query}")
                            logger.info(f"🔍 DEBUG: Context key for retrieval: {context_key}")

                            k = 1  # Number of results to retrieve
                            filtered_elements, docs_with_similarity, threshold = chroma_connector.retrieval_html_context(
                                query=query,
                                context_key=context_key)  # Retrieve and filter relevant context from ChromaDB

                            logger.info(f"🔍 DEBUG: Retrieved docs count: {len(docs_with_similarity) if docs_with_similarity else 0}")
                            logger.info(f"🔍 DEBUG: Similarity threshold: {threshold}")

                            try:
                                parsed_elements = json.loads(filtered_elements) if isinstance(filtered_elements, str) else filtered_elements
                                logger.info(f"🔍 DEBUG: Parsed elements count: {len(parsed_elements) if parsed_elements else 0}")

                                # Check for customer ID elements in filtered results
                                customer_id_filtered = []
                                for element in parsed_elements:
                                    if '332241631' in str(element) or '332241510' in str(element):
                                        customer_id_filtered.append(element)
                                logger.info(f"🔍 DEBUG: Found {len(customer_id_filtered)} elements containing customer IDs in FILTERED results")
                                for idx, elem in enumerate(customer_id_filtered):
                                    logger.info(f"🔍 DEBUG: Customer ID filtered element {idx+1}: {elem}")

                                logger.info(f"This is filtered element:\n{json.dumps(parsed_elements, indent=2)}")
                            except (json.JSONDecodeError, TypeError):
                                logger.info(f"This is filtered element: {filtered_elements}")

                            # Use resolved step for element name extraction to match LLM comparison
                            element_name = resolved_step.split("-", 1)[1]
                            action = resolved_step.split("-", 1)[0]
                            if action.lower() in ["select", "enter", "populate"]:
                                response = llm.send_request(
                                    steps_detection_prompt,
                                    ["test_step", "context"],
                                    {"test_step": "Action is:" + str(action) + " WebElement object is:" + str(
                                        element_name) + " test_data:" + str(test_step['test_data']),
                                    "context": json.dumps(filtered_elements)},
                                    0.25
                                )

                            else:
                                response = llm.send_request(
                                    steps_detection_prompt,
                                    ["test_step", "context"],
                                    {"test_step": "Action is:" + str(action) + "WebElement object is:" + str(element_name),
                                    "context": json.dumps(filtered_elements)},
                                    0.25
                                )
                            if response.startswith("```json") and response.endswith("```"):
                                response = response[7:-3]  # Remove the '''json prefix and the ending '''
                            logger.info(response)
                            response = json.loads(response)
                            for step in response["steps"]:
                                if step['action'] != "switch_to_iframe":
                                    act = resolved_step.split("-", 1)[0]
                                    if act.lower() in ["populate", "enter"]:
                                        act = act.lower().replace("populate", "enter")
                                        step['action'] = step['action'].lower().replace("populate", "enter")
                                    # 🔧 FIX: Normalize action names for comparison
                                    step_action_normalized = step['action'].lower().replace('webelement', '').replace('element', '')
                                    act_normalized = act.lower().replace('webelement', '').replace('element', '')
                                    
                                    if step_action_normalized == act_normalized:
                                        element_name = resolved_step.split("-", 1)[1]
                                        # element_name = re.sub(r'(radio\s*)?button', '', element_name, flags=re.IGNORECASE)
                                        element_name = regex_module.sub(r'(radio\s*)?button', '', element_name, flags=regex_module.IGNORECASE)
                                        element_name_normalized = element_name.lower().replace('-', ' ').replace('_', ' ')
                                        step_element_normalized = step['element'].lower().replace('-', ' ').replace('_', ' ')
                                        similarity_score = levenshtein_similarity(element_name, step['element']) * 100
                                        
                                        # 🔧 FIX: For click actions with numeric IDs, check if test_data matches element
                                        test_data_match = False
                                        if act.lower() == 'click' and test_step.get('test_data'):
                                            test_data_str = str(test_step['test_data']).strip()
                                            step_element_str = str(step.get('element', '')).strip()
                                            if test_data_str == step_element_str:
                                                test_data_match = True
                                                logger.info(f"🔧 MATCH: test_data '{test_data_str}' matches LLM element '{step_element_str}'")
                                        
                                        # Determine if the LLM element matches a generic intent (e.g. numeric or number patterns)
                                        match_ok = (
                                                similarity_score > 80
                                                or act.lower() in ['verify', 'select']
                                                or _matches_intent_pattern(element_name, step.get('element', ''))
                                                or test_data_match  # 🔧 NEW: Accept match if test_data matches element
                                        )
                                        if match_ok:
                                            step['condition'] = test_step['condition']
                                            words_to_check = ['not displayed', 'is not displayed','not visible','is not visible']
                                            if any(word in test_step['condition'].lower() for word in
                                                words_to_check) and act.lower() == 'verify':
                                                if step['element'] != element_name:
                                                    step['element'] = element_name
                                                    step["xpath"] = None
                                                    step['condition'] = test_step['condition']
                                            else:
                                                # When verifying, also consider intent-based matching in the negative check
                                                if not (similarity_score > 80 or _matches_intent_pattern(element_name,step.get('element',''))) and act.lower() == 'verify':
                                                    logger.info(f"Provided Input element and response from LLM doesn't match")
                                                    logger.info(
                                                        f"Input element provided: {test_step} and Response from LLM: {response['steps']}")
                                                    raise TypeError(
                                                        "Provided Input element and response from LLM doesn't match")

                                                # Enterprise field injection into response steps
                                                for resp_step in response["steps"]:
                                                    if 'text' in resp_step:
                                                        resp_step['text'] = resolve_placeholder_in_test_data(resp_step['text'])

                                            logger.info(f"🚀 EXECUTING STEPS: {response['steps']}")
                                            logger.info(
                                                f"🚀 FINAL STEP TEXT VALUES: {[step.get('text', 'NO TEXT') for step in response['steps']]}")
                                            status, steps_performed, steps_not_performed, current_df = await perform(page,
                                                                                                                    response[
                                                                                                                        "steps"],
                                                                                                                    chroma_connector,
                                                                                                                    context_key,
                                                                                                                    current_df,
                                                                                                                    test_step,
                                                                                                                    new_window_detected)
                                        else:
                                            logger.info(f"Provided Input element and response from LLM doesn't match")
                                            logger.info(
                                                f"Input element provided: {test_step} and Response from LLM: {response['steps']}")
                                            raise Exception(
                                                "Element matching failed between input and LLM response"
                                            )
                                            # sys.exit()

                                    else:
                                        logger.info(f"Provided Input action and response from LLM doesn't match")
                                        logger.info(
                                            f"Input element provided: {test_step} and Response from LLM: {response['steps']}")
                                        raise Exception(
                                            "Action matching failed between input and LLM response"
                                        )
                        if status == -1:
                            # Query the vector database for elements related to steps not performed
                            query = steps_not_performed
                            filtered_elements, docs_with_similarity, threshold = chroma_connector.retrieval_html_context(
                                query=query, context_key=context_key)
                            logger.info(f"Filtered elements for not performed steps: {filtered_elements}")
                            response = llm.send_request(

                                steps_detection_prompt,
                                ["test_step", "context"],
                                {"test_step": query,
                                "context": json.dumps(filtered_elements)},
                                0.25
                            )
                            if response.startswith("```json") and response.endswith("```"):
                                response = response[7:-3]  # Remove the '''json prefix and the ending '''
                            response = json.loads(response)
                            logger.info(json.dumps(response, indent=4))

                            for step in response["steps"]:
                                if step['action'] != "switch_to_iframe":
                                    # Resolve variables for element matching validation in retry logic
                                    resolved_step_retry = resolve_placeholder_in_test_data(test_step['step'])
                                    act = resolved_step_retry.split("-", 1)[0]
                                    if act.lower() in ["populate", "enter"]:
                                        act = act.lower().replace("populate", "enter")
                                        step['action'] = step['action'].lower().replace("populate", "enter")
                                    # 🔧 FIX: Normalize action names for comparison (retry logic)
                                    step_action_normalized_retry = step['action'].lower().replace('webelement', '').replace('element', '')
                                    act_normalized_retry = act.lower().replace('webelement', '').replace('element', '')
                                    
                                    if step_action_normalized_retry == act_normalized_retry:
                                        element_name = resolved_step_retry.split("-")[1]
                                        # if "Under" in element_name:
                                        # element_name = re.sub(r'(radio\s*)?button', '', element_name, flags=re.IGNORECASE)
                                        element_name = regex_module.sub(r'(radio\s*)?button', '', element_name, flags=regex_module.IGNORECASE)
                                        # Compute similarity and use intent-based matching for deferred steps (status == -1)
                                        similarity_score_retry = levenshtein_similarity(element_name,
                                                                                        step['element']) * 100
                                        
                                        # 🔧 FIX: For click actions with numeric IDs, check if test_data matches element (retry logic)
                                        test_data_match_retry = False
                                        if act.lower() == 'click' and test_step.get('test_data'):
                                            test_data_str = str(test_step['test_data']).strip()
                                            step_element_str = str(step.get('element', '')).strip()
                                            if test_data_str == step_element_str:
                                                test_data_match_retry = True
                                                logger.info(f"🔧 RETRY-MATCH: test_data '{test_data_str}' matches LLM element '{step_element_str}'")
                                        
                                        match_ok = (
                                                similarity_score_retry > 80
                                                or act.lower() == 'verify'
                                                or _matches_intent_pattern(element_name, step.get('element', ''))
                                                or test_data_match_retry  # 🔧 NEW: Accept match if test_data matches element
                                        )
                                        if match_ok:
                                            step['condition'] = test_step['condition']
                                            words_to_check = ['not displayed']
                                            if any(word in test_step['condition'].lower() for word in
                                                words_to_check) and act.lower() == 'verify':
                                                if step['element'] != element_name:
                                                    step['element'] = element_name
                                                    step["xpath"] = None
                                                    step['condition'] = test_step['condition']
                                            else:
                                                # When verifying, also consider intent-based matching in the negative check
                                                if not (similarity_score_retry > 80 or _matches_intent_pattern(
                                                        element_name,
                                                        step.get('element', ''))) and act.lower() == 'verify':
                                                    logger.info(f"Provided Input element and response from LLM doesn't match")
                                                    logger.info(
                                                        f"Input element provided: {test_step} and Response from LLM: {response['steps']}")
                                                    raise TypeError(
                                                        "Provided Input element and response from LLM doesn't match")

                                            # Enterprise field injection into response steps
                                            for resp_step in response["steps"]:
                                                if 'text' in resp_step:
                                                    resp_step['text'] = resolve_placeholder_in_test_data(resp_step['text'])
                                            status, steps_performed, steps_not_performed, current_df = await perform(page,
                                                                                                                    response[
                                                                                                                        "steps"],
                                                                                                                    chroma_connector,
                                                                                                                    context_key,
                                                                                                                    current_df,
                                                                                                                    test_step,
                                                                                                                    new_window_detected)
                                        else:
                                            logger.info(f"Provided Input element and response from LLM doesn't match")
                                            logger.info(
                                                f"Input element provided: {test_step} and Response from LLM: {response['steps']}")
                                            raise Exception(
                                                "Element matching failed between input and LLM response"
                                            )

                                    else:
                                        logger.info(f"Provided Input action and response from LLM doesn't match")
                                        logger.info(
                                            f"Input element provided: {test_step} and Response from LLM: {response['steps']}")
                                        raise Exception(
                                            "Action matching failed between input and LLM response"
                                        )

                        if not steps_not_performed:
                            logging.debug("Steps implemented successfully")
                            steps_implemented = True
                        if steps_performed and steps_performed.isspace():
                            steps_implemented = False
                            scroll = True
                        elif steps_performed and not steps_performed.isspace() and status == -1:
                            steps_implemented = False
                            scroll = True
                        elif response["stop_reason"]["Do you Want next set of elements?"] == "yes":
                            scroll = True
                        else:
                            steps_implemented = True
                        attempt += 1
                    except Exception as e:
                        logger.error(f"Error during step execution: {e}")
                        attempt += 1
                        stored_exception = e
                        if attempt >= max_attempts:
                            logger.error("Max attempts reached, moving to next step")
                        else:
                            logger.info(f"Retrying... Attempt {attempt + 1} of {max_attempts}")

                # 🔧 UPDATE DB: If locator reuse failed but normal discovery succeeded, update DB
                logger.info(f"🔧 DB-UPSERT: CHECK CONDITIONS | locator_reuse_failed: {locator_reuse_failed}, steps_implemented: {steps_implemented}, stored_exception: {stored_exception is not None}")

                if locator_reuse_failed and steps_implemented and not stored_exception:
                    logger.info(f"🔧 DB-UPSERT: CONDITIONS MET ✅ | Saving working locator to database")
                    await _update_working_locator_in_db(test_step, current_df)
                elif locator_reuse_failed:
                    if not steps_implemented:
                        logger.info(f"🔧 DB-UPDATE: SKIP - NO IMPLEMENTATION ❌ | steps_implemented: {steps_implemented}")
                    if stored_exception:
                        logger.info(f"🔧 DB-UPDATE: SKIP - EXCEPTION ❌ | stored_exception: {str(stored_exception)[:100]}")
                else:
                    logger.debug(f"🔧 DB-UPDATE: SKIP - NO REUSE FAILURE | locator_reuse_failed: {locator_reuse_failed}")

                # 🔥 CRITICAL FIX: If normal discovery failed after max attempts, raise exception
                if not steps_implemented and attempt >= max_attempts:
                    if stored_exception:
                        logger.error("Re-raising stored exception after max attempts exceeded")
                        raise stored_exception
                    else:
                        # No stored exception, but discovery failed - create one
                        error_msg = f"❌ STEP FAILED: {current_bdd_step} | Element discovery failed after {max_attempts} attempts"
                        logger.error(error_msg)
                        logger.error(f"🔍 Discovery results: steps_performed='{steps_performed}', status={status}")
                        raise Exception(error_msg)

                if stored_exception and attempt >= max_attempts:
                    logger.error("Re-raising exception after max attempts exceeded")
                    raise stored_exception
        except Exception as __e:
            __run_failed = True
            logger.error(f"❌ TEST EXECUTION HALTED - Step failed: {__e}")
            logger.error(f"🛑 FAILED BDD STEP: {current_bdd_step if 'current_bdd_step' in locals() else 'Unknown step'}")

            if record_video:
                # Close page first to finalize video before accessing path
                try:
                    if 'page' in locals() and page:
                        await page.close()
                except Exception:
                    pass
                failure_zip = await handle_video_recording_and_upload(
                    success=False,
                    record_video=record_video,
                    video_dir=__video_dir,
                    context=locals().get('context'),
                    page=locals().get('page'),
                    jira_id=jira_id
                )
                if failure_zip:
                    logger.error(f"[Recording] Failure recording available: {failure_zip}")
                    logger.error(f"RECORDING_ZIP: {failure_zip}")

                    # Attach recording info to exception
                    try:
                        if hasattr(__e, '__dict__'):
                            __e.recording_zip = failure_zip
                        else:
                            logger.warning(f"Exception type {type(__e)} does not support attribute assignment")
                    except (AttributeError, TypeError) as attr_error:
                        logger.warning(f"Could not attach recording_zip to exception: {attr_error}")

            # Attach the current BDD step to the exception for error reporting
            try:
                setattr(__e, "step", current_bdd_step if 'current_bdd_step' in locals() else "Unknown step")
            except Exception:
                pass
            
            raise __e
        finally:
            # Stop Xvfb process if it was started
            try:
                if 'xvfb_process' in locals() and xvfb_process is not None:
                    logger.info("Stopping Xvfb process...")
                    xvfb_process.terminate()
                    xvfb_process.wait(timeout=5)  # Wait up to 5 seconds for clean shutdown
                    logger.info("Xvfb process stopped successfully")
            except subprocess.TimeoutExpired:
                logger.warning("Xvfb process did not terminate gracefully, forcing kill...")
                try:
                    xvfb_process.kill()
                    xvfb_process.wait()
                    logger.info("Xvfb process killed")
                except Exception as kill_e:
                    logger.error(f"Failed to kill Xvfb process: {kill_e}")
            except Exception as xvfb_e:
                logger.error(f"Error stopping Xvfb process: {xvfb_e}")
            if not __run_failed and record_video:
                # Close page first to finalize video before accessing path
                try:
                    if 'page' in locals() and page:
                        await page.close()
                except Exception:
                    pass
                success_recording_zip = await handle_video_recording_and_upload(
                    success=True,
                    record_video=record_video,
                    video_dir=__video_dir,
                    context=locals().get('context'),
                    page=locals().get('page'),
                    jira_id=jira_id
                )
            elif not record_video:
                # Clean up recording directory if recording is disabled
                _cleanup_success_recording_dir(__video_dir)
            # best-effort: close context/browser after video processing
            try:
                if 'context' in locals() and context:
                    await asyncio.sleep(8)
                    await context.close()
            except Exception:
                pass
            try:
                if 'browser' in locals() and browser:
                    await browser.close()
            except Exception:
                pass
            clear_video_run_dir_registration()
            cleanup_orphan_recording_dir(__video_dir)
    # 🔥 CRITICAL FIX: Stop execution here if test failed
    if __run_failed:
        logger.error("🛑 TEST EXECUTION FAILED - Stopping processing and returning early")
        return scenario_dict  # Return early without post-processing

    logger.info(f'*****Done*****')
    flag = False
    df_inter = current_df
    scenario_dict[f"Scenario_{i}"] = current_df

    previous_key, previous_index = find_previous_scenario(scenario_dict, i)
    if previous_key is not None:
        # 🔧 FIX: Compare against ALL previous scenarios, not just the immediate previous
        all_previous_df = pd.DataFrame()
        for j in range(i):
            prev_key = f"Scenario_{j}"
            if prev_key in scenario_dict:
                all_previous_df = pd.concat([all_previous_df, scenario_dict[prev_key]], ignore_index=True)

        if not all_previous_df.empty:
            modified_df, removed_df = remove_duplicates_with_conditions(all_previous_df,
                                                                        scenario_dict[f"Scenario_{i}"], keep='first')
            final_df = pd.concat([modified_df, removed_df], ignore_index=True)
            scenario_dict[f"Scenario_{i}"] = final_df
        else:
            modified_df, removed_df = remove_duplicates_with_conditions(scenario_dict[previous_key],
                                                                        scenario_dict[f"Scenario_{i}"], keep='first')
            final_df = pd.concat([modified_df, removed_df], ignore_index=True)
            scenario_dict[f"Scenario_{i}"] = final_df
        if removed_df is None or removed_df.empty:
            logger.info(f"All steps are duplicate")
        else:
            flag = True
            field_details = (
                "Details of all actions on web element objects with test data and PlaywrightLocator and condition given below in the form of "
                "'BDDStep -- ActionType -- WebElementName -- TestDataValue -- PlaywrightLocator -- Condition -- WindowDetection'. \n"
                "CRITICAL: Column 5 is PlaywrightLocator (the actual selector to use in code). Column 4 is TestDataValue (e.g., 'user@test.com'). "
                "Do NOT confuse them. Use PlaywrightLocator verbatim in your generated code. Do NOT generate, infer, or substitute a different locator.\n")
            for _, row in removed_df.iterrows():
                if str(row['action']).lower() == "verify":
                    field_details += str(row['original_bdd_step']) + " -- " + str(row['action']) + " -- " + str(
                        row['object']) + " -- " + str(
                        row['data']) + " -- " + str(
                        row['playwrightLocator']) + " -- " + str(row['Condition']) + " -- " + str(
                        row['window_detected']) + "\n"
                elif str(row['action']).lower() == "switch_to_iframe":
                    xpath_value = str(row['Xpath'])
                    field_details += str(row['original_bdd_step']) + " -- " + str(row['action']) + " -- " + str(
                        row['object']) + " -- " + str(
                        row['data']) + " -- " + f"page.locator('xpath={xpath_value}')" + " -- " + str(
                        row['Condition']) + " -- " + str(row['window_detected']) + "\n"

                else:
                    field_details += str(row['original_bdd_step']) + " -- " + str(row['action']) + " -- " + str(
                        row['object']) + " -- " + str(
                        row['data']) + " -- " + str(
                        row['playwrightLocator']) + " -- " + str(row['Condition']) + " -- " + str(
                        row['window_detected']) + "\n"

            if generate_step_definitions:
                final_script  = stepDefTemplate.generate_playwright(file_content, field_details, df_inter, i, flag,
                                                         jira_id=jira_id,current_script=current_script, generate_step_definitions=True)
            else:
                playwright_generator = StepDefGenerationPlaywright()
                final_script  = playwright_generator.generate_playwright_test(file_content, field_details, df_inter, i, flag, jira_id=jira_id, current_script=current_script, datasets=datasets)

    else:
        field_details = (
            "Details of all actions on web element objects with test data and PlaywrightLocator and condition given below in the form of "
            "'BDDStep -- ActionType -- WebElementName -- TestDataValue -- PlaywrightLocator -- Condition -- WindowDetection'. \n"
            "CRITICAL: Column 5 is PlaywrightLocator (the actual selector to use in code). Column 4 is TestDataValue (e.g., 'user@test.com'). "
            "Do NOT confuse them. Use PlaywrightLocator verbatim in your generated code. Do NOT generate, infer, or substitute a different locator.\n")
        for _, row in df_inter.iterrows():
            if str(row['action']).lower() == "verify":
                field_details += str(row['original_bdd_step']) + " -- " + str(row['action']) + " -- " + str(
                    row['object']) + " -- " + str(
                    row['data']) + " -- " + str(
                    row['playwrightLocator']) + " -- " + str(row['Condition']) + " -- " + str(
                    row['window_detected']) + "\n"
            elif str(row['action']).lower() == "switch_to_iframe":
                xpath_value = str(row['Xpath'])
                field_details += str(row['original_bdd_step']) + " -- " + str(row['action']) + " -- " + str(
                    row['object']) + " -- " + str(
                    row['data']) + " -- " + f"page.locator('xpath={xpath_value}')" + " -- " + str(
                    row['Condition']) + " -- " + str(row['window_detected']) + "\n"

            else:
                field_details += str(row['original_bdd_step']) + " -- " + str(row['action']) + " -- " + str(
                    row['object']) + " -- " + str(
                    row['data']) + " -- " + str(
                    row['playwrightLocator']) + " -- " + str(row['Condition']) + " -- " + str(
                    row['window_detected']) + "\n"

    if generate_step_definitions:
            final_script  = stepDefTemplate.generate_playwright(file_content, field_details, df_inter, i, flag,
                                                         jira_id=jira_id,current_script=current_script, generate_step_definitions=True)
    else:
        playwright_generator = StepDefGenerationPlaywright()
        final_script  = playwright_generator.generate_playwright_test(file_content, field_details, df_inter, i, flag, jira_id=jira_id, current_script=current_script, datasets=datasets)

    # 💾 FINAL DB INSERT: Save all discovered locators to database (same as generate_bdd_script.py)
    try:
        if current_df is not None and not current_df.empty:
            # Clean up any remaining current_frame. locators before saving
            if 'playwrightLocator' in current_df.columns:
                current_frame_mask = current_df['playwrightLocator'].astype(str).str.contains('current_frame.', na=False)
                current_frame_count = current_frame_mask.sum()

                if current_frame_count > 0:
                    logger.warning(f"💾 DB-INSERT: CONVERTING LOCATORS | {current_frame_count} current_frame. → page. locators")
                    current_df.loc[current_frame_mask, 'playwrightLocator'] = (
                        current_df.loc[current_frame_mask, 'playwrightLocator']
                        .astype(str)
                        .str.replace('current_frame.', 'page.', regex=False)
                    )
                    logger.info(f"💾 DB-INSERT: CONVERTED | {current_frame_count} locators updated")

            # Import the database function
            from utils.db_utils import push_steps_df, cleanup_legacy_current_frame_locators

            # Clean up legacy locators first
            try:
                cleaned_count = cleanup_legacy_current_frame_locators()
                if cleaned_count > 0:
                    logger.info(f"💾 DB-INSERT: CLEANUP | Removed {cleaned_count} legacy current_frame. locators")
            except Exception as cleanup_err:
                logger.warning(f"💾 DB-INSERT: CLEANUP FAILED | {cleanup_err}")

            # Save all locators to database
            logger.info(f"💾 DB-INSERT: STARTING | Saving {len(current_df)} locators to database")
            pushed = push_steps_df(
                df=current_df,
                table="t_locators",
                key_col="unique_id",
            )
            logger.info(f"💾 DB-INSERT: COMPLETE ✅ | {pushed} locator records processed")
        else:
            logger.info("💾 DB-INSERT: SKIP | No locator data to save")
    except Exception as db_err:
        logger.error(f"💾 DB-INSERT: ERROR ❌ | Failed to save locators: {str(db_err)[:100]}")
        # Don't fail the entire process if DB insert fails
        pass

    if record_video and 'success_recording_zip' in locals() and success_recording_zip:
        return final_script, success_recording_zip
    return final_script



def generate_field_details(dataframe):
    """
    Generate field details string from DataFrame rows

    Args:
        dataframe: pandas DataFrame containing the test step data

    Returns:
        str: Formatted field details string
    """
    field_details = (
        "Details of all actions on web element objects with test data and PlaywrightLocator and condition given below in the form of "
        "'BDDStep -- ActionType -- WebElementName -- TestDataValue -- PlaywrightLocator -- Condition -- WindowDetection'. \n"
        "CRITICAL: Column 5 is PlaywrightLocator (the actual selector to use in code). Column 4 is TestDataValue (e.g., 'user@test.com'). "
        "Do NOT confuse them. Use PlaywrightLocator verbatim in your generated code. Do NOT generate, infer, or substitute a different locator.\n"
    )

    for _, row in dataframe.iterrows():
        if str(row['action']).lower() == "verify":
            field_details += (
                f"{row['original_bdd_step']} -- {row['action']} -- {row['object']} -- "
                f"{row['data']} -- {row['playwrightLocator']} -- {row['Condition']} -- "
                f"{row['window_detected']}\n"
            )
        elif str(row['action']).lower() == "switch_to_iframe":
            xpath_value = str(row['Xpath'])
            field_details += (
                f"{row['original_bdd_step']} -- {row['action']} -- {row['object']} -- "
                f"{row['data']} -- page.locator('xpath={xpath_value}') -- {row['Condition']} -- "
                f"{row['window_detected']}\n"
            )
        else:
            field_details += (
                f"{row['original_bdd_step']} -- {row['action']} -- {row['object']} -- "
                f"{row['data']} -- {row['playwrightLocator']} -- {row['Condition']} -- "
                f"{row['window_detected']}\n"
            )

    return field_details


def find_previous_scenario(scenario_dict, i):
    for j in range(i - 1, -1, -1):
        scenario_key = f"Scenario_{j}"
        if scenario_key in scenario_dict:
            return scenario_key, j
    return None, None



def check_bdd_step_match(row1: pd.Series, row2: pd.Series) -> bool:
    """
    Check if two rows match based on 4 sequential conditions:
    1. original_bdd_step exact match OR data-only difference
    2. window_detected must be same
    3. playwrightLocator must be same
    4. All conditions must pass sequentially
    """

    # CONDITION 1: Check original_bdd_step column
    bdd1 = str(row1['original_bdd_step']) if pd.notna(row1['original_bdd_step']) else 'NaN'
    bdd2 = str(row2['original_bdd_step']) if pd.notna(row2['original_bdd_step']) else 'NaN'

    # Check for exact match first
    if bdd1 == bdd2:
        bdd_condition_passed = True
    else:
        # CONDITION 2: Check if difference is only in data column
        bdd_condition_passed = check_data_only_difference(row1, row2)

    # If BDD conditions failed, no need to check further
    if not bdd_condition_passed:
        return False

    # CONDITION 3: Check window_detected column
    window1 = str(row1['window_detected']) if pd.notna(row1['window_detected']) else 'NaN'
    window2 = str(row2['window_detected']) if pd.notna(row2['window_detected']) else 'NaN'

    if window1 != window2:
        return False

    # CONDITION 4: Enhanced Locator + Page Context Validation
    locator1 = str(row1['playwrightLocator']) if pd.notna(row1['playwrightLocator']) else 'NaN'
    locator2 = str(row2['playwrightLocator']) if pd.notna(row2['playwrightLocator']) else 'NaN'

    # PRIMARY: Check locator match first
    if locator1 == locator2:
        pass
    else:

        # Get page and url values
        page1 = str(row1['page']) if pd.notna(row1['page']) else 'NaN'
        page2 = str(row2['page']) if pd.notna(row2['page']) else 'NaN'
        url1 = str(row1['url']) if pd.notna(row1['url']) else 'NaN'
        url2 = str(row2['url']) if pd.notna(row2['url']) else 'NaN'

        if page1 == page2 and url1 == url2:
            pass
        else:

            return False
    return True


def check_data_only_difference(row1: pd.Series, row2: pd.Series) -> bool:
    """
    CONDITION 2: Check if the only difference between two BDD steps is the test data
    Replace row1's data with row2's data and see if sentences become identical
    """
    bdd1 = str(row1['original_bdd_step']) if pd.notna(row1['original_bdd_step']) else 'NaN'
    bdd2 = str(row2['original_bdd_step']) if pd.notna(row2['original_bdd_step']) else 'NaN'
    data1 = str(row1['data']) if pd.notna(row1['data']) else 'NaN'
    data2 = str(row2['data']) if pd.notna(row2['data']) else 'NaN'

    # If both data values are NaN, they should be exactly the same BDD steps
    if data1 == 'NaN' and data2 == 'NaN':
        result = bdd1 == bdd2
        return result

    # If one data is NaN and other is not, they can't be data-only difference
    if (data1 == 'NaN') != (data2 == 'NaN'):
        return False

    # Replace row1's data with row2's data in the BDD step
    try:
        # Simple string replacement - replace data1 with data2 in bdd1
        modified_bdd1 = bdd1.replace(data1, data2) if data1 != 'NaN' else bdd1

        if modified_bdd1 == bdd2:
            return True
        else:
            return False

    except Exception as e:
        return False


def identify_iframe_sequences(df: pd.DataFrame) -> List[Tuple[int, int]]:
    """
    Identify sequences that start with 'switch_to_iframe'
    Returns list of (start_index, end_index) tuples
    """
    sequences = []
    i = 0

    while i < len(df):
        if df.iloc[i]['action'] == 'switch_to_iframe':
            start_idx = i
            i += 1

            # Continue until we hit a non-iframe action or end of dataframe
            while i < len(df) and df.iloc[i]['action'] == 'switch_to_iframe':
                i += 1

            # Include the next action after iframe switches (if exists)
            if i < len(df):
                end_idx = i
                sequences.append((start_idx, end_idx))
                i += 1
            else:
                # If we're at the end, just include the iframe switches
                sequences.append((start_idx, i - 1))
        else:
            i += 1

    return sequences


def get_rows_in_sequences(sequences: List[Tuple[int, int]]) -> Set[int]:
    """Get all row indices that are part of iframe sequences"""
    sequence_rows = set()
    for start, end in sequences:
        sequence_rows.update(range(start, end + 1))
    return sequence_rows


def check_sequence_match_4_conditions(df1: pd.DataFrame, df2: pd.DataFrame,
                                      seq1_range: Tuple[int, int],
                                      seq2_range: Tuple[int, int]) -> bool:
    """
    Check if two iframe sequences match using 4 conditions
    All corresponding rows in sequences must satisfy all 4 conditions
    """
    seq1_start, seq1_end = seq1_range
    seq2_start, seq2_end = seq2_range

    # Sequences must have same length
    seq1_length = seq1_end - seq1_start + 1
    seq2_length = seq2_end - seq2_start + 1

    if seq1_length != seq2_length:
        return False

    # Check each corresponding row pair in the sequences using 4 conditions
    for i in range(seq1_length):
        row1_idx = seq1_start + i
        row2_idx = seq2_start + i

        # Apply ALL 4 CONDITIONS to each row pair
        if not check_bdd_step_match(df1.iloc[row1_idx], df2.iloc[row2_idx]):
            return False

    return True


def find_all_duplicates_with_4_conditions(df1: pd.DataFrame, df2: pd.DataFrame) -> Dict:
    """
    Find duplicates using 4-condition logic in correct order:
    STEP 1: Handle iframe sequences first
    STEP 2: Handle regular (non-iframe) rows
    """

    # STEP 1: Identify iframe sequences FIRST
    seq1 = identify_iframe_sequences(df1)
    seq2 = identify_iframe_sequences(df2)
    sequence_rows_df1 = get_rows_in_sequences(seq1)
    sequence_rows_df2 = get_rows_in_sequences(seq2)

    # STEP 2: Compare iframe sequences using 4 conditions
    iframe_duplicates = {
        'df1_duplicates': [],
        'df2_duplicates': [],
        'matches': []
    }

    for i, seq1_range in enumerate(seq1):
        for j, seq2_range in enumerate(seq2):
            logger.info(f"\n  🔄 Checking iframe sequence pair: DF1_seq_{i} vs DF2_seq_{j}")

            if check_sequence_match_4_conditions(df1, df2, seq1_range, seq2_range):
                iframe_duplicates['matches'].append((f"df1_seq_{i}", f"df2_seq_{j}"))
                iframe_duplicates['df1_duplicates'].append(seq1_range)
                iframe_duplicates['df2_duplicates'].append(seq2_range)

    # STEP 3: Compare regular (non-iframe) rows using 4 conditions
    row_duplicates = {
        'df1_duplicates': [],
        'df2_duplicates': [],
        'matches': []
    }

    individual_comparisons = 0
    for i1 in range(len(df1)):
        # SKIP rows that are part of iframe sequences (already handled)
        if i1 in sequence_rows_df1:
            continue

        for i2 in range(len(df2)):
            # SKIP rows that are part of iframe sequences (already handled)
            if i2 in sequence_rows_df2:
                continue

            individual_comparisons += 1

            # Apply ALL 4 CONDITIONS to individual rows
            if check_bdd_step_match(df1.iloc[i1], df2.iloc[i2]):
                row_duplicates['matches'].append((f"df1_row_{i1}", f"df2_row_{i2}"))
                row_duplicates['df1_duplicates'].append(i1)
                row_duplicates['df2_duplicates'].append(i2)

    return {
        'iframe_duplicates': iframe_duplicates,
        'row_duplicates': row_duplicates
    }


def normalize_quotes(text: str) -> str:
    """
    Normalize quotes in BDD steps - convert all single quotes to double quotes
    So 'Start Chat' becomes "Start Chat" for consistent comparison
    """
    # Replace single quotes with double quotes for normalization
    return text.replace("'", '"')


def is_parameterizable_step(row1: pd.Series, row2: pd.Series) -> bool:
    """
    Check if two steps are parameterizable (should generate one reusable step definition)
    Returns True for navigation, click, enter steps with different data
    """
    bdd1 = str(row1['original_bdd_step']) if pd.notna(row1['original_bdd_step']) else 'NaN'
    bdd2 = str(row2['original_bdd_step']) if pd.notna(row2['original_bdd_step']) else 'NaN'

    # Check for parameterizable patterns
    parameterizable_patterns = [
        'clicks on', 'click on', 'clicks', 'click',
        'enters', 'enter', 'types', 'type'
    ]

    for pattern in parameterizable_patterns:
        if pattern in bdd1.lower() and pattern in bdd2.lower():
            return True

    return False

def check_data_only_difference(row1: pd.Series, row2: pd.Series) -> bool:
    """
    CONDITION 2: Check if the only difference between two BDD steps is the test data
    Replace row1's data with row2's data and see if sentences become identical
    """
    bdd1 = str(row1['original_bdd_step']) if pd.notna(row1['original_bdd_step']) else 'NaN'
    bdd2 = str(row2['original_bdd_step']) if pd.notna(row2['original_bdd_step']) else 'NaN'
    data1 = str(row1['data']) if pd.notna(row1['data']) else 'NaN'
    data2 = str(row2['data']) if pd.notna(row2['data']) else 'NaN'

    # 🔧 NORMALIZE QUOTES BEFORE COMPARISON
    bdd1_normalized = normalize_quotes(bdd1)
    bdd2_normalized = normalize_quotes(bdd2)
    data1_normalized = normalize_quotes(data1)
    data2_normalized = normalize_quotes(data2)

    # If both data values are NaN, they should be exactly the same BDD steps
    if data1_normalized == 'NaN' and data2_normalized == 'NaN':
        result = bdd1_normalized == bdd2_normalized
        return result

    # If one data is NaN and other is not, they can't be data-only difference
    if (data1_normalized == 'NaN') != (data2_normalized == 'NaN'):
        return False

    # 🔧 FIX: Don't treat parameterizable steps as duplicates
    if is_parameterizable_step(row1, row2):
        return False  # Keep both steps for parameterized step definition

    # Replace row1's data with row2's data in the BDD step
    try:
        # Use normalized versions for replacement
        modified_bdd1 = bdd1_normalized.replace(data1_normalized,
                                                data2_normalized) if data1_normalized != 'NaN' else bdd1_normalized

        if modified_bdd1 == bdd2_normalized:
            return True
        else:
            return False

    except Exception as e:
        return False


def remove_duplicates_with_conditions(df1: pd.DataFrame, df2: pd.DataFrame, keep='first') -> Tuple[
    pd.DataFrame, pd.DataFrame]:
    """
    Remove duplicates using 4-condition logic
    """

    # Find all duplicates using 4 conditions
    all_duplicates = find_all_duplicates_with_4_conditions(df1, df2)
    iframe_dups = all_duplicates['iframe_duplicates']
    row_dups = all_duplicates['row_duplicates']

    # Show what will be removed
    if len(iframe_dups['matches']) > 0:
        for i, match in enumerate(iframe_dups['matches']):
            df1_seq, df2_seq = match
            df1_range = iframe_dups['df1_duplicates'][i]
            df2_range = iframe_dups['df2_duplicates'][i]

    if len(row_dups['matches']) > 0:
        for i, match in enumerate(row_dups['matches']):
            df1_row, df2_row = match
            df1_idx = row_dups['df1_duplicates'][i]
            df2_idx = row_dups['df2_duplicates'][i]

    # Create masks for rows to keep
    df1_keep_mask = pd.Series([True] * len(df1))
    df2_keep_mask = pd.Series([True] * len(df2))

    if keep == 'first':

        # Remove iframe sequence duplicates from df2
        for start, end in iframe_dups['df2_duplicates']:
            df2_keep_mask.iloc[start:end + 1] = False

        # Remove individual row duplicates from df2
        for row_idx in row_dups['df2_duplicates']:
            df2_keep_mask.iloc[row_idx] = False

    elif keep == 'last':

        # Remove iframe sequence duplicates from df1
        for start, end in iframe_dups['df1_duplicates']:
            df1_keep_mask.iloc[start:end + 1] = False

        # Remove individual row duplicates from df1
        for row_idx in row_dups['df1_duplicates']:
            df1_keep_mask.iloc[row_idx] = False

    # Apply masks and reset indices
    df1_cleaned = df1[df1_keep_mask].reset_index(drop=True)
    df2_cleaned = df2[df2_keep_mask].reset_index(drop=True)
    return df1_cleaned, df2_cleaned


async def handle_video_recording_and_upload(
    success: bool,
    record_video: bool,
    video_dir: Path = None,
    context = None,
    page = None,
    jira_id: str = None
) -> str:
    """
    Handle video recording, zipping, and S3 upload for both success and failure cases.
    Args:
        success: Whether the test run was successful
        record_video: Whether video recording is enabled
        video_dir: Directory where videos are stored
        context: Playwright browser context
        page: Playwright page object
        jira_id: JIRA ID for naming

    Returns:
        str: S3 URL of the uploaded video zip file, or None if no recording
    """
    if not record_video or not video_dir:
        return None

    zip_url = None

    try:
        # Ensure video directory exists
        Path(video_dir).mkdir(parents=True, exist_ok=True)

        def _move_video_to_standard_name(src_path: str) -> Path:
            """Move/rename the raw video to a standardized name; fallback to copy+delete."""
            if not src_path:
                return None
            src = Path(src_path)
            if not src.exists():
                return None

            target_path = video_dir / f"{video_dir.name}.webm"

            # If already the standardized file, nothing to do
            try:
                if src.resolve() == target_path.resolve():
                    return target_path
            except Exception:
                pass

            # Try atomic move/replace; fallback to copy + delete
            try:
                if target_path.exists():
                    try:
                        target_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                shutil.move(str(src), str(target_path))
                return target_path
            except Exception:
                try:
                    shutil.copyfile(str(src), str(target_path))
                    try:
                        src.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return target_path
                except Exception:
                    return None

        # Try to find and copy video files
        copied_path = None
        video_found = False

        # First try from page video
        if page and getattr(page, "video", None):
            try:
                actual_path = await page.video.path()
                if actual_path and Path(actual_path).exists():
                    logger.info(f"[Recording] Video file found at: {actual_path}")
                    copied_path = _move_video_to_standard_name(actual_path)
                    if copied_path:
                        video_found = True
                        logger.info(f"[Recording] ✅ Video moved to standard location: {copied_path}")
                else:
                    logger.warning(f"[Recording] ❌ Video file not found at expected path: {actual_path}")
            except Exception as e:
                logger.warning(f"[Recording] ❌ Could not get video from page: {e}")

        # If no page video, try from context pages
        if not video_found and context:
            for p in getattr(context, "pages", []):
                if getattr(p, "video", None):
                    try:
                        ap = await p.video.path()
                        # ✅ FIX BUG #1: Validate video path exists before processing
                        if ap and Path(ap).exists():
                            logger.info(f"[Recording] Video file found from context page at: {ap}")
                            copied_path = _move_video_to_standard_name(ap)
                            if copied_path:
                                video_found = True
                                logger.info(f"[Recording] ✅ Video moved to standard location: {copied_path}")
                                break
                        else:
                            logger.warning(f"[Recording] ❌ Video file not found at expected path: {ap}")
                    except Exception as e:
                        logger.warning(f"[Recording] ❌ Could not get video from context page: {e}")

        # ✅ FIX BUG #1: Validate that video files actually exist in directory before zipping
        has_video_files = False
        if video_dir.exists():
            video_files = list(video_dir.glob("*.webm")) + list(video_dir.glob("*.mp4"))
            has_video_files = len(video_files) > 0
            if has_video_files:
                logger.info(f"[Recording] Found {len(video_files)} video files in directory")
            else:
                logger.warning(f"[Recording] ⚠️ No video files found in directory: {video_dir}")

        # Create zip archive only if video files exist
        if video_dir.exists() and has_video_files:
            zip_path = shutil.make_archive(str(video_dir), "zip", video_dir)
            logger.info(f"[Recording] ✅ Video archive created: {zip_path}")
            
            # Clean up the directory after zipping
            shutil.rmtree(video_dir, ignore_errors=True)

            # Upload to S3
            try:
                upload_result = upload_file_to_s3_and_get_urls(Path(zip_path))
                zip_url = upload_result.get("presigned_url") or upload_result.get("http_url")

                # Delete local zip after successful upload
                try:
                    Path(zip_path).unlink(missing_ok=True)
                except Exception:
                    pass

                status = "Success" if success else "Failure"
                logger.info(f"[Recording] {status} recording uploaded to S3: {redact_presigned_url_for_log(zip_url)}")

                # Set the last recording URL
                try:
                    set_last_recording_url(zip_url)
                except Exception:
                    pass

            except Exception as s3_err:
                # Fallback to local static URL
                logger.warning(f"[Recording] S3 upload failed, falling back to local URL: {s3_err}")
                zip_url = _build_download_url(Path(zip_path))
        elif video_dir.exists():
            logger.error(f"[Recording] ❌ Video directory exists but contains no video files: {video_dir}")
            logger.error(f"[Recording] Directory contents: {list(video_dir.glob('*'))}")
            shutil.rmtree(video_dir, ignore_errors=True)
        else:
            logger.warning(f"[Recording] ⚠️ Video directory does not exist: {video_dir}")

    except Exception as e:
        logger.error(f"[Recording] Failed to process video recording: {e}")
    finally:
        try:
            vd = Path(video_dir)
            if vd.is_dir():
                shutil.rmtree(vd, ignore_errors=True)
        except Exception:
            pass
        
    return zip_url
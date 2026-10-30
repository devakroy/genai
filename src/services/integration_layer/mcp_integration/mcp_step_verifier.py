"""
MCP Step Verification Enhancement
================================

This file contains the implementation plan for adding step verification logic 
to the MCP flow, similar to the JS crawler verification.

Recent Improvements (2025-12-15):
- Fixed incompatible UI types logic to allow 'select' elements to work with 'button' actions
- Added semantic synonyms for 'select', 'select button', 'choose', 'pick' relationships
- Enhanced UI element synonyms to include 'button' as valid for 'select'/'dropdown' contexts
- Added special case handling for "Select" button patterns (95% confidence)
- Improved confidence scoring for select-button UI interactions
- LATEST FIX: Removed 'button' from 'select' incompatible types to prevent false negatives
- Enhanced context-aware incompatible UI type detection for Select button patterns
- Added comprehensive Select button pattern matching in confidence calculation
- Protected Select button patterns from confidence penalties in overall scoring

Text Verification Enhancement (2025-12-15):
- Added special handling for text verification scenarios where text content matters more than element description
- Implemented text verification confidence boosting (95% when text content matches)
- Added text verification recovery strategy for legitimate verification attempts
- Enhanced semantic synonyms for verification and message-related patterns
- Prevents blocking of valid text verification when element description has low similarity

Progressive Error Recovery (2025-12-15):
- Added recovery strategy framework to prevent over-strict validation blocking
- Context-aware validation for rectification scenarios (navigation for verification)
- Progressive confidence thresholds (strict/moderate/lenient/emergency)
- Smart recovery for text verification requiring navigation
- Iframe redirection and fallback tool strategies
- Prevents infinite retry loops while maintaining safety

These changes resolve validation failures where legitimate rectification attempts
were incorrectly blocked, while maintaining protection against dangerous mismatches.
The latest improvements specifically address text verification scenarios where the
verifier was incorrectly comparing text content with element descriptions.
"""

import re
from utils.constants import PLATFORM_CONFIG
import logging
import boto3
from typing import Dict, Any, Optional
from dataclasses import dataclass
from botocore.config import Config
from langchain_aws import ChatBedrockConverse
from .onboarding_config import get_allowed_hosts_for_current_domain, set_current_domain

@dataclass
class MCPStepValidation:
    """Validation result for MCP step matching"""
    is_valid: bool
    confidence_score: float
    action_match: bool
    element_match: bool
    intent_match: bool
    validation_details: str
    suggested_corrections: Optional[Dict[str, Any]] = None
    recovery_strategy: Optional[str] = None
    allow_proceed: bool = False  # Whether to proceed despite validation failure

class MCPStepVerifier:
    """
    Step verification class for MCP flow to ensure LLM responses match intended steps.
    Implements similar logic to JS crawler verification.
    """

    def __init__(self, domain_selection: Optional[str] = None):
        self.logger = logging.getLogger(self.__class__.__name__)
        # Best-effort: set current domain early so allowlist validations use correct module context
        if isinstance(domain_selection, str) and domain_selection.strip():
            try:
                set_current_domain(domain_selection.strip())
            except Exception:
                # Non-fatal; domain context is best-effort
                pass
        self.similarity_threshold = 40.0  # Lowered from 50.0 to allow legitimate form field matches with indicators
        self.progressive_thresholds = {
            'strict': 80.0,      # High confidence required
            'moderate': 50.0,    # Standard threshold
            'lenient': 30.0,     # Lower threshold for recovery
            'emergency': 15.0    # Last resort threshold
        }
        self.intent_patterns = {
            'numeric': [r'\bnumeric\s+link\b', r'\bnumeric\b', r'\bnumber\s+link\b',
                       r'\bid\s+link\b', r'\bnumber\b', r'\bid\b'],
            'spatial': ['beside', 'near', 'next to', 'close to', 'adjacent to']
        }
        # Add UI element synonyms for better matching
        self.ui_element_synonyms = {
            'field': ['textbox', 'input', 'box', 'field'],
            'textbox': ['field', 'input', 'box', 'textbox'],
            'input': ['field', 'textbox', 'box', 'input'],
            'button': ['btn', 'button'],
            'btn': ['button', 'btn'],
            'link': ['hyperlink', 'anchor', 'link'],
            'checkbox': ['check', 'tickbox', 'checkbox'],
            'radio': ['radiobutton', 'option', 'radio'],
            'dropdown': ['select', 'menu', 'picker', 'filter', 'list', 'option', 'dropdown', 'button'],
            'select': ['dropdown', 'menu', 'picker', 'filter', 'list', 'option', 'select', 'button'],
            'menu': ['dropdown', 'select', 'picker', 'filter', 'list', 'option', 'menu'],
            'filter': ['dropdown', 'select', 'menu', 'picker', 'list', 'option', 'filter'],
            'option': ['dropdown', 'select', 'menu', 'picker', 'filter', 'list', 'option'],
            'picker': ['dropdown', 'select', 'menu', 'filter', 'list', 'option', 'picker']
        }

        # Add semantic element synonyms for matching similar concepts
        self.semantic_synonyms = {
            'login': ['sign in', 'signin', 'log in', 'authenticate', 'enter'],
            'sign in': ['login', 'signin', 'log in', 'authenticate', 'enter'],
            'signin': ['login', 'sign in', 'log in', 'authenticate', 'enter'],
            'log in': ['login', 'sign in', 'signin', 'authenticate', 'enter'],
            'submit': ['send', 'confirm', 'proceed', 'continue'],
            'send': ['submit', 'confirm', 'proceed'],
            'continue': ['proceed', 'next', 'forward', 'go'],
            'proceed': ['continue', 'next', 'forward', 'go'],
            'cancel': ['close', 'dismiss', 'exit', 'abort'],
            'close': ['cancel', 'dismiss', 'exit'],
            'search': ['find', 'lookup', 'locate'],
            'find': ['search', 'lookup', 'locate'],
            'select': ['select button', 'choose', 'pick', 'apply', 'choose button'],
            'select button': ['select', 'choose', 'pick', 'apply', 'choose button'],
            'choose': ['select', 'select button', 'pick', 'choose button'],
            'choose button': ['select', 'select button', 'choose', 'pick'],
            'pick': ['select', 'choose', 'select button', 'choose button'],
            # Verification and message-related synonyms
            'success': ['success message', 'confirmation', 'confirmation message', 'success text'],
            'success message': ['success', 'confirmation', 'confirmation message', 'success text'],
            'confirmation': ['success', 'success message', 'confirmation message'],
            'confirmation message': ['success', 'success message', 'confirmation'],
            'message': ['text', 'notification', 'alert', 'banner'],
            'text': ['message', 'notification', 'alert', 'content'],
            'notification': ['message', 'text', 'alert', 'banner'],
            'alert': ['message', 'text', 'notification', 'banner'],
            # Cookie and extraction synonyms
            'extract': ['extract cookies', 'get', 'retrieve', 'capture', 'collect', 'obtain'],
            'extract cookies': ['extract', 'get cookies', 'retrieve cookies', 'capture cookies', 'collect cookies'],
            'get': ['extract', 'retrieve', 'capture', 'obtain', 'fetch'],
            'retrieve': ['extract', 'get', 'capture', 'obtain', 'fetch'],
            'capture': ['extract', 'get', 'retrieve', 'collect', 'obtain'],
            'collect': ['extract', 'get', 'retrieve', 'capture', 'obtain'],
            'cookies': ['cookie data', 'session data', 'browser cookies', 'web cookies'],
            'current page': ['this page', 'active page', 'loaded page', 'displayed page'],
            # Mock API related synonyms
            'mock': ['mock api', 'mock response', 'mock service', 'mocking', 'intercept'],
            'mock api': ['mock', 'mock response', 'mock service', 'api mock', 'intercept api'],
            'mock response': ['mock', 'mock api', 'response mock', 'mocked response', 'mock config'],
            'mocks': ['mock', 'mock api', 'mock response', 'mocking tool'],
            'intercept': ['mock', 'mock api', 'intercept request', 'mock response']
   
        }

        # Define incompatible UI element types that should never match
        # Note: 'button' removed from 'select' incompatible list to allow "Select button" patterns
        self.incompatible_ui_types = {
            'textbox': ['checkbox', 'radio', 'button', 'link'],
            'input': ['checkbox', 'radio', 'button', 'link'],
            'field': ['checkbox', 'radio', 'button', 'link'],
            'checkbox': ['textbox', 'input', 'field', 'radio', 'dropdown', 'select'],
            'radio': ['textbox', 'input', 'field', 'checkbox', 'dropdown', 'select'],
            'button': ['textbox', 'input', 'field', 'checkbox', 'radio'],
            'btn': ['textbox', 'input', 'field', 'checkbox', 'radio'],
            'link': ['textbox', 'input', 'field', 'checkbox', 'radio'],
            'dropdown': ['checkbox', 'radio', 'textbox', 'input', 'field'],
            'select': ['checkbox', 'radio', 'textbox', 'input', 'field'],  # 'button' removed to allow "Select button" patterns
        }

    def validate_llm_response(self,
                            original_step: str,
                            llm_response: 'MCPPlayWrightLLMResponse',
                            step_action: str = None,
                            step_element: str = None,
                            test_data: str = None) -> MCPStepValidation:
        """
        Main validation method to verify LLM response matches original step intent.

        Args:
            original_step: The original BDD step (e.g., "click login button")
            llm_response: The parsed LLM response with tool and input
            step_action: Extracted action from step (e.g., "click")
            step_element: Extracted element from step (e.g., "login button")
            test_data: Any test data associated with the step

        Returns:
            MCPStepValidation: Validation result with confidence score and details
        """

        # Defensive check for llm_response.input
        if not isinstance(llm_response.input, dict):
            self.logger.warning(f"⚠️ LLM response input is not dict, converting: {llm_response.input} (type: {type(llm_response.input).__name__})")

            if isinstance(llm_response.input, str):
                # Try to parse if it's JSON string
                try:
                    import json
                    parsed_input = json.loads(llm_response.input)
                    # Ensure the parsed result is a dict
                    if isinstance(parsed_input, dict):
                        llm_response.input = parsed_input
                    else:
                        # JSON parsed but not a dict (could be list, number, etc.)
                        llm_response.input = {'element': str(parsed_input)}
                except (json.JSONDecodeError, TypeError):
                    # Not valid JSON, treat as string element
                    llm_response.input = {'element': llm_response.input}
            else:
                # Non-string, non-dict input - convert to dict
                llm_response.input = {'element': str(llm_response.input)}

        # 1. Extract step components if not provided
        if not step_action or not step_element:
            parsed_action, parsed_element = self._parse_step_components(original_step)
            step_action = step_action or parsed_action
            step_element = step_element or parsed_element

        # NAVIGATION DOMAIN GUARD: Enforce module vs applicationBaseURL for navigate actions
        if isinstance(step_action, str) and step_action.lower().strip() == 'navigate':
            is_allowed, error_report = self._enforce_allowed_navigation(original_step, step_element, test_data, llm_response)
            if not is_allowed:
                return MCPStepValidation(
                    is_valid=False,
                    confidence_score=0.0,
                    action_match=False,
                    element_match=False,
                    intent_match=False,
                    validation_details=error_report,
                    suggested_corrections={
                        'reason': 'Navigation host not allowed for this module',
                        'action': 'Update onboardingConfiguration.json applicationBaseURL or correct the URL in the step'
                    },
                    allow_proceed=False
                )

        # 2. Validate action mapping
        action_valid, is_action_fallback = self._validate_action_match(step_action, llm_response.tool)

        # SPECIAL CASE: Mock-only steps don't validate against UI elements
        # Mock steps have config files as "elements", not actual UI elements
        is_mock_only = step_action.lower() == 'mock' and self._is_config_file(step_element)
        if is_mock_only:
            self.logger.info(f"🎭 MOCK-ONLY STEP DETECTED: Element is config file '{step_element}', not a UI element")
            # Skip element matching validation for mock-only steps
            element_valid = True
        else:
            # 3. Validate element identification
            element_valid = self._validate_element_match(
                step_element,
                llm_response.input.get('element', ''),
                test_data
            )

        # CRITICAL: If element validation failed due to mismatch, force failure
        if not element_valid and step_element and llm_response.input.get('element', ''):
            # Check if this was a critical mismatch (not just similarity issue)
            step_norm = step_element.lower().strip()
            llm_norm = llm_response.input.get('element', '').lower().strip()

            # Critical mismatch keywords - be more specific to avoid false positives
            critical_mismatches = [
                (['proceed', 'checkout', 'continue', 'next'], ['close', 'cancel', 'dismiss', 'exit']),
                (['submit', 'send', 'confirm'], ['close', 'cancel', 'back', 'dismiss']),
                (['add', 'cart'], ['remove', 'delete', 'clear', 'close']),
                (['sign in', 'login', 'log in'], ['sign up', 'register', 'create account']),
                (['navigate', 'go to', 'visit'], ['close', 'dismiss', 'cancel']),
            ]

            is_critical_mismatch = False
            for step_keywords, conflicting_keywords in critical_mismatches:
                step_has_keyword = any(keyword in step_norm for keyword in step_keywords)
                # Use word boundaries for conflicting keywords to avoid false positives
                llm_has_conflicting = any(
                    re.search(r'\b' + re.escape(keyword) + r'\b', llm_norm)
                    for keyword in conflicting_keywords
                )

                if step_has_keyword and llm_has_conflicting:
                    # Create pattern outside f-string to avoid backslash issue
                    matching_conflicts = [kw for kw in conflicting_keywords if re.search(r'\b' + re.escape(kw) + r'\b', llm_norm)]
                    self.logger.error(f"🚫 CRITICAL MISMATCH DETECTED:")
                    self.logger.error(f"   Step wants: {step_element} (contains {[kw for kw in step_keywords if kw in step_norm]})")
                    self.logger.error(f"   LLM selected: {llm_response.input.get('element', '')} (contains {matching_conflicts})")
                    self.logger.error(f"   This action is BLOCKED to prevent wrong interaction")
                    is_critical_mismatch = True
                    break

            if is_critical_mismatch:
                # Force validation to fail with 0 confidence
                return MCPStepValidation(
                    is_valid=False,
                    confidence_score=0.0,
                    action_match=action_valid,
                    element_match=False,
                    intent_match=False,
                    validation_details=f"🚫 CRITICAL ELEMENT MISMATCH: Step '{step_element}' vs LLM '{llm_response.input.get('element', '')}' - Action blocked to prevent wrong interaction",
                    suggested_corrections={
                        'reason': 'Critical element mismatch detected',
                        'expected': step_element,
                        'received': llm_response.input.get('element', ''),
                        'action': 'Step execution blocked - LLM should select correct element'
                    }
                )

        # 4. Calculate confidence scores
        action_score = 100.0 if action_valid else 0.0
        # Apply penalty for suboptimal tool choices
        if action_valid and is_action_fallback:
            action_score = 60.0  # Reduced confidence for fallback tools
            self.logger.info(f"🎯 CONFIDENCE PENALTY: Applied 60% confidence for fallback tool '{llm_response.tool}' on '{step_action}' action")
        element_score = self._calculate_element_confidence(
            step_element,
            llm_response.input.get('element', ''),
            test_data
        )

        # 5. Intent-based validation
        intent_valid = self._validate_intent_match(step_element, llm_response.input.get('element', ''))
        intent_score = 100.0 if intent_valid else 0.0

        # 6. Special handling for navigation, API services, store operations, and other non-element actions
        is_navigation_or_special = step_action.lower() in ['navigate', 'wait', 'refresh', 'back', 'forward', 'resize', 'invoke_service', 'invoke', 'store_response', 'store', 'modify_request', 'add', 'set','extract', 'extract_cookies', 'evaluate', 'mock']

        # 6a. Special handling for text verification scenarios
        is_text_verification = False
        if (step_action.lower() == 'verify' and
            llm_response.tool in ['browser_verify_text_visible', 'mcp_playwright_browser_verify_text_visible'] and
            'text' in llm_response.input and
            llm_response.input['text']):

            # More robust text matching - normalize both texts for comparison
            step_text_normalized = step_element.lower().strip('"\'., ').replace('.', '').replace(',', '').replace('!', '').replace('?', '')
            llm_text_normalized = llm_response.input['text'].lower().replace('.', '').replace(',', '').replace('!', '').replace('?', '')

            # Check if texts match (either direction)
            is_text_verification = (
                step_text_normalized in llm_text_normalized or
                llm_text_normalized in step_text_normalized or
                step_text_normalized == llm_text_normalized
            )

            self.logger.info(f"📝 TEXT VERIFICATION CHECK:")
            self.logger.info(f"   Step Action: '{step_action}'")
            self.logger.info(f"   LLM Tool: '{llm_response.tool}'")
            self.logger.info(f"   Step Element: '{step_element}'")
            self.logger.info(f"   Step Text Normalized: '{step_text_normalized}'")
            self.logger.info(f"   LLM Text: '{llm_response.input.get('text', '')}'")
            self.logger.info(f"   LLM Text Normalized: '{llm_text_normalized}'")
            self.logger.info(f"   Text Match Result: {is_text_verification}")

        if is_text_verification:
            self.logger.info(f"📝 TEXT VERIFICATION SCENARIO DETECTED: Step wants to verify text '{step_element}', LLM found text '{llm_response.input['text']}'")
            # For text verification, the important thing is that the text content matches, not the element description
            # Boost confidence significantly if the text content from step is found in LLM text parameter
            text_match_confidence = 95.0  # High confidence for text content match
            overall_confidence = (action_score * 0.3 + text_match_confidence * 0.7)
            is_valid = action_valid  # Only require action match for text verification
            self.logger.info(f"📝 TEXT VERIFICATION: Boosted confidence to {overall_confidence}% due to text content match")

        # 7. Overall validation decision
        if is_navigation_or_special:
            # For navigation/special actions, only validate action match
            overall_confidence = action_score
            is_valid = action_valid
        elif is_text_verification:
            # Already handled above in text verification section
            pass  # Values already set
        else:
            # For regular actions, validate both action and element
            # Only apply intent weighting if intent matching is actually relevant (not False due to normal UI)
            if intent_valid:
                # When intent match is true, give it some weight
                overall_confidence = (action_score * 0.4 + element_score * 0.4 + intent_score * 0.2)
            else:
                # When intent match is false (normal case), focus on action and element matching
                # But heavily penalize cases where element score is extremely low (clear mismatches)
                # Exception: Don't penalize Select button patterns which have their own confidence scoring
                llm_element = llm_response.input.get('element', '').lower()
                is_select_button_pattern = (
                    ('select' in step_element.lower() and 'button' in llm_element) or
                    ('button' in step_element.lower() and 'select' in llm_element) or
                    ('select' in step_element.lower() and 'select' in llm_element)
                )

                if element_score < 20.0 and not is_select_button_pattern:
                    # For clear element mismatches, cap confidence at 40% regardless of action match
                    overall_confidence = min(40.0, (action_score * 0.3 + element_score * 0.7))
                else:
                    # Normal case with reasonable element matching or special patterns
                    overall_confidence = (action_score * 0.5 + element_score * 0.5)
            is_valid = (
                action_valid and
                (element_score >= self.similarity_threshold or intent_valid)
            )

        # 7. Generate validation details
        validation_details = self._generate_validation_report(
            original_step, step_action, step_element, llm_response,
            action_valid, element_score, intent_valid
        )

        # 8. Determine recovery strategy and proceed decision
        recovery_strategy, allow_proceed = self._determine_recovery_strategy(
            is_valid, overall_confidence, action_valid, element_score, intent_valid,
            step_action, step_element, llm_response
        )

        # 9. Suggest corrections if validation fails
        suggested_corrections = None
        if not is_valid:
            suggested_corrections = self._suggest_corrections(
                step_action, step_element, llm_response, action_valid, element_score
            )

        return MCPStepValidation(
            is_valid=is_valid,
            confidence_score=overall_confidence,
            action_match=action_valid,
            element_match=(element_score >= self.similarity_threshold or is_text_verification),
            intent_match=intent_valid,
            validation_details=validation_details,
            suggested_corrections=suggested_corrections,
            recovery_strategy=recovery_strategy,
            allow_proceed=allow_proceed
        )

    # ---------- Logging helpers (centralized, best-effort) ----------
    def log_validation_details(self, validation_result: Any, logger: Optional[logging.Logger] = None) -> None:
        """Best-effort logging of validation details without affecting control flow."""
        active_logger = logger or self.logger
        try:
            if getattr(validation_result, "validation_details", None):
                active_logger.warning(str(validation_result.validation_details))
        except Exception:
            # Logging must never crash the flow
            pass

    def log_suggested_corrections(self, validation_result: Any, logger: Optional[logging.Logger] = None) -> None:
        """Best-effort logging of suggested corrections (items only; caller prints header)."""
        active_logger = logger or self.logger
        try:
            corrections = getattr(validation_result, "suggested_corrections", None)
            if not corrections:
                return
            for correction_type, details in corrections.items():
                try:
                    if isinstance(details, dict):
                        suggestion = details.get('suggestion', details)
                    else:
                        suggestion = details
                    active_logger.warning(f"  - {correction_type}: {suggestion}")
                except Exception:
                    active_logger.warning(f"  - {correction_type}: {details}")
        except Exception:
            # Logging must never crash the flow
            pass

    def _extract_hostname_from_value(self, value: Optional[str]) -> Optional[str]:
        """Best-effort extraction of a hostname from a string value."""
        if not isinstance(value, str):
            return None
        raw = value.strip().strip('"\'');
        if not raw:
            return None
        try:
            from urllib.parse import urlparse
            # If value lacks scheme, add dummy scheme to parse netloc
            parsed = urlparse(raw if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', raw) else f"http://{raw}")
            host = parsed.netloc or parsed.path.split('/')[0]
            # Strip port if present
            host = host.split(':')[0]
            host = host.strip().lower()
            # Basic sanity: must contain a dot or be localhost
            if host and ('.' in host or host == 'localhost'):
                return host
            return None
        except Exception:
            return None

    def _host_is_allowed_for_module(self, host: str) -> tuple[bool, Optional[str]]:
        """Check if the given host is permitted by applicationBaseURL for the current module.
        Returns (is_allowed, matched_base) where matched_base is the allowed base that matched.
        """
        allowed = get_allowed_hosts_for_current_domain()
        if not allowed:
            return False, None
        host_lc = (host or '').strip().lower()
        if not host_lc:
            return False, None
        # Allow exact match or subdomain match with dot-boundary
        for base in allowed:
            if host_lc == base:
                return True, base
            if host_lc.endswith(f".{base}"):
                return True, base
        return False, None

    def _enforce_allowed_navigation(self, original_step: str, step_element: Optional[str], test_data: Optional[str], llm_response) -> tuple[bool, str]:
        """Validate that navigation target host is allowed for the current module."""
        # Derive candidate URL/host from multiple sources
        candidates: list[str] = []
        if isinstance(step_element, str) and step_element.strip():
            candidates.append(step_element.strip())
        if isinstance(test_data, str) and test_data.strip():
            candidates.append(test_data.strip())
        try:
            if isinstance(llm_response, object) and isinstance(getattr(llm_response, 'input', None), dict):
                for key in ['url', 'href', 'to', 'target']:
                    v = llm_response.input.get(key)
                    if isinstance(v, str) and v.strip():
                        candidates.append(v.strip())
        except Exception:
            pass

        # Extract first viable hostname
        host = None
        for cand in candidates:
            host = self._extract_hostname_from_value(cand)
            if host:
                break

        # If we cannot extract a hostname, block to be safe
        if not host:
            allowed_list = sorted(list(get_allowed_hosts_for_current_domain()))
            msg = "🚫 NAVIGATION BLOCKED: Could not determine a valid URL/host from the step to validate against applicationBaseURL."
            if allowed_list:
                msg += f"\nAllowed hosts for module: {allowed_list}"
            else:
                msg += "\nNo allowed hosts configured for the module (applicationBaseURL is missing/empty)."
            return False, msg

        # Check host against allowlist
        is_allowed, matched_base = self._host_is_allowed_for_module(host)
        if not is_allowed:
            allowed_list = sorted(list(get_allowed_hosts_for_current_domain()))
            return False, (
                f"🚫 NAVIGATION BLOCKED: Host '{host}' is not allowed for this module.\n"
                f"Step: '{original_step}'\n"
                f"Allowed hosts (applicationBaseURL): {allowed_list if allowed_list else '[]'}"
            )

        # Positive, professional log on successful allow-match
        try:
            self.logger.info(
                f"✅ NAVIGATION ALLOWED: Host '{host}' matches allowed base '{matched_base}' "
                f"for the current module"
            )
        except Exception:
            pass

        return True, ""

    def _parse_step_components(self, step: str) -> tuple[str, str]:
        """Parse step into action and element components using generalized semantic approach"""
        import re

        # Handle "action-element" format first
        if '-' in step and not step.lower().startswith(('when', 'then', 'given', 'and')):
            parts = step.split('-', 1)
            return parts[0].strip(), parts[1].strip()

        # Use generalized semantic parsing instead of hardcoded patterns
        action, target = self._extract_action_and_target_semantically(step)
        return action, target

    def _extract_action_and_target_semantically(self, step: str) -> tuple[str, str]:
        """Extract action and target using semantic analysis instead of hardcoded patterns"""
        import re

        step_lower = step.lower().strip()

        # Remove common BDD prefixes
        step_clean = re.sub(r'^(?:when|then|given|and)\s+(?:user\s+|the\s+user\s+)?', '', step_lower)

        # Define action verb mappings with their semantic meanings
        action_verbs = {
            # Navigation actions
            'navigate': ['navigate', 'navigates', 'go', 'goes', 'open', 'opens', 'load', 'browse'],
            'switch': ['switch', 'change', 'move'],

            # Interaction actions
            'click': ['click', 'clicks', 'tap', 'taps', 'choose', 'chooses', 'press', 'presses', 'hit', 'hits', 'add', 'adds', 'proceed', 'proceeds', 'continue', 'continues'],
            'type': ['enter', 'enters', 'type', 'types', 'input', 'inputs', 'fill', 'fills', 'populate', 'populates', 'write', 'writes'],
            'hover': ['hover', 'mouse over', 'mouseover'],
            'wait': ['wait', 'pause', 'delay'],

            # Selection actions (for dropdowns) - higher priority for select keywords
            'select': ['select', 'selects', 'choose from', 'pick from'],

            # Verification actions
            'verify': ['verify', 'verifies', 'check', 'checks', 'validate', 'validates', 'confirm', 'confirms', 'ensure', 'ensures', 'assert', 'asserts'],

            # Browser actions
            'resize': ['resize', 'set size', 'change size'],

             # Cookie and data extraction actions
            'extract': ['extract', 'extracts', 'get', 'gets', 'retrieve', 'retrieves', 'capture', 'captures', 'collect', 'collects'],
            'extract_cookies': ['extract cookies', 'extracts cookies', 'get cookies', 'retrieve cookies', 'capture cookies'],
    
            # JavaScript evaluation actions
            'evaluate': ['evaluate', 'evaluates', 'execute', 'executes', 'run', 'runs'],

            # API actions
            'invoke_service': ['invoke', 'invokes', 'call', 'calls', 'execute', 'executes', 'trigger', 'triggers'],
            'store_response': ['store', 'stores', 'save', 'saves', 'capture', 'captures'],
            'modify_request': ['modify', 'modifies', 'update', 'updates', 'change', 'changes', 'add header', 'adds header', 'set header', 'sets header'],
            
            # Mock API actions
            'mock': ['mock', 'mocks', 'mocking', 'intercept', 'intercepts']
        }

        # Find the best matching action based on semantic similarity
        detected_action = self._find_best_action_match(step_clean, action_verbs)

        # Extract target element/object from the step
        target = self._extract_target_from_step(step_clean, detected_action)

        self.logger.debug(f"🔍 ACTION PARSING: '{step}' -> action='{detected_action}', target='{target}'")

        return detected_action, target

    def _find_best_action_match(self, step: str, action_verbs: dict) -> str:
        """Find the best matching action based on verb presence and context"""
        step_words = step.split()

        # Score each action based on how well it matches the step
        action_scores = {}

        for action, verbs in action_verbs.items():
            score = 0

            # Check for direct verb matches
            for verb in verbs:
                if verb in step:
                    score += 10
                    # Bonus for exact word matches
                    if verb in step_words:
                        score += 5

            # Context-based scoring - order matters for disambiguation
            if action == 'click' and any(word in step for word in ['clicks', 'click on', 'button', 'link', 'adds item', 'add to cart', 'proceeds', 'proceed']):
                score += 20  # High priority for click patterns including complex actions
            elif action == 'verify' and any(word in step for word in ['verifies', 'verify', 'displayed', 'visible', 'present', 'that text', 'shown', 'appears', 'is displayed']):
                score += 25  # Very high priority for verification patterns - higher than click
            elif action == 'switch' and any(word in step for word in ['tab', 'window', 'switch to']) and 'click' not in step:
                score += 15  # Only if not a click action
            elif action == 'navigate' and any(phrase in step for phrase in ['navigate to', 'navigates to', 'go to', 'goes to', 'open url', 'browse to', 'https://', 'http://']) and not any(word in step for word in ['clicks', 'adds', 'proceeds', 'enters', 'types', 'fills']):
                score += 12  # More specific navigation phrases, but exclude input actions
            elif action == 'type' and any(word in step for word in ['fills', 'enters', 'types', 'enter', 'fill', 'type']):
                score += 20  # Higher priority for input actions to beat navigation
            elif action == 'invoke_service' and any(phrase in step for phrase in ['invoke service', 'invokes service', 'call service', 'api service']) and not any(word in step for word in ['fills', 'enters', 'types']):
                score += 15  # More specific service invocation
            elif action == 'store_response' and (
                (any(word in step for word in ['response', 'stores', 'store']) and any(phrase in step for phrase in ['from response', 'as ']))
                or any(phrase in step for phrase in ['stores "', 'store "', 'save response', 'capture response'])
            ) and 'click' not in step:
                score += 30  # Very high priority for API storage patterns - should beat select
            elif action == 'resize' and any(phrase in step for phrase in ['resize browser', 'resizes browser', 'browser resize', 'window resize']) and 'verif' not in step:
                score += 15  # More specific browser resizing
            elif action == 'select' and any(word in step for word in ['select', 'selects', 'choose', 'chooses', ' as ', 'option', 'dropdown', 'from']) and not any(phrase in step for phrase in ['from response', 'stores "', 'store "']):
                score += 25  # Very high priority for select actions - should beat click, but exclude API storage patterns
            elif action == 'modify_request' and any(phrase in step for phrase in ['add header', 'adds header', 'set header', 'sets header', 'header authorization', 'authorization header', 'bearer token', 'modify request']):
                score += 35  # Very high priority for header modification - should beat click which might match "add"
            elif action == 'extract' and any(word in step for word in ['cookies', 'extract cookies', 'cookie data', 'session']):
                score += 15
            elif action == 'extract_cookies' and any(word in step for word in ['cookies', 'extract', 'current page', 'from']):
                score += 20
            elif action == 'evaluate' and any(word in step for word in ['evaluate', 'evaluates', 'execute', 'executes', 'run', 'runs']):
                score += 30  # High priority for evaluate actions
            elif action == 'mock' and any(phrase in step for phrase in ['mock', 'mocks', 'mocking', 'intercept', 'mock api']):
                score += 40  # Very high priority for mock actions - should beat other action types including API service calls
            action_scores[action] = score

        # Debug logging for score calculation
        self.logger.debug(f"🎯 ACTION SCORES for step '{step}': {action_scores}")

        # Return the highest scoring action, default to 'click' if no clear match
        best_action = max(action_scores.items(), key=lambda x: x[1])

        self.logger.debug(f"🏆 BEST ACTION: {best_action[0]} (score: {best_action[1]})")

        if best_action[1] > 0:
            return best_action[0]
        else:
            # Improved fallback logic - be more context-aware
            # Check for input/type context first (high priority)
            if any(word in step for word in ['enters', 'enter', 'types', 'type', 'fills', 'fill', 'inputs', 'input']):
                return 'type'
            # Check for verification context (high priority)
            elif any(word in step for word in ['verifies', 'verify', 'displayed', 'visible', 'present', 'shown', 'appears']):
                return 'verify'
            # Check for navigation context (only when clear navigation indicators exist)
            elif any(phrase in step for phrase in ['navigate to', 'navigates to', 'go to', 'goes to', 'open url', 'browse to']):
                return 'navigate'
            # Check for specific URL patterns  
            elif any(pattern in step for pattern in ['http://', 'https://', '.com', '.org', '.net']):
                return 'navigate'
            # Check for tab/window switching
            elif any(word in step for word in ['tab', 'window']) and 'switch' in step:
                return 'switch'
            else:
                return 'click'  # Default fallback

    def _extract_target_from_step(self, step: str, action: str) -> str:
        """Extract the target element/object from the step based on action context"""
        import re

        self.logger.debug(f"Extracting target from step: '{step}' with action: '{action}'")

        # Handle different action types with context-aware extraction
        if action == 'type':
            # For type actions, extract the field/input target (not the value)
            patterns = [
                # Pattern: user enters "VALUE" in the "FIELD" field -> extract FIELD
                r'(?:enters|types?)\s+["\']([^"\']+?)["\']?\s+(?:in|into)\s+(?:the\s+)?["\']([^"\']+?)["\']?\s*(?:field|textbox|input|box)',
                # Pattern: user enters "FIELD_NAME" as "VALUE" -> extract FIELD_NAME (must have "as")
                r'(?:enters|types?)\s+["\']([^"\']+?)["\']?\s+as\s+',
                # Pattern: in the "FIELD" field
                r'(?:in|into)\s+(?:the\s+)?["\']([^"\']+?)["\']?\s*(?:field|textbox|input|box)',
                # Pattern: enters "FIELD" field
                r'(?:enters|types?)\s+["\']([^"\']+?)["\']?\s*(?:field|textbox|input|box)',
                # Pattern: "FIELD" textbox
                r'["\']([^"\']+?)["\']\s+(?:field|textbox|input|box)',
            ]
            for i, pattern in enumerate(patterns):
                match = re.search(pattern, step, re.IGNORECASE)
                if match:
                    # Pattern 0 specifically handles "VALUE in FIELD" with two groups
                    if i == 0 and match.lastindex >= 2:
                        result = match.group(2).strip()
                        self.logger.debug(f"Pattern {i}: extracted FIELD '{result}' from VALUE in FIELD pattern")
                        return result  # Return FIELD, not VALUE
                    else:
                        result = match.group(1).strip()
                        self.logger.debug(f"Pattern {i}: extracted '{result}' from standard pattern")
                        return result

            # Fallback: if no field pattern found, look for quoted strings
            quoted_matches = re.findall(r'["\']([^"\']+?)["\']', step)
            self.logger.debug(f"Quoted matches found: {quoted_matches}")
            if len(quoted_matches) >= 2:
                # For "enters VALUE in FIELD", prioritize FIELD (second match) if pattern suggests so
                if ' in ' in step or ' into ' in step:
                    result = quoted_matches[1].strip()
                    self.logger.debug(f"Fallback: using FIELD '{result}' from position 1")
                    return result  # Return FIELD, not VALUE
            if len(quoted_matches) >= 1:
                result = quoted_matches[0].strip()
                self.logger.debug(f"Fallback: using first quoted '{result}'")
                return result

        elif action == 'click':
            # For click actions, extract the clickable element
            patterns = [
                # Pattern: clicks on "ELEMENT" button/link
                r'(?:clicks?|taps?)\s+(?:on\s+)?["\']([^"\']+?)["\']?\s*(?:button|link|element|icon)',
                # Pattern: "ELEMENT" button/link
                r'["\']([^"\']+?)["\']\s+(?:button|link|element|icon)',
                # Pattern: clicks on "ELEMENT" (general)
                r'(?:clicks?|taps?)\s+(?:on\s+)?["\']([^"\']+?)["\']',
                # Pattern: clicks on ELEMENT button (non-quoted)
                r'(?:clicks?|taps?)\s+(?:on\s+)?(\w+)\s+(?:button|link|element|icon)',
                # Pattern: clicks ELEMENT button (non-quoted)
                r'(?:clicks?|taps?)\s+(\w+)\s+(?:button|link|element|icon)',
            ]
            for i, pattern in enumerate(patterns):
                match = re.search(pattern, step, re.IGNORECASE)
                if match:
                    result = match.group(1).strip()
                    self.logger.debug(f"Click Pattern {i}: extracted '{result}'")
                    return result

            # Fallback: look for quoted strings first
            quoted_matches = re.findall(r'["\']([^"\']+?)["\']', step)
            self.logger.debug(f"Click quoted matches found: {quoted_matches}")
            if len(quoted_matches) >= 1:
                result = quoted_matches[0].strip()
                self.logger.debug(f"Click fallback: using first quoted '{result}'")
                return result

        elif action == 'select':
            # For select actions, extract the dropdown/selector element (not the value being selected)
            patterns = [
                # Pattern: select "ELEMENT" as "VALUE" -> extract ELEMENT
                r'(?:selects?|chooses?)\s+["\']([^"\']+?)["\']?\s+as\s+["\']([^"\']+?)["\']',
                # Pattern: selects "VALUE" from "SELECTOR" -> extract SELECTOR
                r'(?:selects?|chooses?)\s+["\']([^"\']+?)["\']?\s+(?:from|in)\s+(?:the\s+)?["\']([^"\']+?)["\']?\s*(?:dropdown|select|menu|picker|list)',
                # Pattern: selects "VALUE" from "SELECTOR" (without suffix)
                r'(?:selects?|chooses?)\s+["\']([^"\']+?)["\']?\s+(?:from|in)\s+(?:the\s+)?["\']([^"\']+?)["\']',
                # Pattern: from "SELECTOR" dropdown
                r'(?:from|in)\s+(?:the\s+)?["\']([^"\']+?)["\']?\s*(?:dropdown|select|menu|picker|list)',
                # Pattern: "SELECTOR" dropdown
                r'["\']([^"\']+?)["\']\s+(?:dropdown|select|menu|picker|list)',
            ]
            for i, pattern in enumerate(patterns):
                match = re.search(pattern, step, re.IGNORECASE)
                if match:
                    # Pattern 0: "select ELEMENT as VALUE" - return ELEMENT (first group)
                    if i == 0:
                        result = match.group(1).strip()
                        self.logger.debug(f"Select Pattern {i}: extracted ELEMENT '{result}' from select-as pattern")
                        return result  # Return ELEMENT, not VALUE
                    # Pattern 1 and 2 have two groups (VALUE, SELECTOR) - return SELECTOR
                    elif i <= 2 and match.lastindex >= 2:
                        result = match.group(2).strip()
                        self.logger.debug(f"Select Pattern {i}: extracted SELECTOR '{result}' from VALUE/SELECTOR pattern")
                        return result  # Return SELECTOR, not VALUE
                    else:
                        result = match.group(1).strip()
                        self.logger.debug(f"Select Pattern {i}: extracted '{result}' from single group")
                        return result

            # Fallback: if no selector pattern found, look for quoted strings
            quoted_matches = re.findall(r'["\']([^"\']+?)["\']', step)
            self.logger.debug(f"Select quoted matches found: {quoted_matches}")
            if len(quoted_matches) >= 2:
                # For "selects VALUE from SELECTOR", prioritize SELECTOR (second match)
                if ' from ' in step or ' in ' in step:
                    result = quoted_matches[1].strip()
                    self.logger.debug(f"Select fallback: using SELECTOR '{result}' from position 1")
                    return result  # Return SELECTOR, not VALUE

            # Look for selector context words even without quotes
            if ' from ' in step.lower():
                # Pattern: "selects VALUE from SELECTOR_TYPE"
                from_parts = step.lower().split(' from ')
                if len(from_parts) > 1:
                    selector_part = from_parts[1].strip()
                    # Clean up and return the selector part
                    result = re.sub(r'^(the\s+)?', '', selector_part)  # Remove 'the'
                    self.logger.debug(f"Select from-pattern: extracted '{result}'")
                    return result

            if len(quoted_matches) >= 1:
                # If only one quoted string, check if it's more likely a selector
                quote = quoted_matches[0].strip().lower()
                selector_indicators = ['dropdown', 'menu', 'select', 'picker', 'list', 'option', 'filter']
                if any(indicator in step.lower() for indicator in selector_indicators):
                    # Likely a selector context, but we got the value - use fallback
                    if 'dropdown' in step.lower():
                        return 'dropdown'
                    elif 'menu' in step.lower():
                        return 'menu'
                    elif 'filter' in step.lower():
                        return 'filter'
                    elif 'select' in step.lower():
                        return 'select'
                    elif 'picker' in step.lower():
                        return 'picker'
                result = quote
                self.logger.debug(f"Select fallback: using first quoted '{result}'")
                return result

        elif action == 'verify':
            # For verify actions, extract what's being verified
            patterns = [
                r'(?:that\s+)?["\']([^"\']+?)["\']?\s+(?:button|link|element|text)?\s*(?:is\s+)?(?:displayed|visible|present)',
                r'(?:verify|check|validate|confirm|ensure)\s+(?:that\s+)?["\']?([^"\']+?)["\']?(?:\s+(?:button|link|element))?',
            ]
            for pattern in patterns:
                match = re.search(pattern, step, re.IGNORECASE)
                if match:
                    return match.group(1).strip()

        elif action == 'switch':
            # For switch actions, the target is usually tab/window related
            if 'new tab' in step:
                return 'new tab'
            elif 'tab' in step:
                return 'tab'
            elif 'window' in step:
                return 'window'
            return step

        elif action == 'navigate':
            # For navigation, extract URL or page name
            patterns = [
                r'(?:to|visit|open|load)\s+["\']?([^"\']+?)["\']?$',
                r'["\']([^"\']+?)["\']',
            ]
            for pattern in patterns:
                match = re.search(pattern, step, re.IGNORECASE)
                if match:
                    return match.group(1).strip()

        # General extraction - look for quoted strings or last significant words
        quoted_match = re.search(r'["\']([^"\']+)["\']', step)
        if quoted_match:
            return quoted_match.group(1).strip()

        # Improved fallback: extract meaningful element words
        # Remove common action words and user-related words to extract the target element
        cleaned_step = re.sub(r'(?:when|then|and|user|users?|clicks?|taps?|enters?|types?|selects?|navigates?|goes?|visit|open|load|in|into|on|at|to|the|a|an|adds?|proceeds?|continue|item)\s+', '', step, flags=re.IGNORECASE).strip()

        # If we have some meaningful text left, return it
        if cleaned_step and len(cleaned_step) > 0:
            # Further clean up: remove trailing descriptors if they exist
            cleaned_step = re.sub(r'\s+(?:button|link|field|textbox|input|dropdown|menu|element|box)$', '', cleaned_step, flags=re.IGNORECASE).strip()
            self.logger.debug(f"Fallback extraction: '{step}' → '{cleaned_step}'")
            return cleaned_step

        # Ultimate fallback: return original step
        return step

    def _validate_action_match(self, step_action: str, llm_tool: str) -> tuple[bool, bool]:
        """Validate that LLM selected tool matches intended action

        Returns:
            tuple: (is_valid, is_fallback) where is_fallback indicates suboptimal tool choice
        """
        # Generalized action mappings - now more flexible with semantic actions
        action_mappings = {
            'click': ['browser_click', 'mcp_playwright_browser_click', 'browser_evaluate'],
            'type': ['browser_type', 'mcp_playwright_browser_type'],
            'select': ['browser_select_option', 'mcp_playwright_browser_select_option'],
            'navigate': ['browser_navigate', 'mcp_playwright_browser_navigate', 'mcp_playwright_browser_goto'],
            'verify': ['browser_verify_text_visible', 'mcp_playwright_browser_verify_text_visible', 'browser_verify_element_visible'],
            'hover': ['browser_hover', 'mcp_playwright_browser_hover'],
            'wait': ['browser_wait_for', 'mcp_playwright_browser_wait_for'],
            'resize': ['browser_resize', 'mcp_playwright_browser_resize'],
            'switch': ['browser_tabs', 'mcp_playwright_browser_tabs'],
            'extract': ['custom_browser_extract_cookie_tool', 'custom_extract_cookies_tool', 'extract_cookies_tool', 'browser_evaluate'],
            'extract_cookies': ['custom_browser_extract_cookie_tool', 'custom_extract_cookies_tool', 'extract_cookies_tool', 'browser_evaluate'],
            'evaluate': ['browser_evaluate'],
            'invoke_service': ['custom_api_service_tool', 'api_service_tool', 'service_tool'],
            'store_response': ['custom_store_response_tool', 'store_response_tool', 'store_tool'],
            'modify_request': ['custom_modify_request_tool', 'modify_request_tool', 'request_modifier_tool'],
            'mock': ['custom_mock_api_tool', 'mcp_custom_mock_api_tool', 'browser_mock']
        }

        step_action_norm = step_action.lower().strip()
        llm_tool_norm = llm_tool.lower().strip()

        expected_tools = action_mappings.get(step_action_norm, [])

        # If no direct mapping exists, try to infer from tool name
        if not expected_tools:
            # Try reverse mapping - if tool matches semantic intent, allow it
            tool_to_action = {
                'browser_click': 'click',
                'mcp_playwright_browser_click': 'click',
                'browser_type': 'type',
                'mcp_playwright_browser_type': 'type',
                'browser_select_option': 'select',
                'mcp_playwright_browser_select_option': 'select',
                'browser_navigate': 'navigate',
                'mcp_playwright_browser_navigate': 'navigate',
                'mcp_playwright_browser_goto': 'navigate',
                'browser_verify_text_visible': 'verify',
                'mcp_playwright_browser_verify_text_visible': 'verify',
                'browser_verify_element_visible': 'verify',
                'browser_hover': 'hover',
                'mcp_playwright_browser_hover': 'hover',
                'browser_wait_for': 'wait',
                'mcp_playwright_browser_wait_for': 'wait',
                'browser_resize': 'resize',
                'mcp_playwright_browser_resize': 'resize',
                'browser_tabs': 'switch',
                'mcp_playwright_browser_tabs': 'switch'
            }

            tool_action = tool_to_action.get(llm_tool_norm)
            if tool_action and self._actions_are_semantically_similar(step_action_norm, tool_action):
                return True, False  # Valid match through semantic similarity

        # Check for exact matches first (preferred)
        exact_match = llm_tool_norm in expected_tools or any(
            llm_tool_norm == tool.lower() or
            llm_tool_norm.replace('browser_', '').replace('mcp_playwright_', '') == tool.lower().replace('browser_', '').replace('mcp_playwright_', '')
            for tool in expected_tools
        )

        if exact_match:
            return True, False  # Valid, not a fallback

        # Check for acceptable fallbacks with logging
        fallback_mappings = {
            # Removed 'verify': ['browser_click'] - verification should never use click
            # This prevents LLM from using click tools for verification steps
        }

        fallback_tools = fallback_mappings.get(step_action_norm, [])
        is_fallback = llm_tool_norm in fallback_tools

        if is_fallback:
            self.logger.warning(f"⚠️ SUBOPTIMAL TOOL CHOICE: Using '{llm_tool}' for '{step_action}' action. Consider using: {expected_tools}")
            return True, True  # Valid, but is a fallback

        return False, False  # Invalid

    def _actions_are_semantically_similar(self, action1: str, action2: str) -> bool:
        """Check if two actions are semantically similar"""
        # Define semantic similarity groups
        similarity_groups = [
            {'click', 'tap', 'select', 'choose', 'press'},
            {'type', 'enter', 'input', 'fill', 'populate', 'write'},
            {'navigate', 'go', 'visit', 'open', 'load'},
            {'verify', 'check', 'validate', 'confirm', 'ensure'},
            {'switch', 'change', 'move', 'tab'},
            {'hover', 'mouseover'},
            {'wait', 'pause', 'delay'},
            {'resize', 'change size', 'set size'},
            {'store', 'save', 'capture'},
            {'invoke', 'call', 'execute', 'trigger'},
            {'modify', 'update', 'change', 'set'}
        ]

        # Check if both actions are in the same similarity group
        for group in similarity_groups:
            if action1 in group and action2 in group:
                return True

        return False

    def _is_config_file(self, element: str) -> bool:
        """Check if the element is a configuration file (used in mock-only steps)"""
        if not element:
            return False
        return element.lower().endswith('.json') or 'mock' in element.lower()

    def _validate_element_match(self, step_element: str, llm_element: str, test_data: str = None) -> bool:
        """Enhanced element validation with strict mismatched action prevention"""
        if not step_element or not llm_element:
            return True  # No element to validate

        # Check for incompatible UI element types with context awareness
        step_words = set(step_element.lower().split())
        llm_words = set(llm_element.lower().split())

        # Special case: If step contains "select" and LLM contains "button", check if it's a "Select button" pattern
        if 'select' in step_words and 'button' in llm_words:
            # This is likely a "Select button" scenario, not a dropdown vs button mismatch
            if ('select' in step_element.lower() and 'button' in llm_element.lower()) or \
               ('select' in llm_element.lower() and 'button' in step_element.lower()):
                self.logger.debug(f"Select button pattern detected in validation, skipping incompatibility check")
            else:
                # Check for other incompatible combinations
                for step_word in step_words:
                    if step_word in self.incompatible_ui_types:
                        for llm_word in llm_words:
                            if llm_word in self.incompatible_ui_types[step_word] and llm_word != 'button':
                                self.logger.debug(f"Incompatible UI types: '{step_word}' vs '{llm_word}'")
                                return False
        else:
            # Normal incompatibility check for other combinations
            for step_word in step_words:
                if step_word in self.incompatible_ui_types:
                    for llm_word in llm_words:
                        if llm_word in self.incompatible_ui_types[step_word]:
                            self.logger.debug(f"Incompatible UI types: '{step_word}' vs '{llm_word}'")
                            return False

        # STRICT VALIDATION: Prevent obvious mismatched actions
        step_norm = step_element.lower().strip()
        llm_norm = llm_element.lower().strip()

        # Critical mismatch detection - BLOCK these actions (consistent with validate_llm_response)
        critical_mismatches = [
            # If step wants "proceed to checkout" but LLM wants to close/cancel
            (['proceed', 'checkout', 'continue', 'next'], ['close', 'cancel', 'dismiss', 'exit']),
            # If step wants "submit" but LLM wants to cancel
            (['submit', 'send', 'confirm'], ['close', 'cancel', 'back', 'dismiss']),
            # If step wants "add to cart" but LLM wants to remove
            (['add', 'cart'], ['remove', 'delete', 'clear', 'close']),
            # If step wants "sign in" but LLM wants to sign up/register
            (['sign in', 'login', 'log in'], ['sign up', 'register', 'create account']),
            # If step wants navigation but LLM wants modal actions
            (['navigate', 'go to', 'visit'], ['close', 'dismiss', 'cancel']),
        ]

        for step_keywords, conflicting_keywords in critical_mismatches:
            step_has_keyword = any(keyword in step_norm for keyword in step_keywords)
            # Use word boundaries for conflicting keywords to avoid false positives (consistent with validate_llm_response)
            llm_has_conflicting = any(
                re.search(r'\b' + re.escape(keyword) + r'\b', llm_norm)
                for keyword in conflicting_keywords
            )

            if step_has_keyword and llm_has_conflicting:
                # Create pattern outside f-string to avoid backslash issue
                matching_conflicts = [kw for kw in conflicting_keywords if re.search(r'\b' + re.escape(kw) + r'\b', llm_norm)]
                self.logger.error(f"🚫 CRITICAL MISMATCH DETECTED:")
                self.logger.error(f"   Step wants: {step_element} (contains {[k for k in step_keywords if k in step_norm]})")
                self.logger.error(f"   LLM selected: {llm_element} (contains {matching_conflicts})")
                self.logger.error(f"   This action is BLOCKED to prevent wrong interaction")
                return False

        # SEMANTIC CONTEXT VALIDATION: Ensure actions make sense in context
        # If step is clearly about progressing forward, LLM shouldn't go backward
        forward_actions = ['proceed', 'continue', 'next', 'submit', 'confirm', 'complete', 'finish']
        backward_actions = ['back', 'previous', 'cancel', 'close', 'dismiss', 'exit']

        # Use word boundaries to avoid false positives (e.g., "next" in "Next button" shouldn't trigger backward)
        step_is_forward = any(re.search(r'\b' + re.escape(action) + r'\b', step_norm) for action in forward_actions)
        llm_is_backward = any(re.search(r'\b' + re.escape(action) + r'\b', llm_norm) for action in backward_actions)

        if step_is_forward and llm_is_backward:
            self.logger.error(f"🚫 SEMANTIC MISMATCH DETECTED:")
            self.logger.error(f"   Step is forward action: {step_element}")
            self.logger.error(f"   LLM selected backward action: {llm_element}")
            self.logger.error(f"   This contradicts the intended flow direction")
            return False

        # Strategy 1: Exact match (case insensitive)
        if step_norm == llm_norm:
            return True

        # Strategy 2: Enhanced word overlap with UI descriptor handling
        # Remove common UI descriptors for better matching
        ui_descriptors = {'button', 'textbox', 'input', 'field', 'link', 'dropdown', 'select', 'checkbox', 'radio'}

        step_words = set(word for word in step_norm.split() if word not in ui_descriptors)
        llm_words = set(word for word in llm_norm.split() if word not in ui_descriptors)

        if step_words and llm_words:
            overlap = step_words.intersection(llm_words)
            min_words = min(len(step_words), len(llm_words))

            # More permissive: 50% word overlap for validation
            overlap_percentage = len(overlap) / min_words if min_words > 0 else 0
            if overlap_percentage >= 0.5:
                return True

            # Special case: if step has all core words in LLM element, accept it
            if step_words.issubset(llm_words):
                return True

        # Strategy 3: Word-boundary contains relationship (prevent false substring matches)
        # Use word boundaries to ensure complete word matches, not substrings
        step_words_set = set(step_norm.split())
        llm_words_set = set(llm_norm.split())

        # Check if step words are contained as complete words in LLM element
        if step_words_set and step_words_set.issubset(llm_words_set):
            return True

        # Check if LLM words are contained as complete words in step element
        if llm_words_set and llm_words_set.issubset(step_words_set):
            return True

        # Strategy 4: Synonym match
        if self._validate_synonym_match(step_element, llm_element):
            return True

        # Strategy 5: Intent-based matching (with stricter criteria)
        if self._validate_intent_match(step_element, llm_element):
            # Additional check: ensure intent match isn't contradictory
            if not (step_is_forward and llm_is_backward):
                return True

        # Strategy 6: Semantic action compatibility (with additional similarity requirements)
        # Allow compatible forward-forward or backward-backward actions ONLY if elements are related
        llm_is_forward = any(re.search(r'\b' + re.escape(action) + r'\b', llm_norm) for action in forward_actions)

        if step_is_forward and llm_is_forward:
            # Both are forward actions - require additional similarity to prevent mismatches
            # Check for word overlap or contains relationship before allowing
            step_words = set(word for word in step_norm.split() if word not in {'button', 'textbox', 'input', 'field', 'link', 'dropdown', 'select', 'checkbox', 'radio'})
            llm_words = set(word for word in llm_norm.split() if word not in {'button', 'textbox', 'input', 'field', 'link', 'dropdown', 'select', 'checkbox', 'radio'})

            # Require at least one shared meaningful word OR contains relationship
            shared_words = step_words.intersection(llm_words) - {'to', 'the', 'a', 'an', 'and', 'or', 'with', 'for'}
            has_contains_relationship = (step_norm in llm_norm and len(step_norm) >= 3) or (llm_norm in step_norm and len(llm_norm) >= 3)

            if shared_words or has_contains_relationship:
                return True

        step_is_backward = any(re.search(r'\b' + re.escape(action) + r'\b', step_norm) for action in backward_actions)
        if step_is_backward and llm_is_backward:
            # Both are backward actions - require additional similarity to prevent mismatches
            step_words = set(word for word in step_norm.split() if word not in {'button', 'textbox', 'input', 'field', 'link', 'dropdown', 'select', 'checkbox', 'radio'})
            llm_words = set(word for word in llm_norm.split() if word not in {'button', 'textbox', 'input', 'field', 'link', 'dropdown', 'select', 'checkbox', 'radio'})

            # Require at least one shared meaningful word OR contains relationship
            shared_words = step_words.intersection(llm_words) - {'to', 'the', 'a', 'an', 'and', 'or', 'with', 'for'}
            has_contains_relationship = (step_norm in llm_norm and len(step_norm) >= 3) or (llm_norm in step_norm and len(llm_norm) >= 3)

            if shared_words or has_contains_relationship:
                return True

        return False

    def _calculate_element_confidence(self, step_element: str, llm_element: str, test_data: str = None) -> float:
        """Calculate confidence score for element matching"""
        self.logger.info(f"🔍 CONFIDENCE CALCULATION START:")
        self.logger.info(f"   Step Element: '{step_element}'")
        self.logger.info(f"   LLM Element: '{llm_element}'")
        self.logger.info(f"   Test Data: '{test_data}'")

        if not step_element or not llm_element:
            self.logger.info(f"   → Empty elements, returning 100.0")
            return 100.0  # High confidence for navigation/special actions without elements

        # Check for incompatible UI element types with context awareness
        step_words = set(step_element.lower().split())
        llm_words = set(llm_element.lower().split())

        # Special case: If step contains "select" and LLM contains "button", check if it's a "Select button" pattern
        if 'select' in step_words and 'button' in llm_words:
            # This is likely a "Select button" scenario, not a dropdown vs button mismatch
            if ('select' in step_element.lower() and 'button' in llm_element.lower()) or \
               ('select' in llm_element.lower() and 'button' in step_element.lower()):
                self.logger.info(f"   → Select button pattern detected, skipping incompatibility check")
            else:
                # Check for other incompatible combinations
                for step_word in step_words:
                    if step_word in self.incompatible_ui_types:
                        for llm_word in llm_words:
                            if llm_word in self.incompatible_ui_types[step_word] and llm_word != 'button':
                                self.logger.info(f"   → Incompatible UI types: '{step_word}' vs '{llm_word}', returning 5.0")
                                return 5.0
        else:
            # Normal incompatibility check for other combinations
            for step_word in step_words:
                if step_word in self.incompatible_ui_types:
                    for llm_word in llm_words:
                        if llm_word in self.incompatible_ui_types[step_word]:
                            self.logger.info(f"   → Incompatible UI types: '{step_word}' vs '{llm_word}', returning 5.0")
                            return 5.0  # Very low confidence for incompatible UI types

        # Normalize for comparison
        step_norm = step_element.lower().strip()
        llm_norm = llm_element.lower().strip()

        self.logger.info(f"   Normalized - Step: '{step_norm}', LLM: '{llm_norm}'")

        # Perfect match (case insensitive)
        if step_norm == llm_norm:
            self.logger.info(f"   → Perfect match found, returning 100.0")
            return 100.0

        # Enhanced special case: "Select" step with "Select button" LLM response - common UI pattern
        # Handle various combinations: "select" <-> "select button", "Select" <-> "Select button", etc.
        if (('select' in step_norm and 'select' in llm_norm and 'button' in llm_norm) or \
           ('select' in llm_norm and 'select' in step_norm and ('button' in step_norm or 'button' in llm_norm)) or \
           (step_norm == 'select' and llm_norm == 'select button') or \
           (step_norm == 'select button' and llm_norm == 'select') or \
           ('select button' in step_norm and 'select' in llm_norm) or \
           ('select' in step_norm and 'select button' in llm_norm)):
            self.logger.info(f"   → Enhanced select button pattern match found, returning 95.0")
            return 95.0

        # Enhanced partial match scoring with field descriptor normalization
        # Remove common field descriptors for better matching
        field_descriptors = ['field', 'input field', 'textbox', 'input', 'box', 'labeled', 'label']

        # Create normalized versions without field descriptors
        step_norm_no_desc = step_norm
        llm_norm_no_desc = llm_norm

        for desc in field_descriptors:
            step_norm_no_desc = step_norm_no_desc.replace(f' {desc}', '').replace(f'{desc} ', '').strip()
            llm_norm_no_desc = llm_norm_no_desc.replace(f' {desc}', '').replace(f'{desc} ', '').strip()

        # Clean up quotes and special characters that break tokenization
        step_norm_clean = step_norm_no_desc.replace('"', '').replace("'", '').replace('*', '').strip()
        llm_norm_clean = llm_norm_no_desc.replace('"', '').replace("'", '').strip()

        self.logger.info(f"   Normalized without descriptors - Step: '{step_norm_no_desc}', LLM: '{llm_norm_no_desc}'")

        # Check for matches after removing field descriptors
        if (step_norm_no_desc and llm_norm_no_desc and
            (step_norm_no_desc in llm_norm_no_desc or llm_norm_no_desc in step_norm_no_desc)):
            self.logger.info(f"   → Field descriptor normalized match found, returning 90.0")
            return 90.0

        # Enhanced clean match - after removing quotes and special chars
        if (step_norm_clean and llm_norm_clean and
            (step_norm_clean in llm_norm_clean or llm_norm_clean in step_norm_clean)):
            self.logger.info(f"   → Clean normalized match found ('{step_norm_clean}' <-> '{llm_norm_clean}'), returning 88.0")
            return 88.0

        # Special handling for form field indicators (*, +, !, etc.)
        # These are common in form fields but don't affect element matching
        form_indicators = ['*', '+', '!', '(required)', '(optional)', 'required', 'optional']
        step_without_indicators = step_norm_clean
        for indicator in form_indicators:
            step_without_indicators = step_without_indicators.replace(indicator, '').strip()
        
        if (step_without_indicators and llm_norm_clean and
            (step_without_indicators in llm_norm_clean or llm_norm_clean in step_without_indicators)):
            self.logger.info(f"   → Form field indicator match found ('{step_without_indicators}' <-> '{llm_norm_clean}'), returning 85.0")
            return 85.0

        # Original substring matching
        if step_norm in llm_norm or llm_norm in step_norm:
            # High confidence for partial matches (e.g. "add to cart" in "add to cart button")
            self.logger.info(f"   → Direct substring match found, returning 90.0")
            return 90.0

        # Check if step words are subset of LLM words (ignoring UI descriptors)
        ui_descriptors = {'button', 'textbox', 'input', 'field', 'link', 'dropdown', 'select', 'checkbox', 'radio', 'labeled', 'label'}
        
        # Clean tokenization - remove quotes, special chars, and form indicators
        step_words_clean = step_norm_clean.split() if step_norm_clean else []
        llm_words_clean = llm_norm_clean.split() if llm_norm_clean else []
        
        step_words = set(word for word in step_words_clean if word not in ui_descriptors and word not in ['*', '+', '!', '?'])
        llm_words = set(word for word in llm_words_clean if word not in ui_descriptors)

        self.logger.info(f"🔍 CONFIDENCE DEBUG: step_norm='{step_norm}', llm_norm='{llm_norm}'")
        self.logger.info(f"🔍 CONFIDENCE DEBUG: step_words={step_words}, llm_words={llm_words}")

        if step_words and step_words.issubset(llm_words):
            # High confidence when step is subset of LLM (e.g. "user id" in "user id textbox")
            self.logger.info(f"🔍 CONFIDENCE DEBUG: Subset match found - returning 85.0")
            return 85.0

        # Synonym match bonus
        if self._validate_synonym_match(step_element, llm_element):
            self.logger.info(f"   → Synonym match found, returning 88.0")
            return 88.0

        # Base similarity score
        similarity_score = self._levenshtein_similarity(step_element, llm_element) * 100
        self.logger.info(f"   → Levenshtein similarity: {similarity_score}")

        # Bonus for exact test data match
        if test_data and test_data.strip().lower() == llm_norm:
            similarity_score = max(similarity_score, 95.0)
            self.logger.info(f"   → Test data match bonus applied: {similarity_score}")

        # Bonus for intent matching
        if self._validate_intent_match(step_element, llm_element):
            similarity_score = max(similarity_score, 85.0)
            self.logger.info(f"   → Intent match bonus applied: {similarity_score}")

         # Special case for cookie extraction steps - high confidence for cookie-related elements
        if ('cookie' in step_element.lower() and 'cookie' in llm_element.lower()):
            self.logger.info(f"   → Cookie extraction pattern match found, returning 95.0")
            return 95.0
    
        if ('current page' in step_element.lower() and any(word in llm_element.lower() for word in ['page', 'current', 'browser', 'context'])):
            self.logger.info(f"   → Current page pattern match found, returning 90.0")
            return 90.0
        
        # Special case for mock API steps - high confidence for mock-related elements
        if ('mock' in step_element.lower() and 'mock' in llm_element.lower()):
            self.logger.info(f"   → Mock API pattern match found, returning 95.0")
            return 95.0
        
        # Special case for mock config file names
        if step_element.lower().endswith('.json') and llm_element.lower().endswith('.json'):
            self.logger.info(f"   → Config file name match found, returning 90.0")
            return 90.0
    
        final_score = min(similarity_score, 100.0)
        self.logger.info(f"   → FINAL CONFIDENCE SCORE: {final_score}")
        return final_score

    def _validate_intent_match(self, step_element: str, llm_element: str) -> bool:
        """Validate intent-based matching for generic patterns"""
        try:
            step_norm = step_element.lower().strip().replace('-', ' ').replace('_', ' ')
            llm_norm = llm_element.lower().strip()

            # Check numeric patterns
            numeric_match = any(
                re.search(pattern, step_norm)
                for pattern in self.intent_patterns['numeric']
            )
            if numeric_match:
                return re.fullmatch(r'\d[\d\s,]*', llm_element.strip()) is not None

            # Check spatial patterns
            for keyword in self.intent_patterns['spatial']:
                if keyword in step_norm:
                    element_part = step_norm.split(keyword)[0].strip()
                    if element_part and (element_part in llm_norm or llm_norm in element_part):
                        return True

            return False

        except Exception:
            return False

    def _validate_synonym_match(self, step_element: str, llm_element: str) -> bool:
        """Validate element matching using UI element synonyms and semantic synonyms"""
        try:
            step_words = set(step_element.lower().split())
            llm_words = set(llm_element.lower().split())

            # First check semantic synonyms
            step_phrase = step_element.lower().strip()
            llm_phrase = llm_element.lower().strip()

            # Check exact semantic matches
            if step_phrase in self.semantic_synonyms:
                # Check if any synonyms match the LLM element
                for synonym in self.semantic_synonyms[step_phrase]:
                    if synonym in llm_phrase or llm_phrase in synonym:
                        self.logger.debug(f"Semantic synonym match: '{step_phrase}' matches '{llm_phrase}' via '{synonym}'")
                        return True

            if llm_phrase in self.semantic_synonyms:
                # Check if any synonyms match the step element
                for synonym in self.semantic_synonyms[llm_phrase]:
                    if synonym in step_phrase or step_phrase in synonym:
                        self.logger.debug(f"Semantic synonym match: '{step_phrase}' matches '{llm_phrase}' via '{synonym}'")
                        return True

            # Check individual words for semantic matches
            for step_word in step_words:
                if step_word in self.semantic_synonyms:
                    for synonym in self.semantic_synonyms[step_word]:
                        # Use word-boundary matching to prevent false positive substring matches
                        llm_phrase_words = set(llm_phrase.split())
                        synonym_words = set(synonym.split())
                        # Check if any synonym words exist as complete words in LLM phrase
                        if synonym_words.intersection(llm_phrase_words):
                            self.logger.debug(f"Word-level semantic match: '{step_word}' → '{synonym}' found in '{llm_phrase}'")
                            return True

            # Check if core element names match (ignoring UI type)
            step_core = step_words.copy()
            llm_core = llm_words.copy()

            # Remove UI element type words and check if core elements match
            ui_types = set()
            for synonyms in self.ui_element_synonyms.values():
                ui_types.update(synonyms)

            step_core = {word for word in step_core if word not in ui_types}
            llm_core = {word for word in llm_core if word not in ui_types}

            # If core elements match, check if UI types are synonymous
            # Require both cores to be non-empty AND equal to avoid false positives
            if step_core and llm_core and step_core == llm_core:
                step_ui_types = step_words - step_core
                llm_ui_types = llm_words - llm_core

                # Check if any UI types are synonymous
                for step_type in step_ui_types:
                    for llm_type in llm_ui_types:
                        if step_type in self.ui_element_synonyms:
                            if llm_type in self.ui_element_synonyms[step_type]:
                                self.logger.debug(f"UI element synonym match: '{step_type}' → '{llm_type}'")
                                return True
                        if llm_type in self.ui_element_synonyms:
                            if step_type in self.ui_element_synonyms[llm_type]:
                                self.logger.debug(f"UI element synonym match: '{llm_type}' → '{step_type}'")
                                return True

                # If core elements match and only one has UI type, consider it a match
                if len(step_ui_types) != len(llm_ui_types):
                    return True

            # Handle pure UI element synonym matching (when both cores are empty)
            if not step_core and not llm_core:
                # Both are pure UI types - check for direct synonyms
                step_ui_types = step_words
                llm_ui_types = llm_words

                # Check if any UI types are synonymous
                for step_type in step_ui_types:
                    for llm_type in llm_ui_types:
                        if step_type in self.ui_element_synonyms:
                            if llm_type in self.ui_element_synonyms[step_type]:
                                self.logger.debug(f"Pure UI synonym match: '{step_type}' → '{llm_type}'")
                                return True
                        if llm_type in self.ui_element_synonyms:
                            if step_type in self.ui_element_synonyms[llm_type]:
                                self.logger.debug(f"Pure UI synonym match: '{llm_type}' → '{step_type}'")
                                return True

            return False

        except Exception:
            return False

    def _levenshtein_similarity(self, str1: str, str2: str) -> float:
        """Calculate Levenshtein similarity between two strings"""
        try:
            import Levenshtein as lev
            distance = lev.distance(str1, str2)
            max_len = max(len(str1), len(str2))
            if max_len == 0:
                return 1.0
            return 1 - (distance / max_len)
        except ImportError:
            # Fallback implementation if Levenshtein not available
            return self._simple_similarity(str1, str2)

    def _simple_similarity(self, str1: str, str2: str) -> float:
        """Simple similarity fallback"""
        str1_norm = str1.lower().strip()
        str2_norm = str2.lower().strip()

        if str1_norm == str2_norm:
            return 1.0
        if str1_norm in str2_norm or str2_norm in str1_norm:
            return 0.8

        # Word overlap
        words1 = set(str1_norm.split())
        words2 = set(str2_norm.split())
        if words1 and words2:
            overlap = len(words1.intersection(words2))
            total = len(words1.union(words2))
            return overlap / total if total > 0 else 0.0

        return 0.0

    def _generate_validation_report(self,
                                  original_step: str,
                                  step_action: str,
                                  step_element: str,
                                  llm_response: 'MCPPlayWrightLLMResponse',
                                  action_valid: bool,
                                  element_score: float,
                                  intent_valid: bool) -> str:
        """Generate detailed validation report"""
        report = [
            f"🔍 MCP STEP VERIFICATION REPORT",
            f"Original Step: '{original_step}'",
            f"Parsed Action: '{step_action}' → LLM Tool: '{llm_response.tool}'",
            f"Parsed Element: '{step_element}' → LLM Element: '{llm_response.input.get('element', 'N/A')}'",
            f"",
            f"✅ Action Match: {action_valid}",
            f"📊 Element Similarity: {element_score:.1f}%",
            f"🎯 Intent Match: {intent_valid}",
            f"",
            f"LLM Input Parameters: {llm_response.input}"
        ]
        return "\n".join(report)

    def _suggest_corrections(self,
                           step_action: str,
                           step_element: str,
                           llm_response: 'MCPPlayWrightLLMResponse',
                           action_valid: bool,
                           element_score: float) -> Dict[str, Any]:
        """Suggest corrections for failed validation"""
        corrections = {}

        if not action_valid:
            # Get the expected tools for this action
            action_mappings = {
                'click': ['browser_click', 'mcp_playwright_browser_click', 'browser_evaluate'],
                'type': ['browser_type', 'mcp_playwright_browser_type'],
                'select': ['browser_select_option', 'mcp_playwright_browser_select_option'],
                'navigate': ['browser_navigate', 'mcp_playwright_browser_navigate', 'mcp_playwright_browser_goto'],
                'verify': ['browser_verify_text_visible', 'mcp_playwright_browser_verify_text_visible', 'browser_verify_element_visible'],
                'hover': ['browser_hover', 'mcp_playwright_browser_hover'],
                'wait': ['browser_wait_for', 'mcp_playwright_browser_wait_for'],
                'resize': ['browser_resize', 'mcp_playwright_browser_resize'],
                'switch': ['browser_tabs', 'mcp_playwright_browser_tabs'],
                'extract': ['custom_browser_extract_cookie_tool', 'custom_extract_cookies_tool', 'extract_cookies_tool'],
                'extract_cookies': ['custom_browser_extract_cookie_tool', 'custom_extract_cookies_tool', 'extract_cookies_tool'],
                'invoke_service': ['custom_api_service_tool', 'api_service_tool', 'service_tool'],
                'store_response': ['custom_store_response_tool', 'store_response_tool', 'store_tool'],
                'modify_request': ['custom_modify_request_tool', 'modify_request_tool', 'request_modifier_tool']
            }
            
            expected_tools = action_mappings.get(step_action.lower(), [f"browser_{step_action.lower()}"])
            expected_tool_names = " or ".join(expected_tools)
            
            corrections['action'] = {
                'issue': f"Tool '{llm_response.tool}' doesn't match action '{step_action}'",
                'suggestion': f"Expected tool: {expected_tool_names}"
            }

        if element_score < self.similarity_threshold:
            corrections['element'] = {
                'issue': f"Element mismatch (similarity: {element_score:.1f}%)",
                'expected': step_element,
                'actual': llm_response.input.get('element', 'N/A'),
                'suggestion': "Improve element identification or use alternative locators"
            }

        return corrections

    def _determine_recovery_strategy(self, is_valid: bool, confidence: float, action_match: bool,
                                   element_score: float, intent_match: bool, step_action: str,
                                   step_element: str, llm_response) -> tuple[str, bool]:
        """
        Determine recovery strategy based on validation context and attempt recovery scenarios.

        Returns:
            tuple[str, bool]: (recovery_strategy, allow_proceed)
        """

        # If validation passes, no recovery needed
        if is_valid:
            return "none", True

        # Context-aware recovery strategies

        # 1. TEXT VERIFICATION RECOVERY: Special handling for text verification scenarios
        if (step_action == 'verify' and
            llm_response.tool in ['browser_verify_text_visible', 'mcp_playwright_browser_verify_text_visible'] and
            'text' in llm_response.input and
            action_match):
            # For text verification, if action matches and we have the right tool, allow it
            # The text content match is more important than element description
            self.logger.info(f"📝 TEXT VERIFICATION RECOVERY: Action matches for text verification - allowing")
            return "text_verification", True

        # 2. RECTIFICATION CONTEXT: Allow navigation for verification steps
        if (step_action in ['verify', 'validate', 'check'] and
            llm_response.tool == 'browser_navigate' and
            not action_match):
            self.logger.info(f"🔄 RECOVERY: Navigation for verification - allowing rectification")
            return "navigate_to_verify", True

        # 3. MISSING TEXT: Allow navigation if trying to verify text that's not on current page
        if (step_action == 'verify' and
            'text' in llm_response.input and
            llm_response.tool == 'browser_navigate'):
            self.logger.info(f"🔄 RECOVERY: Text verification requires navigation - allowing")
            return "navigate_for_text", True

        # 4. PARTIAL MATCH WITH HIGH INTENT: Lower threshold for meaningful attempts
        if (action_match and element_score >= self.progressive_thresholds['lenient']):
            self.logger.info(f"🔄 RECOVERY: Partial match with decent confidence ({element_score:.1f}%) - allowing")
            return "partial_match", True

        # 5. EMERGENCY RECOVERY: Very low confidence but action matches
        if (action_match and element_score >= self.progressive_thresholds['emergency']):
            self.logger.warning(f"⚠️ EMERGENCY RECOVERY: Low confidence ({element_score:.1f}%) but action matches - allowing with caution")
            return "emergency", True

        # 5. SEMANTIC NAVIGATION: Navigation steps that make sense contextually
        if (llm_response.tool == 'browser_navigate' and
            any(keyword in step_element.lower() for keyword in ['payment', 'account', 'profile', 'settings', 'checkout'])):
            self.logger.info(f"🔄 RECOVERY: Contextual navigation for {step_element} - allowing")
            return "contextual_navigation", True

        # 6. IFRAME REDIRECTION: Allow element targeting when dealing with cross-origin issues
        if (action_match and 'iframe' in llm_response.input.get('element', '').lower()):
            self.logger.info(f"🔄 RECOVERY: Iframe element redirection - allowing")
            return "iframe_redirection", True

        # 7. STRICT VERIFICATION BLOCKING: Never allow click tools for verification steps
        if (step_action in ['verify', 'validate', 'check'] and
            llm_response.tool in ['browser_click', 'mcp_playwright_browser_click']):
            self.logger.error(f"❌ VERIFICATION TOOL MISMATCH: Verification steps must use verification tools, not click tools")
            return "verification_click_block", False

        # 8. FALLBACK TOOLS: Allow suboptimal but functional tools
        fallback_tools = {
            'click': ['browser_evaluate', 'browser_hover'],
            'type': ['browser_evaluate'],
            'select': ['browser_click']
        }

        if (step_action in fallback_tools and
            llm_response.tool in fallback_tools[step_action] and
            element_score >= self.progressive_thresholds['moderate']):
            self.logger.info(f"🔄 RECOVERY: Fallback tool strategy - allowing {llm_response.tool} for {step_action}")
            return "fallback_tool", True

        # 9. NO RECOVERY: Block dangerous mismatches
        if not action_match and element_score < self.progressive_thresholds['emergency']:
            self.logger.error(f"❌ NO RECOVERY: Dangerous mismatch - blocking")
            return "block", False

        # Default: Allow with warning for borderline cases
        self.logger.warning(f"⚠️ BORDERLINE RECOVERY: Allowing with reduced confidence")
        return "borderline", True

    async def _llm_validate_next_step_readiness(self, 
                                              current_step_index: int = 0, 
                                              total_steps: int = 1, 
                                              step_list: list = None,
                                              page_snapshot: str = "", 
                                              previous_snapshot: str = "",
                                              mcp_response: str = "") -> tuple[bool, str]:
        try:
            if current_step_index >= total_steps - 1:
                self.logger.info("✅ LLM NEXT STEP VALIDATION: Skipped (last step in sequence)")
                return True, ""
            if not step_list or current_step_index + 1 >= len(step_list):
                self.logger.info("✅ LLM NEXT STEP VALIDATION: Skipped (no next step available)")
                return True, ""           
            next_step = step_list[current_step_index + 1]
            current_step = step_list[current_step_index]
            if not next_step or not next_step.strip():
                self.logger.info("✅ LLM NEXT STEP VALIDATION: Skipped (next step is empty)")
                return True, ""           
            self.logger.info(f"🤖 LLM NEXT STEP VALIDATION: Analyzing readiness for: '{next_step}'")
            validation_prompt = self._create_next_step_validation_prompt(next_step,current_step, page_snapshot,previous_snapshot,mcp_response)           
            # Get LLM validation response
            validation_result, reason = await self._get_llm_next_step_validation(validation_prompt)          
            if validation_result:
                self.logger.info(f"✅ LLM NEXT STEP VALIDATION: SUCCESS - Next step '{next_step}' can be executed")
                return True, ""
            else:
                self.logger.warning(f"❌ LLM NEXT STEP VALIDATION: FAILED - Next step '{next_step}' cannot be executed. Reason: {reason}")
                return False, reason                
        except Exception as e:
            self.logger.warning(f"⚠️ LLM next step validation error: {e}")
            # If validation fails, assume success (don't double-penalize)
            return True, ""

    def _create_next_step_validation_prompt(self, next_step: str, current_step: str, page_snapshot: str, previous_snapshot: str, mcp_response: str) -> str: 
        prompt = f"""
TASK: You are an expert Playwright UI test automation analyst.Your task is to determine if the Check if the element/locator needed for the TEST STEP exists in the PAGE SNAPSHOT.

CURRENT PAGE SNAPSHOT:
"{page_snapshot}"

TEST STEP :
"{next_step}"

**CRITICAL VALIDATION RULE (check in order, stop at first success):**
1. IGNORE ALL FORMATTING and CAPITALIZATION while performing any text validation.
2. CALENDAR/DATE PICKER,DATE SELECTION,CALENDAR GRID**:Numbers like "31", "15", "20" in calendar grids ARE clickable date buttons - if the number exists in the calendar grid it means the button is present and clickable.If step mentions clicking date number from calendar section and that number appears in calendar grid and available then it can be clicked.
3. If Core element/locator  text or name from TEST STEP is partially present in PAGE SNAPSHOT → Return True
4. The presence of these elements, objects, or locators within an iframe or Shadow DOM in the current page snapshot will be considered a SUCCESS


**MANDATORY RESPONSE FORMAT - NO EXCEPTIONS ALLOWED**
1. Response MUST start with EXACTLY "True|" or "False|" - NO SPACES, NO OTHER WORDS 
2. NO additional text before True or False under any circumstances
3. ALL analysis sections, explanations, details will come after "True|" or "False|"
4. Format: "True|Detailed success reason" OR "False|Detailed failure reason"
5. IF SAME QUESTION ASKED MULTIPLE TIMES, RESPOND CONSISTENTLY WITH SAME FORMAT
**CRITICAL**: ANY deviation from this above MANDATORY RESPONSE FORMAT will cause system failure
"""       
        return prompt

    async def _get_llm_next_step_validation(self, validation_prompt: str) -> tuple[bool, str]:
        try:
            llm_instance = self._get_llm()
            from langchain_core.messages import HumanMessage
            response = await llm_instance.ainvoke([HumanMessage(content=validation_prompt)])
            response_text = str(response.content).strip().replace('\n', '')
            if '|' in response_text:
                parts = response_text.split('|', 1)
                result = parts[0].strip().lower()
                reason = parts[1].strip() if len(parts) > 1 else "Unknown reason"                                            
                if result == 'false':
                    return False, reason
                elif result == 'true':
                    return True, ""
            else:
                result = response_text.lower().strip()
                if result == 'true':
                    return True, ""
                elif result == 'false':
                    return False, "No reason provided"
            self.logger.warning(f"⚠️ LLM validation response unclear: '{response_text}', assuming success")
            return True, ""
        except Exception as e:
            self.logger.error(f"❌ LLM validation failed: {e}")
            return True, ""

    def _get_llm(self):
        """Get LLM instance for tool selection"""
        config = Config(read_timeout=60, connect_timeout=30)
        bedrock_client = boto3.client('bedrock-runtime', config=config, region_name="us-east-1")
        return ChatBedrockConverse(
            model=PLATFORM_CONFIG["BEDROCK_MODEL_ID"],
            client=bedrock_client,
            max_tokens=2000,
            temperature=0.9
        )
"""
Beautiful error response formatter with complete technical details
"""

import traceback
import logging
import logging
from datetime import datetime
from typing import Dict, Any
from utils.error_messages import get_error_message
from utils.constants import PLATFORM_CONFIG
import re
from utils.excepations import BaseCustomException
from services.prompt_layer.error_translation_prompt import ERROR_TRANSLATION_PROMPT

logger = logging.getLogger(__name__)


def get_detailed_error_analysis(raw_message: str, failure_step: str) -> Dict[str, Any]:
    """Get detailed analysis from LLM using direct Bedrock API for clean JSON"""
    try:
        import json
        import boto3
        from botocore.config import Config
        
        logger.info("📊 Calling LLM for detailed error analysis...")
        
        prompt = f"""Analyze test failure. Return valid JSON only:
        {{
            "root_cause": "what caused failure",
            "what_went_wrong": "what failed",
            "why_it_failed": "why",
            "suggested_fixes": ["fix1", "fix2", "fix3"],
            "technical_insights": "insights"
        }}

        Step: {failure_step}
        Error: {raw_message[:1500]}"""

        logger.info(f"📝 Prompt length: {len(prompt)} chars")
        
        config = Config(read_timeout=60, connect_timeout=30)
        client = boto3.client('bedrock-runtime', config=config, region_name="us-east-1")
        
        response = client.converse(
            modelId=PLATFORM_CONFIG["BEDROCK_MODEL_ID"],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": "Respond with ONLY valid JSON"}],
            inferenceConfig={"temperature": 0.1, "maxTokens": 1500}
        )
        
        result = response['output']['message']['content'][0]['text'].strip()
        logger.info(f"📨 LLM response length: {len(result)} chars")
        
        if result.startswith('```'):
            result = '\n'.join(l for l in result.split('\n') if not l.strip().startswith('```'))
        
        parsed = json.loads(result.strip())
        logger.info(f"✅ Detailed analysis complete - keys: {list(parsed.keys())}")
        return parsed
    except Exception as e:
        logger.error(f"❌ Detailed analysis failed: {e}")
        logger.error(f"❌ Error type: {type(e).__name__}")
        return {}


def translate_error_to_user_friendly(
    raw_message: str,
    failure_step: str = "NA",
    exception_type: str = "Unknown"
) -> str:
    """
    Translate a technical error message into a user-friendly message using LLM.
    Falls back to original message if translation fails.
    
    Args:
        raw_message: The raw technical error message
        failure_step: The step where the failure occurred
        exception_type: The type of exception
        
    Returns:
        A user-friendly error message
    """
    try:
        from services.llm_layer.LLM import LLM
        
        llm = LLM()
        
        # Call the LLM to translate the error
        user_friendly_message = llm.send_request(
            template_prompt=ERROR_TRANSLATION_PROMPT,
            input_variables=["exception_type", "failure_step", "raw_message"],
            input_variables_dict={
                "exception_type": exception_type,
                "failure_step": failure_step,
                "raw_message": raw_message[:2000]  # Limit to prevent token overflow
            },
            temperature=0.1  # Very low temperature for consistent, predictable output
        )
        
        # Clean up the response - remove any leading/trailing whitespace
        if user_friendly_message:
            user_friendly_message = user_friendly_message.strip()
            logger.info("Successfully translated error to user-friendly message")
            return user_friendly_message
        
        return raw_message
        
    except Exception as e:
        # If LLM translation fails, log the error and return original message
        logger.warning(f"Failed to translate error to user-friendly message: {str(e)}")
        return raw_message
from services.prompt_layer.error_translation_prompt import ERROR_TRANSLATION_PROMPT

logger = logging.getLogger(__name__)


def translate_error_to_user_friendly(
    raw_message: str,
    failure_step: str = "NA",
    exception_type: str = "Unknown"
) -> str:
    """
    Translate a technical error message into a user-friendly message using LLM.
    Falls back to original message if translation fails.
    
    Args:
        raw_message: The raw technical error message
        failure_step: The step where the failure occurred
        exception_type: The type of exception
        
    Returns:
        A user-friendly error message
    """
    try:
        from services.llm_layer.LLM import LLM
        
        llm = LLM()
        
        # Call the LLM to translate the error
        user_friendly_message = llm.send_request(
            template_prompt=ERROR_TRANSLATION_PROMPT,
            input_variables=["exception_type", "failure_step", "raw_message"],
            input_variables_dict={
                "exception_type": exception_type,
                "failure_step": failure_step,
                "raw_message": raw_message[:2000]  # Limit to prevent token overflow
            },
            temperature=0.1  # Very low temperature for consistent, predictable output
        )
        
        # Clean up the response - remove any leading/trailing whitespace
        if user_friendly_message:
            user_friendly_message = user_friendly_message.strip()
            logger.info("Successfully translated error to user-friendly message")
            return user_friendly_message
        
        return raw_message
        
    except Exception as e:
        # If LLM translation fails, log the error and return original message
        logger.warning(f"Failed to translate error to user-friendly message: {str(e)}")
        return raw_message


def format_beautiful_error(
        exc: Exception,
        request_path: str = None,
        request_method: str = None,
        request_id: str = None
) -> Dict[str, Any]:
    """Transform any exception into a beautiful response with full technical details"""


    def _filename_from_url_like(value: str) -> str:
        try:
            from urllib.parse import urlparse
            import os as _os
            if not isinstance(value, str) or not value:
                return "recording.zip"
            if value.startswith(("http://", "https://", "s3://", "file:")):
                parsed = urlparse(value)
                return _os.path.basename(parsed.path) or "recording.zip"
            if "/" in value:
                return value.rsplit('/', 1)[-1]
            return value
        except Exception:
            return "recording.zip"

    def _redact_recording_link(text: str) -> str:
        if not isinstance(text, str):
            return text
        def _repl(m: re.Match) -> str:
            prefix = m.group(1)
            link = m.group(2)
            return f"{prefix}{_filename_from_url_like(link)}"
        # Replace any RECORDING_ZIP: <url-or-path> with filename only
        return re.sub(r"(RECORDING_ZIP:\s*)(\S+)", _repl, text)

    def _sanitize_context(obj: Any) -> Any:
        """Recursively sanitize context to avoid exposing S3 links.
        - Replace any dict key 'zip_url' or 'recording_zip' with 'zip_filename' (basename only)
        - Replace any dict key that contains both 'zip' and 'url' (case-insensitive) with 'zip_filename'
        - EXCEPTION: Preserve 'download_zip_url', 'download_zip_filename', 'download_token' (intentional download fields)
        - Drop the original 'zip_url' key
        - Recurse into lists/dicts
        """
        try:
            # Dict: sanitize keys/values
            if isinstance(obj, dict):
                sanitized: Dict[str, Any] = {}
                for k, v in obj.items():
                    lk = k.lower() if isinstance(k, str) else k
                    if isinstance(lk, str):
                        # Preserve intentional download fields (for failure artifact downloads)
                        if lk in ("download_zip_url", "download_zip_filename", "download_token"):
                            sanitized[k] = v
                            continue
                        # Sanitize S3 recording URLs
                        if lk in ("zip_url", "recording_zip") or ("zip" in lk and "url" in lk):
                            sanitized["zip_filename"] = _filename_from_url_like(v)
                            # do not carry forward the URL field
                            continue
                    sanitized[k] = _sanitize_context(v)
                return sanitized
            # List/Tuple: sanitize each element
            if isinstance(obj, list):
                return [_sanitize_context(x) for x in obj]
            if isinstance(obj, tuple):
                return tuple(_sanitize_context(x) for x in obj)
            # Primitive/other: return as-is
            return obj
        except Exception:
            # On any sanitize error, return original object
            return obj

    # Get full traceback information and sanitize
    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tb_lines = [_redact_recording_link(line) for line in tb_lines]
    tb_info = traceback.extract_tb(exc.__traceback__)

    # Extract error location - FIND LAST FRAME IN YOUR APPLICATION CODE
    error_location = None
    if tb_info:
        # Define external library patterns to skip
        external_patterns = [
            'site-packages',
            'botocore',
            'langchain',
            'boto3',
            'requests',
            'urllib3',
            'httpx',
            'playwright',
            'asyncio',
            'concurrent'
        ]

        # Find the last frame that's in YOUR application code
        app_frames = []
        for frame in tb_info:
            # Skip external library frames
            is_external = any(pattern in frame.filename for pattern in external_patterns)
            if not is_external:
                app_frames.append(frame)

        # Use the last application frame, or fallback to the actual last frame
        target_frame = app_frames[-1] if app_frames else tb_info[-1]

        error_location = {
            "file": target_frame.filename.split('/')[-1],
            "function": target_frame.name,
            "code": target_frame.line,
            "is_application_code": len(app_frames) > 0
        }

    # Handle custom exceptions
    if isinstance(exc, BaseCustomException):
        error_info = get_error_message(exc.error_code)
        error_type = "user_error"
        error_code = exc.error_code
    else:
        # Handle unexpected system exceptions
        error_info = get_error_message("UNKNOWN_ERROR")
        error_type = "system_error"
        error_code = "UNKNOWN_ERROR"

    # Determine the failure step (where the exception occurred)
    failure_step = "NA"
    
    # Check if exception has a custom step attribute (BDD step from feature file)
    if hasattr(exc, 'step') and exc.step:
        failure_step = exc.step
    elif hasattr(exc, 'action') and exc.action:
        failure_step = exc.action
    
    # Get the raw error message and sanitize it
    raw_error_message = _redact_recording_link(str(exc))
    exception_type_name = type(exc).__name__
    
    # Translate technical error to user-friendly message
    user_friendly_message = translate_error_to_user_friendly(
        raw_message=raw_error_message,
        failure_step=failure_step,
        exception_type=exception_type_name
    )
    complete_raw_error = str(exc)
    detailed_analysis = get_detailed_error_analysis(raw_error_message, failure_step)
    
    # Get the raw error message and sanitize it
    raw_error_message = _redact_recording_link(str(exc))
    exception_type_name = type(exc).__name__
    
    # Translate technical error to user-friendly message
    user_friendly_message = translate_error_to_user_friendly(
        raw_message=raw_error_message,
        failure_step=failure_step,
        exception_type=exception_type_name
    )
    
    # Build beautiful response with technical details
    response = {
        "success": False,
        "error": {
            # 🎨 Beautiful user-facing information
            "message": error_info["message"],
            "suggestion": error_info["suggestion"],
            "user_action": error_info["user_action"],
            
            # 📍 Where the failure occurred
            "failure_step": failure_step,

            # 📊 Error classification
            "type": error_type,
            "code": error_code,
            "timestamp": datetime.now().isoformat(),

            # 🔧 Minimal technical details (user-friendly)
            "technical_details": {
                "exception_type": exception_type_name,
                "exception_module": exc.__class__.__module__,
                "raw_message": user_friendly_message,
                "complete_error_with_internal_details": complete_raw_error
            }
        }
    }
    
    # ✅ ADD DETAILED ANALYSIS TO RESPONSE
    if detailed_analysis:
        response["error"]["detailed_analysis"] = detailed_analysis
        logger.info(f"✅ Added detailed_analysis to response with {len(detailed_analysis)} keys")
    else:
        logger.warning("⚠️ detailed_analysis is empty - not adding to response")

    # Add context if available (for any exception with context attribute)
    if hasattr(exc, 'context') and exc.context:
        # Sanitize the context to ensure S3 links (e.g., zip_url) are not exposed
        response["error"]["context"] = _sanitize_context(exc.context)
        logger.info(f"📦 Added exception context to response ({len(exc.context)} fields)")

    # Add request information
    if request_path or request_method:
        response["error"]["request_info"] = {
            "path": request_path,
            "method": request_method,
            "request_id": request_id
        }

    # Add any custom attributes from exception
    if hasattr(exc, 'jira_id'):
        response["error"]["jira_id"] = exc.jira_id

    # Extract run ID from exception attribute or message
    if hasattr(exc, 'run_id'):
        response["error"]["run_id"] = exc.run_id
    else:
        # Try to extract from error message
        run_id_match = re.search(r'\[Run ID: ([^\]]+)\]', raw_error_message)
        if run_id_match:
            response["error"]["run_id"] = run_id_match.group(1)

    return response
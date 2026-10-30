import asyncio
import json
import os
import time
import traceback
from typing import Optional, Union, List, Literal, Any
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, Body, Query, status
from pydantic import BaseModel, AnyUrl, EmailStr
import smtplib
#from pygments.lexer import default
from services.validation_layer.gherkin_validator import validate_gherkin
from utils.functions import setup_logging
from fastapi import Depends, Request
from utils.temp_file_manager import temp_file_manager
from pydantic import Field
import requests
# Import the process_feature_files function
from services.integration_layer.playwright_script_generation.processing_layer.generate_bdd_script import \
    process_feature_files # Import necessary paths
from dotenv import load_dotenv
load_dotenv()
from enum import Enum
from typing import Optional, Tuple, Dict
from utils.excepations import UserInputError, GitHubIntegrationError, SystemError, BaseCustomException
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse, StreamingResponse
import os
import re
import logging
from utils.temp_file_manager import temp_file_manager
from services.interface_layer.aws_secret import get_secret
from utils.db_utils import _get_engine, PGSCHEMA, BASE_DATABASE_URL
from utils.db_utils import get_playwright_locator_by_uid_prefix
from services.integration_layer.mcp_integration.mcp_enhanced_utilization import _make_mcp_uid_base
from sqlalchemy import text
from datetime import datetime
from fastapi.responses import FileResponse
from fastapi import Path as FPath
from fastapi import Query as FQuery
from utils.recording_cleanup import _recording_base_dir, _ensure_under, _find_latest_zip_for
from utils.s3_utils import _get_s3_client, get_recordings_bucket_and_prefix
from utils.constants import PLATFORM_CONFIG
from services.integration_layer.notifications.email_notifier import send_run_summary_email
from utils.recording_cleanup import get_last_recording_url
from utils.analytics_tracker import track_execution
from fastapi.responses import JSONResponse
import httpx
import uuid
from datetime import datetime, timezone
from services.interface_layer.autoheal_update_service import AutoHealUpdateService
from sqlalchemy import text
from utils.db_utils import engine, PGSCHEMA
from services.validation_layer.stepExpand_validator import InstructionExpander
from zoneinfo import ZoneInfo
from services.migration_layer.steps_migration import migrate_steps_for_date_range
from utils.run_id_manager import (
    resolve_run_id,
    store_generated_response,
    update_response_status,
    create_response_summary_with_run_id
)
from utils.feature_metadata_extractor import (
    extract_feature_tags,
    extract_feature_name,
    extract_scenarios_metadata,
    build_step_locator_map,
    extract_api_steps_from_json_files
)


# Initialize module logger early so helper functions can log during import-time initialization
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/Web-Automation", tags=["recordings"])

# Add JSON path constants
base = os.path.dirname(os.path.abspath(__file__))            # .../src/services/interface_layer
root = os.path.dirname(os.path.dirname(base))               # .../src
REPO_MODULES_JSON_PATH = os.path.join(root, "onboardingConfiguration.json")

RECORDING_ZIP_RE = re.compile(r"\|\s*RECORDING_ZIP:\s*([^|]+?)\s*$")

def _load_module_domains_for_enum() -> List[str]:
    """Load distinct module/domain names from src/onboardingConfiguration.json for Swagger enum."""
    try:
        if not os.path.exists(REPO_MODULES_JSON_PATH):
            logger.warning(f"onboardingConfiguration.json not found at {REPO_MODULES_JSON_PATH}")
            return []
        with open(REPO_MODULES_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        domains: List[str] = []
        for item in data or []:
            if isinstance(item, dict) and item.get("domain"):
                domains.append(str(item["domain"]))
        # Ensure deterministic order and uniqueness
        return sorted(set(domains))
    except Exception as e:
        logger.warning(f"Failed to load module domains for Swagger enum: {e}")
        return []

def _build_module_enum_from(values: List[str]):
    """Dynamically build an Enum for module dropdown in Swagger from provided values."""
    if not values:
        return None
    # Create unique, safe member names while preserving original string values
    members = {}
    for v in values:
        safe_name = re.sub(r"[^A-Za-z0-9_]", "_", v).upper()
        if not safe_name or safe_name[0].isdigit():
            safe_name = f"V_{safe_name}" if safe_name else "VALUE"
        base_name = safe_name
        suffix = 2
        while safe_name in members:
            safe_name = f"{base_name}_{suffix}"
            suffix += 1
        members[safe_name] = v
    try:
        # Create a plain Enum with string values; FastAPI will expose the values as enum choices
        return Enum("ModuleEnum", members)
    except Exception as e:
        logger.warning(f"Failed to build ModuleEnum: {e}")
        return None

# Build the ModuleEnum at import time for Swagger to render a dropdown
_module_values_for_enum = _load_module_domains_for_enum()
ModuleEnum = _build_module_enum_from(_module_values_for_enum)

def _generate_internal_run_id() -> str:
    """
    Generate a run ID in format: internal_<uuid4hex> (lowercase, no hyphens).
    Example: internal_3f4a2c9e7b8d4a1c9e8f3b2a1d0c4e6f
    """
    return f"internal_{uuid.uuid4().hex}"

def _process_multiple_video_urls(zip_marker: str) -> str:
    """Process multiple video URLs from pipe-separated format and return filenames"""
    if not zip_marker:
        return ""
    
    # Split by pipe separator and process each URL
    urls = [url.strip() for url in zip_marker.split(" | ") if url.strip()]
    filenames = []
    
    for url in urls:
        filename = _zip_filename_from_url(url)
        if filename and filename != "recording.zip":  # Skip generic fallback names
            filenames.append(filename)
    
    # Return comma-separated filenames for the API response
    return ", ".join(filenames) if filenames else _zip_filename_from_url(urls[0] if urls else "")

def _extract_recording_zip_url(err: Exception) -> Optional[str]:
    m = RECORDING_ZIP_RE.search(str(err))
    return m.group(1) if m else None

def _abs_url(request: Request, url: str) -> str:
    base = str(request.base_url).rstrip("/")
    return url if url.startswith("http") else f"{base}{url if url.startswith('/') else '/' + url}"

def _normalize_zip_url_for_http(zip_url: str, request: Request) -> str:
    """
    Convert 'file:/.../xyz.zip' or '/recordings/xyz.zip' to an absolute http(s) URL
    based on the current request base URL, assuming you've mounted:
        app.mount("/recordings", StaticFiles(directory=<RECORDINGS_DIR>), name="recordings")
    """
    base_url = str(request.base_url).rstrip('/')
    if zip_url.startswith("file:"):
        zip_name = os.path.basename(zip_url)
        return f"{base_url}/recordings/{zip_name}"
    if zip_url.startswith("/recordings/"):
        return f"{base_url}{zip_url}"
    if zip_url.startswith("recordings/"):
        return f"{base_url}/{zip_url}"
    return zip_url

def _zip_filename_from_url(zip_url: str) -> str:
    """Extract a filename from various URL/path formats (http, /recordings/, file:, s3://)."""
    try:
        from urllib.parse import urlparse
        import os as _os
        if isinstance(zip_url, str):
            if " | " in zip_url:
                parts = [p.strip() for p in zip_url.split(" | ") if p.strip()]
                names = [_zip_filename_from_url(p) for p in parts]
                names = [n for n in names if n and n != "recording.zip"]
                return ", ".join(names) if names else "recording.zip"
            if zip_url.startswith(("http://", "https://", "s3://", "file:")):
                parsed = urlparse(zip_url)
                return _os.path.basename(parsed.path) or "recording.zip"
            if zip_url.startswith("/recordings/") or zip_url.startswith("recordings/"):
                return zip_url.rsplit('/', 1)[-1]
            if "," in zip_url:
                parts = [p.strip() for p in zip_url.split(",") if p.strip()]
                names = [_zip_filename_from_url(p) for p in parts]
                names = [n for n in names if n and n != "recording.zip"]
                return ", ".join(names) if names else "recording.zip"
            # Relative paths e.g. target/trace/foo.webm → foo.webm (must match S3 key basename)
            if "/" in zip_url or "\\" in zip_url:
                return _os.path.basename(zip_url.replace("\\", "/"))
            return zip_url
    except Exception:
        pass
    return "recording.zip"

def _split_recording_filenames(zip_name: str) -> List[str]:
    """Split recording filenames from either pipe-separated or comma-separated values."""
    if not isinstance(zip_name, str) or not zip_name.strip():
        return []
    normalized = zip_name.replace(" | ", ",")
    return [f.strip() for f in normalized.split(",") if f.strip()]

# --- Helper: Extract raw Gherkin steps (Given/When/Then/And/*) from a feature file ---
_GHERKIN_PREFIXES = ("Given ", "When ", "Then ", "And ", "* ")
def _extract_gherkin_steps_text(feature_text: str) -> List[str]:
    steps: List[str] = []
    if not feature_text:
        return steps
    for raw in feature_text.splitlines():
        line = raw.strip()
        # Skip tags, comments, empty lines
        if not line or line.startswith("#") or line.startswith("@"):
            continue
        # Capture only step lines
        for p in _GHERKIN_PREFIXES:
            if line.startswith(p):
                steps.append(line)
                break
    return steps

def _get_scenario_name_from_tran_status(run_id: str, mcp_crawler: bool, log_context: str = "") -> Optional[str]:
    """
    Extract scenario name from tran_status table if available.
    Centralizes the duplicated pattern for retrieving scenario names.
    
    Args:
        run_id: The run ID to look up
        mcp_crawler: Whether MCP crawler is being used (only fetches if True)
        log_context: Optional context string for logging (e.g., "failure storage", "success storage")
    
    Returns:
        Scenario name string or None if not available
    """
    if not mcp_crawler or not run_id:
        return None
    
    try:
        from utils.run_id_manager import get_tran_status
        tran_data = get_tran_status(run_id)
        if tran_data:
            scenario_name = tran_data.get("scenario_name")
            if scenario_name and log_context:
                logger.info(f"📋 Scenario name for {log_context}: {scenario_name}")
            return scenario_name
    except Exception as e:
        if log_context:
            logger.warning(f"⚠️  Could not retrieve scenario name for {log_context}: {str(e)}")
    return None



# Get the project root directory - REMOVE THIS
# PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent.parent.absolute()

class FeatureValidationResponse(BaseModel):
    """Response model for feature file validation"""
    responseCode: int = Field(..., description="HTTP response status code")
    is_valid: bool = Field(..., description="Whether the feature file has valid Gherkin structure")
    message: str = Field(..., description="Validation result message")
    issues: Optional[List[Dict[str, Any]]] = Field(default=[], description="List of validation issues if any")

class FeatureUploadResponse(BaseModel):
    """Response model for feature file upload"""
    jira_id: str
    status: str
    message: str
    version: Optional[str] = None  # Make version optional
    # Make download fields optional (only for push_to_github = False)

class ExecutionItem(BaseModel):
    """Model for individual execution item"""
    runid: str
    statusDescription: Any  # Changed to Any to support JSON objects
    scenarioName: Optional[str]  # Changed to Optional to support NULL values
    moduleName: Optional[str]  # Module name from the execution
    status: Optional[str]  # Changed to Optional to support NULL values
    videofile: Optional[List[Dict[str, Any]]] = None  # Video files array - supports single or multiple videos
    complt_scripts: Optional[str] = None  # Complete TypeScript scripts (for Download link visibility)
    feature_steps: Optional[str] = None  # Feature file steps (for Download link visibility)
    error_message: Optional[str] = None  # Error message for failed scenarios


class successfulStepByModuleResponse(BaseModel):
    """Response model for successful steps by module API endpoint"""
    responseCode: int = Field(..., description="HTTP response status code")
    message: str = Field(..., description="Response message indicating request status")
    commaSeparatedValues: str = Field(..., description="Comma-separated string values")

class WorkflowGenerationRequest(BaseModel):
    unformattedrequest: str = Field(..., description="Unformatted test request string")
    module: str = Field(..., description="Module name for the test")

class WorkflowStep(BaseModel):
    stepNo: int = Field(..., description="Step number in sequence")
    action: str = Field(..., description="Action type (Navigate, Click, Enter, etc.)")
    description: str = Field(..., description="Step description in Gherkin format")
    confidence: str = Field(..., description="Confidence percentage for the step")

class WorkflowGenerationResponse(BaseModel):
    responseId: str = Field(..., description="Unique response identifier")
    status: str = Field(..., description="Response status (SUCCESS/ERROR)")
    timestamp: str = Field(..., description="Response timestamp in ISO format")
    formattedWorkflow: List[WorkflowStep] = Field(..., description="List of formatted workflow steps")
    error: str = Field(None, description="Error message if status is ERROR")

    

class ExecutionStatusResponse(BaseModel):
    """Response model for execution status by date"""
    inprogressExecution: List[ExecutionItem]
    completedExecution: List[ExecutionItem]

class RunExecutionDetailsResponse(BaseModel):
    """Response model for individual run execution details"""
    runid: str
    description: Any  # Changed to Any to support JSON objects/arrays
    scenarioName: Optional[str]  # Changed to Optional to support NULL values
    status: Optional[str]  # Changed to Optional to support NULL values

router = APIRouter(
    prefix="/Web-Automation",  # Changed from "/playwright"
    tags=["Web-Automation"],  # Changed from "playwright"
    responses={404: {"description": "Not found"}},
)



def load_repo_modules():
    """Load repository modules from JSON file and return formatted options"""
    try:
        # Check if file exists, raise HTTPException instead of fallback
        if not os.path.exists(REPO_MODULES_JSON_PATH):
            logger.error(f"Repo modules JSON file not found at: {REPO_MODULES_JSON_PATH}")
            raise SystemError(
                message="Repository configuration file not found",
                error_code="CONFIG_FILE_MISSING",
                context={"file_path": REPO_MODULES_JSON_PATH}
            )

        with open(REPO_MODULES_JSON_PATH, 'r', encoding='utf-8') as f:
            modules_data = json.load(f)

        formatted_options = {}
        for item in modules_data:
            if isinstance(item, dict) and "domain" in item:
                domain_name = item['domain']
                formatted_options[domain_name] = item

        # Raise HTTPException if no valid modules found
        if not formatted_options:
            logger.error("No valid modules found in the JSON file")
            raise SystemError(
                message="No valid domain modules found in configuration file",
                error_code="CONFIG_FILE_INVALID",
                context={"file_path": REPO_MODULES_JSON_PATH}
            )

        logger.info(f"Loaded {len(formatted_options)} repo modules from JSON")
        return formatted_options

    except SystemError:
        raise
    except Exception as e:
        logger.error(f"Error loading repo modules: {str(e)}")
        raise SystemError(
            message=f"Error reading repository configuration: {str(e)}",
            error_code="CONFIG_FILE_ERROR",
            context={"file_path": REPO_MODULES_JSON_PATH, "original_error": str(e)}
        )

def create_domain_enum():
    """Create a dynamic Enum from JSON file for dropdown options"""
    try:
        repo_modules = load_repo_modules()
        enum_values = {}

        for domain_name in repo_modules.keys():
            # Sanitize domain name for Python enum key
            enum_key = domain_name.replace("-", "_").replace(" ", "_")
            # Ensure it doesn't start with number
            if enum_key and enum_key[0].isdigit():
                enum_key = f"_{enum_key}"
            enum_values[enum_key] = domain_name

        DomainOptions = Enum('DomainOptions', enum_values)
        logger.info(f"Created dynamic enum with {len(enum_values)} options")
        return DomainOptions

    except Exception as e:
        logger.error(f"Error creating domain enum: {str(e)}")

        # Return static fallback enum
        class DomainOptions(str, Enum):
            Payment = "Payment"
            Autoship = "Autoship"

        return DomainOptions


def validate_github_jenkins_setup(domain_name: str, push_to_github: bool):
    """Validate GitHub and Jenkins setup based on push_to_github flag"""
    try:
        repo_modules = load_repo_modules()

        if domain_name not in repo_modules:
            raise UserInputError(
                message=f"Domain '{domain_name}' not found in configuration",
                error_code="INVALID_DOMAIN_SELECTION",
                context={
                    "domain_name": domain_name,
                    "available_domains": list(repo_modules.keys())
                }
            )

        domain_config = repo_modules[domain_name]

        if push_to_github:
            # Check if both GitHub and Jenkins are present
            github_repo = domain_config.get('github-repo')
            jenkins_project = domain_config.get('jenkins-project-name')


            if not github_repo and not jenkins_project:
                raise SystemError(
                    message="GitHub & Jenkins setup is not present in onboarding document",
                    error_code="CONFIG_MISSING",
                    context={"domain": domain_name}
                )

            # If GitHub URL is present but Jenkins is not, continue with GitHub validation
            if github_repo and not jenkins_project:
                logger.warning(
                    f"Jenkins project name not found for domain '{domain_name}', proceeding with GitHub only")

            # Validate GitHub token if GitHub repo is present
            if github_repo:
                # Fetch the secret values for this selected domain
                domain_secret_values = get_secret(selected_domain=domain_name)
                github_access_token = domain_secret_values.get('github_access_token')
                if not github_access_token:
                    raise UserInputError(
                        message="GitHub access token is required when pushing to GitHub",
                        error_code="GITHUB_TOKEN_MISSING",
                        context={"push_to_github": True}
                    )

                # Validate GitHub token
                is_valid, validation_details = validate_github_token(github_access_token)
                if not is_valid:
                    raise GitHubIntegrationError(
                        message="GitHub token validation failed",
                        error_code="GITHUB_TOKEN_INVALID",
                        context={
                            "validation_error": validation_details["error"],
                            "details": validation_details.get("details", {})
                        }
                    )

        return True


    except (UserInputError, GitHubIntegrationError, SystemError):
        raise
    except Exception as e:
        raise SystemError(
            message=f"Error validating setup for domain '{domain_name}': {str(e)}",
            error_code="SETUP_VALIDATION_ERROR",
            context={"domain": domain_name, "original_error": str(e)}
        )

def get_domain_credentials(domain_name: str):
    """Get GitHub and Jenkins credentials for a specific domain from repo modules JSON"""
    try:
        repo_modules = load_repo_modules()

        if domain_name not in repo_modules:
            raise UserInputError(
                message=f"Domain '{domain_name}' not found in configuration",
                error_code="INVALID_DOMAIN_SELECTION",
                context={
                    "domain_name": domain_name,
                    "available_domains": list(repo_modules.keys())
                }
            )

        domain_config = repo_modules[domain_name]

        # Fetch the secret values for this selected domain
        domain_secret_values = get_secret(selected_domain=domain_name)
        github_access_token = domain_secret_values.get('github_access_token')
        jenkins_username = domain_secret_values.get('jenkins_username')
        jenkins_api_token = domain_secret_values.get('jenkins_api_token')

        return {
            'github_access_token': github_access_token,
            'jenkins_username': jenkins_username,
            'jenkins_api_token': jenkins_api_token
        }

    except HTTPException:
        raise
    except Exception as e:
        raise SystemError(
            message=f"Error retrieving credentials for domain '{domain_name}': {str(e)}",
            error_code="CREDENTIAL_RETRIEVAL_ERROR",
            context={"domain": domain_name, "original_error": str(e)}
        )

def validate_domain_selection(selection: str) -> bool:
    """Validate that the selected domain exists in our options"""
    try:
        available_options = load_repo_modules()
        return selection in available_options
    except Exception as e:
        logger.error(f"Error validating domain selection: {str(e)}")
        return False


def validate_github_token(token: str) -> Tuple[bool, Dict]:
    """
    Validate GitHub access token by making an actual API call.

    Args:
        token (str): GitHub access token to validate

    Returns:
        Tuple[bool, Dict]: (is_valid, validation_details)
    """
    validation_result = {
        "valid": False,
        "error": None,
        "details": {},
        "user_info": None
    }

    try:
        # Basic format check first
        if not token:
            validation_result["error"] = "Token is empty"
            return False, validation_result

        if not token.strip():
            validation_result["error"] = "Token is whitespace only"
            return False, validation_result

        # Make API call to validate token
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "GitHubTokenValidator/1.0"
        }

        # Use the /user endpoint to validate token and get user info
        response = requests.get(
            "https://api.github.com/user",
            headers=headers,
            timeout=10
        )

        if response.status_code == 200:
            # Token is valid
            user_data = response.json()
            validation_result.update({
                "valid": True,
                "error": None,
                "user_info": {
                    "login": user_data.get("login"),
                    "id": user_data.get("id"),
                    "type": user_data.get("type"),
                    "name": user_data.get("name")
                },
                "details": {
                    "token_scopes": response.headers.get("X-OAuth-Scopes", ""),
                    "rate_limit_remaining": response.headers.get("X-RateLimit-Remaining", "unknown"),
                    "validated_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
            })
            logger.info(f"✅ GitHub token validated successfully for user: {user_data.get('login')}")
            return True, validation_result

        elif response.status_code == 401:
            validation_result["error"] = "Invalid GitHub token - authentication failed"
            validation_result["details"] = {
                "status_code": 401,
                "message": "Token is invalid or expired"
            }
            logger.error("❌ GitHub token validation failed: 401 Unauthorized")
            return False, validation_result

        elif response.status_code == 403:
            # Token might be valid but has insufficient permissions
            validation_result["error"] = "GitHub token has insufficient permissions"
            validation_result["details"] = {
                "status_code": 403,
                "message": "Token valid but lacks required permissions",
                "rate_limit_exceeded": "X-RateLimit-Remaining" in response.headers and response.headers[
                    "X-RateLimit-Remaining"] == "0"
            }
            logger.error("❌ GitHub token validation failed: 403 Forbidden")
            return False, validation_result

        else:
            validation_result["error"] = f"Unexpected response from GitHub API: {response.status_code}"
            validation_result["details"] = {
                "status_code": response.status_code,
                "response_text": response.text[:200] if response.text else "No response body"
            }
            logger.error(f"❌ GitHub token validation failed: Unexpected status {response.status_code}")
            return False, validation_result

    except requests.exceptions.Timeout:
        validation_result["error"] = "GitHub API request timed out"
        validation_result["details"] = {"exception_type": "Timeout"}
        logger.error("❌ GitHub token validation failed: Request timeout")
        return False, validation_result

    except requests.exceptions.ConnectionError:
        validation_result["error"] = "Unable to connect to GitHub API"
        validation_result["details"] = {"exception_type": "ConnectionError"}
        logger.error("❌ GitHub token validation failed: Connection error")
        return False, validation_result

    except json.JSONDecodeError:
        validation_result["error"] = "Invalid JSON response from GitHub API"
        validation_result["details"] = {"exception_type": "JSONDecodeError"}
        logger.error("❌ GitHub token validation failed: JSON decode error")
        return False, validation_result

    except Exception as e:
        validation_result["error"] = f"Unexpected error during token validation: {str(e)}"
        validation_result["details"] = {"exception_type": type(e).__name__}
        logger.error(f"❌ GitHub token validation failed: {str(e)}")
        return False, validation_result


async def get_json_files_safely(request: Request) -> List[UploadFile]:
    """
    Custom dependency to safely handle JSON file uploads.
    Filters out empty strings while maintaining proper type signature.
    """
    try:
        form = await request.form()
        json_files_raw = form.getlist("json_files")

        valid_files = []
        for item in json_files_raw:
            # Skip empty strings or invalid items
            if isinstance(item, str) and (not item or item.strip() == ""):
                logger.debug(f"Skipping empty string in json_files")
                continue

            # Only add valid UploadFile objects
            if hasattr(item, 'filename') and item.filename and item.filename.strip():
                valid_files.append(item)

        logger.info(f"Filtered {len(valid_files)} valid JSON files from {len(json_files_raw)} total items")
        return valid_files

    except Exception as e:
        logger.warning(f"Error processing JSON files: {str(e)}")
        return []
DomainOptions = create_domain_enum()
@router.post("/upload-feature-file")
async def upload_feature_file(
        request: Request,
        feature_file: UploadFile = File(...),
        json_files: List[UploadFile] = File([], description="Optional list of JSON files with request bodies"),
        push_to_github: bool = Form(True, description="Would you like to push to GitHub?", title="Push to GitHub"),
        domain_selection: Optional[DomainOptions] = Form(None, description="🔴 REQUIRED if GitHub=True | Select domain"),
     
        generate_step_definitions: bool = Form(True, description="Generate output in Step Definition format?", title="Generate Step Definitions"),
        dry_run_strategy: Literal["Javascript", "MCP"] = Form("Javascript", title="Dry Run Strategy"),
        preview_mode: bool = Form(False, description="When true, return step preview JSON instead of full generation", title="Preview Mode"),
        branch_name: Optional[str] = Form(
            "",
            title="Custom Branch Name",
            description="Optional. Used only when Push to GitHub = True. Do not include 'feature/'",
            example=""
        ),
        notify_email: Optional[str] = Form(
            "",
            title="Notify Email",
            description="Optional. Comma-separated emails allowed. If provided, the run summary email will be sent to these addresses.",
            example=""
        ),
        run_id: Optional[str] = Form(
            None,
            title="Run ID",
            description="Optional. Custom run ID in format 'internal_<alphanumeric_with_underscores>'. If invalid or not provided, a new run ID will be generated automatically.",
            example="internal_test123"
        )
):
    """
    ## TestGenie Automation Script Generation Guidelines

    To generate automation scripts using TestGenie, please follow the guidelines below when uploading your feature files:

    ### 1. Feature File Format
    • Upload feature files in BDD Cucumber Gherkin format

    • Each feature file may contain one or multiple scenarios

    ### 2. Tagging Requirements
    • The first tag in both the feature file and each scenario must be unique across the entire automation project

    • The format of the first tag should be: `<modulename>-<uniqueid>`
      - **modulename**: Represents the functional module (e.g., payments, checkout)
      - **uniqueid**: Can be a descriptive phrase related to the feature or a Jira story ID

    • **Note**: Only alphanumeric characters are allowed in modulename and uniqueid. Special characters and spaces are not supported

    ### 3. Handling E2E Scenarios with Web and API Steps
    • If an end-to-end scenario includes both web and REST API steps:
      - Provide the API details in a predefined JSON format
      - Template available: https://chewyinc.atlassian.net/wiki/spaces/EN/pages/3687645865/TestGenie+Onboarding+Future+implementation
      - Reference the corresponding JSON file name within the scenario steps
      - You may include multiple JSON files based on the number of API requests involved

    ### 4. GitHub and Jenkins Integration (Optional)
    • Push to GitHub is an optional feature that allows TestGenie to:
      - Push the generated feature file to the configured automation repository (in a new branch)
      - Raise a pull request against the main branch
      - Trigger test execution via the Jenkins pipeline configured during onboarding

    • If your GitHub repository and Jenkins pipeline are not set up, please select "No" during the integration prompt
      - In this case, TestGenie will provide a download URL or ID in the response
      - You will need to manually merge the generated scripts into your repository and trigger execution in Jenkins
    """
    try:
        logger.info("Uploaded feature file")
        start_time = time.time()
        
        # === RUN ID RESOLUTION ===
        # Resolve run ID - validate user input or generate new one with millisecond timestamp
        resolved_run_id, was_user_provided = resolve_run_id(
            user_run_id=run_id, 
            generation_prefix="upload"
        )
        
        # Log run ID information
        if was_user_provided:
            logger.info(f"🆔 Using user-provided run ID: {resolved_run_id}")
        else:
            logger.info(f"🆔 Generated new run ID: {resolved_run_id}")
        
        # Parse and pre-validate notify emails (if provided)
        def _normalize_string_list(v):
            if isinstance(v, list):
                return [x.strip() for x in v if isinstance(x, str) and x.strip()]
            if isinstance(v, str) and v.strip():
                return [s.strip() for s in v.split(",") if s.strip()]
            return []
        def _is_email_like(s: str) -> bool:
            try:
                if "@" not in s:
                    return False
                local, domain = s.split("@", 1)
                return bool(local.strip()) and "." in domain
            except Exception:
                return False
        notify_list_provided = []
        if notify_email and isinstance(notify_email, str) and notify_email.strip():
            parsed = _normalize_string_list(notify_email)
            notify_list_provided = [e for e in parsed if _is_email_like(e)]
            if not notify_list_provided:
                # If user provided a value but none are valid-looking, raise 422 so Swagger shows error
                raise HTTPException(status_code=422, detail="Notify Email: no valid email addresses found. Provide comma-separated addresses with '@' and a domain (e.g., user@example.com).")
        # Extract JIRA ID from the filename
        filename = feature_file.filename
        if not filename:
            raise UserInputError(
                message="No filename provided in uploaded file",
                error_code="FILE_NOT_FOUND",
                context={"feature_file": "missing filename"}
            )
        jira_id = filename.split(".feature")[0]
        
        record_video_enabled = True  # Video recording enabled by default
        # Convert string options to boolean for internal use
        mcp_crawler = (dry_run_strategy == "MCP")
        
        # Use simple static version
        file_version = "v1"

        if push_to_github:
            # Prepare optional custom branch base
            custom_branch_base = None
            try:
                if branch_name and isinstance(branch_name, str) and branch_name.strip():
                    candidate = branch_name.strip()
                    # If user included 'feature/' prefix, strip it to avoid duplication
                    if candidate.lower().startswith("feature/"):
                        candidate = candidate.split("/", 1)[1].strip()
                    # Validate allowed characters (alphanumeric, hyphen, underscore, dot)
                    if not re.fullmatch(r"[A-Za-z0-9._-]+", candidate):
                        raise UserInputError(
                            message="Custom branch name may contain only letters, numbers, hyphen (-), underscore (_), or dot (.)",
                            error_code="INVALID_BRANCH_NAME",
                            context={"provided": branch_name}
                        )
                    # Disallow path separators
                    if "/" in candidate or "\\" in candidate:
                        raise UserInputError(
                            message="Custom branch name must not contain slashes",
                            error_code="INVALID_BRANCH_NAME",
                            context={"provided": branch_name}
                        )
                    custom_branch_base = f"feature/{candidate}"
            except UserInputError:
                raise
            except Exception as e:
                raise UserInputError(
                    message=f"Invalid custom branch name: {str(e)}",
                    error_code="INVALID_BRANCH_NAME",
                    context={"provided": branch_name}
                )
            # Validate required fields only if pushing to GitHub
            if not domain_selection:
                raise UserInputError(
                    message="Domain selection is required when pushing to GitHub",
                    error_code="MISSING_DOMAIN_SELECTION",
                    context={"push_to_github": True}
                )

            domain_selection_str = domain_selection.value

            if not validate_domain_selection(domain_selection_str):
                available_options = load_repo_modules()
                raise UserInputError(
                    message=f"Invalid domain selection: {domain_selection_str}",
                    error_code="INVALID_DOMAIN_SELECTION",
                    context={"available_options": list(available_options.keys())}
                )

            # Validate GitHub and Jenkins setup
            validate_github_jenkins_setup(domain_selection_str, push_to_github)

            # Load credentials from repo modules JSON
            credentials = get_domain_credentials(domain_selection_str)

            github_access_token = credentials['github_access_token']
            jenkins_username = credentials['jenkins_username']
            jenkins_api_token = credentials['jenkins_api_token']

            # Get selected configuration
            repo_modules = load_repo_modules()
            selected_config = repo_modules[domain_selection_str]

            logger.info(f"Selected domain: {domain_selection_str}")
            logger.info(f"GitHub repo: {selected_config.get('github-repo', 'N/A')}")
            logger.info(f"Jenkins project: {selected_config.get('jenkins-project-name', 'N/A')}")

        # Read the feature file content
        feature_content = await feature_file.read()
        feature_content = feature_content.decode("windows-1252")
        feature_content = feature_content.replace('¥', '•')
        
        # === EXTRACT FEATURE METADATA ===
        # Extract feature tags, name, and scenario metadata
        feature_tags_list = extract_feature_tags(feature_content)
        feature_name = extract_feature_name(feature_content)
        scenarios_metadata = extract_scenarios_metadata(feature_content)
        
        logger.info(f"📋 Feature Metadata Extracted:")
        logger.info(f"   Feature Name: {feature_name}")
        logger.info(f"   Feature Tags: {feature_tags_list}")
        logger.info(f"   Scenarios: {len(scenarios_metadata)}")
        
        # Convert tags to comma-separated text for storage
        feature_tags_text = ",".join(feature_tags_list) if feature_tags_list else None
        
        # === EXTRACT API STEPS FROM JSON FILES ===
        # Read and store JSON file contents (IMPORTANT: Store for later use in target/json)
        json_files_content = {}
        json_files_buffered = []  # Store buffered content for later file writing
        if json_files:
            logger.info(f"📄 Processing {len(json_files)} JSON file(s) for API steps extraction")
            for json_file in json_files:
                if json_file.filename:
                    try:
                        content = await json_file.read()
                        if content:
                            content_str = content.decode('utf-8').strip()
                            json_files_content[json_file.filename] = content_str
                            # CRITICAL: Buffer the content for later file writing (UploadFile can only be read once!)
                            json_files_buffered.append({
                                'filename': json_file.filename,
                                'content': content_str
                            })
                            logger.info(f"✅ Loaded JSON file for API steps: {json_file.filename} ({len(content_str)} chars)")
                        else:
                            logger.warning(f"⚠️  JSON file {json_file.filename} is empty")
                    except Exception as e:
                        logger.error(f"❌ Failed to read JSON file {json_file.filename} for API steps: {e}")
        
        # Extract API steps from JSON files
        api_steps_json = extract_api_steps_from_json_files(json_files_content)
        logger.info(f"📡 API Steps extracted from {len(json_files_content)} files")

        # Auto-expand Step Macros if directive lines are present
        try:
            if "# step-macro" in feature_content.lower():
                from services.interface_layer.common_steps_api import expand_feature_text_with_macros
                module_name = None
                if push_to_github and domain_selection:
                    module_name = domain_selection.value
                elif domain_selection:
                    module_name = domain_selection.value
                if not module_name:
                    logger.info("Step Macro directive found but no domain selected; skipping expansion")
                else:
                    exp = expand_feature_text_with_macros(
                        feature_text=feature_content,
                        module=module_name,
                        materialize_defaults_for_quoted=False,
                        materialize_from_examples=True,
                    )
                    feature_content = exp.get("content", feature_content)
                    logger.info(f"Step Macros expanded: {len(exp.get('expanded', []))} replacements")
                    
                    # Print expanded feature file for user visibility
                    logger.info("📄 Expanded Feature File (Post-Macro):")
                    logger.info("="*80)
                    for line_num, line in enumerate(feature_content.split('\n'), 1):
                        logger.info(f"{line_num:3d} | {line}")
                    logger.info("="*80)
            else:
                logger.info("No step-macro directives found; skipping macro expansion")
        except Exception as e:
            logger.warning(f"Step Macro expansion failed (continuing without change): {e}")

        # Build feature_steps JSON (Scenario names and expanded steps) for DB storage
        feature_steps_json_text = None
        try:
            import json as _json
            # Helper to materialize steps with first Examples row (if any) and filter only step lines
            def _build_feature_steps_json_text(_feature_text: str) -> str:
                from services.integration_layer.playwright_script_generation.processing_layer.generate_bdd_script import (
                    prepare_scenario_transcript_steps as _prepare_scenario_transcript_steps,
                    split_scenarios as _split_scenarios,
                )
                scenarios = _split_scenarios(_feature_text or "")
                out = []
                for sc in (scenarios or []):
                    name = sc.get("Scenario", "")
                    raw_steps = sc.get("steps", []) or []
                    expanded, _ds = _prepare_scenario_transcript_steps(raw_steps)

                    def _is_step_line(s: str) -> bool:
                        s0 = (s or "").strip().lower()
                        return s0.startswith(("given ", "when ", "then ", "and ", "but "))

                    filtered = [s for s in (expanded or []) if _is_step_line(s)]
                    out.append({"scenario": name, "steps": filtered})
                return _json.dumps(out, ensure_ascii=False)

            feature_steps_json_text = _build_feature_steps_json_text(feature_content)
        except Exception as e:
            logger.warning(f"Failed to build feature_steps JSON; proceeding without it: {e}")
            feature_steps_json_text = None

        # === PREVIEW MODE (MCP only) - Check BEFORE Gherkin validation ===
        if preview_mode:
            if (dry_run_strategy or "Javascript") != "MCP":
                # Preview supported only for MCP crawler
                return JSONResponse(
                    status_code=400,
                    content={"message": "preview mode is not available for java script crawler execution mode"}
                )

            # Same transcript pipeline as execution (Examples + bulk tables), then step lines only
            from services.integration_layer.playwright_script_generation.processing_layer.generate_bdd_script import (  # noqa: E402
                prepare_scenario_transcript_steps as _preview_prepare_transcript,
                split_scenarios as _preview_split_scenarios,
            )

            steps_text: List[str] = []
            for _sc in _preview_split_scenarios(feature_content or "") or []:
                _expanded, _ = _preview_prepare_transcript(_sc.get("steps", []) or [])
                for _line in _expanded or []:
                    _s0 = (_line or "").strip().lower()
                    if _s0.startswith(("given ", "when ", "then ", "and ", "but ")):
                        steps_text.append((_line or "").strip())
            domain_selection_str_for_uid = domain_selection.value if domain_selection else None

            def _infer_action(step_line: str) -> str:
                s = step_line.lower()
                if "navigate" in s or "go to" in s or "launch" in s:
                    return "navigate"
                if "click" in s or "tap" in s or "press" in s:
                    return "click"
                if "enter" in s or "type" in s or "populate" in s or "fill" in s:
                    return "type"
                if "select" in s or "choose" in s or "pick" in s:
                    return "select"
                if "verify" in s or "assert" in s or "validate" in s:
                    return "verify"
                return "action"

            preview_steps: List[Dict[str, Any]] = []
            for idx, step_line in enumerate(steps_text, start=1):
                action = _infer_action(step_line)
                locator = ""
                confidence = 80.0

                if action == "navigate":
                    # Use first quoted URL (if any) as the locator; confidence 100
                    try:
                        m = re.search(r'"([^"]+)"', step_line)
                        if m:
                            locator = m.group(1)
                    except Exception:
                        locator = ""
                    confidence = 100.0
                else:
                    try:
                        uid = _make_mcp_uid_base(step_line, domain_selection_str_for_uid)
                        cached = get_playwright_locator_by_uid_prefix(uid)
                        if cached:
                            locator = cached
                            confidence = 100.0
                        else:
                            locator = "Needs dry run"
                            confidence = 80.0
                    except Exception:
                        locator = "Needs dry run"
                        confidence = 80.0

                preview_steps.append({
                    "stepNo": idx,
                    "fullStep": step_line,
                    "action": action,
                    "locator": locator,
                    "confidence": confidence,
                })

            return {"testSteps": preview_steps}

        # === GHERKIN VALIDATION (After Step Macro expansion, but only for non-preview mode) ===
        from services.validation_layer.gherkin_validator import validate_gherkin
        validation_result = validate_gherkin(feature_content)
        
        if not validation_result.get("is_valid"):
            # Validation failed - format detailed error message
            issues = validation_result.get("issues", [])
            error_parts = [f"Gherkin Format Validation Failed [Run ID: {resolved_run_id}]\n"]
            
            for i, issue in enumerate(issues, 1):
                error_parts.append(f"\n{i}. {issue.get('type', 'Error')} (Line {issue.get('line', 'N/A')})")
                
                # Show actual line content if available
                line_content = issue.get('content', '')
                if line_content:
                    error_parts.append(f"   Content: \"{line_content.strip()}\"")
                
                error_parts.append(f"   Problem: {issue.get('problem', 'Unknown')}")
                error_parts.append(f"   Solution: {issue.get('solution', 'Please review the feature file')}")
            
            detailed_error = "\n".join(error_parts)
            logger.error(f"❌ {detailed_error}")
            
            raise UserInputError(
                message=detailed_error,
                error_code="GHERKIN_VALIDATION_FAILED",
                context={"run_id": resolved_run_id, "jira_id": jira_id, "issues": issues}
            )
        
        logger.info(f"✅ Gherkin validation passed: {validation_result.get('message')}")
        
        base = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(os.path.dirname(base))
        parent = os.path.dirname(root)

        json_path = os.path.join(parent, PLATFORM_CONFIG.get("JSON_FILE_PATH"))
        logger.info(f"📁 JSON_FILE_PATH from config: {PLATFORM_CONFIG.get('JSON_FILE_PATH')}")
        logger.info(f"📁 Computed json_path: {json_path}")
        
        # Ensure json_path directory exists
        try:
            os.makedirs(json_path, exist_ok=True)
            logger.info(f"✅ JSON directory ensured: {json_path}")
        except Exception as dir_err:
            logger.error(f"❌ Failed to create JSON directory {json_path}: {dir_err}")
        
        # CRITICAL FIX: Use buffered content from earlier read (UploadFile can only be read once!)
        # The json_files_buffered list was populated at lines 820-837 when we first read the files
        json_count = 0
        if json_files_buffered:
            logger.info(f"📄 Writing {len(json_files_buffered)} buffered JSON file(s) to target/json")
            for i, buffered_file in enumerate(json_files_buffered):
                filename = buffered_file.get('filename')
                content_str = buffered_file.get('content', '')
                logger.info(f"📄 Processing buffered file {i + 1}: {filename} ({len(content_str)} chars)")
                
                if filename and content_str:
                    try:
                        # Validate JSON
                        try:
                            json.loads(content_str)
                            logger.info(f"✅ JSON validation passed for {filename}")
                        except json.JSONDecodeError as json_err:
                            logger.error(f"❌ JSON validation failed for {filename}: {json_err}")
                            raise json_err
                        
                        # Write to target/json
                        json_file_path = os.path.join(json_path, filename)
                        logger.info(f"📝 Writing JSON file to: {json_file_path}")
                        
                        with open(json_file_path, 'w', encoding='utf-8') as f:
                            bytes_written = f.write(content_str)
                            logger.info(f"✅ Wrote {bytes_written} bytes to {json_file_path}")
                        
                        # Verify file was written
                        if os.path.exists(json_file_path):
                            file_size = os.path.getsize(json_file_path)
                            logger.info(f"✅ Verified file exists: {json_file_path} ({file_size} bytes)")
                        else:
                            logger.error(f"❌ File verification failed - file not found after write: {json_file_path}")
                        
                        logger.info(f"✅ Saved JSON file {i + 1}: {json_file_path}")
                        json_count += 1
                    except Exception as e:
                        logger.error(f"❌ Failed to write JSON file {filename}: {type(e).__name__}: {str(e)}")
                        import traceback
                        logger.error(f"❌ Traceback: {traceback.format_exc()}")
                else:
                    logger.warning(f"⚠️  Buffered file {i + 1} missing filename or content")
        else:
            logger.info("ℹ️  No buffered JSON files to write - proceeding with feature file only")
        
        logger.info(f"📊 JSON file writing complete: {json_count} file(s) successfully written to {json_path}")

        # Process the feature file using process_feature_files
        if push_to_github:
            # Process with GitHub push
            try:
                logger.info(f"Processing feature file for JIRA ID: {jira_id}")
                process_result = await process_feature_files(
                    content=feature_content,
                    jira_id=jira_id,
                    s3_path=None,
                    file_version=file_version,
                    push_to_github=True,
                    selected_domain=domain_selection_str,
                    target_github_repo=selected_config.get('github-repo'),
                    jenkins_project_name=selected_config.get('jenkins-project-name'),
                    github_access_token=github_access_token,
                    jenkins_username=jenkins_username,
                    jenkins_api_token=jenkins_api_token,
                    record_video=record_video_enabled,
                    generate_step_definitions=generate_step_definitions,
                    custom_branch_base=custom_branch_base,
                    dry_run_strategy=mcp_crawler,
                    repo_base_path=selected_config.get('path', 'src/tests'),
                    base_branch_override=selected_config.get('default_branch', 'main'),
                    run_id=resolved_run_id,  # NEW: Pass run_id for status tracking
                    feature_tags=feature_tags_text,
                    feature_nm=feature_name,
                    scn_tags=",".join(scenarios_metadata[0].get('tags', [])) if scenarios_metadata and scenarios_metadata[0].get('tags') else None,
                    scn_nm=None,  # Scenario name will be set per-scenario in generate_bdd_script.py
                    api_steps=api_steps_json
                )

                if process_result["success"]:
                    # If preview requested, return preview steps immediately
                    if preview_mode:
                        return {"testSteps": process_result.get("preview_steps", [])}

                    logger.info(f"✅ Successfully processed feature file and pushed to GitHub")

                    # Extract GitHub details for response
                    github_details = process_result.get("github_result", {})
                    pr_url = github_details.get("github_workflow", {}).get("details", {}).get("pr_url", "N/A")
                    jenkins_queue = github_details.get("jenkins_trigger", {}).get("details", {}).get("queue_location",
                                                                                                     "N/A")
                    
                    # Extract version from GitHub branch creation result
                    branch_versioning_info = github_details.get("branch_versioning_info", {})
                    github_version = branch_versioning_info.get("version", 1)
                    if github_version > 0:
                        file_version = f"v{github_version}"
                    else:
                        file_version = "v1"

                    end_time = time.time()
                    execution_time = end_time - start_time
                    logger.info(f"Execution time: {execution_time} seconds")
                    processing_summary = process_result.get("processing_summary", {})
                    success_recordings = processing_summary.get("success_recordings", [])
                    failed_recordings = processing_summary.get("failed_recordings", [])
                    failed_reasons = processing_summary.get("failed_reasons", [])
                    failed_details = processing_summary.get("failed_details", [])
                    # Convert to filenames; preserve existing zip_filename if already present
                    def _map_rec(arr):
                        mapped = []
                        for r in (arr or []):
                            base = {k: v for k, v in r.items() if k != "zip_url"}
                            zip_name = r.get("zip_filename") or _zip_filename_from_url(r.get("zip_url", ""))
                            
                            # Handle multiple video filenames (pipe/comma separated)
                            video_files = _split_recording_filenames(zip_name)
                            if len(video_files) > 1:
                                for video_file in video_files:
                                    video_entry = base.copy()
                                    video_entry["zip_filename"] = video_file
                                    mapped.append(video_entry)
                            else:
                                base["zip_filename"] = video_files[0] if video_files else zip_name
                                mapped.append(base)
                        return mapped
                    recordings = _map_rec(success_recordings) + _map_rec(failed_recordings)
                    # Create response without null fields for GitHub processing
                    response_data = {
                        "run_id": resolved_run_id,
                        "jira_id": jira_id,
                        "status": "success",
                        "message": f"✅ Successfully uploaded and pushed to GitHub: {jira_id} | Domain: {domain_selection_str} | PR: {pr_url} | Scenarios: {processing_summary.get('successful_scenarios', 0)}/{processing_summary.get('total_scenarios', 0)} [Run ID: {resolved_run_id}]",
                        "version": file_version,
                        "selected_domain": domain_selection_str,
                        "github_repo": selected_config.get('github-repo', 'N/A'),
                        "jenkins_project": selected_config.get('jenkins-project-name', 'N/A'),
                        "recordings": recordings
                    }
                    # Add Jenkins status fields from process_result
                    if "jenkins_pipeline_status" in process_result:
                        response_data["jenkins_pipeline_status"] = process_result["jenkins_pipeline_status"]
                    if "jenkins_error" in process_result:
                        response_data["jenkins_error"] = process_result["jenkins_error"]

                    # Fire-and-forget summary email to Slack/email
                    try:
                        final_branch = github_details.get("final_branch_name") or github_details.get("github_workflow", {}).get("details", {}).get("branch_name")
                        pr_number = github_details.get("github_workflow", {}).get("details", {}).get("pr_number")
                        # Collect per-team recipients from onboarding JSON
                        _slack_emails = selected_config.get("slack_channel_emails") or []
                        _email_recipients = selected_config.get("email_recipients") or []
                        def _normalize_list(v):
                            if isinstance(v, list):
                                return [x for x in v if isinstance(x, str) and x.strip()]
                            if isinstance(v, str) and v.strip():
                                # support comma-separated string
                                return [s.strip() for s in v.split(",") if s.strip()]
                            return []
                        slack_list = _normalize_list(_slack_emails)
                        json_emails = _normalize_list(_email_recipients)
                        notify_list = notify_list_provided
                        human_primary = notify_list if notify_list else json_emails
                        team_recipients = slack_list + human_primary
                        # Derive Jenkins Job URL from queue or base if possible
                        jenkins_job_url = None
                        if jenkins_queue and isinstance(jenkins_queue, str) and jenkins_queue.startswith(("http://", "https://")):
                            try:
                                from urllib.parse import urlparse
                                parsed = urlparse(jenkins_queue)
                                base = f"{parsed.scheme}://{parsed.netloc}"
                                job_name = selected_config.get('jenkins-project-name', '').strip()
                                if job_name:
                                    jenkins_job_url = f"{base}/job/{job_name}"
                            except Exception:
                                pass
                        # Build recordings download base URL
                        base_url = str(request.base_url).rstrip('/')
                        recordings_download_base = f"{base_url}/Web-Automation/recordings/download"
                        summary_payload = {
                            "run_id": resolved_run_id,
                            "status": "success",
                            "jira_id": jira_id,
                            "selected_domain": domain_selection_str,
                            "push_to_github": True,
                            "record_video": record_video_enabled,
                            "generate_step_definitions": generate_step_definitions,
                            "github_repo": selected_config.get('github-repo', 'N/A'),
                            "pr_url": pr_url,
                            "pr_number": pr_number,
                            "branch_name": final_branch,
                            "jenkins_project": selected_config.get('jenkins-project-name', 'N/A'),
                            "jenkins_queue": jenkins_queue,
                            "jenkins_job_url": jenkins_job_url,
                            "version": file_version,
                            "execution_seconds": execution_time,
                            "scenarios_total": processing_summary.get("total_scenarios", 0),
                            "scenarios_passed": processing_summary.get("successful_scenarios", 0),
                            "scenarios_failed": processing_summary.get("failed_scenarios", 0),
                            "recordings": recordings,
                            "recordings_download_base_url": recordings_download_base,
                            "scenario_failure_reasons": failed_reasons,
                            "scenario_failure_details": failed_details,
                            # No local artifacts zip for GitHub path
                            "download_zip_filename": None,
                            "download_zip_url": None,
                        }
                        try:
                            logger.info(f"Notify: attempting success summary | jira={jira_id} run_id={resolved_run_id} recipients={len(team_recipients)}")
                            await asyncio.to_thread(send_run_summary_email, summary_payload, team_recipients)
                        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPException, ValueError):
                            logger.warning(f"Notify: initial success summary send failed, trying fallback recipients")
                            if notify_list and json_emails:
                                fallback_recipients = slack_list + json_emails
                                try:
                                    logger.info(f"Notify: attempting success summary (fallback) | jira={jira_id} run_id={resolved_run_id} recipients={len(fallback_recipients)}")
                                    await asyncio.to_thread(send_run_summary_email, summary_payload, fallback_recipients)
                                except Exception as fallback_exc:
                                    logger.error(f"Notify: success summary fallback send failed: {type(fallback_exc).__name__}: {fallback_exc}")
                    except Exception as notify_exc:
                        # Do not block user response if email fails
                        logger.error(f"Notify: success summary notification failed: {type(notify_exc).__name__}: {notify_exc}")
                    
                    # === STORE RESPONSE IN DATABASE ===
                    # Skip if scenarios were already stored individually in generate_bdd_script.py
                    # This happens when dry_run_strategy=True with status_tracking_available and run_id
                    scenarios_stored_in_db = processing_summary.get("scenarios_stored_in_db", 0)
                    
                    if scenarios_stored_in_db > 0:
                        logger.info(f"ℹ️  Skipping overall storage - {scenarios_stored_in_db} scenario(s) already stored individually in t_gen_resp")
                    else:
                        try:
                            # Get scenario name from tran_status if available
                            scenario_name = _get_scenario_name_from_tran_status(resolved_run_id, mcp_crawler, "GitHub success storage")

                            # Create database storage data with scenarios array
                            db_storage_data = response_data.copy()
                            db_storage_data["scenarios"] = processing_summary.get("scenarios", [])
                            
                            # Determine database status based on scenario results
                            failed_count = processing_summary.get("failed_scenarios", 0)
                            db_status = "FAILED" if failed_count > 0 else "PASSED"
                            logger.info(f"📊 GitHub - Storing with status: {db_status} (failed: {failed_count})")
                            video_filename = None
                            if recordings and len(recordings) > 0:
                                # ✅ FIX: Store ALL video filenames (comma-separated) not just the first one
                                all_filenames = [r.get("zip_filename") for r in recordings if r.get("zip_filename")]
                                if all_filenames:
                                    video_filename = ",".join(all_filenames)
                                    logger.info(f"📹 Video filenames for database storage (all videos): {video_filename}")
                            elif db_status == "FAILED":
                                if failed_recordings and len(failed_recordings) > 0:
                                    # ✅ FIX: Store ALL failed video filenames (comma-separated) not just the first one
                                    all_filenames = [r.get("zip_filename") for r in failed_recordings if r.get("zip_filename")]
                                    if all_filenames:
                                        video_filename = ",".join(all_filenames)
                                        logger.info(f"📹 Video filenames for database storage (from failed_recordings, all videos): {video_filename}")
                            
                            # Extract complete scripts from response
                            complete_scripts = process_result.get("step_definition_file")
                            
                            store_success = store_generated_response(
                                run_id=resolved_run_id,
                                response_data=db_storage_data,
                                jira_id=jira_id,
                                module_name=domain_selection_str,
                                status=db_status,
                                scenario_name=scenario_name,
                                video_filename=video_filename,
                                complete_scripts=complete_scripts,
                                feature_steps=feature_steps_json_text,
                                feature_tags=feature_tags_text,
                                feature_nm=feature_name,
                                scn_tags=",".join(scenarios_metadata[0].get('tags', [])) if scenarios_metadata and scenarios_metadata[0].get('tags') else None,
                                scn_nm=None,  # Scenario name will be set per-scenario in generate_bdd_script.py
                                api_steps=api_steps_json
                            )
                            if store_success:
                                logger.info(f"💾 Response stored in database for run ID: {resolved_run_id}")
                            else:
                                logger.warning(f"⚠️  Failed to store response in database for run ID: {resolved_run_id}")
                        except Exception as e:
                            logger.error(f"❌ Error storing response in database: {str(e)}")
                    
                    # Delete tran_status on success (MCP flow only)
                    if mcp_crawler:
                        try:
                            from utils.run_id_manager import delete_tran_status
                            delete_tran_status(resolved_run_id, allow_inner_deletion=True)
                            logger.info(f"🗑️  Deleted tran_status for successful run_id: {resolved_run_id}")
                        except Exception as del_e:
                            logger.warning(f"⚠️  Failed to delete tran_status: {del_e}")

                    # === AUTO-HEAL UPDATE (GitHub Push Success) ===
                    # Update t_autoHealScn if this was an autoheal execution
                    try:
                        autoheal_result = AutoHealUpdateService.update_autoheal_on_completion(
                            run_id=resolved_run_id,
                            new_stp_loc_map=None,  # Will be fetched from t_gen_resp
                            new_complete_scripts=None,  # Will be fetched from t_gen_resp
                            module_name=domain_selection_str,
                            feature_tags=feature_tags_text,
                            feature_name=feature_name,
                            scenario_tags=",".join(scenarios_metadata[0].get('tags', [])) if scenarios_metadata and scenarios_metadata[0].get('tags') else None,
                            scenario_name=None
                        )
                        if autoheal_result.get("status") not in ["skipped"]:
                            logger.info(f"🔄 Auto-heal update result: {autoheal_result.get('status')} - {autoheal_result.get('message', '')}")
                    except Exception as ah_e:
                        logger.warning(f"⚠️  Auto-heal update failed (non-blocking): {ah_e}")

                    return response_data
                else:
                    # ❌ Handle GitHub workflow failure
                    error_message = process_result.get("error", "Unknown error")
                    error_stage = process_result.get("error_stage", "unknown")
                    error_details = process_result.get("error_details", "No additional details")

                    logger.error(f"❌ GitHub workflow failed at stage: {error_stage}")
                    logger.error(f"❌ Error: {error_message}")

                    # === STORE GITHUB FAILURE IN DATABASE ===
                    # Skip if scenarios were already stored individually in generate_bdd_script.py
                    # Access processing_summary from process_result (not locals)
                    _ps = process_result.get("processing_summary", {}) if process_result else {}
                    scenarios_stored_in_db = _ps.get("scenarios_stored_in_db", 0) if _ps else 0
                    
                    if scenarios_stored_in_db > 0:
                        logger.info(f"ℹ️  Skipping failure storage - {scenarios_stored_in_db} scenario(s) already stored individually in t_gen_resp")
                    else:
                        try:
                            # Get scenario name from tran_status if available
                            scenario_name = _get_scenario_name_from_tran_status(resolved_run_id, mcp_crawler, "GitHub failure storage")
                            
                            error_response = {
                                "run_id": resolved_run_id,
                                "status": "failure",
                                "jira_id": jira_id,
                                "error_message": error_message,
                                "error_stage": error_stage
                            }
                            
                            # Add scenarios array if available in process_result
                            if process_result and "processing_summary" in process_result:
                                scenarios = process_result["processing_summary"].get("scenarios", [])
                                if scenarios:
                                    error_response["scenarios"] = scenarios
                                    logger.info(f"📊 Added {len(scenarios)} scenarios to error_response for database storage")
                                    for idx, sc in enumerate(scenarios):
                                        steps_count = len(sc.get('successful_steps', [])) if isinstance(sc, dict) else 0
                                        logger.info(f"   Scenario {idx+1}: {sc.get('scenario_name', 'Unknown')} - {sc.get('status', 'Unknown')} - {steps_count} steps")
                            
                            video_filename = None
                            complete_scripts = None
                            if process_result and "processing_summary" in process_result:
                                failed_recordings = process_result["processing_summary"].get("failed_recordings", [])
                                if failed_recordings and len(failed_recordings) > 0:
                                    # ✅ FIX: Store ALL failed video filenames (comma-separated) not just the first one
                                    all_filenames = [r.get("zip_filename") for r in failed_recordings if r.get("zip_filename")]
                                    if all_filenames:
                                        video_filename = ",".join(all_filenames)
                                        logger.info(f"📹 Video filenames for database storage (GitHub failure, all videos): {video_filename}")
                                complete_scripts = process_result.get("step_definition_file")
                                if complete_scripts:
                                    logger.info(f"📄 Complete scripts extracted for database storage (GitHub failure)")
                            
                            # Store failure in t_gen_resp (MCP flow only)
                            if mcp_crawler:
                                store_success = store_generated_response(
                                    run_id=resolved_run_id,
                                    response_data=error_response,
                                    jira_id=jira_id,
                                    module_name=domain_selection_str,
                                    status="FAILED",
                                    scenario_name=scenario_name,
                                    video_filename=video_filename,
                                    complete_scripts=complete_scripts,
                                    feature_steps=feature_steps_json_text,
                                    feature_tags=feature_tags_text,
                                    feature_nm=feature_name,
                                    scn_tags=",".join(scenarios_metadata[0].get('tags', [])) if scenarios_metadata and scenarios_metadata[0].get('tags') else None,
                                    scn_nm=None,  # Scenario name will be set per-scenario in generate_bdd_script.py
                                    api_steps=api_steps_json
                                )
                                if store_success:
                                    logger.info(f"💾 GitHub failure stored in database for run ID: {resolved_run_id} with scenario: {scenario_name}")
                                else:
                                    logger.warning(f"⚠️  Failed to store GitHub failure in database for run ID: {resolved_run_id}")

                                # DON'T delete tran_status yet - let the outer exception handler do it
                                # This ensures scenario_name is still available if this exception is caught again
                            else:
                                # For non-MCP flow, just update if exists
                                update_response_status(resolved_run_id, "failure", f"{error_stage}: {error_message}")
                                logger.info(f"💾 GitHub failure stored in database for run ID: {resolved_run_id}")
                        except Exception as db_e:
                            logger.error(f"❌ Error storing GitHub failure in database: {str(db_e)}")

                    github_error = GitHubIntegrationError(
                        message=f"GitHub workflow failed at {error_stage}: {error_message} [Run ID: {resolved_run_id}]",
                        error_code="GITHUB_WORKFLOW_FAILED",
                        context={
                            "run_id": resolved_run_id,
                            "stage": error_stage,
                            "error_details": error_details,
                            "jira_id": jira_id,
                            "scenarios_processed": process_result.get("scenarios_processed", 0),
                            "can_retry": True
                        }
                    )
                    
                    # Mark exception as already stored to prevent duplicate storage
                    github_error._db_stored = True
                    raise github_error

            except HTTPException:
                # Re-raise HTTP exceptions (our detailed errors)
                raise
            except Exception as proc_error:
                logger.error(f"❌ Unexpected error processing feature file: {str(proc_error)}")

                # 🔽 Extract recording zip marker and attach ONLY the filename (no URLs) to the error
                zip_marker = _extract_recording_zip_url(proc_error)
                if zip_marker:
                    # Process multiple videos and get all filenames
                    all_filenames = _process_multiple_video_urls(zip_marker)
                    # Attach filename for middleware/handlers
                    try:
                        setattr(proc_error, "recording_zip", all_filenames)
                    except Exception:
                        pass
                    # also inject into the first arg if it’s a string (your extractor supports this)
                    try:
                        if proc_error.args:
                            first = proc_error.args[0]
                            tag = f"RECORDING_ZIP: {all_filenames}"
                            proc_error.args = ((f"{first} | {tag}") if isinstance(first, str) else tag,
                                               *proc_error.args[1:])
                        else:
                            proc_error.args = (f"RECORDING_ZIP: {all_filenames}",)
                    except Exception:
                        pass
                
                # 📦 CREATE ZIP FILE FOR FAILURE CASE (GitHub Push)
                # Extract step definition from error context if available
                # Initialize download variables to None (used in email notifications)
                download_zip_filename = None
                download_zip_url = None
                try:
                    step_definition_content = None
                    if hasattr(proc_error, "context") and isinstance(proc_error.context, dict):
                        step_definition_content = proc_error.context.get("step_definition_file")
                    
                    # If we have partial/failed step definitions, create zip file
                    if step_definition_content and isinstance(step_definition_content, str) and step_definition_content.strip():
                        logger.info(f"📦 Creating failure artifact zip for GitHub push failure: {jira_id}")
                        
                        # Create files dictionary for ZIP
                        files_to_zip = {
                            f"{jira_id}.feature": feature_content,
                            f"{jira_id}.ts": step_definition_content
                        }
                        
                        # Use _extra suffix to differentiate from success downloads
                        zip_name = f"{jira_id}_playwright_scripts"
                        download_token = f"{jira_id}"
                        
                        download_jira_id = temp_file_manager.create_zip_file(
                            files_dict=files_to_zip,
                            zip_name=zip_name,
                            jira_id=download_token,
                            expiry_hours=2
                        )
                        
                        # Create download URL
                        base_url = str(request.base_url).rstrip('/')
                        download_zip_url = f"{base_url}/Web-Automation/download/{download_token}"
                        download_zip_filename = f"{zip_name}.zip"
                        
                        logger.info(f"✅ Failure artifact zip created: {download_zip_filename}")
                        
                        # Remove step_definition_file from context (should not be in response)
                        if hasattr(proc_error, "context") and "step_definition_file" in proc_error.context:
                            del proc_error.context["step_definition_file"]
                            logger.info(f"🗑️  Removed step_definition_file from context (not needed in response)")
                        
                        # Add ONLY download fields to exception context (appears in error.context in response)
                        if not hasattr(proc_error, "context"):
                            proc_error.context = {}
                        proc_error.context["download_zip_filename"] = download_zip_filename
                        proc_error.context["download_zip_url"] = download_zip_url
                        proc_error.context["download_token"] = download_token
                        logger.info(f"📦 Added download fields to exception context")
                except Exception as zip_error:
                    logger.warning(f"Failed to create failure artifact zip: {str(zip_error)}")
                
                # Attempt failure notification
                try:
                    _slack_emails = selected_config.get("slack_channel_emails") or []
                    _email_recipients = selected_config.get("email_recipients") or []
                    def _normalize_list(v):
                        if isinstance(v, list):
                            return [x for x in v if isinstance(x, str) and x.strip()]
                        if isinstance(v, str) and v.strip():
                            return [s.strip() for s in v.split(",") if s.strip()]
                        return []
                    slack_list = _normalize_list(_slack_emails)
                    json_emails = _normalize_list(_email_recipients)
                    notify_list = notify_list_provided
                    human_primary = notify_list if notify_list else json_emails
                    team_recipients = slack_list + human_primary
                    base_url = str(request.base_url).rstrip('/')
                    recordings_download_base = f"{base_url}/Web-Automation/recordings/download"
                    # Try to extract additional details from custom exception context
                    scenarios_total = None
                    scenarios_passed = None
                    scenarios_failed = None
                    recordings = []
                    generated_ts_filename = f"{jira_id}.ts"
                    pr_url = None
                    pr_number = None
                    branch_name = None
                    jenkins_queue = None
                    jenkins_job_url = None
                    failed_reasons = None
                    failed_details = None
                    try:
                        if hasattr(proc_error, "context") and isinstance(proc_error.context, dict):
                            ctx = proc_error.context
                            ps = ctx.get("processing_summary") or {}
                            scenarios_total = ps.get("total_scenarios")
                            scenarios_passed = ps.get("successful_scenarios")
                            scenarios_failed = ps.get("failed_scenarios")
                            failed_reasons = ps.get("failed_reasons")
                            failed_details = ps.get("failed_details")
                            def _map_rec(arr):
                                mapped = []
                                for r in (arr or []):
                                    base = {k: v for k, v in r.items() if k != "zip_url"}
                                    zip_name = r.get("zip_filename") or _zip_filename_from_url(r.get("zip_url", ""))
                                    
                                    # Handle multiple video filenames (pipe/comma separated)
                                    video_files = _split_recording_filenames(zip_name)
                                    if len(video_files) > 1:
                                        for video_file in video_files:
                                            video_entry = base.copy()
                                            video_entry["zip_filename"] = video_file
                                            mapped.append(video_entry)
                                    else:
                                        base["zip_filename"] = video_files[0] if video_files else zip_name
                                        mapped.append(base)
                                return mapped
                            recordings = _map_rec(ps.get("success_recordings")) + _map_rec(ps.get("failed_recordings"))
                            generated_ts_filename = ctx.get("generated_ts_filename") or generated_ts_filename
                            # Extract PR/branch and Jenkins info from github_result if present
                            gh = ctx.get("github_result") or {}
                            gw = gh.get("github_workflow") or {}
                            gw_details = gw.get("details") or {}
                            pr_url = gw_details.get("pr_url") or pr_url
                            pr_number = gw_details.get("pr_number") or pr_number
                            branch_name = gh.get("final_branch_name") or gw_details.get("final_branch_name") or branch_name
                            jt = gh.get("jenkins_trigger") or {}
                            jt_details = jt.get("details") or {}
                            jenkins_queue = jt_details.get("queue_location") or jenkins_queue
                            # derive job URL if we have queue URL and job name
                            if jenkins_queue and isinstance(jenkins_queue, str) and jenkins_queue.startswith(("http://", "https://")):
                                try:
                                    from urllib.parse import urlparse
                                    parsed = urlparse(jenkins_queue)
                                    base = f"{parsed.scheme}://{parsed.netloc}"
                                    job_name = selected_config.get('jenkins-project-name', '').strip()
                                    if job_name:
                                        jenkins_job_url = f"{base}/job/{job_name}"
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    try:
                        logger.info(f"Notify: attempting failure summary (github) | jira={jira_id} run_id={resolved_run_id} recipients={len(team_recipients)}")
                        await asyncio.to_thread(send_run_summary_email, {
                        "run_id": resolved_run_id,
                        "status": "failure",
                        "jira_id": jira_id,
                        "selected_domain": domain_selection_str,
                        "push_to_github": True,
                        "record_video": record_video_enabled,
                        "generate_step_definitions": generate_step_definitions,
                        "github_repo": selected_config.get('github-repo', 'N/A'),
                        "jenkins_project": selected_config.get('jenkins-project-name', 'N/A'),
                        "error_message": f"{str(proc_error)} [Run ID: {resolved_run_id}]",
                        "error_stage": "github_push_or_workflow",
                        "pr_url": pr_url,
                        "pr_number": pr_number,
                        "branch_name": branch_name,
                        "jenkins_queue": jenkins_queue,
                        "jenkins_job_url": jenkins_job_url,
                        "scenarios_total": scenarios_total,
                        "scenarios_passed": scenarios_passed,
                        "scenarios_failed": scenarios_failed,
                        "recordings": (recordings if recordings else ([{"scenario_number": "?", "zip_filename": _zip_filename_from_url(getattr(proc_error, "recording_zip", ""))}] if getattr(proc_error, "recording_zip", None) else [])),
                        "recordings_download_base_url": recordings_download_base,
                        "scenario_failure_reasons": failed_reasons,
                        "scenario_failure_details": failed_details,
                        # Include failure artifact zip if created
                        "download_zip_filename": download_zip_filename,
                        "download_zip_url": download_zip_url,
                    }, team_recipients)
                    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPException, ValueError):
                        logger.warning("Notify: initial failure summary send failed (github), trying fallback recipients")
                        if notify_list and json_emails:
                            fallback_recipients = slack_list + json_emails
                            try:
                                logger.info(f"Notify: attempting failure summary (github, fallback) | jira={jira_id} run_id={resolved_run_id} recipients={len(fallback_recipients)}")
                                await asyncio.to_thread(send_run_summary_email, {
                                    "run_id": resolved_run_id,
                                    "status": "failure",
                                    "jira_id": jira_id,
                                    "selected_domain": domain_selection_str,
                                    "push_to_github": True,
                                    "record_video": record_video_enabled,
                                    "generate_step_definitions": generate_step_definitions,
                                    "github_repo": selected_config.get('github-repo', 'N/A'),
                                    "jenkins_project": selected_config.get('jenkins-project-name', 'N/A'),
                                    "error_message": f"{str(proc_error)} [Run ID: {resolved_run_id}]",
                                    "error_stage": "github_push_or_workflow",
                                    "pr_url": pr_url,
                                    "pr_number": pr_number,
                                    "branch_name": branch_name,
                                    "jenkins_queue": jenkins_queue,
                                    "jenkins_job_url": jenkins_job_url,
                                    "scenarios_total": scenarios_total,
                                    "scenarios_passed": scenarios_passed,
                                    "scenarios_failed": scenarios_failed,
                                    "recordings": (recordings if recordings else ([{"scenario_number": "?", "zip_filename": _zip_filename_from_url(getattr(proc_error, "recording_zip", ""))}] if getattr(proc_error, "recording_zip", None) else [])),
                                    "recordings_download_base_url": recordings_download_base,
                                    "scenario_failure_reasons": failed_reasons,
                                    "scenario_failure_details": failed_details,
                                    "download_zip_filename": download_zip_filename,
                                    "download_zip_url": download_zip_url,
                                }, fallback_recipients)
                            except Exception as fallback_exc:
                                logger.error(f"Notify: failure summary fallback send failed (github): {type(fallback_exc).__name__}: {fallback_exc}")
                except Exception as notify_exc:
                    logger.error(f"Notify: failure summary notification (github) failed: {type(notify_exc).__name__}: {notify_exc}")
                
                # === STORE FAILURE IN DATABASE AND CLEANUP ===
                # Only store if this is MCP flow and NOT already stored
                # Check for _db_stored marker attribute or GitHubIntegrationError type
                from utils.excepations import GitHubIntegrationError
                skip_storage = getattr(proc_error, '_db_stored', False) or isinstance(proc_error, GitHubIntegrationError)
                
                if mcp_crawler and not skip_storage:
                    try:
                        # Get scenario name from tran_status if available
                        scenario_name = _get_scenario_name_from_tran_status(resolved_run_id, mcp_crawler, "GitHub processing error")
                        
                        # Store failure response in t_gen_resp
                        failure_response = {
                            "run_id": resolved_run_id,
                            "status": "failure",
                            "error_message": str(proc_error),
                            "error_stage": "processing_error",
                            "jira_id": jira_id
                        }
                        
                        # Try to extract partial/complete step definitions for DB storage
                        complete_scripts = None
                        try:
                            # Prefer already-extracted local content (survives even if context key was deleted)
                            if 'step_definition_content' in locals() and isinstance(step_definition_content, str) and step_definition_content.strip():
                                complete_scripts = step_definition_content
                            # Fallback: from process_result if available
                            elif 'process_result' in locals() and process_result and isinstance(process_result, dict):
                                complete_scripts = process_result.get("step_definition_file")
                            # Last resort: from exception context (may be missing if removed earlier)
                            if not complete_scripts and hasattr(proc_error, 'context') and isinstance(proc_error.context, dict):
                                complete_scripts = proc_error.context.get("step_definition_file")
                            if complete_scripts:
                                logger.info(f"📄 Complete scripts prepared for database storage (processing_error)")
                        except Exception:
                            pass

                        store_success = store_generated_response(
                            run_id=resolved_run_id,
                            response_data=failure_response,
                            jira_id=jira_id,
                            module_name=domain_selection_str,
                            status="FAILED",  # Failure case
                            scenario_name=scenario_name,
                            feature_steps=feature_steps_json_text,
                            video_filename=video_filename,
                            complete_scripts=complete_scripts,
                            feature_tags=feature_tags_text,
                            feature_nm=feature_name,
                            scn_tags=",".join([s.get('tags', []) for s in scenarios_metadata][0]) if scenarios_metadata and scenarios_metadata[0].get('tags') else None,
                            scn_nm=None,  # Scenario name will be set per-scenario in generate_bdd_script.py
                            api_steps=api_steps_json
                        )
                        
                        if store_success:
                            logger.info(f"💾 Failure stored in database for run ID: {resolved_run_id}")
                        else:
                            logger.warning(f"⚠️  Failed to store failure in database for run ID: {resolved_run_id}")
                            
                    except Exception as db_fail_e:
                        logger.error(f"❌ Error storing failure in database: {str(db_fail_e)}")
                else:
                    logger.info(f"ℹ️  Skipping duplicate storage for GitHubIntegrationError")
                
                # Delete tran_status after exception handling (regardless of exception type)
                if mcp_crawler:
                    try:
                        from utils.run_id_manager import delete_tran_status
                        delete_tran_status(resolved_run_id, allow_inner_deletion=True)
                        logger.info(f"🗑️  Deleted tran_status for failed run_id: {resolved_run_id}")
                    except Exception as del_e:
                        logger.warning(f"⚠️  Failed to delete tran_status: {del_e}")
                
                raise proc_error
        else:
            # Process locally without GitHub push
            logger.info(f"Processing feature file locally for JIRA ID: {jira_id}")
            
            # Extract domain selection for MCP UID generation even when not pushing to GitHub
            domain_selection_str = None
            if domain_selection and mcp_crawler:
                # If MCP is selected and domain is provided, use it for UID generation
                domain_selection_str = domain_selection.value
                logger.info(f"🔧 MCP Local Processing | Using domain '{domain_selection_str}' for UID generation")
            elif mcp_crawler:
                logger.info(f"🔧 MCP Local Processing | No domain selected, using legacy UID format")
            
            try:
                process_result = await process_feature_files(
                    content=feature_content,
                    jira_id=jira_id,
                    s3_path=None,
                    file_version=file_version,
                    push_to_github=False,
                    selected_domain=domain_selection_str,
                    target_github_repo=None,
                    jenkins_project_name=None,
                    github_access_token=None,
                    jenkins_username=None,
                    jenkins_api_token=None,
                    record_video=record_video_enabled,
                    generate_step_definitions=generate_step_definitions,
                    custom_branch_base=None,
                    dry_run_strategy=mcp_crawler,
                    run_id=resolved_run_id,  # NEW: Pass run_id for status tracking
                    feature_tags=feature_tags_text,
                    feature_nm=feature_name,
                    scn_tags=",".join(scenarios_metadata[0].get('tags', [])) if scenarios_metadata and scenarios_metadata[0].get('tags') else None,
                    scn_nm=None,  # Scenario name will be set per-scenario in generate_bdd_script.py
                    api_steps=api_steps_json
                )

                if process_result and process_result.get("success"):
                    # If preview requested, return preview steps immediately
                    if preview_mode:
                        return {"testSteps": process_result.get("preview_steps", [])}

                    typescript_content = process_result.get("step_definition_file", "// No step definition generated")

                    logger.info(f"✅ Successfully processed feature file locally")

                    # Create files dictionary for ZIP only if processing succeeded
                    files_to_zip = {
                        f"{jira_id}.feature": feature_content,
                        f"{jira_id}.ts": typescript_content
                    }
                    logger.info("files_to_zip", files_to_zip)

                    # Auto-generate ZIP name
                    zip_name = f"{jira_id}_playwright_scripts"

                    download_jira_id = temp_file_manager.create_zip_file(
                        files_dict=files_to_zip,
                        zip_name=zip_name,
                        jira_id=jira_id,
                        expiry_hours=2
                    )

                    # Create download URL using jira_id
                    base_url = str(request.base_url).rstrip('/')
                    download_url = f"{base_url}/Web-Automation/download/{jira_id}"

                    end_time = time.time()
                    execution_time = end_time - start_time
                    logger.info(f"Execution time: {execution_time} seconds")
                    processing_summary = process_result.get("processing_summary", {})
                    success_recordings = processing_summary.get("success_recordings", [])
                    failed_recordings = processing_summary.get("failed_recordings", [])
                    failed_reasons = processing_summary.get("failed_reasons", [])
                    failed_details = processing_summary.get("failed_details", [])
                    def _map_rec(arr):
                        mapped = []
                        for r in (arr or []):
                            base = {k: v for k, v in r.items() if k != "zip_url"}
                            zip_name = r.get("zip_filename") or _zip_filename_from_url(r.get("zip_url", ""))
                            
                            # Handle multiple video filenames (pipe/comma separated)
                            video_files = _split_recording_filenames(zip_name)
                            if len(video_files) > 1:
                                for video_file in video_files:
                                    video_entry = base.copy()
                                    video_entry["zip_filename"] = video_file
                                    mapped.append(video_entry)
                            else:
                                base["zip_filename"] = video_files[0] if video_files else zip_name
                                mapped.append(base)
                        return mapped
                    recordings = _map_rec(success_recordings) + _map_rec(failed_recordings)

                    # Create response without null fields for local processing
                    response_data = {
                        "run_id": resolved_run_id,
                        "jira_id": jira_id,
                        "status": "success",
                        "message": f"Successfully processed feature file. Download ready: {jira_id} | Scenarios: {processing_summary.get('successful_scenarios', 0)}/{processing_summary.get('total_scenarios', 0)} [Run ID: {resolved_run_id}]",
                        "version": file_version,
                        "download_token": jira_id,
                        "download_url": download_url,
                        "zip_filename": f"{zip_name}.zip",
                        "recordings": recordings
                    }
                    # Attempt summary email for local processing; allow sending with notify_email even without domain
                    try:
                        domain_selection_str = domain_selection.value if domain_selection else None
                        selected_config = {}
                        _slack_emails = []
                        _email_recipients = []
                        if domain_selection or notify_email:
                            repo_modules = load_repo_modules()
                            selected_config = repo_modules.get(domain_selection_str, {}) if domain_selection else {}
                            _slack_emails = selected_config.get("slack_channel_emails") or []
                            _email_recipients = selected_config.get("email_recipients") or []
                        def _normalize_list(v):
                            if isinstance(v, list):
                                return [x for x in v if isinstance(x, str) and x.strip()]
                            if isinstance(v, str) and v.strip():
                                return [s.strip() for s in v.split(",") if s.strip()]
                            return []
                        slack_list = _normalize_list(_slack_emails)
                        json_emails = _normalize_list(_email_recipients)
                        notify_list = notify_list_provided
                        human_primary = notify_list if notify_list else json_emails
                        team_recipients = slack_list + human_primary
                        if team_recipients:
                            summary_payload_local_success = {
                                "run_id": resolved_run_id,
                                "status": "success",
                                "jira_id": jira_id,
                                "selected_domain": domain_selection_str,
                                "push_to_github": False,
                                "record_video": record_video_enabled,
                                "generate_step_definitions": generate_step_definitions,
                                "version": file_version,
                                "execution_seconds": execution_time,
                                "scenarios_total": processing_summary.get("total_scenarios", 0),
                                "scenarios_passed": processing_summary.get("successful_scenarios", 0),
                                "scenarios_failed": processing_summary.get("failed_scenarios", 0),
                                "recordings": recordings,
                                "recordings_download_base_url": f"{base_url}/Web-Automation/recordings/download",
                                "scenario_failure_reasons": failed_reasons,
                                "scenario_failure_details": failed_details,
                                "download_zip_filename": f"{jira_id}.zip",
                                "download_zip_url": download_url,
                            }
                            try:
                                logger.info(f"Notify: attempting success summary (local) | jira={jira_id} run_id={resolved_run_id} recipients={len(team_recipients)}")
                                await asyncio.to_thread(send_run_summary_email, summary_payload_local_success, team_recipients)
                            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPException, ValueError):
                                logger.warning("Notify: initial success summary send failed (local), trying fallback recipients")
                                if notify_list and json_emails:
                                    fallback_recipients = slack_list + json_emails
                                    try:
                                        logger.info(f"Notify: attempting success summary (local, fallback) | jira={jira_id} run_id={resolved_run_id} recipients={len(fallback_recipients)}")
                                        await asyncio.to_thread(send_run_summary_email, summary_payload_local_success, fallback_recipients)
                                    except Exception as fallback_exc:
                                        logger.error(f"Notify: success summary fallback send failed (local): {type(fallback_exc).__name__}: {fallback_exc}")
                    except Exception as notify_exc:
                        logger.error(f"Notify: success summary notification (local) failed: {type(notify_exc).__name__}: {notify_exc}")
                    
                    # === STORE RESPONSE IN DATABASE ===
                    # Skip if scenarios were already stored individually in generate_bdd_script.py
                    # This happens when dry_run_strategy=True with status_tracking_available and run_id
                    scenarios_stored_in_db = processing_summary.get("scenarios_stored_in_db", 0)
                    
                    if scenarios_stored_in_db > 0:
                        logger.info(f"ℹ️  Skipping overall storage - {scenarios_stored_in_db} scenario(s) already stored individually in t_gen_resp")
                    else:
                        try:
                            # Get domain/module name even for local processing
                            local_module = domain_selection_str if domain_selection else None

                            # Get scenario name from tran_status if available
                            scenario_name = _get_scenario_name_from_tran_status(resolved_run_id, mcp_crawler, "local success storage")

                            # Create database storage data with scenarios array
                            db_storage_data = response_data.copy()
                            db_storage_data["scenarios"] = processing_summary.get("scenarios", [])
                            
                            # Determine database status based on scenario results
                            failed_count = processing_summary.get("failed_scenarios", 0)
                            db_status = "FAILED" if failed_count > 0 else "PASSED"
                            logger.info(f"📊 Local - Storing with status: {db_status} (failed: {failed_count})")
                            video_filename = None
                            if recordings and len(recordings) > 0:
                                # ✅ FIX: Store ALL video filenames (comma-separated) not just the first one
                                all_filenames = [r.get("zip_filename") for r in recordings if r.get("zip_filename")]
                                if all_filenames:
                                    video_filename = ",".join(all_filenames)
                                    logger.info(f"📹 Video filenames for database storage (all videos): {video_filename}")
                            elif db_status == "FAILED":
                                if failed_recordings and len(failed_recordings) > 0:
                                    # ✅ FIX: Store ALL failed video filenames (comma-separated) not just the first one
                                    all_filenames = [r.get("zip_filename") for r in failed_recordings if r.get("zip_filename")]
                                    if all_filenames:
                                        video_filename = ",".join(all_filenames)
                                        logger.info(f"📹 Video filenames for database storage (from failed_recordings, all videos): {video_filename}")
                            complete_scripts = typescript_content
                            
                            store_success = store_generated_response(
                                run_id=resolved_run_id,
                                response_data=db_storage_data,
                                jira_id=jira_id,
                                module_name=local_module,
                                status=db_status,
                                scenario_name=scenario_name,
                                video_filename=video_filename,
                                complete_scripts=complete_scripts,
                                feature_steps=feature_steps_json_text,
                                feature_tags=feature_tags_text,
                                feature_nm=feature_name,
                                scn_tags=",".join(scenarios_metadata[0].get('tags', [])) if scenarios_metadata and scenarios_metadata[0].get('tags') else None,
                                scn_nm=None,  # Scenario name will be set per-scenario in generate_bdd_script.py
                                api_steps=api_steps_json
                            )
                            if store_success:
                                logger.info(f"💾 Response stored in database for run ID: {resolved_run_id}")
                            else:
                                logger.warning(f"⚠️  Failed to store response in database for run ID: {resolved_run_id}")
                        except Exception as e:
                            logger.error(f"❌ Error storing response in database: {str(e)}")
                    
                    # Delete tran_status on success (MCP flow only)
                    if mcp_crawler:
                        try:
                            from utils.run_id_manager import delete_tran_status
                            delete_tran_status(resolved_run_id, allow_inner_deletion=True)
                            logger.info(f"🗑️  Deleted tran_status for successful run_id: {resolved_run_id}")
                        except Exception as del_e:
                            logger.warning(f"⚠️  Failed to delete tran_status: {del_e}")

                    # === AUTO-HEAL UPDATE (Local Processing Success) ===
                    # Update t_autoHealScn if this was an autoheal execution
                    try:
                        autoheal_result = AutoHealUpdateService.update_autoheal_on_completion(
                            run_id=resolved_run_id,
                            new_stp_loc_map=None,  # Will be fetched from t_gen_resp
                            new_complete_scripts=None,  # Will be fetched from t_gen_resp
                            module_name=domain_selection_str,  # Use domain_selection_str, always defined
                            feature_tags=feature_tags_text,
                            feature_name=feature_name,
                            scenario_tags=",".join(scenarios_metadata[0].get('tags', [])) if scenarios_metadata and scenarios_metadata[0].get('tags') else None,
                            scenario_name=None
                        )
                        if autoheal_result.get("status") not in ["skipped"]:
                            logger.info(f"🔄 Auto-heal update result: {autoheal_result.get('status')} - {autoheal_result.get('message', '')}")
                    except Exception as ah_e:
                        logger.warning(f"⚠️  Auto-heal update failed (non-blocking): {ah_e}")

                    return response_data
                else:
                    # Processing failed - raise a regular exception to get full traceback
                    error_msg = process_result.get('message',
                                                   'Unknown processing error') if process_result else 'No result returned'
                    logger.error(f"❌ Local processing failed: {error_msg}")

                    # Create a custom exception with the error details
                    class FeatureProcessingError(BaseCustomException):
                        def __init__(self, message, jira_id, stage):
                            super().__init__(
                                message=message,
                                error_code="FEATURE_PROCESSING_FAILED",
                                context={"jira_id": jira_id, "stage": stage}
                            )

                    # Attempt failure notification for local processing if a domain is provided
                    try:
                        domain_selection_str = domain_selection.value if domain_selection else None
                        selected_config = {}
                        _slack_emails = []
                        _email_recipients = []
                        if domain_selection:
                            repo_modules = load_repo_modules()
                            selected_config = repo_modules.get(domain_selection_str, {})
                            _slack_emails = selected_config.get("slack_channel_emails") or []
                            _email_recipients = selected_config.get("email_recipients") or []
                        def _normalize_list(v):
                            if isinstance(v, list):
                                return [x for x in v if isinstance(x, str) and x.strip()]
                            if isinstance(v, str) and v.strip():
                                return [s.strip() for s in v.split(",") if s.strip()]
                            return []
                        slack_list = _normalize_list(_slack_emails)
                        json_emails = _normalize_list(_email_recipients)
                        notify_list = notify_list_provided
                        human_primary = notify_list if notify_list else json_emails
                        team_recipients = slack_list + human_primary
                        if team_recipients:
                            # Build recordings and base URL for local failure notifications
                            base_url = str(request.base_url).rstrip('/')
                            recordings_download_base = f"{base_url}/Web-Automation/recordings/download"
                            processing_summary = process_result.get("processing_summary", {}) if process_result else {}
                            success_recordings = processing_summary.get("success_recordings", [])
                            failed_recordings = processing_summary.get("failed_recordings", [])
                            failed_reasons = processing_summary.get("failed_reasons", [])
                            failed_details = processing_summary.get("failed_details", [])
                            def _map_rec(arr):
                                mapped = []
                                for r in (arr or []):
                                    base = {k: v for k, v in r.items() if k != "zip_url"}
                                    zip_name = r.get("zip_filename") or _zip_filename_from_url(r.get("zip_url", ""))
                                    video_files = _split_recording_filenames(zip_name)
                                    if len(video_files) > 1:
                                        for video_file in video_files:
                                            video_entry = base.copy()
                                            video_entry["zip_filename"] = video_file
                                            mapped.append(video_entry)
                                    else:
                                        base["zip_filename"] = video_files[0] if video_files else zip_name
                                        mapped.append(base)
                                return mapped
                            recordings = _map_rec(success_recordings) + _map_rec(failed_recordings)
                            scenarios_total = processing_summary.get("total_scenarios")
                            scenarios_passed = processing_summary.get("successful_scenarios")
                            scenarios_failed = processing_summary.get("failed_scenarios")
                            summary_payload_local_failure = {
                                "run_id": resolved_run_id,
                                "status": "failure",
                                "jira_id": jira_id,
                                "selected_domain": domain_selection_str,
                                "push_to_github": False,
                                "record_video": record_video_enabled,
                                "generate_step_definitions": generate_step_definitions,
                                "version": file_version,
                                "error_message": f"{error_msg} [Run ID: {resolved_run_id}]",
                                "error_stage": "process_feature_files_local",
                                "scenarios_total": scenarios_total,
                                "scenarios_passed": scenarios_passed,
                                "scenarios_failed": scenarios_failed,
                                "recordings": recordings,
                                "recordings_download_base_url": recordings_download_base,
                                "scenario_failure_reasons": failed_reasons,
                                "scenario_failure_details": failed_details,
                            }
                            try:
                                logger.info(f"Notify: attempting failure summary (local) | jira={jira_id} run_id={resolved_run_id} recipients={len(team_recipients)}")
                                await asyncio.to_thread(send_run_summary_email, summary_payload_local_failure, team_recipients)
                            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPException, ValueError):
                                logger.warning("Notify: initial failure summary send failed (local), trying fallback recipients")
                                if notify_list and json_emails:
                                    fallback_recipients = slack_list + json_emails
                                    try:
                                        logger.info(f"Notify: attempting failure summary (local, fallback) | jira={jira_id} run_id={resolved_run_id} recipients={len(fallback_recipients)}")
                                        await asyncio.to_thread(send_run_summary_email, summary_payload_local_failure, fallback_recipients)
                                    except Exception as fallback_exc:
                                        logger.error(f"Notify: failure summary fallback send failed (local): {type(fallback_exc).__name__}: {fallback_exc}")
                    except Exception as notify_exc:
                        logger.error(f"Notify: failure summary notification (local) failed: {type(notify_exc).__name__}: {notify_exc}")
                    
                    # === STORE FAILURE IN DATABASE AND CLEANUP ===
                    # Skip if scenarios were already stored individually in generate_bdd_script.py
                    # Access processing_summary from process_result (not locals)
                    _ps = process_result.get("processing_summary", {}) if process_result and isinstance(process_result, dict) else {}
                    scenarios_stored_in_db = _ps.get("scenarios_stored_in_db", 0) if _ps else 0
                    
                    if scenarios_stored_in_db > 0:
                        logger.info(f"ℹ️  Skipping local failure storage - {scenarios_stored_in_db} scenario(s) already stored individually in t_gen_resp")
                    else:
                        try:
                            # Get scenario name from tran_status if available
                            scenario_name = _get_scenario_name_from_tran_status(resolved_run_id, mcp_crawler, "local failure storage")
                            
                            error_response = {
                                "run_id": resolved_run_id,
                                "status": "failure",
                                "jira_id": jira_id,
                                "error_message": error_msg,
                                "error_stage": "process_feature_files_local"
                            }
                            
                            # Add scenarios array if available in process_result
                            scenarios = []
                            
                            # Try to get from process_result
                            if process_result and isinstance(process_result, dict):
                                if "processing_summary" in process_result:
                                    scenarios = process_result["processing_summary"].get("scenarios", [])
                                    logger.info(f"📊 Found {len(scenarios)} scenarios in process_result")
                            
                            if scenarios:
                                error_response["scenarios"] = scenarios
                                logger.info(f"📊 Added {len(scenarios)} scenarios to local error_response for database storage")
                                for idx, sc in enumerate(scenarios):
                                    steps_count = len(sc.get('successful_steps', [])) if isinstance(sc, dict) else 0
                                    logger.info(f"   Scenario {idx+1}: {sc.get('scenario_name', 'Unknown')} - {sc.get('status', 'Unknown')} - {steps_count} steps")
                            
                            video_filename = None
                            complete_scripts = None
                            if process_result and isinstance(process_result, dict):
                                success_recordings = process_result.get("processing_summary", {}).get("success_recordings", [])
                                failed_recordings = process_result.get("processing_summary", {}).get("failed_recordings", [])
                                all_recordings = success_recordings + failed_recordings
                                if all_recordings and len(all_recordings) > 0:
                                    video_filename = all_recordings[0].get("zip_filename")
                                    logger.info(f"📹 Video filename for database storage (failure): {video_filename}")
                                complete_scripts = process_result.get("step_definition_file")
                            
                            # Store failure in t_gen_resp (MCP flow only)
                            if mcp_crawler:
                                store_success = store_generated_response(
                                    run_id=resolved_run_id,
                                    response_data=error_response,
                                    jira_id=jira_id,
                                    module_name=domain_selection_str if domain_selection else None,
                                    status="FAILED",
                                    scenario_name=scenario_name,
                                    video_filename=video_filename,
                                    complete_scripts=complete_scripts,
                                    feature_steps=feature_steps_json_text,
                                    feature_tags=feature_tags_text,
                                    feature_nm=feature_name,
                                    scn_tags=",".join(scenarios_metadata[0].get('tags', [])) if scenarios_metadata and scenarios_metadata[0].get('tags') else None,
                                    scn_nm=None,  # Scenario name will be set per-scenario in generate_bdd_script.py
                                    api_steps=api_steps_json
                                )
                                if store_success:
                                    logger.info(f"💾 Failure stored in database for run ID: {resolved_run_id}")
                                else:
                                    logger.warning(f"⚠️  Failed to store failure in database for run ID: {resolved_run_id}")
                                
                                # DON'T delete tran_status yet - let the outer exception handler do it
                                # This ensures scenario_name is still available if this exception is caught again
                            else:
                                # For non-MCP flow, just update if exists
                                update_response_status(resolved_run_id, "failure", error_msg)
                                logger.info(f"💾 Failure status stored in database for run ID: {resolved_run_id}")
                        except Exception as db_e:
                            logger.error(f"❌ Error storing failure in database: {str(db_e)}")
                    
                    # Raise regular exception instead of HTTPException
                    # Include run ID in the error message
                    enhanced_error = f"{error_msg} [Run ID: {resolved_run_id}]"
                    failure_exception = FeatureProcessingError(enhanced_error, jira_id, "process_feature_files_local")
                    
                    # Mark exception as already stored to prevent duplicate storage
                    failure_exception._db_stored = True
                    raise failure_exception

            except HTTPException:
                # Re-raise HTTP exceptions (our detailed errors)
                raise
            except Exception as proc_error:
                logger.error(f"❌ Unexpected error occurred while  processing feature file locally: {str(proc_error)}")

                # === EXTRACT SCENARIOS DATA FROM EXCEPTION ===
                # Check if exception has processing_summary attached (from generate_bdd_script.py)
                scenarios_from_exception = []
                if hasattr(proc_error, 'processing_summary'):
                    processing_summary = getattr(proc_error, 'processing_summary', {})
                    scenarios_from_exception = processing_summary.get("scenarios", [])
                    logger.info(f"📊 Found {len(scenarios_from_exception)} scenarios attached to exception")
                    for idx, sc in enumerate(scenarios_from_exception):
                        steps_count = len(sc.get('successful_steps', [])) if isinstance(sc, dict) else 0
                        logger.info(f"   Scenario {idx+1}: {sc.get('scenario_name', 'Unknown')} - {sc.get('status', 'Unknown')} - {steps_count} steps")

                recordings_from_exception = []
                if hasattr(proc_error, 'context') and isinstance(proc_error.context, dict):
                    technical_details = proc_error.context.get("technical_details", {})
                    if isinstance(technical_details, dict):
                        recording_filename = technical_details.get("recording_filename")
                        if recording_filename:
                            recordings_from_exception = [{
                                "scenario_number": 1,
                                "zip_filename": recording_filename
                            }]
                            logger.info(f"📹 Extracted video filename from exception context: {recording_filename}")

                # 🔽 NEW: extract and normalize the recording-zip URL, return *just* a download link
                zip_marker = _extract_recording_zip_url(proc_error)
                if zip_marker:
                    # Process multiple videos and normalize URLs
                    urls = [url.strip() for url in zip_marker.split(" | ") if url.strip()]
                    http_zip_urls = [_normalize_zip_url_for_http(url, request) for url in urls]
                    all_urls = ", ".join(http_zip_urls)
                    # 👇 Attach the URL to the exception; middleware will add it to the error JSON
                    try:
                        setattr(proc_error, "recording_zip", all_urls)
                    except Exception:
                        pass
                    # also inject into the first arg if it’s a string (your extractor supports this)
                    try:
                        if proc_error.args:
                            first = proc_error.args[0]
                            tag = f"RECORDING_ZIP: {all_urls}"
                            proc_error.args = ((f"{first} | {tag}") if isinstance(first, str) else tag,
                                               *proc_error.args[1:])
                        else:
                            proc_error.args = (f"RECORDING_ZIP: {all_urls}",)
                    except Exception:
                        pass
                
                # 📦 CREATE ZIP FILE FOR FAILURE CASE (Local Processing)
                # Extract step definition from process_result or error context if available
                # Initialize download variables to None (used in email notifications)
                download_zip_filename = None
                download_zip_url = None
                try:
                    step_definition_content = None
                    
                    # Try to get from process_result first
                    if 'process_result' in locals() and process_result and isinstance(process_result, dict):
                        step_definition_content = process_result.get("step_definition_file")
                    
                    # If not available, try to extract from error context
                    if not step_definition_content and hasattr(proc_error, "context") and isinstance(proc_error.context, dict):
                        step_definition_content = proc_error.context.get("step_definition_file")
                    
                    # If we have partial/failed step definitions, create zip file
                    if step_definition_content and isinstance(step_definition_content, str) and step_definition_content.strip():
                        logger.info(f"📦 Creating failure artifact zip for local processing failure: {jira_id}")
                        
                        # Create files dictionary for ZIP
                        files_to_zip = {
                            f"{jira_id}.feature": feature_content,
                            f"{jira_id}.ts": step_definition_content
                        }
                        
                        # Use _extra suffix to differentiate from success downloads
                        zip_name = f"{jira_id}_playwright_scripts"
                        download_token = f"{jira_id}"
                        
                        download_jira_id = temp_file_manager.create_zip_file(
                            files_dict=files_to_zip,
                            zip_name=zip_name,
                            jira_id=download_token,
                            expiry_hours=2
                        )
                        
                        # Create download URL
                        base_url = str(request.base_url).rstrip('/')
                        download_zip_url = f"{base_url}/Web-Automation/download/{download_token}"
                        download_zip_filename = f"{zip_name}.zip"
                        
                        logger.info(f"✅ Failure artifact zip created: {download_zip_filename}")
                        
                        # Remove step_definition_file from context (should not be in response)
                        if hasattr(proc_error, "context") and "step_definition_file" in proc_error.context:
                            del proc_error.context["step_definition_file"]
                            logger.info(f"🗑️  Removed step_definition_file from context (not needed in response)")
                        
                        # Add ONLY download fields to exception context (appears in error.context in response)
                        if not hasattr(proc_error, "context"):
                            proc_error.context = {}
                        proc_error.context["download_zip_filename"] = download_zip_filename
                        proc_error.context["download_zip_url"] = download_zip_url
                        proc_error.context["download_token"] = download_token
                        logger.info(f"📦 Added download fields to exception context")
                except Exception as zip_error:
                    logger.warning(f"Failed to create failure artifact zip: {str(zip_error)}")
                
                # Attempt failure notification even on unexpected exception (local path)
                try:
                    domain_selection_str = domain_selection.value if domain_selection else None
                    selected_config = {}
                    _slack_emails = []
                    _email_recipients = []
                    if domain_selection or notify_email:
                        repo_modules = load_repo_modules()
                        selected_config = repo_modules.get(domain_selection_str, {}) if domain_selection else {}
                        _slack_emails = selected_config.get("slack_channel_emails") or []
                        _email_recipients = selected_config.get("email_recipients") or []
                    def _normalize_list(v):
                        if isinstance(v, list):
                            return [x for x in v if isinstance(x, str) and x.strip()]
                        if isinstance(v, str) and v.strip():
                            return [s.strip() for s in v.split(",") if s.strip()]
                        return []
                    slack_list = _normalize_list(_slack_emails)
                    json_emails = _normalize_list(_email_recipients)
                    notify_list = notify_list_provided
                    human_primary = notify_list if notify_list else json_emails
                    team_recipients = slack_list + human_primary
                    if team_recipients:
                        # Derive recordings and scenario details for local exception notifications
                        base_url = str(request.base_url).rstrip('/')
                        recordings_download_base = f"{base_url}/Web-Automation/recordings/download"
                        scenarios_total = None
                        scenarios_passed = None
                        scenarios_failed = None
                        failed_reasons = None
                        failed_details = None
                        recordings = []
                        try:
                            if hasattr(proc_error, "context") and isinstance(proc_error.context, dict):
                                ps = proc_error.context.get("processing_summary") or {}
                                scenarios_total = ps.get("total_scenarios")
                                scenarios_passed = ps.get("successful_scenarios")
                                scenarios_failed = ps.get("failed_scenarios")
                                failed_reasons = ps.get("failed_reasons")
                                failed_details = ps.get("failed_details")
                                def _map_rec(arr):
                                    mapped = []
                                    for r in (arr or []):
                                        base = {k: v for k, v in r.items() if k != "zip_url"}
                                        zip_name = r.get("zip_filename") or _zip_filename_from_url(r.get("zip_url", ""))
                                        video_files = _split_recording_filenames(zip_name)
                                        if len(video_files) > 1:
                                            for video_file in video_files:
                                                video_entry = base.copy()
                                                video_entry["zip_filename"] = video_file
                                                mapped.append(video_entry)
                                        else:
                                            base["zip_filename"] = video_files[0] if video_files else zip_name
                                            mapped.append(base)
                                    return mapped
                                recordings = _map_rec(ps.get("success_recordings")) + _map_rec(ps.get("failed_recordings"))
                        except Exception:
                            pass
                        if not recordings and getattr(proc_error, "recording_zip", None):
                            try:
                                zip_name = _zip_filename_from_url(getattr(proc_error, "recording_zip", ""))
                                split_names = _split_recording_filenames(zip_name)
                                if split_names:
                                    recordings = [{"scenario_number": "?", "zip_filename": n} for n in split_names]
                                else:
                                    recordings = [{"scenario_number": "?", "zip_filename": zip_name}] if zip_name else []
                            except Exception:
                                pass
                        if recordings and hasattr(proc_error, "context") and isinstance(proc_error.context, dict):
                            if "recordings" not in proc_error.context:
                                proc_error.context["recordings"] = recordings
                                logger.info(f"📹 Added {len(recordings)} recordings to exception context for normalized error response")
                        summary_payload_local_exception = {
                            "run_id": resolved_run_id,
                            "status": "failure",
                            "jira_id": jira_id,
                            "selected_domain": domain_selection_str,
                            "push_to_github": False,
                            "record_video": record_video_enabled,
                            "generate_step_definitions": generate_step_definitions,
                            "version": file_version,
                            "error_message": f"{str(proc_error)} [Run ID: {resolved_run_id}]",
                            "error_stage": "process_feature_files_local_exception",
                            "scenarios_total": scenarios_total,
                            "scenarios_passed": scenarios_passed,
                            "scenarios_failed": scenarios_failed,
                            "recordings": recordings,
                            "recordings_download_base_url": recordings_download_base,
                            "scenario_failure_reasons": failed_reasons,
                            "scenario_failure_details": failed_details,
                            "download_zip_filename": download_zip_filename,
                            "download_zip_url": download_zip_url,
                        }
                        try:
                            logger.info(f"Notify: attempting failure summary (local exception) | jira={jira_id} run_id={resolved_run_id} recipients={len(team_recipients)}")
                            await asyncio.to_thread(send_run_summary_email, summary_payload_local_exception, team_recipients)
                        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPException, ValueError):
                            logger.warning("Notify: initial failure summary send failed (local exception), trying fallback recipients")
                            if notify_list and json_emails:
                                fallback_recipients = slack_list + json_emails
                                try:
                                    summary_payload_local_exception["download_zip_filename"] = download_zip_filename
                                    summary_payload_local_exception["download_zip_url"] = download_zip_url
                                    logger.info(f"Notify: attempting failure summary (local exception, fallback) | jira={jira_id} run_id={resolved_run_id} recipients={len(fallback_recipients)}")
                                    await asyncio.to_thread(send_run_summary_email, summary_payload_local_exception, fallback_recipients)
                                except Exception as fallback_exc:
                                    logger.error(f"Notify: failure summary fallback send failed (local exception): {type(fallback_exc).__name__}: {fallback_exc}")
                except Exception as notify_exc:
                    logger.error(f"Notify: failure summary notification (local exception) failed: {type(notify_exc).__name__}: {notify_exc}")
                
                # === STORE EXCEPTION IN DATABASE AND CLEANUP ===
                # Skip if this is a known error type that already stored the failure
                # Check for _db_stored marker attribute set by inner handlers
                skip_storage = getattr(proc_error, '_db_stored', False) or isinstance(proc_error, BaseCustomException)
                
                if skip_storage:
                    logger.info(f"ℹ️  Skipping duplicate storage for already-handled error: {type(proc_error).__name__}")
                
                try:
                    if not skip_storage:
                        # Get scenario name from tran_status if available
                        scenario_name = _get_scenario_name_from_tran_status(resolved_run_id, mcp_crawler, "local exception storage")
                        
                        error_response = {
                            "run_id": resolved_run_id,
                            "status": "failure",
                            "jira_id": jira_id,
                            "error_message": str(proc_error),
                            "error_stage": "process_feature_files_local_exception"
                        }
                        
                        # Add scenarios array if extracted from exception
                        if scenarios_from_exception:
                            error_response["scenarios"] = scenarios_from_exception
                            logger.info(f"📊 Added {len(scenarios_from_exception)} scenarios to outer error_response for database storage")
                        
                        video_filename_for_storage = None
                        complete_scripts_for_storage = None
                        
                        logger.info(f"🔍 DEBUG: recordings_from_exception={recordings_from_exception}, zip_marker={zip_marker}")
                        
                        if recordings_from_exception and len(recordings_from_exception) > 0:
                            video_filename_for_storage = recordings_from_exception[0].get("zip_filename")
                            logger.info(f"📹 Video filename for database storage (from exception context): {video_filename_for_storage}")
                        
                        if not video_filename_for_storage and zip_marker:
                            # Extract filename from the zip_marker (which contains the actual S3 URL or filename)
                            video_filename_for_storage = _zip_filename_from_url(zip_marker)
                            logger.info(f"📹 Video filename for database storage (from zip_marker): {video_filename_for_storage}")
                        
                        if not video_filename_for_storage and hasattr(proc_error, 'recording_zip'):
                            recording_zip_attr = getattr(proc_error, 'recording_zip', None)
                            if recording_zip_attr:
                                video_filename_for_storage = _zip_filename_from_url(recording_zip_attr)
                                logger.info(f"📹 Video filename for database storage (from exception.recording_zip): {video_filename_for_storage}")
                        
                        if hasattr(proc_error, 'context') and isinstance(proc_error.context, dict):
                            # Prepare complete scripts for storage with robust sourcing
                            try:
                                if 'step_definition_content' in locals() and isinstance(step_definition_content, str) and step_definition_content.strip():
                                    complete_scripts_for_storage = step_definition_content
                                elif 'process_result' in locals() and process_result and isinstance(process_result, dict):
                                    complete_scripts_for_storage = process_result.get("step_definition_file")
                                if not complete_scripts_for_storage:
                                    complete_scripts_for_storage = proc_error.context.get("step_definition_file")
                                if complete_scripts_for_storage:
                                    logger.info(f"📄 Complete scripts prepared for database storage (exception handler)")
                            except Exception:
                                pass
                        
                        # Store exception in t_gen_resp (MCP flow only)
                        if mcp_crawler:
                            store_success = store_generated_response(
                                run_id=resolved_run_id,
                                response_data=error_response,
                                jira_id=jira_id,
                                module_name=domain_selection_str if domain_selection else None,
                                status="FAILED",
                                scenario_name=scenario_name,
                                video_filename=video_filename_for_storage,
                                complete_scripts=complete_scripts_for_storage,
                                feature_steps=feature_steps_json_text
                            )
                            if store_success:
                                logger.info(f"💾 Exception stored in database for run ID: {resolved_run_id}")
                            else:
                                logger.warning(f"⚠️  Failed to store exception in database for run ID: {resolved_run_id}")
                        else:
                            # For non-MCP flow, just update if exists
                            update_response_status(resolved_run_id, "failure", str(proc_error))
                            logger.info(f"💾 Exception status stored in database for run ID: {resolved_run_id}")
                except Exception as db_e:
                    logger.error(f"❌ Error storing exception in database: {str(db_e)}")
                
                # Mark exception as stored to prevent duplicate storage in outer handlers
                if not skip_storage:
                    proc_error._db_stored = True
                    logger.info(f"🔖 Marked exception as stored to prevent duplicate storage")
                
                # Delete tran_status after exception handling (regardless of exception type)
                if mcp_crawler:
                    try:
                        from utils.run_id_manager import delete_tran_status
                        delete_tran_status(resolved_run_id, allow_inner_deletion=True)
                        logger.info(f"🗑️  Deleted tran_status for failed run_id: {resolved_run_id}")
                    except Exception as del_e:
                        logger.warning(f"⚠️  Failed to delete tran_status: {del_e}")
                
                # Include run ID in the exception message
                enhanced_error_msg = f"{str(proc_error)} [Run ID: {resolved_run_id}]"
                proc_error.args = (enhanced_error_msg,) + proc_error.args[1:] if proc_error.args else (enhanced_error_msg,)
                raise proc_error


    except HTTPException as http_ex:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error uploading feature file: {str(e)}")
        
        # Check if this exception was already stored to avoid duplicates
        if getattr(e, '_db_stored', False):
            logger.info(f"ℹ️  Skipping duplicate storage at top level for already-stored error")
            # Include run ID in error message but don't store
            try:
                if 'resolved_run_id' in locals():
                    enhanced_msg = f"{str(e)} [Run ID: {resolved_run_id}]"
                    e.args = (enhanced_msg,) + e.args[1:] if e.args else (enhanced_msg,)
                    e.run_id = resolved_run_id
            except:
                pass
            raise e
        
        # Try to include run ID in top-level errors if available
        try:
            if 'resolved_run_id' in locals():
                enhanced_msg = f"{str(e)} [Run ID: {resolved_run_id}]"
                e.args = (enhanced_msg,) + e.args[1:] if e.args else (enhanced_msg,)
                # Add run_id as attribute to exception for error handlers
                e.run_id = resolved_run_id
                # Try to store in database
                try:
                    update_response_status(resolved_run_id, "failure", str(e))
                    logger.info(f"💾 Top-level failure stored for run ID: {resolved_run_id}")
                except:
                    pass
        except:
            pass
        raise e


@router.post("/upload-feature-file-async")
async def upload_feature_file_async(
        request: Request,
        feature_file: UploadFile = File(...),
        json_files: List[UploadFile] = File([], description="Optional list of JSON files with request bodies"),
        push_to_github: bool = Form(True, description="Would you like to push to GitHub?", title="Push to GitHub"),
        domain_selection: Optional[DomainOptions] = Form(None, description="🔴 REQUIRED if GitHub=True | Select domain"),
     
        generate_step_definitions: bool = Form(True, description="Generate output in Step Definition format?", title="Generate Step Definitions"),
        dry_run_strategy: Literal["Javascript", "MCP"] = Form("Javascript", title="Dry Run Strategy"),
        preview_mode: bool = Form(False, description="When true, call sync endpoint and return preview steps", title="Preview Mode"),
        branch_name: Optional[str] = Form(
            "",
            title="Custom Branch Name",
            description="Optional. Used only when Push to GitHub = True. Do not include 'feature/'",
            example=""
        ),
        notify_email: Optional[str] = Form(
            "",
            title="Notify Email",
            description="Optional. Comma-separated emails allowed. If provided, the run summary email will be sent to these addresses.",
            example=""
        )
):
    """
    Draft endpoint for initiating asynchronous script generation.
    Mirrors parameters of /upload-feature-file; implementation to trigger background processing will follow.
    """
    # Generate run ID and submission time (kept for user response; not yet sent to existing API)
    run_id = _generate_internal_run_id()
    # Build primary (Eastern) and secondary (UTC) human-readable timestamps
    now_utc = datetime.now(timezone.utc)
    submitted_at_display = now_utc.astimezone(ZoneInfo("America/New_York")).strftime("%B %d, %Y, %H:%M:%S %Z")
    submitted_at_utc_display = now_utc.strftime("%B %d, %Y, %H:%M:%S UTC")

    # Buffer incoming files and fields before returning response (request stream will be closed)
    try:
        feature_filename = feature_file.filename or "feature.feature"
        feature_bytes = await feature_file.read()
    except Exception as e:
        logger.error(f"Failed to read feature_file for async trigger: {e}")
        raise HTTPException(status_code=400, detail="Invalid or unreadable feature file")

    buffered_json_files: List[Dict[str, Any]] = []
    try:
        if json_files:
            for jf in json_files:
                if getattr(jf, "filename", None):
                    try:
                        buffered_json_files.append({
                            "filename": jf.filename,
                            "content": await jf.read()
                        })
                    except Exception as ie:
                        logger.warning(f"Skipping unreadable JSON file '{getattr(jf,'filename', '?')}': {ie}")
    except Exception as e:
        logger.warning(f"Error buffering JSON files: {e}")

    # Prepare form fields for existing endpoint (omit runid for now)
    domain_value = domain_selection.value if domain_selection else None
    data_fields: Dict[str, Any] = {
        "push_to_github": "true" if push_to_github else "false",
        "generate_step_definitions": "true" if generate_step_definitions else "false",
        "dry_run_strategy": dry_run_strategy or "Javascript",
    }
    # Include orchestrator-generated run ID so the existing endpoint can correlate processing
    data_fields["run_id"] = run_id
    if domain_value:
        data_fields["domain_selection"] = domain_value
    if isinstance(branch_name, str) and branch_name.strip():
        data_fields["branch_name"] = branch_name
    if isinstance(notify_email, str) and notify_email.strip():
        data_fields["notify_email"] = notify_email

    async def _trigger_existing_upload():
        url = _abs_url(request, "/Web-Automation/upload-feature-file")
        try:
            logger.info(
                "Async→Sync trigger URL debug | url=%s | base_url=%s | XFP=%s | XFH=%s | XFPfx=%s",
                url,
                str(request.base_url).rstrip("/"),
                request.headers.get("X-Forwarded-Proto"),
                request.headers.get("X-Forwarded-Host"),
                request.headers.get("X-Forwarded-Prefix"),
            )
        except Exception:
            pass
        files_payload = []
        try:
            # Attach feature file
            files_payload.append(
                ("feature_file", (feature_filename, feature_bytes, "text/plain"))
            )
            # Attach JSON files
            for j in buffered_json_files:
                if j.get("content"):
                    files_payload.append(
                        ("json_files", (j.get("filename") or "request.json", j["content"], "application/json"))
                    )
            timeout = httpx.Timeout(3600.0)  # allow long-running processing
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, data=data_fields, files=files_payload)
                logger.info(
                    "Async→Sync trigger response | status=%s | requested_url=%s",
                    resp.status_code, url
                )
                if resp.status_code >= 400:
                    logger.error(f"Async trigger to existing upload failed: {resp.status_code} {resp.text[:300]}")
                else:
                    logger.info(f"Async trigger to existing upload succeeded: {resp.status_code}")
        except Exception as e:
            logger.error(f"Error triggering existing upload-feature-file asynchronously: {e}")

    # Fire background task; do not await
    if preview_mode:
        # Synchronous call-through to obtain preview steps
        try:
            url = _abs_url(request, "/Web-Automation/upload-feature-file")
            files_payload = []
            files_payload.append(("feature_file", (feature_filename, feature_bytes, "text/plain")))
            for j in buffered_json_files:
                if j.get("content"):
                    files_payload.append(("json_files", (j.get("filename") or "request.json", j["content"], "application/json")))
            data_fields["preview_mode"] = "true"
            timeout = httpx.Timeout(3600.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, data=data_fields, files=files_payload)
                try:
                    content = resp.json()
                except Exception:
                    content = {"message": resp.text}
                return JSONResponse(status_code=resp.status_code, content=content)
        except Exception as e:
            logger.error(f"Preview call-through failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to obtain preview response")
    else:
        # (Handled below after helper definition)
        pass

    # Send immediate initiation notification (Slack/Email) using existing recipient logic
    async def _send_initiation_notification():
        try:
            # Determine selected domain config if applicable
            domain_selection_str = domain_selection.value if domain_selection else None
            repo_modules = None
            selected_config = {}
            if domain_selection_str:
                try:
                    repo_modules = load_repo_modules()
                    selected_config = repo_modules.get(domain_selection_str, {}) or {}
                except Exception:
                    selected_config = {}

            # Parse notify_email into a clean list
            def _normalize_string_list(v):
                if isinstance(v, list):
                    return [x.strip() for x in v if isinstance(x, str) and x.strip()]
                if isinstance(v, str) and v.strip():
                    return [s.strip() for s in v.split(",") if s.strip()]
                return []
            def _is_email_like(s: str) -> bool:
                try:
                    if "@" not in s:
                        return False
                    local, dom = s.split("@", 1)
                    return bool(local.strip()) and "." in dom
                except Exception:
                    return False
            notify_list = []
            if notify_email and isinstance(notify_email, str) and notify_email.strip():
                parsed = _normalize_string_list(notify_email)
                notify_list = [e for e in parsed if _is_email_like(e)]

            # Build recipient list (domain recipients included whenever domain is selected, regardless of push_to_github)
            slack_list = []
            json_emails = []
            if domain_selection_str and selected_config:
                slack_list = _normalize_string_list(selected_config.get("slack_channel_emails") or [])
                json_emails = _normalize_string_list(selected_config.get("email_recipients") or [])
            recipients = list(dict.fromkeys(slack_list + json_emails + notify_list))

            if not recipients:
                logger.info("Initiation notification skipped: no recipients resolved")
                return  # silently skip if no recipients available

            # Extract jira_id from filename if present
            jira_id = feature_filename.split(".feature")[0] if feature_filename else None

            # Build initiation payload
            initiation_payload = {
                "status": "initiated",
                "message": "Request accepted. Asynchronous script generation has been initiated.",
                "run_id": run_id,
                "jira_id": jira_id,
                "selected_domain": domain_selection_str,
                "push_to_github": bool(push_to_github),
                "submitted_at_eastern": submitted_at_display,
                "submitted_at_utc": submitted_at_utc_display,
            }

            try:
                logger.info(f"Notify: attempting initiation summary | jira={jira_id} run_id={run_id} recipients={len(recipients)}")
                # Use shorter timeout for initiation so it does not block the flow
                await asyncio.to_thread(send_run_summary_email, initiation_payload, recipients, timeout_seconds=5)
                logger.info("Notify: initiation summary attempt completed")
            except Exception as notify_exc:
                logger.error(f"Notify: initiation summary failed: {type(notify_exc).__name__}: {notify_exc}")
        except Exception:
            # Do not block or raise on notification issues
            pass

    # Perform initiation notification now (synchronously, short timeout), then trigger processing
    try:
        logger.info("Notify: attempting initiation summary")
        await _send_initiation_notification()
        logger.info("Notify: initiation summary attempt finished")
    except Exception as e:
        logger.error(f"Notify: initiation summary attempt failed: {type(e).__name__}: {e}")
    try:
        asyncio.create_task(_trigger_existing_upload())
    except Exception as e:
        logger.error(f"Failed to schedule async trigger task: {e}")

    # Return immediate acknowledgement
    return JSONResponse(
        status_code=202,
        content={
            "message": "Request accepted. Asynchronous script generation initiation endpoint (draft).",
            "runid": run_id,
            "submittedAt": submitted_at_display,
            "submittedAtUtc": submitted_at_utc_display
        }
    )


@router.get("/download/{feature_file_name}")
async def download_file(feature_file_name: str):
    """
    Download generated test files by JIRA ID.

    This endpoint allows you to download the generated BDD test scenarios and
    Playwright scripts that were created for a specific JIRA ticket.

    Args:
        jira_id (str): The JIRA ticket ID used when generating the files

    Returns:
        FileResponse: ZIP file containing generated test scenarios and scripts

    Example:
        GET /download/JIRA-123

    Note:
        - Files are temporary and expire after a certain time
        - Files are marked as downloaded after successful retrieval
        - Only one download per JIRA ID is typically allowed
    """
    try:

        file_info = temp_file_manager.get_file_info(feature_file_name)

        if not file_info:
            raise HTTPException(status_code=404, detail="File not found or expired")

        file_path = file_info['file_path']
        original_filename = file_info['original_filename']

        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="File no longer exists")

        # Mark as downloaded
        temp_file_manager.mark_downloaded(feature_file_name)

        logger.info(f"File downloaded: {feature_file_name} -> {original_filename}")

        return FileResponse(
            path=file_path,
            filename=original_filename,
            media_type='application/zip'
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"File is not present. Error downloading file {feature_file_name}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")




@router.get("/recordings/download/{filename}")
def download_recording_by_filename(filename: str = FPath(..., description="Recording zip filename"),):
        """
        Stream the recording ZIP directly from S3 by filename.
        No local file dependency.
        """
        bucket_name, prefix = get_recordings_bucket_and_prefix()
        if not bucket_name:
            raise HTTPException(status_code=500, detail="S3 bucket not configured")
        key = f"{prefix}/{filename}" if prefix else filename

        try:
            s3 = _get_s3_client()
            obj = s3.get_object(Bucket=bucket_name, Key=key)
        except Exception:
            raise HTTPException(status_code=404, detail="Recording not found in S3")

        body = obj["Body"]  # botocore.response.StreamingBody
        media_type = obj.get("ContentType", "application/zip")
        headers = {"Content-Disposition": f"attachment; filename=\"{filename}\""}
        content_length = obj.get("ContentLength")
        if content_length:
            headers["Content-Length"] = str(content_length)

        def _iter():
            while True:
                chunk = body.read(8192)
                if not chunk:
                    break
                yield chunk

        return StreamingResponse(_iter(), media_type=media_type, headers=headers)


@router.get("/analytics")
async def get_analytics_data(
    start_date: Optional[str] = FQuery(None, description="Start date in YYYY-MM-DD format"),
    end_date: Optional[str] = FQuery(None, description="End date in YYYY-MM-DD format")
):
    try:
        from utils.db_utils import engine, PGSCHEMA
        from sqlalchemy import text
        from datetime import datetime

        where_conditions = []
        params = {}

        if start_date:
            try:
                # Validate date format
                datetime.strptime(start_date, '%Y-%m-%d')
                where_conditions.append("execution_dt >= :start_date")
                params["start_date"] = start_date
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid start_date format. Use YYYY-MM-DD")

        if end_date:
            try:
                # Validate date format
                datetime.strptime(end_date, '%Y-%m-%d')
                where_conditions.append("execution_dt <= :end_date")
                params["end_date"] = end_date
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid end_date format. Use YYYY-MM-DD")

        where_clause = ""
        if where_conditions:
            where_clause = "WHERE " + " AND ".join(where_conditions)

        query = text(f"""
            SELECT
                id, module, scn_count, feature_nm, time_taken,
                execution_dt, scrpt_gen_success, reexecution_success,
                crl_type, push_to_github, feature_tag, feature_description,
                scn_tag, scn_description
            FROM {PGSCHEMA}.t_gen_usage
            {where_clause}
            ORDER BY id DESC
        """)

        with engine.begin() as conn:
            results = conn.execute(query, params).fetchall()
            
            records = []
            for row in results:
                records.append({
                    "id": row.id,
                    "module": row.module,
                    "scn_count": row.scn_count,
                    "feature_nm": row.feature_nm,
                    "time_taken": row.time_taken,
                    "execution_dt": str(row.execution_dt) if row.execution_dt else None,
                    "scrpt_gen_success": row.scrpt_gen_success,
                    "reexecution_success": row.reexecution_success,
                    "crl_type": row.crl_type,
                    "push_to_github": row.push_to_github,
                    "feature_tag": row.feature_tag,
                    "feature_description": row.feature_description,
                    "scn_tag": row.scn_tag,
                    "scn_description": row.scn_description
                })
        
        filter_info = ""
        if start_date and end_date:
            filter_info = f" (filtered: {start_date} to {end_date})"
        elif start_date:
            filter_info = f" (filtered: from {start_date})"
        elif end_date:
            filter_info = f" (filtered: up to {end_date})"

        logger.info(f"📊 Analytics: Retrieved {len(records)} records{filter_info}")
        
        return {
            "success": True,
            "total_records": len(records),
            "records": records,
            "filters": {
                "start_date": start_date,
                "end_date": end_date
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching analytics data: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch analytics data: {str(e)}")




# ============================================================================
# EXECUTION STATUS ENDPOINT
# ============================================================================

@router.get(
    "/execution-status",
    response_model=ExecutionStatusResponse,
    tags=["Web-Automation"],
    summary="Get Execution Status by Date",
    description="Retrieve execution status for a specific date, with optional runid/module filters, showing in-progress and completed test executions"
)
async def get_execution_status_by_date(
    date: Optional[str] = Query(
        None,
        description="Optional date in MMDDYYYY format"
    ),
    module: Optional[ModuleEnum] = Query(
        None,
        description="Optional module filter (case-insensitive exact match)"
    ),
    runid: Optional[str] = Query(
        None,
        description="Optional run ID filter (exact match)"
    )
):
    """
    Get execution status for a specific date.

    This endpoint retrieves test execution data from the database and categorizes them into:
    - **In-Progress**: Currently running or pending executions
    - **Completed**: Finished executions with success/failure status

    Args:
        date: Optional date in MMDDYYYY format (e.g., "12252024" for December 25, 2024)

    Returns:
        ExecutionStatusResponse: Contains arrays of in-progress and completed executions

    Example Response:
    ```json
    {
      "inprogressExecution": [
        {
          "runid": "RUN-1001",
          "statusDescription": "Test execution is currently running",
          "scenarioName": "User Login - Valid Credentials",
          "status": "IN_PROGRESS"
        }
      ],
      "completedExecution": [
        {
          "runid": "RUN-0901",
          "statusDescription": "Completed with steps: 5",
          "scenarioName": "User Registration",
          "status": "PASSED",
          "videofile": {
            "name": "page-2026-02-02T15-03-56-321Z.webm",
            "status": "Active"
          }
        }
      ]
    }
    ```
    """
    try:
        # Validate date format if provided
        formatted_date = None
        if date is not None:
            if not re.match(r'^\d{8}$', date):
                raise HTTPException(status_code=400, detail="Date must be in MMDDYYYY format (8 digits)")
            # Parse date to validate it's a real date
            try:
                parsed_date = datetime.strptime(date, "%m%d%Y")
                formatted_date = parsed_date.strftime("%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid date provided")

        # Query database for execution data
        engine = _get_engine(BASE_DATABASE_URL)

        with engine.begin() as conn:
            # Normalize optional filters
            module_filter = None
            if module is not None:
                try:
                    # Enum member -> its string value
                    module_filter = str(module.value).strip()
                except Exception:
                    # Fallback in unlikely case of non-enum input
                    module_filter = str(module).strip() if str(module).strip() != "" else None
            runid_filter = runid.strip() if isinstance(runid, str) and runid.strip() != "" else None
            # Query for in-progress executions from tran_status table
            inprogress_where_clauses = []
            inprogress_params = {}
            if formatted_date:
                inprogress_where_clauses.append("DATE(ts.execution_dt) = :date_param")
                inprogress_params["date_param"] = formatted_date
            if module_filter:
                inprogress_where_clauses.append("LOWER(ts.modulename) = LOWER(:module_param)")
                inprogress_params["module_param"] = module_filter
            if runid_filter:
                inprogress_where_clauses.append("CAST(ts.runid AS TEXT) = :runid_param")
                inprogress_params["runid_param"] = runid_filter

            inprogress_where_sql = " AND ".join(inprogress_where_clauses) or "1=1"
            inprogress_query = text(f"""
                SELECT
                    CAST(ts.runid AS VARCHAR) as runid,
                    CAST(ts.succ_steps AS TEXT) as statusDescription,
                    ts.scenarioname as scenarioName,
                    ts.modulename as moduleName,
                    ts.status as status,
                    ts.execution_dt,
                    NULL as complt_scripts,
                    NULL as feature_steps
                FROM {PGSCHEMA}.tran_status ts
                WHERE {inprogress_where_sql}
                ORDER BY ts.execution_dt DESC
            """)

            # Query for completed executions from t_gen_resp table
            completed_where_clauses = []
            completed_params = {}
            if formatted_date:
                completed_where_clauses.append("DATE(tgr.execution_dt) = :date_param")
                completed_params["date_param"] = formatted_date
            if module_filter:
                completed_where_clauses.append("LOWER(tgr.modulenm) = LOWER(:module_param)")
                completed_params["module_param"] = module_filter
            if runid_filter:
                completed_where_clauses.append("CAST(tgr.runid AS TEXT) = :runid_param")
                completed_params["runid_param"] = runid_filter

            completed_where_sql = " AND ".join(completed_where_clauses) or "1=1"
            completed_query = text(f"""
                SELECT
                    CAST(tgr.runid AS VARCHAR) as runid,
                    CAST(tgr.resp AS TEXT) as statusDescription,
                    tgr.scenarioname as scenarioName,
                    tgr.modulenm as moduleName,
                    tgr.status as status,
                    tgr.execution_dt,
                    tgr.video_filenm as video_filename,
                    tgr.complt_scripts as complt_scripts,
                    tgr.feature_steps as feature_steps
                FROM {PGSCHEMA}.t_gen_resp tgr
                WHERE {completed_where_sql}
                ORDER BY tgr.execution_dt DESC
            """)

            # Execute queries
            inprogress_result = conn.execute(inprogress_query, inprogress_params)
            completed_result = conn.execute(completed_query, completed_params)

            # Process results
            inprogress_executions = [
                ExecutionItem(
                    runid=row[0],  # runid
                    statusDescription=row[1],  # statusDescription (JSON as text)
                    scenarioName=row[2],  # scenarioName
                    moduleName=row[3],  # moduleName
                    status=row[4],  # status
                    complt_scripts=row[6],  # complt_scripts (NULL for in-progress)
                    feature_steps=row[7]  # feature_steps (NULL for in-progress)
                ) for row in inprogress_result
            ]

            # Helper function to calculate video file status
            def get_video_status(execution_date, video_filename):
                """Return video status using filename timestamp when available; fallback to 7-day date logic."""
                if not video_filename or (isinstance(video_filename, str) and not video_filename.strip()):
                    logger.debug(f"🎥 Video filename is NULL or empty for execution_date: {execution_date}")
                    return None
                
                try:
                    # Try precise check using timestamp embedded in filename, e.g. page-2026-02-02T15-03-56-321Z.webm
                    from datetime import datetime, timezone, timedelta
                    import re

                    match = re.search(r'(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})(?:-(\d{3}))?Z', str(video_filename))
                    if match:
                        date_part = match.group(1)
                        hour = int(match.group(2))
                        minute = int(match.group(3))
                        second = int(match.group(4))
                        millis = match.group(5)
                        microsecond = (int(millis) * 1000) if millis is not None else 0

                        ts_dt = datetime.strptime(date_part, "%Y-%m-%d").replace(
                            hour=hour, minute=minute, second=second, microsecond=microsecond, tzinfo=timezone.utc
                        )

                        now_utc = datetime.now(timezone.utc)
                        age_seconds = (now_utc - ts_dt).total_seconds()
                        if age_seconds < 0:
                            age_seconds = 0

                        # Standard retention threshold: 7 days (in seconds)
                        threshold_seconds = 604800
                        status = "Active" if age_seconds <= threshold_seconds else "Test Log Expired"
                        logger.debug(f"🎥 Video status (filename ts): {video_filename} -> {status} (age_seconds: {int(age_seconds)})")
                        return {
                            "name": video_filename,
                            "status": status
                        }

                    # If no timestamp in filename, fall back to day-based logic
                    raise ValueError("No parsable timestamp in filename")

                except Exception:
                    from datetime import datetime, timedelta
                    # Convert execution_date to date if it's a datetime object
                    if isinstance(execution_date, datetime):
                        execution_date = execution_date.date()
                    current_date = datetime.now().date()
                    days_diff = (current_date - execution_date).days
                    status = "Active" if days_diff <= 7 else "Test Log Expired"
                    logger.debug(f"🎥 Video status (fallback days): {video_filename} -> {status} (days_diff: {days_diff})")
                    return {
                        "name": video_filename,
                        "status": status
                    }

            completed_executions = []
            for row in completed_result:
                execution_date = row[5]  # execution_dt
                video_filename = row[6]  # video_filenm
                complt_scripts = row[7]  # complt_scripts
                feature_steps = row[8]  # feature_steps
                status_desc = row[1]  # statusDescription (JSON as text)
                
                # Extract error_message from resp JSON
                error_message = None
                try:
                    if status_desc and isinstance(status_desc, str):
                        resp_json = json.loads(status_desc)
                        
                        # For failed scenarios with error details
                        if isinstance(resp_json, dict) and "error" in resp_json:
                            error_obj = resp_json.get("error") or {}
                            # Get technical_insights as error_message (from detailed_analysis section)
                            detailed_analysis = error_obj.get("detailed_analysis") or {}
                            error_message = detailed_analysis.get("technical_insights")
                            
                            # If technical_insights not found, try complete_error_with_internal_details
                            if not error_message:
                                technical_details = error_obj.get("technical_details") or {}
                                error_message = technical_details.get("complete_error_with_internal_details")
                            
                            # Note: error_stage is extracted by backend but not used by frontend
                            # Frontend only displays error_message
                        else:
                            # Fallback for old format: look for error_message directly
                            error_message = resp_json.get("error_message")
                except (json.JSONDecodeError, AttributeError, TypeError):
                    # If resp is not valid JSON, error fields remain None
                    pass
                
                # Ensure error_message is a string or None (Pydantic validation)
                if error_message is not None and not isinstance(error_message, str):
                    error_message = str(error_message) if error_message else None
                
                # Handle multiple videos (comma-separated filenames)
                videofile_list = []
                if video_filename and isinstance(video_filename, str) and video_filename.strip():
                    filenames = [f.strip() for f in video_filename.split(",") if f.strip()]
                    for filename in filenames:
                        video_status = get_video_status(execution_date, filename)
                        if video_status:
                            videofile_list.append(video_status)
                
                item = ExecutionItem(
                    runid=row[0],  # runid
                    statusDescription=row[1],  # statusDescription (JSON as text)
                    scenarioName=row[2],  # scenarioName
                    moduleName=row[3],  # moduleName
                    status=row[4],  # status
                    videofile=videofile_list if videofile_list else None,  # All videos in array format
                    complt_scripts=complt_scripts,  # Complete scripts for Download link visibility
                    feature_steps=feature_steps,  # Feature steps for Download link visibility
                    error_message=error_message  # Error message from resp
                )
                completed_executions.append(item)

        logger.info(f"Retrieved execution status for date {date}: {len(inprogress_executions)} in-progress, {len(completed_executions)} completed")

        return ExecutionStatusResponse(
            inprogressExecution=inprogress_executions,
            completedExecution=completed_executions
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving execution status for date {date}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error while retrieving execution status: {str(e)}"
        )

@router.post(
    "/validatefeaturefile",
    response_model=FeatureValidationResponse,
    tags=["Web-Automation"],
    summary="Validate Feature File Gherkin Structure",
    description="Upload a feature file and validate its Gherkin format structure using LLM validation"
)
async def validate_feature_file(
    feature_file: UploadFile = File(..., description="Feature file to validate (.feature extension)")
) -> FeatureValidationResponse:
    try:
        logger.info("📝 Feature file validation request received")
        filename = feature_file.filename
        if not filename.lower().endswith('.feature'):
            raise ValueError("Feature file extension is not correct. Please upload a .feature file.")
        feature_content_bytes = await feature_file.read()
        feature_content = feature_content_bytes.decode("utf-8")
        if not feature_content.strip():
            raise ValueError("Feature file is empty or contains no content")
        
        validation_result = validate_gherkin(feature_content)
        is_valid = validation_result.get("is_valid", False)
        message = validation_result.get("message", "Validation completed")
        issues = validation_result.get("issues", [])
        return FeatureValidationResponse(
            responseCode=200 if is_valid else 500,
            is_valid=is_valid,
            message=message,
            issues=issues
        )
    except Exception as e:
        logger.error(f"error occured during feature file validation: {str(e)}")
        return FeatureValidationResponse(
            responseCode=500,
            is_valid=False,
            message=f"Internal server error during validation: {str(e)}",
            issues=[{"error": "Internal processing error"}]
        )

@router.get(
    "/stepbyModule",
    response_model=successfulStepByModuleResponse,
    tags=["Web-Automation"],
    summary="Get Steps by Module",
    description="Retrieve all already executed successful steps for a specific module"
)
async def get_steps_by_module(
    module: str = Query(...,description="Module name to fetch steps for (case-insensitive exact match)", example="Example-Module")
)-> successfulStepByModuleResponse:
    try:
        if not module or module.strip() == "":
            raise HTTPException(
                status_code=400,
                detail="Module name is required"
            )
        steps_result = await get_steps_by_modulenm(module.strip())
        if not steps_result["success"]:
            return successfulStepByModuleResponse(
                responseCode=404,
                message=steps_result["message"],
                commaSeparatedValues="None"
            )
        steps_list = steps_result["data"]
        pipe_separated_steps = "|".join(steps_list) if steps_list else ""
        return successfulStepByModuleResponse(
            responseCode=200,
            message=f"Successfully retrieved {len(steps_list)} steps for module '{module}'",
            commaSeparatedValues=pipe_separated_steps
        )    
    except Exception as e:
        logger.error(f"❌ Error getting steps for module {module}: {str(e)}")
        return successfulStepByModuleResponse(
            responseCode=500,
            message=f"Internal server error: {str(e)}",
            commaSeparatedValues="None"
        )      



@router.get(
    "/execution-details",
    response_model=RunExecutionDetailsResponse,
    tags=["Web-Automation"],
    summary="Get Execution Details by Run ID",
    description="Retrieve detailed execution information for a specific run ID, including step-by-step progress"
)
async def get_execution_details_by_runid(
    runid: str = Query(
        ...,
        description="Run ID to fetch execution details for"
    )
):
    """
    Get detailed execution information for a specific run ID.

    This endpoint first checks the tran_status table for ongoing executions.
    If found, it returns step-by-step progress with the last step as 'In-progress'.
    If not found in tran_status, it checks t_gen_resp for completed executions.

    Args:
        runid: The run ID to fetch details for

    Returns:
        RunExecutionDetailsResponse: Contains run details with step descriptions and status

    Example Response:
    ```json
    {
      "runid": "RUN-1001",
      "description": [
        "Then the user clicks on \"Continue\" – Completed",
        "Then user enters \"Password\" as \"chewyplaywright123#\" – Completed",
        "Then the user clicks on \"Sign In\" button – In-progress"
      ],
      "scenarioName": "User Login - Valid Credentials",
      "status": "IN_PROGRESS"
    }
    ```
    """
    try:
        logger.info(f"🔍 [EXECUTION-DETAILS] Request received for runid: {runid}")
        engine = _get_engine(BASE_DATABASE_URL)

        with engine.begin() as conn:
            # Validate runid format and extract numeric part if needed
            # Handle two formats: "internal_<hex_string>" and pure numeric
            numeric_runid = None

            if runid.isdigit():
                # Format: 260113164846 (pure numeric)
                numeric_runid = runid
                logger.info(f"🔍 [EXECUTION-DETAILS] Runid format: numeric ({numeric_runid})")
            elif runid.startswith("internal_") and len(runid) > 9:
                # Format: internal_cba576fe5aad419f83c01186b5dad877
                # For internal format, we need to check if there's a numeric mapping
                # or use the hex string. Let's first try to find it in t_gen_resp
                numeric_runid = runid  # Keep original for t_gen_resp query
                logger.info(f"🔍 [EXECUTION-DETAILS] Runid format: internal ({runid})")
            else:
                # Invalid format
                logger.warning(f"🔍 [EXECUTION-DETAILS] Invalid runid format: {runid}")
                raise HTTPException(
                    status_code=400,
                    detail="runid is invalid"
                )

            # First check tran_status table for ongoing execution
            # Try to query tran_status with the runid (both numeric and internal_ formats)
            logger.info(f"🔍 [EXECUTION-DETAILS] Checking tran_status table...")
            tran_status_query = text(f"""
                SELECT 
                    runid,
                    succ_steps,
                    scenarioname,
                    status
                FROM {PGSCHEMA}.tran_status 
                WHERE CAST(runid AS VARCHAR) = :runid
            """)

            tran_result = conn.execute(tran_status_query, {"runid": runid})
            tran_row = tran_result.fetchone()
            
            logger.info(f"🔍 [EXECUTION-DETAILS] tran_status query result: {'Found' if tran_row else 'Not found'}")

            if tran_row:
                # Found in tran_status - execution is in progress
                scenario_name = tran_row.scenarioname

                # Parse and format steps for modal display using helper function
                description_list = []
                
                logger.info(f"📊 IN_PROGRESS: Extracting steps for runid={runid}")
                
                # Use get_tran_status_steps to extract and parse steps (handles all formats)
                from utils.run_id_manager import get_tran_status_steps
                steps_data = get_tran_status_steps(runid)
                logger.info(f"📊 Extracted {len(steps_data)} completed steps")
                
                # Extract current executing step from succ_steps JSON
                import json
                succ_steps_data = tran_row.succ_steps
                current_step = None
                try:
                    if isinstance(succ_steps_data, str):
                        succ_steps_obj = json.loads(succ_steps_data)
                    else:
                        succ_steps_obj = succ_steps_data
                    current_step = succ_steps_obj.get("current_step")
                    logger.info(f"📊 Current executing step: {current_step}")
                except Exception as e:
                    logger.warning(f"⚠️  Failed to extract current_step: {e}")
                
                # Format completed steps with checkmarks
                for idx, step in enumerate(steps_data, 1):
                    description_list.append(f"{idx}. {step} - Step execution Completed")
                
                # Add current executing step as IN-Progress (if exists)
                if current_step:
                    description_list.append(f"{len(steps_data) + 1}. {current_step} - Step execution IN-Progress")
                    logger.info(f"📊 Added current executing step as IN-Progress")
                elif len(steps_data) == 0:
                    # No completed steps and no current step - just started
                    description_list.append("🔄 Execution starting... No steps completed yet")

                logger.info(f"📊 Returning {len(description_list)} description items for IN_PROGRESS")
                
                return RunExecutionDetailsResponse(
                    runid=runid,
                    description=description_list,
                    scenarioName=scenario_name,
                    status="IN_PROGRESS"
                )

            else:
                # Not found in tran_status - check t_gen_resp for completed execution
                logger.info(f"🔍 [EXECUTION-DETAILS] Checking t_gen_resp table...")
                if runid.isdigit():
                    # Numeric format - fetch ALL scenarios for this run_id (multi-scenario support)
                    gen_resp_query = text(f"""
                        SELECT 
                            runid,
                            resp,
                            status,
                            scenarioname,
                            modulenm
                        FROM {PGSCHEMA}.t_gen_resp 
                        WHERE runid = :runid
                        ORDER BY id ASC
                    """)
                    gen_result = conn.execute(gen_resp_query, {"runid": numeric_runid})
                else:
                    # Internal format - need to search by string comparison
                    gen_resp_query = text(f"""
                        SELECT 
                            runid,
                            resp,
                            status,
                            scenarioname,
                            modulenm
                        FROM {PGSCHEMA}.t_gen_resp 
                        WHERE CAST(runid AS TEXT) = :runid
                        ORDER BY id ASC
                    """)
                    gen_result = conn.execute(gen_resp_query, {"runid": runid})
                
                # Fetch ALL scenarios (not just one)
                all_rows = gen_result.fetchall()
                gen_row = all_rows[0] if all_rows else None
                
                logger.info(f"🔍 [EXECUTION-DETAILS] Found {len(all_rows)} scenario(s) for runid: {runid}")
                
                logger.info(f"🔍 [EXECUTION-DETAILS] t_gen_resp query result: {'Found' if gen_row else 'Not found'}")

                if gen_row:
                    # Found in t_gen_resp - execution is completed
                    logger.info(f"🔍 [EXECUTION-DETAILS] Found in t_gen_resp - COMPLETED")
                    
                    # === MULTI-SCENARIO SUPPORT ===
                    # If multiple scenarios exist, return ALL of them
                    if len(all_rows) > 1:
                        logger.info(f"🔍 [EXECUTION-DETAILS] Multi-scenario run detected: {len(all_rows)} scenarios")
                        all_scenarios_data = []
                        overall_status = "PASSED"  # Will change to FAILED if any scenario failed
                        
                        for row in all_rows:
                            row_resp = row.resp
                            row_status = row.status
                            row_scenario_name = row.scenarioname
                            row_module = row.modulenm
                            
                            logger.info(f"🔍 [EXECUTION-DETAILS] Processing scenario: {row_scenario_name}, Status: {row_status}, Module: {row_module}")
                            
                            # Update overall status if any scenario failed
                            if row_status == "FAILED":
                                overall_status = "FAILED"
                            
                            # Extract steps for this scenario
                            scenario_steps = []
                            if row_resp and isinstance(row_resp, dict):
                                successful_steps = row_resp.get('successful_steps', [])
                                if isinstance(successful_steps, list):
                                    for idx, step in enumerate(successful_steps, 1):
                                        scenario_steps.append(f"{idx}. {step} - Step execution Completed")
                                
                                # Add failure info if status is FAILED
                                if row_status == "FAILED":
                                    error_message = row_resp.get('error_message', 'Execution failed')
                                    failed_step_number = len(scenario_steps) + 1
                                    scenario_steps.append(f"{failed_step_number}. Execution failed - {str(error_message)[:100]} - Step execution failed")
                            elif row_status == "FAILED":
                                # No valid row_resp but status is FAILED - add generic failure step
                                scenario_steps.append("1. Execution failed - No detailed error available - Step execution failed")
                            
                            all_scenarios_data.append({
                                "scenario_name": row_scenario_name,
                                "module_name": row_module,
                                "status": row_status,
                                "steps": scenario_steps
                            })
                        
                        # Return multi-scenario response
                        logger.info(f"✅ [EXECUTION-DETAILS] Returning {len(all_scenarios_data)} scenarios, overall status: {overall_status}")
                        
                        return RunExecutionDetailsResponse(
                            runid=runid,
                            description=all_scenarios_data,  # Array of scenario data
                            scenarioName=None,  # Multiple scenarios - name is in each scenario object
                            status=overall_status
                        )
                    
                    # === SINGLE SCENARIO HANDLING (existing logic) ===
                    resp_content = gen_row.resp
                    execution_status = gen_row.status
                    scenario_name = gen_row.scenarioname
                    
                    logger.info(f"🔍 [EXECUTION-DETAILS] Status: {execution_status}, Scenario: {scenario_name}")

                    # Parse and format steps for modal display
                    description_list = []
                    
                    if resp_content and isinstance(resp_content, dict):
                        # Priority 1: Extract from scenarios array (most detailed)
                        scenarios = resp_content.get('scenarios', [])
                        logger.info(f"🔍 [EXECUTION-DETAILS] Found {len(scenarios)} scenarios in resp_content")
                        
                        # Debug: Show what keys are in resp_content
                        logger.info(f"🔍 [EXECUTION-DETAILS] resp_content keys: {list(resp_content.keys())}")
                        
                        if scenarios and isinstance(scenarios, list):
                            logger.info(f"🔍 [EXECUTION-DETAILS] Scenarios array type: {type(scenarios)}, length: {len(scenarios)}")
                            for scenario in scenarios:
                                if isinstance(scenario, dict):
                                    scenario_status = scenario.get('status', '').lower()
                                    logger.info(f"🔍 [EXECUTION-DETAILS] Scenario status: {scenario_status}")
                                    
                                    if scenario_status == 'passed':
                                        # Get successful_steps from passed scenario
                                        successful_steps = scenario.get('successful_steps', [])
                                        logger.info(f"🔍 [EXECUTION-DETAILS] PASSED - {len(successful_steps)} steps")
                                        if successful_steps:
                                            for idx, step in enumerate(successful_steps, 1):
                                                description_list.append(f"{idx}. {step} - Step execution Completed")
                                    
                                    elif scenario_status == 'failed':
                                        # For failed scenario, show successful steps + error info
                                        successful_steps = scenario.get('successful_steps', [])
                                        error_message = scenario.get('error_message', 'Execution failed')
                                        logger.info(f"🔍 [EXECUTION-DETAILS] FAILED - {len(successful_steps)} steps before failure")
                                        logger.info(f"🔍 [EXECUTION-DETAILS] FAILED scenario data: {scenario}")  # Debug full scenario
                                        
                                        # Show successful steps that completed before failure
                                        if successful_steps:
                                            for idx, step in enumerate(successful_steps, 1):
                                                description_list.append(f"{idx}. {step} - Step execution Completed")
                                        
                                        # Add failure information as the last step
                                        failed_step_number = len(successful_steps) + 1
                                        description_list.append(f"{failed_step_number}. Execution failed - {str(error_message)[:100]} - Step execution failed")
                        
                        # Priority 2: Extract from top-level successful_steps (fallback for PASSED)
                        if not description_list and 'successful_steps' in resp_content:
                            successful_steps = resp_content['successful_steps']
                            if isinstance(successful_steps, list):
                                for idx, step in enumerate(successful_steps, 1):
                                    # Mark all as completed if status is PASSED
                                    if execution_status == "PASSED":
                                        description_list.append(f"{idx}. {step} - Step execution Completed")
                                    else:
                                        # For failed, mark all completed steps
                                        description_list.append(f"{idx}. {step} - Step execution Completed")
                            elif isinstance(successful_steps, str):
                                # Handle comma or pipe-separated string
                                steps = [s.strip() for s in successful_steps.replace('|', ',').split(',') if s.strip()]
                                for idx, step in enumerate(steps, 1):
                                    description_list.append(f"{idx}. {step} - Step execution Completed")
                        
                        # Priority 3: Fallback to other structures
                        if not description_list:
                            if 'steps' in resp_content:
                                steps = resp_content['steps']
                                if isinstance(steps, list):
                                    for idx, step in enumerate(steps, 1):
                                        description_list.append(f"{idx}. {step} - Step execution Completed")
                            elif 'description' in resp_content:
                                desc = resp_content['description']
                                if isinstance(desc, list):
                                    for idx, step in enumerate(desc, 1):
                                        description_list.append(f"{idx}. {step} - Step execution Completed")
                        
                        # If status is FAILED and we don't have a failure step already, add generic failure message
                        if execution_status == "FAILED" and description_list:
                            # Check if last step already has "failed" in it
                            if "Step execution failed" not in description_list[-1]:
                                error_info = resp_content.get('error_message', resp_content.get('message', 'Execution failed'))
                                description_list.append(f"{len(description_list) + 1}. Test execution failed - {str(error_info)[:100]} - Step execution failed")

                    # Return empty list if no description data found
                    if not description_list:
                        description_list = ["No step details available"]

                    # Map status to standard format
                    status = "PASSED" if execution_status == "PASSED" else "FAILED"
                    
                    logger.info(f"✅ [EXECUTION-DETAILS] Returning {len(description_list)} steps, status: {status}")

                    return RunExecutionDetailsResponse(
                        runid=runid,
                        description=description_list,
                        scenarioName=scenario_name,
                        status=status
                    )

                else:
                    # Run ID not found in either table
                    logger.warning(f"🔍 [EXECUTION-DETAILS] Runid {runid} not found in tran_status or t_gen_resp")
                    raise HTTPException(
                        status_code=404,
                        detail="runId is invalid"
                    )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [EXECUTION-DETAILS] Error retrieving execution details for runid {runid}: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error while retrieving execution details: {str(e)}"
        )


@router.post(
    "/generateWorkflow",
    tags=["Web-Automation"],
    summary="Generate Workflow Steps from Unformatted Request",
    description="Convert an unformatted test request into detailed step-by-step workflow instructions using AI",
    response_model=WorkflowGenerationResponse,
    status_code=status.HTTP_200_OK
)
async def generate_workflow(request: WorkflowGenerationRequest) -> WorkflowGenerationResponse:
    try:
        logger.info(f"Generating workflow for module: {request.module}")
        logger.info("generateWorkflow request body:\n%s", json.dumps(request.model_dump(), indent=2, default=str))

        if request.unformattedrequest is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unformatted request cannot be null"
            )
        
        if not isinstance(request.unformattedrequest, str) or not request.unformattedrequest.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unformatted request cannot be empty"
            )
        
        if request.module is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Module name cannot be null"
            )
        
        if not isinstance(request.module, str) or not request.module.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Module name cannot be empty"
            )
        
        # Initialize the instruction expander
        expander = InstructionExpander()
        
        # Generate workflow steps
        result = expander.expand_summary(
            module=request.module,
            summary=request.unformattedrequest
        )

        if result is None:
            raise ValueError("InstructionExpander returned None")
        
        if not isinstance(result, dict):
            logger.error(f"InstructionExpander returned invalid type: {type(result)}")
            raise ValueError(f"InstructionExpander returned invalid type: {type(result)}, expected dict")
        
        # Check for error status
        if result.get("status") == "ERROR":
            logger.error(f"Error from InstructionExpander: {result.get('error')}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to generate workflow: {result.get('error', 'Unknown error')}"
            )
        
        # ✅ Validate required fields exist in response
        if "formattedWorkflow" not in result:
            raise ValueError("Response missing 'formattedWorkflow' field")
        
        logger.info(f"Successfully generated {len(result.get('formattedWorkflow', []))} workflow steps")
        logger.info("generateWorkflow response:\n%s", json.dumps(result, indent=2, default=str))
        return result
        
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.error(f"Unexpected error generating workflow: {str(e)}", exc_info=True)
        # Return error response instead of raising exception to match expected format
        error_response = WorkflowGenerationResponse(
            responseId=f"RESP-{datetime.now().strftime('%Y-%m-%d')}-ERROR",
            status="ERROR",
            timestamp=datetime.now().isoformat() + "Z",
            formattedWorkflow=[],
            error=f"Internal error: {str(e)}"
        )
        logger.info("generateWorkflow response (error):\n%s", json.dumps(error_response.model_dump(), indent=2, default=str))
        return error_response



@router.get(
    "/execution-details/moredetails",
    tags=["Web-Automation"],
    summary="Download Feature File or Complete Script by Run ID",
    description="Download either the feature file or complete TypeScript script for a specific run ID"
)
async def get_execution_more_details(
        runid: str = Query(
            ...,
            description="Run ID to fetch details for"
        ),
        detailstype: Literal["featurefile", "totalscript"] = Query(
            ...,
            description="Type of details to download: 'featurefile' or 'totalscript'"
        )
):
    """
    Download feature file or complete script for a specific run ID.

    This endpoint retrieves data from t_gen_resp table and creates a downloadable file.

    Args:
        runid: The run ID to fetch details for
        detailstype: Either 'featurefile' (downloads .feature file) or 'totalscript' (downloads .ts file)

    Returns:
        FileResponse: Downloaded file with appropriate extension

    Example:
        GET /execution-details/moredetails?runid=RUN-1001&detailstype=featurefile
        GET /execution-details/moredetails?runid=internal_abc123&detailstype=totalscript
    """
    temp_file_path = None
    try:
        logger.info(f"🔍 [MORE-DETAILS] Request received for runid: {runid}, detailstype: {detailstype}")

        # Validate detailstype parameter (Pydantic Literal already enforces this, but adding explicit check for clarity)
        if detailstype not in ["featurefile", "totalscript"]:
            raise HTTPException(
                status_code=400,
                detail="Invalid detailstype. Must be either 'featurefile' or 'totalscript'"
            )

        engine = _get_engine(BASE_DATABASE_URL)

        with engine.begin() as conn:
            # Determine which column to fetch based on detailstype
            if detailstype == "featurefile":
                column_name = "feature_steps"
                file_extension = ".feature"
                # For feature file, get the first row (ORDER BY id ASC)
                order_direction = "ASC"
            else:  # totalscript
                column_name = "complt_scripts"
                file_extension = ".ts"
                # For total script, get the last row (ORDER BY id DESC) to get the complete accumulated script
                order_direction = "DESC"

            # Query t_gen_resp table for the specified runid
            query = text(f"""
                SELECT 
                    {column_name},
                    scenarioname,
                    modulenm
                FROM {PGSCHEMA}.t_gen_resp 
                WHERE CAST(runid AS VARCHAR) = :runid
                ORDER BY id {order_direction}
                LIMIT 1
            """)

            result = conn.execute(query, {"runid": runid})
            row = result.fetchone()

            if not row:
                logger.warning(f"🔍 [MORE-DETAILS] Runid {runid} not found in t_gen_resp")
                raise HTTPException(
                    status_code=404,
                    detail=f"No data found for runid: {runid}"
                )

            file_content = row[0]
            scenario_name = row[1] or "scenario"
            module_name = row[2] or "module"

            # Validate that content exists
            if not file_content:
                logger.warning(f"🔍 [MORE-DETAILS] No {column_name} content found for runid {runid}")
                raise HTTPException(
                    status_code=404,
                    detail=f"No {detailstype} content available for runid: {runid}"
                )

            # Create target/temp directory if it doesn't exist
            base = os.path.dirname(os.path.abspath(__file__))
            root = os.path.dirname(os.path.dirname(base))
            parent = os.path.dirname(root)
            temp_dir = os.path.join(parent, "target", "temp")
            os.makedirs(temp_dir, exist_ok=True)

            # Generate logical filename
            # Sanitize scenario_name and module_name for filename
            safe_scenario = re.sub(r'[^\w\-_]', '_', scenario_name)[:50]
            safe_module = re.sub(r'[^\w\-_]', '_', module_name)[:30]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            if detailstype == "featurefile":
                filename = f"{safe_module}_{safe_scenario}_{timestamp}{file_extension}"
            else:  # totalscript
                filename = f"{safe_module}_{safe_scenario}_script_{timestamp}{file_extension}"

            temp_file_path = os.path.join(temp_dir, filename)

            # Write content to file
            with open(temp_file_path, 'w', encoding='utf-8') as f:
                f.write(file_content)

            logger.info(f"✅ [MORE-DETAILS] Created temporary file: {temp_file_path}")

            # Define cleanup callback to delete file after download
            def cleanup_temp_file():
                try:
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                        logger.info(f"🗑️ [MORE-DETAILS] Cleaned up temporary file: {temp_file_path}")
                except Exception as e:
                    logger.error(f"❌ [MORE-DETAILS] Error cleaning up temporary file: {str(e)}")

            # Return FileResponse with background task for cleanup
            from fastapi import BackgroundTasks
            background_tasks = BackgroundTasks()
            background_tasks.add_task(cleanup_temp_file)

            return FileResponse(
                path=temp_file_path,
                filename=filename,
                media_type='text/plain' if detailstype == "featurefile" else 'application/typescript',
                background=background_tasks
            )

    except HTTPException:
        # Clean up temp file if HTTPException occurs
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.info(f"🗑️ [MORE-DETAILS] Cleaned up temporary file after error: {temp_file_path}")
            except Exception as cleanup_error:
                logger.error(f"❌ [MORE-DETAILS] Error cleaning up file after exception: {str(cleanup_error)}")
        raise
    except Exception as e:
        # Clean up temp file if unexpected error occurs
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.info(f"🗑️ [MORE-DETAILS] Cleaned up temporary file after error: {temp_file_path}")
            except Exception as cleanup_error:
                logger.error(f"❌ [MORE-DETAILS] Error cleaning up file after exception: {str(cleanup_error)}")

        logger.error(f"❌ [MORE-DETAILS] Error processing request for runid {runid}: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error while processing request: {str(e)}"
        )


async def get_steps_by_modulenm(module_name: str) -> Dict[str, Any]:
        
    try:
        logger.info(f"🔍 Fetching steps for module: {module_name}")
        engine = _get_engine(BASE_DATABASE_URL)   
        with engine.begin() as conn:              
            query = text(f"""
                SELECT step
                FROM {PGSCHEMA}.t_steps
                WHERE LOWER(TRIM(modulenm)) = LOWER(TRIM(:module_name))
                AND step IS NOT NULL
                AND TRIM(step) != ''
                ORDER BY step
            """)
            result = conn.execute(query, {"module_name": module_name})
            steps = []
            for row in result:
                step_value = row[0]  # step column
                if step_value and str(step_value).strip():
                    steps.append(str(step_value).strip())               
            if not steps:
                return {
                    "success": False,
                    "message": f"No steps found for module '{module_name}'",
                    "data": []
                }
            logger.info(f"✅ Found {len(steps)} steps for module: {module_name}")
            for idx, step in enumerate(steps, 1):
                logger.info(f"  [{idx}] {step}")
            return {
                "success": True,
                "message": f"Successfully retrieved steps for module '{module_name}'",
                "data": steps
            }                
    except Exception as e:
        logger.error(f"❌ Database error while fetching steps for module {module_name}: {str(e)}")
        return {
            "success": False,
            "message": f"Database error: {str(e)}",
            "data": []
        }

    
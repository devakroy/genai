"""
Chewy GenAI Automation API

This module provides a FastAPI application for generating BDD test scenarios.
It includes endpoints for API integration and Playwright automation.
"""

# --- Standard library imports -----------------------------------------------
import sys
import asyncio
import os
import traceback
import logging
import uuid
import re
import hashlib
import re
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

# --- Third-party imports ----------------------------------------------------
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# --- Project imports --------------------------------------------------------
from services.interface_layer.playwright_api import router as playwright_router
from services.interface_layer.autoheal_api import router as autoheal_router
from utils.functions import setup_logging
from utils.constants import PLATFORM_CONFIG
from utils.recording_cleanup import clean_recording_folder_forever, _recording_base_dir
from utils.error_formatter import format_beautiful_error
from utils.excepations import BaseCustomException
from services.interface_layer.common_steps_api import router as common_steps_router
from services.migration_layer.steps_migration import daily_steps_migration_forever, ensure_steps_unique_index, export_steps_data
from services.migration_layer.migration_api import router as migration_router

# --- Windows asyncio policy (must be EARLY) --------------------------------
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Load environment variables from .env file
load_dotenv()

def setup_python_paths():
    """Configure Python path for both development and production environments."""
    current_dir = Path(__file__).parent.absolute()   # .../src
    parent_dir = current_dir.parent.absolute()       # project root
    for p in (str(current_dir), str(parent_dir), "/app", "/app/src", os.getcwd()):
        if p and os.path.exists(p) and p not in sys.path:
            sys.path.insert(0, p)

setup_python_paths()

# --- Global variables -------------------------------------------------------
logger = logging.getLogger(__name__)
RECORDINGS_DIR = _recording_base_dir()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: kick off background cleanup
    task = asyncio.create_task(clean_recording_folder_forever())
    app.state.cleanup_task = task

    # Ensure DB index for steps de-duplication and start daily migration scheduler
    try:
        ensure_steps_unique_index()
    except Exception:
        logger.exception("Failed ensuring steps unique index at startup")
    mig_task = asyncio.create_task(daily_steps_migration_forever(hour=0, minute=10))
    app.state.daily_steps_migration_task = mig_task

    yield  # 👈 App runs here

    # Shutdown: cancel background task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Shutdown: cancel migration scheduler
    mig_task.cancel()
    try:
        await mig_task
    except asyncio.CancelledError:
        pass

def _extract_recording_zip_from_exc(exc: Exception) -> Optional[str]:
    """
    Inspect an exception to see if a recording ZIP path/URL was attached.
    Returns the raw zip path or URL string, or None if not present.
    """
    # case 1: our crawl logic set attribute
    if hasattr(exc, "recording_zip"):
        val = getattr(exc, "recording_zip", None)
        if isinstance(val, str) and val.strip():
            return val

    # case 2: message text contains "RECORDING_ZIP:"
    if exc.args:
        for arg in exc.args:
            if isinstance(arg, str) and "RECORDING_ZIP:" in arg:
                # extract substring after tag
                return arg.split("RECORDING_ZIP:", 1)[1].strip()

    return None

def _abs_url(request: Request, url: str) -> str:
    base = str(request.base_url).rstrip("/")
    return url if url.startswith("http") else f"{base}{url if url.startswith('/') else '/' + url}"

def _zip_filename_from_url_like(value: str) -> str:
    """Extract filename(s) from URL/path-like string(s). Handles pipe-separated multiple URLs."""
    try:
        from urllib.parse import urlparse
        import os as _os
        if not isinstance(value, str) or not value:
            return "recording.zip"
        
        # Handle pipe-separated URLs (multiple videos)
        if " | " in value:
            urls = [url.strip() for url in value.split(" | ") if url.strip()]
            filenames = []
            for url in urls:
                if url.startswith(("http://", "https://", "s3://", "file:")):
                    parsed = urlparse(url)
                    filename = _os.path.basename(parsed.path) or "recording.zip"
                elif "/" in url:
                    filename = url.rsplit('/', 1)[-1]
                else:
                    filename = url
                if filename and filename != "recording.zip":
                    filenames.append(filename)
            # Return comma-separated filenames
            return ", ".join(filenames) if filenames else "recording.zip"
        
        # Handle single URL
        if value.startswith(("http://", "https://", "s3://", "file:")):
            parsed = urlparse(value)
            return _os.path.basename(parsed.path) or "recording.zip"
        if "/" in value:
            return value.rsplit('/', 1)[-1]
        return value
    except Exception:
        return "recording.zip"

# --- FastAPI app setup ------------------------------------------------------
app = FastAPI(
    title="Test Genie- AI Powered",
    description="""TestGenie is an AI-powered web automation platform that transforms human readable input written in BDD (Behavior-Driven Development) feature file format—into executable""",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")

# --- UI Static Files -----------------------------------------------------------
UI_DIR = Path(__file__).parent / "ui"
if UI_DIR.exists():
    app.mount("/TestGenie/static", StaticFiles(directory=str(UI_DIR)), name="ui_static")

# Migration CSV is written by export_steps_data to target/steps_migration/steps_migration.csv
MIGRATION_CSV_PATH = Path(__file__).resolve().parent.parent / "target" / "steps_migration" / "steps_migration.csv"

logger = setup_logging()

# --- Beautiful Error Handlers ------------------------------------
@app.exception_handler(Exception)
async def beautiful_exception_handler(request: Request, exc: Exception):
    """
    Global exception handler that returns beautiful error messages with full technical details
    """
    # Generate unique request ID for tracking
    request_id = str(uuid.uuid4())[:8]

    # Extract run ID from error message if present
    run_id = None
    error_message = str(exc)
    run_id_match = re.search(r'\[Run ID: ([^\]]+)\]', error_message)
    if run_id_match:
        run_id = run_id_match.group(1)

    # Log the error for internal tracking (preserve existing detailed logging)
    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tb_info = traceback.extract_tb(exc.__traceback__)
    error_location = None

    if tb_info:
        last_frame = tb_info[-1]
        error_location = {
            "file": last_frame.filename.split('/')[-1],
            "line": last_frame.lineno,
            "function": last_frame.name,
            "code": last_frame.line
        }

    # logger.error(f"[{request_id}] Exception in {error_location}: {exc}", exc_info=True)

    # Create beautiful error response with full technical details
    beautiful_response = format_beautiful_error(
        exc=exc,
        request_path=str(request.url.path),
        request_method=request.method,
        request_id=request_id
    )
    
    # Add run ID to response if found
    if run_id:
        beautiful_response["run_id"] = run_id
    
    # No unnecessary delay for general exceptions - users should get immediate feedback
    zip_marker = _extract_recording_zip_from_exc(exc)
    if zip_marker:
        td = beautiful_response.setdefault("error", {}).setdefault("technical_details", {})
        td["recording_filename"] = _zip_filename_from_url_like(zip_marker)

    # Determine HTTP status code
    if isinstance(exc, BaseCustomException):
        status_code = 400  # User error
    elif isinstance(exc, HTTPException):
        status_code = exc.status_code
    else:
        status_code = 500  # System error

    return JSONResponse(
        status_code=status_code,
        content=beautiful_response
    )


@app.exception_handler(HTTPException)
async def beautiful_http_exception_handler(request: Request, exc: HTTPException):
    """
    Handle HTTP exceptions with beautiful messages
    """
    request_id = str(uuid.uuid4())[:8]

    # Extract run ID from error detail if present
    run_id = None
    error_detail = str(exc.detail) if exc.detail else ""
    run_id_match = re.search(r'\[Run ID: ([^\]]+)\]', error_detail)
    if run_id_match:
        run_id = run_id_match.group(1)

    # Map common HTTP status codes to beautiful messages
    status_messages = {
        404: {
            "message": "🔍 The page or resource you're looking for doesn't exist",
            "suggestion": "Please check the URL and try again",
            "user_action": "Verify the endpoint path in your request"
        },
        422: {
            "message": "📝 There's an issue with the data you provided",
            "suggestion": "Please check your input and try again",
            "user_action": "Review the required fields and their formats"
        },
        401: {
            "message": "🔐 You need to be authenticated to access this",
            "suggestion": "Please provide valid authentication credentials",
            "user_action": "Check your API key or login credentials"
        },
        403: {
            "message": "🚫 You don't have permission to access this resource",
            "suggestion": "Your credentials don't have the required permissions",
            "user_action": "Contact your administrator for access"
        },
        405: {
            "message": "❌ This HTTP method is not allowed for this endpoint",
            "suggestion": "Please check the API documentation for allowed methods",
            "user_action": "Use the correct HTTP method (GET, POST, PUT, DELETE)"
        },
        429: {
            "message": "🚦 Too many requests - please slow down",
            "suggestion": "You're making requests too quickly",
            "user_action": "Wait a moment and try again"
        }
    }

    error_info = status_messages.get(exc.status_code, {
        "message": f"❌ Request failed with status {exc.status_code}",
        "suggestion": "Please review your request and try again",
        "user_action": "Check the API documentation for correct usage"
    })
    
    # Only add delay for rate limiting (429) - other errors should respond immediately
    if exc.status_code == 429:
        logger.info("⏳ Rate limit detected - waiting 30 seconds before responding...")
        await asyncio.sleep(30)
    
    response = {
        "success": False,
        "error": {
            "type": "http_error",
            "code": f"HTTP_{exc.status_code}",
            "message": error_info["message"],
            "suggestion": error_info["suggestion"],
            "user_action": error_info["user_action"],
            "timestamp": datetime.now().isoformat(),
            "request_info": {
                "path": str(request.url.path),
                "method": request.method,
                "request_id": request_id
            },
            "technical_details": {
                "status_code": exc.status_code,
                "original_detail": exc.detail,
                "exception_type": type(exc).__name__
            }
        }
    }

    # Add run ID to response if found
    if run_id:
        response["run_id"] = run_id

    # logger.warning(f"[{request_id}] HTTP {exc.status_code} on {request.method} {request.url.path}: {exc.detail}")

    return JSONResponse(
        status_code=exc.status_code,
        content=response
    )


# --- Request Tracking Middleware (NEW) --------------------------------------
@app.middleware("http")
async def add_request_tracking(request: Request, call_next):
    """Add request ID for error tracking and enhanced logging"""
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id

    # Enhanced request logging with ID
    logger.info(f"[{request_id}] {request.method} {request.url.path}")

    try:
        response = await call_next(request)
        # Add request ID to response headers for debugging
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception as exc:
        # Handle the exception here instead of letting it bubble up to uvicorn
        # This prevents the ASGI error from being printed
        # Create a proper error response using your existing exception handler logic
        beautiful_response = format_beautiful_error(
            exc=exc,
            request_path=str(request.url.path),
            request_method=request.method,
            request_id=request_id
        )
        # Attach recording link (both technical_details + top-level download_url under error)
        zip_marker = _extract_recording_zip_from_exc(exc)
        if zip_marker:
            td = beautiful_response.setdefault("error", {}).setdefault("technical_details", {})
            td["recording_filename"] = _zip_filename_from_url_like(zip_marker)
        # Determine HTTP status code
        if isinstance(exc, BaseCustomException):
            status_code = 400
        elif isinstance(exc, HTTPException):
            status_code = exc.status_code
        else:
            status_code = 500
        # Return response instead of letting exception bubble up
        error_response = JSONResponse(
            status_code=status_code,
            content=beautiful_response
        )
        error_response.headers["X-Request-ID"] = request_id
        return error_response


# --- CORS Middleware (EXISTING) ---------------------------------------------
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=["*"],
)

# --- CORS Middleware (EXISTING) ---------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Include routers (EXISTING) ---------------------------------------------
app.include_router(playwright_router)
app.include_router(common_steps_router)
app.include_router(migration_router)
app.include_router(autoheal_router)



@app.get("/")
def health_check():
    """Health check endpoint to verify API is running."""
    return {"status": "healthy"}


@app.get("/TestGenie", response_class=HTMLResponse)
async def serve_ui():
    """Serve the TestGenie UI with automatic cache-busting hash."""
    import re
    import time
    ui_path = Path(__file__).parent / "ui" / "index.html"
    if ui_path.exists():
        # Calculate hash of app.js for automatic cache busting
        app_js_path = Path(__file__).parent / "ui" / "app.js"
        js_hash = str(int(time.time()))  # Timestamp fallback
        if app_js_path.exists():
            try:
                js_content = app_js_path.read_bytes()
                js_hash = hashlib.md5(js_content).hexdigest()[:8]  # Use first 8 chars of MD5
            except Exception:
                pass  # Use timestamp fallback if hash calculation fails
        
        # Calculate hash of execution_status_functions.js for cache busting
        es_js_path = Path(__file__).parent / "ui" / "execution_status_functions.js"
        es_js_hash = str(int(time.time()))  # Timestamp fallback
        if es_js_path.exists():
            try:
                es_js_content = es_js_path.read_bytes()
                es_js_hash = hashlib.md5(es_js_content).hexdigest()[:8]  # Use first 8 chars of MD5
            except Exception:
                pass  # Use timestamp fallback if hash calculation fails
        
        # Read and modify HTML to use correct static paths with hash
        html_content = ui_path.read_text(encoding="utf-8")
        # Replace app.js references with hashed version
        html_content = re.sub(
            r'src="app\.js(\?v=[^"]*)?',
            f'src="/TestGenie/static/app.js?v={js_hash}',
            html_content
        )
        # Replace execution_status_functions.js references with hashed version
        html_content = re.sub(
            r'src="execution_status_functions\.js(\?v=[^"]*)?',
            f'src="/TestGenie/static/execution_status_functions.js?v={es_js_hash}',
            html_content
        )
        return HTMLResponse(content=html_content)
    return HTMLResponse(content="<h1>UI not found</h1>", status_code=404)

@app.get("/target/steps_migration/steps_migration.csv", response_class=FileResponse)
async def serve_migration_csv():
    """
    Serve the migration CSV file for the UI (create-steps autocomplete).
    File is created by POST /api/migration/export. Returns 404 if not yet exported.
    """
    if not MIGRATION_CSV_PATH.exists():
        raise HTTPException(status_code=404, detail="Migration CSV not found. Trigger export: POST /api/migration/export")
    return FileResponse(path=str(MIGRATION_CSV_PATH), media_type="text/csv", filename="steps_migration.csv")


@app.post("/api/migration/export")
async def trigger_migration_export():
    """
    Trigger an on-demand migration steps export.
    Exports all steps from t_steps table to CSV file: steps_migration.csv
    Note: File is overwritten on each call.
    """
    try:
        logger.info("Triggering on-demand migration export")
        result = await asyncio.to_thread(export_steps_data)
        
        return {
            "status": "success",
            "message": "Migration data exported successfully",
            "file_path": result.get("file_path"),
            "export_timestamp": result.get("export_timestamp"),
            "total_records": result.get("total_records", 0)
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {str(e)}")
    except Exception as e:
        logger.exception("Migration export failed")
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")

# --- Entrypoint (EXISTING) --------------------------------------------------
def main():
    """Main function to run the application."""
    port = int(PLATFORM_CONFIG.get("PORT", 8000))
    logger.info(f"🚀 Starting Chewy GenAI Automation API on port {port}")
    logger.info(f"🔗 Access the API at: http://localhost:{port}")
    logger.info(f"📖 API Documentation: http://localhost:{port}/docs")

    # On Windows, avoid reload (child may not inherit event-loop policy).
    reload_flag = False if sys.platform.startswith("win") else True

    # Pick module path whether run from project root or from src/
    module_path = "app:app" if Path(__file__).parent.name == "src" else "src.app:app"

    uvicorn.run(
        module_path,
        host="0.0.0.0",
        port=port,
        reload=reload_flag,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
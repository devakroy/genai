import base64
import re
import os
import glob
import logging
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler


def encode_image(image_paths: list[str]) -> list:
    encoded_images = []
    for image_path in image_paths:
        with open(image_path, "rb") as image_file:
            base_64_image = base64.b64encode(image_file.read()).decode('utf-8')
        encoded_images.append(base_64_image)
    return encoded_images


def extract_and_clean_urls(text):
    # Pattern specifically for Confluence page URLs
    confluence_pattern = re.compile(
        r'https?://chewyinc\.atlassian\.net/wiki/spaces/\w+/pages/\d+/[^"\s<>)]+')

    # Find all matching URLs
    cleaned_urls = confluence_pattern.findall(text)

    # # Clean the URLs by removing any trailing special characters
    # cleaned_urls = [url.rstrip('.,;:]}') for url in urls]

    return cleaned_urls[0] if cleaned_urls else None



def setup_logging():
    """Configure logging to output only to the console with formatting."""
    from pathlib import Path

    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    
    # Configure root logger
    logging.basicConfig(level=logging.INFO, force=True)
    
    # Detect environment - only enable file logging in local development
    is_ecs = os.path.exists('/.dockerenv') or os.getenv('ECS_CONTAINER_METADATA_URI') is not None
    is_kubernetes = os.path.exists('/var/run/secrets/kubernetes.io')
    is_container = is_ecs or is_kubernetes
    
    file_handler = None
    if not is_container:
        # LOCAL DEVELOPMENT: Setup file logging
        try:
            target_dir = Path("target")
            target_dir.mkdir(exist_ok=True)
            log_file = target_dir / "execution_logs.txt"
            
            # Create file handler (overwrites on each execution)
            file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8', errors='ignore')
            file_handler.setLevel(logging.INFO)
            file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(file_formatter)
            
            # Use line buffering for reasonable performance with timely updates
            if hasattr(file_handler, 'stream'):
                file_handler.stream = open(log_file, mode='w', encoding='utf-8', errors='ignore', buffering=1)
            
            logging.info("📝 File logging enabled: target/execution_logs.txt")
        except Exception as e:
            logging.warning(f"⚠️ Could not setup file logging: {e}")
            file_handler = None
    else:
        # ECS/KUBERNETES: Use CloudWatch/container logs only
        logging.info("☁️ Container environment detected - using CloudWatch/container logs")
    
    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)


    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers = []  # Clear existing handlers
    root_logger.addHandler(console_handler)
    
    # Add file handler only if it was created (local environment)
    if file_handler:
        root_logger.addHandler(file_handler)


    # Suppress noisy third-party loggers
    third_party_loggers = [
        "urllib3", "asyncio", "watchfiles", "uvicorn", "uvicorn.access", "uvicorn.error",
        "sentence_transformers", "langchain", "httpx", "huggingface_hub", "fastapi",
        "pydantic", "jira", "requests", "matplotlib", "PIL", "boto3", "botocore",
        "s3transfer", "smart_open", "multiprocessing", "concurrent", "uvloop", "websockets","chromadb", "chromadb.telemetry"
    ]
    for logger_name in third_party_loggers:
        logging.getLogger(logger_name).setLevel(logging.ERROR)

    logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)

    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)

    # Create logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    # Remove any existing handlers and add console handler only
    logger.handlers = []
    logger.addHandler(console_handler)

    return logger


def delete_files(folder_path, file_extension):
    for filename in os.listdir(folder_path):
        if filename.endswith(file_extension):
            file_path = os.path.join(folder_path, filename)
            try:
                os.remove(file_path)
                print(f"Deleted: {file_path}")
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")

def get_latest_local_version(folder_path, jira_id, file_extension):
    """
    Determine the latest version of a file in the local filesystem.

    Args:
        folder_path (str): Folder path to check
        jira_id (str): JIRA ID for the files (lowercase)
        file_extension (str): File extension to filter by

    Returns:
        int: Latest version number (0 if no files exist)
    """
    try:
        # Ensure jira_id is lowercase
        jira_id = jira_id.lower()

        # Ensure the folder exists
        if not os.path.exists(folder_path):
            return 0

        # Pattern to match bdd_feature_<jira_id>_v<version>.<extension>
        pattern = f"bdd_feature_{jira_id}_v*.{file_extension}"
        matching_files = glob.glob(os.path.join(folder_path, pattern))

        if not matching_files:
            return 0

        latest_version = 0

        # Extract version numbers from filenames
        version_pattern = re.compile(f"bdd_feature_{jira_id}_v(\\d+)\\.{file_extension}$")

        for file_path in matching_files:
            filename = os.path.basename(file_path)
            match = version_pattern.search(filename)
            if match:
                version = int(match.group(1))
                latest_version = max(latest_version, version)

        return latest_version
    except Exception as e:
        logging.error(f"Error getting latest local version: {str(e)}\n{traceback.format_exc()}")
        return 0

def store_local_feature_file(content, base_path, jira_id, file_extension='feature', subfolder=None):
    """
    Store a feature file locally with versioning.

    Args:
        content (str): Content to save
        base_path (str): Base path for storage
        jira_id (str): JIRA ID for folder organization (lowercase)
        file_extension (str): File extension (default: 'feature')
        subfolder (str): Optional subfolder

    Returns:
        str: Path to the saved file
    """
    try:
        # Ensure jira_id is lowercase
        jira_id = jira_id.lower()

        # Ensure file_extension doesn't have a leading dot
        file_extension = file_extension.lstrip('.')

        # Determine the folder path based on subfolder
        folder_path = os.path.join(base_path, subfolder, jira_id) if subfolder else os.path.join(base_path, jira_id)

        # Create the folder if it doesn't exist
        os.makedirs(folder_path, exist_ok=True)

        # Get latest version
        latest_version = get_latest_local_version(folder_path, jira_id, file_extension)

        # Increment version
        new_version = latest_version + 1

        # Construct filename
        filename = f"bdd_feature_{jira_id}_v{new_version}.{file_extension}"
        file_path = os.path.join(folder_path, filename)

        logging.debug({
            "event": "store_local_feature_file",
            "folder_path": folder_path,
            "filename": filename,
            "version": new_version,
            "status": "starting"
        })

        # Save the file
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        logging.info({
            "event": "store_local_feature_file",
            "status": "success",
            "file_path": file_path,
            "version": new_version
        })

        return file_path
    except Exception as e:
        logging.error(f"Error storing local feature file: {str(e)}\n{traceback.format_exc()}")
        raise
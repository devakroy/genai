import os
import tempfile
import time
import threading
import zipfile
from typing import Dict, Optional
from pathlib import Path
import logging
from typing import List,Union
from dotenv import load_dotenv
from utils.constants import PLATFORM_CONFIG

load_dotenv()
logger = logging.getLogger(__name__)


class TempFileManager:
    """Manager for temporary files with automatic cleanup"""

    def __init__(self, temp_dir: Optional[Union[str, Path]] = None, cleanup_interval: int = 3600):  # 1 hour default
        self.temp_files: Dict[str, dict] = {}
        self.cleanup_interval = cleanup_interval
        self.temp_dir = Path(temp_dir)
        # Ensure directory exists
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_orphaned_files_on_startup()

        # Start cleanup thread
        self.cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self.cleanup_thread.start()

    def create_zip_file(self, files_dict: dict, zip_name: str, jira_id: str, expiry_hours: int = 2) -> str:
        """
        Create a ZIP file with multiple files and return jira_id as identifier

        Args:
            files_dict: Dictionary with filename as key and content as value
            zip_name: Name for the ZIP file (without .zip extension)
            jira_id: JIRA ID to use as identifier
            expiry_hours: Hours until file expires

        Returns:
            JIRA ID for download
        """
        try:
            if not files_dict:
                raise ValueError("files_dict cannot be empty")

            # Create zip file path
            zip_filename = f"{zip_name}.zip"
            zip_path = self.temp_dir / f"{zip_filename}"
            # zip_path = self.temp_dir

            # # Create ZIP file
            # with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            #     for filename, content in files_dict.items():
            #         zipf.writestr(filename, content)

            # Create ZIP file with explicit error handling
            logger.info("Creating ZIP file...")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for filename, content in files_dict.items():
                    logger.info(f"Adding file: {filename}, content length: {len(content) if content else 0}")
                    if content:  # Only add if content exists
                        zipf.writestr(filename, content)
                    else:
                        logger.info(f"WARNING: Empty content for {filename}")


            # Store file info using jira_id as key
            expiry_time = time.time() + (expiry_hours * 3600)
            self.temp_files[jira_id] = {
                'file_path': str(zip_path),
                'original_filename': zip_filename,
                'created_at': time.time(),
                'expires_at': expiry_time,
                'downloaded': False,
                'file_type': 'zip'
            }

            logger.info(f"ZIP file created: {jira_id} -> {zip_filename}")
            return jira_id

        except Exception as e:
            logger.error(f"Error creating ZIP file: {e}")
            raise

    def _cleanup_orphaned_files_on_startup(self):
        for file_path in self.temp_dir.glob("*.zip"):
            file_age = time.time() - file_path.stat().st_mtime  # Calculate age
            if file_age > 7200:  # If older than 2 hours
                file_path.unlink()  # DELETE ✅

    def get_file_info(self, jira_id: str) -> Optional[dict]:
        """Get file info by jira_id"""
        return self.temp_files.get(jira_id)

    def mark_downloaded(self, jira_id: str):
        """Mark file as downloaded"""
        if jira_id in self.temp_files:
            self.temp_files[jira_id]['downloaded'] = True

    def cleanup_expired_files(self):
        """Remove expired files"""
        current_time = time.time()
        expired_jira_ids = []

        for jira_id, file_info in self.temp_files.items():
            if current_time > file_info['expires_at']:
                try:
                    if os.path.exists(file_info['file_path']):
                        os.remove(file_info['file_path'])
                    expired_jira_ids.append(jira_id)
                except Exception as e:
                    logger.error(f"Error removing expired file {jira_id}: {e}")

        for jira_id in expired_jira_ids:
            del self.temp_files[jira_id]

        if expired_jira_ids:
            logger.info(f"Cleaned up {len(expired_jira_ids)} expired files")

    def _cleanup_loop(self):
        """Background cleanup loop"""
        while True:
            time.sleep(self.cleanup_interval)
            self.cleanup_expired_files()


base = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(os.path.dirname(base))

custom_temp_dir  = os.path.join(root, PLATFORM_CONFIG.get("PLAYWRIGHT_STORE_DIR"))
custom_json_dir  = os.path.join(root, PLATFORM_CONFIG.get("JSON_FILE_PATH"))


try:
    if not os.path.exists(custom_temp_dir):
        os.makedirs(custom_temp_dir)
        logger.info(f"Target temp dir created at: {custom_temp_dir}")
    else:
        logger.info(f"Target temp dir already exists: {custom_temp_dir}")

    if not os.path.exists(custom_json_dir):
        os.makedirs(custom_json_dir)
        logger.info(f"Target JSON dir created at: {custom_json_dir}")
    else:
        logger.info(f"Target JSON dir already exists: {custom_json_dir}")

except Exception as e:
    logger.error(f"Could not create directories: {e}")

temp_file_manager = TempFileManager(custom_temp_dir or tempfile.gettempdir())
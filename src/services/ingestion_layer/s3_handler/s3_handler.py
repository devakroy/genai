import boto3
import re
import logging
import os
import traceback
from smart_open import open
from utils.constants import PLATFORM_CONFIG

# Configure logging
logger = logging.getLogger(__name__)

class S3FileReader:
    """Handles S3 file operations using boto3."""

    def __init__(self, region_name: str = 'us-east-1'):
        """Initialize S3 client with AWS credentials."""
        logger.debug({"event": "S3FileReader.__init__", "status": "initializing"})

        try:
            self.s3_client = boto3.client(
                's3',
                region_name=region_name,
                aws_access_key_id=PLATFORM_CONFIG.get("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=PLATFORM_CONFIG.get("AWS_SECRET_ACCESS_KEY"),
                aws_session_token=PLATFORM_CONFIG.get("AWS_SESSION_TOKEN")
            )
            logger.debug({"event": "S3FileReader.__init__", "status": "initialized"})
        except Exception as e:
            logger.error({
                "event": "S3FileReader.__init__",
                "error": str(e),
                "trace": traceback.format_exc()
            })
            raise

    def parse_s3_url(self, s3_url: str) -> tuple[str, str]:
        """
        Parse S3 URL to extract bucket name and key.

        Args:
            s3_url (str): Full S3 URL path

        Returns:
            tuple: (bucket_name, key)
        """
        pattern = r"https://([^.]+)\.s3\.amazonaws\.com/(.+)"
        match = re.match(pattern, s3_url)
        if match:
            bucket = match.group(1)
            key = match.group(2)
            return bucket, key
        raise ValueError("Invalid S3 URL format")

    def read_bdd_from_s3(self, s3_url: str) -> str:
        """
        Read BDD content from S3 file.

        Args:
            s3_url (str): Full S3 URL path

        Returns:
            str: File content
        """
        try:
            # Parse S3 URL
            bucket, key = self.parse_s3_url(s3_url)

            # Read file content directly without creating a new instance
            bdd_content = self.read_file(bucket=bucket, key=key)

            logger.info({
                "event": "read_bdd_from_s3",
                "status": "success",
                "bucket": bucket,
                "key": key,
                "content_length": len(bdd_content)
            })

            return bdd_content

        except Exception as e:
            logger.error({
                "event": "read_bdd_from_s3",
                "status": "failed",
                "error": str(e),
                "trace": traceback.format_exc()
            })
            raise

    def read_file(self, bucket: str, key: str) -> str:
        """
        Read a file from S3 bucket.

        Args:
            bucket (str): S3 bucket name
            key (str): S3 object key (file path)

        Returns:
            str: Content of the file

        Raises:
            Exception: If file reading fails
        """
        try:
            logger.debug({
                "event": "read_s3_file",
                "bucket": bucket,
                "key": key,
                "status": "starting"
            })

            response = self.s3_client.get_object(Bucket=bucket, Key=key)
            file_content = response['Body'].read().decode('utf-8')

            logger.debug({
                "event": "read_s3_file",
                "status": "success",
                "content_length": len(file_content)
            })

            return file_content

        except self.s3_client.exceptions.NoSuchKey:
            logger.error({
                "event": "read_s3_file",
                "status": "failed",
                "error": "File not found",
                "bucket": bucket,
                "key": key
            })
            raise FileNotFoundError(f"File {key} not found in bucket {bucket}")

        except Exception as e:
            logger.error({
                "event": "read_s3_file",
                "status": "failed",
                "error": str(e),
                "trace": traceback.format_exc()
            })
            raise

    def upload_file_content(self, content: str, bucket_name: str, object_name: str) -> bool:
        """
        Upload string content directly to an S3 bucket.

        Args:
            content (str): Content to upload
            bucket_name (str): Name of the S3 bucket
            object_name (str): S3 object name (file path)

        Returns:
            bool: True if content was uploaded successfully

        Raises:
            Exception: If upload fails
        """
        try:
            logger.debug({
                "event": "upload_file_content",
                "bucket": bucket_name,
                "object_name": object_name,
                "status": "starting"
            })
            if "\\n" in content:
                content = content.replace("\\n", "\n")
                
            self.s3_client.put_object(
                Body=content.encode('utf-8'),
                Bucket=bucket_name,
                Key=object_name
            )

            logger.debug({
                "event": "upload_file_content",
                "status": "success",
                "object_name": object_name
            })

            return True

        except Exception as e:
            logger.error({
                "event": "upload_file_content",
                "status": "failed",
                "error": str(e),
                "trace": traceback.format_exc()
            })
            raise

    def get_latest_version(self, bucket_name: str, prefix: str, file_extension: str) -> int:
        """
        Determine the latest version of a file for a given prefix path.
        
        Args:
            bucket_name (str): Name of the S3 bucket
            prefix (str): Prefix path to check (e.g., "JIRA-123" or "Ready_For_Review/JIRA-123")
            file_extension (str): File extension to filter by (.feature or .ts)
            
        Returns:
            int: Latest version number (0 if no files exist)
        """
        try:
            # Extract just the JIRA ID from the prefix
            jira_id = prefix.split('/')[-1]
            
            # List all objects in the prefix
            response = self.s3_client.list_objects_v2(
                Bucket=bucket_name,
                Prefix=f"{prefix}/"
            )
            
            if 'Contents' not in response:
                return 0
            
            # Pattern to match bdd_feature_<jira_id>_v<version>.<extension>
            pattern = re.compile(f".*bdd_feature_{jira_id}_v(\\d+)\\.{file_extension}\$")
            
            latest_version = 0
            
            # Check each file and find the highest version
            for item in response['Contents']:
                key = item['Key']
                match = pattern.search(key)
                if match:
                    version = int(match.group(1))
                    latest_version = max(latest_version, version)
                    
            return latest_version
        except Exception as e:
            logger.error({
                "event": "get_latest_version",
                "status": "failed",
                "error": str(e),
                "trace": traceback.format_exc()
            })
            raise

    def store_feature_file(self, content: str, bucket_name: str, jira_id: str, 
                           file_extension: str = 'feature', subfolder: str = None) -> str:
        """
        Store a feature file in the appropriate S3 folder with versioning.
        
        Args:
            content (str): Content to upload
            bucket_name (str): Name of the S3 bucket
            jira_id (str): JIRA ID for folder organization
            file_extension (str): File extension (default: 'feature')
            subfolder (str): Optional subfolder (e.g., "Ready_For_Review")
            
        Returns:
            str: S3 object key of the uploaded file
        """
        try:
            # Ensure file_extension doesn't have a leading dot
            file_extension = file_extension.lstrip('.')
            
            # Determine the prefix based on subfolder
            prefix = f"{subfolder}/{jira_id}" if subfolder else jira_id
            
            # Get latest version
            latest_version = self.get_latest_version(bucket_name, prefix, file_extension)
            
            # Increment version
            new_version = latest_version + 1
            
            # Construct object name
            object_name = f"{prefix}/bdd_feature_{jira_id}_v{new_version}.{file_extension}"
            
            logger.debug({
                "event": "store_feature_file",
                "bucket": bucket_name,
                "object_name": object_name,
                "version": new_version,
                "status": "starting"
            })
            
            # Upload the file
            self.upload_file_content(content, bucket_name, object_name)
            
            logger.info({
                "event": "store_feature_file",
                "status": "success",
                "object_name": object_name,
                "version": new_version
            })
            
            return object_name
        except Exception as e:
            logger.error({
                "event": "store_feature_file",
                "status": "failed",
                "error": str(e),
                "trace": traceback.format_exc()
            })
            raise
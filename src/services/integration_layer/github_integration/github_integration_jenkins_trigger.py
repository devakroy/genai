import sys
import time
import requests
import json
from typing import Dict, Optional, List
import base64
from urllib.parse import urlencode, quote
import logging
from fastapi import HTTPException
from .ts_step_deduper import filter_ts_steps_against_repo
import re


logger = logging.getLogger(__name__)


class GitHubAPIClient:
    """
    A client class for interacting with GitHub API to retrieve repository details.
    """

    def __init__(self, personal_access_token: str):
        """
        Initialize the GitHub API client.

        Args:
            personal_access_token (str): GitHub personal access token for authentication
        """
        self.token = personal_access_token
        self.base_url = "https://api.github.com"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "GitHubAPIClient/1.0"
        }
        logger.info("GitHubAPIClient initialized")

    def get_repository_details(self, owner: str, repo: str) -> Optional[Dict]:
        """
        Get repository details from GitHub API.

        Args:
            owner (str): Repository owner/organization name
            repo (str): Repository name

        Returns:
            Dict: Repository details if successful, None if failed
        """
        url = f"{self.base_url}/repos/{owner}/{repo}"
        logger.info(f"Getting repository details for {owner}/{repo}")

        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()

            repo_data = response.json()
            logger.info(f"✅ Successfully retrieved repository details for {owner}/{repo}")
            return repo_data

        except requests.exceptions.HTTPError as http_err:
            logger.error(f"❌ HTTP error occurred: {http_err}")
            logger.error(f"Response: {response.text}")
            return None
        except requests.exceptions.RequestException as req_err:
            logger.error(f"❌ Request error occurred: {req_err}")
            return None
        except json.JSONDecodeError as json_err:
            logger.error(f"❌ JSON decode error: {json_err}")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error: {e}")
            return None


class GitHubBranchManager:
    """
    Enhanced GitHub client for branch management workflow
    """

    def __init__(self, personal_access_token: str):
        self.token = personal_access_token
        self.base_url = "https://api.github.com"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "GitHubBranchManager/1.0"
        }
        logger.info("GitHubBranchManager initialized")

    def create_branch_with_auto_versioning(self, owner: str, repo: str, base_branch_name: str,
                                           from_branch: str = "main") -> Dict:
        """
        Create branch with smart auto-versioning - finds highest version and creates next one.
        """
        logger.info(f"🌿 Creating branch with smart auto-versioning: '{base_branch_name}' from '{from_branch}'")

        original_name = base_branch_name

        # Try original name first
        if not self.branch_exists(owner, repo, original_name):
            success = self.create_branch(owner, repo, original_name, from_branch)
            if success:
                return {
                    "success": True,
                    "branch_name": original_name,
                    "version": 0,
                    "error": None,
                    "method": "original"
                }

        # ✅ SMART APPROACH: Find the highest existing version
        logger.info(f"⚠️ Branch '{original_name}' exists, finding highest version...")

        highest_version = 0

        # Check versions 1-50 to find the highest existing version
        for version in range(1, 51):
            versioned_name = f"{original_name}-v{version}"
            if self.branch_exists(owner, repo, versioned_name):
                highest_version = version
                logger.info(f"🔍 Found existing version: {versioned_name}")
            else:
                # First non-existing version found
                break

        # Create the next version after the highest found
        next_version = highest_version + 1
        next_branch_name = f"{original_name}-v{next_version}"

        logger.info(f"🚀 Creating next available version: {next_branch_name}")

        success = self.create_branch(owner, repo, next_branch_name, from_branch)

        if success:
            logger.info(f"✅ Created smart versioned branch: {next_branch_name} (skipped v1-v{highest_version})")
            return {
                "success": True,
                "branch_name": next_branch_name,
                "version": next_version,
                "highest_existing_version": highest_version,
                "error": None,
                "method": "smart_versioning"
            }
        else:
            return {
                "success": False,
                "branch_name": None,
                "version": next_version,
                "highest_existing_version": highest_version,
                "error": f"Failed to create smart versioned branch {next_branch_name}",
                "method": "smart_versioning"
            }

    def branch_exists(self, owner: str, repo: str, branch_name: str) -> bool:
        """
        Check if a branch already exists.

        Returns:
            bool: True if branch exists, False if not
        """
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/git/refs/heads/{branch_name}"
            response = requests.get(url, headers=self.headers, timeout=30)

            if response.status_code == 200:
                logger.info(f"✅ Branch '{branch_name}' already exists")
                return True
            elif response.status_code == 404:
                logger.info(f"ℹ️ Branch '{branch_name}' does not exist")
                return False
            else:
                logger.warning(f"⚠️ Unexpected response checking branch: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"❌ Error checking if branch exists: {e}")
            return False

    def create_branch(self, owner: str, repo: str, new_branch: str, from_branch: str = "main") -> bool:
        """
        Create a new branch from existing branch.

        Args:
            owner (str): Repository owner
            repo (str): Repository name
            new_branch (str): New branch name
            from_branch (str): Source branch name

        Returns:
            bool: Success status
        """
        logger.info(f"🌿 Creating branch '{new_branch}' from '{from_branch}' in {owner}/{repo}")

        try:

            if self.branch_exists(owner, repo, new_branch):
                logger.warning(f"⚠️ Branch '{new_branch}' already exists. Skipping creation.")
                return False
            # Get source branch SHA
            ref_url = f"{self.base_url}/repos/{owner}/{repo}/git/refs/heads/{from_branch}"
            response = requests.get(ref_url, headers=self.headers, timeout=30)
            response.raise_for_status()

            source_sha = response.json()['object']['sha']
            logger.info(f"📍 Source branch SHA: {source_sha}")

            # Create new branch
            create_url = f"{self.base_url}/repos/{owner}/{repo}/git/refs"
            payload = {
                "ref": f"refs/heads/{new_branch}",
                "sha": source_sha
            }

            response = requests.post(create_url, headers=self.headers, data=json.dumps(payload), timeout=30)
            response.raise_for_status()

            logger.info(f"✅ Branch '{new_branch}' created successfully")
            return True

        except requests.exceptions.HTTPError as http_err:
            logger.error(f"❌ HTTP error creating branch: {http_err}")
            if response.status_code == 422:
                logger.error("Branch might already exist or invalid branch name")
            logger.error(f"Response: {response.text}")
            return False
        except Exception as e:
            logger.error(f"❌ Error creating branch '{new_branch}': {e}")
            return False

    #def add_file_to_branch(self, owner: str, repo: str, branch: str, file_path: str, file_content: str, commit_message: str) -> bool:
        """
        Add a single file to a specific branch.

        Args:
            owner (str): Repository owner
            repo (str): Repository name
            branch (str): Target branch
            file_path (str): File path in repository
            file_content (str): File content
            commit_message (str): Commit message

        Returns:
            bool: Success status
        """
        logger.info(f"📝 Adding file '{file_path}' to {owner}/{repo}:{branch}")

        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/contents/{file_path}"

            # Check if file already exists
            try:
                check_response = requests.get(f"{url}?ref={branch}", headers=self.headers, timeout=30)
                if check_response.status_code == 200:
                    logger.warning(f"⚠️ File '{file_path}' already exists on branch '{branch}'")
                    return False
            except:
                pass  # File doesn't exist, which is fine

            # Encode content to base64
            base64_content = base64.b64encode(file_content.encode('utf-8')).decode('utf-8')

            payload = {
                "message": commit_message,
                "content": base64_content,
                "branch": branch
            }

            response = requests.put(url, headers=self.headers, data=json.dumps(payload), timeout=30)
            response.raise_for_status()

            commit_sha = response.json().get('commit', {}).get('sha', 'N/A')
            logger.info(f"✅ File '{file_path}' added successfully. Commit SHA: {commit_sha}")
            return True

        except requests.exceptions.HTTPError as http_err:
            logger.error(f"❌ HTTP error adding file: {http_err}")
            logger.error(f"Response: {response.text}")
            return False
        except Exception as e:
            logger.error(f"❌ Error adding file '{file_path}': {e}")
            return False

    def add_file_to_branch(self, owner: str, repo: str, branch: str, file_path: str, file_content: str,
                       commit_message: str) -> bool:
        """
        Add a single file to a specific branch (overwrites if exists).
        """
        logger.info(f":memo: Adding file '{file_path}' to {owner}/{repo}:{branch}")
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/contents/{file_path}"
            # Check if file already exists and get SHA for overwrite
            existing_sha = None
            try:
                check_response = requests.get(f"{url}?ref={branch}", headers=self.headers, timeout=30)
                if check_response.status_code == 200:
                    existing_sha = check_response.json().get('sha')
                    logger.info(f":arrows_counterclockwise: File '{file_path}' exists, will overwrite (SHA: {existing_sha})")
            except:
                logger.info(f":page_facing_up: Creating new file '{file_path}'")
            # Encode content to base64
            base64_content = base64.b64encode(file_content.encode('utf-8')).decode('utf-8')
            # Build payload
            payload = {
                "message": commit_message,
                "content": base64_content,
                "branch": branch
            }
            # Add SHA if file exists (required for overwrite)
            if existing_sha:
                payload["sha"] = existing_sha
            response = requests.put(url, headers=self.headers, data=json.dumps(payload), timeout=30)
            response.raise_for_status()
            commit_sha = response.json().get('commit', {}).get('sha', 'N/A')
            action = "updated" if existing_sha else "created"
            logger.info(f":white_check_mark: File '{file_path}' {action} successfully. Commit SHA: {commit_sha}")
            return True
        except requests.exceptions.HTTPError as http_err:
            logger.error(f":x: HTTP error adding file: {http_err}")
            logger.error(f"Response: {response.text}")
            return False
        except Exception as e:
            logger.error(f":x: Error adding file '{file_path}': {e}")
            return False

    def add_multiple_files_to_branch(self, owner: str, repo: str, branch: str, files: List[Dict]) -> bool:
        """
        Add multiple files to a specific branch.

        Args:
            owner (str): Repository owner
            repo (str): Repository name
            branch (str): Target branch
            files (List[Dict]): Files to add [{'path': 'file.txt', 'content': 'content', 'message': 'commit msg'}]

        Returns:
            bool: Success status
        """
        logger.info(f"📋 Adding {len(files)} files to {owner}/{repo}:{branch}")

        success_count = 0

        for file_info in files:
            try:
                file_path = file_info['path']
                file_content = file_info['content']
                commit_message = file_info.get('message', f"Add {file_path}")

                success = self.add_file_to_branch(owner, repo, branch, file_path, file_content, commit_message)

                if success:
                    success_count += 1
                    logger.info(f"✅ Added file: {file_path} ({success_count}/{len(files)})")
                else:
                    logger.error(f"❌ Failed to add file: {file_path}")

                # Add small delay to avoid rate limiting
                time.sleep(1)

            except Exception as e:
                logger.error(f"❌ Error processing file {file_info.get('path', 'unknown')}: {e}")

        logger.info(f"📊 File addition summary: {success_count}/{len(files)} files added successfully")
        return success_count == len(files)


class GitHubBasicPR:
    """
    A simple class for creating basic Pull Requests via GitHub API.
    """

    def __init__(self):
        """
        Initialize the GitHub Basic PR class.
        """
        self.base_url = "https://api.github.com"
        logger.info("GitHubBasicPR initialized")

    def create_basic_pr(self,
                        project_name: str,
                        head: str,
                        base: str,
                        authorization: str,
                        title: Optional[str] = None,
                        body: Optional[str] = None) -> Optional[Dict]:
        """
        Create a basic Pull Request in GitHub repository.
        """
        logger.info(f"🚀 Creating PR: {head} → {base} in {project_name}")

        # Validate project_name format
        if '/' not in project_name:
            logger.error("❌ Error: project_name must be in format 'owner/repo'")
            return None

        try:
            owner, repo = project_name.split('/', 1)
        except ValueError:
            logger.error("❌ Error: Invalid project_name format. Expected 'owner/repo'")
            return None

        # Generate default title if not provided
        if not title:
            title = f"Merge {head} into {base}"

        # Prepare headers
        headers = {
            "Authorization": f"Bearer {authorization}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "GitHubBasicPR/1.0"
        }

        # Construct API URL
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls"

        # Prepare request payload
        payload = {
            "title": title,
            "head": head,
            "base": base
        }

        # Add body if provided
        if body:
            payload["body"] = body

        try:
            logger.info(f"📁 Repository: {project_name}")
            logger.info(f"📝 Title: {title}")

            # Make the API call
            response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
            response.raise_for_status()

            # Parse response
            pr_data = response.json()
            pr_number = pr_data.get('number')
            pr_url = pr_data.get('html_url')
            pr_state = pr_data.get('state')

            # Success message
            logger.info(f"✅ Pull Request created successfully!")
            logger.info(f"📊 PR Number: #{pr_number}")
            logger.info(f"🔗 URL: {pr_url}")
            logger.info(f"📈 State: {pr_state}")

            return pr_data

        except requests.exceptions.HTTPError as http_err:
            logger.error(f"❌ HTTP error occurred: {http_err}")
            if response.status_code == 401:
                logger.error("🔐 Check your authorization token")
            elif response.status_code == 404:
                logger.error("🔍 Repository not found or no access")
            elif response.status_code == 422:
                logger.error("⚠️ Validation failed - check branch names or PR already exists")
            logger.error(f"📄 Response: {response.text}")
            return None

        except requests.exceptions.RequestException as req_err:
            logger.error(f"❌ Request error occurred: {req_err}")
            return None

        except json.JSONDecodeError as json_err:
            logger.error(f"❌ JSON decode error: {json_err}")
            return None

        except Exception as e:
            logger.error(f"❌ Unexpected error: {e}")
            return None

class JenkinsTestGenieRunner:

    """
    A client class for triggering the test-genie_execution Jenkins job with parameters.
    """

    def __init__(self, jenkins_project_name , jenkins_username,jenkins_api_token, jenkins_url: str = "https://jenkins-nonprod.shss.chewy.com/"):
        """
        Initialize the Jenkins Test Genie Runner.

        Args:
            jenkins_url (str): Base Jenkins URL
        """
        self.jenkins_url = jenkins_url.rstrip('/')
        self.job_name = f"{jenkins_project_name}"
        self.jenkins_username = jenkins_username
        self.jenkins_api_token = str(jenkins_api_token).strip()


    def trigger_test_job(self,
                         branch_name: str,
                         test_tag: str) -> Dict:  # ✅ Change from Optional[Dict] to Dict
        """
        Trigger the test-genie_execution Jenkins job with parameters.

        Returns:
            Dict: Always returns structured result (never None)
        """

        result = {
            "success": False,
            "error": None,
            "details": {},
            "stage": "jenkins_job_trigger"
        }

        try:
            # Prepare parameters
            params = {
                'TestTags': test_tag,
                'branch': branch_name
            }

            # Build URL with query parameters - revert to simple concatenation
            base_url = f"{self.jenkins_url}/job/{self.job_name}/buildWithParameters"
            url = f"{base_url}?TestTags={test_tag}&branch={branch_name}"

            # Prepare headers
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "JenkinsTestGenieRunner/1.0"
            }

            logger.info(f"🚀 Triggering Jenkins job: {self.job_name}")
            logger.info(f"📋 Parameters:")
            logger.info(f"   - Branch: {branch_name}")
            logger.info(f"   - Test Tags: {test_tag}")
            logger.info(f"📍 URL: {url}")

            # Make the API call with basic authentication
            response = requests.post(
                url,
                headers=headers,
                auth=(self.jenkins_username, self.jenkins_api_token),
                timeout=30
            )

            response.raise_for_status()

            # Get queue item location from headers
            queue_location = response.headers.get('Location', 'N/A')

            # ✅ Update result structure
            result.update({
                "success": True,
                "error": None,
                "details": {
                    "job_name": self.job_name,
                    "branch": branch_name,
                    "test_tags": test_tag,
                    "queue_location": queue_location,
                    "response_code": response.status_code,
                    "triggered_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
            })

            logger.info(f"✅ Job triggered successfully!")
            logger.info(f"📊 Response Code: {response.status_code}")
            logger.info(f"📍 Queue Location: {queue_location}")
            logger.info(f"⏰ Triggered At: {result['details']['triggered_at']}")

            return result  # ✅ Return structured result

        except requests.exceptions.HTTPError as http_err:
            error_msg = f"HTTP error occurred: {http_err}"
            logger.info(f"❌ {error_msg}")

            # ✅ Add detailed error handling
            if response.status_code == 401:
                result["error"] = "Jenkins authentication failed - check username/API token"
                logger.info("🔐 Authentication failed - check username/password")
            elif response.status_code == 403:
                result["error"] = "Jenkins access forbidden - check permissions"
                logger.info("🚫 Access forbidden - check permissions")
            elif response.status_code == 404:
                result["error"] = f"Jenkins job '{self.job_name}' not found"
                logger.info("🔍 Job not found - check job name")
            elif response.status_code == 400:
                result["error"] = "Jenkins bad request - check parameters"
                logger.info("⚠️ Bad request - check parameters")
            else:
                result["error"] = f"Jenkins HTTP error: {http_err}"

            result["details"] = {
                "status_code": response.status_code,
                "response_text": response.text,
                "job_name": self.job_name,
                "jenkins_url": self.jenkins_url
            }

            logger.info(f"📄 Response: {response.text}")
            return result  # ✅ Change from return None

        except requests.exceptions.RequestException as req_err:
            result["error"] = f"Jenkins request error: {req_err}"
            result["details"] = {"exception_type": type(req_err).__name__}  # ✅ Add this
            logger.info(f"❌ Request error occurred: {req_err}")
            return result  # ✅ Change from return None

        except Exception as e:
            result["error"] = f"Jenkins job trigger failed: {str(e)}"
            result["details"] = {"exception_type": type(e).__name__}  # ✅ Add this
            logger.info(f"❌ Unexpected error: {e}")
            return result  # ✅ Change from return None

    def trigger_smoke_tests(self, user_id: str, password: str, branch_name: str) -> Optional[Dict]:
        """
        Convenience method to trigger smoke tests.

        Args:
            user_id (str): Jenkins username
            password (str): Jenkins password/API token
            branch_name (str): Git branch name

        Returns:
            Dict: Response information
        """
        return self.trigger_test_job(branch_name, "smoke")

    def trigger_regression_tests(self, user_id: str, password: str, branch_name: str) -> Optional[Dict]:
        """
        Convenience method to trigger regression tests.

        Args:
            user_id (str): Jenkins username
            password (str): Jenkins password/API token
            branch_name (str): Git branch name

        Returns:
            Dict: Response information
        """
        return self.trigger_test_job(branch_name, "regression")

    def trigger_multiple_test_tags(self,
                                   user_id: str,
                                   password: str,
                                   branch_name: str,
                                   test_tags: List[str]) -> Optional[Dict]:
        """
        Trigger tests with multiple tags.

        Args:
            user_id (str): Jenkins username
            password (str): Jenkins password/API token
            branch_name (str): Git branch name
            test_tags (List[str]): List of test tags

        Returns:
            Dict: Response information
        """
        combined_tags = ",".join(test_tags)
        return self.trigger_test_job(branch_name, combined_tags)

    def get_job_status(self, user_id: str, password: str) -> Optional[Dict]:
        """
        Get the current status of the test-genie_execution job.

        Args:
            user_id (str): Jenkins username
            password (str): Jenkins password/API token

        Returns:
            Dict: Job status information
        """
        url = f"{self.jenkins_url}/job/{self.job_name}/api/json"

        try:
            response = requests.get(url, auth=(user_id, password))
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.info(f"❌ Error getting job status: {e}")
            return None

    def get_last_build_info(self, user_id: str, password: str) -> Optional[Dict]:
        """
        Get information about the last build.

        Args:
            user_id (str): Jenkins username
            password (str): Jenkins password/API token

        Returns:
            Dict: Last build information
        """
        url = f"{self.jenkins_url}/job/{self.job_name}/lastBuild/api/json"

        try:
            response = requests.get(url, auth=(user_id, password))
            response.raise_for_status()

            build_info = response.json()

            logger.info(f"📊 Last Build Information:")
            logger.info(f"   - Build Number: #{build_info.get('number', 'N/A')}")
            logger.info(f"   - Result: {build_info.get('result', 'RUNNING')}")
            logger.info(f"   - Duration: {build_info.get('duration', 0) / 1000:.1f} seconds")
            logger.info(f"   - Building: {build_info.get('building', False)}")

            return build_info
        except Exception as e:
            logger.info(f"❌ Error getting last build info: {e}")
            return None


# Standalone function for quick job triggering
def trigger_jenkins_test_job(user_id: str, password: str, branch_name: str, test_tag: str) -> Optional[Dict]:
    """
    Standalone function to trigger Jenkins test job.

    Args:
        user_id (str): Jenkins username
        password (str): Jenkins password/API token
        branch_name (str): Git branch name
        test_tag (str): Test tags to execute

    Returns:
        Dict: Response information
    """
    runner = JenkinsTestGenieRunner()
    return runner.trigger_test_job(branch_name, test_tag)





class GitHubSimpleWorkflowManager:
    """
    Simplified workflow manager that works within existing repository:
    1. Check repo exists
    2. Create new branch from main
    3. Add new files to branch
    4. Raise PR: new-branch → main
    """

    def __init__(self, github_token: str):
        self.token = github_token
        self.api_client = GitHubAPIClient(github_token)
        self.branch_manager = GitHubBranchManager(github_token)
        self.pr_manager = GitHubBasicPR()
        logger.info("GitHubSimpleWorkflowManager initialized")


    def execute_simple_workflow(self,
                                repo_owner: str,
                                repo_name: str,
                                new_branch_name: str,
                                new_files: List[Dict],
                                pr_title: str = None,
                                pr_body: str = None,
                                base_branch: str = "main") -> Dict:
        """
        Execute the simplified workflow within existing repository.
        """
        result = {
            "success": False,
            "stage": None,
            "error": None,
            "details": {},
            "completed_steps": []
        }

        logger.info("🚀 Starting simplified GitHub workflow")
        logger.info(f"Repository: {repo_owner}/{repo_name}")
        logger.info(f"New Branch: {new_branch_name}")
        logger.info(f"Base Branch: {base_branch}")

        try:
            # Step 1: Check if repository exists
            logger.info("=" * 60)
            logger.info("STEP 1: Checking repository exists")
            logger.info("=" * 60)

            result["stage"] = "repository_validation"

            repo_details = self.api_client.get_repository_details(repo_owner, repo_name)

            if not repo_details:
                result["error"] = f"Repository {repo_owner}/{repo_name} not found or access denied"
                result["details"] = {"repo_owner": repo_owner, "repo_name": repo_name}
                return result

            result["completed_steps"].append("repository_validation")
            result["details"]["repository_url"] = repo_details.get('html_url')
            logger.info(f"✅ Repository found: {repo_details.get('html_url')}")

            # Step 2: Create new branch (with smart auto-versioning)
            logger.info("=" * 60)
            logger.info("STEP 2: Creating new branch with smart versioning")
            logger.info("=" * 60)

            result["stage"] = "branch_creation"

            # ✅ UPDATED: Use the smart auto-versioning method with new return structure
            branch_result = self.branch_manager.create_branch_with_auto_versioning(
                repo_owner, repo_name, new_branch_name, base_branch
            )

            if not branch_result["success"]:
                result["error"] = branch_result["error"]
                result["details"] = {
                    "requested_branch": new_branch_name,
                    "base_branch": base_branch,
                    "branch_result": branch_result,
                    "versioning_method": branch_result.get("method", "unknown")
                }
                return result

            # ✅ UPDATED: Extract new fields from smart versioning result
            actual_branch_name = branch_result["branch_name"]
            branch_version = branch_result["version"]
            versioning_method = branch_result.get("method", "unknown")
            highest_existing = branch_result.get("highest_existing_version", 0)

            result["completed_steps"].append("branch_creation")
            result["details"].update({
                "branch_created": actual_branch_name,
                "branch_version": branch_version,
                "original_branch_requested": new_branch_name,
                "versioning_method": versioning_method,
                "highest_existing_version": highest_existing
            })

            # ✅ UPDATED: Enhanced logging based on versioning method
            if versioning_method == "original":
                logger.info(f"✅ Original branch '{actual_branch_name}' created successfully")
            elif versioning_method == "smart_versioning":
                logger.info(f"✅ Smart versioned branch '{actual_branch_name}' created")
                logger.info(f"📊 Skipped existing versions: v1-v{highest_existing}")
                logger.info(f"🎯 Created version: v{branch_version}")

            # Step 3: Add new files to branch
            logger.info("=" * 60)
            logger.info("STEP 3: Adding new files to branch")
            logger.info("=" * 60)

            result["stage"] = "file_addition"

            # Add commit message to each file if not provided
            files_with_messages = []
            for file_info in new_files:
                file_data = {
                    'path': file_info['path'],
                    'content': file_info['content'],
                    'message': file_info.get('message', f"Add {file_info['path']}")
                }
                files_with_messages.append(file_data)

            # Use actual_branch_name (which could be versioned)
            files_added = self.branch_manager.add_multiple_files_to_branch(
                repo_owner, repo_name, actual_branch_name, files_with_messages
            )

            if not files_added:
                result["error"] = f"Failed to add files to branch '{actual_branch_name}'"
                result["details"].update({
                    "branch": actual_branch_name,
                    "files_to_add": [f["path"] for f in files_with_messages],
                    "total_files": len(files_with_messages)
                })
                return result

            result["completed_steps"].append("file_addition")
            result["details"]["files_added"] = [f["path"] for f in files_with_messages]
            logger.info("✅ New files added to branch successfully")

            # Step 4: Create Pull Request
            logger.info("=" * 60)
            logger.info("STEP 4: Creating Pull Request")
            logger.info("=" * 60)

            result["stage"] = "pull_request_creation"

            # ✅ UPDATED: Enhanced PR title to reflect versioning
            if not pr_title:
                if versioning_method == "original":
                    pr_title = f"Add new files via {actual_branch_name}"
                else:
                    pr_title = f"Add new files via {actual_branch_name} (auto-versioned)"

            # ✅ UPDATED: Enhanced PR body with versioning info
            if not pr_body:
                pr_body = f"This PR adds new files to the repository.\n\n"
                pr_body += f"## 📁 Files Added:\n"
                for file_info in new_files:
                    pr_body += f"- `{file_info['path']}`\n"

                pr_body += f"\n## 🌿 Branch Information:\n"
                pr_body += f"- **Source**: {base_branch}\n"
                pr_body += f"- **Target**: {actual_branch_name}\n"
                pr_body += f"- **Method**: {versioning_method}\n"

                if versioning_method == "smart_versioning":
                    pr_body += f"- **Version**: v{branch_version}\n"
                    pr_body += f"- **Note**: Original branch '{new_branch_name}' existed\n"
                    if highest_existing > 0:
                        pr_body += f"- **Skipped**: v1-v{highest_existing} (already exist)\n"

            # Use actual_branch_name for head
            pr_result = self.pr_manager.create_basic_pr(
                project_name=f"{repo_owner}/{repo_name}",
                head=actual_branch_name,
                base=base_branch,
                authorization=self.token,
                title=pr_title,
                body=pr_body
            )

            if not pr_result:
                result["error"] = f"Failed to create Pull Request from '{actual_branch_name}' to '{base_branch}'"
                result["details"].update({
                    "head_branch": actual_branch_name,
                    "base_branch": base_branch,
                    "pr_title": pr_title
                })
                return result

            result["completed_steps"].append("pull_request_creation")
            result["details"].update({
                "pr_url": pr_result.get('html_url'),
                "pr_number": pr_result.get('number'),
                "final_branch_name": actual_branch_name
            })
            logger.info("✅ Pull Request created successfully")

            # ✅ UPDATED: Enhanced success summary with versioning details
            logger.info("🎉 WORKFLOW COMPLETED SUCCESSFULLY!")
            logger.info(f"✅ Repository: {repo_details.get('html_url')}")
            logger.info(f"✅ Final Branch: {actual_branch_name}")
            logger.info(f"✅ Versioning Method: {versioning_method}")
            if versioning_method == "smart_versioning":
                logger.info(f"✅ Version Created: v{branch_version}")
                logger.info(f"✅ Existing Versions Skipped: v1-v{highest_existing}")
            logger.info(f"✅ Files Added: {len(new_files)}")
            logger.info(f"✅ Pull Request: {pr_result.get('html_url')}")

            # Success!
            result["success"] = True
            result["stage"] = "completed"
            result["error"] = None

            return result

        except Exception as e:
            result["error"] = f"Unexpected error during {result['stage']}: {str(e)}"
            result["details"]["exception_type"] = type(e).__name__
            logger.error(f"❌ Workflow failed with error: {e}")
            return result


def main(jira_id, github_token, repo_name, target_module, new_branch_name, base_branch,
         jenkins_project_name, jenkins_username, jenkins_api_token, feature_file_content,
         step_definition_content, test_tag, base_path: str = "src/tests", is_non_bdd: bool = False):
    """
    Execute complete workflow and return detailed result with smart versioning support
    """

    # ✅ UPDATED: Enhanced workflow result structure
    workflow_result = {
        "success": False,
        "github_workflow": None,
        "jenkins_trigger": None,
        "overall_error": None,
        "final_branch_name": None,
        "branch_versioning_info": {}  # ✅ NEW: Add versioning info
    }

    try:
        GITHUB_TOKEN = github_token
        REPO_OWNER = "Chewy-Inc"
        REPO_NAME = repo_name
        NEW_BRANCH_NAME = new_branch_name

        # Normalize base_path (repo-relative, no leading/trailing slashes)
        norm_base_path = (base_path or "src/tests").strip().strip("/")

        # Filter out step definitions already present in the repository (BDD only)
        if not is_non_bdd:
            try:
                steps_root = f"{norm_base_path}/steps/"
                filtered_ts = filter_ts_steps_against_repo(
                    github_token,
                    REPO_OWNER,
                    REPO_NAME,
                    base_branch,
                    step_definition_content,
                    steps_root=steps_root
                )
            except Exception as _dedupe_err:
                logger.warning(f"Step de-duplication failed, proceeding with original TS content: {_dedupe_err}")
                filtered_ts = step_definition_content
        else:
            filtered_ts = step_definition_content

        # Build files list
        if is_non_bdd:
            new_test_files = [
                {
                    'path': f'{norm_base_path}/{target_module}/{jira_id}.spec.ts',
                    'content': step_definition_content,
                    'message': f'Add {jira_id} Playwright spec for {target_module} module'
                }
            ]
        else:
            new_test_files = [
                {
                    'path': f'{norm_base_path}/features/{target_module}/{jira_id}.feature',
                    'content': feature_file_content,
                    'message': f'Add {jira_id} feature file for {target_module} module'
                }
            ]
            
            has_steps = bool(re.search(r'^\s*(Given|When|Then)\s*\(', filtered_ts, re.MULTILINE))
            if has_steps:
                new_test_files.append({
                    'path': f'{norm_base_path}/steps/{target_module}/{jira_id}.ts',
                    'content': filtered_ts,
                    'message': f'Add {jira_id} step definitions for {target_module} module'
                })
            else:
                logger.info(f"All step definitions in {jira_id}.ts already exist in repo; skipping TS file creation and adding only the feature file.")
            
        # Create workflow manager
        workflow_manager = GitHubSimpleWorkflowManager(GITHUB_TOKEN)

        # Compose PR title and body dynamically
        if is_non_bdd:
            pr_title = f"Add {jira_id} {target_module.title()} Spec"
            pr_body = f"""# 🧪 New {target_module.title()} Spec - {jira_id}

## 🆕 File Added:
- `{norm_base_path}/{target_module}/{jira_id}.spec.ts`

## 🎯 Test Configuration:
- **Module**: {target_module}
- **JIRA ID**: {jira_id}
- **Test Tag**: @{test_tag}
- **Target Branch**: {base_branch}
"""
        else:
            pr_title = f"Add New {target_module.title()} Test Files - {jira_id}"
            pr_body = f"""# 🧪 New {target_module.title()} Test Implementation - {jira_id}

This PR adds comprehensive test files for the new {target_module} flow.

## 🆕 New Files Added:
- **Feature File**: `{norm_base_path}/features/{target_module}/{jira_id}.feature`
  - Contains new {target_module} flow test scenarios
  - Includes @{target_module} and @{test_tag} tags
  - JIRA: {jira_id}

- **Step Definitions**: `{norm_base_path}/steps/{target_module}/{jira_id}.ts`
  - TypeScript step definitions for new {target_module} tests
  - Includes Playwright integration
  - Comprehensive error handling

## 🎯 Test Configuration:
- **Module**: {target_module}
- **JIRA ID**: {jira_id}
- **Test Tag**: @{test_tag}
- **Target Branch**: {base_branch}

## 🚀 Next Steps:
1. Review test scenarios and step definitions
2. Validate test tag configuration  
3. Execute smoke tests via Jenkins
4. Merge after successful testing

## 📋 Checklist:
- [ ] Code review completed
- [ ] Test scenarios validated
- [ ] Step definitions tested
- [ ] Jenkins tests triggered
- [ ] Ready for {base_branch} deployment"""

        # Execute workflow and capture result
        github_result = workflow_manager.execute_simple_workflow(
            repo_owner=REPO_OWNER,
            repo_name=REPO_NAME,
            new_branch_name=NEW_BRANCH_NAME,
            new_files=new_test_files,
            pr_title=pr_title,
            pr_body=pr_body,
            base_branch=base_branch
        )

        workflow_result["github_workflow"] = github_result

        if not github_result["success"]:
            workflow_result["overall_error"] = f"GitHub workflow failed at stage: {github_result['stage']}"
            return workflow_result

        # ✅ UPDATED: Extract enhanced branch information
        actual_branch_name = github_result["details"].get("final_branch_name", new_branch_name)
        workflow_result["final_branch_name"] = actual_branch_name

        # ✅ NEW: Extract versioning information
        versioning_info = {
            "original_requested": new_branch_name,
            "final_created": actual_branch_name,
            "version": github_result["details"].get("branch_version", 0),
            "method": github_result["details"].get("versioning_method", "unknown"),
            "highest_existing": github_result["details"].get("highest_existing_version", 0)
        }
        workflow_result["branch_versioning_info"] = versioning_info

        # ✅ UPDATED: Enhanced logging with versioning details
        if actual_branch_name != new_branch_name:
            logger.info("🔄 Branch Versioning Applied:")
            logger.info(f"   📝 Requested: {new_branch_name}")
            logger.info(f"   ✅ Created: {actual_branch_name}")
            logger.info(f"   🏷️ Version: v{versioning_info['version']}")
            logger.info(f"   ⚡ Method: {versioning_info['method']}")
            if versioning_info['highest_existing'] > 0:
                logger.info(f"   ⏭️ Skipped: v1-v{versioning_info['highest_existing']}")
        else:
            logger.info(f"✅ Using original branch name: {actual_branch_name}")

        # Initialize Jenkins runner
        jenkins_runner = JenkinsTestGenieRunner(jenkins_project_name, jenkins_username, jenkins_api_token)

        logger.info("\n" + "=" * 60)
        logger.info("🧪 TRIGGERING JENKINS TESTS")
        logger.info("=" * 60)
        logger.info(f"🎯 Branch: {actual_branch_name}")
        logger.info(f"🏷️ Test Tag: @{test_tag}")
        logger.info(f"🔧 Jenkins Job: {jenkins_project_name}")

        # ✅ UPDATED: Enhanced versioning info in Jenkins trigger
        if versioning_info['method'] == 'smart_versioning':
            logger.info(f"📋 Note: Using auto-versioned branch (v{versioning_info['version']})")

        # Use actual_branch_name for Jenkins trigger
        jenkins_result = jenkins_runner.trigger_test_job(
            branch_name=actual_branch_name,
            test_tag=f"@{test_tag}"
        )

        workflow_result["jenkins_trigger"] = jenkins_result

        if jenkins_result["success"]:
            logger.info("\n🎉 COMPLETE WORKFLOW EXECUTED SUCCESSFULLY!")
            logger.info("=" * 50)
            logger.info(f"✅ Repository: {REPO_OWNER}/{REPO_NAME}")
            logger.info(f"✅ Branch: {actual_branch_name}")
            if versioning_info['method'] == 'smart_versioning':
                logger.info(f"✅ Version: v{versioning_info['version']} (auto-generated)")
            logger.info(f"✅ PR: {github_result['details'].get('pr_url', 'N/A')}")
            logger.info(f"✅ Jenkins: {jenkins_result['details'].get('queue_location', 'Triggered')}")
            workflow_result["success"] = True
        else:
            logger.error(f"❌ Jenkins pipeline creation failed: {jenkins_result.get('error', 'Unknown error')}")
            logger.error(f"Jenkins details: {jenkins_result.get('details', {})}")
            workflow_result["success"] = True  # GitHub succeeded, but Jenkins failed
            # Ensure Jenkins failure info is properly structured
            workflow_result["jenkins_trigger"]["jenkins_pipeline_status"] = "Failed"
            workflow_result["jenkins_trigger"]["jenkins_error_message"] = jenkins_result.get('error', 'Unknown Jenkins error')

        return workflow_result

    except Exception as e:
        workflow_result["overall_error"] = f"Unexpected workflow error: {str(e)}"
        workflow_result["details"] = {"exception_type": type(e).__name__}
        logger.error(f"❌ Unexpected workflow error: {e}")
        return workflow_result



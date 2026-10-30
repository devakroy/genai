import json
from dotenv import load_dotenv
import os
import boto3
from botocore.exceptions import ClientError
import logging
from utils.constants import PLATFORM_CONFIG

logger = logging.getLogger(__name__)
# Load environment variables from .env file
load_dotenv()

# Path to onboarding configuration JSON (src/onboardingConfiguration.json)
base = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(base)  # src/services
src_dir = os.path.dirname(root)  # src
REPO_MODULES_JSON_PATH = os.path.join(src_dir, "onboardingConfiguration.json")

def get_secret(selected_domain: str = None):
    """Fetch secrets from AWS Secrets Manager.

    If selected_domain is provided, read `src/onboardingConfiguration.json` to find
    the key names for that domain and return only those values.
    Returns a dict with keys: github_access_token, jenkins_username, jenkins_api_token.
    """
    secrets = {}
    logger.info("Fetching secrets from AWS Secrets Manager")
    secret_name = "chewy/genai/automation"
    
    deploy_env = PLATFORM_CONFIG.get("DEPLOY_ENV", "").lower()
    #deploy_env = "stage"
    if deploy_env == "dev":
    # Fetch credentials from environment variables
        logger.info("Running in development environment, using IAM role assumption for AWS credentials")
        role_arn  = "arn:aws:iam::123456789012:role/dev-use1-chewy-genai-automation-chewy-genai-automation-task"
        session_name = "bedrock-langchain-session"

        sts_client = boto3.client("sts", region_name="us-east-1")

        logger.info(f"Assuming role: {role_arn}")
        assumed_role = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            DurationSeconds=3600
        )

        creds = assumed_role["Credentials"]
        logger.info("Successfully assumed IAM role")                

        # Create a Secrets Manager client with explicit credentials
        session = boto3.session.Session()
        client = session.client(
            service_name='secretsmanager',
            region_name="us-east-1",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"]
        )
    else:
        logger.info("Running in local environment. so, using local environment variables for AWS credentials")
        access_key = os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        session_token = os.getenv("AWS_SESSION_TOKEN")
        # Create a Secrets Manager client with explicit credentials
        session = boto3.session.Session()
        client = session.client(
            service_name='secretsmanager',
            region_name="us-east-1",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token
        )

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
        logger.info("Secret fetched successfully.")
    except ClientError as e:
        logger.error(f"Error fetching secret: {e}")
        raise e

    secret = get_secret_value_response['SecretString']
    secret_dict = json.loads(secret)

    # If a domain is specified, map the domain's configured keys to values
    if selected_domain:
        try:
            with open(REPO_MODULES_JSON_PATH, 'r', encoding='utf-8') as f:
                modules_data = json.load(f)
            domain_config = next((m for m in modules_data if isinstance(m, dict) and m.get('domain') == selected_domain), None)
        except Exception:
            domain_config = None

        github_key = domain_config.get('github_access_token') if domain_config else None
        jenkins_user_key = domain_config.get('jenkins_username') if domain_config else None
        jenkins_api_key = domain_config.get('jenkins_api_token') if domain_config else None

        return {
            "github_access_token": secret_dict.get(github_key) if github_key else None,
            "jenkins_username": secret_dict.get(jenkins_user_key) if jenkins_user_key else None,
            "jenkins_api_token": secret_dict.get(jenkins_api_key) if jenkins_api_key else None,
        }

    # Backward-compatible return using default known keys
    return {
        "github_access_token": secret_dict.get("GITHUB_PAT"),
        "jenkins_username": secret_dict.get("JENKINS_USERNAME"),
        "jenkins_api_token": secret_dict.get("JENKINS_API_TOKEN"),
    }

# if __name__ == "__main__":
#     print(json.dumps(get_secret("Payment"), indent=4))


def get_secret_raw(secret_name: str = None):
    """
    Non-breaking helper to return the full secret payload as a dict.

    - If secret_name is None, defaults to the application's primary secret
      ("chewy/genai/automation").
    - Mirrors the credential bootstrap of get_secret(), but returns
      the entire secret content without mapping/filtering.
    """
    logger.info("Fetching raw secret from AWS Secrets Manager")
    default_secret_name = "chewy/genai/automation"
    target_secret = secret_name or default_secret_name

    deploy_env = PLATFORM_CONFIG.get("DEPLOY_ENV", "").lower()

    if deploy_env == "dev":
        logger.info("Running in development environment, using IAM role assumption for AWS credentials")
        role_arn  = "arn:aws:iam::123456789012:role/dev-use1-chewy-genai-automation-chewy-genai-automation-task"
        session_name = "bedrock-langchain-session"

        sts_client = boto3.client("sts", region_name="us-east-1")

        logger.info(f"Assuming role: {role_arn}")
        assumed_role = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            DurationSeconds=3600
        )

        creds = assumed_role["Credentials"]
        logger.info("Successfully assumed IAM role")

        session = boto3.session.Session()
        client = session.client(
            service_name='secretsmanager',
            region_name="us-east-1",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"]
        )
    else:
        logger.info("Running in local environment. so, using local environment variables for AWS credentials")
        access_key = os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        session_token = os.getenv("AWS_SESSION_TOKEN")
        session = boto3.session.Session()
        client = session.client(
            service_name='secretsmanager',
            region_name="us-east-1",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token
        )

    try:
        get_secret_value_response = client.get_secret_value(SecretId=target_secret)
        logger.info("Raw secret fetched successfully.")
    except ClientError as e:
        logger.error(f"Error fetching raw secret '{target_secret}': {e}")
        raise e

    secret = get_secret_value_response.get('SecretString')
    if not secret:
        return {}
    try:
        return json.loads(secret)
    except Exception:
        # Secret is not JSON; return as a single key payload
        return {"value": secret}
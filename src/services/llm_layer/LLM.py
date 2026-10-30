
import os
import boto3
import logging
import traceback
from dotenv import load_dotenv
from botocore.config import Config
from langchain_aws import ChatBedrock, ChatBedrockConverse
from langchain_core.prompts import PromptTemplate

from utils.constants import PLATFORM_CONFIG
# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)


class LLM:

    def send_request(self, template_prompt, input_variables, input_variables_dict, temperature=None ,prompt_validation=True):
        try:
            logger.info("Starting LLM request")
            prompt = None
            if prompt_validation:
                prompt = PromptTemplate(
                    input_variables=input_variables,
                    template=template_prompt
                )
            else:
                prompt = template_prompt                

            # Set the temperature
            temp = temperature if temperature is not None else 0.25
            logger.info(f"Using temperature: {temp}")

            # Get deployment environment
            deploy_env = PLATFORM_CONFIG.get("DEPLOY_ENV", "").lower()
            region = "us-east-1"

            logger.info(f"Deploy environment: {deploy_env}")

            # Create boto3 config with timeout settings
            config = Config(
                read_timeout=120,  # 2 minutes timeout
                connect_timeout=60,
                retries={'max_attempts': 3}
            )

            if deploy_env == "dev":
                # Use IAM Role assumption for dev environment
                logger.info("Development environment detected, using IAM role assumption")

                try:
                    # Step 1: Assume IAM Role via STS
                    role_arn  = "arn:aws:iam::123456789012:role/dev-use1-chewy-genai-automation-chewy-genai-automation-task"
                    session_name = "bedrock-langchain-session"

                    sts_client = boto3.client("sts", region_name=region)

                    logger.info(f"Assuming role: {role_arn}")
                    assumed_role = sts_client.assume_role(
                        RoleArn=role_arn,
                        RoleSessionName=session_name,
                        DurationSeconds=3600
                    )

                    creds = assumed_role["Credentials"]
                    logger.info("Successfully assumed IAM role")

                    # Step 2: Create Bedrock client with temporary credentials
                    bedrock_client = boto3.client(
                        service_name="bedrock-runtime",
                        region_name=region,
                        aws_access_key_id=creds["AccessKeyId"],
                        aws_secret_access_key=creds["SecretAccessKey"],
                        aws_session_token=creds["SessionToken"],
                        config=config
                    )

                    # Step 3: Use ChatBedrock for IAM role
                    llm = ChatBedrock(
                        client=bedrock_client,
                        model_id=PLATFORM_CONFIG["BEDROCK_MODEL_ID"],
                        model_kwargs={
                            "max_tokens": 8192,
                            "temperature": temp,
                            "top_p": 0.3,
                        }
                    )
                    logger.info("ChatBedrock initialized with IAM role credentials")

                except Exception as e:
                    logger.error(f"Error with IAM role assumption: {str(e)}")
                    raise

            else:
                # Use environment variables for other environments
                logger.info("Non-dev environment, using environment variables")

                try:
                    # Create boto3 client with environment variables
                    bedrock_client = boto3.client(
                        'bedrock-runtime',
                        config=config,
                        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                        aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
                        region_name="us-east-1"
                    )

                    # Use ChatBedrockConverse for environment variables
                    llm = ChatBedrockConverse(
                        model=PLATFORM_CONFIG["BEDROCK_MODEL_ID"],
                        client=bedrock_client,
                        max_tokens=8192,
                        temperature=temp,
                        top_p=0.3,
                    )
                    logger.info("ChatBedrockConverse initialized with environment variables")

                except Exception as e:
                    logger.error(f"Error with environment variable credentials: {str(e)}")
                    raise

            # Format the prompt with the input variables
            if prompt_validation:
                formatted_prompt = prompt.format(**input_variables_dict)
            else:
                formatted_prompt = prompt
            
            logger.info(f"Formatted prompt length: {len(formatted_prompt)} characters")

            # Send the formatted prompt to the model
            logger.info("Sending request to LLM")
            output = llm.invoke(formatted_prompt)

            logger.info("Successfully received response from LLM")
            logger.info(
                f"Response length: {len(output.content) if hasattr(output, 'content') else 'Unknown'} characters")

            return output.content

        except Exception as e:
            logger.error(f"Error in send_request: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise e
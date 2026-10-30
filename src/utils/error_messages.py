"""
Human-friendly error message mappings
"""

ERROR_MESSAGES = {
    # File Processing Errors
    "FILE_NOT_FOUND": {
        "message": "We couldn't find the file you uploaded",
        "suggestion": "Please check if the file was uploaded correctly",
        "user_action": "Try uploading the file again"
    },
    "FILE_TOO_LARGE": {
        "message": "Your file is larger than we can process",
        "suggestion": "File size limit is 25MB for GitHub integration",
        "user_action": "Please reduce the file size or split it into smaller files"
    },
    "INVALID_FILE_FORMAT": {
        "message": "This file format isn't supported",
        "suggestion": "We support .feature, .json, .txt, and .csv files",
        "user_action": "Please convert your file to a supported format"
    },
    "FILE_ENCODING_ERROR": {
        "message": "We couldn't read your file properly",
        "suggestion": "The file might have encoding issues",
        "user_action": "Please save your file as UTF-8 and try again"
    },

    # GitHub Integration Errors
    "GITHUB_TOKEN_MISSING": {
        "message": "GitHub access token is required",
        "suggestion": "You need a valid GitHub personal access token for integration",
        "user_action": "Please provide your GitHub personal access token"
    },
    "GITHUB_TOKEN_INVALID": {
        "message": "Your GitHub token isn't working",
        "suggestion": "The token might be expired or doesn't have the right permissions",
        "user_action": "Please check your GitHub personal access token and permissions"
    },
    "GITHUB_REPO_NOT_FOUND": {
        "message": "We couldn't access the GitHub repository",
        "suggestion": "The repository might not exist or you don't have access",
        "user_action": "Verify the repository name and your permissions"
    },
    "GITHUB_BRANCH_EXISTS": {
        "message": "A branch with this name already exists",
        "suggestion": "We created a versioned branch instead",
        "user_action": "Check the response for the actual branch name created"
    },
    "GITHUB_RATE_LIMITED": {
        "message": "GitHub API rate limit reached",
        "suggestion": "You've made too many requests in a short time",
        "user_action": "Please wait 15 minutes and try again"
    },
    "GITHUB_WORKFLOW_FAILED": {
        "message": "GitHub integration workflow failed",
        "suggestion": "There was an error during the GitHub repository integration process",
        "user_action": "Please verify your GitHub repository settings and try again"
    },
    # Jenkins Integration Errors
    "JENKINS_AUTH_FAILED": {
        "message": "Jenkins authentication failed",
        "suggestion": "Your Jenkins credentials might be incorrect",
        "user_action": "Please check your Jenkins username and API token"
    },
    "JENKINS_JOB_NOT_FOUND": {
        "message": "Jenkins job not found",
        "suggestion": "The specified Jenkins project doesn't exist",
        "user_action": "Please verify the Jenkins project name"
    },
    "JENKINS_TRIGGER_FAILED": {
        "message": "Failed to trigger Jenkins job",
        "suggestion": "There might be an issue with the Jenkins server",
        "user_action": "Please try again or contact your DevOps team"
    },

    # Browser Automation Errors
    "ELEMENT_NOT_FOUND": {
        "message": "We couldn't find an element on the webpage",
        "suggestion": "The page might have changed or not loaded properly",
        "user_action": "Please try again or check if the webpage is working"
    },
    "PAGE_TIMEOUT": {
        "message": "The webpage took too long to respond",
        "suggestion": "This might be a temporary network issue",
        "user_action": "Please check your internet connection and try again"
    },
    "BROWSER_ERROR": {
        "message": " We encountered an issue with browser automation",
        "suggestion": "The browser might have crashed or the page changed",
        "user_action": "Please try running the automation again"
    },

    # Feature File Processing Errors
    "INVALID_TAG_FORMAT": {
        "message": "The feature file tag format is invalid",
        "suggestion": "Feature files must have a valid @TAG format",
        "user_action": "Please check your feature file tags and ensure they follow @JIRA-123 format"
    },
    "MISSING_BDD_STEPS": {
        "message": "No valid BDD steps found in the feature file",
        "suggestion": "The feature file might be empty or improperly formatted",
        "user_action": "Please check your feature file contains Given/When/Then steps"
    },
    "SCENARIO_PROCESSING_FAILED": {
        "message": "Failed to process one or more scenarios",
        "suggestion": "Some scenarios in your feature file couldn't be processed",
        "user_action": "Please check the scenario format and try again"
    },

    # LLM/AI Service Errors
    "LLM_QUOTA_EXCEEDED": {
        "message": "AI service quota exceeded",
        "suggestion": "We've reached the daily limit for AI processing",
        "user_action": "Please try again later or contact support for increased quota"
    },
    "LLM_SERVICE_TIMEOUT": {
        "message": "AI service took too long to respond",
        "suggestion": "The AI service might be busy processing other requests",
        "user_action": "Please try again in a few minutes"
    },
    "CODE_GENERATION_FAILED": {
        "message": "Failed to generate step definitions",
        "suggestion": "The AI couldn't process your feature file properly",
        "user_action": "Please check your feature file format and try again"
    },
    "FEATURE_PROCESSING_FAILED": {
        "message": "Feature file processing failed",
        "suggestion": "There was an error processing your feature file or generating the automation scripts",
        "user_action": "Please verify your feature file format and try again, or contact support if the issue persists"
    },
    # Domain/Configuration Errors
    "MISSING_DOMAIN_SELECTION": {
        "message": "Domain selection is required",
        "suggestion": "You need to select a domain for GitHub integration",
        "user_action": "Please select a domain from the available options"
    },
    "INVALID_DOMAIN_SELECTION": {
        "message": "Invalid domain selected",
        "suggestion": "The selected domain is not available",
        "user_action": "Please choose from the available domain options"
    },
    "CONFIG_FILE_MISSING": {
        "message": "Configuration file not found",
        "suggestion": "Required configuration files are missing",
        "user_action": "Please contact support to resolve this configuration issue"
    },
    "CREDENTIAL_RETRIEVAL_ERROR": {
        "message": "Unable to retrieve domain credentials",
        "suggestion": "There was an error accessing the stored credentials for this domain",
        "user_action": "Please contact support or verify the domain configuration"
    },

    # Workflow Errors
    "WORKFLOW_VIOLATION": {
        "message": " This operation violates the workflow rules",
        "suggestion": "You might be trying to perform operations in the wrong order",
        "user_action": "Please follow the correct workflow sequence"
    },
    "PROCESSING_TIMEOUT": {
        "message": "Processing took longer than expected",
        "suggestion": "Your request timed out due to complexity or system load",
        "user_action": "Try breaking your request into smaller parts or try again later"
    },

    # Default Error
    "UNKNOWN_ERROR": {
        "message": "Something unexpected happened",
        "suggestion": "An unexpected error occurred during processing",
        "user_action": "Please try again, or contact support if the problem continues"
    }
}


def get_error_message(error_code: str) -> dict:
    """Get user-friendly error message by code"""
    return ERROR_MESSAGES.get(error_code, ERROR_MESSAGES["UNKNOWN_ERROR"])
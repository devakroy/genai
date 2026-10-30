"""
Custom exception classes for beautiful error handling
"""

class BaseCustomException(Exception):
    """Base class for all custom exceptions"""
    def __init__(self, message: str, error_code: str = None, context: dict = None):
        self.message = message
        self.error_code = error_code or self.__class__.__name__
        self.context = context or {}
        super().__init__(self.message)

class UserInputError(BaseCustomException):
    """For user input validation errors (400-level)"""
    pass

class ExternalServiceError(BaseCustomException):
    """For external service failures (502/503-level)"""
    pass

class SystemError(BaseCustomException):
    """For internal system errors (500-level)"""
    pass

class FileProcessingError(BaseCustomException):
    """For file-related errors"""
    pass

class GitHubIntegrationError(BaseCustomException):
    """For GitHub API related errors"""
    pass

class BrowserAutomationError(BaseCustomException):
    """For Playwright/browser automation errors"""
    pass

class LLMServiceError(BaseCustomException):
    """For LLM/AI service related errors"""
    pass

class JenkinsIntegrationError(BaseCustomException):
    """For Jenkins API related errors"""
    pass


# LLM Layer

This layer handles interactions with Large Language Models.

## Components

### llm_routes.py
- Manages LLM API communication
- Handles request formatting
- Processes LLM responses
- Implements retry logic

### llm_bedrock_routes.py
- AWS Bedrock integration
- Model selection and configuration
- Response processing
- Error handling

### Key Features
- Multiple LLM provider support
- Response validation
- Token management
- Rate limiting
- Error recovery
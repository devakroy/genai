# `LLM.py` Documentation

The `LLM.py` module, located in `src/services/llm_layer/`, defines a class `LLM` that serves as a client for interacting with a specific Language Model hosted on AWS Bedrock. It uses the Langchain library, particularly `ChatBedrockConverse`, to facilitate these interactions.

## Class: `LLM`

This class encapsulates the logic for sending templated prompts to a pre-configured AWS Bedrock language model and retrieving its responses.

### Method: `send_request(self, template_prompt, input_variables, input_variables_dict, temperature=None)`

*   **Purpose**: To format a given prompt template with specific input values and send it to an AWS Bedrock (Claude 3.5 Sonnet) model for processing, then return the model's textual response.
*   **Parameters**:
    *   `template_prompt` (str): A string representing the prompt template. This template can contain placeholders for dynamic values, typically enclosed in curly braces (e.g., `"Summarize the following text: {document_text}"`).
    *   `input_variables` (list of str): A list of strings, where each string is the name of a placeholder variable used in the `template_prompt` (e.g., `["document_text"]`).
    *   `input_variables_dict` (dict): A dictionary that maps the placeholder names (from `input_variables`) to their actual string values that will be substituted into the template (e.g., `{"document_text": "This is the long document..."}`).
    *   `temperature` (float, optional): A float value to control the randomness of the LLM's output. If `None`, it defaults to `0.25`. Lower values (e.g., 0.0-0.3) make the output more deterministic and focused, while higher values (e.g., 0.7-1.0) make it more creative or diverse.

*   **Process**:
    1.  **Prompt Templating (Langchain `PromptTemplate`)**:
        *   A `PromptTemplate` object is instantiated using the provided `template_prompt` string and the `input_variables` list.
        *   The `prompt.format(**input_variables_dict)` method is then called to substitute the actual values from `input_variables_dict` into the template, producing a `formatted_prompt` string.
    2.  **LLM Configuration (Langchain `ChatBedrockConverse`)**:
        *   The `temperature` for the LLM call is determined (defaults to `0.25`).
        *   An instance of `ChatBedrockConverse` is configured with the following parameters:
            *   `model`: Hardcoded to `"anthropic.claude-3-5-sonnet-20240620-v1:0"`, specifying the exact version of the Anthropic Claude 3.5 Sonnet model on Bedrock.
            *   AWS Credentials (`aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`): These are retrieved from environment variables using `os.environ.get()`. Securely managing credentials via environment variables is a standard practice.
            *   `max_tokens`: Set to `8192`, defining the maximum number of tokens the model can generate in its response.
            *   `temperature`: The value determined in the previous step.
            *   `top_p`: Set to `0.3`. Top-p (nucleus) sampling is another way to control response randomness; a value of 0.3 means the model considers only the tokens comprising the top 30% probability mass.
            *   `region_name`: Hardcoded to `"us-east-1"`, specifying the AWS region where the Bedrock model is accessed.
            *   *(A `verbose=True` option is commented out, which, if enabled, would provide more detailed logging from Langchain.)*
    3.  **LLM Invocation**: 
        *   The `llm.invoke(formatted_prompt)` method is called, sending the fully prepared `formatted_prompt` string to the configured Claude 3.5 Sonnet model via AWS Bedrock.
    4.  **Response Extraction**:
        *   The method returns `output.content`, where `output` is the response object from `llm.invoke()`. The `.content` attribute typically holds the textual part of the model's generation.

*   **Error Handling**: This method does not include explicit `try-except` blocks. Exceptions related to AWS authentication, Bedrock service issues, or Langchain processing would propagate to the caller.

## Dependencies:

*   **`langchain_aws.ChatBedrockConverse`**: The core Langchain component for interacting with Bedrock models in a conversational manner.
*   **`langchain.prompts.prompt.PromptTemplate`**: For managing and formatting prompts with dynamic inputs.
*   **`os`**: For accessing environment variables (primarily for AWS credentials).
*   **Commented-out imports**: `configparser`, `langchain_aws.BedrockLLM`, `langchain_core.messages`, `openai.OpenAI` suggest exploration of other configurations or LLM providers in the past or for future use.

## Overall Purpose and Use Case:

The `LLM` class provides a simplified and reusable interface for making text-generation requests to a specific Anthropic Claude 3.5 Sonnet model hosted on AWS Bedrock. It abstracts the complexities of Langchain and AWS SDK setup for prompt-based LLM calls.

This class is a foundational element for any part of the application that requires text generation, summarization, question answering, or other NLP tasks suited to a powerful language model like Claude 3.5 Sonnet. By taking a template, variables, and data, it allows other services to easily leverage the LLM without needing to manage the direct Bedrock API interaction details themselves. 
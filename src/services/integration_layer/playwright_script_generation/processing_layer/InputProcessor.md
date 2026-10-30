# `InputProcessor.py` Documentation

The `InputProcessor.py` module is responsible for transforming raw, often human-readable, test case descriptions into a structured JSON format that can be used for automated test execution. It achieves this by leveraging a Language Model (LLM) guided by a specific prompt.

## Core Functionality: `generate(file_content)`

This is the primary function of the module.

**Inputs:**
*   `file_content`: A string containing the raw test case steps and associated test data.

**Process:**

1.  **LLM Initialization**:
    *   An instance of the `LLM` class is created from the `services.llm_layer.LLM` module. This suggests an internal service or wrapper for interacting with a foundational language model.

2.  **Prompt Engineering**:
    *   A detailed prompt template (`template_tc`) is defined. This prompt is crucial as it provides the LLM with:
        *   **Role Persona**: "As an Automation Engineer..."
        *   **Core Task**: To convert given Test Steps and Test Data into a predefined JSON structure.
        *   **JSON Output Specification**: The prompt strictly defines the expected JSON output, which includes:
            *   `app_url` (string): The primary application URL for the test. The first URL encountered in the input is designated as this.
            *   `steps` (array of objects): Each object represents a single test step and contains:
                *   `step` (string): A hyphenated "Action-Object" pair (e.g., "click-SignInButton", "populate-UsernameField"). The element name is mandatory.
                *   `test_data` (string): The specific data required for the action (e.g., text to enter, URL to navigate to). It should be an empty string if no data is applicable.
                *   `condition` (string): Any conditions or specific parameters for the step (e.g., "mobile view-430:932 resolution", "is displayed", "is enabled").

        *   **Key Behavioral Instructions for the LLM**:
            *   **URL Handling**: The first URL becomes `app_url`. Subsequent URLs are associated directly with their respective actions within the `test_data` field of a step.
            *   **Empty Test Data**: If a step requires no input data, `test_data` must be `""`.
            *   **Mobile View**: If the input mentions "mobile view" in a navigation step, a `condition` should be added (e.g., "mobile view" or "mobile view-430:932 resolution" if a specific resolution isn't given).
            *   **Data Type**: `test_data` must always be a string.
            *   **Strict JSON Output**: The LLM is instructed to *only* return the JSON response without any extraneous explanations or conversational text.
            *   **Defined Actions**: A specific list of allowed actions (`Navigate`, `Launch`, `Click`, `Uncheck`, `Enter`, `Populate`, `Upload`, `Assert_text`, `Validate`, `Verify`, `Select`, `Hover`, `Hover_and_Click`) is provided.
            *   **Element Naming**: Element names are mandatory in the `step` field.
        *   **Example**: A clear example of the expected input-to-output transformation is embedded within the prompt to guide the LLM.

3.  **LLM Invocation**:
    *   The `model.send_request()` method of the `LLM` object is called.
    *   The `template_tc` (prompt), a list of placeholder variables within the prompt (in this case, `["mtc"]`), and a dictionary mapping these placeholders to their actual values (`{"mtc": file_content}`) are passed as arguments.

**Outputs:**
*   The function returns the direct output from the LLM, which is expected to be a JSON string conforming to the structure defined in the prompt.

## Dependencies

*   **`services.llm_layer.LLM`**: This module relies on an internal LLM abstraction for its core processing logic.

## Overall Purpose and Use Case

`InputProcessor.py` serves as a critical bridge in a test automation generation pipeline. It translates potentially unstructured or natural language test case inputs into a machine-readable and executable JSON format.

**Typical Workflow:**
1.  A user or another system provides test case details (e.g., "Navigate to the login page, enter username 'testuser', enter password 'password123', and click the login button").
2.  This `file_content` is passed to the `generate()` function.
3.  The `generate()` function uses the LLM to parse this input and transform it into the structured JSON.
4.  The resulting JSON can then be consumed by a downstream module (e.g., a Playwright or Selenium script executor) that understands this specific JSON structure to perform the automated browser actions.

This approach allows for more flexible test case definition while ensuring that the automation engine receives a consistent and predictable input format. 
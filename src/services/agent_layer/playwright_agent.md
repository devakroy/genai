# `selenium_agent.py` Documentation

The `selenium_agent.py` module serves as an automated execution engine for performing sequences of actions on web pages using the Selenium WebDriver.
It takes a defined set of steps, translates them into browser interactions, and logs the details and outcomes of these actions.

## Key Components and Functionalities:

1.  **`perform(driver: WebDriver, steps: dict, inter_file, chroma_connector, session_id: str)`**
    *   **Core Execution Loop**: This is the primary function that drives the automation. It iterates through a list of `steps` (presumably a list of dictionaries, where each dictionary defines an action and its parameters).
    *   **Supported Actions**:
        *   `enter` / `populate`: Locates an element using its XPath, ensures it's interactable (scrolling if necessary), clears any existing content, and then types the provided `text` into it.
        *   `click`: Finds an element by XPath, scrolls it into the center of the view, and then attempts to click it. It has a primary method using `ActionChains` and a fallback method using a JavaScript click (`arguments[0].click();`) for robustness.
        *   `verify`: Intended for validation steps. It can check for the presence of an element, optionally identified by an XPath.
        *   `hover`: Simulates moving the mouse cursor over a target element, using `ActionChains` or a JavaScript fallback.
        *   `wait`: Introduces a pause in the execution for a specified duration using `driver.implicitly_wait()`.
        *   `switch_to_iframe`: Changes the WebDriver's focus to a specified iframe, which is located by its XPath.
        *   `select`: Designed to interact with dropdown/select elements. It clicks on a specific option, which can be identified by `option_xpath` or a general `xpath` for the select element itself.
    *   **Page Load Synchronization**: After each action, it waits for the `document.readyState` to become "complete", ensuring that the page has fully loaded before proceeding to the next step. This helps prevent errors due to interacting with an incompletely loaded page.
    *   **Error Handling**: Each step's execution is wrapped in a `try-except` block. If an exception occurs, the action is marked as not performed, a status flag is set to indicate failure, and the error is implicitly logged via `record_page_object_details` or by appending to `steps_not_performed`.
    *   **Reporting**: The function returns:
        *   `status`: An integer (1 for overall success, -1 if any step failed).
        *   `steps_performed`: A string concatenating the successfully performed actions.
        *   `steps_not_performed`: A string concatenating the actions that failed.
    *   **Interaction with `record_page_object_details`**: For most actions, this function is called to log detailed information about the step.

2.  **`record_page_object_details(page, action, obj, test_data, failed_msg, input_data, driver, check_for_visible_element, xpath, file_name, condition, condition_value)`**
    *   **Comprehensive Logging**: This function is crucial for observability and debugging. It gathers and records a wide array of data for each performed action.
    *   **Data Points Collected**: Includes the current URL, a derived page name, the type of action, an identifier for the target object/element (`obj`), any input `test_data`, failure messages, the element's XPath, and various attributes of the element (TagName, ID, Class, Name, LinkText, CSS selectors).
    *   **Conditional Logging**: The specific attributes logged can vary slightly based on the `action` type (e.g., 'navigate', 'switch to iframe', 'validate'/'verify').
    *   **Output Format**: It appears to collect data into lists (`rows`, `cols`) and likely uses the `pandas` library (imported as `pd`) to structure and possibly write this data to a file (indicated by the `file_name` parameter or `inter_file` from the `perform` function). This suggests output to formats like CSV or Excel for easy analysis.

3.  **`extract_page_name(url: str) -> str`**
    *   **URL Parsing Utility**: A helper function that takes a full URL string and attempts to extract a concise, human-readable "page name."
    *   **Logic**: It typically returns the last segment of the URL path. If the path is empty or just '/', it returns the netloc (domain name). It also handles common cleanup like removing 'www.' and file extensions (e.g., .html, .php).

4.  **Helper/Utility Functions (Inferred from Outline)**:
    *   **`locator_priority(priority_list, df, element, driver, xpath)`**: This function likely implements a strategy to determine the most reliable locator for a given web element. It might take a list of preferred locator types (e.g., ID, name, XPath, CSS selector) and the element's attributes to choose the best one, potentially using a `pandas` DataFrame (`df`) for processing.
    *   **`generate_relative_xpath(driver, element, abs_xpath)`**: This function's purpose is likely to create a relative XPath expression for a web element, given the WebDriver instance, the element itself, and possibly its absolute XPath. Relative XPaths are often more resilient to minor changes in page structure compared to absolute XPaths.

## Overall Purpose and Use Cases:

`selenium_agent.py` is a robust module for executing Selenium-based web automation tasks. It provides a structured way to define a series of interactions (clicks, typing, verification, etc.) and then carries them out in the browser. The detailed logging of each step, including element attributes and success/failure status, makes it highly valuable for:

*   **Automated Software Testing**: Running test scripts and recording results.
*   **Web Scraping**: Navigating websites and extracting data, with clear logs of the process.
*   **Robotic Process Automation (RPA)**: Automating repetitive web-based workflows.
*   **Debugging Automation Scripts**: The detailed logs help pinpoint where and why an automation script might be failing.

The use of `chroma_connector` and `session_id` in the `perform` function signature suggests potential integration with a vector database (like ChromaDB) for more advanced features, possibly related to element identification, state tracking, or learning from past interactions, though the exact nature of this integration is not fully detailed in the provided snippet. 
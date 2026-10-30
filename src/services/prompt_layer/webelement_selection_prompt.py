click_action_prompt="""
🚨🚨🚨 ULTRA-CRITICAL IFRAME ARRAY RULE 🚨🚨🚨
BEFORE READING ANYTHING ELSE, CHECK THIS:
1. Does ANY element have iframe_xpath as an ARRAY like ["path1", "path2"]?
2. If YES: Generate switch_to_iframe for EACH array item SEPARATELY
3. Array with 2 items = 2 switch_to_iframe steps REQUIRED
4. NEVER skip array elements - process ALL of them in sequence

As an Experienced Automation Engineer, you are tasked to write detailed test steps specifically for CLICK actions.
You will be provided with Web Element information and a test step that requires a click action to be performed.
## STRICT REQUIREMENTS FOR CLICK ACTION:
### CRITICAL: IFRAME ARRAY DETECTION
- **BEFORE generating any actions, check if the target element has iframe_xpath as an ARRAY**
- **IF iframe_xpath is an array with multiple values, you MUST generate switch_to_iframe for EACH value in order**
- **Example: if iframe_xpath: ["/path1", "/path2"], create TWO switch_to_iframe actions with "/path1" then "/path2"**
- **NEVER ignore array elements - each iframe in the array MUST have its own switch_to_iframe step**
### Element Selection Rules:
- IDENTIFY and SELECT the web element that needs to be clicked based on the test step
- Give priority to these clickable tagNames: button, a, input, span, div, label
- Choose ONLY ONE web element that matches the object mentioned in the 'step' field
- Select the element that EXACTLY matches the target object in the test step description
### Click Action Element Selection:
- SELECT the web element only if click action is mentioned in the test step
- Do NOT select elements that are not the target of the click action in the "step" field
- Use only the required xpath for the target clickable element
- Match elements by their "element" field text.
### Element Availability Constraint:
- **IF the required element is NOT PRESENT in the provided web elements information, return empty xpath ("")**
- **ONLY select elements that actually exist in the given context/web elements data**
- **Do NOT create or assume xpath for non-existent elements**
### Special Handling:
- If the target clickable element is inside an iframe, first select the iframe element for switch action
- **For NESTED IFRAMES**: If the element has multiple iframe_xpath values in an array, you MUST create switch_to_iframe steps for EACH iframe in sequence
- ONLY include iframe selection if iframe tag is present in the element information
- **CRITICAL**: When element has iframe_xpath as an array, switch to ALL iframes in the correct order
### IFRAME ARRAY PROCESSING LOGIC FOR CLICK ACTION:
**STEP 1: Check iframe_xpath field**
- If iframe_xpath is a STRING (single value): Generate ONE switch_to_iframe action
- If iframe_xpath is an ARRAY (multiple values): Generate switch_to_iframe action for EACH array item
**STEP 2: Array processing**
- FOR EACH iframe path in the iframe_xpath array:
  - Create a separate switch_to_iframe step with that specific path
  - Use the EXACT path from the array (do not modify)
- Process array items in order: first item first, second item second, etc.
**STEP 3: Target action**
- After ALL iframe switches, add the click action
### Output Format:
**When Element is Present:**
```json
{{
    "steps": [
        {{
            "action": "click",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_CLICK",
            "xpath": "XPATH_OF_THE_TARGET_ELEMENT"
        }}
    ],
    "stop_reason": {{
        "Do you Want next set of elements?": "no"
    }}
}}
When Element is NOT Present:
{{
    "steps": [
        {{
            "action": "click",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_CLICK",
            "xpath": ""
        }}
    ],
    "stop_reason": {{
        "Do you Want next set of elements?": "no"
    }}
}}
If IFrame Element Selection is Required (Single IFrame):
{{
    "steps": [
        {{
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "XPATH_OF_THE_IFRAME"
        }},
        {{
            "action": "click",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_CLICK",
            "xpath": "XPATH_OF_THE_TARGET_ELEMENT_OR_EMPTY_IF_NOT_PRESENT"
        }}
    ],
    "stop_reason": {{
        "Do you Want next set of elements?": "no"
    }}
}}
If NESTED IFrames are Required (Multiple iframe_xpath Array):
🔥 REAL EXAMPLE FROM YOUR DATA 🔥
Input: "iframe_xpath": ["/html/body/div/div/main/div/div/div/iframe", "/html/body/div/div/div/iframe"]
❌ WRONG (only 1 switch): {{ "action": "switch_to_iframe", "iframe_xpath": "/html/body/div/div/main/div/div/div/iframe" }}
✅ CORRECT (2 switches):
{{
    "steps": [
        {{
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "/html/body/div/div/main/div/div/div/iframe"
        }},
        {{
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "/html/body/div/div/div/iframe"
        }},
        {{
            "action": "click",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_CLICK",
            "xpath": "XPATH_OF_THE_TARGET_ELEMENT_OR_EMPTY_IF_NOT_PRESENT"
        }}
    ],
    "stop_reason": {{
        "Do you Want next set of elements?": "no"
    }}
}}
CRITICAL VALIDATION:
The 'action' field MUST be "click"
Element name must match the target object in the test step
XPath must point to the correct element that should be clicked OR be empty ("") if element not found
Do NOT include other elements unless iframe switch is required
NEVER create xpath for elements that don't exist in the provided context
Match elements based on exact text content in "element" field or tagName

🚨 FINAL IFRAME ARRAY CHECK:
- Before responding, verify: Does the target element have iframe_xpath as an array?
- If YES: Count the array items and create that many switch_to_iframe steps
- Example: ["/path1", "/path2"] = TWO switch_to_iframe steps required
- NEVER skip array elements - each one needs its own switch_to_iframe step

🔥 STEP-BY-STEP CHECKLIST BEFORE RESPONDING:
Step 1: Look at your selected element's iframe_xpath field
Step 2: Is it an array with [ ] brackets? 
Step 3: If YES, count the items inside the array
Step 4: Generate that exact number of switch_to_iframe actions
Step 5: Then add your main action (click, etc.)
✅ Two array items = Two switch_to_iframe steps MANDATORY

🚨 CRITICAL OUTPUT REQUIREMENT:
- Respond ONLY with valid JSON - NO explanatory text
- Do NOT include "```json" code blocks
- Start directly with {{ and end with }}
- Follow the exact format shown in examples above

Test Step Description: {test_step} 
Web Elements Information: {context}
Analyze the test step and SELECT the correct web element that needs to be clicked from the provided web elements. Match by element text specified in the test step. If the element is not present, return empty xpath. Return ONLY the JSON response with the click action step.
"""


populate_action_prompt="""

As an Experienced Automation Engineer, you are tasked to write detailed test steps specifically for ENTER/POPULATE actions.
You will be provided with Web Element information and a test step that requires entering/populating text into an input field.
## STRICT REQUIREMENTS FOR ENTER/POPULATE ACTION:
### CRITICAL: IFRAME ARRAY DETECTION
- **BEFORE generating any actions, check if the target element has iframe_xpath as an ARRAY**
- **IF iframe_xpath is an array with multiple values, you MUST generate switch_to_iframe for EACH value in order**
- **Example: if iframe_xpath: ["/path1", "/path2"], create TWO switch_to_iframe actions with "/path1" then "/path2"**
- **NEVER ignore array elements - each iframe in the array MUST have its own switch_to_iframe step**
### Element Selection Rules:
- IDENTIFY and SELECT the web element that needs text to be entered based on the test step
- Give priority to these input tagNames: input, textarea, select
- Choose ONLY ONE web element that matches the object mentioned in the 'step' field
- Select the element that EXACTLY matches the target input field in the test step description
### Enter/Populate Action Element Selection:
- Match elements by their "element" field text.
- SELECT the web element only if enter/populate/type action is mentioned in the test step
- Do NOT select elements that are not the target of the text input action in the "step" field
- Use only the required xpath for the target input element
- Ensure the selected element can accept text input (input, textarea elements)
### Text Data Handling:
- Extract the text to be entered from the "test_data" field in the test step
### Element Availability Constraint:
- **IF the required input element is NOT PRESENT in the provided web elements information, return empty xpath ("")**
- **ONLY select elements that actually exist in the given context/web elements data**
- **Do NOT create or assume xpath for non-existent elements**
### Special Handling:
- If the target input element is inside an iframe, first select the iframe element for switch action
- **For NESTED IFRAMES**: If the element has multiple iframe_xpath values in an array, you MUST create switch_to_iframe steps for EACH iframe in sequence
- ONLY include iframe selection if iframe tag is present in the element information
- **CRITICAL**: When element has iframe_xpath as an array, switch to ALL iframes in the correct order
### Output Format:
**When Element is Present:**
```json
{
    "steps": [
        {
            "action": "populate",
            "element": "NAME_OF_THE_TARGET_INPUT_ELEMENT",
            "xpath": "XPATH_OF_THE_TARGET_INPUT_ELEMENT",
            "text": "TEXT_TO_BE_ENTERED_FROM_TEST_DATA"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
When Element is NOT Present:
{
    "steps": [
        {
            "action": "populate",
            "element": "NAME_OF_THE_TARGET_INPUT_ELEMENT",
            "xpath": "",
            "text": "TEXT_TO_BE_ENTERED_FROM_TEST_DATA"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
If IFrame Element Selection is Required (Single IFrame):
{
    "steps": [
        {
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "XPATH_OF_THE_IFRAME"
        },
        {
            "action": "populate",
            "element": "NAME_OF_THE_TARGET_INPUT_ELEMENT",
            "xpath": "XPATH_OF_THE_TARGET_INPUT_ELEMENT_OR_EMPTY_IF_NOT_PRESENT",
            "text": "TEXT_TO_BE_ENTERED_FROM_TEST_DATA"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
If NESTED IFrames are Required (Multiple iframe_xpath Array):
// Example input: "iframe_xpath": ["/html/body/div/iframe", "/html/body/div[2]/iframe"]
{
    "steps": [
        {
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "/html/body/div/iframe"
        },
        {
            "action": "switch_to_iframe",
            "element": "IFRAME", 
            "iframe_xpath": "/html/body/div[2]/iframe"
        },
        {
            "action": "populate",
            "element": "NAME_OF_THE_TARGET_INPUT_ELEMENT",
            "xpath": "XPATH_OF_THE_TARGET_INPUT_ELEMENT_OR_EMPTY_IF_NOT_PRESENT",
            "text": "TEXT_TO_BE_ENTERED_FROM_TEST_DATA"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
CRITICAL VALIDATION:
The 'action' field MUST be "populate"
Element name must match the target input field in the test step
XPath must point to the correct input element OR be empty ("") if element not found
Text field must contain the data from test_data field
Do NOT include other elements unless iframe switch is required
NEVER create xpath for elements that don't exist in the provided context
Match elements based on exact text content in "element" field.
Ensure selected element can accept text input
Test Step Description: {test_step} 
Web Elements Information: {context}
Analyze the test step and SELECT the correct input element that needs text to be entered from the provided web elements. Extract the text data from test_data field. Match by element text, placeholder, or tagName as specified in the test step. If the element is not present, return empty xpath. Return the JSON response with the populate action step.
"""

Select_action_prompt="""
As an Experienced Automation Engineer, you are tasked to write detailed test steps specifically for SELECT actions.
You will be provided with Web Element information and a test step that requires selecting an option from a dropdown or select element.
## STRICT REQUIREMENTS FOR SELECT ACTION:
### CRITICAL: IFRAME ARRAY DETECTION
- **BEFORE generating any actions, check if the target element has iframe_xpath as an ARRAY**
- **IF iframe_xpath is an array with multiple values, you MUST generate switch_to_iframe for EACH value in order**
- **Example: if iframe_xpath: ["/path1", "/path2"], create TWO switch_to_iframe actions with "/path1" then "/path2"**
- **NEVER ignore array elements - each iframe in the array MUST have its own switch_to_iframe step**
### Element Selection Rules:
- IDENTIFY and SELECT the web element that needs an option to be selected based on the test step
- Give priority to these selectable tagNames: select, input, option, div, span, ul, li
- Choose ONLY ONE web element that matches the object mentioned in the 'step' field
- Select the element that EXACTLY matches the target dropdown/select field in the test step description
### Select Action Element Selection:
- SELECT the web element only if select action is mentioned in the test step
- Do NOT select elements that are not the target of the select action in the "step" field
- Use only the required xpath for the target select element
- Ensure the selected element supports option selection (select, dropdown elements)
- Match elements by their "element" field text or tagName as specified in the test step
### Option Data Handling:
- Extract the option to be selected from the "test_data" field in the test step
- Include the option value/text that needs to be selected in the "option" field
- Find the specific option element xpath from the provided web elements
- If no test_data is provided for option, use empty string
### Option Element Selection:
- Identify the specific option element that matches the test_data value
- Use the xpath of the option element in "option_xpath" field
- If option element is not found, return empty xpath for option_xpath
### Element Availability Constraint:
- **IF the required select element is NOT PRESENT in the provided web elements information, return empty xpath ("")**
- **IF the required option element is NOT PRESENT, return empty option_xpath ("")**
- **ONLY select elements that actually exist in the given context/web elements data**
- **Do NOT create or assume xpath for non-existent elements**
### Special Handling:
- If the target select element is inside an iframe, first select the iframe element for switch action
- **For NESTED IFRAMES**: If the element has multiple iframe_xpath values in an array, you MUST create switch_to_iframe steps for EACH iframe in sequence
- ONLY include iframe selection if iframe tag is present in the element information
- **CRITICAL**: When element has iframe_xpath as an array, switch to ALL iframes in the correct order
### Output Format:
**When Both Select Element and Option are Present:**
```json
{
    "steps": [
        {
            "action": "select",
            "element": "NAME_OF_THE_TARGET_SELECT_ELEMENT",
            "xpath": "XPATH_OF_THE_SELECT_ELEMENT",
            "option": "OPTION_TEXT_FROM_TEST_DATA",
            "option_xpath": "XPATH_OF_THE_OPTION_ELEMENT"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
When Select Element is NOT Present:
{
    "steps": [
        {
            "action": "select",
            "element": "NAME_OF_THE_TARGET_SELECT_ELEMENT",
            "xpath": "",
            "option": "OPTION_TEXT_FROM_TEST_DATA",
            "option_xpath": ""
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
When Select Element Present but Option NOT Present:
{
    "steps": [
        {
            "action": "select",
            "element": "NAME_OF_THE_TARGET_SELECT_ELEMENT",
            "xpath": "XPATH_OF_THE_SELECT_ELEMENT",
            "option": "OPTION_TEXT_FROM_TEST_DATA",
            "option_xpath": ""
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
If IFrame Element Selection is Required (Single IFrame):
{
    "steps": [
        {
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "XPATH_OF_THE_IFRAME"
        },
        {
            "action": "select",
            "element": "NAME_OF_THE_TARGET_SELECT_ELEMENT",
            "xpath": "XPATH_OF_THE_SELECT_ELEMENT_OR_EMPTY_IF_NOT_PRESENT",
            "option": "OPTION_TEXT_FROM_TEST_DATA",
            "option_xpath": "XPATH_OF_THE_OPTION_ELEMENT_OR_EMPTY_IF_NOT_PRESENT"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
If NESTED IFrames are Required (Multiple iframe_xpath Array):
// Example input: "iframe_xpath": ["/html/body/div/iframe", "/html/body/div[2]/iframe"]
{
    "steps": [
        {
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "/html/body/div/iframe"
        },
        {
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "/html/body/div[2]/iframe"
        },
        {
            "action": "select",
            "element": "NAME_OF_THE_TARGET_SELECT_ELEMENT",
            "xpath": "XPATH_OF_THE_SELECT_ELEMENT_OR_EMPTY_IF_NOT_PRESENT",
            "option": "OPTION_TEXT_FROM_TEST_DATA",
            "option_xpath": "XPATH_OF_THE_OPTION_ELEMENT_OR_EMPTY_IF_NOT_PRESENT"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
CRITICAL VALIDATION:
The 'action' field MUST be "select"
Element name must match the target select field in the test step
XPath must point to the correct select element OR be empty ("") if element not found
Option field must contain the option value from test_data field
Option_xpath must point to the specific option element OR be empty ("") if option not found
Do NOT include other elements unless iframe switch is required
NEVER create xpath for elements that don't exist in the provided context
Match elements based on exact text content in "element" field
Ensure selected element supports option selection
Test Step Description: {test_step} 
Web Elements Information: {context}
Analyze the test step and SELECT the correct dropdown/select element and its option that needs to be selected from the provided web elements. Extract the option data from test_data field. Match by element text or tagName as specified in the test step. If the select element or option is not present, return empty xpath accordingly. Return the JSON response with the select action step
"""
verify_action_prompt="""
As an Experienced Automation Engineer, you are tasked to write detailed test steps specifically for VERIFY actions.
You will be provided with Web Element information and a test step that requires verifying/validating an element.
## STRICT REQUIREMENTS FOR VERIFY ACTION:
### CRITICAL: IFRAME ARRAY DETECTION
- **BEFORE generating any actions, check if the target element has iframe_xpath as an ARRAY**
- **IF iframe_xpath is an array with multiple values, you MUST generate switch_to_iframe for EACH value in order**
- **Example: if iframe_xpath: ["/path1", "/path2"], create TWO switch_to_iframe actions with "/path1" then "/path2"**
- **NEVER ignore array elements - each iframe in the array MUST have its own switch_to_iframe step**
### Element Selection Rules:
- IDENTIFY and SELECT the web element that needs to be verified based on the test step
- Give priority to these verifiable tagNames: div, h1, h2, h3, span,input, button, a, label, strong, p
- Choose ONLY ONE web element that matches the object mentioned in the 'step' field
- Select the element that EXACTLY matches the target element in the test step description
### Verify Action Element Selection:
- SELECT the web element only if verify/validate/check action is mentioned in the test step
- Do NOT select elements that are not the target of the verification action in the "step" field
- Use only the required xpath for the target element to be verified
- Match elements by their "element" field text or tagName as specified in the test step
### Element Availability Constraint:
- **IF the required element is PRESENT in the provided web elements information, return element name and xpath**
- **IF the required element is NOT PRESENT in the provided web elements information, return element name and empty xpath ("")**
- **ONLY select elements that actually exist in the given context/web elements data**
- **Do NOT create or assume xpath for non-existent elements**
### Special Handling:
- If the target element is inside an iframe, first select the iframe element for switch action
- **For NESTED IFRAMES**: If the element has multiple iframe_xpath values in an array, you MUST create switch_to_iframe steps for EACH iframe in sequence
- ONLY include iframe selection if iframe tag is present in the element information
- **CRITICAL**: When element has iframe_xpath as an array, switch to ALL iframes in the correct order
### Output Format:
**When Element is Present:**
```json
{
    "steps": [
        {
            "action": "verify",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_VERIFY",
            "xpath": "XPATH_OF_THE_TARGET_ELEMENT",
            "condition": "EXPECTED_CONDITION_AS_PER_Test_Step_Description"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
When Element is NOT Present:
{
    "steps": [
        {
            "action": "verify",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_VERIFY",
            "xpath": "",
            "condition": "EXPECTED_CONDITION_AS_PER_Test_Step_Description"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
If IFrame Element Selection is Required (Single IFrame):
{
    "steps": [
        {
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "XPATH_OF_THE_IFRAME"
        },
        {
            "action": "verify",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_VERIFY",
            "xpath": "XPATH_OF_THE_TARGET_ELEMENT_OR_EMPTY_IF_NOT_PRESENT",
            "condition": "EXPECTED_CONDITION_AS_PER_Test_Step_Description"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
If NESTED IFrames are Required (Multiple iframe_xpath Array):
// Example input: "iframe_xpath": ["/html/body/div/iframe", "/html/body/div[2]/iframe"]
{
    "steps": [
        {
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "/html/body/div/iframe"
        },
        {
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "/html/body/div[2]/iframe"
        },
        {
            "action": "verify",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_VERIFY",
            "xpath": "XPATH_OF_THE_TARGET_ELEMENT_OR_EMPTY_IF_NOT_PRESENT",
            "condition": "EXPECTED_CONDITION_AS_PER_Test_Step_Description"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
CRITICAL VALIDATION:
The 'action' field MUST be "verify"
Element name must match the target element in the test step
XPath must point to the correct element to be verified OR be empty ("") if element not found
Condition field should contain the verification expectation from test step
Do NOT include other elements unless iframe switch is required
NEVER create xpath for elements that don't exist in the provided context
Match elements based on exact text content in "element" field
Test Step Description: {test_step} 
Web Elements Information: {context}
Analyze the test step and SELECT the correct element that needs to be verified from the provided web elements. Match by element text or tagName as specified in the test step. If the element is present, return element name and xpath. If the element is not present, return element name and empty xpath. Return the JSON response with the verify action step.
"""
hover_action_prompt="""
As an Experienced Automation Engineer, you are tasked to write detailed test steps specifically for HOVER actions.
You will be provided with Web Element information and a test step that requires hovering over an element.
## STRICT REQUIREMENTS FOR HOVER ACTION:
### Element Selection Rules:
- IDENTIFY and SELECT the web element that needs to be hovered over based on the test step
- Give priority to these hoverable tagNames: button, a, div, span, input, label, img
- Choose ONLY ONE web element that matches the object mentioned in the 'step' field
- Select the element that EXACTLY matches the target element in the test step description
### Hover Action Element Selection:
- SELECT the web element only if hover action is mentioned in the test step
- Do NOT select elements that are not the target of the hover action in the "step" field
- Use only the required xpath for the target element to be hovered
- Match elements by their "element" field text or tagName as specified in the test step
### Element Availability Constraint:
- **IF the required element is PRESENT in the provided web elements information, return element name and xpath**
- **IF the required element is NOT PRESENT in the provided web elements information, return element name and empty xpath ("")**
- **ONLY select elements that actually exist in the given context/web elements data**
- **Do NOT create or assume xpath for non-existent elements**
### Special Handling:
- If the target element is inside an iframe, first select the iframe element for switch action
- ONLY include iframe selection if iframe tag is present in the element information
### Output Format:
**When Element is Present:**
```json
{
    "steps": [
        {
            "action": "hover",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_HOVER",
            "xpath": "XPATH_OF_THE_TARGET_ELEMENT"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
When Element is NOT Present:
{
    "steps": [
        {
            "action": "hover",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_HOVER",
            "xpath": ""
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
If IFrame Element Selection is Required:
{
    "steps": [
        {
            "action": "switch_to_iframe",
            "element": "IFRAME",
            "iframe_xpath": "XPATH_OF_THE_IFRAME"
        },
        {
            "action": "hover",
            "element": "NAME_OF_THE_TARGET_ELEMENT_TO_HOVER",
            "xpath": "XPATH_OF_THE_TARGET_ELEMENT_OR_EMPTY_IF_NOT_PRESENT"
        }
    ],
    "stop_reason": {
        "Do you Want next set of elements?": "no"
    }
}
CRITICAL VALIDATION:
The 'action' field MUST be "hover"
Element name must match the target element in the test step
XPath must point to the correct element to be hovered OR be empty ("") if element not found
Do NOT include other elements unless iframe switch is required
NEVER create xpath for elements that don't exist in the provided context
Match elements based on exact text content in "element" field or tagName
Test Step Description: {test_step} Web Elements Information: {context}
Analyze the test step and SELECT the correct element that needs to be hovered over from the provided web elements. Match by element text or tagName as specified in the test step. If the element is present, return element name and xpath. If the element is not present, return element name and empty xpath. Return the JSON response with the hover action step.
"""



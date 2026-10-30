TEST_SCENARIOS_PROMPT = """

STRICTLY follow the below instructions

As a QA automation expert, your task is to create detailed test scenarios based on the provided user story, acceptance criteria, and accompanying UI images and additional data. Follow these steps:

1. Carefully analyze the provided UI images, noting all visible elements such as headers, buttons, input fields, labels, and their relative positions.
2. Create scenarios that start with the user logging into the application and include all necessary navigation steps to reach the relevant page. 
3. Follow these instructions when there are multiple visible web elements with the SAME NAME present in the specific screenshot you are working on:
   For each user action, specify the exact web element being interacted with, using the names and labels as they appear in the image. When there are multiple web elements with the same name within a single screenshot, use the 'Under' keyword to create unique identifiers by mentioning Underby elements visible in that same screenshot. The number of Underby elements mentioned may vary based on what's needed to ensure uniqueness within that specific screenshot. For example:
	'<Visible_Webelement_Object_Name>' Under <Visible_Webelement_Object_Name> Under <Webelement Object>
	'<Visible_Webelement_Object_Name>' Under '<Visible_Webelement_Object_Name>' 
   Only consider elements visible in the current screenshot when using the 'Under' keyword. Do not reference elements from other screenshots in the image when creating these identifiers.
4. After each action, detail the expected result, focusing on visible changes in the UI that match the image flow.
5. Understand the user story and all the additional details.
6. IMPORTANT: - Generate the scenarios for each mentioned acceptance criteria.
6. Understand the acceptance criterias and generate test cases for all possible combinations for both positive and negative scenarios. STRICTLY!!
   - STRICTLY!! All the Acceptance criteria are mandatory to be covered in the test cases. You can choose additional or all the possible test cases other than the provided Acceptance Criteria.
   - By looking and analyzing the confluence images and the acceptance criteria, you can identify the possible test cases for both negative and positive scenarios.
7. The test cases correspond to UI testing, so you can omit any detail/acceptance criteria that pertain to API testing. Only consider testing for those scenarios that can be validated from an end-user's point of view.
8. Each test case should describe an end-to-end flow where you start with the pre-requisite steps defined above and concatenate the generated steps that will cover the given acceptance criteria. You do not go back in a flow.
    - STRICTLY DO NOT MISS ANY STEP WHICH IS MENTIONINED IN ANY PRE-REQUISITE !!!
9. The test cases should be in JSON format, one JSON object per test case.
10. Each test case JSON needs to have the below fields:
    A. Summary - One line description of the test case and the acceptance criteria it covers.
    B. Test Steps - A list of strings with the test steps which should have the following format themselves. Strictly follow one of the formats given below:
       - 'The user <ACTION> <a suitable preposition> (the) <Visible_OBJECT>' - For click, hover etc.
       - 'The user <ACTION> <Visible_OBJECT 1> <a suitable preposition> <Visible_OBJECT 2>' - Mostly used for dropdown selection
       - 'The user <ACTION> <TEST DATA> <a suitable preposition> <Visible_OBJECT 2>' - Mostly used for entering data in text box/text field
       - 'The user verifies that <Visible_OBJECT> is <displayed/enabled/clickable>' - For assertion statements
11. Do not put any test step that is not automatable.
12. STRICTLY return the list of JSONs and nothing else.
13. Keep all the web elements that you have identified in double quotes.
14. When you identify two different web elements objects with same name, do some analysis and try to differentiate them.
"""

PROCESS_DESIGN_DOC = """

Given the below excerpts and user story with acceptance criteria:

##Excerpts starts here##
Title: {title}
Overview: {overview}
What We're Launching: {launching}
##Excerpts starts here##

## User story starts here ##
Jira Title: {jira_title}
Jira Description: {jira_description}
## User story ends here ##

Please analyze the provided excerpts and user story and summarize excerpts with following guidelines:
Review the excerpts thoroughly to identify all information that relates to:
 *The user story description
 *The acceptance criteria
 *Any explicitly mentioned constraints, blockers, or negative scenarios in the source material 
Present a numbered summary that:
 *Uses complete sentences
 *Includes only information from the excerpts that directly relates to the user story and acceptance criteria
 *Ensures no relevant details are missed
 *Maintains clear traceability to both user story and acceptance criteria
 *Incorporates any mentioned constraints or failure scenarios from the source material
Validate the summary:
 *Cross-check against user story requirements
 *Verify all acceptance criteria are covered
 *Confirm no relevant details from excerpts are missed
 *Include any explicitly stated negative scenarios
 *Don't add any irrelevant data on your own
Please provide the summary in numbered points without any additional comments, ensuring each point is clearly stated in complete sentences and maintaining traceability to original requirements


"""

VALIDATION_PROMPT = """
You are a QA automation expert tasked with validating and converting test cases into BDD scenarios using Gherkin format. You will be provided with a set of test cases in JSON format, along with user stories, acceptance criteria, and UI images.

First, validate the test cases against the following criteria:

Requirement Coverage:

Verify that each acceptance criterion is addressed by at least one test case.
Ensure all key functionalities described in the user story are covered.
UI Interaction:

Confirm that all visible UI elements from the provided images are included in appropriate test cases.
Verify that the test cases accurately represent the UI state and possible interactions shown in each image.
Ensure that the exact names and labels of UI elements as shown in the images are used in the generated test cases.
Test Case Format:

Ensure each test case is in the correct JSON format with required fields.
Verify that test steps follow the specified action formats.
Pre-requisite Steps:

Confirm each test case includes necessary setup steps.
Test Data Usage:

Verify any provided test data is correctly used in relevant test cases.
Navigation and Flow:

Check that test cases represent logical end-to-end flows.
Ensure navigation between different pages or states is correctly represented.
Element Identification:

Verify that UI elements are uniquely and clearly identified in the test steps.
Automation Feasibility:

Ensure all test steps are automatable with no manual intervention required.

After validation, for each test case:

Create a scenario using the 'Summary' field as the scenario title.
Start each scenario with the exact pre-requisite steps provided, using 'Given' for the first step.
Convert each step in the 'Test Steps' array into Gherkin format:
Use 'When' for the first action step after the pre-requisites.
Use 'Then' for the first verification step.
Maintain the exact wording of each step as provided in the original test case.
Do not add any steps that are not in the original test case.
Do not remove any steps from the original test case.
Ensure that each scenario stands alone and includes all necessary pre-requisite steps.
Start the Feature file with the Feature and rest of the scenarios below it.

GET ALL THE WEB ELEMENTS THATS BEING IDENTIFIED IN DOUBLE QUOTES.

Do not add any explanations or comments outside of the Gherkin syntax. Provide only the formatted BDD scenarios."
STRICTLY!! DONT USE 'AND' in any lines, this should be followed STRICTLY!!!

PLEASE Follow the below Example for your reference:

Feature: Website Interaction Example

  Scenario: User login process
    Given user navigates to "https://www.example.com"
    When user clicks on "Login" button
    Then user populates the "Username" as testuser
    Then user enters "Password" as Password123
    When user clicks on "Submit" button
    Then user verifies that "Welcome" message is displayed
    Then user selects "subject" as English 

"""

BDD_PLAYWRIGHT_PROMPT = """You are a specialized BDD automation engineer tasked with generating Playwright test code. You have access to the following tools:

{tools}

IMPORTANT GUIDELINES:
1. The thought process can go N number of times, until you are satisfied with the thought process.
2. Use code_vector_search to analyze source code
3. Use html_content_search to analyze runtime HTML
4. Use xpath_vector_search for precise element locations
5. Use duckduckgo_results_json for best practices
6. Use duckduckgo_results_json to validate your thought process and analysis
7. Build selectors ONLY from tool results
8. There might be duplicate selectors with the same name but different functionality, so be careful and analyse based on the xpath_vector_search and html_content_search.
9. Please dont import an libraries or files from code_vector_search tool, if required you can copy the code into the final code. You can use only the selectors, html contentes to implement the code.

Analysis should cover:
1. Source Code Analysis:
   - Component implementations
   - Data-test-id patterns
   - Component hierarchy
   - Routing logic
   - State management

2. HTML & Runtime Analysis:
   - DOM structure
   - Dynamic attributes
   - Component rendering
   - Form implementations
   - Navigation elements

3. Selector Strategy:
   - Prioritize data-test-id
   - Use XPath when needed
   - Validate against HTML
   - Consider visibility states
   - Use specific attributes

4. Best Practices:
   - Selector strategies
   - Waiting patterns
   - Error handling
   - Page objects
   - Test stability

The final code must:
- Use TypeScript and Cucumber
- Only use XPATH selectors dont use playwright selectors.
- Include complete step definitions
- Use proper selectors
- Handle all scenarios
- Include proper email/password
- Be production-ready
- Include the step definition code into a single file. 
  - No multi file structure, everything in one file.
- No other information or explanation, just the code.
- No BDD scenarios or test cases, just the code.
- Put !! in front of nullable variables to avoid null errors in TypeScript
- Remove duplicate step-definitions
- Remove {{ page }} from async function parameters
- Include common browser setup/teardown code

Use the following format:

Question: the BDD scenario you must implement
Thought: you should always think about what needs to be analyzed or implemented
Action: Validate the thought process with the tools you have, should be one of [{tool_names}]
Action Input: the input to the action. If you get no relevant results, you should change the action input and try again.
Observation: the result of the action

Action: the action to take, should be one of [{tool_names}]
Action Input: the input to the action. If you get no relevant results, you should change the action input and try again.
Observation: the result of the action

... (this Thought/Action/Action Input/Observation can repeat N times)
Thought: I now know how to implement this scenario, but it should not use this.page only use the full XPATHS
Final Answer: 

// Step Definition File (example.ts)
import {{ setDefaultTimeout, After, Before, Given, When, Then }} from '@cucumber/cucumber';
import {{ expect }} from '@playwright/test';
import {{ Page, Browser, chromium }} from '@playwright/test';
import {{ BrowserContext }} from 'playwright-core';
import {{ CustomWorld }} from "../support/world";

// Common browser setup code
setDefaultTimeout(10000);

// Page Object and step definitions implementation
Given('step pattern', async function(this: CustomWorld) {
    const page = this.page!;
    // Implementation
});

When('step pattern', async function(this: CustomWorld, param1: string) {
    const page = this.page!;
    // Implementation
});

Then('step pattern', async function(this: CustomWorld, param1: string, param2: string) {
    const page = this.page!;
    // Implementation
});
// ...

// Feature File (example.feature)
/*
Feature: Feature name

  Scenario: Scenario name
    Given step pattern
    When step pattern
    Then step pattern
*/

Begin!

Question: {input}
Thought:{agent_scratchpad}
"""

STEPS_DETECTION_PROMPT = """
🚨🚨🚨 ULTRA-CRITICAL IFRAME ARRAY RULE 🚨🚨🚨
BEFORE READING ANYTHING ELSE, CHECK THIS:
1. Does ANY element have iframe_xpath as an ARRAY like ["path1", "path2"]?
2. If YES: Generate switch_to_iframe for EACH array item SEPARATELY
3. Array with 2 items = 2 switch_to_iframe steps REQUIRED
4. NEVER skip array elements - process ALL of them in sequence

As an Experienced Automation Engineer, You are tasked to write detailed test steps for any given test step.
You will be provided with the Web Element information and the test step.
Test Step Description: {test_step}

Important: When the exact match for the web element object is not available, choose the most relevant element from the provided list, keeping the element name in your output exactly as it appears in the 'element' field of the selected item.

Strictly write actions required Only for the given test step. If any non-required actions are written, the test step will not be approved.
Do Not perform steps that are not mentioned in the "step" & "test_data".
Only use the xpath which is required.
For same element you might get multiple xpath with different tag names.
Use interact able elements based on Action to be performed. Only use one element per action-object pair which are required to perform the step.
Every Test Data should be entered in the Actions.
You can perform the actions in batches, but make sure to provide the answer for `Do you Want next set of elements?` after each batch.
STRICTLY While choosing element give priority to these tagName: button, input, span, a, select, textarea, label, div, option, summary, details.

🔥 CRITICAL IFRAME ARRAY PROCESSING:
For iframe element: 
- If element has iframe_xpath as a STRING: Generate ONE switch_to_iframe action
- If element has iframe_xpath as an ARRAY: Generate switch_to_iframe action for EACH array item
- EXAMPLE: iframe_xpath ["/path1", "/path2"] = TWO separate switch_to_iframe steps
- Process array items in order, then perform the actual action
- NEVER skip array elements - ALL must be processed

Use only required elements based on test_step no additional is required.

** How to define answer for `Do you Want next set of elements?` **
- Answer with `yes`, IF YOU WANT TO PERFORM THE NEXT SET OF ACTIONS.
- Answer with `no`, IF THE TEST STEP IS ACHIEVED WITH THE GIVEN ELEMENTS.

To answer `Do you Want next set of elements?`, please refer "How to define answer for `Do you Want next set of elements?`" section.


You are expected to write detailed test steps in the below given JSON format:
{{
    "steps": [
                {{
                    "action": "switch_to_iframe",
                    "element": "IFRAME",
                    "iframe_xpath": "XPATH_OF_THE_IFRAME"
                }},
                {{
                    "action": "populate",
                    "element": "NAME_OF_THE_ELEMENT",
                    "xpath": "XPATH_OF_THE_ELEMENT",
                    "text": "TEXT_TO_BE_ENTERED"
                }},
                {{
                    "action": "click",
                    "element": "NAME_OF_THE_ELEMENT",
                    "xpath": "XPATH_OF_THE_ELEMENT",
                }},
                {{
                    "action": "verify",
                    "element": "NAME_OF_THE_ELEMENT",
                    "xpath": "XPATH_OF_THE_ELEMENT",
                    "condition": "EXPECTED_CONDITION_AS_PER_Test_Step_Description"
                }},
                {{
                    "action": "wait",
                    "element": "None"
                    "time": "TIME_IN_SECONDS"

                }},
                {{
                    "action": "select",
                    "element": "NAME_OF_THE_ELEMENT",
                    "option": "OPTION_TO_BE_SELECTED",
                    "xpath": "XPATH_OF_THE_OPTION",
                    "option_xpath":"XPATH_OF_OPTION"
                }},
                {{
                    "action": "hover",
                    "element": "NAME_OF_THE_ELEMENT",
                    "xpath": "XPATH_OF_THE_ELEMENT",
                }}


            ],

        "stop_reason": {{
            "Do you Want next set of elements?": "YES_OR_NO"
        }}
    }}

🎯 NESTED IFRAME EXAMPLE (When iframe_xpath is an array):
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
            "element": "TARGET_ELEMENT",
            "xpath": "TARGET_XPATH"
        }}
    ],
    "stop_reason": {{
        "Do you Want next set of elements?": "no"
    }}
}}


Above test step is in form of action-web element object. so find xpath based on web element object name don't choose xpath based on it's meaning because that is web element name which is on UI.
Strictly choose the element which exactly matches the object provided in the 'step' field of the `Test Step Description`.
For `select` action choose element based on `step` field and `test_data`.
Crucially, ensure the 'action' field in your output always matches the action specified in the `step` field of the `Test Step Description`. Do not deviate from this instruction.
STRICTLY !! If iframe tag present in selected element from list then only you have pick 'switch to iframe' action 
The 'switch_to_iframe' action should only be included when there's an element in the provided list that has both:
   - "tagName" that includes "iframe"
   - An "iframe_xpath" property
STRICTLY!! Understand Matching Element has TagName: Iframe -> `tag` present then  only you have to pick iframe action.
Important: Return only json format output enclosed property name in double quotes without any additional comments
Answer using the information provided below:
{context}
"""
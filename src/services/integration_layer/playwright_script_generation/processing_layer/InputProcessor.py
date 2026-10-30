
from services.llm_layer.LLM import LLM


def generate(file_content):
    model = LLM()
    template_tc = """
             As an Automation Engineer, you are tasked to write test steps in a JSON format.
        You will be given the Test Steps and the Test Data. Identify Test Data required for each Test Step and map them.
        Important:
        1. Extract first URL from the Test Data, write it with app_url and include it with the test data.
        2. If multiple URLs are present, designate the first URL as 'app_url'. For any additional URLs, associate them with their corresponding actions and include them in the test data.
        3. If a test step does not contain a data value, set the "test_data" field as an empty string.
        4. If the input scenario contains keywords 'mobile view' in navigate action, then add with 'mobile view' and with resolution in the condition step. If no resolution mentioned then take 430:932 resolution.
        4. test_data must always contain string.
        5. Only return the response in the specified JSON format. Do not give any additional information.
        6. Steps Actions can be Navigate , Launch , Click , Uncheck , Enter , Populate , Upload ,Assert_text , Validate , Verify ,Select , Hover , Hover_and_Click
        7. Always give your answer as Action-Object pair in "step" without any additional word. Include test data in "test_data" if required.
        8. STRICTLY ALWAYS include element name and have proper test step with test data. As it is mandatory.
        9. If input scenario step is like "Create New User" please return step which is given in example.
        10. For gift card steps like "Adds Giftcard with $20" or "Add Giftcard with $100" or " return step which is given in example.
        11. For standard Gherkin API patterns, convert as follows:
            GET API patterns:
            - "I make a GET request to" or "I send a GET request to" or "I call GET" or "I request GET" -> "GET-API" with URL in test_data and auth/headers in condition
            - "I send GET request with parameters" or "with query parameters" -> "GET-API" with URL and parameters in test_data
            POST API patterns:
            - "user invokes" or "user invokes [service] service with" -> "POST-API" with service name in test_data and config file in condition
            - "I make a POST request to" or "I send a POST request to" or "I call POST" or "I post to" or "user posts" -> "POST-API" with URL in test_data and request body/headers in condition
            - "I send POST request with body" or "with request body" -> "POST-API" with URL and body in test_data
            Header patterns:
            - "user add Header" or "user adds Header" -> "ADD-HEADER" with header value in test_data and service name in condition
            Request Body patterns:
            - "user add requestbody as" or "user adds requestbody as" -> "ADD-REQUESTBODY" with body content in test_data and service name in condition
            Authentication patterns:
            - "user creates new user account with sessionResponse as cookie" -> "CREATE-USER-COOKIE" with empty test_data and sessionResponse in condition
            - "user logs in with [filename].json as cookie" -> "LOAD-COOKIES-JSON" with filename in test_data
            - "user logs in with [responseVariable] as cookie" -> "CREATE-USER-COOKIE" with empty test_data and responseVariable in condition
            Validation patterns:
            - "the response status should be" or "status code should be" -> "VALIDATE-STATUS" with status code in test_data
            - "the response should contain" or "response contains" -> "VALIDATE-CONTENT" with expected text in test_data
            - "response time should be" or "response within" -> "VALIDATE-TIME" with time limit in test_data
            - "response header" or "header should be" -> "VALIDATE-HEADER" with header name in test_data and expected value in condition
            - "response body should have" or "response has field" -> "VALIDATE-BODY" with field name in test_data and expected value in condition
            Storage patterns:
            - "user stores response as" or "stores response as" -> "STORE-RESPONSE" with variable name in test_data
            - "user stores [field] from response as" or "stores [field] from response as" -> "STORE-FIELD" with field path in test_data and variable name in condition
            Field Verification patterns:
            - "user verifies [variable.field] contains" or "verifies [variable.field] contains" -> "VALIDATE-FIELD" with field path in test_data and expected value in condition
            Mock API patterns:
            - "user MOCKS [element] with [file]" or "MOCKS [element] with [file]" -> "MOCK-API" with element name in test_data and mock file in condition
        Your response can only be in the following JSON format:
        {{
        "app_url": "<URL>",
        "steps": [
        {{
        "step": "<TEST STEP ACTION 1>",
        "test_data": "<TEST_DATA FOR ACTION 1>",
        "condition": "<CONDITION IF REQUIRED>"
        }},
        {{
        "step": "<TEST STEP ACTION 2>",
        "test_data": "<TEST_DATA FOR ACTION 2>",
        "condition": "<CONDITION IF REQUIRED>"
        }},
        {{
        ...
        }}
        ]
        }}
        For Example:
         {{
        "app_url": "<URL>",
        "steps": [
        {{
        "step": "<create-new user>",
        "test_data": "",
        "condition": ""
        }},
        {{
        "step": "CREATE-USER-COOKIE",
        "test_data": "",
        "condition": "sessionResponse"
        }},
        {{
        "step": "add-Giftcard",
        "test_data": "$20",
        "condition": ""
        }},
        {{
        "step": "<navigate>",
        "test_data": "https:www.example.com",
        "condition": "mobile view-430:932 resolution"
        }},
        {{
        "step": "<click-Sign In>",
        "test_data": ""
        "condition": ""
        }},
        {{
        "step": "populate-Username",
        "test_data": "xyz@abc.com"
        "condition": ""
        }},
        {{
        "step": "verify-Sign In",
        "test_data": "<ALWAYS_EMPTY_FOR_VERIFY_ACTION>"
        "condition": "is displayed"
        }}
        {{
        "step": "select-Product",
        "test_data": "Toys",
        "condition": ""
        }},
        {{
        "step": "GET-API",
        "test_data": "https://www.example.com",
        "condition": "auth_token=bearer_token_123|Accept-Encoding=gzip,deflate|User-Agent=Mozilla/5.0|expected_status=200"
        }},
        {{
        "step": "GET-API",
        "test_data": "https://www.example.com",
        "condition": "auth_token=bearer_token_123|Accept-Encoding=gzip,deflate|User-Agent=Mozilla/5.0|expected_status=200"
        }},
        {{
        "step": "POST-API",
        "test_data": "service_name",
        "condition": "config_file.json"
        }},
        {{
        "step": "ADD-HEADER",
        "test_data": "header_value",
        "condition": "service_name"
        }},
        {{
        "step": "ADD-REQUESTBODY",
        "test_data": "body_content",
        "condition": "service_name"
        }},
        {{
        "step": "VALIDATE-STATUS",
        "test_data": "200",
        "condition": ""
        }},
        {{
        "step": "VALIDATE-CONTENT",
        "test_data": "userId",
        "condition": ""
        }},
        {{
        "step": "VALIDATE-TIME",
        "test_data": "5000",
        "condition": "milliseconds"
        }},
        {{
        "step": "VALIDATE-HEADER",
        "test_data": "Content-Type",
        "condition": "application/json"
        }},
        {{
        "step": "VALIDATE-BODY",
        "test_data": "login",
        "condition": "octocat"
        }},
        {{
        "step": "STORE-RESPONSE",
        "test_data": "responseVariableName",
        "condition": ""
        }},
        {{
        "step": "STORE-FIELD",
        "test_data": "field_name",
        "condition": "tokenVariableName"
        }},
        {{
        "step": "VALIDATE-FIELD",
        "test_data": "responsevariable.fieldName",
        "condition": "expectedvalue"
        }},
        {{
        "step": "CREATE-USER-COOKIE",
        "test_data": "",
        "condition": "sessionResponse"
        }},
        {{
        "step": "MOCK-API",
        "test_data": "Add Card",
        "condition": "mock_add_card.json"
        }},
        {{
        "step": "LOAD-COOKIES-JSON",
        "test_data": "merchOps.json",
        "condition": ""
        }}
        ]
        }}
        
        STRICTLY Stick to above format ONLY.
        The Test Steps and Test Data you need to use is {mtc}
        STRICTLY ALWAYS include element name and have proper test step with test data. As it is mandatory.
        Don't use your own Intelligence.
        QUICKLY RETURN THE JSON OUTPUT
    """


    output = model.send_request(template_tc, ["mtc"],
                                {"mtc": file_content})
    return output
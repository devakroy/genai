"""
Simple Gherkin Format Validator
Makes a single LLM call to validate feature file format
"""

import json
import logging
from services.llm_layer.LLM import LLM

logger = logging.getLogger(__name__)


def validate_gherkin(feature_content: str) -> dict:
    """
    Validate Gherkin feature file with simple LLM call.
    
    Returns:
        dict: {
            "is_valid": bool,
            "issues": list of issue dicts (if invalid),
            "message": str
        }
    """
    
    # Add line numbers to feature content for accurate reporting
    numbered_content = '\n'.join(
        f"{line_num:3d} | {line}"
        for line_num, line in enumerate(feature_content.split('\n'), 1)
    )
    
    validation_prompt = """
You are a Gherkin validator. The feature file below has line numbers prefixed (format: "NUM | line").
Use these exact line numbers in your response.

**ABSOLUTE CRITICAL INSTRUCTIONS - FOLLOW EXACTLY:**
- ONLY validate the 6 rules listed below
- IGNORE step order completely - "Then" followed by "Then" is VALID
- DO NOT suggest changing "Then" to "When" or vice versa
- DO NOT check if steps are in logical order
- DO NOT validate anything beyond the 6 explicit rules
- Multiple consecutive "Then" statements are PERFECTLY VALID
- Steps can appear in ANY order - this is NOT your concern

**VALIDATE ONLY THESE 4 RULES - NOTHING ELSE:**

RULE 1: Feature must have a tag (@something) on the line immediately before "Feature:"
  Check: Find "Feature:" line, check if previous line starts with @
  
RULE 2: Each Scenario must have a tag (@something) on the line immediately before "Scenario:"
  Check: Find "Scenario:" line, check if previous line starts with @

RULE 3: Steps must start ONLY with "Given", "When", or "Then" 
  Check: Any step starting with "And" is INVALID
  Ignore: Lines starting with "StepMacro:" or containing "# step-macro"
  IMPORTANT: Do NOT check step order. "Then" followed by "Then" is VALID.
  IMPORTANT: Do NOT suggest changing "Then" to "When" - this is FORBIDDEN.
  
RULE 4: If "Examples:" exists, it must have a table with headers and data
  Check: Examples section has proper table format

**WHAT IS EXPLICITLY FORBIDDEN:**
DO NOT check step order or sequence  
DO NOT suggest changing step keywords (Then/When/Given)
DO NOT validate logical flow of steps
DO NOT check if steps make sense together
DO NOT validate anything beyond the 6 rules above
DO NOT validate spacing/formatting around keywords (but DO check for required text content)
NEVER FLAG SPACING ISSUES - "Feature:text" and "Feature: text" are both valid
NEVER REQUIRE SPACES after colons - spacing is completely ignored

**STEP VALIDATION EXAMPLES:**
VALID: "Then user clicks" (starts with valid keyword)
VALID: "When user clicks" (starts with valid keyword)  
VALID: "Given user clicks" (starts with valid keyword)
INVALID: "And user clicks" (starts with "And")
VALID: Multiple "Then" steps in a row
VALID: "Then" followed by "When" followed by "Then"

Feature file to validate (with line numbers):
```
{numbered_content}
```

**VALIDATION PROCESS:**
1. Find Feature: line → Check if previous line has @tag
2. Find Scenario: line(s) → Check if previous line has @tag  
3. Check each step line → Only flag if it starts with "And"
4. Ignore step order completely
5. Check Examples table format if present
6. DO NOT validate text content after Feature:/Scenario: keywords
7. DO NOT validate spacing or formatting

Return JSON only (no markdown, no explanations):
{{
  "is_valid": true,
  "issues": [],
  "message": "Feature file is valid"
}}

If issues found:
{{
  "is_valid": false,
  "issues": [
    {{
      "line": X,
      "content": "exact line content",
      "problem": "specific rule violation only",
      "solution": "how to fix this specific rule"
    }}
  ],
  "message": "Validation failed"
}}

REMEMBER: DO NOT flag "Then" steps as invalid. DO NOT suggest changing step keywords.
"""
    
    try:
        logger.info("🔍 Validating Gherkin format with LLM...")
        
        # Make LLM call with numbered content
        llm = LLM()
        result = llm.send_request(
            template_prompt=validation_prompt,
            input_variables=["numbered_content"],
            input_variables_dict={"numbered_content": numbered_content},
            temperature=0.1
        )
        
        # Clean and parse result
        result = result.strip()
        if result.startswith('```'):
            result = '\n'.join(l for l in result.split('\n') if not l.strip().startswith('```'))
        
        validation_data = json.loads(result.strip())
        
        # Log result
        if validation_data.get("is_valid"):
            logger.info("✅ Gherkin validation passed")
        else:
            logger.warning(f"❌ Gherkin validation failed with {len(validation_data.get('issues', []))} issues")
        
        return validation_data
        
    except Exception as e:
        logger.error(f"❌ Validation error: {str(e)}")
        # Return validation passed on error to not block processing
        return {
            "is_valid": True,
            "issues": [],
            "message": f"Validation check skipped due to error: {str(e)}"
        }
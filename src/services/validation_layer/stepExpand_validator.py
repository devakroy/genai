import logging
import re
import json
from typing import List, Dict, Any
from dataclasses import dataclass
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, DatabaseError
from utils.db_utils import BASE_DATABASE_URL, PGSCHEMA, _get_engine
from ..integration_layer.mcp_integration.mcp_step_verifier import MCPStepVerifier
from services.llm_layer.LLM import LLM

logger = logging.getLogger(__name__)

class InstructionExpander:
    def __init__(self):
        self.action_patterns = self._load_action_patterns()

    def _load_action_patterns(self) -> Dict[str, List[str]]:
        """Load action synonyms and patterns"""
        return {
            "navigate": ["goes to", "navigates to", "visits", "opens"],
            "click": ["clicks", "selects", "chooses", "taps"],
            "enter": ["enters", "types", "inputs", "fills"],
            "verify": ["verifies", "checks", "confirms", "validates"],
            "wait": ["waits", "pauses", "delays"]
        }
    def _common_step_handler(self, summary: str, module_name:str) -> str:
        logger.info("Handling common steps in summary...")
        keylines = []
        lines = summary.split('\n')
        for line in lines:
            stripped_line = line.strip()
            pattern = r'#\s*step-macro:\s*(.*?)\.'
            matches = re.findall(pattern, line)
            for match in matches:
                cleaned_text = match.strip()
                if cleaned_text:
                    keylines.append(cleaned_text)
        if keylines:
            try:
                engine = _get_engine(BASE_DATABASE_URL)
            except (OperationalError, DatabaseError) as e:
                logger.error(f"Database engine initialization failed: {e}", exc_info=True)
                raise

            try:
                with engine.begin() as conn:
                    query_params = {"module_name": module_name}
                    keyline_placeholders = []
                    for i, keyline in enumerate(keylines):
                        param_name = f"keyline_{i}"
                        keyline_placeholders.append(f":{param_name}")
                        query_params[param_name] = keyline               
                    cmnstep_query = text(f"""
                        SELECT "keyStep", "cmnSteps"
                        FROM {PGSCHEMA}.cmn_stps
                        WHERE LOWER(TRIM("module")) = LOWER(TRIM(:module_name))
                        AND LOWER(TRIM("keyStep")) IN ({','.join(keyline_placeholders)})
                        AND "cmnSteps" IS NOT NULL
                        ORDER BY "keyStep"
                    """)              
                    result = conn.execute(cmnstep_query, query_params) 

                    for row in result:
                        keyline = row[0]
                        cmnstep_data = row[1]                  
                        try:
                            if isinstance(cmnstep_data, list):
                                step_list = [f"* {str(step).strip()}" for step in cmnstep_data if step]
                            else:
                                if cmnstep_data.startswith('{') or cmnstep_data.startswith('['):
                                    steps_data = json.loads(cmnstep_data)
                                    if isinstance(steps_data, dict):
                                        step_list = [f"* {str(value).strip()}" for value in steps_data.values() if value]
                                    elif isinstance(steps_data, list):
                                        step_list = [f"* {str(step).strip()}" for step in steps_data if step]
                                    else:
                                        step_list = [f"* {str(steps_data)}"]
                                else:
                                    step_list = [f"* {cmnstep_data.strip()}"]
                            
                            step_list = [step for step in step_list if step.strip()]
                            
                            if step_list:
                                expanded_steps = '\n'.join(step_list)
                                summary = summary.replace("step-macro:", "")
                                summary = summary.replace("step-macro :", "")
                                summary = summary.replace("#", "")
                                summary = summary.replace(keyline, expanded_steps)
                                logger.info(f"Replaced keyline '{keyline}' with {len(step_list)} expanded steps")
                
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse JSON for keyline '{keyline}': {e}")
                            if isinstance(cmnstep_data, list):
                                fallback_steps = '\n'.join([f"* {str(step).strip()}" for step in cmnstep_data if step])
                            else:
                                fallback_steps = f"* {str(cmnstep_data)}"
                            summary = summary.replace(f"# step-macro: {keyline}.", fallback_steps)
                            
                        except Exception as e:
                            logger.error(f"Error processing keyline '{keyline}': {e}")
                            if isinstance(cmnstep_data, list):
                                fallback_data = '\n'.join([f"* {str(item).strip()}" for item in cmnstep_data if item])
                            else:
                                fallback_data = f"* {str(cmnstep_data)}"
                            summary = summary.replace(f"# step-macro: {keyline}.", fallback_data)
            except (OperationalError, DatabaseError) as e:
                logger.error(f"Database error while expanding common steps: {e}", exc_info=True)
                raise
            except Exception as e:
                logger.error(f"Unexpected error while expanding common steps: {e}", exc_info=True)
                raise
        return summary

    def expand_summary(self, module: str, summary: str) -> List[str]:
        """Main method to expand a summary into detailed steps"""
        try:
            #handle common steps
            updated_summary = self._common_step_handler(summary, module)
           
            # Extract key actions and entities from summary
            entities = self._extract_entities(updated_summary)
            
            # Get sample steps from database if available
            sample_steps = self._get_sample_steps(module) 
            
            # Use LLM to generate detailed steps
            llm_response = self._generate_steps_with_llm(updated_summary, sample_steps, entities)
            
            # Validate and refine steps
            #refined_steps = self._refine_steps(llm_response)
            
            return llm_response
        
        except Exception as e:
            current_date = datetime.now().strftime("%Y-%m-%d")
            return {
                "responseId": f"RESP-{current_date}-ERROR",
                "status": "ERROR",
                "timestamp": datetime.now().isoformat() + "Z",
                "formattedWorkflow": [],
                "error": str(e)
            }
    
    def _extract_entities(self, summary: str) -> Dict[str, List[str]]:
        """Extract key entities like URLs, buttons, fields, etc."""
        entities = {
            "urls": re.findall(r'https?://[^\s]+', summary),
            "buttons": re.findall(r'(?:button|btn|click)\s+([^,\s]+)', summary, re.IGNORECASE),
            "fields": re.findall(r'(?:field|input|textbox)\s+([^,\s]+)', summary, re.IGNORECASE),
            "actions": re.findall(r'(logs in|navigates|clicks|enters|selects|searches|opens)', summary, re.IGNORECASE)
        }
        return entities
    
    def _get_sample_steps(self, module_name: str) -> List[str]:
        """Fetch similar steps from database based on entities"""
        try:
            engine = _get_engine(BASE_DATABASE_URL)
        except (OperationalError, DatabaseError) as e:
            logger.error(f"Database engine initialization failed: {e}", exc_info=True)
            raise

        try:
            with engine.begin() as conn: 
                query = text(f"""
                    SELECT step
                    FROM {PGSCHEMA}.t_steps
                    WHERE LOWER(TRIM(modulenm)) = LOWER(TRIM(:module_name))
                    AND step IS NOT NULL
                    AND TRIM(step) != ''
                    ORDER BY step
                """)
                result = conn.execute(query, {"module_name": module_name})
                # Direct list comprehension for better performance
                sample_steps = [row[0] for row in result]        
            return sample_steps
        except (OperationalError, DatabaseError) as e:
            logger.error(f"Database error while fetching sample steps: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error while fetching sample steps: {e}", exc_info=True)
            raise
        
    
    def _generate_steps_with_llm(self, summary: str, sample_steps: List[str], entities: Dict[str, List[str]]) -> Dict[str, Any]:
        """Use LLM to generate detailed steps"""
        try:
            current_date = datetime.now().strftime("%Y-%m-%d")
            current_timestamp = datetime.now().isoformat()
            cleaned_sample_steps = []
            for step in sample_steps:
                cleaned_step = step.replace('{', '{{').replace('}', '}}')
                cleaned_step = re.sub(r'(\w+)\.(\w+)(?:\.(\w+))*', lambda m: '_'.join(filter(None, m.groups())), cleaned_step)
                cleaned_sample_steps.append(cleaned_step)     

            examples_text = '\n'.join(cleaned_sample_steps) if cleaned_sample_steps else 'No examples available'
            prompt_lines = [
            "Convert this test summary into detailed step-by-step instructions and return ONLY a valid JSON response in this exact format:",
            "",
            "{{",
            "  \"responseId\": \"RESP-" + current_date + "-001\",",
            "  \"status\": \"SUCCESS\",",
            "  \"timestamp\": \"" + current_timestamp + "Z\",",
            "  \"formattedWorkflow\": [",
            "    {{",
            "      \"stepNo\": 1,",
            "      \"action\": \"Navigate\",",
            "      \"description\": \"When user navigates to URL\",",
            "      \"confidence\": \"95%\"",
            "    }},",
            "    {{",
            "      \"stepNo\": 2,",
            "      \"action\": \"Enter\",",
            "      \"description\": \"Then user enters Field Name as value\",",
            "      \"confidence\": \"90%\"",
            "    }}",
            "  ]",
            "}}",
            "",
            "Summary: " + summary,
            "",
            "Examples from similar tests:",
            examples_text,
            "",
            "Key entities found:",
            "- URLs: " + str(entities.get('urls', [])),
            "- Buttons: " + str(entities.get('buttons', [])),
            "- Fields: " + str(entities.get('fields', [])),
            "- Actions: " + str(entities.get('actions', [])),
            "",
            "Action types to use: Navigate, Click, Enter, Verify, Wait, Search",
            "",
            "IMPORTANT:",
            "- Return ONLY the JSON response, no other text",
            "- If any line/step is starting with '*' in Summary , then donot change any part of the Summary line, only remove the * from the line and generate the step with the same line content as description in the output JSON",
            "- Each step description should follow Gherkin format (When/Then user...)",
            "- Always generate step in this order - <Given/When/Then> user <action> <entity/object> (if applicable) and description/value to be used in the entity/object (if applicable), like 'Then user enters \"Password\" as \"welcome123\"'",
            "- Confidence should be realistic (40-100%) based on summary and examples",
            "- Sequential stepNo starting from 1",
            "- Preserve all variable references like {{paymentAuthToken}} exactly as they appear"
            ]
            full_prompt = '\n'.join(prompt_lines)
        
            llm = LLM()
            response =llm.send_request(
                template_prompt=full_prompt,
                input_variables=[], 
                input_variables_dict={},
                temperature=0.1,
                prompt_validation=False
            )
            response_data = json.loads(response)            
            return response_data
            
        except json.JSONDecodeError as e:
            print(f"Error parsing LLM JSON response: {e}")
            # Return error response in same format
            return {
                "responseId": f"RESP-{current_date}-ERROR",
                "status": "ERROR",
                "timestamp": datetime.now().isoformat() + "Z",
                "formattedWorkflow": [],
                "error": str(e)
         }   
    
    def _refine_steps(self, steps: List[str]) -> List[str]:
        """Refine and validate the generated steps"""
        refined_steps = []
        
        for step in steps:
            # Clean up the step
            step = step.strip()
            if not step:
                continue
                
            # Normalize step format
            step = self._normalize_step_format(step)
            
            # Validate step using MCP Step Verifier logic
            if self._is_valid_step(step):
                refined_steps.append(step)
                
        return refined_steps
    
    def _normalize_step_format(self, step: str) -> str:
        """Normalize step format to match expected pattern"""
        # Remove step numbers or bullets
        step = re.sub(r'^\d+\.\s*', '', step)
        step = re.sub(r'^[-*]\s*', '', step)
        
        # Ensure proper Gherkin format
        if not step.startswith(('Given', 'When', 'Then', 'And', 'But')):
            if 'navigate' in step.lower() or 'go to' in step.lower():
                step = f"When {step}"
            elif 'verify' in step.lower() or 'check' in step.lower():
                step = f"Then {step}"
            else:
                step = f"Then {step}"
                
        return step
    
    def _is_valid_step(self, step: str) -> bool:
        """Basic validation of step format"""
        # Check if step has proper Gherkin keyword
        gherkin_keywords = ['Given', 'When', 'Then', 'And', 'But']
        starts_with_keyword = any(step.startswith(keyword) for keyword in gherkin_keywords)
        
        # Check if step has actionable content
        has_action = any(action in step.lower() for actions in self.action_patterns.values() for action in actions)
        
        return starts_with_keyword and (has_action or 'verify' in step.lower())
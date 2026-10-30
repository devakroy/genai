import asyncio
import json
import logging
import pandas as pd
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from pathlib import Path

from .mcp_client_adapter import UniversalMCPClient
from services.llm_layer.LLM import LLM
from services.prompt_layer.step_def_template import StepDefTemplate
from services.integration_layer.playwright_script_generation.processing_layer.InputProcessor import generate
from utils.functions import setup_logging
from definitions import INTERMEDIATORY_FILES_DIR, REVIEW_PLAYWRIGHT_SCRIPTS_DIR

logger = setup_logging()

@dataclass
class MCPAction:
    """Represents an MCP action with metadata"""
    action_type: str
    element: str
    ref: str
    test_data: str
    condition: str
    timestamp: float
    success: bool
    playwright_selector: str = ""  # Best Playwright selector for the element
    error_message: Optional[str] = None
    scenario_index: Optional[int] = None  # Track which scenario this action belongs to

class MCPAutomationEngine:
    """
    MCP-based automation engine to replace Selenium WebDriver
    """
    
    def __init__(self, mcp_config=None):
        # Universal adapter auto-detects environment, no specific config needed
        self.mcp_client: Optional[UniversalMCPClient] = None
        self.llm = LLM()
        self.step_def_template = StepDefTemplate()
        self.actions_log: List[MCPAction] = []
        self.scenario_actions: Dict[int, List[MCPAction]] = {}  # Track actions by scenario
        self._setup_logging()
    
    def _setup_logging(self):
        """Setup logging for MCP automation engine"""
        self.logger = logging.getLogger(__name__)
    
    async def __aenter__(self):
        """Async context manager entry"""
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.disconnect()
    
    async def connect(self):
        """Connect to MCP server using Universal Adapter"""
        self.mcp_client = UniversalMCPClient()
        await self.mcp_client.connect()
        self.logger.info("MCP automation engine connected (universal adapter)")
    
    async def disconnect(self):
        """Disconnect from MCP server"""
        if self.mcp_client:
            await self.mcp_client.disconnect()
            self.mcp_client = None
        self.logger.info("MCP automation engine disconnected (universal adapter)")
    
    def _log_action(self, action: MCPAction):
        """Log an MCP action"""
        self.actions_log.append(action)
        
        # Track actions by scenario (if scenario_index is available)
        if hasattr(action, 'scenario_index'):
            scenario_index = action.scenario_index
            if scenario_index not in self.scenario_actions:
                self.scenario_actions[scenario_index] = []
            self.scenario_actions[scenario_index].append(action)
        
        if action.success:
            self.logger.info(f"MCP Action: {action.action_type} on {action.element}")
        else:
            self.logger.error(f"MCP Action failed: {action.action_type} on {action.element} - {action.error_message}")
    
    async def _find_element_in_snapshot(self, snapshot: str, element_description: str) -> Optional[str]:
        """
        Find element reference in accessibility snapshot using improved parsing
        """
        try:
            if not isinstance(snapshot, str):
                self.logger.warning("Snapshot is not a string, converting...")
                snapshot = str(snapshot)
            
            # Parse the YAML-like structure from MCP snapshot
            lines = snapshot.split('\n')
            elements = []
            
            # Find the Page Snapshot section
            in_snapshot_section = False
            for line in lines:
                line = line.strip()
                
                # Look for the start of the snapshot section
                if 'Page Snapshot' in line:
                    in_snapshot_section = True
                    continue
                
                # Skip if not in snapshot section
                if not in_snapshot_section:
                    continue
                
                # Skip empty lines and section headers
                if not line or line.startswith('```'):
                    continue
                
                # Parse element lines that contain refs
                if 'ref=' in line:
                    # Extract element information
                    element_info = self._parse_element_line(line)
                    if element_info:
                        elements.append(element_info)
            
            self.logger.debug(f"Found {len(elements)} elements in snapshot")
            
            # Search for the target element
            target_element = self._find_best_element_match(elements, element_description)
            
            if target_element:
                self.logger.info(f"Found element '{element_description}' -> {target_element}")
                return target_element
            else:
                self.logger.warning(f"Element '{element_description}' not found in snapshot")
                # Return first available element as fallback
                if elements:
                    fallback = elements[0]['ref']
                    self.logger.info(f"Using fallback element: {fallback}")
                    return fallback
                return None
                
        except Exception as e:
            self.logger.error(f"Error finding element in snapshot: {e}")
            return None
    
    def _parse_element_line(self, line: str) -> Optional[Dict]:
        """
        Parse a single element line from the MCP snapshot and generate Playwright selectors
        """
        try:
            # Example line: 'heading "Example Domain" [level=1] [ref=e3]'
            # or: 'link "More information..." [ref=e6] [cursor=pointer]:'
            
            # Extract ref
            import re
            ref_match = re.search(r'\[ref=([a-zA-Z0-9]+)\]', line)
            if not ref_match:
                return None
            
            ref = ref_match.group(1)
            
            # Extract element type and text
            # Remove the ref part first
            line_without_ref = re.sub(r'\[ref=[a-zA-Z0-9]+\]', '', line).strip()
            
            # Extract element type (first word)
            parts = line_without_ref.split()
            if not parts:
                return None
            
            element_type = parts[0]
            
            # Extract text content (anything in quotes)
            text_match = re.search(r'"([^"]*)"', line_without_ref)
            text_content = text_match.group(1) if text_match else ""
            
            # Extract additional attributes
            attributes = {}
            attr_matches = re.findall(r'\[([a-zA-Z0-9=]+)\]', line_without_ref)
            for attr in attr_matches:
                if '=' in attr:
                    key, value = attr.split('=', 1)
                    attributes[key] = value
                else:
                    attributes[attr] = True
            
            return {
                'ref': ref,
                'type': element_type,
                'text': text_content,
                'attributes': attributes,
                'full_line': line
            }
            
        except Exception as e:
            self.logger.debug(f"Error parsing element line '{line}': {e}")
            return None
    

    
    def _find_best_element_match(self, elements: List[Dict], target_description: str) -> Optional[str]:
        """
        Find the best matching element for the given description
        """
        if not elements:
            return None
        
        target_lower = target_description.lower()
        
        # Scoring system for element matching
        best_match = None
        best_score = 0
        
        for element in elements:
            score = 0
            
            # Check text content (highest priority)
            if element['text'] and target_lower in element['text'].lower():
                score += 100
            
            # Check element type
            if element['type'] and target_lower in element['type'].lower():
                score += 50
            
            # Check for exact text match
            if element['text'] and target_lower == element['text'].lower():
                score += 200
            
            # Check for partial text match
            if element['text']:
                text_words = element['text'].lower().split()
                target_words = target_lower.split()
                common_words = set(text_words) & set(target_words)
                if common_words:
                    score += len(common_words) * 10
            
            # Check attributes
            for attr_name, attr_value in element['attributes'].items():
                if isinstance(attr_value, str) and target_lower in attr_value.lower():
                    score += 25
            
            # Update best match
            if score > best_score:
                best_score = score
                best_match = element['ref']
        
        return best_match
    

    async def _execute_action(self, test_step: Dict) -> MCPAction:
        """
        Execute a single test step using MCP
        """
        import time
        
        step_text = test_step['step']
        test_data = test_step.get('test_data', '')
        condition = test_step.get('condition', '')
        scenario_index = test_step.get('scenario_index', None)  # Get scenario index from test step
        
        # Parse action and element from step
        if '-' in step_text:
            action, element = step_text.split('-', 1)
        else:
            action = step_text
            element = ''
        
        action = action.lower().strip()
        element = element.strip()
        
        try:
            # Handle navigation actions that don't need element detection
            if action in ['navigate', 'open', 'go to', 'launch']:
                await self.mcp_client.browser_navigate(url=test_data)
                result = MCPAction(
                    action_type="navigate",
                    element=element,
                    ref="",  # Navigation doesn't need element ref
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector="",
                    scenario_index=scenario_index
                )
                self._log_action(result)
                return result
            
            # Handle tab switching
            if 'switches to new tab' in step_text.lower():
                await self.mcp_client.browser_tabs(action="select", index=1)
                result = MCPAction(
                    action_type="switch_tab",
                    element="new tab",
                    ref="",
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector="",
                    scenario_index=scenario_index
                )
                self._log_action(result)
                return result
            
            # Handle numbered tab switching
            if 'switches to tab' in step_text.lower() and test_data.isdigit():
                tab_index = int(test_data)
                await self.mcp_client.browser_tabs(action="select", index=tab_index)
                result = MCPAction(
                    action_type="switch_tab",
                    element=f"tab {tab_index}",
                    ref="",
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector="",
                    scenario_index=scenario_index
                )
                self._log_action(result)
                return result
            
            # For other actions, get current page snapshot and find element
            snapshot = await self.mcp_client.browser_snapshot()
            
            # Find element reference and get element details
            element_ref = await self._find_element_in_snapshot(snapshot, element)
            
            if not element_ref:
                raise ValueError(f"Element '{element}' not found in page snapshot")
            
            # Generate locator using MCP - use response directly
            playwright_selector = await self.mcp_client.browser_generate_locator(
                element=element, ref=element_ref
            )
            if not playwright_selector:
                raise ValueError(f"Empty locator response for element: {element}")
            
            # Execute action based on type
            if action in ['click']:
                await self.mcp_client.browser_click(element=element, ref=element_ref)
                result = MCPAction(
                    action_type="click",
                    element=element,
                    ref=element_ref,
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector=playwright_selector,
                    scenario_index=scenario_index
                )
            
            elif action in ['type', 'enter', 'populate']:
                await self.mcp_client.browser_type(
                    element=element, 
                    ref=element_ref, 
                    text=test_data
                )
                result = MCPAction(
                    action_type="type",
                    element=element,
                    ref=element_ref,
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector=playwright_selector,
                    scenario_index=scenario_index
                )
            
            elif action in ['select']:
                await self.mcp_client.browser_select_option(
                    element=element, 
                    ref=element_ref, 
                    values=[test_data]
                )
                result = MCPAction(
                    action_type="select",
                    element=element,
                    ref=element_ref,
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector=playwright_selector,
                    scenario_index=scenario_index
                )
            
            elif action in ['hover']:
                await self.mcp_client.browser_hover(element=element, ref=element_ref)
                result = MCPAction(
                    action_type="hover",
                    element=element,
                    ref=element_ref,
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector=playwright_selector,
                    scenario_index=scenario_index
                )
            
            elif action in ['verify', 'validate', 'assert']:
                # For verification, we just check if element exists in snapshot
                result = MCPAction(
                    action_type="verify",
                    element=element,
                    ref=element_ref,
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector=playwright_selector,
                    scenario_index=scenario_index
                )
            
            elif action in ['assert_text']:
                # For text assertion, we check if element contains the expected text
                result = MCPAction(
                    action_type="assert_text",
                    element=element,
                    ref=element_ref,
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector=playwright_selector,
                    scenario_index=scenario_index
                )
            
            elif action in ['switches to new tab', 'switch to new tab', 'switches to tab', 'switch tab']:
                # Handle tab switching - get list of tabs and switch to the newest one
                tabs = await self.mcp_client.browser_tab_list()
                if tabs and len(tabs.get('tabs', [])) > 1:
                    # Switch to the last tab (newest)
                    await self.mcp_client.browser_tab_select(len(tabs['tabs']) - 1)
                result = MCPAction(
                    action_type="switch_tab",
                    element="new tab",
                    ref="",
                    test_data=test_data,
                    condition=condition,
                    timestamp=time.time(),
                    success=True,
                    playwright_selector="",
                    scenario_index=scenario_index
                )
            
            else:
                raise ValueError(f"Unsupported action: {action}")
            
            self._log_action(result)
            return result
            
        except Exception as e:
            result = MCPAction(
                action_type=action,
                element=element,
                ref="",
                test_data=test_data,
                condition=condition,
                timestamp=time.time(),
                success=False,
                playwright_selector="",
                error_message=str(e),
                scenario_index=scenario_index
            )
            self._log_action(result)
            return result
    
    async def _get_element_details(self, snapshot: str, element_ref: str) -> Optional[Dict]:
        """
        Get detailed information about a specific element from the snapshot
        """
        try:
            lines = snapshot.split('\n')
            in_snapshot_section = False
            
            for line in lines:
                line = line.strip()
                
                if 'Page Snapshot' in line:
                    in_snapshot_section = True
                    continue
                
                if not in_snapshot_section:
                    continue
                
                if not line or line.startswith('```'):
                    continue
                
                if f'ref={element_ref}' in line:
                    return self._parse_element_line(line)
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting element details: {e}")
            return None
    

    
    async def crawl_with_mcp(self, file_content: str, scenario_index: int, 
                           jira_id: str, current_script: str = "") -> Dict:
        """
        MCP-based crawl function to replace Selenium crawl
        """
        try:
            # Parse BDD content using existing InputProcessor
            output = generate(file_content)
            if output.startswith("```json") and output.endswith("```"):
                output = output[7:-3]
            
            output_json = json.loads(output)
            test_url = output_json['app_url']
            test_steps = output_json['steps']
            
            self.logger.info(f"Processing scenario {scenario_index} for JIRA {jira_id}")
            self.logger.info(f"Target URL: {test_url}")
            self.logger.info(f"Number of test steps: {len(test_steps)}")
            
            # Navigate to the target URL
            await self.mcp_client.browser_navigate(url=test_url)
            await self.mcp_client.browser_wait_for(time=2)  # Wait for page load
            
            # Handle mobile view if specified
            for test_step in test_steps:
                if 'mobile view' in test_step.get('condition', '').lower():
                    await self.mcp_client.browser_resize(width=430, height=932)
                    break
            
            # Execute each test step
            for i, test_step in enumerate(test_steps):
                self.logger.info(f"Executing step {i+1}/{len(test_steps)}: {test_step['step']}")
                
                # Skip create/generate steps (handled differently)
                if str(test_step['step']).lower().strip().startswith(("create", "generate")):
                    self.logger.info("Skipping create/generate step")
                    continue
                
                # Add scenario index to test step
                test_step['scenario_index'] = scenario_index
                
                # Execute the action
                action_result = await self._execute_action(test_step)
                
                # Wait between actions
                await self.mcp_client.browser_wait_for(time=1)
                
                if not action_result.success:
                    self.logger.warning(f"Step {i+1} failed: {action_result.error_message}")
            
            # Generate Playwright scripts from MCP actions
            final_script = await self._generate_playwright_scripts(
                file_content, scenario_index, jira_id, current_script
            )
            
            return {
                "success": True,
                "jira_id": jira_id,
                "scenario_index": scenario_index,
                "actions_executed": len(self.actions_log),
                "actions_successful": len([a for a in self.actions_log if a.success])
            }
            
        except Exception as e:
            self.logger.error(f"Error in MCP crawl: {e}")
            return {
                "success": False,
                "error": str(e),
                "jira_id": jira_id,
                "scenario_index": scenario_index
            }
    
    async def _generate_playwright_scripts(self, file_content: str, scenario_index: int,
                                         jira_id: str, current_script: str = ""):
        """
        Generate Playwright scripts from MCP actions
        """
        try:
            # Convert MCP actions to DataFrame format expected by StepDefTemplate
            actions_data = []
            
            for action in self.actions_log:
                # Generate original_bdd_step from action data
                original_bdd_step = f"user {action.action_type}s on \"{action.element}\""
                if action.test_data:
                    original_bdd_step += f" as \"{action.test_data}\""
                
                actions_data.append({
                    'url': '',  # Will be filled from navigation actions
                    'page': '',
                    'action': action.action_type,
                    'object': action.element,
                    'data': action.test_data,
                    'failure': action.error_message if not action.success else '',
                    'input': '',
                    'TagName': '',
                    'ID': '',
                    'Class': '',
                    'Name': '',
                    'LinkText': '',
                    'CssSelector_with_Class': '',
                    'CssSelector_with_ID': '',
                    'All_Attributes': '',
                    'Xpath': action.ref,
                    'RelXpath': action.ref,
                    'playwrightLocator': action.playwright_selector,
                    'Condition': action.condition,
                    'Condition_Value': '',
                    'original_bdd_step': original_bdd_step,  # Add missing column
                    'window_detected': False  # Add missing column
                })
            
            # Create DataFrame
            df_inter = pd.DataFrame(actions_data)
            
            # Create field details string with proper Playwright locators.
            field_details = "Details of all actions on web element objects with test data, PlaywrightLocator, condition, and window detection given below in the form of 'BDDStep -- ActionType -- WebElementName -- TestDataValue -- PlaywrightLocator -- Condition -- WindowDetection'. \nCRITICAL: Column 5 is PlaywrightLocator (the actual selector to use in code). Column 4 is TestDataValue (e.g., 'user@test.com'). Column 6 is Condition. Column 7 is WindowDetection. Do NOT confuse them. Use PlaywrightLocator verbatim in your generated code. Do NOT generate, infer, or substitute a different locator.\n"

            for _, row in df_inter.iterrows():
                field_details += (
                    f"{row['original_bdd_step']} -- {row['action']} -- {row['object']} -- "
                    f"{row['data']} -- {row['playwrightLocator']} -- {row['Condition']} -- {row['window_detected']}\n"
                )
            
            # Log the complete field_details content
            logger.info(f"Field Details Content:\n{field_details}")
            
            # Generate Playwright scripts using existing StepDefTemplate with correct parameters
            final_script = self.step_def_template.generate_playwright(
                data=file_content,
                df=field_details,
                df_inter=df_inter,
                i=scenario_index,
                flag=True,  # Use merged prompt for MCP-generated scripts
                jira_id=jira_id,
                current_script=current_script,  # Fix parameter name
                generate_step_definitions=True,  # Add missing parameter for BDD format
                exclude_paypal_code=True  # Exclude custom PayPal code for MCP flows
            )
            
            self.logger.info(f"Generated Playwright scripts for scenario {scenario_index}")
            return final_script
            
        except Exception as e:
            self.logger.error(f"Error generating Playwright scripts: {e}")
            raise
    
    def get_actions_summary(self) -> Dict:
        """Get summary of executed actions"""
        return {
            "total_actions": len(self.actions_log),
            "successful_actions": len([a for a in self.actions_log if a.success]),
            "failed_actions": len([a for a in self.actions_log if not a.success]),
            "action_types": list(set(a.action_type for a in self.actions_log))
        }
    
    def get_scenario_actions(self, scenario_index: int) -> List[Dict]:
        """Get actions for a specific scenario"""
        if scenario_index in self.scenario_actions:
            return [self._action_to_dict(action) for action in self.scenario_actions[scenario_index]]
        return []
    
    def get_all_actions(self) -> List[Dict]:
        """Get all actions from all scenarios"""
        return [self._action_to_dict(action) for action in self.actions_log]
    
    def _action_to_dict(self, action: MCPAction) -> Dict:
        """Convert MCPAction to dictionary"""
        return {
            'action_type': action.action_type,
            'element': action.element,
            'ref': action.ref,
            'test_data': action.test_data,
            'condition': action.condition,
            'timestamp': action.timestamp,
            'success': action.success,
            'playwright_selector': action.playwright_selector,
            'error_message': action.error_message,
            'scenario_index': action.scenario_index
        } 
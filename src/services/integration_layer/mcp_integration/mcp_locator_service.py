import logging
import asyncio
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from .mcp_client_adapter import UniversalMCPClient

logger = logging.getLogger(__name__)

@dataclass
class MCPLocatorResult:
    """Result from MCP locator discovery"""
    step: str
    locator: Optional[str] = None
    element_description: Optional[str] = None
    error: bool = False
    error_message: Optional[str] = None

class MCPLocatorService:
    """Service to get locators from MCP without generating TypeScript"""
    
    def __init__(self, mcp_config=None):
        # Universal adapter handles configuration automatically
        self.mcp_client: Optional[UniversalMCPClient] = None
        self.logger = logging.getLogger(__name__)
    
    async def execute_full_scenario(self, test_steps: List[str], file_content: str, scenario_num: int, jira_id: str, scenario_dict: dict, new_window_detected, current_script: str, record_video: bool = False, generate_step_definitions: bool = True) -> str:
        """Execute full scenario using single MCP browser"""
        import pandas as pd
        from services.prompt_layer.step_def_template import StepDefTemplate
        
        current_df = pd.DataFrame()
        stepDefTemplate = StepDefTemplate()
        
        # Video recording setup (same as traditional)
        __video_dir = None
        success_recording_zip = None
        if record_video:
            from utils.recording_cleanup import _setup_failure_recording_dir
            __video_dir = _setup_failure_recording_dir("mcp-test", str(jira_id or scenario_num))
            logger.info(f"📹 MCP video recording enabled: {__video_dir}")
        
        try:
            # Initialize MCP client - single browser for everything
            if not self.mcp_client:
                self.mcp_client = UniversalMCPClient()
                await self.mcp_client.connect()
                logger.info("✅ MCP client connected for full execution")
            
            # Execute each step using MCP browser
            for step in test_steps:
                if not step:
                    continue
                    
                logger.info(f"🎯 Executing step: {step}")
                
                try:
                    # Navigate to page if it's a navigation step
                    if self._is_navigation_step(step):
                        url = self._extract_url_from_step(step)
                        if url:
                            await self.mcp_client.browser_navigate(url)
                            await asyncio.sleep(2)
                            # Record navigation using simplified approach for MCP
                            current_df = self._record_step_to_dataframe(
                                current_df, step, 'navigate', url, f'page.goto("{url}")'  
                            )
                    
                    # Handle click steps
                    elif 'click' in step.lower():
                        # Wait for page stability then get snapshot
                        await asyncio.sleep(5)
                        snapshot = await self.mcp_client.browser_snapshot()
                        element_desc, ref = self._extract_element_info(step, snapshot)
                        if element_desc and ref:
                            # Generate and use locator in one step
                            clean_locator = await self.mcp_client.browser_generate_locator(element=element_desc, ref=ref)
                            if not clean_locator:
                                raise ValueError(f"Empty locator response for element: {element_desc}")
                            
                            # Execute click using MCP
                            await self.mcp_client.browser_click(element=element_desc, ref=ref)
                            
                            # Record to DataFrame using proper function with MCP element
                            from services.agent_layer.playwright_agent import record_page_object_details, extract_page_name
                            page_name = extract_page_name('https://www-stg.chewy.net')  # MCP page
                            
                            # Create mock test_step for compatibility
                            test_step = {'original_bdd_step': step, 'generated_by': 'mcp_full_execution'}
                            
                            # Record to DataFrame using simplified approach for MCP
                            current_df = self._record_step_to_dataframe(
                                current_df, step, 'click', element_desc, clean_locator
                            )
                    
                    # Handle type/enter steps  
                    elif 'enter' in step.lower() or 'type' in step.lower():
                        # Wait for page stability then get snapshot
                        await asyncio.sleep(5)
                        snapshot = await self.mcp_client.browser_snapshot()
                        element_desc, ref = self._extract_element_info(step, snapshot)
                        text_value = self._extract_text_from_step(step)
                        if element_desc and ref and text_value:
                            # Generate locator
                            clean_locator = await self.mcp_client.browser_generate_locator(element=element_desc, ref=ref)
                            if not clean_locator:
                                raise ValueError(f"Empty locator response for element: {element_desc}")
                            
                            # Execute type using MCP
                            await self.mcp_client.browser_type(element=element_desc, ref=ref, text=text_value)
                            
                            # Record to DataFrame using proper function with MCP element
                            from services.agent_layer.playwright_agent import record_page_object_details, extract_page_name
                            page_name = extract_page_name('https://www-stg.chewy.net')  # MCP page
                            
                            # Create mock test_step for compatibility
                            test_step = {'original_bdd_step': step, 'generated_by': 'mcp_full_execution'}
                            
                            # Record to DataFrame using simplified approach for MCP
                            current_df = self._record_step_to_dataframe(
                                current_df, step, 'type', element_desc, clean_locator, text_value
                            )
                    
                except Exception as e:
                    logger.error(f"❌ Error executing step '{step}': {e}")
                    continue
            
            # Store scenario data
            scenario_dict[f"Scenario_{scenario_num}"] = current_df
            
            # Generate TypeScript using existing template
            field_details = self._generate_field_details_from_df(current_df)
            final_script = stepDefTemplate.generate_playwright(
                file_content, field_details, current_df, scenario_num, False,
                jira_id=jira_id, current_script=current_script, generate_step_definitions=generate_step_definitions,
                exclude_paypal_code=True  # Exclude custom PayPal code for MCP flows
            )
            
            # Handle video recording completion (same as traditional)
            if record_video and __video_dir:
                try:
                    from utils.s3_utils import redact_presigned_url_for_log, upload_file_to_s3_and_get_urls
                    from pathlib import Path
                    import shutil
                    
                    # Package video recording
                    zip_path = shutil.make_archive(str(__video_dir), "zip", __video_dir)
                    shutil.rmtree(__video_dir, ignore_errors=True)
                    
                    # Upload to S3
                    upload_result = upload_file_to_s3_and_get_urls(Path(zip_path))
                    success_recording_zip = upload_result.get("presigned_url") or upload_result.get("http_url")
                    
                    # Cleanup local zip
                    Path(zip_path).unlink(missing_ok=True)
                    logger.info(f"📹 MCP recording uploaded: {redact_presigned_url_for_log(success_recording_zip)}")
                    
                    if success_recording_zip:
                        return final_script, success_recording_zip
                except Exception as e:
                    logger.warning(f"⚠️ MCP video processing failed: {e}")
            
            return final_script
            
        except Exception as e:
            logger.error(f"❌ Error in MCP full execution: {e}")
            return f"Error in MCP execution: {str(e)}"
        finally:
            if self.mcp_client:
                await self.mcp_client.disconnect()
    
    async def _get_locator_for_step(self, step: str, snapshot: str) -> MCPLocatorResult:
        """Get locator for a specific step"""
        try:
            # Extract element information from step
            element_desc, ref = self._extract_element_info(step, snapshot)
            
            if element_desc and ref:
                # Use MCP to generate locator
                locator = await self.mcp_client.browser_generate_locator(
                    element=element_desc,
                    ref=ref
                )
                
                if locator:
                    return MCPLocatorResult(
                        step=step,
                        locator=locator,
                        element_description=element_desc
                    )
            
            return MCPLocatorResult(
                step=step,
                error=True,
                error_message="Could not generate locator"
            )
            
        except Exception as e:
            return MCPLocatorResult(
                step=step,
                error=True,
                error_message=str(e)
            )
    

    def _is_navigation_step(self, step: str) -> bool:
        """Check if step is a navigation step"""
        navigation_keywords = ["navigate", "load", "browse", "visit", "open", "go to"]
        return any(keyword in step.lower() for keyword in navigation_keywords)
    
    def _extract_url_from_step(self, step: str) -> Optional[str]:
        """Extract URL from navigation step"""
        import re
        url_match = re.search(r'"([^"]+)"', step)
        if url_match:
            url = url_match.group(1)
            if url.startswith('http'):
                return url
        raise ValueError(f"No valid URL found in navigation step: {step}")
    
    def _extract_element_info(self, step: str, snapshot: str) -> tuple:
        """Extract element description and reference from step and snapshot using advanced parsing"""
        import re
        
        # Extract quoted text as element description
        element_match = re.search(r'"([^"]+)"', step)
        element_desc = element_match.group(1) if element_match else ""
        
        if not element_desc or not snapshot:
            return element_desc, element_desc
        
        # Use advanced element finding from MCPAutomationEngine
        ref = self._find_element_in_snapshot(snapshot, element_desc)
        return element_desc, ref or element_desc
    
    def _find_element_in_snapshot(self, snapshot: str, element_description: str) -> Optional[str]:
        """Find element reference in accessibility snapshot using improved parsing"""
        try:
            if not isinstance(snapshot, str):
                snapshot = str(snapshot)
            
            # Parse the YAML-like structure from MCP snapshot
            lines = snapshot.split('\n')
            elements = []
            
            # Find the Page Snapshot section
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
                
                # Parse element lines that contain refs
                if 'ref=' in line:
                    element_info = self._parse_element_line(line)
                    if element_info:
                        elements.append(element_info)
            
            logger.debug(f"Found {len(elements)} elements in snapshot")
            
            # Search for the target element
            target_element = self._find_best_element_match(elements, element_description)
            
            if target_element:
                logger.info(f"Found element '{element_description}' -> {target_element}")
                return target_element
            else:
                raise ValueError(f"Element '{element_description}' not found in snapshot")
                
        except Exception as e:
            logger.error(f"Error finding element in snapshot: {e}")
            return None
    
    def _parse_element_line(self, line: str) -> Optional[Dict]:
        """Parse a single element line from the MCP snapshot"""
        try:
            import re
            ref_match = re.search(r'\[ref=([a-zA-Z0-9]+)\]', line)
            if not ref_match:
                return None
            
            ref = ref_match.group(1)
            line_without_ref = re.sub(r'\[ref=[a-zA-Z0-9]+\]', '', line).strip()
            parts = line_without_ref.split()
            if not parts:
                return None
            
            element_type = parts[0]
            text_match = re.search(r'"([^"]*)"', line_without_ref)
            text_content = text_match.group(1) if text_match else ""
            
            return {
                'ref': ref,
                'type': element_type,
                'text': text_content,
                'full_line': line
            }
            
        except Exception as e:
            logger.debug(f"Error parsing element line '{line}': {e}")
            return None
    
    def _find_best_element_match(self, elements: List[Dict], target_description: str) -> Optional[str]:
        """Find the best matching element for the given description"""
        if not elements:
            return None
        
        target_lower = target_description.lower()
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
            
            if score > best_score:
                best_score = score
                best_match = element['ref']
        
        return best_match
    
    def _extract_text_from_step(self, step: str) -> str:
        """Extract text value from type/enter steps"""
        import re
        # Look for 'as "value"' pattern
        match = re.search(r'as\s+"([^"]+)"', step)
        return match.group(1) if match else ""
    
    def _record_step_to_dataframe(self, current_df, step: str, action: str, element: str, locator: str, data: str = '') -> 'pd.DataFrame':
        """Record step execution to DataFrame for TypeScript generation"""
        import pandas as pd
        
        new_row = {
            'original_bdd_step': step,
            'action': action,
            'object': element,
            'data': data,
            'playwrightLocator': locator,
            'Condition': '',
            'window_detected': 'false',
            'page': 'MCP_Page',
            'url': '',
            'Xpath': '',
            'generated_by': 'mcp_full_execution'
        }
        
        new_df = pd.DataFrame([new_row])
        return pd.concat([current_df, new_df], ignore_index=True)
    
    def _generate_field_details_from_df(self, dataframe) -> str:
        """Generate field details string from DataFrame for TypeScript generation"""
        field_details = (
            "Details of all actions on web element objects with test data and Locator and condition given below in the form of "
            "'BDDStep -- ActionType -- WebElementName -- TestDataValue -- PlaywrightLocator -- Condition -- WindowDetection'. \n"
            "CRITICAL: Column 5 is PlaywrightLocator (the actual selector to use in code). "
            "Column 4 is TestDataValue (e.g., 'user@test.com'). Do NOT confuse them. "
            "Use PlaywrightLocator (column 5) verbatim in your generated code. Do NOT generate, infer, or substitute a different locator.\n"
        )
        
        for _, row in dataframe.iterrows():
            field_details += (
                f"{row['original_bdd_step']} -- {row['action']} -- {row['object']} -- "
                f"{row['data']} -- {row['playwrightLocator']} -- {row['Condition']} -- "
                f"{row['window_detected']}\n"
            )
        
        return field_details
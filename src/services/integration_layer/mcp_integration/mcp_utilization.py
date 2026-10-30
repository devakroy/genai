# Standard library imports
import asyncio
import ast
import datetime
import glob
import json
import logging
import os
import re
import shutil
import tempfile
import traceback
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional

# Third-party imports
import boto3
import pandas as pd
from botocore.config import Config
from dotenv import load_dotenv
from langchain_aws import ChatBedrock, ChatBedrockConverse
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.prompts import PromptTemplate

# Local imports
from utils.constants import PLATFORM_CONFIG

# Local imports
from .mcp_client_adapter import UniversalMCPClient
from .mcp_step_verifier import MCPStepVerifier
from services.prompt_layer.step_def_template import StepDefTemplate
from services.prompt_layer.step_def_generation_playwright import StepDefGenerationPlaywright
from utils.recording_cleanup import _setup_failure_recording_dir
from utils.s3_utils import redact_presigned_url_for_log, upload_file_to_s3_and_get_urls

load_dotenv()
logger = logging.getLogger(__name__)

@dataclass
class ScenarioStep:
    """Represents a BDD scenario step"""
    step: str
    response_context: Optional[str] = None

@dataclass
class BDDScenario:
    """Represents a BDD scenario"""
    name: str
    steps: List[ScenarioStep]

@dataclass
class BDDFeature:
    """Represents a BDD feature"""
    name: str
    scenarios: List[BDDScenario]

@dataclass
class MCPPlayWrightLLMResponse:
    """Response from LLM for Playwright MCP commands"""
    tool: str
    locator: Optional[str] = None
    step: Optional[str] = None
    page_url: Optional[str] = None
    input: Optional[Dict[str, Any]] = None
    error: bool = False
    error_message: Optional[str] = None


@dataclass
class MCPResult:
    """Result from MCP command execution"""
    error: bool 
    output: str
    inconsistent: bool = False
    error_message: Optional[str] = None

    def __post_init__(self):
        """Validate the MCPResult after creation"""
        if self.error_message is None and self.error:
            raise ValueError("Error message is required when error=True")

class UiAutomationService:
    """Python equivalent of the Java UiAutomationService"""

    def __init__(self, mcp_config=None, domain_selection: Optional[str] = None):
        """Initialize the service with MCP configuration"""
        # Universal adapter handles configuration automatically
        self.mcp_client: Optional[UniversalMCPClient] = None
        self.logger = logging.getLogger(__name__)
        self._is_shared_instance = False  # ✅ Track if this is a shared instance
        self.mcp_generated_locators = []  # ✅ Store MCP locators
        self.scenario_dataframe = None  # Track scenario data like traditional crawler
        self.step_verifier = MCPStepVerifier(domain_selection=domain_selection)  # ✅ Step verification like JS crawler

        # Load system prompts
        self.playwright_system_prompt = self._load_playwright_system_prompt()
        self.playwright_rectification_system_prompt = self._load_playwright_rectification_system_prompt()

        # Load templates
        self.page_analysis_template = self._load_page_analysis_template()
        self.error_analysis_template = self._load_error_analysis_template()

    async def chat(self, step_lst, record_video: bool = False, jira_id: str = None, generate_step_definitions: bool = True, current_script: str = "", scenario_number: int = 1, scenario_dict: dict = None, datasets=None, run_id: Optional[str] = None) -> tuple:
        """Main method with LLM tool selection and retry logic
        
        Args:
            run_id: Optional run ID for status tracking in tran_status table
        """
        if scenario_dict is None:
            scenario_dict = {}
        trace_file = None
        
        # Import status tracking functions only when needed
        if run_id:
            try:
                from utils.run_id_manager import (
                    append_step_to_tran_status,
                    mark_dry_run_complete,
                    set_current_executing_step
                )
                status_tracking_available = True
            except ImportError:
                logger.warning("⚠️  Status tracking unavailable")
                status_tracking_available = False
        else:
            status_tracking_available = False
        
        try:
            if not self.mcp_client:
                self.mcp_client = UniversalMCPClient()
                await self.mcp_client.connect()
                logger.info("✅ MCP client connected successfully")
                if self.mcp_client.session_id:
                    logger.info(f"📋 MCP Session ID: {self.mcp_client.session_id}")
                    logger.info(f"🎬 Video files will be associated with session: {self.mcp_client.session_id[:8]}...")
                else:
                    logger.info("⚠️ MCP Session ID not available (local MCP or extraction failed)")
            # Check if recording is enabled - MCP with --save-trace automatically traces
            if record_video:
                logger.info("🎬 MCP server configured with --save-trace - automatic tracing enabled")
                logger.info("📝 Trace files will be automatically saved to MCP output directory")
            # Initialize DataFrame like traditional crawler with ALL required columns
            self.scenario_dataframe = pd.DataFrame(columns=[
                'original_bdd_step', 'action', 'object', 'data', 'playwrightLocator',
                'Condition', 'window_detected', 'page', 'url', 'Xpath', 'RelXpath',
                'TagName', 'ID', 'Class', 'Name', 'LinkText', 'CssSelector_with_Class',
                'CssSelector_with_ID', 'All_Attributes', 'failure', 'input', 
                'Condition_Value', 'generated_by'
            ])

            for step_index, step in enumerate(step_lst):
                if not step or not step.strip():
                    continue
                logger.info(f"---------------------------------------------------\n")
                logger.info(f"🎯 Processing step ---- {step_index + 1}/{len(step_lst)}: {step}")
                
                # Set current executing step BEFORE processing (if run_id provided)
                if status_tracking_available and run_id:
                    try:
                        set_current_executing_step(run_id, step)
                        logger.debug(f"▶️  Set current executing step {step_index + 1}: {step[:50]}...")
                    except Exception as status_e:
                        logger.warning(f"⚠️  Failed to set current executing step: {status_e}")
                
                try:
                    # Element finding with LLM tool selection and retry
                    llm_response = await self._find_element_with_retry(step, step_index=step_index, step_lst=step_lst)
                    
                    # Record to DataFrame like traditional crawler
                    self.scenario_dataframe = await self._record_mcp_action_to_dataframe(
                        self.scenario_dataframe, step, llm_response, None, step_index
                    )
                    
                    # Update tran_status with successful step AFTER execution (if run_id provided)
                    if status_tracking_available and run_id:
                        try:
                            append_step_to_tran_status(run_id, step)
                            logger.debug(f"📝 Updated tran_status for step {step_index + 1}: {step[:50]}...")
                        except Exception as status_e:
                            logger.warning(f"⚠️  Failed to update tran_status: {status_e}")
                    
                    logger.info(f"✅ Step {step_index + 1} completed successfully")                   
                except (RuntimeError, Exception) as step_error:
                    # Handle both critical failures (RuntimeError) and unexpected errors
                    error_type = "CRITICAL FAILURE" if isinstance(step_error, RuntimeError) else "UNEXPECTED ERROR"
                    error_description = "Critical failure" if isinstance(step_error, RuntimeError) else "Unexpected error"
                    
                    logger.error(f"❌ {error_type} at step {step_index + 1}: {step_error}")
                    
                    # 📝 Generate step definitions for successful steps before failure
                    final_script = await self._generate_partial_step_definitions(
                        scenario_number, scenario_dict, jira_id, current_script, generate_step_definitions, datasets
                    )
                    
                    mcp_error = RuntimeError(
                        f"MCP Process Terminated: {error_description} at step {step_index + 1} of {len(step_lst)}\n"
                        f"Failed Step: '{step}'\n"
                        f"Successful Steps: {step_index}/{len(step_lst)}\n"
                        f"Error Details: {str(step_error)}\n"
                        f"Process cannot continue due to {'unrecoverable' if isinstance(step_error, RuntimeError) else 'unexpected'} step failure."
                    )
                    # Attach the failed step to the exception for error_formatter.py
                    setattr(mcp_error, "step", step)
                    
                    # ALWAYS initialize context dict (even if empty)
                    mcp_error.context = {}
                    
                    # Attach partial step definitions to exception context
                    log_prefix = "CRITICAL" if isinstance(step_error, RuntimeError) else "UNEXPECTED"
                    logger.info(f"🔍 {log_prefix}: final_script exists: {bool(final_script)}, length: {len(final_script) if final_script else 0}")
                    if final_script and final_script.strip():
                        mcp_error.context['step_definition_file'] = final_script
                        logger.info(f"📝 ✅ ATTACHED partial step definitions to RuntimeError.context (key='step_definition_file', {step_index} successful steps, {len(final_script)} chars)")
                    else:
                        logger.warning(f"⚠️ final_script is empty - NO step definitions to attach! Context dict still initialized.")
                    
                    logger.info(f"🔍 RAISING {error_type} with context: {hasattr(mcp_error, 'context')}, keys: {list(mcp_error.context.keys()) if hasattr(mcp_error, 'context') else 'N/A'}")
                    raise mcp_error

            # Mark dry run complete before script generation (if run_id provided)
            if status_tracking_available and run_id:
                try:
                    mark_dry_run_complete(run_id)
                    logger.info(f"🏁 Dry run marked complete for run_id: {run_id}")
                except Exception as status_e:
                    logger.warning(f"⚠️  Failed to mark dry run complete: {status_e}")

            # Generate field details like traditional crawler
            field_details = self._generate_field_details_from_df(self.scenario_dataframe)
            
            # Generate step definitions using the same logic as traditional crawler
            # Use proper BDD feature content and flag=False for first scenario
            feature_content = f"Feature: MCP Generated Test\n\nScenario: MCP Test Scenario\n"
            for _, row in self.scenario_dataframe.iterrows():
                feature_content += f"  {row['original_bdd_step']}\n"
            
            # Store scenario in scenario_dict like JS crawler
            scenario_dict[f"Scenario_{scenario_number}"] = self.scenario_dataframe
            
            # Determine flag based on whether this is first scenario and if there are previous scenarios
            flag = scenario_number > 1 and any(f"Scenario_{j}" in scenario_dict for j in range(1, scenario_number))
            
            # Use same conditional logic as JS crawler
            # Wrap script generation in try/catch to attempt partial generation on failure
            try:
                if generate_step_definitions:
                    stepDefTemplate = StepDefTemplate()
                    final_script = stepDefTemplate.generate_playwright(
                        data=feature_content,
                        df=field_details,
                        df_inter=self.scenario_dataframe,
                        i=scenario_number,  # Use actual scenario number
                        flag=flag,  # Determine if merged prompt should be used
                        jira_id=jira_id,
                        current_script=current_script,  # Append to previous scenarios
                        generate_step_definitions=True,
                        exclude_paypal_code=True  # Exclude custom PayPal code for MCP flows
                    )
                else:
                    playwright_generator = StepDefGenerationPlaywright()
                    final_script = playwright_generator.generate_playwright_test(
                        feature_content,
                        field_details,
                        self.scenario_dataframe,
                        i=scenario_number,  # Use actual scenario number
                        flag=flag,  # Determine if merged prompt should be used
                        jira_id=jira_id,
                        current_script=current_script,  # Append to previous scenarios
                        datasets=datasets
                    )
            except Exception as script_gen_error:
                # Script generation failed - attempt partial generation as fallback
                logger.error(f"❌ Script generation failed: {script_gen_error}")
                logger.info("📝 Attempting partial step definitions as fallback...")
                final_script = await self._generate_partial_step_definitions(
                    scenario_number, scenario_dict, jira_id, current_script, generate_step_definitions, datasets
                )
                if final_script:
                    logger.info(f"✅ Partial script generated as fallback ({len(final_script)} chars)")
                else:
                    logger.warning("⚠️ Partial script generation also failed - no script available")
                    # Re-raise with context about successful steps
                    gen_error = RuntimeError(
                        f"Script generation failed after {len(self.scenario_dataframe)} successful steps.\n"
                        f"Error: {str(script_gen_error)}"
                    )
                    gen_error.context = {}
                    raise gen_error
            
            logger.info(f"✅ MCP Process completed successfully - all {len(step_lst)} steps executed")
            
            # Handle trace/video download based on MCP type
            if record_video:
                trace_file = None
                
                # REMOTE MCP: Download from /videos API
                if self.mcp_client and self.mcp_client._is_remote:
                    try:
                        # CRITICAL: Close browser to finalize video recording
                        logger.info("🔒 Closing browser to finalize video recording...")
                        try:
                            await self.mcp_client.browser_close()
                            logger.info("✅ Browser closed successfully")
                        except Exception as close_error:
                            logger.warning(f"⚠️ Browser close warning (may already be closed): {close_error}")
                        
                        # Wait 10 seconds for server to finish writing video file (per team lead)
                        logger.info("⏳ Waiting 10 seconds for server to finalize video file...")
                        await asyncio.sleep(5)
                        logger.info("✅ Wait complete, proceeding with download")
                        
                        logger.info(f"📥 Downloading video from remote MCP server (session: {self.mcp_client.session_id[:8] if self.mcp_client.session_id else 'unknown'}...)...")
                        trace_file = await self._download_remote_trace(jira_id, self.mcp_client.session_id)
                        if trace_file:
                            logger.info(f"✅ Remote video downloaded: {trace_file}")
                        else:
                            logger.warning("⚠️ No video files available on remote MCP")
                    except Exception as download_error:
                        logger.warning(f"⚠️ Remote video download failed: {download_error}")
                
                # LOCAL MCP: Find trace files from filesystem
                else:
                    try:
                        logger.info("🎬 Looking for local MCP trace files...")
                        trace_file = await self._find_and_save_mcp_trace(jira_id)
                        if trace_file:
                            logger.info(f"✅ Local trace saved: {trace_file}")
                        else:
                            logger.warning("⚠️ No local trace files found")
                    except Exception as trace_error:
                        logger.warning(f"⚠️ Failed to save local trace: {trace_error}")
                
                # Return script + trace/video URL
                if trace_file:
                    return final_script, trace_file
            
            return final_script

        except RuntimeError as re:
            # Re-raise RuntimeError (critical failures) with full context
            # Step definition file should already be attached by inner exception handlers
            logger.error(f"❌ MCP Process failed with critical error: {re}")
            
            # 📝 Log if step definition file is present in exception context
            if hasattr(re, 'context') and isinstance(re.context, dict):
                step_def_len = len(re.context.get('step_definition_file', ''))
                if step_def_len > 0:
                    logger.info(f"📝 Exception context contains step definition file ({step_def_len} chars)")
                else:
                    logger.warning(f"⚠️ Exception context exists but step_definition_file is empty or missing")
            else:
                logger.warning(f"⚠️ Exception has no context attribute")
            
            # Save trace on failure - check if remote or local MCP
            if record_video:
                try:
                    # REMOTE MCP: Download from /videos API
                    if self.mcp_client and self.mcp_client._is_remote:
                        logger.info("🛑 Downloading failure video from remote MCP server...")
                        # Close browser to finalize video recording
                        try:
                            await self.mcp_client.browser_close()
                            logger.info("✅ Browser closed for failure recording")
                        except Exception as close_error:
                            logger.warning(f"⚠️ Browser close warning: {close_error}")

                        # Wait for server to finalize video
                        await asyncio.sleep(5)
                        
                        trace_file = await self._download_remote_trace(jira_id, self.mcp_client.session_id)
                        if trace_file:
                            if hasattr(re, '__dict__'):
                                re.recording_zip = trace_file
                            logger.info(f"📁 Remote failure video saved: {trace_file}")
                    
                    # LOCAL MCP: Find trace files from filesystem
                    else:
                        logger.info("🛑 Looking for local MCP trace files on failure...")
                        trace_file = await self._find_and_save_mcp_trace(jira_id, failed=True)
                        if trace_file:
                            if hasattr(re, '__dict__'):
                                re.recording_zip = trace_file
                            logger.info(f"📁 Local failure trace saved: {trace_file}")
                except Exception as trace_error:
                    logger.warning(f"⚠️ Failed to save failure trace: {trace_error}")
            raise re
        except Exception as e:
            logger.error(f"❌ MCP Process failed with unexpected error: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            
            # 📝 Generate step definitions from DataFrame if available (for unexpected errors)
            # This handles cases where exception occurred outside the step loop but after steps completed
            partial_script = None
            try:
                # Check if we have successful steps in DataFrame
                if self.scenario_dataframe is not None and len(self.scenario_dataframe) > 0:
                    logger.info(f"📝 Attempting to generate step definitions for {len(self.scenario_dataframe)} successful steps...")
                    partial_script = await self._generate_partial_step_definitions(
                        scenario_number, scenario_dict, jira_id, current_script, generate_step_definitions, datasets
                    )
                    if partial_script:
                        logger.info(f"✅ Generated partial step definitions ({len(partial_script)} chars)")
            except Exception as gen_error:
                logger.warning(f"⚠️ Failed to generate partial step definitions: {gen_error}")
            
            # Attach step definitions to exception context
            if partial_script:
                if not hasattr(e, 'context') or not isinstance(e.context, dict):
                    e.context = {}
                e.context['step_definition_file'] = partial_script
                logger.info(f"📝 Attached partial step definitions to exception context ({len(partial_script)} chars)")
            elif 'final_script' in locals() and final_script:
                # Fallback to final_script if it was assigned before exception
                if not hasattr(e, 'context') or not isinstance(e.context, dict):
                    e.context = {}
                e.context['step_definition_file'] = final_script
                logger.info(f"📝 Attached existing final_script to exception context ({len(final_script)} chars)")
            
            # Save trace on failure - check if remote or local MCP
            if record_video:
                try:
                    # REMOTE MCP: Download from /videos API
                    if self.mcp_client and self.mcp_client._is_remote:
                        logger.info("🛑 Downloading failure video from remote MCP server...")
                        # Close browser to finalize video recording
                        try:
                            await self.mcp_client.browser_close()
                            logger.info("✅ Browser closed for failure recording")
                        except Exception as close_error:
                            logger.warning(f"⚠️ Browser close warning: {close_error}")
                        
                        # Wait for server to finalize video
                        await asyncio.sleep(10)
                        
                        trace_file = await self._download_remote_trace(jira_id, self.mcp_client.session_id)
                        if trace_file:
                            if hasattr(e, '__dict__'):
                                e.recording_zip = trace_file
                            logger.info(f"📁 Remote failure video saved: {trace_file}")
                    
                    # LOCAL MCP: Find trace files from filesystem
                    else:
                        logger.info("🛑 Looking for local MCP trace files on exception...")
                        trace_file = await self._find_and_save_mcp_trace(jira_id, failed=True)
                        if trace_file:
                            if hasattr(e, '__dict__'):
                                e.recording_zip = trace_file
                            logger.info(f"📁 Local failure trace saved: {trace_file}")
                except Exception as trace_error:
                    logger.warning(f"⚠️ Failed to save failure trace: {trace_error}")
            raise RuntimeError(
                f"MCP Process Terminated: Unexpected system error\n"
                f"Error Details: {str(e)}\n"
                f"This indicates a system-level failure that prevents execution."
            )
        finally:
            # Cleanup MCP client in same async context
            if self.mcp_client:
                try:
                    await self.mcp_client.disconnect()
                    self.mcp_client = None
                    logger.info("✅ MCP client disconnected cleanly")
                except Exception as cleanup_error:
                    logger.warning(f"⚠️ MCP cleanup warning: {cleanup_error}")
                    # Don't raise - just log the warning

    async def _find_element_with_retry(self, step: str, max_retries: int = 10, validation_retry_count: int = 0, step_index: int = 0, step_lst: list = None) -> MCPPlayWrightLLMResponse:
        """Find element using LLM analysis with validation retry support"""
        try:
            # Initialize iframe element normalization flag
            is_iframe_element_normalized = None
            
            # First resolve any template variables in the step
            custom_tools = self._get_shared_custom_tools()
            resolved_step = custom_tools._replace_variables(step, custom_tools.stored_data)           
            if resolved_step != step:
                logger.info(f"🔄 Step resolved: {step} → {resolved_step[:200]}..." if len(resolved_step) > 200 else f"🔄 Step resolved: {step} → {resolved_step}")
                step_for_execution = resolved_step  # Keep full resolved step for execution
                step_for_llm = resolved_step  # CRITICAL FIX: Send RESOLVED step to LLM (not template) so LLM sees actual values
            else:
                logger.warning(f"⚠️ Step NOT resolved (no placeholders found): {step}")
                step_for_execution = step
                step_for_llm = step
                
            # Generate user prompt with page snapshot (use original step template)
            current_page_snapshot = await self.capture_page_snapshot()
            formatted_prompt = await self._generate_user_prompt(step_for_llm, current_page_snapshot)
            
            # Get LLM instructions (LLM will choose from all available tools)
            llm_response = await self._get_playwright_llm_instructions(formatted_prompt, step_for_execution)
            
            # ✅ VALIDATE STEP RESPONSE (like JS crawler verification) - only when soft_validation is false
            if not PLATFORM_CONFIG.get('soft_validation'):
                validation_passed = await self._validate_step_response(step_for_llm, llm_response)
                if not validation_passed:
                    if validation_retry_count >= 1:
                        # Already retried once, now stop execution
                        logger.error(f"❌ CRITICAL: Step validation failed after retry: {step}")
                        raise RuntimeError(
                            f"MCP Step Validation Failed: Step '{step}' failed validation after retry attempt.\n"
                            f"LLM Tool: '{llm_response.tool}'\n"
                            f"LLM Input: {llm_response.input}\n"
                            f"This indicates the LLM response doesn't match the intended step action."
                        )
                    else:
                        # First validation failure - allow one retry
                        logger.warning(f"⚠️ Step validation failed - attempting retry: {step}")
                        return await self._find_element_with_retry(step, max_retries, validation_retry_count + 1, step_index, step_lst)
            
            if self._requires_element_safety_check(llm_response.tool):
                element_safety_passed = await self._validate_element_safety(step, llm_response)
                if not element_safety_passed:
                    logger.error(f"❌ CRITICAL: Element safety validation failed: {step}")
                    raise RuntimeError(
                        f"MCP Element Safety Failed: Refusing to interact with element that doesn't match step intent.\n"
                        f"Step: '{step}'\n"
                        f"LLM Tool: '{llm_response.tool}'\n"
                        f"LLM Element: '{llm_response.input.get('element', 'N/A')}'\n"
                        f"LLM Input: {llm_response.input}\n"
                        f"This prevents interacting with wrong UI elements that could cause test failures."
                    )
            
            # Generate locator AFTER getting LLM response (if needed)
            if (llm_response.input and 'element' in llm_response.input and 'ref' in llm_response.input and
                llm_response.tool not in ['browser_navigate', 'browser_wait_for']):              
                target_ref = llm_response.input['ref']               
                # CRITICAL: Check if iframe (ref starts with 'f')
                if target_ref and target_ref.startswith('f'):
                    logger.info(f"🎯 Detected iframe element ref '{target_ref}' (starts with 'f')")
                    
                    # Step 1: GENERATE LOCATOR FIRST (while ref is still valid in snapshot)
                    try:
                        raw_locator = await self.mcp_client.browser_generate_locator(
                            element=llm_response.input['element'],
                            ref=target_ref
                        )
                        if raw_locator:
                            llm_response.locator = str(raw_locator).replace('### Result', '').strip()
                            logger.info(f"✅ Generated locator for iframe element (ref={target_ref}): {llm_response.locator[:150]}...")
                        else:
                            logger.error(f"❌ Locator generation returned empty for ref={target_ref}")
                            raise RuntimeError(f"Locator generation failed for iframe element")
                    except Exception as e:
                        logger.error(f"❌ Failed to generate locator for iframe element: {e}")
                        raise RuntimeError(f"Locator generation failed: {e}")
                    
                    # Step 2: Get current snapshot and find iframe with stable selector
                    iframe_container_selector = await self._get_iframe_container_locator(target_ref, current_page_snapshot)
                    if iframe_container_selector:    
                        isDynamic = self._checkDynamicLocator(llm_response.locator)                
                        if isDynamic:
                            logger.info(f"UUID found in stable locator...")
                            is_iframe_element_normalized = False  
                        else:               
                            is_same_origin = await self._is_same_origin_iframe(iframe_container_selector)                       
                            if is_same_origin:
                                # Same-origin: Build frameLocator code with the pre-generated locator
                                logger.info("✅ Same-origin iframe - building frameLocator code")
                                try:
                                    frame_code = self._build_frame_locator_code(
                                        tool=llm_response.tool,
                                        iframe_selector=iframe_container_selector,
                                        element_locator=llm_response.locator,
                                        input_params=llm_response.input
                                    )
                                    logger.info(f"✅ Built frameLocator code: {frame_code}")
                                    # Update locator to show frameLocator notation
                                    is_iframe_element_normalized = False
                                    llm_response.locator = f"frameLocator('{iframe_container_selector}').{llm_response.locator}"
                                except Exception as frame_error:
                                    logger.warning(f"⚠️ Frame code building failed: {frame_error}")
                            else:
                                # Cross-origin: Try to find container, but keep original locator if not found
                                logger.info("⚠️ Cross-origin iframe - searching for container")
                                iframe_container = await self._detect_and_redirect_iframe_element(
                                    target_ref,
                                    llm_response.input['element'],
                                    current_page_snapshot
                                )
                                
                                if iframe_container:
                                    logger.info(f"✅ Found container: {iframe_container['element']} [ref={iframe_container['ref']}]")
                                    # Generate new locator for the container
                                    try:
                                        container_locator = await self.mcp_client.browser_generate_locator(
                                            element=iframe_container['element'],
                                            ref=iframe_container['ref']
                                        )
                                        if container_locator:
                                            is_iframe_element_normalized = False
                                            llm_response.locator = str(container_locator).replace('### Result', '').strip()
                                            llm_response.input['element'] = iframe_container['element']
                                            llm_response.input['ref'] = iframe_container['ref']
                                            logger.info(f"✅ Using container locator: {llm_response.locator[:100]}...")
                                        else:
                                            logger.warning("⚠️ Container locator generation failed - keeping original locator to trigger retry")
                                    except Exception as e:
                                        logger.warning(f"⚠️ Container locator error: {e} - keeping original locator to trigger retry")
                                else:
                                    # No container found - keep original locator, it will fail and trigger retry
                                    logger.warning("⚠️ No clickable container found for cross-origin iframe")
                                    logger.warning("⚠️ Keeping original locator - will fail and trigger LLM retry with alternative element selection")
                                    # Keep the originally generated locator - execution will fail, triggering rectification
                    else:                       
                        is_iframe_element_normalized = False
                        logger.warning("⚠️ unable to normalize iframe selector - proceeding without normalization")
                
                else:
                    # Regular element (not in iframe) - generate locator normally
                    try:
                        raw_locator = await self.mcp_client.browser_generate_locator(
                            element=llm_response.input['element'],
                            ref=llm_response.input['ref']
                        )
                        if raw_locator:
                            llm_response.locator = str(raw_locator).replace('### Result', '').strip()
                            logger.info(f"✅ MCP generated locator: {llm_response.locator[:150]}...")
                        else:
                            logger.error(f"❌ MCP locator generation returned empty")
                            raise RuntimeError(f"MCP locator generation returned empty")
                    except Exception as e:
                        logger.error(f"❌ MCP locator generation failed: {e}")
                        raise RuntimeError(f"MCP locator generation failed: {e}")
            
            # Execute the chosen tool (MCP or custom) using the generated locator
            if await self._is_custom_tool(llm_response.tool):
                mcp_response = await self._execute_custom_tool(llm_response)
            else:
                mcp_response = await self._execute_playwright_mcp_command(llm_response)
            # 🔧 CRITICAL FIX: Check for navigation/popup success patterns even if marked as error
            if mcp_response.error and self._is_navigation_causing_step(step, llm_response):
                navigation_success = await self._check_navigation_success_pattern(step, mcp_response, llm_response)
                if navigation_success:
                    logger.info("✅ Navigation/popup success detected despite error flag - overriding result")
                    mcp_response.error = False
                    mcp_response.error_message = None
            
            if not mcp_response.error and PLATFORM_CONFIG.get('soft_validation'):
                await self.perform_next_step_validation_before_retry(mcp_response, step_index, step_lst, step,current_page_snapshot)
            
            if mcp_response.inconsistent:
                logger.error(f"❌ MCP response marked as inconsistent - stopping execution for step: {step}")
                raise Exception(
                    f"MCP Process Terminated: Inconsistent response detected for step '{step}'\n"
                    f"Validation Error: {mcp_response.error_message}\n"
                    f"The page validation failed multiple times, indicating the page is not ready or stable.\n"
                    f"Process cannot continue due to page inconsistency.' {mcp_response.error_message}"
                )

            # If error, try rectification - THIS WILL NOW THROW EXCEPTION IF ALL RETRIES FAIL
            if mcp_response.error:
                llm_response = await self._rectify_and_execute_step(step, llm_response, mcp_response, step_index, step_lst)
            
            # Only normalize iframe locator if it was specifically marked as needing normalization
            if is_iframe_element_normalized is False:
                logger.info("Normalizing iframe locator after execution")
                normalized_locator = await self._normalize_iframe_locator(llm_response.locator)
                llm_response.locator = normalized_locator
                logger.info("✅ Normalized iframe locator after execution")
            return llm_response           
        except Exception as e:
            if isinstance(e, RuntimeError):                
                raise e
            else:
                mcp_error = RuntimeError(
                    f"MCP Element Finding Failed: Step '{step}' encountered an unexpected error: {str(e)}\n"
                    f"This is a critical failure that prevents further execution."
                )
                setattr(mcp_error, "step", step)
                raise mcp_error
    
    async def perform_next_step_validation_before_retry(self, mcp_response: MCPResult, step_index: int, step_lst: list, step: str, previous_page_snapshot: str) -> None:
        max_validation_attempts = 2  
        api_keywords = ['invokes','tab', 'stores', 'browser' ,'bearer', 'header', 'api', 'service', 'request', 'response', 'navigates']
        if step_index is not None and step_lst and step_index + 1 < len(step_lst):
            next_step = step_lst[step_index + 1]
            next_step_lower = next_step.lower()
            if any(keyword in next_step_lower for keyword in api_keywords):
                logger.info(f"🔄 Skipping next step validation for API-related next step: {next_step}")
                return
        
        for attempt in range(1, max_validation_attempts + 1):
            try:  
                logger.info(f"🔄 Next step validation attempt {attempt}/{max_validation_attempts}")
                await asyncio.sleep(4)                 
                after_action_page_snapshot = await self.capture_page_snapshot()
                current_step_index = step_index if step_index is not None else 0
                total_steps = len(step_lst) if step_lst is not None else 1
                validation_result, failure_reason = await self.step_verifier._llm_validate_next_step_readiness(
                    current_step_index=current_step_index, 
                    total_steps=total_steps,        
                    step_list=step_lst or [step],     
                    page_snapshot=after_action_page_snapshot,
                    previous_snapshot=previous_page_snapshot,
                    mcp_response=mcp_response
                )               
                if validation_result:
                    logger.info(f"✅ Next step validation passed on attempt {attempt}")
                    return  # Break out of loop and method when validation succeeds
                else:
                    next_step = step_lst[step_index + 1] if step_index + 1 < len(step_lst) else "N/A"
                    if attempt < max_validation_attempts:
                        logger.warning(f"⚠️ Next step validation failed on attempt {attempt}: {next_step} - {failure_reason}")
                        logger.info(f"🔄 Retrying validation (attempt {attempt + 1}/{max_validation_attempts})...")
                        continue
                    else:
                        # Final attempt failed
                        logger.warning(f"❌ Next step validation failed after {max_validation_attempts} attempts: {next_step} - {failure_reason}")
                        mcp_response.inconsistent = True
                        mcp_response.error_message = f"Next step validation failed after {max_validation_attempts} attempts - page may not be ready for the step as page element or object may not be ready for next step: {next_step} - {failure_reason}"
                        return
                        
            except Exception as validation_error:
                next_step = step_lst[step_index + 1] if step_index + 1 < len(step_lst) else "N/A"
                if attempt < max_validation_attempts:
                    logger.warning(f"⚠️ Next step validation exception on attempt {attempt}: {validation_error}")
                    logger.info(f"🔄 Retrying validation due to exception (attempt {attempt + 1}/{max_validation_attempts})...")
                    continue
                else:
                    # Final attempt failed with exception
                    logger.warning(f"❌ Next step validation failed with exception after {max_validation_attempts} attempts: {validation_error}")
                    mcp_response.inconsistent = True
                    mcp_response.error_message = f"Next step validation failed after {max_validation_attempts} attempts - page may not be ready for the step as page element or object may not be ready for next step: " + next_step + " " + str(validation_error)
                    return

    async def _execute_custom_tool(self, llm_response: MCPPlayWrightLLMResponse) -> MCPResult:
        """Execute custom tool with intelligent API workflow detection"""
        try:
            custom_tools = self._get_shared_custom_tools()
            tool_name = llm_response.tool
            
            # Get the tool method dynamically
            if hasattr(custom_tools, tool_name):
                tool_method = getattr(custom_tools, tool_name)
                
                # Dynamic workflow continuity - resolve any template variables
                input_params = llm_response.input.copy()
                for key, value in input_params.items():
                    if isinstance(value, str) and '{' in value and '}' in value:
                        # Resolve template variables from stored data
                        resolved_value = custom_tools._replace_variables(value, custom_tools.stored_data)
                        llm_response.input[key] = resolved_value
                if tool_name == 'custom_browser_extract_cookie_tool':
                    llm_response.input['mcp_client'] = self.mcp_client
                    logger.info(f"🍪 CUSTOM TOOL: Passing mcp_client to {tool_name}")
                
                # Pass mcp_client to mock tool for browser interception
                if tool_name == 'custom_mock_api_tool':
                    llm_response.input['mcp_client'] = self.mcp_client
                    logger.info(f"🎭 CUSTOM TOOL: Passing mcp_client to {tool_name}")
                
                # Call the method with input parameters
                if asyncio.iscoroutinefunction(tool_method):
                    result = await tool_method(**llm_response.input)
                else:
                    result = tool_method(**llm_response.input)
                
                # Auto-store API responses for workflow continuity
                if hasattr(result, 'get') and result.get('data'):
                    custom_tools.stored_data['_last_response'] = result
                elif isinstance(result, dict) and ('status' in result or 'data' in str(result)):
                    custom_tools.stored_data['_last_response'] = result
                
                return MCPResult(error=False, output=str(result))
            
            return MCPResult(error=True, output=f"Custom tool '{tool_name}' not found")
            
        except Exception as e:
            return MCPResult(error=True, output=str(e), error_message=str(e))
    


    async def _llm_select_tool(self, step: str, snapshot: str, attempt: int = 1, previous_error: str = None) -> Dict[str, Any]:
        """LLM selects the best MCP tool and strategy with retry intelligence"""
        
        # Build context-aware prompt based on attempt and previous errors
        retry_context = ""
        if attempt > 1 and previous_error:
            retry_context = f"""

PREVIOUS ATTEMPT FAILED:
Attempt #{attempt-1} Error: {previous_error}

INTELLIGENT RETRY STRATEGIES:
- If radio button click failed due to interception → try clicking the LABEL element instead
- If button click failed due to overlay → try clicking the container or parent element
- If element not found → look for alternative selectors (parent, sibling, child elements)
- If element intercepted → try hover first, then click
- If disabled element → wait or find alternative path
"""
        
        prompt = f"""
Step: {step}
Page snapshot: {snapshot[:2000]}...
{retry_context}

Analyze the page and select the BEST strategy:

FOR RADIO BUTTONS:
- If radio input is hidden/intercepted → target the LABEL element
- Look for: label or parent element instead of input [ref=radio1]

FOR BUTTONS:
- If button intercepted → try parent container or wrapper div
- If disabled → look for alternative elements

FOR FORM ELEMENTS:
- If input field not accessible → try clicking label first, then typing

Return JSON with SMART element selection:
{{"tool": "tool_name", "element": "best_target_element", "locator_strategy": "strategy", "reasoning": "why_this_choice"}}"""

        try:
            llm = self._get_llm()
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            return json.loads(response.content)
        except Exception as e:
            logger.error(f"LLM tool selection failed: {e}")
            raise

    async def _execute_selected_tool(self, tool_selection: Dict, step: str) -> MCPPlayWrightLLMResponse:
        """Execute the LLM-selected tool"""
        tool = tool_selection.get("tool", "browser_click")
        element = tool_selection.get("element", step)
        
        try:
            # Handle navigation steps specially
            if "navigate" in step.lower() or "go to" in step.lower() or "open" in step.lower():
                url_match = re.search(r'"([^"]+)"', step)
                if url_match:
                    url = url_match.group(1)
                    logger.info(f"🌍 Navigating to: {url}")
                    result = await self.mcp_client.browser_navigate(url)
                    return MCPPlayWrightLLMResponse(
                        tool="browser_navigate",
                        step=step,
                        error=False
                    )
            
            if tool == "browser_navigate":
                url_match = re.search(r'"([^"]+)"', step)
                if not url_match:
                    raise ValueError(f"No URL found in navigation step: {step}")
                url = url_match.group(1)
                result = await self.mcp_client.execute_tool("browser_navigate", url=url)
            elif tool == "browser_click":
                result = await self.mcp_client.execute_tool("browser_click", element=element, ref=element)
            elif tool == "browser_type":
                text_match = re.search(r'"([^"]+)"', step)
                if not text_match:
                    raise ValueError(f"No text found in type step: {step}")
                text = text_match.group(1)
                result = await self.mcp_client.execute_tool("browser_type", element=element, ref=element, text=text)
            elif tool == "browser_select_option":
                # CRITICAL FIX: Check if this is actually a radio button step
                step_lower = step.lower()
                element_lower = element.lower()
                
                # Comprehensive radio button detection
                is_radio_step = any(indicator in step_lower for indicator in [
                    'radio button', 'radio', 'buy once', 'autoship', 'select once', 'select autoship'
                ])
                
                is_radio_element = any(indicator in element_lower for indicator in [
                    'radio', 'once', 'autoship', 'recurring'
                ])
                
                if is_radio_step or is_radio_element:
                    logger.warning(f"🚨 RADIO-BUTTON-OVERRIDE: Detected radio button interaction, using browser_click instead of browser_select_option")
                    logger.warning(f"🚨 RADIO-BUTTON-OVERRIDE: Step: '{step}', Element: '{element}'")
                    # Use click instead of selectOption for radio buttons
                    result = await self.mcp_client.execute_tool("browser_click", element=element, ref=element)
                else:
                    # This is a real dropdown/select element
                    result = await self.mcp_client.execute_tool("browser_select_option", element=element, ref=element, values=[step])
            else:
                result = await self.mcp_client.execute_tool(tool, element=element, ref=element)
            
            return MCPPlayWrightLLMResponse(
                tool=tool,
                step=step,
                error=False
            )
            
        except Exception as e:
            return MCPPlayWrightLLMResponse(
                tool=tool,
                step=step,
                error=True,
                error_message=str(e)
            )

    async def _generate_user_prompt(self, step: ScenarioStep, current_page_snapshot: str) -> str:
        """Generate unified user prompt for LLM - let LLM handle all parsing"""
        self.logger.info("Handling step with unified LLM approach")
        
        # Always capture page snapshot for context
        #current_page_snapshot = await self.capture_page_snapshot()
        
        user_message = (
            "You are an AI assistant connected to Playwright automation tools via MCP."
            f"Steps to be Executed: {step}\n"
            "CRITICAL: Analyze the step and extract all necessary information:\n"
            "- For navigation: Extract URL and use 'url' field in input\n"
            "- For typing: Extract text and use 'text' field in input\n"
            "- For selecting: Extract value and use 'values' array field in input\n"
            "- For clicking: Identify element and use 'element' and 'ref' fields in input\n"
            "\nIMPORTANT - IFRAME HANDLING:\n"
            "- If the target element is not found in the main page, check the IFRAME CONTENT section\n"
            "- Elements inside iframes will be listed under '=== IFRAME CONTENT ===' section\n"
            "- Look for text matches in both main page AND iframe content\n"
            "- When finding an element in iframe content, use the element description from the iframe section\n"
            "\nAnalyze the Current Page Snapshot to understand the page context and identify the correct elements.\n"
            f"Current Page Snapshot: {current_page_snapshot}"
        )

        formatted_prompt = f"Human: {user_message}"
        return formatted_prompt

    async def capture_page_snapshot(self) -> str:
        """Capture a fresh browser snapshot for analysis including iframe content"""
        try:
            if not self.mcp_client:
                return "MCP client not available"
            
            # Capture main page snapshot
            snapshot_result = await self.mcp_client.browser_snapshot()
            main_snapshot = str(snapshot_result)
            
            # Try to capture iframe content if iframes are present
            iframe_content = await self._capture_iframe_snapshots()
            
            # Combine main page and iframe content
            combined_snapshot = main_snapshot
            if iframe_content:
                combined_snapshot += "\n\n=== IFRAME CONTENT ===\n" + iframe_content
            # logger.info("Snapshot content: {combined_snapshot[:500]}...")
            return combined_snapshot

        except Exception as e:
            self.logger.error(f"Failed to capture browser snapshot: {e}")
            return f"Unable to capture current page snapshot due to error: {str(e)}"

    async def _capture_iframe_snapshots(self) -> str:
        """Attempt to capture content from iframes using browser_evaluate"""
        try:
            iframe_content_js = """
            () => {
                const result = [];
                const iframes = document.querySelectorAll('iframe');
                
                for (let i = 0; i < iframes.length; i++) {
                    const iframe = iframes[i];
                    try {
                        // Try to access iframe content (same-origin only)
                        if (iframe.contentDocument) {
                            const iframeDoc = iframe.contentDocument;
                            const title = iframe.getAttribute('title') || iframe.getAttribute('name') || `iframe-${i}`;
                            const bodyText = iframeDoc.body ? iframeDoc.body.innerText : 'No body content';
                            const elements = iframeDoc.querySelectorAll('*');
                            
                            let elementInfo = [];
                            for (let el of elements) {
                                if (el.innerText && el.innerText.trim()) {
                                    const tagName = el.tagName.toLowerCase();
                                    const text = el.innerText.trim();
                                    const id = el.id ? ` id="${el.id}"` : '';
                                    const className = el.className ? ` class="${el.className}"` : '';
                                    if (text.length < 100) { // Avoid very long text
                                        elementInfo.push(`<${tagName}${id}${className}>${text}</${tagName}>`);
                                    }
                                }
                            }
                            
                            result.push(`\n--- IFRAME: ${title} ---\n${elementInfo.join('\n')}`);
                        } else {
                            const title = iframe.getAttribute('title') || iframe.getAttribute('name') || `iframe-${i}`;
                            result.push(`\n--- IFRAME: ${title} (Cross-origin - content not accessible) ---`);
                        }
                    } catch (e) {
                        result.push(`\n--- IFRAME ${i}: Access error - ${e.message} ---`);
                    }
                }
                
                return result.join('\n');
            }
            """
            
            iframe_result = await self.mcp_client.browser_evaluate(function=iframe_content_js)
            return str(iframe_result) if iframe_result else ""
            
        except Exception as e:
            logger.debug(f"Could not capture iframe content: {e}")
            return ""

    async def _execute_playwright_mcp_command(self, command: MCPPlayWrightLLMResponse) -> MCPResult:
        """Execute a Playwright MCP command using browser_run_code with generated locators"""
        try:
            self.logger.info(f"Executing Playwright MCP command: {command}")
            self.logger.info(f"Command tool: {getattr(command, 'tool', 'NO TOOL ATTR')}")

            if not self.mcp_client:
                return MCPResult(error=True, output="", error_message="MCP client not available")

            if not hasattr(command, 'tool') or not command.tool:
                return MCPResult(error=True, output="", error_message="Invalid command: missing or empty tool")

            self.logger.info(f"Executing MCP command: {command.tool} with input: {command.input}")

            # Fix parameter mapping for browser_evaluate BEFORE validation
            if command.tool == 'browser_evaluate' and command.input:
                # Convert 'script' parameter to 'function' parameter if needed
                if 'script' in command.input and 'function' not in command.input:
                    command.input['function'] = command.input.pop('script')
                    self.logger.info(f"🔧 Mapped 'script' to 'function' parameter for browser_evaluate")
                
                # browser_evaluate only accepts 'function' parameter, filter out others
                function_param = command.input.get('function')
                if function_param:
                    command.input = {'function': function_param}
                    self.logger.info(f"🔧 Filtered browser_evaluate params to only include 'function'")

            # Validate required parameters after parameter mapping
            validation_error = self._validate_mcp_input(command.tool, command.input)
            if validation_error:
                return MCPResult(error=True, output="", error_message=f"Input validation failed: {validation_error}")

            # If locator was generated, use browser_run_code with generated locator
            # This applies to ALL interaction/verification tools that can use locators
            locator_based_tools = [
                'browser_click',
                'browser_type',
                'browser_hover',
                'browser_select_option',
                'browser_verify_text_visible',
                'browser_verify_element_visible',
                'browser_verify_list_visible',
                'browser_verify_value',
                'browser_drag'  # Uses locators for start/end elements
            ]
            
            # Always use browser_run_code for locator-based tools (NO FALLBACK)
            if command.locator and command.tool in locator_based_tools:
                logger.info(f"🎯 Using browser_run_code with generated locator: {command.locator[:100]}...")
                playwright_code = self._convert_tool_to_playwright_code(command.tool, command.locator, command.input)
                logger.info(f"📝 Generated Playwright code: {playwright_code}")
                # Execute browser_run_code - if missing, will raise clear error
                result = await self.mcp_client.execute_tool("browser_run_code", code=playwright_code)
            else:
                # For non-locator tools (navigation, wait, etc.)
                result = await self.mcp_client.execute_tool(command.tool, **(command.input or {}))
                if command.tool == 'browser_evaluate':
                    logger.info(f" BROWSER_EVALUATE EXECUTION")
                    function_code = command.input.get('function', '') if command.input else ''
                    logger.info(f"   Raw Result: {result}")
            # Add delay
            await asyncio.sleep(2)

            # Process result
            is_error = False
            output = str(result)
            error_message = None

            if isinstance(result, dict):
                is_error = result.get('error', False) or result.get('isError', False)
                if is_error:
                    error_message = result.get('message', str(result))

            # Create result
            mcp_result = MCPResult(error=is_error, output=output, error_message=error_message)
            # self.logger.debug(f"Created MCPResult: {mcp_result}")

            return mcp_result

        except Exception as e:
            exception_str = str(e)
            self.logger.error(f"Exception in _execute_playwright_mcp_command: {type(e).__name__}")
            self.logger.error(f"Full exception message:\n{exception_str}")
            self.logger.debug(f"Contains '### Ran Playwright code': {'### Ran Playwright code' in exception_str}")
            
            # CRITICAL: Smart detection - check if action actually succeeded
            if '### Ran Playwright code' in exception_str:
                # Playwright attempted to execute - but did it succeed?
                
                # Check for element interception (click blocked)
                interception_patterns = [
                    'intercepts pointer events',
                    'intercepts pointer', 
                    'element is not visible',
                    'element not interactable',
                    'overlapped by',
                    'covered by',
                    'backdrop',
                    'modal',
                    'overlay',
                    'element.click()',  # Sometimes click failures are silent
                    'element not clickable',
                    'not attached to the DOM',
                    'click was intercepted'
                ]
                
                # Check for any interception pattern
                interception_detected = any(pattern in exception_str for pattern in interception_patterns)
                
                if interception_detected:
                    logger.warning("⚠️ Playwright tried to click but element was intercepted - click did NOT reach target")
                    logger.info("🔄 Will retry with different element strategy (label, container, etc.)")
                    return MCPResult(
                        error=True,  # Treat as failure to trigger retry
                        output=exception_str,
                        error_message="Element interception: Click was blocked by overlaying element"
                    )
                
                # Check for JavaScript evaluation errors (should be treated as failures)
                js_error_patterns = [
                    'page._evaluateFunction:',
                    'Passed function is not well-serializable',
                    'ReferenceError:',
                    'TypeError:',
                    'SyntaxError:',
                    'Error: page.evaluate',
                    'Cannot read property'
                ]
                
                js_error_detected = any(pattern in exception_str for pattern in js_error_patterns)
                
                if js_error_detected:
                    logger.error("❌ JavaScript evaluation failed - treating as error")
                    return MCPResult(
                        error=True,
                        output=exception_str,
                        error_message="JavaScript evaluation error"
                    )
                
                # Check for context destruction (usually means navigation happened = success for nav actions)
                elif 'Execution context was destroyed' in exception_str:
                    logger.info("✅ Playwright executed action - context destroyed due to navigation/new tab")
                    return MCPResult(
                        error=False,
                        output=f"Action executed successfully (navigation occurred)\n{exception_str}",
                        error_message=None
                    )
                
                # Code executed without interception issues = success
                else:
                    logger.info(f"✅ Playwright executed action successfully")
                    return MCPResult(
                        error=False,
                        output=f"Action executed successfully\n{exception_str}",
                        error_message=None
                    )
            else:
                # No code execution - action failed
                logger.error("❌ Playwright could not execute action")
                return MCPResult(error=True, output=exception_str, error_message=str(e))

    def _convert_tool_to_playwright_code(self, tool: str, locator: str, input_params: dict) -> str:
        """Convert MCP tool action to Playwright code using generated locator"""
        # CRITICAL FIX: Apply placeholder resolution to input_params BEFORE generating code
        custom_tools = self._get_shared_custom_tools()
        resolved_params = custom_tools._replace_variables(input_params, custom_tools.stored_data)
        logger.info(f"🔄 Playwright code generation - Resolved params: {resolved_params}")
        
        # Use resolved_params instead of input_params for code generation
        input_params = resolved_params
        
        # Clean locator - remove "### Result" prefix if present
        clean_locator = locator.replace('### Result', '').strip()
        
        # Parse JSON locator if it's a JSON array/object
        try:
            # Check if locator looks like JSON (starts with [ or {)
            if clean_locator.startswith('[') or clean_locator.startswith('{'):
                parsed_locator = None
                
                # Try JSON parsing first
                try:
                    parsed_locator = json.loads(clean_locator)
                except json.JSONDecodeError:
                    # If JSON fails, try ast.literal_eval for Python dict/list format
                    try:
                        parsed_locator = ast.literal_eval(clean_locator)
                    except (ValueError, SyntaxError):
                        # If both fail, try regex extraction
                        text_match = re.search(r'"text":\s*"([^"]+)"', clean_locator)
                        if text_match:
                            clean_locator = text_match.group(1).strip()
                            logger.info(f"✅ Extracted locator via regex: {clean_locator}")
                        else:
                            logger.warning(f"⚠️ Could not parse locator format: {clean_locator[:100]}...")
                
                # Handle parsed result if successful
                if parsed_locator:
                    # Handle array of locator objects
                    if isinstance(parsed_locator, list) and len(parsed_locator) > 0:
                        locator_obj = parsed_locator[0]  # Take first locator
                        if isinstance(locator_obj, dict) and 'text' in locator_obj:
                            # Extract the actual locator from the text field
                            locator_text = locator_obj['text'].strip()
                            clean_locator = locator_text
                            logger.info(f"✅ Extracted locator from list: {clean_locator}")
                    
                    # Handle single locator object
                    elif isinstance(parsed_locator, dict) and 'text' in parsed_locator:
                        locator_text = parsed_locator['text'].strip()
                        clean_locator = locator_text
                        logger.info(f"✅ Extracted locator from dict: {clean_locator}")
                        
        except Exception as e:
            # If all parsing fails, try regex as final fallback
            try:
                text_match = re.search(r'"text":\s*"([^"]+)"', clean_locator)
                if text_match:
                    clean_locator = text_match.group(1).strip()
                    logger.info(f"✅ Final fallback regex extraction: {clean_locator}")
                else:
                    logger.warning(f"⚠️ All parsing methods failed: {e}")
            except Exception:
                logger.warning(f"⚠️ Complete parsing failure for locator: {clean_locator[:50]}...")
        
        # Handle XPath locators specifically
        if clean_locator.startswith('xpath=') or '//' in clean_locator:
            if clean_locator.startswith('xpath='):
                xpath_expr = clean_locator[6:]  # Remove 'xpath=' prefix
            else:
                xpath_expr = clean_locator
            
            # Convert to page.locator() format for XPath
            playwright_locator = f"locator('xpath={xpath_expr}')"
        else:
            playwright_locator = clean_locator
        
        if tool == 'browser_click':
            return f"await page.{playwright_locator}.click();"
        
        elif tool == 'browser_type':
            text = input_params.get('text', '')
            # Escape single quotes in text to prevent JS syntax errors
            safe_text = text.replace("'", "\\'").replace('"', '\\"')
            submit = input_params.get('submit', False)
            slowly = input_params.get('slowly', False)
            
            if slowly:
                code = f"await page.{playwright_locator}.pressSequentially('{safe_text}');"
            else:
                code = f"await page.{playwright_locator}.fill('{safe_text}');"
            
            if submit:
                code += f"\nawait page.{playwright_locator}.press('Enter');"
            
            return code
        
        elif tool == 'browser_hover':
            return f"await page.{playwright_locator}.hover();"
        
        elif tool == 'browser_select_option':
            # CRITICAL FIX: Check if this is actually a radio button that should use click instead
            locator_lower = clean_locator.lower()
            if ('radio' in locator_lower or "type='radio'" in locator_lower or 
                "role='radio'" in locator_lower or "getbyrole('radio'" in locator_lower or 
                "getByRole('radio'" in locator_lower):
                # This is a radio button, use click instead of selectOption
                logger.info(f"🔧 RADIO-BUTTON-FIX: Detected radio button locator, using click() instead of selectOption()")
                return f"await page.{clean_locator}.click();"
            
            # This is a real select element, proceed with selectOption
            values = input_params.get('values', [])
            if isinstance(values, list) and values:
                value = values[0]
                # Escape single quotes in value
                safe_value = str(value).replace("'", "\\'").replace('"', '\\"')
                return f"await page.{clean_locator}.selectOption('{safe_value}');"
            return f"await page.{clean_locator}.selectOption();"
        
        elif tool == 'browser_verify_text_visible':
            # Verify text is visible using generated locator
            return f"await page.{playwright_locator}.waitFor({{ state: 'visible', timeout: 10000 }});"
        
        elif tool == 'browser_verify_element_visible':
            # Verify element is visible
            return f"await page.{playwright_locator}.waitFor({{ state: 'visible', timeout: 10000 }});"
        
        elif tool == 'browser_verify_list_visible':
            # Verify list is visible
            return f"await page.{playwright_locator}.waitFor({{ state: 'visible', timeout: 10000 }});"
        
        elif tool == 'browser_verify_value':
            # Verify element value
            value = input_params.get('value', '')
            safe_value = value.replace("'", "\\'").replace('"', '\\"')
            return f"const actualValue = await page.{playwright_locator}.inputValue(); if (actualValue !== '{safe_value}') {{ throw new Error('Value mismatch'); }};"
        
        elif tool == 'browser_drag':
            # Drag is handled differently - would need start/end locators
            # For now, return a placeholder
            return f"await page.{playwright_locator}.hover();"
        
        else:
            # Fallback - click for unknown tools
            return f"await page.{playwright_locator}.click();"

    async def _try_frame_aware_access(self, llm_response: MCPPlayWrightLLMResponse, step: str) -> Optional[MCPPlayWrightLLMResponse]:
        """Try to access element inside same-origin iframe using browser_evaluate detection + frameLocator"""
        try:
            target_ref = llm_response.input.get('ref')
            element_description = llm_response.input.get('element', '')
            
            # Step 1: Get iframe container selector from snapshot
            current_snapshot = await self.capture_page_snapshot()
            iframe_container = await self._get_iframe_container_locator(target_ref, current_snapshot)
            
            if not iframe_container:
                logger.warning("Could not extract iframe container selector")
                return None
            
            logger.info(f"📦 Found iframe container: {iframe_container}")
            
            # Step 2: Check if iframe is same-origin using browser_evaluate (RELIABLE!)
            is_same_origin = await self._is_same_origin_iframe(iframe_container)
            
            if not is_same_origin:
                logger.info("⚠️ Cross-origin iframe detected - falling back to container approach")
                return None
            
            # Step 3: We KNOW it's same-origin - generate locator for element inside
            logger.info("✅ Same-origin iframe confirmed - generating locator for element inside")
            try:
                raw_locator = await self.mcp_client.browser_generate_locator(
                    element=element_description,
                    ref=target_ref
                )
                
                if not raw_locator or '### Result' not in str(raw_locator):
                    logger.warning("browser_generate_locator returned empty")
                    return None
                
                element_locator = str(raw_locator).replace('### Result', '').strip()
                logger.info(f"✅ Generated element locator: {element_locator}")
                
            except Exception as gen_error:
                logger.warning(f"❌ Locator generation failed: {gen_error}")
                return None
            
            # Step 4: Build frameLocator code using the generated locator
            frame_code = self._build_frame_locator_code(
                tool=llm_response.tool,
                iframe_selector=iframe_container,
                element_locator=element_locator,
                input_params=llm_response.input
            )
            
            logger.info(f"🎯 Executing frame-aware code: {frame_code}")
            
            # Step 5: Execute via browser_run_code
            result = await self.mcp_client.execute_tool("browser_run_code", code=frame_code)
            
            # Step 6: Check if it succeeded
            if result and not isinstance(result, Exception):
                logger.info("✅ Same-origin iframe access succeeded")
                # Set locator for DataFrame with frameLocator notation
                llm_response.locator = f"frameLocator('{iframe_container}').{element_locator}"
                return llm_response
            else:
                logger.warning("⚠️ Frame-aware code execution failed")
                return None
                
        except Exception as e:
            logger.error(f"❌ Frame-aware access failed: {e}")
            return None
    
    async def _is_same_origin_iframe(self, iframe_selector: str) -> bool:
        """Check if iframe is same-origin using browser_evaluate"""
        try:
            # Clean the iframe_selector - remove any extra quotes
            clean_selector = iframe_selector.strip().strip('"').strip("'")
            logger.info(f"🔍 Checking iframe origin for: {clean_selector}")
            
            # Escape single quotes for JavaScript
            safe_selector = clean_selector.replace("'", "\\'")
            
            origin_check_code = f"""
            () => {{
                try {{
                    const iframe = document.querySelector('{safe_selector}');
                    if (!iframe) {{
                        console.log('Iframe not found');
                        return 'not_found';
                    }}
                    
                    // Try to access contentDocument - browser enforces same-origin policy here
                    const doc = iframe.contentDocument;
                    
                    // If we can access and it has content, it's same-origin
                    if (doc && doc.body) {{
                        console.log('Same-origin iframe detected');
                        return 'same-origin';
                    }} else {{
                        console.log('Cross-origin iframe detected (null document)');
                        return 'cross-origin';
                    }}
                }} catch (e) {{
                    // SecurityError = cross-origin
                    console.log('Cross-origin iframe detected (exception):', e.message);
                    return 'cross-origin';
                }}
            }}
            """
            
            result = await self.mcp_client.execute_tool("browser_evaluate", function=origin_check_code)
            
            # LOG RAW RESULT
            logger.info(f"📊 RAW origin check result type: {type(result)}")
            logger.info(f"📊 RAW origin check result: {repr(result)}")
            
            # Properly clean result - remove MCP markers, quotes, and extra formatting
            result_str = str(result).replace('### Result', '').replace('### Ran Playwright code', '')
            result_str = result_str.replace('"', '').replace("'", '').replace('\n', ' ')
            result_str = result_str.strip().lower()
            logger.info(f"📊 Cleaned origin check result: {result_str}")
            
            # Check for 'same-origin' in cleaned result
            is_same = 'same-origin' in result_str and 'cross-origin' not in result_str
            
            if is_same:
                logger.info("✅ Origin check: SAME-ORIGIN iframe (can access content)")
            else:
                logger.info("⚠️ Origin check: CROSS-ORIGIN iframe (cannot access content)")
            
            return is_same
            
        except Exception as e:
            logger.error(f"❌ Origin check failed: {e}")
            # Default to cross-origin (safe fallback)
            logger.info("⚠️ Defaulting to cross-origin (safe fallback)")
            return False
    
    async def _get_iframe_container_locator(self, target_ref: str, snapshot: str) -> Optional[str]:
        """Extract iframe container locator from snapshot based on target ref"""
        try:
            if not snapshot or not target_ref:
                return None
            
            # Strategy 1: Look for common iframe patterns in the snapshot text
            # Pattern: iframe "title" name="iframe-name" [ref=eXXX]
            iframe_patterns = [
                r'iframe\s+"[^"]*"\s+name="([^"]+)"',  # iframe "title" name="..."
                r'iframe\s+name="([^"]+)"',             # iframe name="..."
                r'iframe\s+id="([^"]+)"',               # iframe id="..."
                r'iframe\s+"([^"]+)".*name=',           # iframe "name" (capture title as name)
            ]
            
            for pattern in iframe_patterns:
                match = re.search(pattern, snapshot, re.IGNORECASE)
                if match:
                    attribute_value = match.group(1)
                    # Determine which attribute type was matched
                    if 'name="' in pattern:
                        logger.info(f"✅ Found iframe with name: {attribute_value}")
                        return f'iframe[name="{attribute_value}"]'
                    elif 'id="' in pattern:
                        logger.info(f"✅ Found iframe with id: {attribute_value}")
                        return f'iframe[id="{attribute_value}"]'
            
            # Strategy 2: Use browser_evaluate to find the iframe dynamically with stable selectors
            try:
                logger.info("🔍 Attempting dynamic iframe detection using browser_evaluate")
                find_iframe_code = """
                () => {
                    // Helper function to check if a string contains a UUID pattern
                    const hasUUID = (str) => {
                        return /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i.test(str);
                    };
                    
                    // Helper function to extract stable part from string with UUID
                    const getStablePart = (str) => {
                        // Remove UUID pattern and trailing spaces
                        return str.replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, '').trim();
                    };
                    
                    const iframes = document.querySelectorAll('iframe');
                    for (let i = 0; i < iframes.length; i++) {
                        const iframe = iframes[i];
                        
                        // Prioritize name (most stable)
                        if (iframe.name) {
                            // If name contains UUID, use starts-with selector
                            if (hasUUID(iframe.name)) {
                                const stablePart = getStablePart(iframe.name);
                                if (stablePart) return 'iframe[name^="' + stablePart + '"]';
                            }
                            return 'iframe[name="' + iframe.name + '"]';
                        }
                        
                        // Check title next
                        if (iframe.title) {
                            // If title contains UUID, use starts-with selector on stable part
                            if (hasUUID(iframe.title)) {
                                const stablePart = getStablePart(iframe.title);
                                if (stablePart) return 'iframe[title^="' + stablePart + '"]';
                            }
                            return 'iframe[title="' + iframe.title + '"]';
                        }
                        
                        // ID is least stable - skip if it's a pure UUID
                        if (iframe.id) {
                            // Skip if ID is a pure UUID (no stable part)
                            if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(iframe.id)) {
                                continue;
                            }
                            // If ID contains UUID but has stable parts, use starts-with
                            if (hasUUID(iframe.id)) {
                                const stablePart = getStablePart(iframe.id);
                                if (stablePart) return 'iframe[id^="' + stablePart + '"]';
                            }
                            return 'iframe[id="' + iframe.id + '"]';
                        }
                    }
                    return iframes.length > 0 ? 'iframe' : null;
                }
                """
                result = await self.mcp_client.execute_tool("browser_evaluate", function=find_iframe_code)
                
                # LOG RAW RESULT
                logger.info(f"📊 RAW browser_evaluate result type: {type(result)}")
                logger.info(f"📊 RAW browser_evaluate result: {repr(result)}")
                
                if result and 'iframe' in str(result).lower():
                    # Extract the selector from the result - with detailed logging
                    result_str = str(result)
                    logger.info(f"📊 Step 1 - String conversion: {repr(result_str[:200])}")
                    
                    # Remove MCP response formatting
                    result_str = result_str.replace('### Result', '').replace('### Ran Playwright code', '')
                    logger.info(f"📊 Step 2 - After removing MCP markers: {repr(result_str[:200])}")
                    
                    # Extract just the iframe selector using regex
                    # Look for iframe selector pattern: iframe[attribute="value"]
                    iframe_match = re.search(r'iframe\[[\w\-]+=["\']?[\w\-]+["\']?\]', result_str)
                    if iframe_match:
                        result_clean = iframe_match.group(0)
                        logger.info(f"📊 Step 3 - Regex match found: {result_clean}")
                    else:
                        # Fallback: Manual cleaning
                        # Split by newlines and find the iframe selector
                        lines = result_str.split('\n')
                        for line in lines:
                            line_stripped = line.strip().strip('"').strip("'").strip()
                            if 'iframe[' in line_stripped and ']' in line_stripped:
                                # Extract just the iframe[...] part
                                iframe_part = re.search(r'iframe\[[^\]]+\]', line_stripped)
                                if iframe_part:
                                    result_clean = iframe_part.group(0)
                                    logger.info(f"📊 Step 3 - Line extraction: {result_clean}")
                                    break
                        else:
                            # Last resort
                            result_clean = result_str.strip().strip('"').strip("'").strip()
                            logger.info(f"📊 Step 3 - Fallback strip: {result_clean}")
                    
                    logger.info(f"✅ Dynamically found iframe (final): {result_clean}")
                    return result_clean
            except Exception as e:
                logger.warning(f"Dynamic iframe detection failed: {e}")
            
            # Strategy 3: Fallback to generic iframe
            if 'iframe' in snapshot.lower():
                logger.warning("⚠️ Using generic iframe selector as last resort")
                return 'iframe'
            
            logger.warning(f"Could not find any iframe for target ref {target_ref}")
            return None
            
        except Exception as e:
            logger.error(f"Error extracting iframe locator: {e}")
            return None
        

    def _make_locator_stable(self, locator: str) -> str:
        import re
        # Dynamic value patterns
        uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        # Base64 pattern: long string of alphanumeric+/+= (typically 40+ chars)
        base64_pattern = r'[A-Za-z0-9+/]{40,}={0,2}'
        # Check if locator contains any dynamic pattern
        has_uuid = re.search(uuid_pattern, locator, re.IGNORECASE)
        has_base64 = re.search(base64_pattern, locator)
        if not has_uuid and not has_base64:
            # No dynamic patterns found, return original
            return locator
        pattern_type = []
        if has_uuid:
            pattern_type.append("UUID")
        if has_base64:
            pattern_type.append("Base64")
        
        logger.info(f"🔍 Dynamic pattern detected in locator ({', '.join(pattern_type)}), making it stable...")
        logger.info(f"   Original: {locator[:150]}...")
        
        # Pattern to match attribute selectors: element[attribute="value"]
        # Captures: element, attribute, value
        attr_pattern = r"(\w+)\[(\w+(?:-\w+)*)=['\"]([^'\"]+)['\"]\]"
        
        def replace_dynamic_in_selector(match):
            element = match.group(1)  # e.g., "iframe", "button", "div"
            attribute = match.group(2)  # e.g., "title", "id", "data-testid", "name"
            value = match.group(3)  # e.g., "__zoid__paypal_buttons__eyJzZW5k..."
            
            # Check if value contains UUID
            if re.search(uuid_pattern, value, re.IGNORECASE):
                # Keep first 8 characters of UUID for uniqueness, remove the rest
                uuid_match = re.search(uuid_pattern, value, re.IGNORECASE)
                if uuid_match:
                    uuid_value = uuid_match.group(0)
                    uuid_first_8 = uuid_value[:8]  # Keep first 8 characters
                    # Replace the full UUID with just first 8 characters
                    stable_value = re.sub(uuid_pattern, uuid_first_8, value, flags=re.IGNORECASE)
                else:
                    # Fallback: remove UUID completely if match fails
                    stable_value = re.sub(uuid_pattern, '', value, flags=re.IGNORECASE).strip()
                
                if stable_value:
                    stable_selector = f'{element}[{attribute}^="{stable_value}"]'
                    logger.info(f"   ✅ {element}[{attribute}] → Using ^= (UUID truncated to 8 chars), stable part: '{stable_value[:50]}...'")
                    return stable_selector
            
            # Check if value contains base64 (long encoded string)
            if re.search(base64_pattern, value):
                # Remove base64 and trailing/leading spaces
                stable_value = re.sub(base64_pattern, '', value).strip()
                
                # Remove trailing underscores often left after base64 removal
                stable_value = stable_value.rstrip('_')
                
                if stable_value:
                    stable_selector = f'{element}[{attribute}^="{stable_value}"]'
                    logger.info(f"   ✅ {element}[{attribute}] → Using ^= (removed Base64), stable part: '{stable_value}'")
                    return stable_selector
                else:
                    # No stable prefix, keep original
                    logger.warning(f"   ⚠️ {element}[{attribute}] has only Base64 data, keeping original")
                    return match.group(0)           
            # NEW: Check for any other long dynamic identifiers (alphanumeric strings > 12 chars)
            # This handles non-UUID dynamic parts like session IDs, tokens, etc.
            long_identifier_pattern = r'[a-zA-Z0-9]{13,}'  # 13+ chars alphanumeric (longer than typical words)
            long_matches = re.findall(long_identifier_pattern, value)
            
            if long_matches:
                logger.info(f"   🔍 Found long identifier(s) in value: {[match[:8] + '...' for match in long_matches]}")
                stable_value = value
                for long_match in long_matches:
                    # Replace each long identifier with its first 8 characters
                    truncated = long_match[:8]
                    stable_value = stable_value.replace(long_match, truncated)
                    logger.info(f"   ✂️ Truncated '{long_match}' → '{truncated}'")
                
                if stable_value != value:
                    stable_selector = f'{element}[{attribute}^="{stable_value}"]'
                    logger.info(f"   ✅ {element}[{attribute}] → Using ^= (long IDs truncated to 8 chars), stable part: '{stable_value}'")
                    return stable_selector
            
            # No dynamic pattern in this attribute, keep original
            return match.group(0)       
        # Replace all attribute selectors that contain dynamic patterns
        stable_locator = re.sub(attr_pattern, replace_dynamic_in_selector, locator)
        
        if stable_locator != locator:
            logger.info(f"   ✅ Stable: {stable_locator[:150]}...")
        else:
            logger.info(f"   ℹ️ No changes made (dynamic pattern detected but not in attribute selectors)")
        
        return stable_locator
    

    def _checkDynamicLocator(self, locator: str) -> str:    
        import re
        uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        base64_pattern = r'[A-Za-z0-9+/]{40,}={0,2}'
        has_uuid = re.search(uuid_pattern, locator, re.IGNORECASE)
        has_base64 = re.search(base64_pattern, locator)
        if not has_uuid and not has_base64:
            return locator
        else:
            return True
    
    
    def _build_frame_locator_code(self, tool: str, iframe_selector: str,
                                  element_locator: str, input_params: dict) -> str:
        """Build frameLocator code using browser_generate_locator output"""
        
        # The element_locator is already in Playwright format from browser_generate_locator
        # e.g., "getByRole('button', { name: 'Account' })"
        
        if tool == 'browser_click':
            return f"await page.frameLocator('{iframe_selector}').{element_locator}.click();"
        
        elif tool == 'browser_type':
            text = input_params.get('text', '')
            safe_text = text.replace("'", "\\'").replace('"', '\\"')
            submit = input_params.get('submit', False)
            slowly = input_params.get('slowly', False)
            
            if slowly:
                code = f"await page.frameLocator('{iframe_selector}').{element_locator}.pressSequentially('{safe_text}');"
            else:
                code = f"await page.frameLocator('{iframe_selector}').{element_locator}.fill('{safe_text}');"
            
            if submit:
                code += f"\nawait page.frameLocator('{iframe_selector}').{element_locator}.press('Enter');"
            
            return code
        
        elif tool == 'browser_hover':
            return f"await page.frameLocator('{iframe_selector}').{element_locator}.hover();"
        
        elif tool == 'browser_select_option':
            # CRITICAL FIX: Check if this is actually a radio button that should use click instead
            locator_lower = element_locator.lower()
            if ('radio' in locator_lower or "type='radio'" in locator_lower or 
                "role='radio'" in locator_lower or "getbyrole('radio'" in locator_lower or 
                "getByRole('radio'" in locator_lower):
                # This is a radio button, use click instead of selectOption
                logger.info(f"🔧 RADIO-BUTTON-FIX-IFRAME: Detected radio button locator in iframe, using click() instead of selectOption()")
                return f"await page.frameLocator('{iframe_selector}').{element_locator}.click();"
            
            # This is a real select element, proceed with selectOption
            values = input_params.get('values', [])
            if isinstance(values, list) and values:
                value = values[0]
                safe_value = str(value).replace("'", "\\'").replace('"', '\\"')
                return f"await page.frameLocator('{iframe_selector}').{element_locator}.selectOption('{safe_value}');"
            return f"await page.frameLocator('{iframe_selector}').{element_locator}.selectOption();"
        
        else:
            # Fallback
            return f"await page.frameLocator('{iframe_selector}').{element_locator}.click();"

    # ========== RECTIFICATION METHODS ==========

    async def _rectify_and_execute_step(
            self,
            step: ScenarioStep,
            llm_response: MCPPlayWrightLLMResponse,
            mcp_response: MCPResult,
            step_index: Optional[int] = None,
            step_lst: Optional[List[ScenarioStep]] = None
    ) -> MCPPlayWrightLLMResponse:
        """Rectify and execute failed step with intelligent retry mechanism"""
        locators_tried = []
        new_llm_response = llm_response
        max_retries = 10  # Increased from 3 to 10 to allow more retries

        for i in range(max_retries):
            self.logger.info(f"🔄 Retry attempt {i + 1}/{max_retries} for step: {step}")

            # Track the failed input to avoid repeating
            failed_input = str(new_llm_response.input) if new_llm_response.input else "unknown input"
            locators_tried.append(failed_input)

            try:
                # Capture fresh page state for each retry
                current_page_snapshot = await self.capture_page_snapshot()

                # Analyze the error context
                error_context_analysis = await self._get_error_analysis(
                    current_page_snapshot, new_llm_response, mcp_response
                )

                # Build comprehensive rectification prompt
                rectification_message = await self._load_and_format_rectification_prompt(
                    retry_attempt=i + 1,
                    step=step,
                    failed_llm_response=new_llm_response,
                    mcp_error_response=mcp_response,
                    current_page_snapshot=current_page_snapshot,
                    locators_tried=locators_tried,
                    error_context_analysis=error_context_analysis
                )

                # Get rectified LLM instructions using specialized system prompt
                new_llm_response = await self._get_rectification_llm_instructions(
                    rectification_message, step
                )

                # CRITICAL: Apply step verification to rectified response to prevent wrong actions
                self.logger.info("🔍 RECTIFICATION STEP VERIFICATION:")
                self.logger.info(f"  - Original Step: '{step}'")
                self.logger.info(f"  - Rectified Tool: '{new_llm_response.tool}'")
                self.logger.info(f"  - Rectified Input: {new_llm_response.input}")
                
                if not PLATFORM_CONFIG.get('soft_validation'):
                    validation_result = self.step_verifier.validate_llm_response(
                        original_step=step,
                        llm_response=new_llm_response
                    )
                    if not validation_result.is_valid:
                        # Surface detailed reason if available
                        self.step_verifier.log_validation_details(validation_result, logger=self.logger)
                        error_msg = f"❌ RECTIFICATION BLOCKED - Step verification failed: {validation_result.validation_details}"
                        blocked_scenario = f"BLOCKED SCENARIO: Step '{step}' was prevented from executing '{new_llm_response.tool}' with element '{new_llm_response.input.get('element', 'N/A')}'"
                        self.logger.error(error_msg)
                        self.logger.error(f"  - {blocked_scenario}")
                        self.logger.error("  - This is exactly the type of mismatch we needed to prevent!")
                        self.logger.error(f"{error_msg} | {blocked_scenario}")
                        # Continue to next retry iteration instead of executing invalid rectification
                        continue
                # Generate locator just before execution for DataFrame storage
                if (new_llm_response.input and 'element' in new_llm_response.input and 'ref' in new_llm_response.input):
                    element_text = new_llm_response.input['element']
                    
                    # Check if element is already an XPath expression - skip MCP generation
                    if element_text.startswith('xpath=') or element_text.startswith('//'):
                        # Use XPath directly 
                        if element_text.startswith('xpath='):
                            xpath_expr = element_text[6:]  # Remove 'xpath=' prefix
                        else:
                            xpath_expr = element_text
                        new_llm_response.locator = f"locator('xpath={xpath_expr}')"
                        logger.info(f"🎯 Using direct XPath locator: {new_llm_response.locator}")
                    else:
                        # Use normal MCP locator generation for semantic elements
                        try:
                            raw_locator = await self.mcp_client.browser_generate_locator(
                                element=new_llm_response.input['element'],
                                ref=new_llm_response.input['ref']
                            )
                            if raw_locator:
                                new_llm_response.locator = str(raw_locator).replace('### Result', '').strip()
                                logger.info(f"✅ Generated locator for DataFrame: {new_llm_response.locator}")
                            else:
                                # No fallback - if MCP locator generation fails, step fails
                                logger.error(f"❌ MCP locator generation failed for element: {new_llm_response.input['element']}")
                                raise RuntimeError(f"MCP locator generation failed - no fallback allowed")
                        except Exception as e:
                            logger.error(f"❌ Failed to generate locator for DataFrame: {e}")
                            # No fallback - if MCP locator generation fails, step fails
                            raise RuntimeError(f"MCP locator generation failed - no fallback allowed: {e}")

                # Execute the rectified command (with custom tool check for mocking, API, etc.)
                if await self._is_custom_tool(new_llm_response.tool):
                    mcp_response = await self._execute_custom_tool(new_llm_response)
                else:
                    mcp_response = await self._execute_playwright_mcp_command(new_llm_response)
                if not mcp_response.error and PLATFORM_CONFIG.get('soft_validation'):
                    await self._perform_next_step_validation_in_rectification(mcp_response, step_index, step_lst, step,current_page_snapshot)
                if mcp_response.inconsistent:
                    logger.error(f"❌ MCP response marked as inconsistent - stopping execution for step: {step}")
                    raise RuntimeError(
                        f"MCP Process Terminated: Inconsistent response detected for step '{step}'\n"
                        f"Validation Error: {mcp_response.error_message}\n"
                        f"The page validation failed multiple times, indicating the page is not ready or stable.\n"
                        f"Process cannot continue due to page inconsistency.' {mcp_response.error_message}"
                    )
                if not mcp_response.error:
                    self.logger.info(f"✅ Step succeeded after {i + 1} retries: {step}")
                    break
                else:
                    self.logger.warning(f"⚠️ Attempt {i + 1} failed: {mcp_response.error_message}")

            except RuntimeError as runtime_error:
                self.logger.error(f"❌ soft validation failure on retry attempt {i + 1}: {runtime_error}")
                break
            except json.JSONDecodeError as json_error:
                self.logger.error(f"❌ JSON parsing error on retry attempt {i + 1}: {json_error}")
                # Continue to next iteration (i will be incremented by for loop)
            except Exception as e:
                self.logger.error(f"❌ Error on retry attempt {i + 1}: {e}")
                # Continue to next iteration (i will be incremented by for loop)

        # Check final result after all retries - STOP PROCESS IF ALL RETRIES FAILED
        if mcp_response.error:
            error_msg = f"❌ CRITICAL FAILURE: Step '{step}' failed after {max_retries} retries. Final error: {mcp_response.error_message}"
            self.logger.error(error_msg)
            
            # Create detailed error context for debugging
            error_context = {
                "failed_step": step,
                "max_retries_attempted": max_retries,
                "final_error": mcp_response.error_message,
                "locators_tried": locators_tried,
                "retry_history": [f"Attempt {i+1}: {loc}" for i, loc in enumerate(locators_tried)]
            }
            
            # Log retry history only once here
            self.logger.error(f"❌ RETRY HISTORY:\n{json.dumps(error_context['retry_history'], indent=2)}")
            
            # STOP THE ENTIRE PROCESS - DO NOT CONTINUE TO NEXT STEP
            mcp_error = RuntimeError(
                f"MCP Step Execution Failed: {error_msg}\n"
                f"Max Retries: {max_retries} | Final Error: {mcp_response.error_message}\n"
                f"Retry History: {json.dumps(error_context['retry_history'])}\n"
                f"This is a critical failure that prevents further execution."
            )
            # Attach the failed step to the exception for error_formatter.py
            setattr(mcp_error, "step", str(step))
            raise mcp_error

        return new_llm_response

    async def _perform_next_step_validation_in_rectification(self, mcp_response: MCPResult, step_index: int, step_lst: list, step: str, previous_page_snapshot: str) -> None:
        """Perform next step validation during rectification with retry logic and RuntimeError on failure"""
        max_validation_attempts = 2
        
        api_keywords = ['invokes','tab', 'stores', 'browser' ,'bearer', 'header', 'api', 'service', 'request', 'response', 'navigates']
        if step_index is not None and step_lst and step_index + 1 < len(step_lst):
            next_step = step_lst[step_index + 1]
            next_step_lower = next_step.lower()
            if any(keyword in next_step_lower for keyword in api_keywords):
                logger.info(f"🔄 Skipping next step validation for API-related next step: {next_step}")
                return
        
        for attempt in range(1, max_validation_attempts + 1):
            try:  
                logger.info(f"🔄 Rectification validation attempt {attempt}/{max_validation_attempts}")
                await asyncio.sleep(4)  # Small delay to ensure page state is stable                
                current_page_snapshot = await self.capture_page_snapshot()
                current_step_index = step_index if step_index is not None else 0
                total_steps = len(step_lst) if step_lst is not None else 1
                validation_result, failure_reason = await self.step_verifier._llm_validate_next_step_readiness(
                    current_step_index=current_step_index, 
                    total_steps=total_steps,  
                    step_list=step_lst or [step],
                    page_snapshot=current_page_snapshot,  
                    previous_snapshot=previous_page_snapshot,
                    mcp_response=mcp_response                
                )
                
                if validation_result:
                    logger.info(f"✅ Next step validation passed in rectification on attempt {attempt}")
                    return  # Break out of loop and method when validation succeeds
                else:
                    next_step = step_lst[current_step_index + 1] if current_step_index + 1 < len(step_lst) else "N/A"
                    if attempt < max_validation_attempts:
                        logger.warning(f"⚠️ Rectification validation failed on attempt {attempt}: {next_step} - {failure_reason}")
                        continue
                    else:
                        # Final attempt failed
                        logger.warning(f"❌ Rectification validation failed after {max_validation_attempts} attempts: {next_step} - {failure_reason}")
                        mcp_response.inconsistent = True
                        mcp_response.error_message = f"Next step validation failed after {max_validation_attempts} attempts - page may not be ready for the step as page element or object may not be ready for next step: {next_step} - {failure_reason}"                      
                        raise RuntimeError(
                            f"MCP Step Validation Failed: Step '{step}' failed validation after {max_validation_attempts} retry attempts.\n"
                            f"Validation Error: Page is not ready to execute --'{next_step}' \n"
                            f"Failure Reason: {failure_reason}\n"
                            f"The current steps has not given the expected state for the next step to proceed.\n"
                            f"This indicates the LLM response doesn't match the intended step action."
                        )
                        
            except Exception as validation_error:
                next_step = step_lst[step_index + 1] if step_index + 1 < len(step_lst) else "N/A"
                if attempt < max_validation_attempts:
                    logger.warning(f"⚠️ Rectification validation exception on attempt {attempt}: {validation_error}")
                    continue
                else:
                    # Final attempt failed with exception
                    mcp_response.inconsistent = True
                    logger.warning(f"❌ Rectification validation failed with exception after {max_validation_attempts} attempts: {validation_error}")
                    raise RuntimeError(
                        f"This indicates the LLM response doesn't match the intended step action with the exception message after {max_validation_attempts} attempts: {str(validation_error)}"
                    )

    async def _get_page_analysis(self, snapshot: str) -> str:
        """Equivalent to Java's getPageAnalyzeResponse() - Analyze page snapshot using template"""
        try:
            # Replace template placeholder
            if not snapshot:
                raise ValueError("Snapshot is required for page analysis")
            formatted_prompt = self.page_analysis_template.replace("{snapshot}", snapshot)

            # Call LLM for analysis
            analysis_result = await self._call_llm_for_analysis(formatted_prompt)
            self.logger.info(f"Page analysis result:\n{analysis_result}")
            return analysis_result

        except Exception as e:
            self.logger.error(f"Page analysis failed: {e}")
            return "Page analysis unavailable due to error"

    async def _get_error_analysis(
            self,
            page_snapshot: str,
            failed_response: MCPPlayWrightLLMResponse,
            error_result: MCPResult
    ) -> str:
        """Equivalent to Java's getErrorAnalyzeResponse() - Analyze error using template"""
        try:
            # Replace template placeholders
            if not error_result.error_message:
                raise ValueError("Error result message is required")
            if not page_snapshot:
                raise ValueError("Page snapshot is required")
            if not failed_response.input:
                raise ValueError("Failed response input is required")
            if not failed_response.step:
                raise ValueError("Failed response step is required")
            
            # Check if the failed element might be in an iframe
            iframe_container = None
            iframe_hint = ""
            
            # For text verification, detect iframe by analyzing snapshot for iframe elements
            if failed_response.tool in ['browser_verify_text_visible', 'browser_verify_element_visible']:
                if '<iframe' in page_snapshot.lower():
                    # Detect common iframe patterns
                    iframe_patterns = ['enactment-iframe', 'payment-iframe', 'paypal', 'stripe', 'login-iframe', 'auth-iframe']
                    for pattern in iframe_patterns:
                        if pattern in page_snapshot.lower():
                            iframe_hint = f" IFRAME DETECTED: Page contains '{pattern}' iframe. Text might be inside cross-origin iframe which requires special handling."
                            self.logger.warning(f"⚠️ Text verification failed - iframe '{pattern}' detected in page snapshot")
                            break
            
            # For element-based actions, use ref-based detection
            if failed_response.input and 'ref' in failed_response.input:
                iframe_container = await self._detect_and_redirect_iframe_element(
                    failed_response.input.get('ref'),
                    failed_response.input.get('element'),
                    page_snapshot
                )
            
            formatted_prompt = self.error_analysis_template.replace(
                "{errorsnapshot}", error_result.error_message
            ).replace(
                "{pagesnapshot}", page_snapshot
            ).replace(
                "{previousLocator}", str(failed_response.input)
            ).replace(
                "{step}", failed_response.step
            )
            
            # Add iframe detection hint if detected
            if iframe_container:
                formatted_prompt += (
                    f"\n\nIMPORTANT IFRAME DETECTION: "
                    f"The element is inside a cross-origin iframe and cannot be accessed. "
                    f"Target the iframe CONTAINER instead: "
                    f"Element: '{iframe_container['element']}', Ref: '{iframe_container['ref']}'"
                )
            elif iframe_hint:
                # Add iframe hint for text verification
                formatted_prompt += f"\n\n{iframe_hint} The MCP server can access iframe content automatically, so keep the same text verification approach."

            # Call LLM for error analysis
            analysis_result = await self._call_llm_for_analysis(formatted_prompt)
            self.logger.info(f"Error analysis result:\n{analysis_result}")
            return analysis_result

        except Exception as e:
            self.logger.warning(f"Error analysis failed: {e}")
            return f"Could not analyze error. Original error: {error_result.error_message}"
    
    async def _detect_and_redirect_iframe_element(self, target_ref: str, target_element: str, snapshot: str) -> dict:
        """
        Detect if the target element is inside an iframe and return the container element instead
        Returns: {'element': 'container description', 'ref': 'container_ref'} or None
        """
        try:
            if not snapshot or not target_ref:
                return None
            
            # Quick check: if ref starts with 'f', it's likely inside an iframe
            # Main page refs start with 'e', iframe content refs start with 'f'
            if target_ref.startswith('f'):
                logger.warning(f"⚠️ Detected iframe element ref '{target_ref}' (starts with 'f')")
                
                # Parse snapshot to find the iframe container
                lines = snapshot.split('\n')
                iframe_container_ref = None
                
                for i, line in enumerate(lines):
                    line_stripped = line.strip()
                    
                    # Look for iframe declaration with [active] or main iframe
                    if 'iframe' in line_stripped.lower() and '[ref=' in line_stripped:
                        import re
                        ref_match = re.search(r'\[ref=([a-zA-Z0-9]+)\]', line_stripped)
                        if ref_match:
                            potential_iframe_ref = ref_match.group(1)
                            
                            # Check if our target ref appears after this iframe (indented)
                            # by looking at subsequent lines
                            for j in range(i+1, min(i+20, len(lines))):
                                if target_ref in lines[j]:
                                    # Found our target under this iframe
                                    # Now find the iframe's DIRECT parent (one level up in indentation)
                                    iframe_line_indent = len(lines[i]) - len(lines[i].lstrip())
                                    
                                    # Look backwards for parent with less indentation
                                    for k in range(i-1, max(0, i-10), -1):  # Increased search range to 10 lines
                                        parent_line = lines[k]
                                        parent_line_stripped = parent_line.strip()
                                        parent_indent = len(parent_line) - len(parent_line.lstrip())
                                        
                                        # Parent must have less indentation and contain a ref
                                        # CRITICAL: Must NOT be an iframe itself, and should be a clickable element
                                        if parent_indent < iframe_line_indent and '[ref=' in parent_line_stripped and 'iframe' not in parent_line_stripped.lower():
                                            parent_match = re.search(r'\[ref=([a-zA-Z0-9]+)\]', parent_line_stripped)
                                            if parent_match:
                                                container_ref = parent_match.group(1)
                                                # Extract container description (clean it up)
                                                container_desc = parent_line_stripped.split('[ref=')[0].strip()
                                                if container_desc.startswith('- '):
                                                    container_desc = container_desc[2:]  # Remove leading "- "
                                                
                                                # Verify it's a clickable element type (not just a div)
                                                clickable_types = ['button', 'link', 'group', 'textbox', 'combobox', 'checkbox', 'radio']
                                                is_clickable = any(ctype in container_desc.lower() for ctype in clickable_types)
                                                
                                                if is_clickable or 'data-testid' in parent_line_stripped or 'id=' in parent_line_stripped:
                                                    logger.info(f"✅ Found clickable iframe parent container: {container_desc} [ref={container_ref}]")
                                                    return {
                                                        'element': container_desc or 'iframe container',
                                                        'ref': container_ref
                                                    }
                                    
                                    # CRITICAL FIX: If no proper clickable container found, return None
                                    # This will cause the system to use the original element or retry with LLM
                                    logger.warning(f"⚠️ Could not find clickable container for cross-origin iframe element")
                                    logger.warning(f"⚠️ Returning None to trigger retry mechanism with alternative element selection")
                                    return None
            
            return None
            
        except Exception as e:
            logger.error(f"Error detecting iframe element: {e}")
            return None
    
    def _detect_iframe_issue(self, error_message: str, step: str, snapshot: str) -> str:
        """Detect if the error is related to cross-origin iframe access"""
        # Check error message for iframe-related issues
        iframe_error_indicators = [
            'cross-origin',
            'different origin',
            'iframe',
            'frame',
            'blocked by CORS',
            'not accessible'
        ]
        
        error_has_iframe_indicator = any(indicator in error_message.lower() for indicator in iframe_error_indicators)
        
        # Check snapshot for iframe elements (generic - ANY iframe)
        snapshot_has_iframe = '<iframe' in snapshot.lower() or 'iframe' in snapshot.lower()
        
        if error_has_iframe_indicator or snapshot_has_iframe:
            return (
                "This appears to be a CROSS-ORIGIN IFRAME issue. "
                "The element is likely inside a cross-origin iframe (payment button, social login, embedded widget, or any external content). "
                "SOLUTION: Target the iframe CONTAINER element (parent wrapper) instead. "
                "Look for the parent/wrapper div that contains the iframe using data-testid, id, or class attributes. "
                "This applies to ANY cross-origin iframe regardless of content type."
            )
        
        return ""

    async def _load_and_format_rectification_prompt(
            self,
            retry_attempt: int,
            step: ScenarioStep,
            failed_llm_response: MCPPlayWrightLLMResponse,
            mcp_error_response: MCPResult,
            current_page_snapshot: str,
            locators_tried: List[str],
            error_context_analysis: str
    ) -> str:
        """Equivalent to Java's loadAndFormatRectificationPrompt() - Create comprehensive rectification prompt"""
        try:
            # Convert objects to strings (like Java version)
            if not failed_llm_response.input:
                raise ValueError("Failed LLM response input is required")
            if not mcp_error_response.error_message:
                raise ValueError("MCP error response message is required")
            if not step:
                raise ValueError("Step is required")
            if not failed_llm_response.tool:
                raise ValueError("Failed LLM response tool is required")
            if not current_page_snapshot:
                raise ValueError("Current page snapshot is required")
            
            failed_input_str = str(failed_llm_response.input)
            error_message_str = mcp_error_response.error_message
            failed_step_str = step
            failed_tool_str = failed_llm_response.tool
            failed_inputs_str = ", ".join(locators_tried)
            snapshot_str = current_page_snapshot

            # Debug logging (like Java version)
            self.logger.info("Template Variables (direct format):")
            self.logger.info(f"  attemptNumber = {retry_attempt}")
            self.logger.info(f"  failedStep = {failed_step_str}")
            self.logger.info(f"  failedInput = {failed_input_str}")
            # self.logger.info(f"  errorMessage = {error_message_str}")
            self.logger.info(f"  errorMessage = #Commented Out")
            self.logger.info(f"  failedTool = {failed_tool_str}")
            self.logger.info(f"  failedInputs = {failed_inputs_str}")

            # Detect if this is an interception error
            is_interception_error = any(indicator in error_message_str.lower() for indicator in [
                'intercepts pointer events', 'intercepts pointer', 'backdrop', 'overlay', 
                'intercepted', 'blocked by', 'covers', 'obscured'
            ])
            
            # Check if we've already tried semantic locators and they keep failing
            semantic_retries = sum(1 for input_str in failed_inputs_str if 'container' in input_str.lower() or 'button' in input_str.lower())
            needs_xpath_strategy = semantic_retries >= 2  # After 2+ semantic failures, suggest XPath
            
            # Track text verification retries specifically
            is_text_verification = failed_tool_str in ['browser_verify_text_visible', 'browser_verify_element_visible']
            text_retry_count = len(locators_tried) if is_text_verification else 0
            
            interception_guidance = ""
            
            # Add text verification specific guidance
            if is_text_verification and text_retry_count >= 1:
                interception_guidance = f"""

**🔍 TEXT VERIFICATION FAILURE - PROGRESSIVE RETRY STRATEGY 🔍**
Attempt #{retry_attempt} of text verification has failed.

**CRITICAL RULES FOR TEXT VERIFICATION:**
1. **NEVER CHANGE THE EXPECTED TEXT** - The step expects specific text: "{failed_step_str}"
2. **DO NOT verify different text** that exists on the page - that defeats the purpose of verification
3. **The original expected text MUST be preserved** in your rectified response

**PROGRESSIVE STRATEGIES (attempt #{retry_attempt}):**

**Attempt 1-2: Check if text is inside an IFRAME**
- Look for <iframe elements in the page snapshot
- Common iframe indicators: "enactment-iframe", "payment-iframe", "login-iframe", "paypal", "stripe"
- **SOLUTION**: If text might be in iframe, suggest scrolling or waiting, but KEEP SAME TEXT
- Note: MCP server handles iframe content access automatically

**Attempt 3-4: Try PARTIAL TEXT MATCH**
- If the exact expected text from the step (for example: "{failed_step_str}") is not found
- Try partial match: Extract key words or key phrases from that expected text
- Example: focus on important nouns or verbs from the expected text while still verifying the full original text
- But include this in error analysis only - DO NOT change the verification text

**Attempt 5+: WAIT AND SCROLL STRATEGIES**
- Text might appear after page loads completely
- Suggest: Add browser_wait_for before verification
- Suggest: Scroll page down to reveal text
- But ALWAYS keep verifying the original expected text

**ERROR CONTEXT ANALYSIS SHOWS**: {error_context_analysis}

**YOUR TASK**:
- Return browser_verify_text_visible with THE EXACT SAME TEXT from the original step
- DO NOT change to different text even if you see it in the page snapshot
- If text truly doesn't exist, the verification SHOULD fail - that's a legitimate test failure
"""
            elif is_interception_error:
                if needs_xpath_strategy:
                    interception_guidance = f"""

**🚨 REPEATED INTERCEPTION DETECTED 🚨**
Semantic locators have failed {semantic_retries} times due to overlay interception.
**ESCALATE TO XPATH STRATEGY** to bypass overlay issues:

**CRITICAL INSTRUCTIONS:**
1. **DO NOT repeat semantic locators** - they keep getting intercepted
2. **USE DIRECT XPATH TARGETING**: Create precise XPath based on the HTML structure
3. **XPATH STRATEGY**: Use xpath=//div[@data-testid="..."]//button[position()] format

**Guidelines for XPath creation:**
- Analyze the HTML structure from the page snapshot
- Use data-testid attributes when available: xpath=//div[@data-testid="container"]//button[1]
- Target specific positions: xpath=//div[3]/button[2] for precise targeting
- Avoid text-based selectors if they're being intercepted
- Use element position when semantic matching fails

**Target Element from Failed Step**: "{failed_step_str}"
**Create XPath that precisely targets the same element but bypasses overlays.**
"""
                else:
                    interception_guidance = f"""

**🚨 ELEMENT INTERCEPTION DETECTED 🚨**
The error indicates the target element exists but is blocked by overlaying elements.
**STRICT REQUIREMENT**: Do NOT suggest clicking different elements (like "Close button"). 
**MAINTAIN THE SAME TARGET ELEMENT** but use these interception bypass strategies:

1. **Parent Container Strategy**: Target the clickable parent/container of the same element  
2. **JavaScript Click**: Use direct JavaScript click via browser_evaluate 
3. **Wait Strategy**: Wait for overlays to disappear before clicking
4. **Scroll Strategy**: Scroll the target element into view first
5. **XPath Strategy**: If semantic locators fail repeatedly, use direct XPath targeting

**Target Element from Failed Step**: "{failed_step_str}"
**Keep targeting the same element concept, just use different locator strategies!**
"""

            # Complete rectification prompt (matching Java exactly)
            rectification_prompt = f"""Your task is to provide a **SINGLE, VALID JSON object** containing a corrected automation step to resolve a failed web page action which has been failed in previous step.
Previous steps has been executed with the help on Playwright MCP server and failed. Generate correct locator and code snippet based on the analysis of the error and current page snapshot.
The rectified locators will be executed via Playwright MCP server. Follow the structure and rules below strictly.
{interception_guidance}

**FAILED STEP CONTEXT:** 
Use the following data to inform your correction:
* **ATTEMPT NUMBER:** {retry_attempt}
* **FAILED STEP:** `{failed_step_str}` (A brief description of the intended action, e.g., "Click the checkout button")
* **FAILED TOOL:** `{failed_tool_str}` (e.g., `browser_click`, `browser_type`, `browser_select_option`)
* **FAILED INPUTS:** `{failed_inputs_str}` (The input parameters that failed in previous attempts)
* **FAILED INPUT STRING:** `{failed_input_str}` (The string used in the failed action, if applicable)
* **ERROR TYPE:** `{error_message_str}` (The complete Playwright MCP response after failure)
* **VALIDATION REQUIREMENTS:** Ensure all required parameters are included based on the tool being used
* **CURRENT PAGE STATE (HTML Snapshot/Relevant Snippet):**  `{snapshot_str}`
* **ERROR CONTEXT ANALYZER:** `{error_context_analysis}` (The analysis of the last error with the page context)

**CRITICAL INPUT PARAMETER REQUIREMENTS:**
- browser_navigate: input MUST contain "url" field ONLY (extract URL from step text)
- browser_click: input MUST contain "element" and "ref" fields
- browser_type: input MUST contain "element", "ref", and "text" fields
- browser_select_option: input MUST contain "element", "ref", and "values" array fields

**AVAILABLE MCP TOOLS (use ONLY these):**
- browser_click: Click on elements
- browser_type: Type text into input fields
- browser_select_option: Select dropdown options (only for standard HTML select elements)
- browser_navigate: Navigate to URLs
- browser_navigate_back: Go back to previous page
- browser_navigate_forward: Go forward to next page
- browser_hover: Hover over elements
- browser_press_key: Press keyboard keys
- browser_wait_for: Wait for conditions
- browser_close: Close the page
- browser_resize: Resize browser window
- browser_console_messages: Get console messages
- browser_handle_dialog: Handle dialogs
- browser_evaluate: Execute JavaScript
- browser_file_upload: Upload files
- browser_install: Install browser
- browser_take_screenshot: Take screenshots
- browser_snapshot: Capture page snapshot
- browser_drag: Drag and drop elements
- browser_tab_list: List browser tabs
- browser_tab_new: Open new tab
- browser_tabs: Manage tabs (list, create, close, select)
- browser_tab_close: Close tab
- browser_network_requests: Get network requests
- browser_pdf_save: Save page as PDF

**STRICT RULES:**
1. **ONLY USE TOOLS** from the AVAILABLE MCP TOOLS list above. DO NOT use tools like 'fill', 'selectOption', 'click', 'type' - these don't exist.
2. **LOCATOR STRATEGIES AVAILABLE:**
   - Semantic locators: element="Button text", ref="e123" (preferred for readability)
   - XPath locators: Use direct XPath expressions for precise targeting when semantic locators fail
   - Examples: xpath=//div[@data-testid="smartshelf-actions"]/div[3]/button[2]
3. **DO NOT** repeat any input parameters that have already been tried in previous attempts and mentioned in **FAILED INPUTS**. Analyse the value present **ERROR TYPE** and **CURRENT PAGE STATE (HTML Snapshot/Relevant Snippet):** above and always create new element/ref combinations based on the analysis.
4. Follow the analysis done in ERROR CONTEXT ANALYZER and do not repeat the same mistake in case of selecting locators.
5. **CRITICAL FOR TEXT VERIFICATION**: If tool is browser_verify_text_visible, you MUST keep the exact same text from the original failed step - DO NOT change it to different text you see on the page. Changing verification text defeats the test purpose.
6. Scroll the page if required to make the element visible.
7. After getting any of the below errors, use the given strategy:	
   - if any web element(like button/Radio button/Input/Hyperlink etc.) click failed due to element interception, try the clickable container/label area instead and use locator strategy to click the clickable container/label area                            
   - if fill or type into input edit box failed due to element locator not matching ,then try to identify parent locator or the text matching in parent container and then use fill or type into input edit box.
5. **ELEMENT INTERCEPTION HANDLING (PROGRESSIVE):**
   - If the error mentions "intercepts pointer events", "intercepted by", "backdrop", or "overlay", DO NOT change the target element
   - KEEP the same target element but use these strategies IN PROGRESSIVE ORDER:
     a) **First attempt**: Use parent container (wrapper div/section) of the target element
     b) **Second attempt**: Use JavaScript click: browser_evaluate with "await page.locator('target').click()"
     c) **Third attempt**: Wait for overlay to disappear with browser_wait_for before clicking
     d) **Fourth attempt**: Try scrolling first with browser_evaluate: "await page.locator('target').scrollIntoViewIfNeeded()"
     e) **Fifth+ attempts**: **ESCALATE TO XPATH** - Use precise XPath targeting: xpath=//div[@data-testid="container"]//button[position()]
   - **XPath Strategy Guidelines:**
     * Analyze HTML structure from page snapshot
     * Use data-testid when available: xpath=//div[@data-testid="actions"]//button[2]
     * Use position-based targeting: xpath=//section[1]//div[3]/button[1]
     * Avoid text selectors if they're intercepted
   - NEVER suggest clicking "Close button" or unrelated elements when the target element exists but is intercepted
6. In case of any Input Box and type action, Click the input box before typing.

**MANDATORY RULE for RESPONSE FORMAT**   
Respond ONLY with JSON in the exact format below. No explanations, no additional text, no markdown formatting
"page_url" attribute with the current page URL,"page_title" attribute with the current page title.
When generating JSON, always use double quotes for all string values. Do not use single quotes to delimit strings. If your code contains single quotes, ensure they are inside double-quoted JSON strings and do not escape them unless required by the code context. All JSON must be valid and parseable.

**MANDATORY RESPONSE FORMAT**  
{{
  "tool": "corrected_tool_name",
  "step": "Previously FAILED STEP",
  "page_url": "current_page_url",
  "input": {{
    // FOR browser_navigate ONLY:
    "url": "extracted_url_from_step_text"
    
    // FOR browser_click/hover:
    "element": "brief_description_of_the_new_target_element",
    "ref": "MUST be exact ref value from snapshot [ref=eXXXX] - use only eXXXX part"
    
    // FOR browser_type:
    "element": "input_field_description",
    "ref": "input_field_reference",
    "text": "Text to type (REQUIRED for browser_type tool)"
    
    // FOR browser_select_option:
    "element": "dropdown_description",
    "ref": "dropdown_reference",
    "values": ["option_value"]
  }}
}}"""

            return rectification_prompt

        except Exception as e:
            self.logger.error(f"Failed to format rectification prompt: {e}")
            import traceback
            traceback.print_exc()
            raise RuntimeError("Could not format rectification prompt") from e

    async def _get_rectification_llm_instructions(
            self,
            rectification_prompt: str,
            step: ScenarioStep
    ) -> MCPPlayWrightLLMResponse:
        """Equivalent to Java's playrightLLmInstructions() for rectification"""
        try:
            config = Config(read_timeout=120, connect_timeout=60)
            bedrock_client = boto3.client(
                'bedrock-runtime',
                config=config,
                region_name="us-east-1"
            )

            llm = ChatBedrockConverse(
                model=PLATFORM_CONFIG["BEDROCK_MODEL_ID"],
                client=bedrock_client,
                max_tokens=8192,
                temperature=0.3,  # Slightly higher for creative alternatives
            )

            messages = [
                SystemMessage(content=self.playwright_rectification_system_prompt),
                HumanMessage(content=rectification_prompt)
            ]

            self.logger.info("Sending rectification request to LLM")
            output = llm.invoke(messages)
            self.logger.info("Successfully received rectification response from LLM")
            self.logger.info(f"Rectification response: {output.content}")

            # Parse the response (equivalent to getFormattedMCPPlayWrightLLMResponse)
            parsed_response = self._parse_llm_response(output.content, step)
            return parsed_response

        except Exception as e:
            self.logger.error(f"Error getting rectification instructions: {e}")
            raise

    async def _call_llm_for_analysis(self, prompt: str) -> str:
        """Helper method to call LLM for analysis tasks"""
        try:
            config = Config(read_timeout=60, connect_timeout=30)
            bedrock_client = boto3.client('bedrock-runtime', config=config, region_name="us-east-1")

            llm = ChatBedrockConverse(
                model=PLATFORM_CONFIG["BEDROCK_MODEL_ID"],  # Fast model for analysis
                client=bedrock_client,
                max_tokens=2000,
                temperature=0.1
            )

            messages = [HumanMessage(content=prompt)]
            result = llm.invoke(messages)
            return result.content

        except Exception as e:
            self.logger.error(f"LLM analysis call failed: {e}")
            return f"Analysis failed: {str(e)}"

    # ========== TEMPLATE LOADING METHODS ==========

    def _load_page_analysis_template(self) -> str:
        """Load page analysis prompt template"""
        return """TASK: Analyze the provided web page snapshot and extract comprehensive page context information.

PAGE SNAPSHOT:
{snapshot}

ANALYSIS REQUIREMENTS:

1. PAGE TYPE IDENTIFICATION:
   - Determine the page type (PRODUCT_DETAIL_PAGE, CART_PAGE, CHECKOUT_PAGE, etc.)
   - Identify the primary purpose of this page

2. ELEMENT EXTRACTION:
   For each interactive element found, provide:
   - Element name/description
   - Element type (button, radio, checkbox, input, link, etc.)
   - Current state (enabled/disabled, checked/unchecked, visible/hidden)
   - Multiple locator strategies in priority order:
     * Role-based: getByRole('button', { name: 'Add to Cart' })
     * Label-based: getByLabel('Buy once')
     * Text-based: getByText('Add to Cart')
     * Test ID: getByTestId('add-to-cart-btn')
     * CSS/XPath as fallback

3. CHEWY-SPECIFIC ANALYSIS (if applicable):
   - Product information (name, price, availability)
   - Purchase options (Autoship vs Buy once radio buttons)
   - Add to Cart button state and availability
   - Any promotional banners or modals
   - Cart status or item count

4. PAGE STATE ASSESSMENT:
   - Is the page fully loaded?
   - Are there any JavaScript errors visible?
   - Are there any overlays, modals, or blocking elements?
   - Is there any dynamic content still loading?

5. AUTOMATION READINESS:
   - Which elements are ready for interaction?
   - Are there any elements that need scrolling to be visible?
   - Any timing considerations for interactions?

RESPONSE FORMAT:
Provide detailed JSON analysis with pageType, pageLoadState, visibleElements, clickableElements, etc."""

    def _load_error_analysis_template(self) -> str:
        """Load error analysis prompt template"""
        return """TASK: The last attempt of the step execution has been failed by Playwright MCP server.
Analyze the error snapshot provided with the context of html page snapshot and previous locators.
After analyzing provide the output in below RESPONSE FORMAT

ERROR SNAPSHOT:
{errorsnapshot}

PAGE SNAPSHOT:
{pagesnapshot}

PREVIOUS LOCATOR
{previousLocator}

STEP FAILED:
{step}

RESPONSE FORMAT:
{
  "Error reason summary": "Provide complete analysis of the error exact reason",
}"""

    # ========== EXISTING METHODS ==========

    def _parse_web_ui_feature(self, feature_content: str) -> BDDFeature:
        """Parse BDD feature content"""
        lines = feature_content.strip().split('\n')
        scenarios = []
        current_scenario = None

        for line in lines:
            line = line.strip()
            if line.startswith('Scenario:'):
                if current_scenario:
                    scenarios.append(current_scenario)
                current_scenario = BDDScenario(name=line[9:].strip(), steps=[])
            elif line.startswith('Given') or line.startswith('When') or line.startswith('Then'):
                if current_scenario:
                    current_scenario.steps.append(ScenarioStep(step=line))

        if current_scenario:
            scenarios.append(current_scenario)

        return BDDFeature(name="Web UI Feature", scenarios=scenarios)

    def _load_playwright_system_prompt(self) -> str:
        """Load Playwright system prompt with enhanced Java-like capabilities"""
        return """You are an AI assistant connected to Playwright automation tools via MCP AND custom API tools.

AVAILABLE TOOL TYPES:
1. MCP BROWSER TOOLS - For web interactions (click, type, navigate, etc.)
2. CUSTOM API TOOLS - For API calls and data management

CUSTOM API TOOLS:
- custom_api_service_tool: Prepare API calls using JSON config files
- custom_mock_api_tool: Mock API responses using configuration files
- custom_modify_request_tool: Modify prepared requests (headers, body, params)
- custom_store_response_tool: Execute API calls and store responses
- Template variables: Use {variable_name} and {object.nested.field} in steps (automatically resolved)

STEP CLASSIFICATION:
- "add Header [template]" → Use custom_modify_request_tool
- "stores [field] from response as [variable]" → Use custom_store_response_tool with field_path + store_as
- "stores response as [variable]" → Use custom_store_response_tool with response_as
- "extract cookies as [variable_name]" → Use custom_browser_extract_cookie_tool with variable_name parameter
- "MOCKS with [mock_file.json]" → Use custom_mock_api_tool with mock_file parameter
- "INVOKES [service] with [config.json]" → Use custom_api_service_tool
- "switches to new tab" → Use browser_tabs with action="select" and index=1 (newest tab)
- "switches to tab [number]" → Use browser_tabs with action="select" and index=[number]
- Standard browser actions (including those with {template_variables}) → Use MCP browser tools

AUTOMATIC VARIABLE RESOLUTION: Template variables like {accountResponse.email} are automatically resolved before browser actions. No special tool needed.

VARIABLE RESOLUTION FEATURES:
- Dot notation: {giftcardresponse.additionalData.accountNumber}
- Timestamps: {timestamp}, {datetimestamp}, {uuid}, {random}
- Array access: {response.items.0.name}

CRITICAL LOCATOR STRATEGY REQUIREMENTS:
1. PREFER getByTestId() for elements with data-testid attributes
2. PREFER getByRole() for semantic elements (button, link, textbox, etc.)
3. Use getByText() for text-based selection
4. Use getByLabel() for form elements with labels
5. CSS selectors as fallback only

ENHANCED CODE GENERATION REQUIREMENTS:
1. Include viewport configuration for navigation steps:
   await page.setViewportSize({ width: 1278, height: 623 });
   await page.waitForLoadState('domcontentloaded');

2. Generate better locator strategies:
   - For radio buttons: page.getByTestId('buy-once-selector').locator('label')
   - For buttons: page.getByRole('button', { name: 'Add to Cart' })
   - For inputs: page.getByLabel('Email Address')

3. Include proper service imports in code:
   - AccountService for account operations
   - PageContextService for page context
   - CustomAPIService for API operations

CRITICAL CROSS-ORIGIN IFRAME HANDLING:
⚠️ IMPORTANT: Cannot interact with elements inside cross-origin iframes (payment buttons, social logins, embedded widgets, etc.)
✅ SOLUTION: Target the iframe CONTAINER element instead of the content inside
- Look for the parent/wrapper div that contains the iframe
- Target elements with data-testid, id, or class attributes on the container
- Click the container element - the embedded SDK/widget will handle the interaction
- NEVER try to access elements inside cross-origin iframes - always target the clickable wrapper

AVAILABLE TOOLS (MCP + Custom):

CUSTOM API TOOLS:
- custom_api_service_tool: Prepare API call config without executing
  Input: {"config_file": "path/to/config.json", "variables": {}}
- custom_mock_api_tool: Mock API responses using configuration files - Intercepts network requests
  Input: {"mock_file": "mock_post_shiptothisaddress.json", "variables": {}}
- custom_modify_request_tool: Modify prepared API request (headers, body, params)
  Input: {"header_template": "Authorization: Bearer {token}", "body_field": "field", "body_value": "value"}
- custom_store_response_tool: Execute prepared API call and store response
  Input: {"field_path": "access_token", "store_as": "NewUserAuthToken"} OR {"response_as": "giftcardresponse"}
- custom_browser_extract_cookie_tool: Extract cookies from current page and store with variable name
  Input: {"variable_name": "login_cookie"} - Extracts all non-HttpOnly cookies and stores as {login_cookie.cookie_name}

MCP BROWSER TOOLS:
{
  "tools" : [ {
    "name" : "browser_close",
    "description" : "Close the page",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_resize",
    "description" : "Resize the browser window",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "width" : {
          "type" : "number",
          "description" : "Width of the browser window"
        },
        "height" : {
          "type" : "number",
          "description" : "Height of the browser window"
        }
      },
      "required" : [ "width", "height" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_console_messages",
    "description" : "Returns all console messages",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_handle_dialog",
    "description" : "Handle a dialog",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "accept" : {
          "type" : "boolean",
          "description" : "Whether to accept the dialog."
        },
        "promptText" : {
          "type" : "string",
          "description" : "The text of the prompt in case of a prompt dialog."
        }
      },
      "required" : [ "accept" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_evaluate",
    "description" : "Evaluate JavaScript expression on page or element",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "function" : {
          "type" : "string",
          "description" : "() => { /* code */ } or (element) => { /* code */ } when element is provided"
        },
        "element" : {
          "type" : "string",
          "description" : "Human-readable element description used to obtain permission to interact with the element"
        },
        "ref" : {
          "type" : "string",
          "description" : "Exact target element reference from the page snapshot"
        }
      },
      "required" : [ "function" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_file_upload",
    "description" : "Upload one or multiple files",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "paths" : {
          "type" : "array",
          "items" : {
            "type" : "string"
          },
          "description" : "The absolute paths to the files to upload. Can be a single file or multiple files."
        }
      },
      "required" : [ "paths" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_install",
    "description" : "Install the browser specified in the config. Call this if you get an error about the browser not being installed.",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_press_key",
    "description" : "Press a key on the keyboard",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "key" : {
          "type" : "string",
          "description" : "Name of the key to press or a character to generate, such as `ArrowLeft` or `a`"
        }
      },
      "required" : [ "key" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_type",
    "description" : "Type text into editable element",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "element" : {
          "type" : "string",
          "description" : "Human-readable element description used to obtain permission to interact with the element"
        },
        "ref" : {
          "type" : "string",
          "description" : "Exact target element reference from the page snapshot"
        },
        "text" : {
          "type" : "string",
          "description" : "Text to type into the element"
        },
        "submit" : {
          "type" : "boolean",
          "description" : "Whether to submit entered text (press Enter after)"
        },
        "slowly" : {
          "type" : "boolean",
          "description" : "Whether to type one character at a time. Useful for triggering key handlers in the page. By default entire text is filled in at once."
        }
      },
      "required" : [ "element", "ref", "text" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_navigate",
    "description" : "Navigate to a URL",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "url" : {
          "type" : "string",
          "description" : "The URL to navigate to"
        }
      },
      "required" : [ "url" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_navigate_back",
    "description" : "Go back to the previous page",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_navigate_forward",
    "description" : "Go forward to the next page",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_network_requests",
    "description" : "Returns all network requests since loading the page",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_take_screenshot",
    "description" : "Take a screenshot of the current page. You can't perform actions based on the screenshot, use browser_snapshot for actions.",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "type" : {
          "type" : "string",
          "enum" : [ "png", "jpeg" ],
          "default" : "png",
          "description" : "Image format for the screenshot. Default is png."
        },
        "filename" : {
          "type" : "string",
          "description" : "File name to save the screenshot to. Defaults to `page-{timestamp}.{png|jpeg}` if not specified."
        },
        "element" : {
          "type" : "string",
          "description" : "Human-readable element description used to obtain permission to screenshot the element. If not provided, the screenshot will be taken of viewport. If element is provided, ref must be provided too."
        },
        "ref" : {
          "type" : "string",
          "description" : "Exact target element reference from the page snapshot. If not provided, the screenshot will be taken of viewport. If ref is provided, element must be provided too."
        },
        "fullPage" : {
          "type" : "boolean",
          "description" : "When true, takes a screenshot of the full scrollable page, instead of the currently visible viewport. Cannot be used with element screenshots."
        }
      },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_snapshot",
    "description" : "Capture accessibility snapshot of the current page, this is better than screenshot",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_click",
    "description" : "Perform click on a web page",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "element" : {
          "type" : "string",
          "description" : "Human-readable element description used to obtain permission to interact with the element"
        },
        "ref" : {
          "type" : "string",
          "description" : "Exact target element reference from the page snapshot"
        },
        "doubleClick" : {
          "type" : "boolean",
          "description" : "Whether to perform a double click instead of a single click"
        },
        "button" : {
          "type" : "string",
          "enum" : [ "left", "right", "middle" ],
          "description" : "Button to click, defaults to left"
        }
      },
      "required" : [ "element", "ref" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_drag",
    "description" : "Perform drag and drop between two elements",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "startElement" : {
          "type" : "string",
          "description" : "Human-readable source element description used to obtain the permission to interact with the element"
        },
        "startRef" : {
          "type" : "string",
          "description" : "Exact source element reference from the page snapshot"
        },
        "endElement" : {
          "type" : "string",
          "description" : "Human-readable target element description used to obtain the permission to interact with the element"
        },
        "endRef" : {
          "type" : "string",
          "description" : "Exact target element reference from the page snapshot"
        }
      },
      "required" : [ "startElement", "startRef", "endElement", "endRef" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_hover",
    "description" : "Hover over element on page",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "element" : {
          "type" : "string",
          "description" : "Human-readable element description used to obtain permission to interact with the element"
        },
        "ref" : {
          "type" : "string",
          "description" : "Exact target element reference from the page snapshot"
        }
      },
      "required" : [ "element", "ref" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_select_option",
    "description" : "Select an option in a dropdown",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "element" : {
          "type" : "string",
          "description" : "Human-readable element description used to obtain permission to interact with the element"
        },
        "ref" : {
          "type" : "string",
          "description" : "Exact target element reference from the page snapshot"
        },
        "values" : {
          "type" : "array",
          "items" : {
            "type" : "string"
          },
          "description" : "Array of values to select in the dropdown. This can be a single value or multiple values."
        }
      },
      "required" : [ "element", "ref", "values" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_tab_list",
    "description" : "List browser tabs",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_tab_new",
    "description" : "Open a new tab",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "url" : {
          "type" : "string",
          "description" : "The URL to navigate to in the new tab. If not provided, the new tab will be blank."
        }
      },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_tabs",
    "description" : "Manage browser tabs - list, create, close, or select",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "action" : {
          "type" : "string",
          "enum" : [ "list", "create", "close", "select" ],
          "description" : "Action to perform on tabs"
        },
        "index" : {
          "type" : "number",
          "description" : "Tab index for close/select actions"
        }
      },
      "required" : [ "action" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_tab_close",
    "description" : "Close a tab",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "index" : {
          "type" : "number",
          "description" : "The index of the tab to close. Closes current tab if not provided."
        }
      },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_wait_for",
    "description" : "Wait for text to appear or disappear or a specified time to pass",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "time" : {
          "type" : "number",
          "description" : "The time to wait in seconds"
        },
        "text" : {
          "type" : "string",
          "description" : "The text to wait for"
        },
        "textGone" : {
          "type" : "string",
          "description" : "The text to wait for to disappear"
        }
      },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_fill_form",
    "description" : "Fill multiple form fields at once",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "fields" : {
          "type" : "array",
          "items" : {
            "type" : "object",
            "properties" : {
              "element" : {"type" : "string"},
              "ref" : {"type" : "string"},
              "text" : {"type" : "string"}
            }
          },
          "description" : "Array of form fields to fill"
        }
      },
      "required" : [ "fields" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_tabs",
    "description" : "Manage browser tabs - list, create, close, or select",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "action" : {
          "type" : "string",
          "enum" : [ "list", "create", "close", "select" ],
          "description" : "Action to perform on tabs"
        },
        "index" : {
          "type" : "number",
          "description" : "Tab index for close/select actions"
        }
      },
      "required" : [ "action" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_mouse_click_xy",
    "description" : "Click at specific coordinates on an element",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "element" : {
          "type" : "string",
          "description" : "Element description"
        },
        "x" : {
          "type" : "number",
          "description" : "X coordinate"
        },
        "y" : {
          "type" : "number",
          "description" : "Y coordinate"
        }
      },
      "required" : [ "element", "x", "y" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_mouse_drag_xy",
    "description" : "Drag mouse from start coordinates to end coordinates",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "element" : {
          "type" : "string",
          "description" : "Element description"
        },
        "startX" : {"type" : "number"},
        "startY" : {"type" : "number"},
        "endX" : {"type" : "number"},
        "endY" : {"type" : "number"}
      },
      "required" : [ "element", "startX", "startY", "endX", "endY" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_mouse_move_xy",
    "description" : "Move mouse to specific coordinates",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "element" : {
          "type" : "string",
          "description" : "Element description"
        },
        "x" : {"type" : "number"},
        "y" : {"type" : "number"}
      },
      "required" : [ "element", "x", "y" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_generate_locator",
    "description" : "Generate optimal Playwright locator for an element",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "element" : {
          "type" : "string",
          "description" : "Human-readable element description"
        },
        "ref" : {
          "type" : "string",
          "description" : "Element reference from page snapshot"
        }
      },
      "required" : [ "element", "ref" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_verify_element_visible",
    "description" : "Verify that an element is visible on the page",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "role" : {
          "type" : "string",
          "description" : "Element role"
        },
        "accessibleName" : {
          "type" : "string",
          "description" : "Accessible name of the element"
        }
      },
      "required" : [ "role", "accessibleName" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_verify_list_visible",
    "description" : "Verify that a list with specific items is visible",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "element" : {
          "type" : "string",
          "description" : "List element description"
        },
        "ref" : {
          "type" : "string",
          "description" : "List element reference"
        },
        "items" : {
          "type" : "array",
          "items" : {"type" : "string"},
          "description" : "Expected list items"
        }
      },
      "required" : [ "element", "ref", "items" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_verify_text_visible",
    "description" : "Verify that specific text is visible on the page",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "text" : {
          "type" : "string",
          "description" : "Text to verify"
        }
      },
      "required" : [ "text" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_verify_value",
    "description" : "Verify the value of an element",
    "inputSchema" : {
      "type" : "object",
      "properties" : {
        "type" : {
          "type" : "string",
          "description" : "Type of verification"
        },
        "element" : {
          "type" : "string",
          "description" : "Element description"
        },
        "ref" : {
          "type" : "string",
          "description" : "Element reference"
        },
        "value" : {
          "type" : "string",
          "description" : "Expected value"
        }
      },
      "required" : [ "type", "element", "ref", "value" ],
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_start_tracing",
    "description" : "Start recording browser trace for debugging",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  }, {
    "name" : "browser_stop_tracing",
    "description" : "Stop recording browser trace and save results",
    "inputSchema" : {
      "type" : "object",
      "properties" : { },
      "additionalProperties" : false
    }
  } ],
  "totalCount" : 36,
  "exportedAt" : "2025-08-13T02:50:57.157646Z"
}
STRICT INSTRUCTION: Respond ONLY with JSON in the exact format below.No explanations, no additional text, no markdown formatting.
The JSON must contain below items:
    a. "tool" attribute with the name of the tool to call

    c. "step" attribute will be exact same step description which has been given in the user prompt.
    d. "input" attribute with a JSON object containing the parameters for the tool call.
    g. CRITICAL FOR NAVIGATION: If the tool is "browser_navigate", the input MUST contain ONLY a "url" field with the URL extracted from the step text. DO NOT include "element", "ref", or "text" fields.
    h. CRITICAL TOOL REQUIREMENTS:
       - browser_navigate: input MUST contain ONLY "url" field (extract from step text)
       - browser_select_option: input MUST contain "element", "ref", and "values" array fields
       - browser_type: input MUST contain "element", "ref", and "text" fields
       - browser_click: input MUST contain "element" and "ref" fields
       - browser_hover: input MUST contain "element" and "ref" fields
       - browser_drag: input MUST contain "startElement", "startRef", "endElement", "endRef" fields
       - browser_evaluate: input MUST contain "function" field
       - browser_file_upload: input MUST contain "paths" array field
       - browser_press_key: input MUST contain "key" field
       - browser_resize: input MUST contain "width" and "height" fields
       - browser_handle_dialog: input MUST contain "accept" boolean field
       - browser_tabs: input MUST contain "action" field and optionally "index" field for select/close actions
       - browser_wait_for: input MUST contain at least one of "time", "text", or "textGone" fields
    i.When generating JSON, always use double quotes for all string values. Do not use single quotes to delimit strings. If your code contains single quotes, ensure they are inside double-quoted JSON strings and do not escape them unless required by the code context. All JSON must be valid and parseable.
    i. "page_url" attribute with the current page URL
    k.  When generating JSON, ensure that:
        - All objects and arrays are properly closed with matching brackets.
        - All string values use double quotes.
        - The JSON is complete, valid, and parseable.
        - Do not leave any object or array unclosed.
    Return only the JSON object, nothing else.
    Here is the sample format. Only and Only follow the above instruction and step strictly and provide this json output:
{
  "tool": "tool_name",

  "step": "step description",
  "page_url": "current_page_url",
  "input": {
    // Examples for different tools:
    "url": "https://example.com", // browser_navigate
    "values": ["option_value"], // browser_select_option
    "text": "text to type", // browser_type
    "element": "button description", // browser_click, browser_hover
    "ref": "element_reference", // browser_click, browser_hover
    "function": "() => { return true; }", // browser_evaluate
    "paths": ["/path/to/file"], // browser_file_upload
    "key": "Enter", // browser_press_key
    "width": 1280, // browser_resize
    "height": 720, // browser_resize
    "accept": true, // browser_handle_dialog
    "action": "select", "index": 0, // browser_tabs (switch to tab 0)
    "action": "select", "index": 1, // browser_tabs (switch to tab 1) 
    "time": 2 // browser_wait_for
  }
}"""

    def _load_playwright_rectification_system_prompt(self) -> str:
        """Load Playwright rectification system prompt"""
        return """CRITICAL ERROR RECOVERY - Attempt {attemptNumber}/3

Previous MCP action FAILED. You MUST provide ONLY valid JSON with corrected locator strategy.

FAILED STEP: {failedStep}
FAILED TOOL: {failedTool}
FAILED LOCATOR: {failedLocator}
FAILED INPUT: {failedInput}
ERROR: {errorMessage}

CURRENT PAGE STATE:
{currentPageSnapshot}

CORRECTION STRATEGIES (choose most appropriate):

FOR TEXT VERIFICATION FAILURES:
⚠️ CRITICAL: If a verification step fails because expected text is not found, you MUST:
✅ Use ONLY verification tools: browser_verify_text_visible, browser_verify_element_visible
✅ NEVER switch to browser_click for verification steps
✅ Look for similar or partial text on the page that matches the intent
✅ Check if text appears with different casing, punctuation, or spacing
✅ If exact text is not found, try verifying a container element with partial text
✅ If verification should fail (text genuinely missing), return browser_verify_text_visible with exact text anyway

VERIFICATION TOOL EXAMPLES:
- For "verifies that X is displayed" → use browser_verify_text_visible with text: "X"
- For "verifies element Y" → use browser_verify_element_visible with element: "Y"
- NEVER use browser_click, browser_type, or browser_navigate for verification steps

FOR CROSS-ORIGIN IFRAME ELEMENTS:
⚠️ CRITICAL: If element is inside a cross-origin iframe, you CANNOT access it directly
✅ SOLUTION: Target the iframe CONTAINER element instead
- Look for the parent/wrapper div that contains the iframe
- Use data-testid, id, or class attributes on the container element
- Click the container/wrapper div - it typically has the click handler attached
- Example: Instead of "Button [ref=e123]" inside iframe → "Container div [ref=e120]" outside iframe
- Works for: payment buttons, social login widgets, embedded forms, third-party plugins

FOR RADIO BUTTON INTERCEPTION:
-If clicking a radio button directly fails, try targeting its container element or associated label instead. Many modern web applications use custom radio button implementations where the actual input element is hidden and the click handler is attached to a wrapper element.
- Use parent container element instead of direct radio button
- Target the label element that wraps the radio button
- if there are issue overlay or interception then try clicking on the label or the parent container instead

FOR ELEMENT NOT FOUND/VISIBLE:
- Use alternative CSS selectors (id, class, data attributes)
- Target parent/child elements in DOM hierarchy
- Check for dynamically loaded elements
- If element is in an iframe, check if it's cross-origin and target container instead

FOR ELEMENT INTERCEPTION:
- Click the intercepting element instead
- Use hover to reveal hidden elements
- Scroll element into view first

IF BUTTON CLICK FAILS :
- identify current state from page snapshot
- Check if button element still exists in DOM
- Scan for modal dialogs with high z-index
- Look for overlay elements covering the button
- Check for loading states or spinners
- Identify promotional popups or notifications
- Dismiss modals using close buttons (X, Close, Cancel)
- Click overlay backgrounds if they're dismissible
- Wait for loading states to complete

MANDATORY RESPONSE FORMAT:
You MUST respond with ONLY the JSON object below. NO explanations, NO additional text, NO markdown, NO formatting.

START YOUR RESPONSE WITH: {
END YOUR RESPONSE WITH: }

EXACT JSON FORMAT REQUIRED:
{
  "tool": "corrected_tool_name",

  "step": "{stepDescription}",
  "input": {
    "element": "corrected_element_description",
    "ref": "corrected_element_reference",
    "text": "Text to type (REQUIRED for browser_type tool)"
  }
}
Escape all inner double quotes in string values to ensure valid JSON.
RESPOND WITH JSON ONLY - NO OTHER TEXT ALLOWED
"""

    def _handle_json_parsing_recovery(self, cleaned_content: str, step: ScenarioStep) -> MCPPlayWrightLLMResponse:
        """Handle JSON parsing recovery for long JWT tokens and complex responses"""
        # Attempt to fix unterminated strings by escaping quotes and handling long tokens
        fixed_content = cleaned_content

        # If the content contains very long JWT tokens, try to parse it with better error handling
        if "eyJ" in cleaned_content and len(cleaned_content) > 3000:
            self.logger.info("Detected long JWT token in response, attempting to parse with robust method")

            # Try to extract just the essential parts for the tool
            import re

            # Look for the tool and input parts
            tool_match = re.search(r'"tool":\s*"([^"]+)"', fixed_content)
            input_match = re.search(r'"input":\s*\{([^}]+)\}', fixed_content)

            if not tool_match:
                raise ValueError("Could not extract tool name")
            
            tool_name = tool_match.group(1)
            self.logger.info(f"Extracted tool: {tool_name}")

            # For custom_modify_request_tool with header_template, extract just the template part
            if tool_name == "custom_modify_request_tool" and "header_template" in fixed_content:
                # Extract header template pattern - handle both well-formed and truncated JSON
                # Try with closing quote first (well-formed)
                header_match = re.search(r'"header_template":\s*"([^"]*Authorization:\s*Bearer\s*[^"]*)"', fixed_content)
                # If that fails, try without closing quote (truncated JWT)
                if not header_match:
                    header_match = re.search(r'"header_template":\s*"([^"]*Authorization:\s*Bearer\s*[^"]*)', fixed_content)
                
                if not header_match:
                    raise ValueError("Could not extract header_template")
                
                header_template = header_match.group(1)
                # If it's too long, keep just the template part
                if len(header_template) > 100:
                    if "Authorization: Bearer" in header_template:
                        # Infer variable name from step text (required - no fallback)
                        step_str = str(step) if not isinstance(step, str) else step
                        variable_name = self._infer_token_variable_name(step_str)
                        if not variable_name:
                            raise ValueError(f"Could not infer token variable name from step: {step_str}")
                        header_template = f"Authorization: Bearer {{{variable_name}}}"
                        self.logger.info(f"Inferred token variable name: {variable_name}")

                # Create a simplified response
                simplified_response = {
                    "tool": tool_name,
                    "step": step,  # Use the original step parameter
                    "page_url": "",
                    "input": {
                        "header_template": header_template
                    }
                }

                response_data = simplified_response
                self.logger.info(f"Successfully created simplified response for long token")

                # Create and return the MCPPlayWrightLLMResponse object from recovered data
                llm_response = MCPPlayWrightLLMResponse(
                    tool=response_data.get('tool', ''),
                    locator=None,  # Will be set by MCP generate_locator
                    step=response_data.get('step', step),
                    page_url=response_data.get('page_url'),
                    input=response_data.get('input', {})
                )

                self.logger.debug(f"Created MCPPlayWrightLLMResponse object from recovered data:")
                self.logger.debug(f"  - tool: {llm_response.tool}")
                self.logger.debug(f"  - input: {llm_response.input}")

                return llm_response
            else:
                raise ValueError("Unsupported tool for recovery")
        else:
            raise ValueError("No recovery strategy available")

    def _infer_token_variable_name(self, step: str) -> Optional[str]:
        """
        Infer token variable name from step text.
        Looks for patterns like {variableName} or mentions of token names.
        
        Args:
            step: The step text to analyze
            
        Returns:
            Inferred variable name or None if not found
        """
        import re
        
        # Pattern 1: Look for {variable_name} in the step
        placeholder_match = re.search(r'\{([a-zA-Z]\w*[Tt]oken[a-zA-Z]*)\}', step)
        if placeholder_match:
            token_var = placeholder_match.group(1)
            self.logger.debug(f"Found token variable placeholder in step: {token_var}")
            return token_var
        
        # Pattern 2: Look for "as {variable_name}" pattern (common in store response steps)
        store_as_match = re.search(r'(?:as|store[s]?\s+as)\s+\{?([a-zA-Z]\w*[Tt]oken[a-zA-Z]*)\}?', step, re.IGNORECASE)
        if store_as_match:
            token_var = store_as_match.group(1)
            self.logger.debug(f"Found token variable in store pattern: {token_var}")
            return token_var
        
        # Pattern 3: Look for mentioned token names (NewUserAuthToken, authToken, etc.)
        token_patterns = [
            r'([a-zA-Z]\w*[Tt]oken\w*)',  # Any word ending or containing "token"
            r'(auth[A-Z]\w+)',  # authSomething pattern
            r'([A-Z][a-zA-Z]*Auth[a-zA-Z]*)',  # SomethingAuth pattern
        ]
        
        for pattern in token_patterns:
            match = re.search(pattern, step)
            if match:
                token_var = match.group(1)
                # Make sure it's not too generic
                if token_var not in ['token', 'Token', 'tokens', 'Tokens']:
                    self.logger.debug(f"Inferred token variable from step: {token_var}")
                    return token_var
        
        # Default: no token variable found
        return None
        
    def _parse_llm_response(self, llm_content: str, step: ScenarioStep) -> MCPPlayWrightLLMResponse:
        """Parse LLM JSON response into MCPPlayWrightLLMResponse object (equivalent to getFormattedMCPPlayWrightLLMResponse)"""
        try:
            self.logger.debug(f"Parsing LLM response of type: {type(llm_content)}")
            raw_content = str(llm_content).strip()

            def _build_response(response_data: Dict[str, Any]) -> MCPPlayWrightLLMResponse:
                llm_response = MCPPlayWrightLLMResponse(
                    tool=response_data.get('tool', ''),
                    locator=None,
                    step=response_data.get('step', step),
                    page_url=response_data.get('page_url'),
                    input=response_data.get('input', {})
                )

                self.logger.debug("Created MCPPlayWrightLLMResponse object:")
                self.logger.debug(f"  - tool: {llm_response.tool}")
                self.logger.debug(f"  - input: {llm_response.input}")
                return llm_response

            def _extract_first_json_object(text: str) -> Optional[str]:
                start = text.find('{')
                if start == -1:
                    return None

                in_string = False
                escaped = False
                depth = 0

                for idx in range(start, len(text)):
                    ch = text[idx]

                    if escaped:
                        escaped = False
                        continue

                    if ch == '\\':
                        escaped = True
                        continue

                    if ch == '"':
                        in_string = not in_string
                        continue

                    if in_string:
                        continue

                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            return text[start:idx + 1]

                return None

            parse_candidates: List[str] = []

            cleaned_content = raw_content
            if cleaned_content.startswith('```json'):
                cleaned_content = cleaned_content[7:]
            if cleaned_content.endswith('```'):
                cleaned_content = cleaned_content[:-3]
            cleaned_content = cleaned_content.strip()
            parse_candidates.append(cleaned_content)

            fenced_json_match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", raw_content, re.IGNORECASE)
            if fenced_json_match:
                parse_candidates.append(fenced_json_match.group(1).strip())

            any_fenced_match = re.search(r"```[a-zA-Z]*\s*(\{[\s\S]*?\})\s*```", raw_content)
            if any_fenced_match:
                parse_candidates.append(any_fenced_match.group(1).strip())

            brace_json = _extract_first_json_object(raw_content)
            if brace_json:
                parse_candidates.append(brace_json)

            parse_error: Optional[json.JSONDecodeError] = None
            for candidate in parse_candidates:
                try:
                    response_data = json.loads(candidate)
                    if isinstance(response_data, dict) and response_data.get('tool'):
                        self.logger.debug("JSON parsed successfully: Same as LLM Response/ Rectification LLM response")
                        return _build_response(response_data)
                except json.JSONDecodeError as decode_error:
                    parse_error = decode_error
                    continue

            if parse_error is not None:
                self.logger.error(f"JSON parsing failed: {parse_error}")
                self.logger.error(f"Raw content (first 1000 chars): {str(llm_content)[:1000]}...")

                # Try to fix JSON parsing issues with very long strings
                try:
                    fixed_content = cleaned_content

                    # If the content contains very long JWT tokens, try to parse with robust extraction
                    if "eyJ" in cleaned_content and len(cleaned_content) > 3000:
                        self.logger.info("Detected long JWT token in response, attempting to parse with robust method")

                        # Look for the tool and input parts
                        tool_match = re.search(r'"tool":\s*"([^"]+)"', fixed_content)
                        input_match = re.search(r'"input":\s*\{([^}]+)\}', fixed_content)

                        if tool_match:
                            tool_name = tool_match.group(1)
                            self.logger.info(f"Extracted tool: {tool_name}")

                            # For custom_modify_request_tool with header_template, extract just the template part
                            if tool_name == "custom_modify_request_tool" and "header_template" in fixed_content:
                                # Try with closing quote first (well-formed), then without (truncated JSON)
                                header_match = re.search(r'"header_template":\s*"([^"]*Authorization:\s*Bearer\s*[^"]*)"', fixed_content)
                                if not header_match:
                                    header_match = re.search(r'"header_template":\s*"([^"]*Authorization:\s*Bearer\s*[^"]*)', fixed_content)

                                if header_match:
                                    header_template = header_match.group(1)
                                    if len(header_template) > 100 and "Authorization: Bearer" in header_template:
                                        # Infer variable name from step text (required - no fallback)
                                        step_str = str(step) if not isinstance(step, str) else step
                                        variable_name = self._infer_token_variable_name(step_str)
                                        if not variable_name:
                                            raise ValueError(f"Could not infer token variable name from step: {step_str}")
                                        header_template = f"Authorization: Bearer {{{variable_name}}}"
                                        self.logger.info(f"Inferred token variable name: {variable_name}")

                                    simplified_response = {
                                        "tool": tool_name,
                                        "step": step,
                                        "page_url": "",
                                        "input": {
                                            "header_template": header_template
                                        }
                                    }

                                    response_data = simplified_response
                                    self.logger.info("Successfully created simplified response for long token")

                                    llm_response = MCPPlayWrightLLMResponse(
                                        tool=response_data.get('tool', ''),
                                        locator=None,
                                        step=response_data.get('step', step),
                                        page_url=response_data.get('page_url'),
                                        input=response_data.get('input', {})
                                    )

                                    self.logger.debug("Created MCPPlayWrightLLMResponse object from recovered data:")
                                    self.logger.debug(f"  - tool: {llm_response.tool}")
                                    self.logger.debug(f"  - input: {llm_response.input}")

                                    return llm_response

                    raise RuntimeError(f"Failed to parse JSON to MCPPlayWrightLLMResponse: {parse_error}")

                except Exception as fix_error:
                    self.logger.error(f"Failed to fix JSON parsing: {fix_error}")
                    raise RuntimeError(f"Failed to parse JSON to MCPPlayWrightLLMResponse: {parse_error}") from parse_error

        except Exception as e:
            self.logger.error(f"JSON parsing failed: {e}")
            self.logger.error(f"Raw content (first 1000 chars): {str(llm_content)[:1000]}...")
            raise RuntimeError(f"Failed to parse JSON to MCPPlayWrightLLMResponse: {e}") from e

        raise RuntimeError("Failed to parse JSON to MCPPlayWrightLLMResponse: No valid JSON object with a tool field found")

    async def _get_playwright_llm_instructions(self, prompt: str, step: ScenarioStep) -> MCPPlayWrightLLMResponse:
        """Get LLM instructions for a step"""
        try:
            config = Config(
                read_timeout=120,
                connect_timeout=60,
                retries={'max_attempts': 10}
            )

            bedrock_client = boto3.client(
                'bedrock-runtime',
                config=config,
                aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
                region_name="us-east-1"
            )

            llm = ChatBedrockConverse(
                model=PLATFORM_CONFIG["BEDROCK_MODEL_ID"],
                client=bedrock_client,
                max_tokens=8192,
                temperature=0.25,
                top_p=0.3,
            )

            messages = [
                SystemMessage(content=self.playwright_system_prompt),
                HumanMessage(content=prompt)
            ]

            self.logger.info("Sending request to LLM")
            output = llm.invoke(messages)
            self.logger.info("Successfully received response from LLM")
            self.logger.debug(f"Response length: {len(output.content)} characters")
            self.logger.debug(output.content)

            # Parse the JSON string into MCPPlayWrightLLMResponse object
            parsed_response = self._parse_llm_response(output.content, step)
            self.logger.debug(f"Parsed response type: {type(parsed_response)}")
            self.logger.debug(f"Parsed response tool: {parsed_response.tool}")

            return parsed_response

        except Exception as e:
            self.logger.error(f"Error in _get_playwright_llm_instructions: {str(e)}")
            raise

    # Placeholder methods
    async def _search_previous_responses(self, filter_data: str, limit: int) -> List[MCPPlayWrightLLMResponse]:
        """Search for previous successful responses (implement with your vector store)"""
        pass

    async def _generate_partial_step_definitions(self, scenario_number, scenario_dict, jira_id, current_script, generate_step_definitions, datasets):
        """
        Generate step definitions for successful steps even when scenario fails.
        This ensures partial progress is captured in the failure artifact zip.
        
        Args:
            scenario_number: Current scenario number
            scenario_dict: Dictionary tracking all scenarios
            jira_id: JIRA ID for the feature
            current_script: Previously generated script content
            generate_step_definitions: Whether to generate step definitions
            datasets: Dataset examples for the scenario
            
        Returns:
            str: Generated TypeScript step definitions for successful steps, or None if no steps succeeded
        """
        try:
            # Check if we have any successful steps in the DataFrame
            logger.info(f"🔍 _generate_partial_step_definitions called: scenario_dataframe exists={self.scenario_dataframe is not None}, len={len(self.scenario_dataframe) if self.scenario_dataframe is not None else 0}")
            if self.scenario_dataframe is None or len(self.scenario_dataframe) == 0:
                logger.warning("⚠️ ❌ No successful steps to generate definitions for - returning None")
                return None
            
            successful_steps = len(self.scenario_dataframe)
            logger.info(f"📝 ✅ Generating step definitions for {successful_steps} successful steps before failure...")
            
            # Generate field details from partial DataFrame (same as success flow)
            field_details = self._generate_field_details_from_df(self.scenario_dataframe)
            
            # Build feature content from successful steps
            feature_content = f"Feature: MCP Generated Test (Partial)\n\nScenario: MCP Test Scenario (Failed - Partial Steps)\n"
            for _, row in self.scenario_dataframe.iterrows():
                feature_content += f"  {row['original_bdd_step']}\n"
            
            # Store scenario in scenario_dict
            scenario_dict[f"Scenario_{scenario_number}"] = self.scenario_dataframe
            
            # Determine flag (same logic as success flow)
            flag = scenario_number > 1 and any(f"Scenario_{j}" in scenario_dict for j in range(1, scenario_number))
            
            # Generate step definitions using the SAME logic as success flow
            if generate_step_definitions:
                from services.prompt_layer.step_def_template import StepDefTemplate
                stepDefTemplate = StepDefTemplate()
                final_script = stepDefTemplate.generate_playwright(
                    data=feature_content,
                    df=field_details,
                    df_inter=self.scenario_dataframe,
                    i=scenario_number,
                    flag=flag,
                    jira_id=jira_id,
                    current_script=current_script,
                    generate_step_definitions=True,
                    exclude_paypal_code=True  # Exclude custom PayPal code for MCP flows
                )
                logger.info(f"✅ Generated partial step definitions ({successful_steps} steps, {len(final_script)} chars)")
                return final_script
            else:
                from services.prompt_layer.step_def_generation_playwright import StepDefGenerationPlaywright
                playwright_generator = StepDefGenerationPlaywright()
                final_script = playwright_generator.generate_playwright_test(
                    feature_content,
                    field_details,
                    self.scenario_dataframe,
                    i=scenario_number,
                    flag=flag,
                    jira_id=jira_id,
                    current_script=current_script,
                    datasets=datasets
                )
                logger.info(f"✅ Generated partial step definitions ({successful_steps} steps, {len(final_script)} chars)")
                return final_script
                
        except Exception as gen_error:
            logger.error(f"❌ Failed to generate partial step definitions: {gen_error}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return None

    async def _record_mcp_action_to_dataframe(self, current_df, step, llm_response, mcp_response, step_index):
        """Record MCP action to DataFrame for TypeScript generation"""
        import pandas as pd
        
        # Extract action details from MCP response
        if not llm_response.tool:
            raise ValueError("Tool is required in LLM response")
        action = llm_response.tool.replace('browser_', '')
        element_desc = llm_response.input.get('element', '') if llm_response.input else ''
        test_data = llm_response.input.get('text', '') or llm_response.input.get('url', '') if llm_response.input else ''
        
        # LOCATOR REUSE FIX: When the response comes from cached locator reuse,
        # CachedLocatorResponse.input is always an empty dict, so action/object/data
        # would all be empty after the above extraction. Instead, use the enriched
        # attributes that were populated from step-text extraction at call time.
        if action == 'cached_locator_reuse':
            cached_action = getattr(llm_response, 'action', '') or ''
            cached_object = getattr(llm_response, 'element_object', '') or ''
            cached_data = getattr(llm_response, 'test_data', '') or ''
            if cached_action:
                action = cached_action
            if cached_object:
                element_desc = cached_object
            if cached_data:
                test_data = cached_data
            logger.info(
                f"🔄 RECORD-DF: Cached locator reuse — restored metadata: "
                f"action='{action}', object='{element_desc[:50]}', data='{test_data[:50]}'"
            )

        # Use the pre-generated locator from successful action execution
        locator = llm_response.locator
        
        # Create new row with ALL required columns for step definition generation
        new_row = {
            'original_bdd_step': step,
            'action': action,
            'object': element_desc,
            'data': test_data,
            'playwrightLocator': locator,
            'Condition': '',
            'window_detected': False,  # Boolean instead of string
            'page': 'MCP_Page',
            'url': llm_response.page_url or '',
            'Xpath': '',
            'RelXpath': '',
            'TagName': '',
            'ID': '',
            'Class': '',
            'Name': '',
            'LinkText': '',
            'CssSelector_with_Class': '',
            'CssSelector_with_ID': '',
            'All_Attributes': '',
            'failure': '',
            'input': '',
            'Condition_Value': '',
            'generated_by': 'mcp_crawl'
        }
        
        # Add to DataFrame
        new_df = pd.DataFrame([new_row])
        return pd.concat([current_df, new_df], ignore_index=True)
    

    
    def _generate_field_details_from_df(self, dataframe):
        """Generate field details string from DataFrame for TypeScript generation.

        COLUMN ORDER (corrected labels so the LLM can unambiguously identify each field):
          BDDStep -- ActionType -- WebElementName -- TestDataValue -- PlaywrightLocator -- Condition -- WindowDetection

        The PlaywrightLocator column always contains the exact Playwright locator expression
        (e.g. page.getByRole(...), page.locator(...)) that MUST be used verbatim in the
        generated TypeScript step definition.
        """
        field_details = (
            "Details of all actions on web element objects with test data and Playwright locator below.\n"
            "COLUMN FORMAT: "
            "BDDStep -- ActionType -- WebElementName -- TestDataValue -- PlaywrightLocator -- Condition -- WindowDetection\n"
            "CRITICAL: The 'PlaywrightLocator' column contains the EXACT Playwright locator expression to use in the "
            "generated code. Copy it verbatim — do NOT infer or generate a different locator.\n"
        )

        for _, row in dataframe.iterrows():
            action_lower = str(row['action']).lower()
            bdd_step = row['original_bdd_step']
            action_type = row['action']
            element_name = row['object']
            test_data = row['data']
            locator = row['playwrightLocator']
            condition = row['Condition']
            window_det = row['window_detected']

            if action_lower == "verify":
                field_details += (
                    f"{bdd_step} -- {action_type} -- {element_name} -- "
                    f"{test_data} -- {locator} -- {condition} -- {window_det}\n"
                )
            elif action_lower == "switch_to_iframe":
                xpath_value = str(row['Xpath'])
                iframe_locator = f"page.locator('xpath={xpath_value}')"
                field_details += (
                    f"{bdd_step} -- {action_type} -- {element_name} -- "
                    f"{test_data} -- {iframe_locator} -- {condition} -- {window_det}\n"
                )
            else:
                field_details += (
                    f"{bdd_step} -- {action_type} -- {element_name} -- "
                    f"{test_data} -- {locator} -- {condition} -- {window_det}\n"
                )

        return field_details

    async def _save_llm_response(self, llm_response: MCPPlayWrightLLMResponse):
        """Save successful LLM response (implement with your vector store)"""
        navigation_keywords = ["navigate", "load", "browse", "visit", "open", "go to"]
        is_navigation_step = any(keyword in llm_response.step.lower() for keyword in navigation_keywords)

        if is_navigation_step:
            self.logger.info(f"Not saving navigation step response to vector store: {llm_response.step}")
        else:
            self.logger.info(f"Saved non-navigation step response to vector store: {llm_response.step}")

    async def _generate_scenario_code(self, responses: List[MCPPlayWrightLLMResponse], scenario: BDDScenario) -> str:
        """Generate complete scenario code (implement based on your needs)"""
        pass
    
    async def ensure_connected(self):
        """Ensure MCP client is connected"""
        if not self.mcp_client:
            self.mcp_client = UniversalMCPClient()
            await self.mcp_client.connect()
            self.logger.info("✅ MCP client connected")
    
    def _validate_mcp_input(self, tool: str, input_params: dict) -> str:
        """Validate MCP tool input parameters"""
        if not input_params:
            raise ValueError(f"Input parameters are required for tool: {tool}")
        
        required_params = {
            'browser_navigate': ['url'],
            'browser_select_option': ['element', 'ref', 'values'],
            'browser_type': ['element', 'ref', 'text'],
            'browser_click': ['element', 'ref'],
            'browser_hover': ['element', 'ref'],
            'browser_drag': ['startElement', 'startRef', 'endElement', 'endRef'],
            'browser_evaluate': ['function'],
            'browser_file_upload': ['paths'],
            'browser_press_key': ['key'],
            'browser_resize': ['width', 'height'],
            'browser_handle_dialog': ['accept'],
            'browser_tabs': ['action'],
            'browser_wait_for': ['time']  # At least one of time/text/textGone
        }
        
        if tool in required_params:
            missing = [param for param in required_params[tool] if param not in input_params or input_params[param] is None]
            if missing:
                return f"Missing required parameters for {tool}: {missing}"
        
        return None


    async def disconnect(self):
        """Disconnect MCP client if connected"""
        if self.mcp_client:
            await self.mcp_client.disconnect()
            self.mcp_client = None
            self.logger.info("✅ MCP client disconnected")
    
    async def _find_and_save_mcp_trace(self, jira_id: str, failed: bool = False) -> str:
        """Find automatically generated MCP trace files and save them"""
        try:
            # Look for MCP trace files in common locations
            trace_patterns = [
                "/tmp/playwright-mcp-output/*/traces/trace.json",
                "/tmp/playwright-mcp-output/*/traces/*.zip",
                "./playwright-mcp-output/*/traces/trace.json",
                "./playwright-mcp-output/*/traces/*.zip",
                "/tmp/playwright-mcp-output/*/trace.json",
                "./playwright-mcp-output/*/trace.json"
            ]
            
            trace_file_found = None
            trace_dir_found = None
            
            # First try to find existing trace files
            for pattern in trace_patterns:
                files = glob.glob(pattern)
                if files:
                    # Get the most recent file
                    trace_file_found = max(files, key=lambda x: Path(x).stat().st_mtime)
                    logger.info(f"🔍 Found MCP trace file: {trace_file_found}")
                    break
            
            # If no trace file found, look for trace directories with resources
            if not trace_file_found:
                trace_dirs = glob.glob("/tmp/playwright-mcp-output/*/traces")
                if trace_dirs:
                    # Get the most recent trace directory
                    trace_dir_found = max(trace_dirs, key=lambda x: Path(x).stat().st_mtime)
                    logger.info(f"🔍 Found MCP trace directory: {trace_dir_found}")
                    
                    # Check if there's a trace.json file in the directory
                    potential_trace = Path(trace_dir_found) / "trace.json"
                    if potential_trace.exists():
                        trace_file_found = str(potential_trace)
                        logger.info(f"🔍 Found trace.json in directory: {trace_file_found}")
                    else:
                        # Create a zip of the entire trace directory
                        logger.info(f"📦 Creating zip from trace directory: {trace_dir_found}")
                        import zipfile
                        temp_zip = Path(trace_dir_found).parent / "trace.zip"
                        with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
                            for root, dirs, files in os.walk(trace_dir_found):
                                for file in files:
                                    file_path = Path(root) / file
                                    arcname = file_path.relative_to(Path(trace_dir_found).parent)
                                    zipf.write(file_path, arcname)
                        trace_file_found = str(temp_zip)
                        logger.info(f"📦 Created trace zip: {trace_file_found}")
            
            if not trace_file_found:
                logger.warning("⚠️ No MCP trace files found in expected locations")
                # Log the trace viewer URL from the logs if available
                logger.info("📺 Trace viewer was available at: http://localhost:50388/trace/index.html")
                return None
            
            # Create trace directory in target/trace folder like recordings
            import datetime
            from pathlib import Path
            
            # Create target/trace directory
            target_dir = Path("target/trace")
            target_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate timestamp-based filename like recordings
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # milliseconds
            status_suffix = "_failed" if failed else ""
            file_ext = ".zip" if trace_file_found.endswith(".zip") else ".json"
            trace_filename = f"{jira_id}_trace_{timestamp}{status_suffix}{file_ext}"
            trace_file = target_dir / trace_filename
            
            # Copy trace file to target directory
            shutil.copy2(trace_file_found, trace_file)
            logger.info(f"📄 Copied trace file to: {trace_file}")
            
            # Upload to S3
            try:
                upload_result = upload_file_to_s3_and_get_urls(trace_file)
                s3_url = upload_result.get("presigned_url") or upload_result.get("http_url")
                logger.info(f"✅ Trace uploaded to S3: {redact_presigned_url_for_log(s3_url)}")
                return s3_url
                
            except Exception as s3_error:
                logger.warning(f"⚠️ S3 upload failed, keeping local file: {s3_error}")
                return str(trace_file)
                
        except Exception as e:
            logger.error(f"❌ Failed to find and save MCP trace: {e}")
            import traceback
            logger.error(f"❌ Trace error traceback: {traceback.format_exc()}")
            return None
    
    async def _save_trace_file(self, trace_result, jira_id: str, failed: bool = False) -> str:
        """Save MCP trace file and upload to S3"""
        try:
            logger.info(f"💾 Saving trace file for {jira_id}, failed={failed}")
            logger.info(f"📊 Trace result type: {type(trace_result)}")
            logger.info(f"📊 Trace result content: {trace_result}")
            
            import tempfile
            import shutil
            from pathlib import Path
            from utils.recording_cleanup import _setup_failure_recording_dir
            from utils.s3_utils import upload_file_to_s3_and_get_urls
            
            # Create trace directory
            prefix = "mcp-trace-failed" if failed else "mcp-trace"
            trace_dir = _setup_failure_recording_dir(prefix, str(jira_id))
            logger.info(f"📁 Created trace directory: {trace_dir}")
            
            # Save trace file
            trace_file = trace_dir / f"{jira_id}_trace.zip"
            logger.info(f"📄 Trace file path: {trace_file}")
            
            # Write trace result to file
            if isinstance(trace_result, (str, bytes)):
                logger.info(f"💾 Writing trace as string/bytes, length: {len(trace_result)}")
                with open(trace_file, 'wb') as f:
                    if isinstance(trace_result, str):
                        f.write(trace_result.encode())
                    else:
                        f.write(trace_result)
            else:
                # If trace_result is a dict or other format, save as JSON
                logger.info(f"💾 Writing trace as JSON")
                import json
                with open(trace_file, 'w') as f:
                    json.dump(trace_result, f, indent=2)
            
            logger.info(f"✅ Trace file written successfully")
            
            # Upload to S3
            try:
                logger.info(f"☁️ Uploading trace to S3...")
                upload_result = upload_file_to_s3_and_get_urls(trace_file)
                s3_url = upload_result.get("presigned_url") or upload_result.get("http_url")
                
                # Clean up local file after upload
                try:
                    trace_file.unlink(missing_ok=True)
                    trace_dir.rmdir()
                except Exception:
                    pass
                
                logger.info(f"✅ Trace uploaded to S3: {redact_presigned_url_for_log(s3_url)}")
                return s3_url
                
            except Exception as s3_error:
                logger.warning(f"⚠️ S3 upload failed, keeping local file: {s3_error}")
                return str(trace_file)
                
        except Exception as e:
            logger.error(f"❌ Failed to save trace file: {e}")
            import traceback
            logger.error(f"❌ Trace save traceback: {traceback.format_exc()}")
            return f"trace_save_failed_{jira_id}"


# # Usage example:
# async def main():
#     """Example usage"""
#     service = UiAutomationService()
#     result = await service.chat()
#     print(result)
#
#
# if __name__ == "__main__":
#     asyncio.run(main())

    # Integration method for existing MCP flow
    async def execute_with_retry_logic(self, step_lst, record_video: bool = False, jira_id: str = None, generate_step_definitions: bool = True, current_script: str = "", scenario_number: int = 1, scenario_dict: dict = None):
        """Integration point for existing MCP implementation"""
        return await self.chat(step_lst, record_video, jira_id, generate_step_definitions, current_script, scenario_number, scenario_dict)
    
    # Shared custom tools instance for data persistence
    _shared_custom_tools = None
    
    def _get_shared_custom_tools(self):
        """Get shared custom tools instance to maintain data across steps"""
        if not hasattr(self.__class__, '_shared_custom_tools') or self.__class__._shared_custom_tools is None:
            from .custom_tools import CustomTools
            self.__class__._shared_custom_tools = CustomTools()
        return self.__class__._shared_custom_tools
    
    async def _is_custom_tool(self, tool_name: str) -> bool:
        """Check if tool is a custom tool by checking if method exists"""
        try:
            custom_tools = self._get_shared_custom_tools()
            # Check if the tool method exists in CustomTools class
            return hasattr(custom_tools, tool_name) and callable(getattr(custom_tools, tool_name))
        except:
            return False
    async def _download_remote_trace(self, jira_id: str, session_id: Optional[str] = None) -> str:
        """Download trace/video from remote MCP server using 3-step API - supports multiple video downloads"""
        try:
            import httpx
            from pathlib import Path
            from datetime import datetime
            
            base_url = "https://playwright-mcp.corp-ai-workloads.stg.aws.chewy.cloud"
            
            logger.info("=" * 80)
            logger.info("📥 REMOTE MCP VIDEO/TRACE DOWNLOAD PROCESS")
            logger.info("=" * 80)
            if session_id:
                logger.info(f"🆔 Session ID: {session_id}")
            logger.info("📋 Step 1/3: GET /videos - Retrieving file list...")
            
            async with httpx.AsyncClient(timeout=300.0) as client:
                # ✅ Use session ID to filter videos if available
                if session_id:
                    logger.info(f"   🎯 Filtering by session ID: {session_id[:8]}...")
                    videos_response = await client.get(f"{base_url}/videos", params={"sessionId": session_id})
                else:
                    logger.info("   ⚠️  No session ID - fetching all videos")
                    videos_response = await client.get(f"{base_url}/videos")
                
                logger.info(f"   Response Status: {videos_response.status_code}")
                
                if videos_response.status_code != 200:
                    logger.warning(f"   ❌ Failed to get file list")
                    return []
                
                videos_data = videos_response.json()
                
                # Pretty print JSON response
                import json
                logger.info("   ✅ Response Data:")
                logger.info(json.dumps(videos_data, indent=2))
                
                # Extract video/trace files from response
                videos = videos_data.get('videos', [])
                total_count = videos_data.get('totalCount', 0)
                output_dir = videos_data.get('outputDirectory', 'unknown')
                
                logger.info(f"   📊 Total files available: {total_count}")
                logger.info(f"   📁 Server output directory: {output_dir}")
                
                if not videos:
                    if session_id:
                        logger.warning(f"   ⚠️  No files found for session {session_id[:8]}...")
                    else:
                        logger.warning("   ⚠️  No files in videos array (may have been auto-cleaned)")
                    logger.info("=" * 80)
                    return ""
                
                # Process ALL videos instead of just the first one
                downloaded_files = []
                logger.info(f"   ✅ Found {len(videos)} video(s) to download...")
                
                for index, video_obj in enumerate(videos):
                    logger.info(f"\n📥 Processing video {index + 1}/{len(videos)}:")
                    
                    # Handle both dict format (with metadata) and string format
                    if isinstance(video_obj, dict):
                        filename = video_obj.get('filename')
                        file_size_human = video_obj.get('sizeHuman', 'unknown')
                        file_created = video_obj.get('createdHuman', 'unknown')
                        logger.info(f"   📄 Video {index + 1} details:")
                        logger.info(f"      • Filename: {filename}")
                        logger.info(f"      • Size: {file_size_human}")
                        logger.info(f"      • Created: {file_created}")
                    else:
                        filename = str(video_obj)
                        logger.info(f"   📄 Video {index + 1}: {filename}")
                    
                    if not filename:
                        logger.warning(f"   ⚠️  Could not extract filename for video {index + 1}")
                        continue
                    
                    # Step 2: Download this specific video
                    logger.info(f"📥 Step 2: GET /download/{filename} - Downloading video {index + 1}...")
                    download_response = await client.get(f"{base_url}/download/{filename}")
                    
                    logger.info(f"   Response Status: {download_response.status_code}")
                    
                    if download_response.status_code != 200:
                        logger.warning(f"   ❌ Download failed for video {index + 1}: {filename}")
                        continue
                    
                    # Save to target/trace directory (same as local MCP traces)
                    target_dir = Path("target/trace")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Use original filename from MCP server (no modifications)
                    file_ext = Path(filename).suffix or '.webm'
                    
                    # Log file type for debugging
                    if file_ext == '.webm':
                        logger.info(f"   📹 File type: Video (.webm)")
                    elif file_ext == '.zip':
                        logger.info(f"   📦 File type: Trace (.zip)")
                    else:
                        logger.info(f"   📄 File type: {file_ext}")
                    
                    # Keep exact filename from server response
                    local_filename = filename
                    local_path = target_dir / local_filename
                    
                    with open(local_path, 'wb') as f:
                        f.write(download_response.content)
                    
                    file_size = len(download_response.content)
                    logger.info(f"   ✅ Downloaded {file_size:,} bytes")
                    logger.info(f"   💾 Saved to: {local_path}")
                    
                    # Upload to S3 immediately after downloading each file
                    logger.info(f"☁️  Uploading video {index + 1} to S3...")
                    try:
                        from utils.s3_utils import upload_file_to_s3_and_get_urls
                        upload_result = upload_file_to_s3_and_get_urls(local_path)
                        s3_url = upload_result.get("presigned_url") or upload_result.get("http_url")
                        logger.info(f"   ✅ S3 Upload complete for video {index + 1}")
                        logger.info(f"   🔗 URL: {redact_presigned_url_for_log(s3_url)}")
                        downloaded_files.append(s3_url)
                    except Exception as s3_error:
                        logger.warning(f"   ⚠️  S3 upload failed for video {index + 1}: {s3_error}")
                        logger.info(f"   📁 Local file available: {local_path}")
                        downloaded_files.append(str(local_path))
                
                # After processing all videos, clean up server files
                logger.info(f"\n🧹 Step 3: Cleaning up server files...")
                
                # Clean up each downloaded file from the server
                for index, video_obj in enumerate(videos):
                    filename = video_obj.get('filename') if isinstance(video_obj, dict) else str(video_obj)
                    if filename:
                        try:
                            cleanup_response = await client.delete(f"{base_url}/cleanup", params={"filename": filename})
                            if cleanup_response.status_code == 200:
                                logger.info(f"   ✅ Cleaned up video {index + 1}: {filename}")
                            else:
                                logger.warning(f"   ⚠️  Cleanup warning for video {index + 1}: {cleanup_response.status_code}")
                        except Exception as cleanup_error:
                            logger.warning(f"   ⚠️  Cleanup error for video {index + 1}: {cleanup_error}")
                
                logger.info(f"\n🎯 Download Summary:")
                logger.info(f"   • Total videos processed: {len(videos)}")
                logger.info(f"   • Successfully downloaded: {len(downloaded_files)}")
                logger.info(f"   • Upload method: {'S3' if any('amazonaws' in url or 's3' in url for url in downloaded_files if isinstance(url, str)) else 'Local'}")
                
                # Return pipe-separated string of all video URLs/paths for better parsing
                if downloaded_files:
                    # Use pipe separator for multiple videos to avoid comma parsing issues
                    result = " | ".join(str(file) for file in downloaded_files)
                    redacted_summary = " | ".join(
                        redact_presigned_url_for_log(str(f)) for f in downloaded_files
                    )
                    logger.info(f"   📋 Returning pipe-separated URLs: {redacted_summary[:300]}...")
                    logger.info(f"   📊 Total video URLs returned: {len(downloaded_files)}")
                    logger.info("=" * 80)
                    return result
                else:
                    logger.info("   📋 No files downloaded - returning empty string")
                    logger.info("=" * 80)
                    return ""
                
        except Exception as e:
            logger.error(f"❌ Remote trace download failed: {e}")
            return ""
    
    async def _validate_step_response(self, original_step: str, llm_response: MCPPlayWrightLLMResponse) -> bool:
        """
        Validate that LLM response matches the original step intent.
        Implements same verification logic as JS crawler.
        
        Args:
            original_step: The original BDD step (e.g., "click login button")
            llm_response: The parsed LLM response with tool and input
            
        Returns:
            bool: True if validation passes, False otherwise
        """
        try:
            # Perform comprehensive validation using the step verifier
            validation_result = self.step_verifier.validate_llm_response(
                original_step=original_step,
                llm_response=llm_response
            )
            
            # Log detailed validation results
            logger.info("🔍 MCP STEP VALIDATION:")
            logger.info(f"Step: '{original_step}'")
            logger.info(f"LLM Tool: '{llm_response.tool}'")
            logger.info(f"LLM Element: '{llm_response.input.get('element', 'N/A') if llm_response.input else 'N/A'}'")
            logger.info(f"LLM Input: {llm_response.input}")
            logger.info(f"Confidence: {validation_result.confidence_score:.1f}%")
            logger.info(f"Action Match: {validation_result.action_match}")
            logger.info(f"Element Match: {validation_result.element_match}")
            logger.info(f"Intent Match: {validation_result.intent_match}")
            
            # Add debug logging for parsed action
            parsed_action, parsed_element = self.step_verifier._parse_step_components(original_step)
            logger.info(f"🔍 PARSED ACTION DEBUG: '{parsed_action}' | ELEMENT: '{parsed_element}'")
            
            if not validation_result.is_valid:
                logger.warning(f"❌ VALIDATION FAILED:")
                logger.warning(f"Confidence: {validation_result.confidence_score:.1f}%")
                # Surface detailed validation reason (e.g., NAVIGATION BLOCKED with host/allowlist)
                self.step_verifier.log_validation_details(validation_result, logger=logger)
                
                if validation_result.suggested_corrections:
                    logger.warning(f"🔧 Suggested corrections:")
                    self.step_verifier.log_suggested_corrections(validation_result, logger=logger)
                
                # Return False to trigger retry logic in _find_element_with_retry
                return False
            
            logger.info(f"✅ STEP VALIDATION PASSED (confidence: {validation_result.confidence_score:.1f}%)")
            return True
            
        except Exception as e:
            logger.error(f"❌ Step validation error: {e}")
            # If validation fails due to error, return False to trigger retry
            return False
    
    def _requires_element_safety_check(self, tool: str) -> bool:
        """
        Determine if a tool requires element safety validation.
        
        Args:
            tool: The MCP tool name
            
        Returns:
            bool: True if tool requires safety check
        """
        interactive_tools = {
            'browser_click', 'browser_hover', 'browser_type', 'browser_select',
            'browser_fill', 'browser_drag', 'browser_scroll', 'browser_upload'
        }
        return tool in interactive_tools
    
    async def _validate_element_safety(self, original_step: str, llm_response: MCPPlayWrightLLMResponse) -> bool:
        """
        Enhanced safety validation for all interactive actions to prevent wrong element interactions.
        This is an additional layer of protection beyond regular step validation.
        
        Args:
            original_step: The original BDD step
            llm_response: The LLM response with interactive action
            
        Returns:
            bool: True if safe to interact, False if element seems wrong
        """
        try:
            if not llm_response.input or 'element' not in llm_response.input:
                logger.error("❌ Click safety check failed: No element specified")
                return False
            
            llm_element = llm_response.input['element']
            step_lower = original_step.lower()
            element_lower = llm_element.lower()
            
            # Extract key words from the step
            step_keywords = self._extract_step_keywords(original_step)
            element_keywords = self._extract_element_keywords(llm_element)
            
            logger.info(f"🛡️ ELEMENT SAFETY CHECK:")
            logger.info(f"   Action: '{llm_response.tool}'")
            logger.info(f"   Step: '{original_step}'")
            logger.info(f"   LLM Element: '{llm_element}'")
            logger.info(f"   Step Keywords: {step_keywords}")
            logger.info(f"   Element Keywords: {element_keywords}")
            
            # Safety checks for common dangerous mismatches
            safety_checks = [
                self._check_button_type_mismatch(step_keywords, element_keywords),
                self._check_action_element_mismatch(step_keywords, element_keywords),
                self._check_form_field_confusion(step_keywords, element_keywords, llm_response.tool),
                self._check_navigation_element_mismatch(step_keywords, element_keywords),
                self._check_input_action_mismatch(step_keywords, element_keywords, llm_response.tool)
            ]
            
            # All safety checks must pass
            all_safe = all(safety_checks)
            
            if all_safe:
                logger.info(f"✅ ELEMENT SAFETY: Element is safe for {llm_response.tool}")
                return True
            else:
                logger.warning(f"⚠️ ELEMENT SAFETY: Element may not be safe for {llm_response.tool}")
                
                # Give one more chance using enhanced step verifier validation
                # Extract the element from the step for proper comparison
                parsed_action, parsed_element = self.step_verifier._parse_step_components(original_step)
                
                # Use step verifier's enhanced element matching (includes synonyms)
                element_valid = self.step_verifier._validate_element_match(
                    parsed_element, 
                    llm_element
                )
                
                if element_valid:
                    confidence = self.step_verifier._calculate_element_confidence(parsed_element, llm_element)
                    logger.info(f"✅ ELEMENT SAFETY: Step verifier validation override (confidence: {confidence:.1f}%)")
                    return True
                    
                # Fallback to basic similarity for edge cases
                similarity = self.step_verifier._levenshtein_similarity(original_step, llm_element) * 100
                if similarity >= 90.0:
                    logger.info(f"✅ ELEMENT SAFETY: High similarity override ({similarity:.1f}%)")
                    return True
                    
                logger.error(f"❌ ELEMENT SAFETY: Failed safety checks and validation ({similarity:.1f}%)")
                return False
                
        except Exception as e:
            logger.error(f"❌ Click safety validation error: {e}")
            return False
    
    def _extract_step_keywords(self, step: str) -> set:
        """Extract meaningful keywords from the step"""
        import re
        
        # Remove BDD keywords and common words
        cleaned = re.sub(r'\b(when|then|given|and|user|clicks?|on|the|a|an|button|link|field|element)\b', '', step.lower())
        # Extract quoted content and remaining words
        quoted_content = re.findall(r'["\']([^"\'\n]*)["\']', step)
        words = re.findall(r'\b[a-zA-Z]{2,}\b', cleaned)
        
        keywords = set(words + [q.lower() for q in quoted_content])
        keywords.discard('')
        return keywords
    
    def _extract_element_keywords(self, element: str) -> set:
        """Extract meaningful keywords from the element description"""
        import re
        
        # Extract all words first
        words = re.findall(r'\b[a-zA-Z]{2,}\b', element.lower())
        
        # Create keyword set
        keywords = set(words)
        keywords.discard('')
        
        # Remove only truly generic words, but keep UI type indicators that safety checks need
        generic_words = {'element', 'div', 'span', 'label', 'container', 'wrapper'}
        keywords = keywords - generic_words
        
        return keywords
    
    def _check_button_type_mismatch(self, step_keywords: set, element_keywords: set) -> bool:
        """Check if user wants to click a specific button type but element is different"""
        button_types = {'login', 'submit', 'cancel', 'save', 'delete', 'add', 'cart', 'checkout', 'search'}
        
        step_button_types = step_keywords.intersection(button_types)
        element_button_types = element_keywords.intersection(button_types)
        
        if step_button_types and element_button_types:
            # Both have button types - they should match
            return len(step_button_types.intersection(element_button_types)) > 0
        
        return True  # No specific button type conflict
    
    def _check_action_element_mismatch(self, step_keywords: set, element_keywords: set) -> bool:
        """Check for obvious action/element mismatches"""
        # Keywords that should appear in both step and element for safety
        important_keywords = {'login', 'signup', 'register', 'checkout', 'cart', 'search', 'filter', 'sort'}
        
        step_important = step_keywords.intersection(important_keywords)
        element_important = element_keywords.intersection(important_keywords)
        
        if step_important:
            # If step has important keywords, element should have at least one match
            return len(step_important.intersection(element_important)) > 0
        
        return True  # No critical keywords to check
    
    def _check_form_field_confusion(self, step_keywords: set, element_keywords: set, action_tool: str) -> bool:
        """Check for form field and action type mismatches"""
        field_indicators = {'input', 'field', 'textbox', 'textarea', 'email', 'password', 'username'}
        button_indicators = {'button', 'btn', 'submit', 'click'}
        
        step_wants_button = len(step_keywords.intersection(button_indicators)) > 0
        element_is_field = len(element_keywords.intersection(field_indicators)) > 0
        
        # Check for button/field confusion
        if step_wants_button and element_is_field and action_tool == 'browser_click':
            logger.warning("⚠️ Safety check: Step wants button but element seems like form field")
            return False
        
        # Check for typing action on non-input elements
        if action_tool in ['browser_type', 'browser_fill'] and not element_is_field:
            step_wants_input = any(word in step_keywords for word in {'type', 'enter', 'fill', 'input'})
            if step_wants_input:
                logger.warning("⚠️ Safety check: Step wants to type but element is not an input field")
                return False
        
        return True
    
    def _check_input_action_mismatch(self, step_keywords: set, element_keywords: set, action_tool: str) -> bool:
        """Check for input action mismatches"""
        input_actions = {'browser_type', 'browser_fill'}
        selection_actions = {'browser_select'}
        
        if action_tool in input_actions:
            # For typing actions, element should be an input field
            field_indicators = {'input', 'field', 'textbox', 'textarea', 'email', 'password', 'username', 'search'}
            element_is_field = len(element_keywords.intersection(field_indicators)) > 0
            
            if not element_is_field:
                # Check if element description suggests it's an input
                element_str = ' '.join(element_keywords).lower()
                if not any(indicator in element_str for indicator in ['input', 'field', 'text', 'box']):
                    logger.warning(f"⚠️ Safety check: {action_tool} action on non-input element")
                    return False
        
        elif action_tool in selection_actions:
            # For select actions, element should be a dropdown/select
            select_indicators = {'select', 'dropdown', 'option', 'menu', 'list'}
            element_is_select = len(element_keywords.intersection(select_indicators)) > 0
            
            if not element_is_select:
                logger.warning("⚠️ Safety check: Select action on non-dropdown element")
                return False
        
        return True
    
    def _check_navigation_element_mismatch(self, step_keywords: set, element_keywords: set) -> bool:
        """Check for navigation element mismatches"""
        nav_keywords = {'home', 'menu', 'nav', 'header', 'footer', 'sidebar', 'dropdown'}
        content_keywords = {'product', 'item', 'article', 'content', 'description'}
        
        step_nav = step_keywords.intersection(nav_keywords)
        element_content = element_keywords.intersection(content_keywords)
        
        if step_nav and element_content:
            logger.warning("⚠️ Safety check: Step wants navigation but element seems like content")
            return False
        
        return True
    
    def _get_llm(self):
        """Get LLM instance for tool selection"""
        config = Config(read_timeout=60, connect_timeout=30)
        bedrock_client = boto3.client('bedrock-runtime', config=config, region_name="us-east-1")
        return ChatBedrockConverse(
            model=PLATFORM_CONFIG["BEDROCK_MODEL_ID"],
            client=bedrock_client,
            max_tokens=2000,
            temperature=0.1
        )
    
    async def _normalize_iframe_locator(self, unstable_locator: str) -> str:
        try:
            logger.info("🔧 Normalizing unstable iframe locator using LLM...")
            
            prompt = f"""Transform this Playwright iframe locator to use stable selectors by replacing dynamic session IDs with partial matching:

UNSTABLE LOCATOR:
{unstable_locator}

⚠️ CRITICAL TRUNCATION REQUIREMENT:
YOU MUST truncate ANY dynamic identifier longer than 8 characters to EXACTLY 8 characters.
DO NOT include the full dynamic part - this defeats the purpose of creating a stable locator.

STEP-BY-STEP PROCESS:
1. Identify the stable prefix (e.g., "__zoid__paypal_buttons__")
2. Find the dynamic part after the stable prefix
3. Take ONLY the first 8 characters of the dynamic part
4. Create iframe[attribute^="stable_prefix + first_8_chars"]
5. Preserve everything after .contentFrame()

EXAMPLES WITH EXACT TRUNCATION:
Input: iframe[name="__zoid__paypal_buttons__eyJzZW5kZXIiOnsiZG9tYWluIjoiaHR0cHM6Ly93d3ctc3RnLmNoZXd5Lm5ldCJ9LCJtZXRhRGF0YSI6eyJ3aW5kb3dSZWYi...VERY_LONG..."]
✅ CORRECT: iframe[name^="__zoid__paypal_buttons__eyJzZW5k"]
❌ WRONG: iframe[name^="__zoid__paypal_buttons__eyJzZW5kZXIiOnsiZG9tYWluIjoiaHR0cHM6Ly93d3ctc3RnLmNoZXd5Lm5ldCJ9LCJtZXRhRGF0YSI6eyJ3aW5kb3dSZWYi..."]

Input: iframe[id="frame_abc12345-6789-abcd-ef01-234567890123_suffix"]
✅ CORRECT: iframe[id^="frame_abc12345_suffix"]
❌ WRONG: iframe[id^="frame_abc12345-6789-abcd-ef01-234567890123_suffix"]

Input: iframe[data-id="widget_987654321098765432109876543210987654_end"]  
✅ CORRECT: iframe[data-id^="widget_98765432_end"]
❌ WRONG: iframe[data-id^="widget_987654321098765432109876543210987654_end"]

MANDATORY REQUIREMENTS:
1. Use iframe[attribute^="..."] syntax with starts-with operator
2. Include .contentFrame() for iframe content access  
3. Preserve exact targeting after contentFrame()
4. TRUNCATE dynamic parts to exactly 8 characters
5. Return ONLY the stable locator - no explanations
6.return only the final transformed locator in response content. No additional text.

For the input locator above, the dynamic part after "__zoid__paypal_buttons__" is "eyJzZW5k..." 
You MUST truncate this to exactly "eyJzZW5k" (8 characters).

TRANSFORM THIS LOCATOR:"""

            llm = self._get_llm()
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            
            # HANDLE DIFFERENT RESPONSE FORMATS - Fix for list structure
            normalized_locator = ""
            if hasattr(response, 'content'):
                content = response.content
                
                # Handle list format: [{'type': 'text', 'text': '...'}]
                if isinstance(content, list) and len(content) > 0:
                    for item in content:
                        if isinstance(item, dict) and 'text' in item:
                            normalized_locator = item['text'].strip()
                            break
                # Handle direct string format
                elif isinstance(content, str):
                    normalized_locator = content.strip()
                else:
                    logger.warning(f"⚠️ IFRAME-NORMALIZE: Unexpected content type: {type(content)}")
                    normalized_locator = str(content).strip()
            else:
                logger.warning(f"⚠️ IFRAME-NORMALIZE: No content attribute in response")
                normalized_locator = str(response).strip()
            
            logger.info(f"🔍 IFRAME-NORMALIZE: Raw LLM response: {normalized_locator[:200]}...")
            
            # Remove any markdown formatting if present
            if normalized_locator.startswith('```'):
                lines = normalized_locator.split('\n')
                for line in lines:
                    if line.strip() and not line.strip().startswith('```') and 'iframe[' in line:
                        normalized_locator = line.strip()
                        break
            
            # Validate that the response is in the expected format
            if not normalized_locator.startswith('iframe['):
                logger.warning(f"⚠️ LLM returned unexpected format: {normalized_locator}")
                logger.info("🔄 Falling back to regex-based normalization...")
                return self._make_locator_stable(unstable_locator)
            
            # Ensure .contentFrame() is present
            if '.contentFrame()' not in normalized_locator:
                logger.warning(f"⚠️ LLM output missing .contentFrame(): {normalized_locator}")
                logger.info("🔄 Falling back to regex-based normalization...")
                return self._make_locator_stable(unstable_locator)
            
            logger.info(f"✅ LLM normalized locator: {normalized_locator[:150]}...")
            return normalized_locator
        
        except Exception as e:
            logger.error(f"❌ Error in LLM iframe locator normalization: {e}")
            logger.info("🔄 Falling back to regex-based normalization...")
            return self._make_locator_stable(unstable_locator)
    
    def _is_navigation_causing_step(self, step: str, llm_response: MCPPlayWrightLLMResponse) -> bool:
        """Check if the step involves any navigation/popup causing interaction"""
        # Check step text for navigation indicators
        step_lower = step.lower()
        navigation_step_indicators = [
            'paypal', 'checkout', 'sign in', 'login', 'log in', 
            'continue', 'proceed', 'submit', 'pay now', 'buy now',
            'add to cart', 'place order', 'complete order', 'confirm',
            'next', 'finish', 'done', 'register', 'create account',
            'google', 'facebook', 'apple', 'microsoft', 'oauth',
            'external', 'redirect', 'link'
        ]
        
        # Check element text/description for navigation indicators  
        element_text = ''
        if llm_response.input and 'element' in llm_response.input:
            element_text = str(llm_response.input['element']).lower()
        
        navigation_element_indicators = [
            'submit', 'continue', 'next', 'proceed', 'confirm', 'pay',
            'login', 'sign in', 'checkout', 'buy', 'purchase', 'order',
            'register', 'create', 'complete', 'finish', 'done'
        ]
        
        # Check if step or element suggests navigation
        step_suggests_navigation = any(indicator in step_lower for indicator in navigation_step_indicators)
        element_suggests_navigation = any(indicator in element_text for indicator in navigation_element_indicators)
        
        # Check if it's a click action (most likely to cause navigation)
        is_click_action = (llm_response.tool == 'browser_click' or 
                          'click' in step_lower or 
                          'clicks' in step_lower)
        
        return is_click_action and (step_suggests_navigation or element_suggests_navigation)
    
    async def _check_navigation_success_pattern(self, step: str, mcp_response: MCPResult, llm_response: MCPPlayWrightLLMResponse) -> bool:
        """Check if a navigation/popup interaction was actually successful despite error flag"""
        try:
            # Check if the error message contains success indicators
            error_output = mcp_response.output.lower() if mcp_response.output else ''
            
            # CRITICAL FIX: Check for click interception first - these should NOT be treated as navigation success
            click_interception_patterns = [
                'intercepts pointer events',
                'intercepts pointer',
                'overlapped by',
                'covered by',
                'click was intercepted',
                'element not clickable'
            ]
            
            # If this is click interception for a radio button, don't treat as navigation success
            has_click_interception = any(pattern in error_output for pattern in click_interception_patterns)
            if has_click_interception:
                # Extra check for radio button context to be more specific
                step_lower = step.lower() if step else ''
                is_radio_step = any(indicator in step_lower for indicator in ['radio', 'buy once', 'checkbox'])
                
                if is_radio_step:
                    logger.info(f"🛡️ INTERCEPTION-BLOCK: Not treating radio button click interception as navigation success")
                    return False
                
                # For non-radio elements, also be more cautious about click interception
                logger.warning(f"⚠️ INTERCEPTION-DETECTED: Click interception found, being cautious about navigation success")
            
            # Comprehensive success patterns for any navigation/popup scenario
            success_patterns = [
                'execution context was destroyed',
                'most likely because of a navigation',
                'navigation',
                'new tab',
                'popup',
                'ran playwright code',
                'context destroyed',
                'page unloaded',
                'target closed',
                'frame detached'
            ]
            
            # If error contains success patterns, likely successful (unless click interception detected above)
            has_success_pattern = any(pattern in error_output for pattern in success_patterns)
            
            if has_success_pattern and not has_click_interception:
                logger.info(f"🔍 Navigation success pattern detected in error: {[p for p in success_patterns if p in error_output]}")
                
                # Additional verification checks
                verification_passed = False
                
                # Check 1: Verify if new tab/popup was created
                try:
                    tabs_result = await self.mcp_client.execute_tool("browser_tabs", action="list")
                    if tabs_result and isinstance(tabs_result, dict):
                        tabs = tabs_result.get('tabs', [])
                        if len(tabs) > 1:
                            logger.info(f"✅ New tab/popup confirmed - {len(tabs)} tabs detected")
                            verification_passed = True
                except Exception as tab_check_error:
                    logger.warning(f"⚠️ Could not verify tabs: {tab_check_error}")
                
                # Check 2: See if page URL changed (for same-tab navigation)
                try:
                    current_snapshot = await self.capture_page_snapshot()
                    if 'page url:' in current_snapshot.lower() and llm_response.page_url:
                        # If URLs differ, navigation likely occurred
                        verification_passed = True
                except Exception as url_check_error:
                    logger.warning(f"⚠️ Could not verify URL change: {url_check_error}")
                
                # Check 3: If we see specific "ran playwright code" pattern, trust it
                if 'ran playwright code' in error_output:
                    logger.info("✅ Playwright code execution confirmed")
                    verification_passed = True
                
                # If any verification passed OR strong success pattern found, consider successful
                if verification_passed or 'execution context was destroyed' in error_output:
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"❌ Error checking navigation success pattern: {e}")
            return False
            
        except Exception as e:
            logger.warning(f"⚠️ LLM normalization failed: {e}")
            # Fallback to existing stable locator logic
            logger.info("🔄 Falling back to regex-based normalization...")
            return self._make_locator_stable(unstable_locator)
    
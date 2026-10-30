"""
Comprehensive MCP Service that integrates all agent layer components
Equivalent to Java's integrated MCP functionality
"""

import asyncio
import logging
from typing import List, Dict, Optional, Any
from dataclasses import dataclass

from .mcp_client_adapter import UniversalMCPClient
from .mcp_utilization import ScenarioStep, MCPPlayWrightLLMResponse, MCPResult
from .mcp_enhanced_utilization import UiAutomationServiceWithLocatorReuse
from .custom_tools import CustomTools
# from services.agent_layer.mcp_crawl_first_runner import McpCrawlFirstRunner
# from services.agent_layer.playwright_heuristic_agent import PlaywrightHeuristicAgent
# from services.agent_layer.page_ops import PageOps
# from services.agent_layer.mcp_utils import MCPUtils
from services.llm_layer.LLM import LLM

logger = logging.getLogger(__name__)


@dataclass
class MCPServiceConfig:
    """Configuration for MCP Service"""
    max_retries: int = 3
    retry_base_ms: int = 1000
    enable_heuristics: bool = True
    enable_vector_search: bool = False  # Can be enabled when vector store is implemented
    domain_selection: Optional[str] = None  # Domain for MCP UID generation


class MCPService:
    """
    Comprehensive MCP Service that provides all functionality from Java implementation
    Integrates all agent layer components for complete automation
    """
    
    def __init__(self, config: MCPServiceConfig):
        self.config = config
        self.mcp_client: Optional[UniversalMCPClient] = None
        self.ui_automation_service: Optional[UiAutomationServiceWithLocatorReuse] = None
        self.crawl_first_runner: Optional[McpCrawlFirstRunner] = None
        self.heuristic_agent: Optional[PlaywrightHeuristicAgent] = None
        self.page_ops: Optional[PageOps] = None
        self.mcp_utils: Optional[MCPUtils] = None
        self.llm: Optional[LLM] = None
        self.custom_tools: Optional[CustomTools] = None
        self._initialized = False
    
    async def __aenter__(self):
        """Async context manager entry"""
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.cleanup()
    
    async def initialize(self):
        """Initialize all MCP service components"""
        if self._initialized:
            return
        
        try:
            logger.info("🚀 Initializing MCP Service...")
            
            # 1. Initialize MCP client
            self.mcp_client = UniversalMCPClient()
            await self.mcp_client.connect()
            logger.info("✅ MCP client connected")
            
            # 2. Initialize LLM
            self.llm = LLM()
            logger.info("✅ LLM initialized")
            
            # 3. Initialize UI automation service with locator reuse
            self.ui_automation_service = UiAutomationServiceWithLocatorReuse(domain_selection=self.config.domain_selection)
            self.ui_automation_service.mcp_client = self.mcp_client  # Reuse the same client
            logger.info(f"✅ UI automation service with locator reuse initialized | Domain: {self.config.domain_selection or 'None (legacy format)'}")
            
            # 4. Initialize custom tools
            self.custom_tools = CustomTools()
            logger.info("✅ Custom tools initialized")
            
            # 5. Initialize agent layer components (import locally to avoid circular deps)
            try:
                from services.agent_layer.playwright_heuristic_agent import PlaywrightHeuristicAgent
                from services.agent_layer.page_ops import PageOps
                from services.agent_layer.mcp_utils import MCPUtils
                from services.agent_layer.mcp_crawl_first_runner import McpCrawlFirstRunner
                
                self.heuristic_agent = PlaywrightHeuristicAgent(
                    max_retries=self.config.max_retries,
                    retry_base_ms=self.config.retry_base_ms
                )
                logger.info("✅ Heuristic agent initialized")
                
                self.page_ops = PageOps(self.mcp_client)
                logger.info("✅ Page operations initialized")
                
                self.mcp_utils = MCPUtils(self.mcp_client)
                logger.info("✅ MCP utils initialized")
                
                self.crawl_first_runner = McpCrawlFirstRunner(
                    mcp_client=self.mcp_client,
                    llm=self.llm,
                    heuristic_agent=self.heuristic_agent,
                    page_ops=self.page_ops,
                    ui_automation_service=self.ui_automation_service
                )
                logger.info("✅ Crawl-first runner initialized")
                
            except ImportError as e:
                logger.warning(f"⚠️ Agent layer components not available: {e}")
                self.heuristic_agent = None
                self.page_ops = None
                self.mcp_utils = None
                self.crawl_first_runner = None
            
            self._initialized = True
            logger.info("🎉 MCP Service fully initialized")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize MCP Service: {e}")
            await self.cleanup()
            raise
    
    async def cleanup(self):
        """Cleanup all resources"""
        try:
            if self.ui_automation_service:
                await self.ui_automation_service.disconnect()
            
            if self.mcp_client:
                await self.mcp_client.disconnect()
            
            self._initialized = False
            logger.info("🧹 MCP Service cleaned up")
            
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
    
    async def execute_scenario_steps(self, steps: List[str]) -> List[MCPPlayWrightLLMResponse]:
        """
        Execute a list of BDD scenario steps using LLM tool selection and retry logic
        """
        if not self._initialized:
            await self.initialize()
        
        logger.info(f"🎬 Executing {len(steps)} scenario steps with retry logic")
        
        try:
            # Use UiAutomationService with retry logic
            result = await self.ui_automation_service.execute_with_retry_logic(steps)
            
            logger.info(f"✅ Successfully executed steps with retry logic")
            return result
            
        except Exception as e:
            logger.error(f"❌ Failed to execute scenario steps: {e}")
            raise
    
    async def execute_single_step(self, step: str) -> MCPPlayWrightLLMResponse:
        """Execute a single BDD step"""
        results = await self.execute_scenario_steps([step])
        return results[0] if results else None
    
    async def try_heuristic_execution(self, step: str) -> Optional[MCPPlayWrightLLMResponse]:
        """
        Try to execute a step using heuristics only (no LLM)
        Returns None if heuristics cannot handle the step
        """
        if not self._initialized:
            await self.initialize()
        
        if not self.config.enable_heuristics:
            return None
        
        scenario_step = ScenarioStep(step=step)
        return await self.heuristic_agent.try_perform_generic(scenario_step)
    
    async def chat_with_steps(self, steps: List[str]) -> str:
        """
        Execute steps and return TypeScript code generation
        Equivalent to Java's chat functionality
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # Execute steps using UI automation service
            result = await self.ui_automation_service.chat(steps)
            return result
            
        except Exception as e:
            logger.error(f"❌ Chat execution failed: {e}")
            raise
    
    async def navigate_to_url(self, url: str) -> MCPResult:
        """Navigate to a specific URL"""
        if not self._initialized:
            await self.initialize()
        
        plan = await self.page_ops.make_navigate_plan(url)
        return await self.mcp_utils.execute_playwright_mcp_command(plan)
    
    async def capture_page_state(self) -> Dict[str, Any]:
        """Capture comprehensive page state information"""
        if not self._initialized:
            await self.initialize()
        
        try:
            snapshot = await self.mcp_utils.capture_page_snapshot()
            current_url = await self.mcp_utils.get_current_url()
            console_messages = await self.mcp_utils.get_console_messages()
            
            return {
                "snapshot": snapshot,
                "url": current_url,
                "console_messages": console_messages,
                "timestamp": asyncio.get_event_loop().time()
            }
            
        except Exception as e:
            logger.error(f"Failed to capture page state: {e}")
            return {"error": str(e)}
    
    async def reset_browser_state(self):
        """Reset browser to clean state"""
        if not self._initialized:
            await self.initialize()
        
        await self.mcp_utils.hard_reset_browser()
        logger.info("🔄 Browser state reset completed")
    
    async def get_available_tools(self) -> List[str]:
        """Get list of available MCP tools"""
        if not self._initialized:
            await self.initialize()
        
        return await self.mcp_utils.list_tool_names()
    
    async def execute_raw_mcp_command(self, tool: str, **kwargs) -> MCPResult:
        """Execute raw MCP command with parameters"""
        if not self._initialized:
            await self.initialize()
        
        return await self.mcp_utils.call(tool, kwargs)
    
    async def wait_for_condition(self, condition_type: str, value: str, timeout: int = 30):
        """
        Wait for various conditions
        condition_type: 'text', 'url', 'element', 'time'
        """
        if not self._initialized:
            await self.initialize()
        
        if condition_type == "text":
            await self.mcp_utils.wait_for_condition(text=value)
        elif condition_type == "url":
            # Wait for URL to contain value
            await self.mcp_utils.wait_for_condition(text=value)  # Simplified
        elif condition_type == "time":
            await self.mcp_utils.wait_for_condition(time=float(value))
        else:
            raise ValueError(f"Unsupported condition type: {condition_type}")
    
    async def take_screenshot(self, filename: Optional[str] = None) -> str:
        """Take a screenshot and return the path/result"""
        if not self._initialized:
            await self.initialize()
        
        return await self.mcp_utils.take_screenshot(filename)
    
    async def resize_browser(self, width: int, height: int):
        """Resize browser window"""
        if not self._initialized:
            await self.initialize()
        
        await self.mcp_utils.resize_browser(width, height)
    
    # Custom Tools Integration
    async def api_call(self, config_file: str, variables: Dict[str, Any] = None) -> Dict[str, Any]:
        """Make API call using custom tools"""
        if not self._initialized:
            await self.initialize()
        
        return await self.custom_tools.api_service_call(config_file, variables)
    
    def store_data(self, key: str, data: Any):
        """Store data using custom tools"""
        if not self._initialized:
            raise RuntimeError("Service not initialized")
        
        self.custom_tools.store_data(key, data)
    
    def get_data(self, key: str) -> Any:
        """Get stored data using custom tools"""
        if not self._initialized:
            raise RuntimeError("Service not initialized")
        
        return self.custom_tools.get_data(key)
    
    async def get_service_status(self) -> Dict[str, Any]:
        """Get comprehensive service status"""
        return {
            "initialized": self._initialized,
            "mcp_client_connected": self.mcp_client is not None,
            "components": {
                "ui_automation_service": self.ui_automation_service is not None,
                "crawl_first_runner": self.crawl_first_runner is not None,
                "heuristic_agent": self.heuristic_agent is not None,
                "page_ops": self.page_ops is not None,
                "mcp_utils": self.mcp_utils is not None,
                "llm": self.llm is not None
            },
            "config": {
                "max_retries": self.config.max_retries,
                "retry_base_ms": self.config.retry_base_ms,
                "enable_heuristics": self.config.enable_heuristics,
                "enable_vector_search": self.config.enable_vector_search
            }
        }


# Convenience factory function
async def create_mcp_service(
    max_retries: int = 3,
    enable_heuristics: bool = True
) -> MCPService:
    """
    Factory function to create and initialize MCP service
    """
    service_config = MCPServiceConfig(
        max_retries=max_retries,
        enable_heuristics=enable_heuristics
    )
    
    service = MCPService(service_config)
    await service.initialize()
    return service
"""
Unified MCP Client for both local and remote
Uses langchain-mcp-adapters directly for both transports
"""
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional
from langchain_mcp_adapters.client import MultiServerMCPClient, load_mcp_tools

class UnifiedMCPClient:
    """
    Unified MCP client using langchain-mcp-adapters
    - Local: stdio transport
    - Remote: SSE transport
    """
    
    def __init__(self):
        self.client: Optional[MultiServerMCPClient] = None
        self._session = None
        self._session_context = None
        self._tools = None
        self._is_remote = False
        self.session_id: Optional[str] = None  # ✅ Store session ID
        self.logger = logging.getLogger(__name__)
    
    async def connect(self):
        """Connect using langchain-mcp-adapters"""
        # Check if explicitly requesting local MCP (dev debugging only)
        use_local_mcp = os.getenv('LOCAL_MCP', 'false').lower() == 'true'
        
        if use_local_mcp:
            # Local MCP (only when LOCAL_MCP=true in .env)
            self._is_remote = False
            
            config = {
                "playwright": {
                    "command": "npx",
                    "args": [
                        "@playwright/mcp",
                        "--browser", "chrome",
                        "--caps=" + os.getenv('MCP_CAPABILITIES', 'testing,vision,pdf,tracing'),
                        "--isolated",
                        "--block-service-workers",
                        "--save-trace"
                    ],
                    "transport": "stdio"
                }
            }
            self.logger.info("💻 Using LOCAL MCP (stdio)")
        else:
            # Default: Team lead's remote MCP (for both local dev and ECS)
            self._is_remote = True
            
            # Hardcoded remote MCP endpoint
            remote_endpoint = 'https://playwright-mcp.corp-ai-workloads.stg.aws.chewy.cloud/sse'
            
            config = {
                "playwright": {
                    "url": remote_endpoint,
                    "transport": "sse"
                }
            }
            self.logger.info(f"📡 Using REMOTE MCP: {remote_endpoint}")
        
        self.client = MultiServerMCPClient(config)
        self.logger.info(f"✅ Connected ({'remote' if self._is_remote else 'local'})")
        
        # Extract session ID for remote connections
        if self._is_remote:
            await self._extract_and_log_session_id(remote_endpoint)
    
    async def disconnect(self):
        """Disconnect"""
        if self._session:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
            self._session_context = None
            self._tools = None
        
        self.client = None
        if self.session_id:
            self.logger.info(f"✅ Disconnected (session: {self.session_id[:8]}...)")
        else:
            self.logger.info("✅ Disconnected")
    
    async def _extract_and_log_session_id(self, sse_url: str):
        """Extract session ID from SSE endpoint's initial event"""
        try:
            import httpx
            import re
            
            self.logger.info("🔍 Attempting to extract session ID from SSE endpoint...")
            
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("GET", sse_url, headers={"Accept": "text/event-stream"}) as response:
                    if response.status_code != 200:
                        self.logger.warning(f"⚠️ SSE endpoint returned status {response.status_code}")
                        return
                    
                    # Read first few events to find session ID
                    event_count = 0
                    max_events = 5
                    
                    async for line in response.aiter_lines():
                        event_count += 1
                        if event_count > max_events:
                            break
                        
                        # Look for sessionId in event data
                        # Format: data: /sse?sessionId=6eabf5b8-6256-4f59-be2f-dd77d3a2b9b4
                        session_match = re.search(r'sessionId=([a-f0-9-]{36})', line, re.IGNORECASE)
                        if session_match:
                            self.session_id = session_match.group(1)
                            self.logger.info(f"✅ Session ID extracted: {self.session_id}")
                            self.logger.info(f"📋 Full session endpoint: /sse?sessionId={self.session_id}")
                            return
                    
                    self.logger.warning("⚠️ Session ID not found in first 5 SSE events")
                    
        except asyncio.TimeoutError:
            self.logger.warning("⚠️ Timeout while extracting session ID")
        except Exception as e:
            self.logger.warning(f"⚠️ Failed to extract session ID: {e}")
    
    async def execute_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute tool with DOM waiting and modal blocking for interactive actions"""
        if not self.client:
            raise RuntimeError("Not connected")
        
        # Create or reuse session
        if not self._session:
            self.logger.info("🎆 Creating MCP session")
            self._session = self.client.session("playwright")
            self._session_context = await self._session.__aenter__()
            self._tools = await load_mcp_tools(self._session_context)
            if self.session_id:
                self.logger.info(f"✅ Session initialized (ID: {self.session_id})")
            else:
                self.logger.info("✅ Session initialized")
        
        # Wait for DOM to be ready for interactive actions
        interactive_actions = ['browser_click', 'browser_type', 'browser_select_option',
                             'browser_hover', 'browser_drag', 'browser_run_code']
        if tool_name in interactive_actions:
            self.logger.info("⏳ Waiting for DOM to be ready...")
            dom_ready = await self.wait_for_dom_ready()
            if not dom_ready:
                self.logger.warning("⚠️ Proceeding with action despite DOM readiness timeout")
            
            # Apply modal blocking before action
            # self.logger.info("🚫 Applying modal blocking before action...")
            # await self._apply_modal_blocking()
            
            # Additional stability wait for click actions
            if tool_name == 'browser_click':
                await asyncio.sleep(1.0)
        
        # Execute tool
        tool = next((t for t in self._tools if t.name == tool_name), None)
        if not tool:
            raise ValueError(f"Tool '{tool_name}' not found")
        
        return await tool.ainvoke(kwargs)
    
    async def wait_for_dom_ready(self, timeout: float = 30.0) -> bool:
        """Wait for DOM to be fully loaded and ready"""
        try:
            max_attempts = int(timeout * 10)
            for _ in range(max_attempts):
                try:
                    result = await self.execute_tool("browser_evaluate", function="() => { return document.readyState === 'complete' && document.body && document.body.offsetHeight > 0; }")
                    if result and 'true' in str(result).lower():
                        self.logger.info("✅ DOM is ready and stable")
                        await asyncio.sleep(0.5)
                        return True
                except Exception:
                    pass
                await asyncio.sleep(0.1)
            
            self.logger.warning(f"⚠️ DOM readiness check timed out after {timeout}s")
            return False
            
        except Exception as e:
            self.logger.error(f"❌ Error checking DOM readiness: {e}")
            return False
    
    async def _log_viewport_info(self):
        """Log current viewport size for debugging"""
        try:
            viewport_script = "() => { return window.innerWidth + 'x' + window.innerHeight; }"
            result = await self.execute_tool("browser_evaluate", function=viewport_script)
            
            # Clean result - remove MCP formatting
            result_str = str(result).replace('### Result', '').replace('### Ran Playwright code', '').strip()
            
            # Extract viewport from result
            import re
            # Look for pattern like "780x493" or extract from quotes
            viewport_match = re.search(r'(\d{3,4})x(\d{3,4})', result_str)
            if viewport_match:
                self.logger.info(f"📐 Viewport: {viewport_match.group(0)}")
            else:
                self.logger.info(f"📐 Viewport result (raw): {result_str[:50]}")
            
        except Exception as e:
            self.logger.warning(f"⚠️ Could not get viewport info: {type(e).__name__}: {str(e)[:100]}")
    
    async def _wait_for_network_idle(self, timeout: float = 10.0) -> bool:
        """Wait for network to be idle (no pending requests)"""
        try:
            network_idle_script = """
            () => {
                return new Promise((resolve) => {
                    const checkIdle = () => {
                        const resources = performance.getEntriesByType('resource');
                        const recentResources = resources.filter(r => {
                            const age = performance.now() - r.responseEnd;
                            return age < 1000 && age > 0;
                        });
                        
                        if (recentResources.length === 0) {
                            resolve(true);
                        } else {
                            setTimeout(checkIdle, 500);
                        }
                    };
                    setTimeout(checkIdle, 500);
                    setTimeout(() => resolve(false), 10000);
                });
            }
            """
            
            result = await self.execute_tool("browser_evaluate", function=network_idle_script)
            
            if result and 'true' in str(result).lower():
                self.logger.info("✅ Network is idle")
                return True
            else:
                self.logger.warning("⚠️ Network idle check timed out")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Network idle check failed: {e}")
            await asyncio.sleep(2.0)
            return False
    
    async def _apply_modal_blocking(self):
        """Apply modal blocking before actions"""
        try:
            modal_blocking_script = """
            () => {
                // Block OneTrust and similar cookie consent managers
                Object.defineProperty(window, 'OneTrust', { value: undefined, writable: false });
                Object.defineProperty(window, 'OptanonWrapper', { value: undefined, writable: false });
                Object.defineProperty(window, 'Optanon', { value: undefined, writable: false });
                
                // Hide common cookie banner selectors and Chewy-specific blocking elements
                const style = document.createElement('style');
                style.textContent = `
                    #onetrust-consent-sdk,
                    #onetrust-banner-sdk,
                    .ot-sdk-container,
                    .cookie-banner,
                    .cookie-consent,
                    .gdpr-banner,
                    [id*="cookie"][id*="banner"],
                    [class*="cookie"][class*="banner"],
                    [id*="consent"][id*="banner"],
                    [class*="consent"][class*="banner"],
                    .kib-sheet__backdrop.kib-transition--fade,
                    .kib-sheet-step__content.kib-sheet-step__content--at-start,
                    [data-testid="smartshelf-content"][data-promotionitemsincartcount="0"] {
                        display: none !important;
                        visibility: hidden !important;
                        opacity: 0 !important;
                        height: 0 !important;
                        width: 0 !important;
                        z-index: -9999 !important;
                        pointer-events: none !important;
                    }
                `;
                document.head.appendChild(style);
                return 'Modal blocking applied';
            }
            """
            
            await self.execute_tool("browser_evaluate", function=modal_blocking_script)
            self.logger.info("✅ Modal blocking applied successfully")
        except Exception as e:
            self.logger.error(f"❌ Modal blocking failed: {e}")
    
    async def list_available_tools(self) -> List[str]:
        """List all available tools"""
        if not self.client:
            raise RuntimeError("Client not connected")
        
        # Use the existing session and tools if available
        if hasattr(self, '_tools') and self._tools:
            return [tool.name for tool in self._tools]
        
        # If no existing session, create a temporary one to get tools
        try:
            async with self.client.session("playwright") as session:
                tools = await load_mcp_tools(session)
                return [tool.name for tool in tools]
        except ImportError:
            # Fallback: return a basic list of known MCP tools
            return [
                "browser_navigate", "browser_click", "browser_type", "browser_select_option",
                "browser_hover", "browser_wait_for", "browser_snapshot", "browser_take_screenshot",
                "browser_close", "browser_resize", "browser_console_messages", "browser_handle_dialog",
                "browser_evaluate", "browser_file_upload", "browser_press_key", "browser_drag",
                "browser_tab_list", "browser_tab_new", "browser_tabs", "browser_tab_close",
                "browser_navigate_back", "browser_navigate_forward", "browser_network_requests",
                "browser_pdf_save", "browser_generate_locator", "browser_verify_element_visible",
                "browser_verify_list_visible", "browser_verify_text_visible", "browser_verify_value",
                "browser_start_tracing", "browser_stop_tracing"
            ]
    
    # Browser methods
    async def browser_navigate(self, url: str):
        """Navigate to a URL"""
        result = await self.execute_tool("browser_navigate", url=url)
        await self.wait_for_dom_ready()
        return result
    
    async def browser_snapshot(self):
        """Capture accessibility snapshot after DOM is ready"""
        # Log viewport info before snapshot
        self.logger.info("📸 Capturing viewport info before snapshot...")
        await self._log_viewport_info()
        
        await self.wait_for_dom_ready(timeout=15.0)
        await self._wait_for_network_idle()
        await asyncio.sleep(1)  # Additional stability wait
        result = await self.execute_tool("browser_snapshot")
        self.logger.info("📸 Page snapshot captured successfully")
        return result
    
    async def browser_click(self, element: str, ref: str):
        return await self.execute_tool("browser_click", element=element, ref=ref)
    
    async def browser_type(self, element: str, ref: str, text: str, submit=False, slowly=False):
        return await self.execute_tool("browser_type", element=element, ref=ref, text=text, submit=submit, slowly=slowly)
    
    async def browser_select_option(self, element: str, ref: str, values: List[str]):
        return await self.execute_tool("browser_select_option", element=element, ref=ref, values=values)
    
    async def browser_hover(self, element: str, ref: str):
        return await self.execute_tool("browser_hover", element=element, ref=ref)
    
    async def browser_wait_for(self, time: float):
        return await self.execute_tool("browser_wait_for", time=time)
    
    async def browser_generate_locator(self, element: str, ref: str):
        return await self.execute_tool("browser_generate_locator", element=element, ref=ref)
    
    async def browser_resize(self, width: int, height: int):
        return await self.execute_tool("browser_resize", width=width, height=height)
    
    async def browser_tab_list(self):
        return await self.execute_tool("browser_tab_list")
    
    async def browser_tab_new(self, url: Optional[str] = None):
        params = {"url": url} if url else {}
        return await self.execute_tool("browser_tab_new", **params)
    
    async def browser_tab_select(self, index: int):
        return await self.execute_tool("browser_tabs", action="select", index=index)
    
    async def browser_tabs(self, action: str, index: Optional[int] = None):
        params: Dict[str, Any] = {"action": action}
        if index is not None:
            params["index"] = index
        return await self.execute_tool("browser_tabs", **params)
    
    async def browser_close(self):
        """Close the current page"""
        return await self.execute_tool("browser_close")
    
    async def browser_take_screenshot(self, raw: bool = False, element: Optional[str] = None, ref: Optional[str] = None):
        """Take a screenshot"""
        params: Dict[str, Any] = {"raw": raw}
        if element:
            params["element"] = element
        if ref:
            params["ref"] = ref
        return await self.execute_tool("browser_take_screenshot", **params)
    
    async def browser_file_upload(self, paths: List[str]):
        """Upload files"""
        return await self.execute_tool("browser_file_upload", paths=paths)
    
    async def browser_pdf_save(self, filename: Optional[str] = None):
        """Save page as PDF"""
        params: Dict[str, Any] = {}
        if filename:
            params["filename"] = filename
        return await self.execute_tool("browser_pdf_save", **params)
    
    async def browser_handle_dialog(self, accept: bool, prompt_text: Optional[str] = None):
        """Handle a dialog"""
        params: Dict[str, Any] = {"accept": accept}
        if prompt_text:
            params["promptText"] = prompt_text
        return await self.execute_tool("browser_handle_dialog", **params)
    
    async def browser_press_key(self, key: str):
        """Press a key"""
        return await self.execute_tool("browser_press_key", key=key)
    
    async def browser_console_messages(self):
        """Get console messages"""
        return await self.execute_tool("browser_console_messages")
    
    async def browser_navigate_back(self):
        """Navigate back"""
        return await self.execute_tool("browser_navigate_back")
    
    async def browser_navigate_forward(self):
        """Navigate forward"""
        return await self.execute_tool("browser_navigate_forward")
    
    async def browser_network_requests(self):
        """Get network requests"""
        return await self.execute_tool("browser_network_requests")
    
    async def browser_drag(self, element: str, ref: str, x: int, y: int):
        """Drag an element"""
        return await self.execute_tool("browser_drag", element=element, ref=ref, x=x, y=y)
    
    async def browser_tab_close(self, index: Optional[int] = None):
        """Close a tab"""
        params: Dict[str, Any] = {}
        if index is not None:
            params["index"] = index
        return await self.execute_tool("browser_tab_close", **params)
    
    async def browser_generate_playwright_test(self):
        """Generate Playwright test"""
        return await self.execute_tool("browser_generate_playwright_test")
    
    async def browser_install(self):
        """Install browser"""
        return await self.execute_tool("browser_install")
    
    async def browser_fill_form(self, fields: List[Dict]):
        """Fill multiple form fields"""
        return await self.execute_tool("browser_fill_form", fields=fields)
    
    async def browser_mouse_click_xy(self, element: str, x: int, y: int):
        """Click at coordinates"""
        return await self.execute_tool("browser_mouse_click_xy", element=element, x=x, y=y)
    
    async def browser_mouse_drag_xy(self, element: str, startX: int, startY: int, endX: int, endY: int):
        """Drag mouse to coordinates"""
        return await self.execute_tool("browser_mouse_drag_xy", element=element, startX=startX, startY=startY, endX=endX, endY=endY)
    
    async def browser_mouse_move_xy(self, element: str, x: int, y: int):
        """Move mouse to coordinates"""
        return await self.execute_tool("browser_mouse_move_xy", element=element, x=x, y=y)
    
    async def browser_verify_element_visible(self, role: str, accessibleName: str):
        """Verify element is visible"""
        return await self.execute_tool("browser_verify_element_visible", role=role, accessibleName=accessibleName)
    
    async def browser_verify_list_visible(self, element: str, ref: str, items: List[str]):
        """Verify list is visible"""
        return await self.execute_tool("browser_verify_list_visible", element=element, ref=ref, items=items)
    
    async def browser_verify_text_visible(self, text: str):
        """Verify text is visible"""
        return await self.execute_tool("browser_verify_text_visible", text=text)
    
    async def browser_verify_value(self, type: str, element: str, ref: str, value: str):
        """Verify element value"""
        return await self.execute_tool("browser_verify_value", type=type, element=element, ref=ref, value=value)
    
    async def browser_start_tracing(self):
        """Start trace recording - Check if tool exists first"""
        available_tools = await self.list_available_tools()
        if "browser_start_tracing" not in available_tools:
            self.logger.warning("⚠️ browser_start_tracing tool not available in MCP server")
            return {"status": "not_supported", "message": "Tracing not supported by MCP server"}
        return await self.execute_tool("browser_start_tracing")
    
    async def browser_stop_tracing(self):
        """Stop trace recording - Check if tool exists first"""
        available_tools = await self.list_available_tools()
        if "browser_stop_tracing" not in available_tools:
            self.logger.warning("⚠️ browser_stop_tracing tool not available in MCP server")
            return {"status": "not_supported", "message": "Tracing not supported by MCP server"}
        return await self.execute_tool("browser_stop_tracing")
    
    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()


# Backward compatibility
UniversalMCPClient = UnifiedMCPClient
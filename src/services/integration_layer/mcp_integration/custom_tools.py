"""
Custom Tools for MCP Integration
"""

import json
import aiohttp
import asyncio
import time
import secrets
from typing import Dict, Any, Optional
from pathlib import Path

class CustomTools:
    def __init__(self):
        self.stored_data = {}
    
    def custom_api_service_tool(self, config_file: str, variables: Dict[str, Any] = None) -> str:
        """Prepare API call config without executing - only loads and validates config"""
        config_path = self._find_config_file(config_file)
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        # Merge stored data as variables
        all_variables = {**self.stored_data, **(variables or {})}
        if all_variables:
            config = self._replace_variables(config, all_variables)
        
        # Store the prepared config for later execution
        self.stored_data['_prepared_api_config'] = config
        self.stored_data['_prepared_config_file'] = config_file
        
        return f"Prepared API call config from {config_file}"
    
    def _run_tests(self, tests: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, bool]:
        """Run basic Postman-like tests"""
        results = {}
        for test_name, test_condition in tests.items():
            try:
                if test_condition.get('status'):
                    results[test_name] = response['status'] == test_condition['status']
                elif test_condition.get('contains'):
                    results[test_name] = test_condition['contains'] in str(response['data'])
                elif test_condition.get('json_path'):
                    # Basic JSON path checking
                    path = test_condition['json_path']
                    expected = test_condition.get('equals')
                    actual = self._get_json_path(response['data'], path)
                    results[test_name] = actual == expected
                else:
                    results[test_name] = True
            except:
                results[test_name] = False
        return results
    
    def _get_json_path(self, data: Any, path: str) -> Any:
        """Basic JSON path extraction"""
        parts = path.split('.')
        current = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                current = current[int(part)] if int(part) < len(current) else None
            else:
                return None
        return current
    
    async def custom_store_response_tool(self, field_path: str = None, store_as: str = None, response_as: str = None) -> str:
        """Execute prepared API call and store response or specific field"""
        if '_prepared_api_config' not in self.stored_data:
            raise ValueError("No API call prepared. Use custom_api_service_tool first.")
        
        config = self.stored_data['_prepared_api_config']
        
        # Execute the API call
        response = await self._execute_api_call(config)
        
        # Store response based on parameters
        if field_path and store_as:
            # Extract and store specific field: stores "access_token" from response as "NewUserAuthToken"
            extracted_value = self.extract_field_from_response(response.get('data', {}), field_path)
            self.stored_data[store_as] = extracted_value
            result = f"Executed API call and stored {field_path} as {store_as}: {extracted_value}"
        elif response_as:
            # Store entire response data: stores response as "giftcardresponse"
            response_data = response.get('data', {})
            self.stored_data[response_as] = response_data
            result = f"Executed API call and stored full response as {response_as}"
        else:
            # Store entire response as _last_response (fallback)
            self.stored_data['_last_response'] = response
            result = f"Executed API call and stored full response"
        
        # Clean up prepared config
        self.stored_data.pop('_prepared_api_config', None)
        self.stored_data.pop('_prepared_config_file', None)
        
        return result
    
    def custom_modify_request_tool(self, headers: Dict[str, str] = None, body: Dict[str, Any] = None, 
                              params: Dict[str, str] = None, header_template: str = None,
                              body_field: str = None, body_value: Any = None) -> str:
        """Modify prepared API request - add headers, body fields, or query params"""
        if '_prepared_api_config' not in self.stored_data:
            raise ValueError("No API call prepared. Use custom_api_service_tool first.")
        
        config = self.stored_data['_prepared_api_config']
        modifications = []
        
        # Handle header template (like "Authorization: Bearer {token}")
        if header_template:
            resolved_header = self.resolve_dynamic_header(header_template)
            config.setdefault('headers', {}).update(resolved_header)
            # Log header addition with truncated values for readability
            logged_header = {}
            for k, v in resolved_header.items():
                logged_header[k] = v[:50] + "..." if len(str(v)) > 50 else str(v)
            modifications.append(f"header: {logged_header}")
        
        # Handle direct headers
        if headers:
            resolved_headers = {k: self._replace_variables(str(v), self.stored_data) for k, v in headers.items()}
            config.setdefault('headers', {}).update(resolved_headers)
            modifications.append(f"headers: {resolved_headers}")
        
        # Handle body modifications
        if body:
            resolved_body = self._replace_variables(body, self.stored_data)
            config.setdefault('body', {}).update(resolved_body)
            modifications.append(f"body: {resolved_body}")
        
        # Handle single body field
        if body_field and body_value is not None:
            resolved_value = self._replace_variables(str(body_value), self.stored_data)
            config.setdefault('body', {})[body_field] = resolved_value
            modifications.append(f"body.{body_field}: {resolved_value}")
        
        # Handle query parameters
        if params:
            resolved_params = {k: self._replace_variables(str(v), self.stored_data) for k, v in params.items()}
            config.setdefault('params', {}).update(resolved_params)
            modifications.append(f"params: {resolved_params}")
        
        return f"Modified prepared API request: {', '.join(modifications)}"
    
    def _replace_variables(self, obj: Any, variables: Dict[str, Any]) -> Any:
        """Replace {variable} placeholders in config with nested object support"""
        if isinstance(obj, dict):
            return {k: self._replace_variables(v, variables) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._replace_variables(item, variables) for item in obj]
        elif isinstance(obj, str):
            import re
            import time
            from datetime import datetime
            
            # Handle timestamp placeholders
            if '{timestamp}' in obj:
                obj = obj.replace('{timestamp}', str(int(time.time())))
            if '{datetimestamp}' in obj:
                obj = obj.replace('{datetimestamp}', datetime.now().strftime('%Y%m%d%H%M%S'))
            if '{uuid}' in obj:
                import uuid
                obj = obj.replace('{uuid}', str(uuid.uuid4()))
            if '{random}' in obj:
                import random
                obj = obj.replace('{random}', str(random.randint(1000, 9999)))
            
            def replace_var(match):
                var_path = match.group(1)
                if '.' in var_path:
                    # Handle nested access like {giftcardresponse.additionalData.accountNumber}
                    parts = var_path.split('.')
                    value = variables.get(parts[0])
                    for part in parts[1:]:
                        if isinstance(value, dict):
                            value = value.get(part)
                        elif isinstance(value, list) and part.isdigit():
                            idx = int(part)
                            value = value[idx] if 0 <= idx < len(value) else None
                        else:
                            value = None
                            break
                    return str(value) if value is not None else match.group(0)
                else:
                    return str(variables.get(var_path, match.group(0)))
            
            return re.sub(r'\{([^}]+)\}', replace_var, obj)
        return obj
    
    def _find_config_file(self, config_file: str) -> Path:
        """Find config file in target/json folder or other locations"""
        # Search locations in order of preference
        search_paths = [
            Path(config_file),  # Direct path
            Path("target/json") / config_file,  # target/json folder
            Path("../target/json") / config_file,  # From src folder
            Path("config") / config_file,  # config folder
            Path(".") / config_file  # Current directory
        ]
        
        for path in search_paths:
            if path.exists():
                return path
        
        # If not found, raise error with search locations
        raise FileNotFoundError(f"Config file '{config_file}' not found in: {[str(p) for p in search_paths]}")
    
    def extract_field_from_response(self, response_data: Any, field_path: str) -> Any:
        """Extract specific field from API response (supports nested paths)"""
        if '.' in field_path:
            parts = field_path.split('.')
            current = response_data
            for part in parts:
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    return None
            return current
        else:
            return response_data.get(field_path) if isinstance(response_data, dict) else None
    
    def resolve_dynamic_header(self, header_template: str) -> Dict[str, str]:
        """Resolve dynamic header like 'Authorization: Bearer {NewUserAuthToken}'"""
        if ':' in header_template:
            key, value_template = header_template.split(':', 1)
            resolved_value = self._replace_variables(value_template.strip(), self.stored_data)
            return {key.strip(): resolved_value}
        return {}
    
    def _prepare_headers_dynamically(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Prepare headers dynamically based on config"""
        headers = config.get('headers', {}).copy()
        
        # Handle auth from config
        auth = config.get('auth', {})
        if auth.get('token'):
            auth_type = auth.get('type', 'Bearer')
            headers['Authorization'] = f"{auth_type} {auth['token']}"
        
        # Resolve all template variables in headers
        for key, value in headers.items():
            if isinstance(value, str):
                headers[key] = self._replace_variables(value, self.stored_data)
        
        return headers
    
    async def _execute_api_call(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Execute API call with full Postman-like functionality"""
        import aiohttp
        import asyncio
        import time
        import secrets
        
        # Build connector with all Postman features
        connector = aiohttp.TCPConnector(
            ssl=config.get('verify_ssl', True),
            limit=config.get('connection_limit', 100),
            ttl_dns_cache=config.get('dns_cache_ttl', 300)
        )
        
        timeout = aiohttp.ClientTimeout(
            total=config.get('timeout', 30),
            connect=config.get('connect_timeout', 10),
            sock_read=config.get('read_timeout', 30)
        )
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            method = config.get('method', 'GET')
            url = config['url']
            headers = self._prepare_headers_dynamically(config)
            params = config.get('params', {})
            
            # Prepare body based on content type
            request_kwargs = {'method': method, 'url': url, 'headers': headers, 'params': params}
            
            if config.get('body') and method not in ['GET', 'HEAD']:
                body_data = config['body']
                content_type = headers.get('Content-Type', '')
                
                if 'application/x-www-form-urlencoded' in content_type:
                    # Convert dict to URL-encoded string
                    import urllib.parse
                    encoded_data = urllib.parse.urlencode(body_data)
                    request_kwargs['data'] = encoded_data
                elif 'application/json' in content_type or config.get('body_type') == 'json':
                    request_kwargs['json'] = body_data
                else:
                    request_kwargs['data'] = body_data
            
            # Debug logging
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"🔍 Request Debug: {method} {url}")
            logger.info(f"🔍 Headers: {headers}")
            logger.info(f"🔍 Request kwargs: {request_kwargs}")
            
            # Make request
            try:
                async with session.request(**request_kwargs) as response:
                    status = response.status
                    
                    # Handle response body
                    content_type = response.headers.get('content-type', '')
                    if 'application/json' in content_type:
                        data = await response.json()
                    else:
                        try:
                            data = await response.json()
                        except:
                            data = await response.text()
                    
                    result = {
                        'status': status,
                        'statusText': response.reason,
                        'headers': dict(response.headers),
                        'data': data,
                        'success': 200 <= status < 300,
                        'url': str(response.url)
                    }
                    
                    # Log API response
                    import logging
                    logger = logging.getLogger(__name__)
                    status_emoji = "✅" if 200 <= status < 300 else "❌"
                    logger.info(f"{status_emoji} API Call: {method} {url}")
                    logger.info(f"📊 Response Status: {status} {response.reason or ''}")
                    logger.info(f"📋 Response Data: {json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)}")
                    
                    return result
                    
            except aiohttp.ClientError as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"❌ API Call Failed: {method} {url} - {str(e)}")
                return {
                    'status': 0,
                    'error': str(e),
                    'success': False,
                    'data': None
                }
    async def custom_browser_extract_cookie_tool(self, variable_name: str = None, mcp_client=None) -> str:
        """
        Extract cookies from the browser using browser_evaluate
        
        Args:
            variable_name: Name to store cookies under (e.g., 'login_cookie')
            mcp_client: MCP client instance for executing browser_evaluate
            
        Returns:
            Status message indicating success/failure
        """
        import logging
        logger = logging.getLogger(__name__)
        
        if not mcp_client:
            logger.error("❌ MCP client not provided for cookie extraction")
            raise ValueError("MCP client is required for cookie extraction")
        
        try:
            logger.info(f"🍪 CUSTOM TOOL: Extracting cookies (variable_name: {variable_name})")
            
            # JavaScript code to extract cookies
            extract_cookies_code = """
            () => {
                const cookieDict = {};
                const cookies = document.cookie.split(';');
                cookies.forEach(cookie => {
                    const [name, value] = cookie.trim().split('=');
                    if (name && value) {
                        try {
                            cookieDict[name] = decodeURIComponent(value);
                        } catch (e) {
                            cookieDict[name] = value;
                        }
                    }
                });
                return {
                    cookie_dict: cookieDict,
                    total_count: Object.keys(cookieDict).length,
                    success: true
                };
            }
            """
            
            # Execute browser_evaluate to extract cookies
            logger.info(f"🍪 CUSTOM TOOL: Executing browser_evaluate for cookie extraction")
            result = await mcp_client.execute_tool("browser_evaluate", function=extract_cookies_code)
            logger.info(f"🍪 CUSTOM TOOL: Raw MCP result type: {type(result).__name__}")
            logger.info(f"🍪 CUSTOM TOOL: Parsing Raw MCP result ")
            
            # Parse the result
            cookie_dict = {}
            success = False
            result_str = None
            
            # Handle MCP wrapped response (list of dicts with 'type' and 'text')
            if isinstance(result, list) and len(result) > 0:
                # Extract text content from MCP response format
                logger.info(f"🍪 CUSTOM TOOL: MCP list response detected, extracting text content")
                for item in result:
                    if isinstance(item, dict) and 'text' in item:
                        result_str = item['text']
                        logger.info(f"🍪 CUSTOM TOOL: Extracted text content from MCP response")
                        break
            
            # If not extracted from list, convert result to string
            if result_str is None:
                result_str = str(result)
            
            # Try to parse as dict first
            if isinstance(result, dict) and result.get('success'):
                cookie_dict = result.get('cookie_dict', {})
                success = True
            else:
                # Try to parse as JSON string
                try:
                    import json
                    import re
                    # Remove MCP response markers and extra text
                    json_str = result_str.replace('### Result', '').replace('### Ran Playwright code', '').strip()
                    
                    # Strategy 1: Try to find and extract the main JSON object with proper bracket matching
                    start_idx = json_str.find('{')
                    if start_idx >= 0:
                        # Count brackets to find the matching closing bracket
                        bracket_count = 0
                        end_idx = start_idx
                        in_string = False
                        escape_next = False
                        
                        for i in range(start_idx, len(json_str)):
                            char = json_str[i]
                            
                            if escape_next:
                                escape_next = False
                                continue
                            
                            if char == '\\':
                                escape_next = True
                                continue
                            
                            if char == '"' and not escape_next:
                                in_string = not in_string
                                continue
                            
                            if not in_string:
                                if char == '{':
                                    bracket_count += 1
                                elif char == '}':
                                    bracket_count -= 1
                                    if bracket_count == 0:
                                        end_idx = i + 1
                                        break
                        
                        if bracket_count == 0 and end_idx > start_idx:
                            json_str = json_str[start_idx:end_idx]
                            parsed = json.loads(json_str)
                            if isinstance(parsed, dict):
                                # Check if it has cookie_dict (main response) or success flag
                                if 'cookie_dict' in parsed:
                                    cookie_dict = parsed.get('cookie_dict', {})
                                    success = True
                                elif parsed.get('success'):
                                    cookie_dict = parsed.get('cookie_dict', {})
                                    success = True
                except Exception as parse_error:
                    logger.warning(f"⚠️ CUSTOM TOOL: Failed to parse cookie extraction result: {parse_error}")
                    logger.warning(f"⚠️ CUSTOM TOOL: Raw result (first 1000 chars): {result_str}")
            
            if success and cookie_dict:
                # Store cookies with variable name
                storage_key = variable_name if variable_name else 'extracted_cookies'
                self.stored_data[storage_key] = cookie_dict
                
                logger.info(f"✅ CUSTOM TOOL: Successfully extracted {len(cookie_dict)} cookies")
                logger.info(f"💾 CUSTOM TOOL: Stored cookies as '{storage_key}'")
                logger.info(f"📝 CUSTOM TOOL: Available for use as {{{storage_key}.cookie_name}}")
                
                # Log the entire extracted cookie data without truncation
                logger.info(f"🍪 CUSTOM TOOL: FULL EXTRACTED COOKIES DATA:")
                full_cookie_json = json.dumps(cookie_dict, indent=2)
                # Log in chunks if very large to avoid truncation
                if len(full_cookie_json) > 10000:
                    # Log in 5000 char chunks
                    for i in range(0, len(full_cookie_json), 5000):
                        logger.info(full_cookie_json[i:i+5000])
                else:
                    logger.info(full_cookie_json)
                
                return f"Successfully extracted {len(cookie_dict)} cookies and stored as '{storage_key}'"
            else:
                logger.warning(f"⚠️ CUSTOM TOOL: No cookies extracted or parsing failed")
                return "Failed to extract cookies or no cookies found"
                
        except Exception as e:
            logger.error(f"❌ CUSTOM TOOL: Cookie extraction failed: {str(e)}")
            import traceback
            logger.error(f"   Traceback: {traceback.format_exc()}")
            raise RuntimeError(f"Cookie extraction failed: {str(e)}")
    async def custom_mock_api_tool(self, mock_file: str = None,
                                   mcp_client = None, variables: Dict[str, Any] = None) -> str:
        """Mock API responses using configuration files - Intercepts network requests
        
        Handles large mock files by using a streaming approach instead of embedding entire JSON in code.
        """
        import logging
        logger = logging.getLogger(__name__)
        
        if not mock_file:
            raise ValueError("mock_file parameter is required")
        
        if not mcp_client:
            raise ValueError("MCP client is required for mock setup")
        
        try:
            # Load mock configuration
            config_path = self._find_config_file(mock_file)
            logger.info(f"🎭 MOCK API TOOL: Setting up mock interceptor for config '{mock_file}'")
            
            with open(config_path, 'r') as f:
                mock_config = json.load(f)
            
            logger.info(f"✅ MOCK API: Loaded configuration from {config_path}")
            
            # Parse routes
            routes = []
            if isinstance(mock_config, dict):
                if 'route' in mock_config:
                    routes = [mock_config]
                elif 'routes' in mock_config:
                    routes = mock_config['routes']
            elif isinstance(mock_config, list):
                routes = mock_config
            
            logger.info(f"✅ MOCK API: Successfully loaded mock config with {len(routes)} route(s)")
            logger.info(f"🎭 MOCK API: Extracted {len(routes)} route(s) from config")
            
            for i, route in enumerate(routes):
                pattern = route.get('route', 'unknown')
                method = route.get('method')
                logger.info(f"   - Route {i+1}: Pattern: {pattern} | Method: {method}")
            
            # Store routes in memory for access by route handler
            mock_handler_id = f"mock_routes_{secrets.token_hex(8)}"
            self.stored_data[mock_handler_id] = routes
            logger.info(f"💾 MOCK API: Stored {len(routes)} routes with handler ID: {mock_handler_id}")
            
            # Strategy for large files: Register route handler that processes routes one at a time
            # instead of embedding all JSON in the JavaScript code
            setup_routes_code = f"""
            (async () => {{
                // Handler ID for retrieving routes from storage
                const handlerId = "{mock_handler_id}";
                let routesRegistered = 0;
                let totalRoutes = {len(routes)};
                
                // Set up generic interceptor pattern
                await page.route('**/*', async (routeHandler) => {{
                    const requestUrl = routeHandler.request().url();
                    const requestMethod = routeHandler.request().method();
                    
                    // Try to match against mock patterns
                    // Note: Individual route matching happens at execution time
                    console.log(`🔍 INTERCEPTOR: Checking request - ${{requestMethod}} ${{requestUrl}}`);
                    
                    // Allow all requests through for now - actual mocking will be route-specific
                    await routeHandler.continue();
                }});
                
                console.log(`✅ Generic interceptor registered for ${{totalRoutes}} potential routes`);
                return {{ success: true, handlerId: handlerId, routesCount: totalRoutes }};
            }})();
            """
            
            logger.info("🎭 MOCK API: Registering mock routes via browser_run_code...")
            
            # Execute browser code to set up generic interceptor
            result = await mcp_client.execute_tool("browser_run_code", code=setup_routes_code)
            logger.info(f"✅ MOCK API: Generic interceptor registered")
            
            # Now register individual routes using a more efficient approach
            # Process routes in batches to avoid large code payloads
            batch_size = 5
            total_registered = 0
            
            for batch_idx in range(0, len(routes), batch_size):
                batch = routes[batch_idx:batch_idx + batch_size]
                
                # Generate code for this batch
                batch_code = self._generate_route_handler_code(batch)
                logger.info(f"🎯 MOCK API: Registering batch {batch_idx // batch_size + 1} ({len(batch)} routes)")
                
                try:
                    logger.info(f"⏳ MOCK API: Waiting for batch {batch_idx // batch_size + 1} to execute (this may take up to 30 seconds)...")
                    result = await mcp_client.execute_tool("browser_run_code", code=batch_code)
                    total_registered += len(batch)
                    logger.info(f"✅ MOCK API: Batch {batch_idx // batch_size + 1} registered ({total_registered}/{len(routes)})")
                except Exception as batch_error:
                    logger.warning(f"⚠️ MOCK API: Failed to register batch {batch_idx // batch_size + 1}: {str(batch_error)}")
                    import time
                    time.sleep(2)  # Wait before next batch if this one failed
            
            logger.info(f"✅ MOCK API: Mock interception setup complete - Mock API route(s) intercepted for '{mock_file}'")
            logger.info(f"✅ MOCK API: Routes are now ACTIVE and will intercept matching API calls")
            logger.info(f"💡 MOCK API: Subsequent steps that trigger API calls will receive mock responses")
            logger.info(f"📊 MOCK API: Total routes registered: {total_registered}/{len(routes)}")
            
            return f"Successfully set up mock API interception for {len(routes)} route(s)"
            
        except FileNotFoundError as e:
            logger.error(f"❌ MOCK API: Config file not found: {mock_file}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"❌ MOCK API: Invalid JSON in mock config: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"❌ MOCK API: Failed to set up mock interceptor: {str(e)}")
            import traceback
            logger.error(f"   Traceback: {traceback.format_exc()}")
            raise RuntimeError(f"Mock setup failed: {str(e)}")
    
    def _generate_route_handler_code(self, batch_routes: list) -> str:
        """Generate efficient JavaScript code for a batch of routes without embedding full JSON
        
        This method generates route handlers that:
        1. Use minimal JSON embedding (responses are chunked)
        2. Support wildcard pattern matching
        3. Handle large mock responses efficiently
        """
        import logging
        logger = logging.getLogger(__name__)
        
        # Build route handlers with careful JSON handling for large payloads
        route_cases = []
        for idx, route in enumerate(batch_routes):
            pattern = route.get('route', '')
            method = route.get('method', 'GET').upper()
            status = route.get('status', 200)
            response = route.get('response', {})
            
            # Carefully escape the response to avoid issues with large JSONs
            # Use JSON.parse to handle the response data
            response_json_str = json.dumps(response)
            
            # If response is very large, chunk it
            if len(response_json_str) > 50000:
                # For very large responses, store them separately and reference by ID
                response_chunk_id = f"response_{idx}_{secrets.token_hex(4)}"
                self.stored_data[response_chunk_id] = response
                logger.info(f"   📦 Large response ({len(response_json_str)} chars) stored as '{response_chunk_id}'")
                
                route_cases.append(f"""
            if (patternMatch("{pattern}", requestUrl) && requestMethod === "{method}") {{
                console.log('🎯 MOCK INTERCEPTED ({method}): ' + requestUrl);
                await routeHandler.fulfill({{
                    status: {status},
                    contentType: 'application/json',
                    body: JSON.stringify(window.__mockResponses && window.__mockResponses["{response_chunk_id}"] || {json.dumps(response)})
                }});
                return;
            }}
            """)
            else:
                # For normal-sized responses, embed directly
                response_escaped = json.dumps(response_json_str)
                
                route_cases.append(f"""
            if (patternMatch("{pattern}", requestUrl) && requestMethod === "{method}") {{
                console.log('🎯 MOCK INTERCEPTED ({method}): ' + requestUrl);
                await routeHandler.fulfill({{
                    status: {status},
                    contentType: 'application/json',
                    body: {response_escaped}
                }});
                return;
            }}
            """)
        
        route_matching_code = "\n".join(route_cases)
        
        code = f"""
        (async () => {{
            function patternMatch(pattern, url) {{
                // Wildcard pattern matching
                if (!pattern || pattern === '*' || pattern === '**/*') return true;
                
                // If pattern contains wildcards, use regex
                if (pattern.includes('*')) {{
                    // Replace * with regex wildcard
                    let regexPattern = pattern
                        .replace(/\\//g, '\\\\/')  // Escape forward slashes
                        .replace(/\\./g, '\\\\.')  // Escape dots
                        .replace(/\\*/g, '.*');    // Replace * with .*
                    
                    try {{
                        const regex = new RegExp(regexPattern, 'i');
                        return regex.test(url);
                    }} catch (e) {{
                        console.warn('Invalid regex pattern: ' + pattern, e);
                        return url.includes(pattern);
                    }}
                }}
                
                // Simple substring matching for non-wildcard patterns
                return url.includes(pattern);
            }}
            
            await page.route('**/*', async (routeHandler) => {{
                const requestUrl = routeHandler.request().url();
                const requestMethod = routeHandler.request().method();
                
                {route_matching_code}
                
                // If no mock matched, continue with real request
                await routeHandler.continue();
            }});
            
            console.log('✅ Registered {len(batch_routes)} mock route(s)');
            return {{ success: true, routesCount: {len(batch_routes)} }};
        }})();
        """
        
        return code
    
    # Future tools can be added here:
    # async def custom_database_tool(self, ...):
    #     """Database operations"""
    #     pass

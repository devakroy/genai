# import requests
# import json
# import os
# from typing import Dict, Optional, Any
# from fastapi import APIRouter, HTTPException
# from pydantic import BaseModel
#
# class GetRequest(BaseModel):
#     url: str
#     headers: Optional[Dict[str, str]] = {}
#     params: Optional[Dict[str, str]] = {}
#     auth_token: Optional[str] = None
#     auth_type: Optional[str] = "bearer"
#     timeout: Optional[int] = 30
#
# class GetResponse(BaseModel):
#     status_code: int
#     response_data: Dict[str, Any]
#     headers: Dict[str, str]
#     success: bool
#     message: str
#
# class GetAPIClient:
#     def __init__(self):
#         self.router = APIRouter()
#         self.setup_routes()
#         self.stored_responses = {}
#         self.stored_tokens = {}
#
#     def setup_routes(self):
#         self.router.get("/universal-get")(self.universal_get)
#         self.router.get("/stored-responses")(self.get_stored_responses)
#         self.router.delete("/stored-responses/{response_name}")(self.delete_stored_response)
#
#     def store_response(self, response_name: str, response_data: Dict[str, Any]):
#         """Store API response for later use"""
#         self.stored_responses[response_name] = response_data
#         print(f"✅ Stored response '{response_name}': {json.dumps(response_data, indent=2)[:200]}...")
#
#     def get_stored_response(self, response_name: str) -> Optional[Dict[str, Any]]:
#         """Retrieve stored API response"""
#         return self.stored_responses.get(response_name)
#
#     async def universal_get(self, request: GetRequest, store_as: Optional[str] = None) -> GetResponse:
#         """Universal GET API call with response storage"""
#         try:
#             result = get_api_call(
#                 request.url,
#                 request.auth_token,
#                 request.headers,
#                 request.params,
#                 request.auth_type
#             )
#
#             # Store response if requested
#             if store_as and result['success']:
#                 self.store_response(store_as, result['data'])
#
#             return GetResponse(
#                 status_code=result['status_code'],
#                 response_data=result['data'],
#                 headers=result.get('response_headers', {}),
#                 success=result['success'],
#                 message=result['message']
#             )
#
#         except Exception as e:
#             raise HTTPException(status_code=500, detail=f"GET request failed: {str(e)}")
#
#     async def get_stored_responses(self) -> Dict[str, Any]:
#         """Get all stored responses"""
#         return {"stored_responses": list(self.stored_responses.keys())}
#
#     async def delete_stored_response(self, response_name: str) -> Dict[str, str]:
#         """Delete a stored response"""
#         if response_name in self.stored_responses:
#             del self.stored_responses[response_name]
#             return {"message": f"Response '{response_name}' deleted successfully"}
#         else:
#             raise HTTPException(status_code=404, detail=f"Response '{response_name}' not found")
#
# def get_api_call(url: str, api_key: Optional[str] = None, headers: Optional[Dict] = None,
#                  params: Optional[Dict] = None, auth_type: str = "bearer") -> Dict[str, Any]:
#     """
#     Universal GET API client with custom headers and query parameters support
#
#     Args:
#         url: API endpoint URL
#         api_key: Authentication token/key
#         headers: Custom headers dictionary
#         params: Query parameters dictionary
#         auth_type: Authentication type - "bearer", "api_key", or "basic"
#
#     Returns:
#         Dict with status, data, headers, and response info
#     """
#
#     # Setup default headers
#     request_headers = {"Content-Type": "application/json"}
#
#     # Add auth key if provided
#     if api_key:
#         if auth_type.lower() == "bearer":
#             request_headers["Authorization"] = f"Bearer {api_key}"
#         elif auth_type.lower() == "api_key":
#             request_headers["X-API-Key"] = api_key
#         elif auth_type.lower() == "basic":
#             request_headers["Authorization"] = f"Basic {api_key}"
#
#     # Add custom headers if provided
#     if headers:
#         request_headers.update(headers)
#
#     try:
#         print(f"Making GET request to: {url}")
#         print(f"Headers: {json.dumps(request_headers, indent=2)}")
#         print(f"Parameters: {params}")
#
#         # Make the GET request
#         response = requests.get(url, headers=request_headers, params=params, timeout=30)
#
#         print(f"Response Status Code: {response.status_code}")
#         print(f"Response Headers: {dict(response.headers)}")
#
#         # Check if request was successful (200 series)
#         if 200 <= response.status_code < 300:
#             print("✅ SUCCESS: Request completed successfully")
#             try:
#                 data = response.json()
#                 return {
#                     "success": True,
#                     "status_code": response.status_code,
#                     "data": data,
#                     "response_headers": dict(response.headers),
#                     "request_headers": request_headers,
#                     "params": params,
#                     "url": url,
#                     "message": "Request successful"
#                 }
#             except json.JSONDecodeError:
#                 return {
#                     "success": True,
#                     "status_code": response.status_code,
#                     "data": response.text,
#                     "response_headers": dict(response.headers),
#                     "request_headers": request_headers,
#                     "params": params,
#                     "url": url,
#                     "message": "Request successful (non-JSON response)"
#                 }
#         else:
#             print(f"❌ ERROR: Request failed with status {response.status_code}")
#             return {
#                 "success": False,
#                 "status_code": response.status_code,
#                 "data": None,
#                 "response_headers": dict(response.headers),
#                 "request_headers": request_headers,
#                 "params": params,
#                 "url": url,
#                 "message": f"Request failed with status {response.status_code}",
#                 "error": response.text
#             }
#
#     except requests.exceptions.Timeout:
#         print("❌ ERROR: Request timed out")
#         return {
#             "success": False,
#             "status_code": None,
#             "data": None,
#             "url": url,
#             "message": "Request timed out",
#             "error": "Timeout"
#         }
#     except requests.exceptions.ConnectionError:
#         print("❌ ERROR: Connection failed")
#         return {
#             "success": False,
#             "status_code": None,
#             "data": None,
#             "url": url,
#             "message": "Connection failed",
#             "error": "Connection Error"
#         }
#     except Exception as e:
#         print(f"❌ ERROR: Unexpected error - {str(e)}")
#         return {
#             "success": False,
#             "status_code": None,
#             "data": None,
#             "url": url,
#             "message": f"Unexpected error: {str(e)}",
#             "error": str(e)
#         }
#
# def generate_typescript_code(url: str, api_key: Optional[str] = None, headers: Optional[Dict] = None,
#                            params: Optional[Dict] = None, auth_type: str = "bearer") -> str:
#     """Generate TypeScript code for the API call"""
#
#     # Build headers object for TypeScript
#     ts_headers = []
#     ts_headers.append("'Content-Type': 'application/json'")
#
#     if api_key:
#         if auth_type.lower() == "bearer":
#             ts_headers.append(f"'Authorization': 'Bearer {api_key}'")
#         elif auth_type.lower() == "api_key":
#             ts_headers.append(f"'X-API-Key': '{api_key}'")
#         elif auth_type.lower() == "basic":
#             ts_headers.append(f"'Authorization': 'Basic {api_key}'")
#
#     if headers:
#         for key, value in headers.items():
#             ts_headers.append(f"'{key}': '{value}'")
#
#     headers_str = ",\n    ".join(ts_headers)
#
#     # Build params object for TypeScript
#     params_str = ""
#     if params:
#         ts_params = []
#         for key, value in params.items():
#             ts_params.append(f"'{key}': '{value}'")
#         params_str = f",\n    params: {{\n      {','.join(ts_params)}\n    }}"
#
#     typescript_code = f'''import {{ test, expect }} from '@playwright/test';
# test('API GET request test', async ({{ request }}) => {{
#   const response = await request.get('{url}', {{
#     headers: {{
#       {headers_str}
#     }}{params_str}
#   }});
#   // Validate response status
#   expect(response.status()).toBe(200);
#
#   // Get response data
#   const responseData = await response.json();
#   console.log('Response:', responseData);
#
#   // Add your assertions here
#   expect(responseData).toBeDefined();
# }});'''
#
#     return typescript_code
#
# def save_typescript_file(typescript_code: str, jira_id: str = "API", file_version: str = "v1") -> str:
#     """Save TypeScript code to file using the same naming convention as other generated TS files"""
#
#     # Import here to avoid circular imports
#
#
#     # Use the same directory structure as other TypeScript files
#     output_dir = os.path.join(REVIEW_PLAYWRIGHT_SCRIPTS_DIR, jira_id, "Scenario_1")
#     os.makedirs(output_dir, exist_ok=True)
#
#     # Follow the same naming convention: {jira_id}_{file_version}.ts
#     filename = f"{jira_id}_{file_version}.ts"
#     file_path = os.path.join(output_dir, filename)
#
#     with open(file_path, 'w') as f:
#         f.write(typescript_code)
#
#     print(f"✅ TypeScript file saved: {file_path}")
#     return file_path
#
# def process_api_request(url: str, api_key: Optional[str] = None, headers: Optional[Dict] = None,
#                        params: Optional[Dict] = None, auth_type: str = "bearer", generate_ts: bool = True,
#                        jira_id: str = "API", file_version: str = "v1", store_as: Optional[str] = None) -> Dict[str, Any]:
#     """
#     Complete workflow: API call -> Validation -> TypeScript generation -> Response Storage
#
#     Steps:
#     1. Make API call with headers
#     2. Validate 200 series response
#     3. Store response if requested
#     4. Generate TypeScript code if successful
#     5. Save .ts file
#     """
#
#     print("STARTING API REQUEST WORKFLOW")
#     print("=" * 50)
#
#     # Step 1: Make API call
#     print("\nStep 1: Making API call...")
#     result = get_api_call(url, api_key, headers, params, auth_type)
#
#     # Step 2: Validate response
#     print("\nStep 2: Validating response...")
#     if result['success'] and 200 <= result['status_code'] < 300:
#         print(f"✅ Response validation passed: {result['status_code']}")
#
#         # Step 3: Store response if requested
#         if store_as:
#             print(f"\nStep 3: Storing response as '{store_as}'...")
#             get_api_client.store_response(store_as, result['data'])
#
#         if generate_ts:
#             # Step 4: Generate TypeScript code
#             print("\nStep 4: Generating TypeScript code...")
#             ts_code = generate_typescript_code(url, api_key, headers, params, auth_type)
#
#             # Step 5: Save TypeScript file
#             print("\nStep 5: Saving TypeScript file...")
#             ts_file_path = save_typescript_file(ts_code, jira_id, file_version)
#
#             result['typescript_code'] = ts_code
#             result['typescript_file'] = ts_file_path
#
#         return result
#     else:
#         print(f"❌ Response validation failed: {result.get('status_code', 'Unknown')}")
#         return result
#
# # Create global instance for response storage
# get_api_client = GetAPIClient()
# router = APIRouter(prefix="/get-api")
# router.include_router(get_api_client.router)
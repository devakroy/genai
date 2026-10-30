"""
Universal POST API Client
"""

import os
import json
import requests
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

class PostRequest(BaseModel):
    url: str
    headers: Optional[Dict[str, str]] = {}
    body: Optional[Dict[str, Any]] = {}
    body_from_file: Optional[str] = None
    body_key: Optional[str] = None
    content_type: Optional[str] = "application/json"
    auth_token: Optional[str] = None
    auth_type: Optional[str] = "Bearer"
    timeout: Optional[int] = 30
    method: Optional[str] = None
    params: Optional[Dict[str, str]] = {}
    response_type: Optional[str] = "json"
    with_credentials: Optional[bool] = False
    max_redirects: Optional[int] = 30
    proxy: Optional[Dict[str, str]] = None
    validate_status: Optional[bool] = True
    base_url: Optional[str] = None

class PostResponse(BaseModel):
    status_code: int
    response_data: Dict[str, Any]
    headers: Dict[str, str]
    success: bool
    message: str

class PostAPIClient:
    def __init__(self):
        self.router = APIRouter()
        self.setup_routes()
        self.stored_tokens = {}
        
    def setup_routes(self):
        self.router.post("/universal-post")(self.universal_post)
        self.router.post("/auth-and-post")(self.auth_and_post)
        self.router.get("/stored-tokens")(self.get_stored_tokens)
        self.router.delete("/stored-tokens/{token_name}")(self.delete_stored_token)

    def load_request_body_from_file(self, file_path: str, key: Optional[str] = None) -> Dict[str, Any]:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Request body file not found: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if key:
            if key not in data:
                raise KeyError(f"Key '{key}' not found in request body file")
            return data[key]
        
        return data

    def prepare_headers(self, headers: Dict[str, str], content_type: str, auth_token: Optional[str] = None, auth_type: str = "Bearer") -> Dict[str, str]:
        prepared_headers = headers.copy()
        
        if content_type:
            prepared_headers["Content-Type"] = content_type
        
        if auth_token:
            if auth_type.lower() == "bearer":
                prepared_headers["Authorization"] = f"Bearer {auth_token}"
            elif auth_type.lower() == "basic":
                prepared_headers["Authorization"] = f"Basic {auth_token}"
            else:
                prepared_headers["Authorization"] = f"{auth_type} {auth_token}"
        
        for key, value in prepared_headers.items():
            if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
                token_name = value[1:-1]
                if token_name in self.stored_tokens:
                    prepared_headers[key] = f"Bearer {self.stored_tokens[token_name]}"
        
        return prepared_headers

    def prepare_body(self, body: Optional[Dict[str, Any]], body_from_file: Optional[str], body_key: Optional[str], content_type: str) -> Any:
        if body_from_file:
            body = self.load_request_body_from_file(body_from_file, body_key)
        
        if not body:
            return None
        
        return body
    
    def _determine_method_from_url(self, url: str) -> str:
        """Dynamically determine HTTP method based on RESTful URL patterns"""
        import re
        
        # Extract path from URL
        path = url.split('?')[0]  # Remove query parameters
        path_parts = [part for part in path.split('/') if part]  # Remove empty parts
        
        if not path_parts:
            return 'POST'
        
        # Check for ID patterns (numbers, UUIDs, etc.)
        id_patterns = [
            r'^\d+$',  # Numeric ID
            r'^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$',  # UUID
            r'^[a-fA-F0-9]{24}$',  # MongoDB ObjectId
            r'^[a-zA-Z0-9_-]+$'  # Alphanumeric ID
        ]
        
        last_part = path_parts[-1]
        
        # If last part matches ID pattern, it's likely an update (PUT)
        for pattern in id_patterns:
            if re.match(pattern, last_part) and len(last_part) > 2:  # Avoid short strings
                return 'PUT'
        
        # Check for common update endpoints
        update_keywords = ['edit', 'update', 'modify', 'patch']
        if any(keyword in path.lower() for keyword in update_keywords):
            return 'PUT'
        
        # Default to POST for creation
        return 'POST'

    async def universal_post(self, request: PostRequest) -> PostResponse:
        try:
            headers = self.prepare_headers(
                request.headers, 
                request.content_type, 
                request.auth_token, 
                request.auth_type
            )
            
            body = self.prepare_body(
                request.body, 
                request.body_from_file, 
                request.body_key, 
                request.content_type
            )
            
            # Use method from config or determine from URL
            method = getattr(request, 'method', None)
            if not method:
                # Check if method is in the body (from config)
                if isinstance(body, dict) and 'method' in body:
                    method = body.pop('method')  # Remove method from body
                else:
                    # Determine method from URL, but only allow POST for post_api_client
                    determined_method = self._determine_method_from_url(request.url)
                    if determined_method != 'POST':
                        raise HTTPException(
                            status_code=400, 
                            detail=f"URL pattern suggests {determined_method} method, but no method specified in config. Please add 'method': '{determined_method}' to your JSON config file."
                        )
                    method = 'POST'
            
            print(f"🔍 DEBUG: Using HTTP method: {method}")
            print(f"🔍 DEBUG: Request URL: {request.url}")
            print(f"🔍 DEBUG: Request body: {body}")
            print(f"🔍 DEBUG: Request headers: {headers}")
            
            # Build final URL with base_url and params
            final_url = request.url
            if request.base_url:
                final_url = request.base_url.rstrip('/') + '/' + request.url.lstrip('/')
            
            # Prepare request kwargs
            req_kwargs = {
                'headers': headers,
                'timeout': request.timeout,
                'params': request.params,
                'allow_redirects': request.max_redirects > 0,
                'proxies': request.proxy
            }
            
            if request.with_credentials:
                req_kwargs['cookies'] = getattr(requests.Session(), 'cookies', None)
            
            if request.content_type == "application/x-www-form-urlencoded":
                req_kwargs['data'] = body
            else:
                req_kwargs['json'] = body
            
            response = requests.request(method, final_url, **req_kwargs)
            
            # Handle different response types
            if request.response_type == "json":
                try:
                    response_data = response.json()
                except:
                    response_data = {"text": response.text}
            elif request.response_type == "text":
                response_data = {"text": response.text}
            elif request.response_type == "blob":
                response_data = {"content": response.content.hex()}
            else:
                try:
                    response_data = response.json()
                except:
                    response_data = {"text": response.text}
            
            if "access_token" in response_data:
                self.stored_tokens["authToken"] = response_data["access_token"]
            
            # Custom status validation
            if request.validate_status:
                success = 200 <= response.status_code < 300
            else:
                success = True  # Accept any status when validation disabled
            
            return PostResponse(
                status_code=response.status_code,
                response_data=response_data,
                headers=dict(response.headers),
                success=success,
                message="Request completed successfully" if success else f"Request failed with status {response.status_code}"
            )
            
        except requests.exceptions.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Request failed: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

    async def auth_and_post(self, auth_request: PostRequest, main_request: PostRequest) -> Dict[str, Any]:
        try:
            auth_response = await self.universal_post(auth_request)
            
            if not auth_response.success:
                raise HTTPException(status_code=401, detail="Authentication failed")
            
            token = None
            if "access_token" in auth_response.response_data:
                token = auth_response.response_data["access_token"]
            elif "token" in auth_response.response_data:
                token = auth_response.response_data["token"]
            
            if not token:
                raise HTTPException(status_code=401, detail="No token found in auth response")
            
            main_request.auth_token = token
            main_response = await self.universal_post(main_request)
            
            return {
                "auth_response": auth_response.dict(),
                "main_response": main_response.dict(),
                "token_used": token
            }
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error in auth and post: {str(e)}")

    async def get_stored_tokens(self) -> Dict[str, str]:
        return {"stored_tokens": list(self.stored_tokens.keys())}

    async def delete_stored_token(self, token_name: str) -> Dict[str, str]:
        if token_name in self.stored_tokens:
            del self.stored_tokens[token_name]
            return {"message": f"Token '{token_name}' deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail=f"Token '{token_name}' not found")

post_api_client = PostAPIClient()
router = APIRouter(prefix="/universal")
router.include_router(post_api_client.router)
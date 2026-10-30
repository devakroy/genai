"""
Mock API Interceptor for handling HTTP request mocking in Playwright tests.
"""

import json
import os
from typing import Dict, Any, Optional


class MockAPIInterceptor:
    """
    Handles loading and managing mock API configurations for test automation.
    """

    def __init__(self):
        self.mock_configs = {}
        self.route_handlers = {}  # Store handlers by route+method

    def load_mock_config(self, mock_file: str, request_bodies: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """
        Load mock configuration from uploaded request bodies or file system.
        
        Args:
            mock_file: Name of the mock configuration file
            request_bodies: Dictionary of uploaded request bodies
            
        Returns:
            Mock configuration dictionary or None if not found
        """
        mock_key = mock_file.replace('.json', '')

        # Try uploaded request bodies first
        if request_bodies and mock_key in request_bodies:
            return request_bodies[mock_key]

        # Fallback to file system
        possible_paths = [
            f"../data/mock_responses/{mock_file}",
            f"data/mock_responses/{mock_file}",
            f"../../data/mock_responses/{mock_file}",
            f"/Users/akumarroy/Documents/chewy-genai-automation/data/mock_responses/{mock_file}"
        ]

        for file_path in possible_paths:
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r') as f:
                        return json.load(f)
                except Exception as e:
                    print(f"Error loading mock file {file_path}: {e}")
                    continue

        return None

    def get_mock_response(self, mock_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate mock response from configuration.
        
        Args:
            mock_config: Mock configuration dictionary
            
        Returns:
            Mock response dictionary
        """
        return {
            'status': mock_config.get('status', 200),
            'content_type': mock_config.get('content_type', 'application/json'),
            'body': json.dumps(mock_config.get('response', {}))
        }

    def get_route_configs(self, mock_config: Dict[str, Any]):
        """
        Extract route configurations from mock config, supporting multiple routes.
        
        Returns:
            List of (route_pattern, method_configs) tuples
        """
        route_configs = []

        if isinstance(mock_config, list):
            # Array format: group by route pattern
            routes_map = {}
            for config in mock_config:
                route = config.get('route', '**/*')
                if route not in routes_map:
                    routes_map[route] = {}
                method = config.get('method', 'GET').upper()
                routes_map[route][method] = config

            for route, methods in routes_map.items():
                route_configs.append((route, methods))

        elif 'methods' in mock_config:
            # Multi-method format with single route
            route = mock_config.get('route', '**/*')
            method_configs = {config.get('method', 'GET').upper(): config for config in mock_config['methods']}
            route_configs.append((route, method_configs))

        else:
            # Single method format
            route = mock_config.get('route', '**/*')
            method = mock_config.get('method', 'GET').upper()
            route_configs.append((route, {method: mock_config}))

        return route_configs

    def create_route_handler(self, method_configs: Dict[str, Dict]):
        """
        Create a method-aware route handler for specific route pattern.
        
        Args:
            method_configs: Dictionary mapping methods to their configs
            
        Returns:
            Async route handler function
        """
        async def handle_route(route):
            request_method = route.request.method.upper()

            # Check if we have a config for this method
            if request_method in method_configs:
                config = method_configs[request_method]
                print(f"🎯 MOCK ROUTE INTERCEPTED: {route.request.url} [{request_method}]")
                mock_response = config.get('response', {})
                print(f"📊 MOCK RESPONSE: {json.dumps(mock_response, indent=2)}")

                await route.fulfill(
                    status=config.get('status', 200),
                    content_type=config.get('content_type', 'application/json'),
                    body=json.dumps(mock_response)
                )
            else:
                # Let other handlers or real network handle this
                await route.continue_()

        return handle_route

    def get_route_key(self, route_pattern: str, method: str) -> str:
        """
        Generate unique key for route+method combination.
        """
        return f"{route_pattern}::{method.upper()}"
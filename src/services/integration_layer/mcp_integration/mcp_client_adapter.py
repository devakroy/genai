"""
Universal MCP Client Adapter
Imports from mcp_client.py for clean naming
"""
from .mcp_client import UnifiedMCPClient

# Export for backward compatibility
UniversalMCPClient = UnifiedMCPClient

# Factory function
async def create_mcp_client():
    """Create and connect MCP client"""
    client = UniversalMCPClient()
    await client.connect()
    return client
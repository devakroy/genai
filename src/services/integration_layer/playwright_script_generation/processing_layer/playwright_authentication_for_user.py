from playwright.async_api import Page, BrowserContext
from typing import Dict, Any, TypedDict


class AccountTokens(TypedDict):
    ticket: str  # maps to idToken
    asid: str  # maps to sessionId
    at: str  # maps to authToken


class PlaywrightAuthService:
    """Service for adding authentication cookies to Playwright."""

    async def add_authentication_cookies(
            self,
            context: BrowserContext,
            auth_cookies: Dict[str, str],
            existing_page: Page = None
    ) -> None:
        """Add authentication cookies to BrowserContext."""
        print("🍪 Adding authentication cookies...")

        # Use existing page if provided, otherwise create temporary one
        if existing_page:
            page = existing_page
            should_close_page = False
        else:
            page = await context.new_page()
            should_close_page = True

        try:
            # Navigate to domain to set cookies
            domain = auth_cookies['cookieDomain']
            await page.goto(f"https://{domain}")

            # FIXED: Use domain and path (not url) - Playwright prefers this format
            cookies = [
                {
                    'name': 'at',
                    'value': auth_cookies['at'],
                    'domain': f".{auth_cookies['cookieDomain']}",  # Note the leading dot
                    'path': '/'
                },
                {
                    'name': 'ticket',
                    'value': auth_cookies['ticket'],
                    'domain': f".{auth_cookies['cookieDomain']}",  # Note the leading dot
                    'path': '/'
                },
                {
                    'name': 'asid',
                    'value': auth_cookies['asid'],
                    'domain': f".{auth_cookies['cookieDomain']}",  # Note the leading dot
                    'path': '/'
                },
                {
                    'name': 'lbGroup',
                    'value': 'use2',
                    'domain': '.www-stg.chewy.net',  # Note the leading dot
                    'path': '/'
                }
            ]

            await context.add_cookies(cookies)
            print("✅ Cookies added successfully")

        except Exception as e:
            print(f"❌ Cookie setting failed: {str(e)}")
            raise
        finally:
            if should_close_page:
                await page.close()

        print("✅ Authentication cookies added")


# Main authentication function using session response
async def authenticate_playwright_session_from_response(context: BrowserContext, session_response: Dict[str, Any], existing_page: Page = None) -> None:
    """
    Set up authentication for a Playwright BrowserContext using session response data.
    """
    print("🎭 Setting up Playwright authentication from session response...")

    try:
        # Extract tokens from session response
        tokens = {
            'ticket': session_response['idToken'],
            'asid': session_response['sessionId'], 
            'at': session_response['authToken'],
            'cookieDomain': 'www-stg.chewy.net'
        }

        # Print out the authentication tokens
        print("Authentication Tokens:")
        print(f"ID Token (ticket): \033[32m{tokens['ticket'][:50]}...\033[0m")
        print(f"Session ID (asid): \033[32m{tokens['asid']}\033[0m")
        print(f"Auth Token (at): \033[32m{tokens['at'][:50]}...\033[0m")

        auth_service = PlaywrightAuthService()
        await auth_service.add_authentication_cookies(context, tokens, existing_page)

        print("🎉 Authentication cookies set successfully!")

    except Exception as e:
        print(f"❌ Authentication failed: {str(e)}")
        raise


# Legacy function for backward compatibility
async def authenticate_playwright_session(context: BrowserContext, existing_page: Page = None) -> Dict[str, str]:
    """
    Legacy function - use authenticate_playwright_session_from_response instead.
    """
    raise NotImplementedError("Use authenticate_playwright_session_from_response with session response data instead")
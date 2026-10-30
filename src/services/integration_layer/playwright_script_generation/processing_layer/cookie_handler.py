import json
import logging
import html
from pathlib import Path

logger = logging.getLogger(__name__)

async def load_cookies_from_json(context, json_file_path, domain='.www-stg.chewy.net'):
    """Load cookies and localStorage from JSON file and set them in the browser context"""
    try:
        with open(json_file_path, 'r') as f:
            content = f.read()
            # Decode HTML entities like &quot;
            content = html.unescape(content)
            cookies_data = json.loads(content)
        
        # Handle different JSON structures
        if isinstance(cookies_data, list):
            cookies = cookies_data
        elif isinstance(cookies_data, dict):
            if 'cookies' in cookies_data:
                cookies = cookies_data['cookies']
            else:
                cookies = [{'name': k, 'value': v, 'domain': domain, 'path': '/'} 
                          for k, v in cookies_data.items()]
        
        # Format cookies
        formatted_cookies = []
        for cookie in cookies:
            if isinstance(cookie, dict) and 'name' in cookie and 'value' in cookie:
                formatted_cookie = {
                    'name': cookie['name'],
                    'value': cookie['value'],
                    'domain': cookie.get('domain', domain),
                    'path': cookie.get('path', '/'),
                }
                for field in ['httpOnly', 'secure', 'sameSite', 'expires']:
                    if field in cookie:
                        formatted_cookie[field] = cookie[field]
                formatted_cookies.append(formatted_cookie)
        
        await context.add_cookies(formatted_cookies)
        
        # Handle localStorage if present
        if isinstance(cookies_data, dict) and 'origins' in cookies_data:
            for origin_data in cookies_data['origins']:
                if 'localStorage' in origin_data:
                    origin_url = origin_data['origin']
                    page = await context.new_page()
                    await page.goto(origin_url)
                    
                    for storage_item in origin_data['localStorage']:
                        await page.evaluate(f"localStorage.setItem('{storage_item['name']}', '{storage_item['value']}')")
                    
                    await page.close()
        
        logger.info(f"✅ Loaded {len(formatted_cookies)} cookies from {json_file_path}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to load cookies from {json_file_path}: {e}")
        return False

async def load_cookies_from_data(context, cookie_data):
    """Load cookies directly from JSON data (not from file)"""
    try:
        # Handle different JSON structures
        if isinstance(cookie_data, list):
            cookies = cookie_data
        elif isinstance(cookie_data, dict):
            if 'cookies' in cookie_data:
                cookies = cookie_data['cookies']
            else:
                cookies = [{'name': k, 'value': v, 'domain': '.chewy.net', 'path': '/'} 
                          for k, v in cookie_data.items()]
        
        # Format cookies
        formatted_cookies = []
        for cookie in cookies:
            if isinstance(cookie, dict) and 'name' in cookie and 'value' in cookie:
                formatted_cookie = {
                    'name': cookie['name'],
                    'value': cookie['value'],
                    'domain': cookie.get('domain', '.chewy.net'),
                    'path': cookie.get('path', '/'),
                }
                for field in ['httpOnly', 'secure', 'sameSite', 'expires']:
                    if field in cookie:
                        formatted_cookie[field] = cookie[field]
                formatted_cookies.append(formatted_cookie)
        
        await context.add_cookies(formatted_cookies)
        
        # Store localStorage data for later use when navigating to the actual page
        if isinstance(cookie_data, dict) and 'origins' in cookie_data:
            # Store localStorage data in context for later use
            context._localStorage_data = cookie_data['origins']
            logger.info(f"📦 Stored localStorage data for {len(cookie_data['origins'])} origins")
        
        logger.info(f"✅ Loaded {len(formatted_cookies)} cookies from data")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to load cookies from data: {e}")
        return False
"""
Simple Database Lookup Strategy for Locator Reuse
================================================

This module implements a clean, production-ready strategy for:
1. Database lookup by fuzzy matching unique_id
2. Locator validation and fallback to normal flow
3. Database storage of successful locators

No cache mechanisms - just simple database operations as requested.
"""

from typing import Optional, Tuple
from .locator_reuse import make_uid_base
from .db_utils import get_playwright_locator_by_uid_prefix, update_playwright_locator_by_uid


def lookup_and_validate_locator(step: str, page_name: str, playwright_page) -> Tuple[Optional[str], str]:
    """
    Production-ready locator lookup with validation fallback.
    
    Strategy:
    1. Generate unique_id for current step/page combination
    2. Try fuzzy database lookup for existing locator
    3. If found, validate the locator works
    4. If validation fails or no match, run normal flow
    5. Store successful locator in database
    
    Args:
        step: BDD step text
        page_name: Current page name 
        playwright_page: Playwright page object for validation
        
    Returns:
        Tuple of (locator_if_found, unique_id_used)
    """
    
    # Generate unique_id for this step/page combination
    unique_id = make_uid_base(step, page_name)
    print(f"🔍 [DB-Lookup] Generated UID: {unique_id[:50]}...")
    
    # Try fuzzy database lookup
    existing_locator = get_playwright_locator_by_uid_prefix(unique_id)
    
    if existing_locator:
        print(f"✅ [DB-Lookup] Found existing locator via fuzzy match")
        
        # Validate the locator works on current page
        if validate_locator_on_page(existing_locator, playwright_page):
            print(f"🎯 [DB-Lookup] Locator validation SUCCESS - reusing existing")
            return existing_locator, unique_id
        else:
            print(f"❌ [DB-Lookup] Locator validation FAILED - falling back to normal flow")
    else:
        print(f"📭 [DB-Lookup] No existing locator found - using normal flow")
    
    # Return None to indicate normal flow should proceed
    return None, unique_id


def store_successful_locator(unique_id: str, locator: str) -> bool:
    """
    Store a successful locator in the database.
    
    Args:
        unique_id: The unique identifier for this step/page combination
        locator: The working locator to store
        
    Returns:
        True if storage was successful, False otherwise
    """
    if not unique_id or not locator:
        print(f"⚠️  [DB-Store] Invalid data - UID: {bool(unique_id)}, Locator: {bool(locator)}")
        return False
        
    success = update_playwright_locator_by_uid(unique_id, locator)
    
    if success:
        print(f"💾 [DB-Store] Successfully stored locator for UID: {unique_id[:50]}...")
    else:
        print(f"❌ [DB-Store] Failed to store locator for UID: {unique_id[:50]}...")
        
    return success


def validate_locator_on_page(locator: str, playwright_page) -> bool:
    """
    Validate that a locator works on the current page.
    
    Args:
        locator: The locator string to validate
        playwright_page: Playwright page object
        
    Returns:
        True if locator is valid and element exists, False otherwise
    """
    try:
        # Try to locate the element with a short timeout
        element = playwright_page.locator(locator)
        
        # Check if element is visible and available
        # Using a short timeout to fail fast
        is_visible = element.is_visible(timeout=2000)  # 2 second timeout
        
        if is_visible:
            print(f"✅ [Validation] Locator found visible element")
            return True
        else:
            print(f"👻 [Validation] Locator found element but not visible")
            return False
            
    except Exception as e:
        print(f"❌ [Validation] Locator validation failed: {str(e)[:100]}...")
        return False


# Integration functions for easy adoption

def try_reuse_or_normal_flow(step: str, page_name: str, playwright_page, normal_locator_function):
    """
    Complete integration function that handles the full flow:
    1. Try database lookup and validation
    2. If successful, return existing locator
    3. If failed, run normal flow and store result
    
    Args:
        step: BDD step text
        page_name: Current page name
        playwright_page: Playwright page object
        normal_locator_function: Function to call for normal locator discovery
        
    Returns:
        Working locator string
    """
    
    # Step 1: Try database lookup and validation
    existing_locator, unique_id = lookup_and_validate_locator(step, page_name, playwright_page)
    
    if existing_locator:
        # Success case: reuse existing validated locator
        return existing_locator
    
    # Step 2: Run normal flow
    print(f"🔧 [Integration] Running normal locator discovery flow...")
    new_locator = normal_locator_function()
    
    if new_locator:
        # Step 3: Store successful result
        print(f"💡 [Integration] Normal flow succeeded, storing for future reuse...")
        store_successful_locator(unique_id, new_locator)
        return new_locator
    else:
        print(f"❌ [Integration] Normal flow also failed to find locator")
        return None


def get_reusable_locator_simple(step: str, page_name: str, playwright_page, 
                               element_discovery_func) -> Optional[str]:
    """
    Simplified version for direct integration into existing code.
    
    Usage Example:
    ```python
    # In your existing playwright automation
    def find_element_for_step(step, page_name, page):
        # Try simple database reuse first
        locator = get_reusable_locator_simple(step, page_name, page, 
                                            lambda: discover_element_normally(step, page))
        
        if locator:
            return page.locator(locator)
        else:
            # Handle failure case
            return None
    ```
    """
    
    return try_reuse_or_normal_flow(step, page_name, playwright_page, element_discovery_func)


# Simple usage pattern for production
def production_locator_strategy(bdd_step: str, current_page: str, page_object, 
                              normal_discovery_callback) -> str:
    """
    Production-ready entry point that encapsulates the entire strategy.
    
    This is the main function you would integrate into your existing codebase.
    
    Args:
        bdd_step: The BDD step being executed
        current_page: Name/identifier of the current page
        page_object: Playwright page instance
        normal_discovery_callback: Function that performs normal locator discovery
        
    Returns:
        A working locator string, or raises exception if all methods fail
    """
    
    result = try_reuse_or_normal_flow(bdd_step, current_page, page_object, normal_discovery_callback)
    
    if not result:
        raise Exception(f"Both database lookup and normal discovery failed for step: {bdd_step[:100]}...")
    
    return result
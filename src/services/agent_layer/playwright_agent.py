import httpx
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
import os
import pandas as pd
from urllib.parse import urlparse
from func_timeout import func_timeout, FunctionTimedOut
from utils.constants import PLATFORM_CONFIG
import logging
from utils.recording_cleanup import get_video_run_dir
from pathlib import Path

# Initialize logger
logger = logging.getLogger(__name__)

from datetime import datetime

def take_screenshot(page, label: str, video_dir: str):
    """Save screenshot inside the same directory as video recordings."""
    try:
        os.makedirs(video_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{label}_{timestamp}.png".replace(" ", "_")
        path = os.path.join(video_dir, filename)
        page.screenshot(path=path, full_page=True)
        return path
    except Exception as e:
        print(f"⚠️ Failed to take screenshot: {e}")
        return None


def _slugify(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '_', (name or '').strip())[:80]

# put these near the top of the file
import time
from utils.recording_cleanup import get_video_run_dir

def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "_" for c in (s or ""))[:40]

async def _snap(page, idx: int, phase: str, action: str, obj: str):
    video_dir = get_video_run_dir()
    if not video_dir:
        return None
    video_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    fname = f"{idx:03d}_{phase}_{_slug(action)}_{_slug(obj)}_{ts}.png"
    path = video_dir / fname
    try:
        await page.screenshot(path=str(path), full_page=False)
        logger.info(f"📸 {phase.upper()} screenshot saved: {path}")
        return path
    except Exception as e:
        logger.error(f"❌ Failed to save {phase} screenshot for step {idx}: {e}")
        return None

async def perform(page: Page, steps: dict, chroma_connector, session_id: str,current_df: pd.DataFrame,original_step,NEW_WINDOW_DETECTED):
    steps_performed = ""
    steps_not_performed = ""
    status = 1
    previous_url = page.url
    current_frame = None  # Track current frame context
    iframe_chain = []  # Track iframe xpath chain for nested iframes

    def log_iframe_state(step_info=""):
        """Helper function to log current iframe state for debugging"""
        chain_info = f"Chain: {iframe_chain}" if iframe_chain else "Chain: []"
        frame_info = f"Frame: {'Active' if current_frame is not None else 'None'}"
        logger.info(f"🔍 [IFRAME STATE] {step_info} | {frame_info} | {chain_info}")

    def should_auto_exit_iframe(step_action, step_xpath):
        """
        Detect if an action should automatically exit iframe context.
        This helps when steps don't explicitly call exit_iframe but the action
        is clearly intended for the main page.
        
        IMPORTANT: This should be conservative to avoid false positives.
        When in doubt, stay in iframe context.
        """
        if not current_frame or not step_xpath:
            return False
            
        # If we just entered an iframe, don't immediately exit on the next action
        # This prevents the case where iframe content has /html/body paths
        if len(iframe_chain) > 0:
            logger.info(f"🔍 Currently in iframe context (chain: {iframe_chain}) - being conservative with auto-exit")
        
        # Check for STRONG main page indicators that are unlikely to be in iframe content
        strong_main_page_indicators = [
            '@id="main"',          # Main content area (rarely in iframes)
            '@id="header"',        # Header elements (usually on main page)
            '@id="nav"',           # Navigation elements (usually on main page)  
            '@id="footer"',        # Footer elements (usually on main page)
            '@id="sidebar"',       # Sidebar elements (usually on main page)
            '//nav/',              # Navigation elements
            '//header/',           # Header elements
            '//footer/',           # Footer elements
        ]
        
        step_xpath_lower = step_xpath.lower()
        for indicator in strong_main_page_indicators:
            if indicator in step_xpath_lower:
                logger.info(f"🔍 Detected STRONG main page xpath pattern '{indicator}' - should exit iframe")
                return True
        
        # REMOVED: Weak patterns like '/html/body' that can appear in iframe content
        # These were causing false positives when iframe content uses absolute paths
        
        logger.info(f"🔍 No strong main page indicators found in '{step_xpath}' - staying in iframe context")
        return False

    video_dir = get_video_run_dir()
    if video_dir is None:
        logger.warning("⚠️ No video directory registered. Screenshots will not be saved.")
    else:
        logger.info(f"📂 Using video directory for screenshots: {video_dir}")

    # Log initial iframe state
    log_iframe_state("Initial")

    for idx, step in enumerate(steps, start=1):
        # Safe defaults so `finally` can always log/snapshot without NameError
        action = step.get("action", "unknown")
        obj = step.get("element", "unknown")

        try:
            # Check for URL changes that should reset iframe context
            current_url = page.url
            if current_url != previous_url and current_frame is not None:
                logger.info(f"🔄 URL changed from {previous_url} to {current_url} - resetting iframe context")
                current_frame = None
                iframe_chain.clear()
                logger.info("✅ Iframe context auto-reset due to navigation")
                log_iframe_state(f"After URL change to {current_url}")
            previous_url = current_url
            
            # Log iframe state before each action for debugging
            log_iframe_state(f"Before {action} on {obj}")
            
            # Check if we should auto-exit iframe based on xpath patterns
            step_xpath = step.get("xpath", "")
            if should_auto_exit_iframe(action, step_xpath):
                logger.info(f"🔄 Auto-exiting iframe for main page action: {action}")
                current_frame = None
                iframe_chain.clear()
                log_iframe_state("After auto-exit")
            
            # BEFORE snapshot
            await _snap(page, idx, "before", action, obj)
            logger.info(f"📸 BEFORE step {idx}: {action} {obj}")

            if action == "navigate":
                # Navigation should reset iframe context
                if current_frame is not None:
                    logger.info("🔄 Navigation detected - resetting iframe context")
                    current_frame = None
                    iframe_chain.clear()
                    logger.info("✅ Iframe context reset for navigation")
                    log_iframe_state("After navigation reset")
                
                # Handle navigation logic here if needed
                page_name = extract_page_name(page.url)
                current_df = await record_page_object_details(page_name, action, step.get('element', 'navigation'),
                                                 step.get('url', ''), '', '', page,
                                                 page, None, step.get('xpath', ''),
                                                 '', '', '', current_df, original_step, NEW_WINDOW_DETECTED, [])
                
            elif action in ["enter", "populate"]:
                obj = step['element']
                test_data = step["text"]
                xp = step["xpath"]
                page_name = extract_page_name(page.url)
                page_height = await page.evaluate("() => document.body.scrollHeight")
                scroll_position = 0
                scroll_increment = 100


                if current_frame is not None:
                    element = current_frame.locator(f'xpath={step["xpath"]}').first

                    while scroll_position < page_height:
                        try:
                            if await element.is_visible() and await element.is_enabled():
                                content_for_recording = page  # Always use page for locator generation
                                current_df= await record_page_object_details(page_name, action, obj,
                                                                 test_data, '', '',
                                                                 content_for_recording, page,
                                                                 element, xp,
                                                                 '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)
                                break  # Exit loop if interaction is successful

                        except Exception as e:
                            #logger.info(f"Frame element not interactable: {e}")
                            # Scroll down by the increment
                            await page.evaluate(f"window.scrollBy(0, {scroll_increment});")
                            scroll_position += scroll_increment
                            await asyncio.sleep(0.5)  # Small delay between scrolls

                else:
                    element = page.locator(f'xpath={step["xpath"]}').first

                    while scroll_position < page_height:
                        try:
                            if await element.is_visible() and await element.is_enabled():
                                content_for_recording = page
                                current_df = await record_page_object_details(page_name, action, obj,
                                                                 test_data, '', '',
                                                                 content_for_recording, page,
                                                                 element, xp,
                                                                 '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)
                                break  # Exit loop if interaction is successful

                        except Exception as e:
                            #logger.info(f"Page element not interactable: {e}")
                            # Scroll down by the increment
                            await page.evaluate(f"window.scrollBy(0, {scroll_increment});")
                            scroll_position += scroll_increment
                            await asyncio.sleep(0.5)  # Small delay between scrolls

                await asyncio.sleep(2)
                await element.clear()
                await element.fill(step["text"])

            elif action == "click":
                obj = step['element']
                xp = step["xpath"]
                logger.info(obj)
                logger.info(xp)
                page_name = extract_page_name(page.url)
                # logger.info("page_name:", page_name)


                if current_frame is not None:
                    element = current_frame.locator(f'xpath={step["xpath"]}').first

                    # Special handling for PayPal - only for actual payment buttons, not radio button selections
                    original_step_text = str(original_step.get('original_bdd_step', '')).lower()
                    
                    # Check if this is a radio button selection (should NOT trigger PayPal flow)
                    is_radio_selection = any(indicator in original_step_text for indicator in [
                        'radio button', 'radio', 'select paypal', 'choose paypal', 'option'
                    ])
                    
                    # Check if element appears to be an input (likely radio/checkbox)
                    is_input_element = 'input' in xp.lower()
                    
                    # Only trigger PayPal flow for actual payment buttons
                    is_payment_button = any(indicator in original_step_text for indicator in [
                        'pay with paypal', 'paypal checkout', 'pay later', 'paypal button', 'button'
                    ]) and not is_radio_selection
                    
                    logger.info(f"[PayPal Debug] Object: {obj}, Step: {original_step_text}")
                    logger.info(f"[PayPal Debug] is_radio_selection: {is_radio_selection}, is_input_element: {is_input_element}, is_payment_button: {is_payment_button}")
                    
                    if (obj in ["Pay with PayPal", "PayPal", "PayPal Checkout", "PayPal PayLater", "Paypal Credit"] and 
                        not is_radio_selection and not is_input_element and 
                        (is_payment_button or 'button' in original_step_text)):
                        
                        logger.info(f"[PayPal Debug] ✅ Triggering PayPal payment flow for: {obj}")
                        try:
                            current_df = await record_page_object_details(
                                page_name, action, obj,
                                '', '', '', current_frame,
                                page,
                                element, xp,
                                step.get("condition", ""), '', '', current_df, original_step, NEW_WINDOW_DETECTED, iframe_chain
                            )
                        except Exception as e:
                            logger.info(f"Error during Record checkout: {e}")

                        try:
                            ok = await handle_paypal_checkout(page, element)  # this does the single click + popup
                            if not ok:
                                logger.info("[PayPal] Handler reported failure")
                        except Exception as e:
                            logger.info(f"Error during PayPal checkout: {e}")
                        continue  # ⬅️ important: skip the generic click path

                    elif obj in ["Pay with PayPal", "PayPal", "PayPal Checkout", "PayPal PayLater", "Paypal Credit"]:
                        # PayPal object detected but conditions not met for payment flow
                        logger.info(f"[PayPal Debug] ❌ Skipping PayPal payment flow for radio/input selection: {obj}")
                        element = current_frame.locator(f'xpath={step["xpath"]}').first
                        current_df = await record_page_object_details(page_name, action, obj,
                                                         '', '', '',
                                                         current_frame, page,
                                                         element, xp,
                                                         '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)

                        await element.evaluate("el => el.scrollIntoView({block: 'center'})")
                    
                    else:
                        element = current_frame.locator(f'xpath={step["xpath"]}').first
                        current_df = await record_page_object_details(page_name, action, obj,
                                                         '', '', '',
                                                         current_frame, page,
                                                         element, xp,
                                                         '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)

                        await element.evaluate("el => el.scrollIntoView({block: 'center'})")

                elif "Giftcard with" in obj and "$" in obj:
                    try:
                        current_df = await record_page_object_details(page_name, action, obj,
                                                         '', '', '', current_frame,
                                                         page,
                                                         element, xp,
                                                         step["condition"], '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)
                    except Exception as e:
                        logger.info(f"Error during Record gift card: {e}")
                    try:
                        await handle_gift_card_flow(page)
                    except Exception as e:
                        logger.info(f"Error during Gift Card flow: {e}")


                else:
                    try:
                        element = page.locator(f'xpath={step["xpath"]}').first
                        content_for_recording = page
                        try:

                            current_df = await record_page_object_details(page_name, action, obj,
                                                             '', '', '',
                                                             content_for_recording, page,
                                                             element, xp,
                                                             '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)

                            await element.evaluate("el => el.scrollIntoView({block: 'center'})")

                        except Exception as e:
                            logger.info(f"exception during record page details:{e}")
                    except Exception as e:
                        logger.info(f"while click Element: {e}")



                try:
                    await element.click(timeout=3000)
                except Exception as e:
                    try:
                        await element.evaluate("element => element.click()")
                        await asyncio.sleep(2)
                    except Exception as e:
                        try:
                            logger.info(f"exception during element click element: {e}")
                            await element.click(force=True)
                            await asyncio.sleep(2)
                        except Exception as e:
                            logger.info(f"Exception during performing click action: {e}")

            elif action == "verify":
                page_name = extract_page_name(page.url)
                obj = step['element']
                xp = step["xpath"]
                content_for_recording = page
                element = None
                if step["xpath"] != None:
                    if current_frame is not None:

                        element = current_frame.locator(f'xpath={step["xpath"]}').first
                        current_df = await record_page_object_details(page_name, action, obj,
                                                         '', '', '', current_frame,
                                                         page,
                                                         element, xp,
                                                         step["condition"], '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)
                    else:
                        element = page.locator(f'xpath={step["xpath"]}').first
                        current_df= await record_page_object_details(page_name, action, obj,
                                                         '', '', '', content_for_recording,
                                                         page,
                                                         element, xp,
                                                         step["condition"], '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)

                else:
                    current_df=await record_page_object_details(page_name, action, obj,
                                                     '', '', '', content_for_recording,
                                                     page,
                                                     '', xp,
                                                     step["condition"], '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)

            elif action == "hover":
                page_name = extract_page_name(page.url)
                obj = step['element']
                xp = step["xpath"]

                if current_frame is not None:
                    element = current_frame.locator(f'xpath={step["xpath"]}').first
                    current_df = await record_page_object_details(page_name, action, obj,
                                                     '', '', '', current_frame,
                                                     page,
                                                     element, xp,
                                                     step["condition"], '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)

                else:
                    element = page.locator(f'xpath={step["xpath"]}').first
                    content_for_recording = page
                    current_df = await record_page_object_details(page_name, action, obj,
                                                     '', '', '', content_for_recording,
                                                     page,
                                                     element, xp,
                                                     '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)
                try:
                    await element.hover()
                except:
                    await element.hover(force=True)

            elif action == "wait":
                await page.wait_for_timeout(int(step["time"]) * 1000)  # Convert seconds to milliseconds

            elif action == "switch_to_iframe":
                try:
                    iframe_xpath = step["iframe_xpath"]
                    logger.info(f"🔄 Switching to iframe with xpath: {iframe_xpath}")
                    
                    # Add to iframe chain for tracking
                    iframe_chain.append(iframe_xpath)
                    logger.info(f"🔗 Current iframe chain: {iframe_chain}")
                    
                    # Add iframe timeout cleanup - if iframe chain gets too deep, something might be wrong
                    if len(iframe_chain) > 5:
                        logger.warning(f"⚠️ Iframe chain depth {len(iframe_chain)} exceeds safety limit - potential iframe leak")
                        # Consider auto-reset if chain gets unreasonably deep
                        # iframe_chain = iframe_chain[-3:]  # Keep only last 3 levels
                    
                except Exception as e:
                    logger.info(f"while switching:{e}")
                
                # 🎯 NESTED IFRAME LOGIC: Use current_frame if already in an iframe, otherwise use page
                try:
                    if current_frame is not None:
                        # We're already in an iframe, so look for nested iframe within current frame
                        logger.info("🔗 Looking for nested iframe within current frame")
                        iframe_locator = current_frame.locator(f'xpath={iframe_xpath}').first
                        await iframe_locator.wait_for(state="attached", timeout=10000)
                        logger.info("✅ Nested iframe found and attached")
                    else:
                        # First iframe from main page
                        logger.info("🎯 Looking for iframe from main page")
                        iframe_locator = page.locator(f'xpath={iframe_xpath}').first
                        await iframe_locator.wait_for(state="attached", timeout=10000)
                        logger.info("✅ Iframe found and attached")
                except Exception as e:
                    logger.info(f"while founding iframe: {e}")
                
                # Create frame locator for subsequent operations
                try:
                    if current_frame is not None:
                        # Chain frame locators for nested iframes
                        current_frame = current_frame.frame_locator(f'xpath={iframe_xpath}').first
                        logger.info(f"🔗 Nested frame locator created: {current_frame}")
                    else:
                        # First frame locator from main page
                        current_frame = page.frame_locator(f'xpath={iframe_xpath}').first
                        logger.info(f"🎯 Main frame locator created: {current_frame}")
                    logger.info("✅ Frame locator created - ready for iframe operations")
                except Exception as e:
                    logger.info(f"while locating frame: {e}")

                obj = step['element']
                xp = step["iframe_xpath"]
                page_name = extract_page_name(page.url)
                content_for_recording = page
                current_df = await record_page_object_details(page_name, action, obj,
                                                 '', '', '', content_for_recording,
                                                 page,
                                                 iframe_locator, xp,
                                                 '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)

            elif action == "switch_to_main" or action == "exit_iframe":
                # Exit iframe context and return to main page
                logger.info(f"🔄 Exiting iframe context - current chain: {iframe_chain}")
                current_frame = None
                iframe_chain.clear()
                logger.info("✅ Returned to main page context")
                log_iframe_state("After iframe exit")
                
                # Record the iframe exit action
                page_name = extract_page_name(page.url)
                current_df = await record_page_object_details(page_name, action, step.get('element', 'main_page'),
                                                 '', '', '', page,
                                                 page, None, '',
                                                 '', '', '', current_df, original_step, NEW_WINDOW_DETECTED, [])

            elif action == "select":
                obj = step['element']
                test_data = step.get("option", step["element"])
                option_xpath = step.get("option_xpath", step["xpath"])
                # This is the option element xpath
                element_xpath = step["xpath"]

                try:
                    page_name = extract_page_name(page.url)

                    # NEW APPROACH: Split XPath and use select_option()
                    try:
                        # Split by last /option to get select element path
                        select_xpath = option_xpath.rsplit('/option', 1)[0]  # Extract select element path

                        # Verify it ends with 'select'
                        if element_xpath.endswith('/select'):
                            # Create locator for select element and use select_option()
                            if current_frame is not None:
                                select_element = current_frame.locator(f'xpath={element_xpath}').first
                                content_for_recording = current_frame
                            else:
                                select_element = page.locator(f'xpath={element_xpath}').first
                                content_for_recording = page

                            # Record the action
                            current_df = await record_page_object_details(page_name, action, obj,
                                                             test_data, '', '', content_for_recording,
                                                             page, select_element, element_xpath,
                                                             '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)

                            # Use select_option method with the test_data
                            await select_element.evaluate("""
                                (el, value) => {
                                    if (el.tagName === 'SELECT') {
                                        el.value = value;
                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                        el.dispatchEvent(new Event('input', {bubbles: true}));
                                    }
                                }
                            """, test_data)
                            await asyncio.sleep(2)

                        else:
                            # If doesn't end with select, use fallback
                            raise Exception("XPath doesn't end with select element")

                    except Exception as e:
                        logger.info(f"select_option approach failed: {e}")

                        # FALLBACK 1: Original option xpath approach
                        try:
                            if current_frame is not None:
                                element = current_frame.locator(f'xpath={option_xpath}').first
                                content_for_recording = current_frame
                            else:
                                element = page.locator(f'xpath={option_xpath}').first
                                content_for_recording = page

                            current_df =await record_page_object_details(page_name, action, obj,
                                                             test_data, '', '', content_for_recording,
                                                             page, element, option_xpath,
                                                             '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)

                            await element.evaluate("""
                                                                     el => {
                                                                         if (el.tagName === 'OPTION') {
                                                                             el.selected = true;
                                                                             el.parentElement.value = el.value;
                                                                             el.parentElement.dispatchEvent(new Event('change', {bubbles: true}));
                                                                             el.dispatchEvent(new Event('click', {bubbles: true}));
                                                                         } else {
                                                                             el.click();
                                                                         }
                                                                     }
                                                                 """)
                            await asyncio.sleep(2)

                        except Exception as e2:
                            logger.info(f"Option xpath approach failed: {e2}")

                            # FALLBACK 2: Main element xpath (using step["xpath"])
                            try:
                                main_xpath = step["xpath"]  # This is also the option xpath
                                if current_frame:
                                    element = current_frame.locator(f'xpath={main_xpath}').first
                                else:
                                    element = page.locator(f'xpath={main_xpath}').first

                                content_for_recording = page
                                current_df = await record_page_object_details(page_name, action, obj,
                                                                 test_data, '', '', content_for_recording,
                                                                 page, element, main_xpath,
                                                                 '', '', '',current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain)
                                await element.click(force=True)
                                await asyncio.sleep(2)
                            except Exception as e3:
                                logger.info(f"Final fallback failed: {e3}")
                                pass

                except Exception as main_e:
                    logger.info(f"Main select action failed: {main_e}")
                    pass

            steps_performed += f"{action} {step['element']}\n"

        except Exception as e:
            # AFTER_ERROR snapshot for failures
            try:
                await _snap(page, idx, "after_error", action, obj)
                logger.info(f"📸 AFTER_ERROR step {idx}: {action} {obj} → {e}")
            except Exception as snap_err:
                logger.debug(f"[snap/after_error] non-fatal: {snap_err}")
            
            # If iframe operations fail, reset iframe context to prevent corruption
            if "iframe" in action.lower() and current_frame is not None:
                logger.warning(f"⚠️ Iframe operation '{action}' failed, resetting iframe context to prevent corruption")
                current_frame = None
                iframe_chain.clear()
                logger.info("✅ Iframe context reset after error")
                log_iframe_state("After error recovery")
                
            raise  # or your existing error handling
        finally:
            # Single AFTER snapshot (success or failure); no overwrites elsewhere
            try:
                await _snap(page, idx, "after", action, obj)
                logger.info(f"📸 AFTER step {idx}: {action} {obj}")
            except Exception as snap_err:
                logger.debug(f"[snap/after] non-fatal: {snap_err}")
    
    # Log final iframe state
    log_iframe_state("Final")
    return status, steps_performed, steps_not_performed,current_df


from contextlib import suppress
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

async def handle_paypal_checkout(page: Page, element) -> bool:
    """
    PayPal checkout flow (Mac & Windows) with frame-detach resilience:
      - Email → Next → (optional) Use Password Instead → Password → Log In → Consent
      - Re-queries frames each pass to avoid 'Frame was detached'
    """
    logger.info("[PayPal] Starting PayPal checkout flow...")
    paypal_email = "ragzmong@gmail.com"
    paypal_password = "welcome123"
    
    # Prevent multiple concurrent PayPal flows
    if hasattr(handle_paypal_checkout, '_in_progress'):
        logger.warning("[PayPal] PayPal checkout already in progress, skipping duplicate call")
        return False
        
    handle_paypal_checkout._in_progress = True
    
    try:
        # small helpers
        async def _visible(loc, timeout=5000):
            try:
                await loc.wait_for(state="visible", timeout=timeout)
                return True
            except Exception:
                return False

        async def _safe_click(loc, label, timeout=8000):
            try:
                await loc.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            with suppress(Exception):
                await loc.click(timeout=timeout)
                logger.info(f"[PayPal] Clicked {label}")
                return True
            with suppress(Exception):
                await loc.click(timeout=timeout, force=True)
                logger.info(f"[PayPal] Clicked {label} (force)")
                return True
            return False

        # 0) Check if PayPal popup already exists (clicked by reuse system)
        logger.info("[PayPal] Checking for existing PayPal popup windows...")
        existing_popup = None
        initial_pages = page.context.pages
        logger.info(f"[PayPal] Found {len(initial_pages)} initial pages/windows")
        
        # Look for existing PayPal popup
        for p in initial_pages:
            if p != page:  # Not the main page
                try:
                    url = p.url
                    logger.info(f"[PayPal] Checking existing page URL: {url}")
                    if ("paypal" in url.lower() or "sandbox" in url.lower() or 
                        "about:blank" in url.lower()):  # about:blank is common for new popups
                        existing_popup = p
                        logger.info(f"[PayPal] ✅ Found existing PayPal popup: {url}")
                        break
                except Exception:
                    pass
        
        # Assign the popup variable
        popup = existing_popup
        
        # 1) Handle popup - either use existing or create new one
        if popup:
            logger.info("[PayPal] ✅ Using existing popup (clicked by reuse system)")
        else:
            logger.info("[PayPal] No existing popup found - need to click PayPal button")
            
            # Enhanced pre-click handling - wait for any loading states to clear
            logger.info("[PayPal] Preparing PayPal button for interaction...")
            
            # Wait for any loading spinners to disappear
            try:
                loading_spinners = page.locator('svg[aria-label*="Loading"], .kib-spinning-loader, [class*="loading"], [class*="spinner"]')
                await loading_spinners.wait_for(state="detached", timeout=3000)
                logger.info("[PayPal] Loading spinners cleared")
            except Exception:
                logger.info("[PayPal] No loading spinners found or already cleared")
            
            # Wait for page to be in stable state
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
                
            # Give PayPal components time to initialize
            await page.wait_for_timeout(2000)
            
            # Try to wait for the element to be actionable
            try:
                await element.wait_for(state="visible", timeout=5000)
                await element.scroll_into_view_if_needed(timeout=2000)
                logger.info("[PayPal] PayPal button is visible and scrolled into view")
            except Exception as e:
                logger.warning(f"[PayPal] Could not prepare element: {str(e)}")
            
            # Enhanced click strategies
            click_attempt = 0
            max_click_attempts = 3
            
            while not popup and click_attempt < max_click_attempts:
                click_attempt += 1
                logger.info(f"[PayPal] Click attempt {click_attempt}/{max_click_attempts}")
                
                # Strategy 1: Regular click with popup expectation
                try:
                    logger.info("[PayPal] Attempting regular click...")
                    async with page.expect_popup(timeout=15000) as pop_wait:
                        await element.click(timeout=10000)
                    popup = await pop_wait.value
                    logger.info("[PayPal] ✅ Regular click successful - popup opened")
                    break
                except Exception as e1:
                    logger.warning(f"[PayPal] Regular click failed: {str(e1)}")
                    
                    # Strategy 2: Force click with popup expectation
                    try:
                        logger.info("[PayPal] Attempting force click...")
                        async with page.expect_popup(timeout=15000) as pop_wait:
                            await element.click(timeout=10000, force=True)
                        popup = await pop_wait.value
                        logger.info("[PayPal] ✅ Force click successful - popup opened")
                        break
                    except Exception as e2:
                        logger.warning(f"[PayPal] Force click failed: {str(e2)}")
                        
                        # Strategy 3: JavaScript click with popup expectation
                        try:
                            logger.info("[PayPal] Attempting JavaScript click...")
                            async with page.expect_popup(timeout=15000) as pop_wait:
                                await element.evaluate("(element) => element.click()")
                            popup = await pop_wait.value
                            logger.info("[PayPal] ✅ JavaScript click successful - popup opened")
                            break
                        except Exception as e3:
                            logger.error(f"[PayPal] All click strategies failed on attempt {click_attempt}: Regular={str(e1)[:100]}, Force={str(e2)[:100]}, JS={str(e3)[:100]}")
                            
                # Wait before retry if not the last attempt
                if click_attempt < max_click_attempts:
                    logger.info("[PayPal] Waiting before retry...")
                    await page.wait_for_timeout(2000)
            
            # If all click strategies failed, check for popup windows or new windows that appeared anyway
            if not popup:
                logger.info("[PayPal] All click attempts failed, checking for new popup windows...")
                try:
                    # Wait for potential popup/new window to appear
                    await page.wait_for_timeout(3000)
                    
                    # Get all pages in the context  
                    all_pages = page.context.pages
                    logger.info(f"[PayPal] Found {len(all_pages)} total pages/windows")
                    
                    # Look for a PayPal page (not the main page)
                    paypal_page = None
                    for p in all_pages:
                        if p != page:  # Not the main page
                            try:
                                url = p.url
                                logger.info(f"[PayPal] Checking page URL: {url}")
                                if "paypal" in url.lower() or "sandbox" in url.lower():
                                    paypal_page = p
                                    logger.info(f"[PayPal] ✅ Found PayPal page: {url}")
                                    break
                            except Exception:
                                pass
                    
                    if paypal_page:
                        # Use the found PayPal page as our popup
                        popup = paypal_page
                        logger.info("[PayPal] ✅ Using existing PayPal window as popup")
                    else:
                        # Look for PayPal overlay iframes as final fallback
                        logger.info("[PayPal] No separate PayPal window found, checking for overlays...")
                        paypal_overlays = page.locator('iframe[class*="paypal-checkout"], iframe[title*="PayPal"], iframe[id*="paypal"]')
                        if await paypal_overlays.count() > 0:
                            logger.info("[PayPal] ⚠️ Found PayPal overlay iframe - this may need different handling")
                            # For now, let's try to continue without popup
                            return True  # PayPal overlay may handle authentication internally
                        else:
                            logger.error("[PayPal] No popup, window, or overlay found - PayPal integration failed")
                            return False
                            
                except Exception as e4:
                    logger.error(f"[PayPal] Window/overlay check failed: {str(e4)}")
                    return False
        
        if not popup:
            logger.error("[PayPal] Failed to open PayPal popup with any strategy")
            return False
            
        # Ensure we're working with the correct window
        try:
            await popup.bring_to_front()
            await popup.wait_for_load_state("domcontentloaded", timeout=10000)
            logger.info(f"[PayPal] ✅ Popup ready - URL: {popup.url}")
        except Exception as e:
            logger.warning(f"[PayPal] Popup setup issue: {str(e)}")
            pass

        # 2) Bring popup to front + wait for PayPal to actually mount
        try:
            await popup.bring_to_front()
        except Exception:
            pass

        # Give the popup time to navigate to paypal.com (or sandbox)
        try:
            await popup.wait_for_url(
                re.compile(r"paypal\.com", re.I), timeout=15000
            )
        except Exception:
            # Sometimes the initial url is blank then navigates; carry on with load-state waits
            pass

        # Base readiness: DOM first, then a few short 'networkidle' tries
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        for _ in range(4):
            try:
                await popup.wait_for_load_state("networkidle", timeout=2500)
                break
            except Exception:
                try:
                    await popup.wait_for_timeout(400)
                except Exception:
                    pass

        # 3) EMAIL — patient, detach-safe poll (up to ~30s), main frame then iframes
        EMAIL_NAMES = ["Email", "Email or mobile number"]
        CSS_FALLBACK = ["#email", "#login_email", "input[name='login_email']", "input[name='email']"]

        async def _find_email_in(target):
            # Prefer role/name (matches PayPal a11y labels)
            for nm in EMAIL_NAMES:
                loc = target.get_by_role("textbox", name=nm)
                if await loc.count() > 0:
                    return loc.first
            # CSS fallbacks
            for sel in CSS_FALLBACK:
                loc = target.locator(sel)
                if await loc.count() > 0:
                    return loc.first
            return None

        email_field = None
        logger.info("[PayPal] 🔍 Starting email field search...")
        
        for attempt in range(30):  # ~30s total; Windows can be slow to mount the inner frame
            try:
                logger.info(f"[PayPal] 📧 Email search attempt {attempt + 1}/30")
                
                # main frame
                logger.info("[PayPal] 🔍 Checking main popup frame for email field...")
                cand = await _find_email_in(popup)
                if cand and await cand.is_visible():
                    email_field = cand
                    logger.info("[PayPal] ✅ Found email field in main popup frame")
                    break

                # iframes (re-snapshot each pass; frames may detach/attach)
                popup_frames = list(popup.frames)
                logger.info(f"[PayPal] 🔍 Checking {len(popup_frames)} iframe(s) for email field...")
                
                for i, fr in enumerate(popup_frames):
                    try:
                        logger.info(f"[PayPal] 🔍 Checking iframe {i + 1}/{len(popup_frames)}: {fr.url[:100]}...")
                        cand = await _find_email_in(fr)
                        if cand and await cand.is_visible():
                            email_field = cand
                            logger.info(f"[PayPal] ✅ Found email field in iframe {i + 1}")
                            break
                    except Exception as e:
                        logger.info(f"[PayPal] ⚠️ Iframe {i + 1} check failed: {str(e)[:50]}...")
                        # frame might have detached mid-lookup; ignore and continue
                        continue
                        
                if email_field:
                    break
                    
            except Exception as e:
                logger.info(f"[PayPal] ⚠️ Email search attempt {attempt + 1} failed: {str(e)[:50]}...")
                # page may be transitioning; ignore and retry
                pass

            # tiny wait before next probe
            try:
                await popup.wait_for_timeout(1000)
            except Exception:
                pass

        if not email_field:
            logger.warning("[PayPal] ❌ Email field not found after 30 attempts; popup likely still mounting or different layout")
            # optional: grab a screenshot to debug
            try:
                await popup.screenshot(path="paypal_no_email.png", full_page=True)
                logger.info("[PayPal] Saved screenshot: paypal_no_email.png")
            except Exception:
                pass
            return False

        await email_field.click()
        await email_field.fill(paypal_email)
        logger.info("[PayPal] Entered email")

        # 4) Next
        next_btn = popup.get_by_role("button", name="Next")
        if await next_btn.count() == 0:
            for fr in list(popup.frames):
                try:
                    nb = fr.get_by_role("button", name="Next")
                    if await nb.count() > 0:
                        next_btn = nb.first
                        break
                except Exception:
                    continue
        if await next_btn.count() > 0 and await next_btn.first.is_enabled():
            await next_btn.first.click()
            logger.info("[PayPal] Clicked 'Next'")

        # ---- After Next, iframe often gets replaced. Poll & re-query each pass.
        with suppress(Exception):
            await popup.wait_for_timeout(400)

        # 5) Use Password Instead (optional, robust & detach-safe)
        use_pwd_clicked = False
        rx_use_pwd = re.compile(r"use\s*password\s*instead", re.I)
        for _ in range(8):  # ~8s total
            try:
                # main frame candidates
                candidates = [
                    popup.get_by_role("button", name=rx_use_pwd),
                    popup.get_by_role("link",   name=rx_use_pwd),
                    popup.locator("button, a[role='button'], a").filter(has_text=rx_use_pwd),
                ]
                found = None
                for c in candidates:
                    with suppress(Exception):
                        if await c.count() > 0 and await c.first.is_visible():
                            found = c.first
                            break
                # iframe candidates
                if not found:
                    for fr in list(popup.frames):
                        try:
                            cands = [
                                fr.get_by_role("button", name=rx_use_pwd),
                                fr.get_by_role("link",   name=rx_use_pwd),
                                fr.locator("button, a[role='button'], a").filter(has_text=rx_use_pwd),
                            ]
                            for c in cands:
                                if await c.count() > 0 and await c.first.is_visible():
                                    found = c.first
                                    break
                            if found:
                                break
                        except Exception:
                            continue

                if found and await _safe_click(found, "'Use Password Instead'"):
                    use_pwd_clicked = True
                    break
            except Exception:
                pass

            with suppress(Exception):
                await popup.wait_for_timeout(1000)

        if not use_pwd_clicked:
            logger.info("[PayPal] 'Use Password Instead' not present/needed; continuing")

        # 6) Password (detach-safe search)
        pwd_field = None
        for _ in range(12):  # up to ~12s in case it renders after a switch
            # main frame
            try:
                pwd = popup.get_by_role("textbox", name="Password")
                if await pwd.count() > 0 and await pwd.first.is_visible():
                    pwd_field = pwd.first
                    break
            except Exception:
                pass
            # iframes
            for fr in list(popup.frames):
                try:
                    cand = fr.get_by_role("textbox", name="Password")
                    if await cand.count() > 0 and await cand.first.is_visible():
                        pwd_field = cand.first
                        break
                except Exception:
                    continue
            if pwd_field:
                break
            with suppress(Exception):
                await popup.wait_for_timeout(1000)

        if not pwd_field:
            logger.warning("[PayPal] Password field not found")
            return False
        await pwd_field.click()
        await pwd_field.fill(paypal_password)
        logger.info("[PayPal] Entered password")

        # 7) Log In (detach-safe)
        login = None
        for _ in range(4):
            try:
                btn = popup.get_by_role("button", name="Log In")
                if await btn.count() > 0:
                    login = btn.first
                    break
            except Exception:
                pass
            for fr in list(popup.frames):
                try:
                    btn = fr.get_by_role("button", name="Log In")
                    if await btn.count() > 0:
                        login = btn.first
                        break
                except Exception:
                    continue
            if login:
                break
            with suppress(Exception):
                await popup.wait_for_timeout(300)

        if login:
            with suppress(Exception):
                await login.click(timeout=15000)
            logger.info("[PayPal] Clicked 'Log In'")

        with suppress(Exception):
            await popup.wait_for_timeout(700)

        # 8) Consent (detach-safe). Labels: Save and/& Continue, Agree &/and Continue, Agree & Pay Now
        LABELS = [
            re.compile(r"Save\s*&?\s*Continue", re.I),
            re.compile(r"Agree\s*(?:&|and)\s*Continue", re.I),
            "Agree & Pay Now",
        ]

        def consent_buckets(target):
            return [
                target.get_by_test_id("consentButton"),
                target.locator("#consentButton"),
                target.locator("button[data-testid='submit-button-initial']"),
                target.locator("button[data-atomic-wait-task='select_agree_and_continue']"),
            ] + [target.get_by_role("button", name=l) for l in LABELS] + [
                target.locator("button, a[role='button']").filter(has_text=l) for l in LABELS
            ]

        clicked_consent = False
        for _ in range(10):  # ~10s total; consent can appear late
            try:
                # main frame
                for bucket in consent_buckets(popup):
                    with suppress(Exception):
                        if await bucket.count() > 0:
                            cand = bucket.first
                            if await cand.is_visible() and await _safe_click(cand, "Consent/Agree"):
                                clicked_consent = True
                                break
                if clicked_consent:
                    break

                # iframes
                for fr in list(popup.frames):
                    try:
                        for bucket in consent_buckets(fr):
                            with suppress(Exception):
                                if await bucket.count() > 0:
                                    cand = bucket.first
                                    if await cand.is_visible() and await _safe_click(cand, "Consent/Agree"):
                                        clicked_consent = True
                                        break
                        if clicked_consent:
                            break
                    except Exception:
                        continue
                if clicked_consent:
                    break
            except Exception:
                pass

            with suppress(Exception):
                await popup.wait_for_timeout(1000)

        if not clicked_consent:
            logger.info("[PayPal] Consent not found/clickable; proceeding")

        # 9) Best-effort wait for popup close (Windows may keep it open)
        popup_closed = False
        try:
            await popup.wait_for_event("close", timeout=15000)
            popup_closed = True
            logger.info("[PayPal] Popup closed successfully")
        except PlaywrightTimeoutError:
            logger.warning("[PayPal] Popup did not close in time - this may be normal for some PayPal integrations")
        except Exception as e:
            logger.warning(f"[PayPal] Popup close monitoring failed: {str(e)}")

        # 10) Switch back to main window - with enhanced error handling
        try:
            # Ensure main page is still valid before attempting to bring to front
            if not page.is_closed():
                await page.bring_to_front()
                logger.info("[PayPal] Switched back to main window")
            else:
                logger.error("[PayPal] Main page was closed unexpectedly during PayPal flow")
                return False
        except Exception as e:
            logger.warning(f"[PayPal] Failed to switch back to main window: {str(e)}")
            # Don't return False here - the PayPal operation might still have succeeded
            
        # 11) Verify main page is still responsive
        try:
            await page.wait_for_timeout(1000)
            # Try to interact with the main page to verify it's still alive
            if not page.is_closed():
                await page.evaluate("() => document.readyState")
                logger.info("[PayPal] Main page verification successful")
            else:
                logger.error("[PayPal] Main page is closed after PayPal authentication")
                return False
        except Exception as e:
            logger.warning(f"[PayPal] Main page verification failed: {str(e)}")
            # Don't fail the PayPal operation for verification issues
            
        return True

    except Exception as e:
        logger.error(f"[PayPal] Exception occurred during PayPal checkout: {str(e)}")
        logger.error(f"[PayPal] Exception type: {type(e).__name__}")
        
        # Check if main page is still alive
        try:
            if not page.is_closed():
                logger.info("[PayPal] Main page is still alive despite PayPal exception")
                # Try to bring main page to front
                await page.bring_to_front()
            else:
                logger.error("[PayPal] Main page was closed during PayPal exception")
        except Exception as page_check_e:
            logger.error(f"[PayPal] Failed to check main page status: {str(page_check_e)}")
            
        return False
    finally:
        # Clear the progress flag
        if hasattr(handle_paypal_checkout, '_in_progress'):
            delattr(handle_paypal_checkout, '_in_progress')


async def handle_gift_card_flow(page: Page, amount: float = 20.0):
    logger.info("[Gift Card] Starting gift card creation and entry flow...")

    try:
        # Check required config values
        client_id = PLATFORM_CONFIG.get("GIFT_CARD_S2S_CLIENT_ID")
        client_secret = PLATFORM_CONFIG.get("GIFT_CARD_S2S_CLIENT_SECRET")
        api_url = PLATFORM_CONFIG.get("GIFT_CARD_API_URL")
        auth_url = PLATFORM_CONFIG.get("GIFT_CARD_AUTH_URL")

        if not all([client_id, client_secret, api_url, auth_url]):
            raise EnvironmentError("Missing one or more required configuration values for gift card API")

        # Step 1: Get access token
        async with httpx.AsyncClient() as client:
            auth_response = await client.post(
                auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            auth_response.raise_for_status()
            token = auth_response.json().get("access_token")

        logger.info("[Gift Card] Auth successful, token received")

        # Step 2: Create gift card
        request_id = f"playwright_{os.urandom(4).hex()}"
        gift_card_payload = {
            "purchaserId": "12234",
            "promotionCode": "44930",
            "isReplacement": False,
            "requestedAmount": {"amount": float(amount), "currency": "USD"},
            "schedule": False
        }

        async with httpx.AsyncClient() as client:
            gift_card_response = await client.post(
                api_url,
                json=gift_card_payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "X-Request-ID": request_id
                }
            )
            gift_card_response.raise_for_status()
            data = gift_card_response.json()

        logger.info(f"[Gift Card] Gift card response: {data}")

        gift_card_number = data["additionalData"]["accountNumber"]
        gift_card_pin = data["additionalData"]["pin"]

        if not gift_card_number or not gift_card_pin:
            raise ValueError(f"Gift card creation failed - Number: {gift_card_number}, PIN: {gift_card_pin}")

        logger.info(f"[Gift Card] Created card - Number: {gift_card_number}, PIN: {gift_card_pin}")

        # Step 3: Wait for form fields
        await page.wait_for_timeout(2000)

        # Gift card number field - try static first
        try:
            # First try static approach
            number_field = page.get_by_test_id('gift-card-number-field').or_(page.get_by_role('textbox', name='Gift Card Number')).first
            await number_field.wait_for(state="visible", timeout=5000)
            logger.info("[Gift Card] Used Static approach")
        except:
            # Fallback to dynamic approach
            temp_locator = page.locator('input[data-testid*="gift-card-number"], input[placeholder*="Gift Card Number"], input[name*="gift-card"]').first
            number_field_locator_str = await generate_playwright_locator(page, temp_locator, '')
            if number_field_locator_str and 'xpath=' not in number_field_locator_str or ('xpath=' in number_field_locator_str and len(number_field_locator_str.split("'")[1]) > 0):
                number_field = eval(number_field_locator_str)
                logger.info("[Gift Card] Used Dynamic approach")
            else:
                raise Exception("Both static and dynamic approaches failed")
        await number_field.wait_for(state="visible", timeout=10000)
        await number_field.fill(gift_card_number)
        logger.info("[Gift Card] Filled gift card number")

        # Gift card PIN field - try static first
        try:
            # First try static approach
            pin_field = page.get_by_test_id('gift-card-pin-field').or_(page.get_by_role('textbox', name='-Digit PIN')).first
            await pin_field.wait_for(state="visible", timeout=5000)
            logger.info("[Gift Card] Used Static approach")
        except:
            # Fallback to dynamic approach
            temp_locator = page.locator('input[data-testid*="pin"], input[placeholder*="PIN"], input[name*="pin"], input[type="password"]').first
            pin_field_locator_str = await generate_playwright_locator(page, temp_locator, '')
            if pin_field_locator_str and 'xpath=' not in pin_field_locator_str or ('xpath=' in pin_field_locator_str and len(pin_field_locator_str.split("'")[1]) > 0):
                pin_field = eval(pin_field_locator_str)
                logger.info("[Gift Card] Used Dynamic approach")
            else:
                raise Exception("Both static and dynamic approaches failed")
        await pin_field.wait_for(state="visible", timeout=10000)
        await pin_field.fill(gift_card_pin)
        logger.info("[Gift Card] Filled gift card PIN")

        # Save / Apply button - try static first
        try:
            # First try static approach
            save_button = page.get_by_test_id('gift-card-form-save-button').or_(page.get_by_role('button', name='Save')).first
            await save_button.wait_for(state="visible", timeout=5000)
            logger.info("[Gift Card] Used Static approach")
        except:
            # Fallback to dynamic approach
            temp_locator = page.locator('button:has-text("Save"), button:has-text("Apply"), button[data-testid*="save"], button[data-testid*="apply"]').first
            save_button_locator_str = await generate_playwright_locator(page, temp_locator, '')
            if save_button_locator_str and 'xpath=' not in save_button_locator_str or ('xpath=' in save_button_locator_str and len(save_button_locator_str.split("'")[1]) > 0):
                save_button = eval(save_button_locator_str)
                logger.info("[Gift Card] Used Dynamic approach")
            else:
                raise Exception("Both static and dynamic approaches failed")
        await save_button.wait_for(state="visible", timeout=10000)
        await save_button.click()
        logger.info("[Gift Card] Clicked save/apply button")

        await page.wait_for_timeout(3000)
        logger.info("[Gift Card] Gift card flow completed successfully")

    except Exception as e:
        logger.info(f"[Gift Card] Error in gift card flow: {e}")
        raise
    return page


def _convert_to_frame_locator_syntax(base_locator: str, iframe_chain: list) -> str:
    """
    Convert a base locator and iframe chain to proper Playwright frameLocator syntax.
    
    Args:
        base_locator: The base locator (e.g., "page.getByText('Save')")
        iframe_chain: List of iframe XPaths
        
    Returns:
        Properly chained frameLocator syntax
    """
    if not iframe_chain or len(iframe_chain) == 0:
        return base_locator
    
    # Remove 'page.' prefix if present
    if base_locator.startswith('page.'):
        locator_part = base_locator[5:]  # Remove 'page.'
    else:
        locator_part = base_locator
    
    # Build frameLocator chain
    frame_chain = "page"
    for iframe_xpath in iframe_chain:
        frame_chain += f".frameLocator('xpath={iframe_xpath}')"
    
    # Combine frame chain with the final locator
    final_locator = f"{frame_chain}.{locator_part}"
    
    logger.info(f"🔗 [FRAMELOCATOR] Converted: {base_locator} + {iframe_chain} → {final_locator}")
    return final_locator


async def record_page_object_details(page_name, action, obj, test_data, failed_msg, input_data, content_for_recording,
                                     page,
                                     check_for_visible_element, xpath,
                                     condition, condition_value, playwrightLocator,current_df,original_step,NEW_WINDOW_DETECTED, iframe_chain=None):
    """
    Async version of record_page_object_details using only Locator methods
    No JavaScript evaluation needed - simpler and more reliable
    """
    
    # Import the dynamic content detection function
    from utils.locator_reuse import _contains_dynamic_content, _should_skip_locator_reuse
    
    # ✅ Skip storing navigation and API actions in database
    original_bdd_step = str(original_step.get('original_bdd_step', '')).strip()
    if _should_skip_locator_reuse(original_bdd_step):
        logger.info(f"🚫 DB-STORE: SKIP NAVIGATION/API | {original_bdd_step[:60]}... | Navigation/API actions not stored in DB")
        original_step['skip_db_storage'] = True
        
    # ✅ Skip storing dynamic content in database
    elif _contains_dynamic_content(original_bdd_step):
        logger.info(f"🚫 DB-STORE: SKIP DYNAMIC | {original_bdd_step[:60]}... | Contains dynamic content - not storing in DB")
        # Still create the DataFrame for current execution, but mark it to not be stored
        original_step['skip_db_storage'] = True

    # Get current URL
    get_url = page.url
    rows = []
    cols = []

    # Basic columns that are always present
    cols.append('url')
    cols.append('page')
    cols.append('action')
    cols.append('object')
    cols.append('data')
    cols.append('failure')
    cols.append('input')
    cols.append('original_bdd_step')
    cols.append('window_detected')
    cols.append('window_type')  # NEW: Track popup vs new_tab vs unknown

    # ✅ NEW: optional provenance flag (e.g., "js_crawl" or "db_reuse")
    cols.append('generated_by')  # harmless if the DB doesn’t have this column

    # Basic data that's always present
    rows.append(get_url)
    rows.append(page_name)
    rows.append(action)
    rows.append(obj)
    rows.append(test_data)
    rows.append(failed_msg)
    rows.append(input_data)
    if action != "switch to window":
        rows.append(str(original_step['original_bdd_step']))
        rows.append(NEW_WINDOW_DETECTED.is_detected)
        rows.append(NEW_WINDOW_DETECTED.window_type or '')  # NEW: Add window type
    else:
        rows.append('')
        rows.append('')
        rows.append('')  # NEW: Empty window_type for switch actions

    # ✅ NEW: write whatever the caller set on original_step (or blank)
    rows.append(original_step.get('generated_by', '') or '')

    # Handle different action types
    if action.lower() == 'navigate':
        get_all_attrib_dict = ''
        cols.append('TagName')
        rows.append('')
        cols.append('ID')
        rows.append('')
        cols.append('Class')
        rows.append('')
        cols.append('Name')
        rows.append('')
        cols.append('LinkText')
        rows.append('')
        cols.append('CssSelector_with_Class')
        rows.append('')
        cols.append('CssSelector_with_ID')
        rows.append('')
        cols.append('All_Attributes')
        rows.append(get_all_attrib_dict)
        cols.append('Xpath')
        rows.append(xpath)
        cols.append('RelXpath')
        rows.append('')
        cols.append('playwrightLocator')
        rows.append('')
        cols.append('Locator')
        rows.append('')
        cols.append('Condition')
        rows.append('')
        cols.append('Condition_Value')
        rows.append('')

    elif action.lower() == 'switch_to_iframe':
        get_all_attrib_dict = ''
        cols.append('TagName')
        rows.append('')
        cols.append('ID')
        rows.append(xpath)
        cols.append('Class')
        rows.append('')
        cols.append('Name')
        rows.append('')
        cols.append('LinkText')
        rows.append('')
        cols.append('CssSelector_with_Class')
        rows.append('')
        cols.append('CssSelector_with_ID')
        rows.append('')
        cols.append('All_Attributes')
        rows.append(get_all_attrib_dict)
        cols.append('Xpath')
        rows.append(xpath)
        cols.append('playwrightLocator')
        rows.append('')
        cols.append('Condition')
        rows.append('')
        cols.append('Condition_Value')
        rows.append('')

    elif action.lower() in ('validate', 'verify'):
        get_all_attrib_dict = ''
        cols.append('TagName')
        rows.append('')
        cols.append('ID')
        rows.append('')
        cols.append('Class')
        rows.append('')
        cols.append('Name')
        rows.append('')
        cols.append('LinkText')
        rows.append('')
        cols.append('CssSelector_with_Class')
        rows.append('')
        cols.append('CssSelector_with_ID')
        rows.append('')
        cols.append('All_Attributes')
        rows.append(get_all_attrib_dict)
        cols.append('Xpath')
        rows.append(xpath)
        cols.append('RelXpath')
        rows.append('')
        cols.append('playwrightLocator')
        rows.append('')
        cols.append('Locator')
        rows.append('')
        cols.append('Condition')
        rows.append(condition)
        cols.append('Condition_Value')
        rows.append(condition_value)

    elif action.lower() == 'switch to window':
        get_all_attrib_dict = ''
        cols.append('TagName')
        rows.append('')
        cols.append('ID')
        rows.append(xpath)
        cols.append('Class')
        rows.append('')
        cols.append('Name')
        rows.append('')
        cols.append('LinkText')
        rows.append('')
        cols.append('CssSelector_with_Class')
        rows.append('')
        cols.append('CssSelector_with_ID')
        rows.append('')
        cols.append('All_Attributes')
        rows.append(get_all_attrib_dict)
        cols.append('Xpath')
        rows.append('')
        cols.append('playwrightLocator')
        rows.append('')
        cols.append('Condition')
        rows.append('')
        cols.append('Condition_Value')
        rows.append('')

    else:
        # Handle regular actions (click, enter, hover, etc.)
        if check_for_visible_element:
            logger.info(f"🔍 Getting element attributes for {action} action...")
            try:
                # ✅ Use only Locator methods - no JavaScript evaluation needed
                tag_name = await check_for_visible_element.get_attribute('tagName') or ''
                element_id = await check_for_visible_element.get_attribute('id') or ''
                element_class = await check_for_visible_element.get_attribute('class') or ''
                element_name = await check_for_visible_element.get_attribute('name') or ''
                element_title = await check_for_visible_element.get_attribute('title') or ''
                element_type = await check_for_visible_element.get_attribute('type') or ''
                element_value = await check_for_visible_element.get_attribute('value') or ''
                element_placeholder = await check_for_visible_element.get_attribute('placeholder') or ''
                element_aria_label = await check_for_visible_element.get_attribute('aria-label') or ''

                logger.info(f"✅ Element attributes - Tag: {tag_name}, ID: {element_id}, Class: {element_class}")

                APP_ID = PLATFORM_CONFIG.get("APP_ID")

                def _sanitize_token(s: str) -> str:
                    s = (s or "").strip()
                    s = re.sub(r'[^A-Za-z0-9_]+', '_', s)
                    s = re.sub(r'_{2,}', '_', s).strip('_')
                    return s

                _step_name = _sanitize_token(str(original_step.get('original_bdd_step', '')))
                _page_label = _sanitize_token(str(page_name))
                _aria_token = _sanitize_token(element_aria_label) if element_aria_label else ''

                if _aria_token:
                    unique_id = f"{APP_ID}_{_step_name}_{_page_label}_{_aria_token}"
                else:
                    unique_id = f"{APP_ID}_{_step_name}_{_page_label}"

                # ✅ Build attributes dictionary manually (no JavaScript needed)
                get_all_attrib_dict = {
                    'tagName': tag_name,
                    'id': element_id,
                    'class': element_class,
                    'name': element_name,
                    'title': element_title,
                    'type': element_type,
                    'value': element_value,
                    'placeholder': element_placeholder,
                    'aria-label': element_aria_label,
                    'unique_id': unique_id
                }

                # Remove empty attributes to keep dict clean
                get_all_attrib_dict = {k: v for k, v in get_all_attrib_dict.items() if v}

                logger.info(f"✅ All attributes collected: {get_all_attrib_dict}")

                # ✅ add into All_Attributes if it's a dict
                if isinstance(get_all_attrib_dict, dict):
                    get_all_attrib_dict['unique_id'] = unique_id

                # ✅ append as the LAST column (no breaking existing consumers)
                cols.append('unique_id')
                rows.append(unique_id)

            except Exception as attr_error:
                logger.info(f"⚠️ Error getting element attributes: {attr_error}")
                # Use fallback empty values
                tag_name = element_id = element_class = ''
                get_all_attrib_dict = {}

            # Add element attribute columns and data
            cols.append('TagName')
            rows.append(tag_name)
            cols.append('ID')
            rows.append(element_id)
            cols.append('Class')
            rows.append(element_class)
            cols.append('Name')
            rows.append('')  # Keep empty as in original
            cols.append('LinkText')
            rows.append('')  # Keep empty as in original
            cols.append('CssSelector_with_Class')
            rows.append(element_class)
            cols.append('CssSelector_with_ID')
            rows.append(element_id)
            cols.append('All_Attributes')
            rows.append(get_all_attrib_dict)
        else:
            logger.info("⚠️ No element provided for attribute extraction")
            # Handle case when no element is provided
            cols.append('TagName')
            rows.append('')
            cols.append('ID')
            rows.append('')
            cols.append('Class')
            rows.append('')
            cols.append('Name')
            rows.append('')
            cols.append('LinkText')
            rows.append('')
            cols.append('CssSelector_with_Class')
            rows.append('')
            cols.append('CssSelector_with_ID')
            rows.append('')
            cols.append('All_Attributes')
            rows.append({})

        # Add remaining columns
        cols.append('Xpath')
        rows.append(xpath)
        cols.append('RelXpath')
        rows.append('')  # Keep empty as requested
        cols.append('playwrightLocator')
        rows.append('')  # Will be filled below
        cols.append('Locator')
        rows.append('')  # Keep empty as in original
        cols.append('Condition')
        rows.append('')  # Keep empty for regular actions
        cols.append('Condition_Value')
        rows.append('')  # Keep empty for regular actions

    # Create DataFrame
    logger.info("📊 Creating DataFrame...")
    df = pd.DataFrame(columns=cols)
    df.loc[len(df)] = rows
    current_df = pd.concat([current_df, df], ignore_index=True)

    # ✅ Generate playwright locator for applicable actions (but skip if already provided or if element is None)
    if action.lower() not in ['navigate', 'switch_to_iframe']:
        # Check if locator is already provided in the playwrightLocator parameter (e.g., from DB reuse)
        if playwrightLocator and playwrightLocator.strip():
            # Use provided locator (from DB reuse or other sources)
            playwright_Locator = playwrightLocator
            logger.info(f"✅ Using provided playwright locator: {playwright_Locator}")
        elif check_for_visible_element is not None:
            # Only generate locator if we have a valid element
            logger.info("🔧 Generating Playwright locator...")

            try:
                playwright_Locator = await generate_playwright_locator(content_for_recording, check_for_visible_element, xpath)
                
                # Convert iframe chain to frameLocator syntax if we're in iframe context
                logger.info(f"🔍 [IFRAME DEBUG] iframe_chain parameter: {iframe_chain}")
                if iframe_chain and len(iframe_chain) > 0:
                    # Convert to frameLocator chain syntax
                    playwright_Locator = _convert_to_frame_locator_syntax(playwright_Locator, iframe_chain)
                    logger.info(f"🔗 [IFRAME DEBUG] Converted to frameLocator syntax")
                else:
                    logger.info(f"🔍 [IFRAME DEBUG] No iframe chain to embed")
                    
                logger.info(f"✅ Generated playwright locator: {playwright_Locator}")

            except FunctionTimedOut:
                logger.info("⚠️ Playwright locator generation timed out, using fallback")
                # Build proper iframe frameLocator fallback
                logger.info(f"🔍 [IFRAME DEBUG] (timeout fallback) iframe_chain parameter: {iframe_chain}")
                
                # Validate xpath before using it
                if not xpath or not xpath.strip():
                    logger.warning("⚠️ Empty xpath in timeout fallback - using generic fallback")
                    playwright_Locator = "page.locator('body')"
                else:
                    if iframe_chain and len(iframe_chain) > 0:
                        # Convert to frameLocator chain syntax
                        base_locator = f"page.locator('xpath={xpath}')"
                        playwright_Locator = _convert_to_frame_locator_syntax(base_locator, iframe_chain)
                        logger.info(f"🔗 [IFRAME DEBUG] (timeout fallback) Converted to frameLocator syntax")
                    else:
                        playwright_Locator = f"page.locator('xpath={xpath}')"
                        logger.info(f"🔍 [IFRAME DEBUG] (timeout fallback) No iframe chain to embed")

            except Exception as locator_error:
                logger.info(f"⚠️ Error generating playwright locator: {locator_error}")
                # Build proper iframe frameLocator fallback
                logger.info(f"🔍 [IFRAME DEBUG] (error fallback) iframe_chain parameter: {iframe_chain}")
                
                # Validate xpath before using it
                if not xpath or not xpath.strip():
                    logger.warning("⚠️ Empty xpath in error fallback - using generic fallback")
                    playwright_Locator = "page.locator('body')"
                else:
                    if iframe_chain and len(iframe_chain) > 0:
                        # Convert to frameLocator chain syntax
                        base_locator = f"page.locator('xpath={xpath}')"
                        playwright_Locator = _convert_to_frame_locator_syntax(base_locator, iframe_chain)
                        logger.info(f"🔗 [IFRAME DEBUG] (error fallback) Converted to frameLocator syntax")
                    else:
                        playwright_Locator = f"page.locator('xpath={xpath}')"
                        logger.info(f"🔍 [IFRAME DEBUG] (error fallback) No iframe chain to embed")
        else:
            # No element and no provided locator - use xpath fallback
            logger.info("⚠️ No element or provided locator - using xpath fallback")
            logger.info(f"🔍 [IFRAME DEBUG] (no element fallback) iframe_chain parameter: {iframe_chain}")
            
            # Validate xpath before using it
            if not xpath or not xpath.strip():
                logger.warning("⚠️ Empty xpath provided - using generic fallback")
                playwright_Locator = "page.locator('body')"  # Safe fallback
            else:
                if iframe_chain and len(iframe_chain) > 0:
                    # Convert to frameLocator chain syntax
                    base_locator = f"page.locator('xpath={xpath}')"
                    playwright_Locator = _convert_to_frame_locator_syntax(base_locator, iframe_chain)
                    logger.info(f"🔗 [IFRAME DEBUG] (no element fallback) Converted to frameLocator syntax")
                else:
                    playwright_Locator = f"page.locator('xpath={xpath}')"
                    logger.info(f"🔍 [IFRAME DEBUG] (no element fallback) No iframe chain to embed")

        # Set the generated locator in DataFrame
        df['playwrightLocator'] = str(playwright_Locator)
        df['Locator'] = None
        df['RelXpath'] = None

        current_df.iloc[-1, current_df.columns.get_loc('playwrightLocator')] = str(playwright_Locator)

    return current_df



def extract_page_name(url):
    parsed_url = urlparse(url)
    if parsed_url.path:
        page_name = re.sub(r'\W+', '_', parsed_url.path.split('/')[-1])
        if page_name:
            return page_name
        else:
            return "landing_page"
    return "landing_page"


import asyncio
import re
import time


async def generate_playwright_locator(context, target_locator, abs_xpath, timeout_seconds=15):
    """
    🎯 Complete Clean Playwright Locator Generator with getByRole Strategy
    """
    import re
    import time
    from playwright.async_api import Page

    start_time = time.time()
    debug_info = []

    def debug_log(message):
        debug_info.append(f"[{time.time() - start_time:.2f}s] {message}")
        print(f"[{time.time() - start_time:.2f}s] {message}")

    def check_timeout():
        return (time.time() - start_time) > timeout_seconds

    # Context detection 
    is_page_context = isinstance(context, Page)
    context_name = "page" if is_page_context else "page"  # Always use "page" to avoid current_frame

    debug_log(f"🎯 Context type: {'Page' if is_page_context else 'FrameLocator'}")
    debug_log(f"🔗 Context name: {context_name}")

    def get_fallback_locator():
        return f"{context_name}.locator('xpath={abs_xpath}')"

    # ✅ CLEAN ELEMENT EXTRACTION with Object Mapping
    async def extract_comprehensive_element_data(target_loc):
        debug_log("🔍 Starting element data extraction...")
        try:
            data = await target_loc.evaluate("""
            (el) => {
                try {
                    const attrs = {};
                    const allAttrs = [
                        'data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id',
                        'id', 'name', 'class', 'placeholder', 'aria-label', 'title', 'alt', 'type', 'role', 'href', 'value'
                    ];

                    allAttrs.forEach(attr => {
                        const val = el.getAttribute(attr);
                        if (val && val.trim()) attrs[attr] = val.trim();
                    });

                    // ✅ ENHANCED ROLE DETECTION - Handles input types properly
                    const getElementRole = (el) => {
                        // 1. Explicit role attribute takes priority
                        const explicitRole = el.getAttribute('role');
                        if (explicitRole) return explicitRole;

                        // 2. Handle input elements by type
                        const tagName = el.tagName.toLowerCase();
                        if (tagName === 'input') {
                            const inputType = el.getAttribute('type') || 'text';
                            const inputRoleMap = {
                                'text': 'textbox',
                                'email': 'textbox',
                                'password': 'textbox',
                                'url': 'textbox',
                                'tel': 'textbox',
                                'search': 'searchbox',
                                'button': 'button',
                                'submit': 'button',
                                'reset': 'button',
                                'checkbox': 'checkbox',
                                'radio': 'radio',
                                'range': 'slider',
                                'number': 'spinbutton'
                            };
                            return inputRoleMap[inputType] || 'textbox';
                        }

                        // 3. Handle other HTML elements
                        const generalRoleMap = {
                            'button': 'button',
                            'a': 'link',
                            'select': 'combobox',
                            'textarea': 'textbox',
                            'form': 'form',
                            'img': 'img',
                            'nav': 'navigation',
                            'header': 'banner',
                            'footer': 'contentinfo'
                        };

                        return generalRoleMap[tagName] || null;
                    };

                    const role = getElementRole(el);
                    const text = el.textContent?.trim() || '';

                    // Add this helper function first
function getAssociatedLabelText(el, attrs) {
    try {
        // Check for label with 'for' attribute
        if (attrs.id) {
            const label = document.querySelector(`label[for="${attrs.id}"]`);
            if (label?.textContent?.trim()) {
                return label.textContent.trim();
            }
        }

        // Check for wrapping label
        const parentLabel = el.closest('label');
        if (parentLabel?.textContent?.trim()) {
            return parentLabel.textContent.trim();
        }

        return null;
    } catch {
        return null;
    }
}

// Then replace your existing accessibleNames array with this:
const accessibleNames = [
    attrs['aria-label'],
    getAssociatedLabelText(el, attrs), // This will catch most form labels
    text && text.length <= 50 && text.length >= 2 ? text : null,
    (el.tagName.toLowerCase() === 'img') ? attrs.alt : null,
    (['button', 'submit', 'reset'].includes(attrs.type)) ? attrs.value : null,
    attrs.title && attrs.title.length <= 40 ? attrs.title : null,
    (['input', 'textarea'].includes(el.tagName.toLowerCase())) ? attrs.placeholder : null
].filter(name => name && name.trim().length >= 1);


// ✅ SUPER SIMPLE: Find closest parent with any testid
        // 🎯 NEW 3-LEVEL + CLOSEST STRATEGY
                const findClosestTestIdParent = (element) => {
                    const testAttributes = ['data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id'];

                    // 🎯 LEVEL 1-3: Check direct ancestors first
                    let currentElement = element.parentElement;

                    for (let level = 1; level <= 3; level++) {
                        if (!currentElement) break;

                        for (let attr of testAttributes) {
                            const testIdValue = currentElement.getAttribute(attr);
                            if (testIdValue && testIdValue.trim()) {
                                return {
                                    attribute: attr,
                                    value: testIdValue.trim(),
                                    strategy: `direct-level-\${level}`,
                                    level: level
                                };
                            }
                        }

                        currentElement = currentElement.parentElement;
                    }

                    // 🎯 FALLBACK: Use closest() if levels 1-3 failed
                    for (let attr of testAttributes) {
                        const parentWithTestId = element.closest('[' + attr + ']');
                        if (parentWithTestId && parentWithTestId !== element) {
                            return {
                                attribute: attr,
                                value: parentWithTestId.getAttribute(attr),
                                strategy: 'closest-fallback',
                                level: 'fallback'
                            };
                        }
                    }

                    return null;
                };

                    return {
                        tag: el.tagName.toLowerCase(),
                        text: text,
                        attrs: attrs,
                        role: role,
                        accessibleNames: [...new Set(accessibleNames)],
                        position: {
                            x: Math.round(el.getBoundingClientRect().x),
                            y: Math.round(el.getBoundingClientRect().y),
                            w: Math.round(el.getBoundingClientRect().width),
                            h: Math.round(el.getBoundingClientRect().height)
                        },
                        closestTestIdParent: findClosestTestIdParent(el)
                    };
                } catch(e) {
                    return {error: e.message};
                }
            }
            """, timeout=3000)

            if data and data.get('error'):
                debug_log(f"❌ Element data extraction failed: {data['error']}")
                return None

            debug_log(f"✅ Element extracted:")
            debug_log(f"   Tag: {data.get('tag')}")
            debug_log(f"   Role: {data.get('role')}")
            debug_log(f"   Text: '{data.get('text', '')[:30]}{'...' if len(data.get('text', '')) > 30 else ''}'")
            debug_log(f"   Attributes: {list(data.get('attrs', {}).keys())}")
            debug_log(f"   Accessible Names: {data.get('accessibleNames', [])}")

            return data

        except Exception as e:
            debug_log(f"❌ Element data extraction exception: {str(e)}")
            return None

    # ✅ YOUR EXACT VALIDATION FUNCTION
    async def simple_validation(test_locator, target_position, strategy_name):
        """Simple 3-step validation: Count → Position → Success"""
        try:
            debug_log(f"🔍 Validating {strategy_name}...")

            # Step 1: Count check
            count = await test_locator.count()
            debug_log(f"   {strategy_name}: Found {count} elements")

            if count == 0:
                debug_log(f"❌ {strategy_name}: No elements")
                return False

                # 🎯 SPECIAL HANDLING FOR getByText STRATEGY
            if "Text(" in strategy_name:  # Detect getByText strategy
                debug_log(f"   🎯 Detected getByText strategy - using nth() logic")

                if count >= 4:
                    debug_log(f"❌ {strategy_name}: Too many elements ({count}) for nth() check")
                    return False

                # Check each element's position for getByText
                for i in range(count):
                    debug_log(f"   🔍 Checking element {i + 1}/{count}")
                    try:
                        nth_element = test_locator.nth(i)
                        found_pos = await nth_element.evaluate("""
                            (el) => ({
                                x: Math.round(el.getBoundingClientRect().x),
                                y: Math.round(el.getBoundingClientRect().y),
                                w: Math.round(el.getBoundingClientRect().width),
                                h: Math.round(el.getBoundingClientRect().height)
                            })
                            """, timeout=2000)

                        pos_diff_x = abs(found_pos['x'] - target_position['x'])
                        pos_diff_y = abs(found_pos['y'] - target_position['y'])

                        debug_log(f"     Element {i + 1}: Position diff - X: {pos_diff_x}px, Y: {pos_diff_y}px")

                        if pos_diff_x <= 20 and pos_diff_y <= 20:
                            debug_log(f"✅ {strategy_name}: Found matching element at index {i}")
                            return {'success': True, 'nth_index': i, 'count': count}  # Return index info

                    except Exception as e:
                        debug_log(f"❌ Error checking element {i + 1}: {str(e)}")
                        continue

                debug_log(f"❌ {strategy_name}: No element found at correct position")
                return False
            elif count > 1:
                debug_log(f"❌ {strategy_name}: Too many elements ({count})")
                return False

            # Step 2: Position check
            try:
                found_pos = await test_locator.first.evaluate("""
                (el) => ({
                    x: Math.round(el.getBoundingClientRect().x),
                    y: Math.round(el.getBoundingClientRect().y),
                    w: Math.round(el.getBoundingClientRect().width),
                    h: Math.round(el.getBoundingClientRect().height)
                })
                """, timeout=2000)

                pos_diff_x = abs(found_pos['x'] - target_position['x'])
                pos_diff_y = abs(found_pos['y'] - target_position['y'])

                debug_log(f"   {strategy_name}: Position diff - X: {pos_diff_x}px, Y: {pos_diff_y}px")

                if pos_diff_x <= 20 and pos_diff_y <= 20:
                    debug_log(f"✅ {strategy_name}: Validation passed!")
                    return True
                else:
                    debug_log(f"❌ {strategy_name}: Position difference too large")
                    return False

            except Exception as e:
                debug_log(f"❌ {strategy_name}: Position check failed: {str(e)}")
                return False

        except Exception as e:
            debug_log(f"❌ {strategy_name}: Validation failed: {str(e)}")
            return False

    def escape_quotes(text):
        """Helper to escape quotes in strings"""
        return str(text).replace("'", "\\'").replace('"', '\\"') if text else ""

    # ✅ ENHANCED STATIC ID DETECTION
    def is_static_id(id_value):
        """Enhanced dynamic ID detection"""
        if not id_value or len(id_value) < 2:
            return False

        dynamic_patterns = [r'\d']

        id_lower = id_value.lower()
        for pattern in dynamic_patterns:
            if re.search(pattern, id_lower):
                debug_log(f"   ID '{id_value}' detected as DYNAMIC")
                return False

        debug_log(f"   ID '{id_value}' detected as STATIC")
        return True

    # 🎯 STRATEGY 1: getByTestId
    async def try_testid_strategy(element_data):
        """🥇 Strategy 1: Enhanced getByTestId with attribute-aware selection"""
        debug_log("🚀 STRATEGY 1: Testing Enhanced getByTestId...")

        test_attributes = ['data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id']
        attrs = element_data.get('attrs', {})

        for attr in test_attributes:
            testid_value = attrs.get(attr)
            if testid_value:
                debug_log(f"   🔍 Found {attr}: '{testid_value}'")
                try:
                    # 🎯 Smart method selection
                    if attr == 'data-testid':
                        if is_page_context:
                            loc = context.get_by_test_id(testid_value)
                            result = f"page.getByTestId('{escape_quotes(testid_value)}')"
                        else:
                            loc = context.get_by_test_id(testid_value)
                            result = f"current_frame.getByTestId('{escape_quotes(testid_value)}')"
                    else:
                        if is_page_context:
                            loc = context.locator(f'[{attr}="{testid_value}"]')
                            result = f"page.locator('[{attr}=\"{escape_quotes(testid_value)}\"]')"
                        else:
                            loc = context.locator(f'[{attr}="{testid_value}"]')
                            result = f"current_frame.locator('[{attr}=\"{escape_quotes(testid_value)}\"]')"

                    if await simple_validation(loc, element_data['position'], f"TestID({testid_value})"):
                        debug_log(f"🏆 Enhanced getByTestId SUCCESS: {result}")
                        return result

                except Exception as e:
                    debug_log(f"❌ Enhanced getByTestId error for {testid_value}: {str(e)}")

        debug_log("❌ No working getByTestId found")
        return None

    # 🎯 STRATEGY 2: Static ID
    async def try_static_id_strategy(element_data):
        """🥈 Strategy 2: Static ID locator"""
        debug_log("🚀 STRATEGY 2: Testing Static ID...")

        attrs = element_data.get('attrs', {})
        id_val = attrs.get('id')

        if not id_val:
            debug_log("❌ No ID attribute found")
            return None

        debug_log(f"   🔍 Found ID: '{id_val}'")

        if not is_static_id(id_val):
            debug_log(f"❌ ID '{id_val}' is dynamic - skipping")
            return None

        try:
            if is_page_context:
                loc = context.locator(f'#{id_val}')
                result = f"page.locator('#{escape_quotes(id_val)}')"
            else:
                loc = context.locator(f'#{id_val}')
                result = f"current_frame.locator('#{escape_quotes(id_val)}')"

            if await simple_validation(loc, element_data['position'], f"StaticID({id_val})"):
                debug_log(f"🏆 Static ID SUCCESS: {result}")
                return result

        except Exception as e:
            debug_log(f"❌ Static ID error for {id_val}: {str(e)}")

        return None

    async def try_placeholder_strategy(element_data):
        """🎯 Strategy: getByPlaceholder for form inputs"""
        debug_log("🚀 STRATEGY: Testing getByPlaceholder...")

        # 1. Check if element has placeholder attribute
        attrs = element_data.get('attrs', {})
        placeholder_text = attrs.get('placeholder', '').strip()

        if not placeholder_text:
            debug_log("❌ No placeholder attribute found")
            return None

        # 2. Normalize placeholder text
        def normalize_placeholder_text(text):
            # Remove extra whitespace and normalize
            normalized = ' '.join(text.split())
            # Remove newlines/tabs
            normalized = normalized.replace('\n', ' ').replace('\t', ' ')
            return normalized.strip()

        placeholder_text = normalize_placeholder_text(placeholder_text)
        debug_log(f"   📝 Found placeholder: '{placeholder_text}'")

        # 3. Validate placeholder is good for targeting
        def is_good_placeholder_text(text):
            # Too short (avoid "...", "?", etc.)
            if len(text) < 1:
                debug_log(f"❌ Placeholder too short: '{text}'")
                return False

            # Too long (avoid paragraphs)
            if len(text) > 100:
                debug_log(f"❌ Placeholder too long: {len(text)} chars")
                return False

            # # Check for dynamic patterns (IDs, timestamps, etc.)
            # dynamic_patterns = [
            #     r'\d{4}-\d{2}-\d{2}',  # Dates
            #     r'ID:\s*\d+',  # IDs
            #     r'\\$\d+\.\d{2}',  # Prices
            #     r'\d{1,2}:\d{2}',  # Times
            #     r'#\d+',  # Hash numbers
            #     r'\b\d{10,}\b',  # Long numbers
            #     r'uid[_-]?\d+',  # UIDs
            # ]
            #
            # for pattern in dynamic_patterns:
            #     if re.search(pattern, text):
            #         debug_log(f"❌ Placeholder contains dynamic content: '{text}'")
            #         return False

            # # Avoid overly generic placeholders
            # generic_placeholders = [
            #     'text', 'input', 'enter', 'type', 'click', 'here', 'value',
            #     '...', '???', 'placeholder', 'default', 'example'
            # ]
            #
            # text_lower = text.lower().strip()
            # if text_lower in generic_placeholders:
            #     debug_log(f"❌ Placeholder too generic: '{text}'")
            #     return False

            # # Avoid very generic single words
            # if len(text.split()) == 1 and len(text) < 5:
            #     debug_log(f"❌ Placeholder too generic (short single word): '{text}'")
            #     return False

            return True

        if not is_good_placeholder_text(placeholder_text):
            debug_log(f"❌ Placeholder not suitable for targeting: '{placeholder_text}'")
            return None

        # 4. Try both exact and partial matching (exact first)
        for exact_mode in [True, False]:
            match_type = "EXACT" if exact_mode else "PARTIAL"
            debug_log(f"   🔍 Trying {match_type} placeholder match: '{placeholder_text}'")

            try:
                # Create locator based on context and exact mode
                if is_page_context:
                    if exact_mode:
                        loc = context.get_by_placeholder(placeholder_text, exact=True)
                        result = f"page.getByPlaceholder('{escape_quotes(placeholder_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_placeholder(placeholder_text)
                        result = f"page.getByPlaceholder('{escape_quotes(placeholder_text)}')"
                else:
                    if exact_mode:
                        loc = context.get_by_placeholder(placeholder_text, exact=True)
                        result = f"current_frame.getByPlaceholder('{escape_quotes(placeholder_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_placeholder(placeholder_text)
                        result = f"current_frame.getByPlaceholder('{escape_quotes(placeholder_text)}')"

                # Validate the locator works
                if await simple_validation(loc, element_data['position'],
                                           f"Placeholder({match_type}): {placeholder_text[:20]}"):
                    debug_log(f"🏆 getByPlaceholder {match_type} SUCCESS: {result}")
                    return result
                else:
                    debug_log(f"❌ getByPlaceholder {match_type} validation failed")

            except Exception as e:
                debug_log(f"❌ getByPlaceholder {match_type} error: {str(e)}")

        debug_log("❌ No working getByPlaceholder strategy found")
        return None

    # 🎯 NEW STRATEGY 3: getByRole with Accessible Names
    async def try_role_strategy(element_data):
        """
        🥉 Strategy 3: getByRole with accessible names
        Tests different roles (button, link, textbox, etc.) with accessible names
        """
        debug_log("🚀 STRATEGY 3: Testing getByRole with accessible names...")

        role = element_data.get('role')
        accessible_names = element_data.get('accessibleNames', [])

        if not role:
            debug_log("❌ No role detected for element")
            return None

        if not accessible_names:
            debug_log("❌ No accessible names found for getByRole")
            return None

        debug_log(f"   🎭 Element role: '{role}'")
        debug_log(f"   🏷️ Accessible names: {accessible_names}")

        # Test each accessible name with the role
        for name in accessible_names[:3]:  # Try up to 3 names to avoid too many attempts
            debug_log(f"   🔍 Testing role '{role}' with name '{name}'")
            try:
                # Create locator based on context
                if is_page_context:
                    loc = context.get_by_role(role, name=name)
                    result = f"page.getByRole('{role}', {{name: '{escape_quotes(name)}'}})"
                else:
                    loc = context.get_by_role(role, name=name)
                    result = f"current_frame.getByRole('{role}', {{name: '{escape_quotes(name)}'}})"

                # Validate the locator
                if await simple_validation(loc, element_data['position'], f"Role({role})+Name({name[:20]})"):
                    debug_log(f"🏆 getByRole SUCCESS: {result}")
                    return result
                else:
                    debug_log(f"❌ getByRole validation failed for role '{role}' with name '{name}'")

            except Exception as e:
                debug_log(f"❌ getByRole error for role '{role}' with name '{name}': {str(e)}")

        debug_log(f"❌ No working getByRole strategy found for role '{role}'")
        return None

    async def try_text_strategy(element_data):
        """🎯 Strategy: getByText with exact match first, then partial"""
        debug_log("🚀 STRATEGY: Testing getByText...")

        # 1. Check if element has suitable text content
        element_text = element_data.get('text', '').strip()
        if not element_text:
            debug_log("❌ No text content found")
            return None

        # 2. Normalize text (remove extra whitespace, newlines)
        def normalize_text(text):
            # Remove extra whitespace and normalize
            normalized = ' '.join(text.split())
            # Remove newlines/tabs
            normalized = normalized.replace('\n', ' ').replace('\t', ' ')
            return normalized.strip()

        element_text = normalize_text(element_text)
        debug_log(f"   📝 Found text: '{element_text}'")

        # 3. Validate text is good for getByText targeting
        def is_good_text_for_targeting(text):
            # Too short (avoid "OK", "Go", "Hi")
            if len(text) < 1:
                debug_log(f"❌ Text too short: '{text}'")
                return False

            # Too long (avoid paragraphs, long descriptions)
            if len(text) > 30:
                debug_log(f"❌ Text too long: {len(text)} chars")
                return False

            return True

        if not is_good_text_for_targeting(element_text):
            debug_log(f"❌ Text not suitable for targeting: '{element_text}'")
            return None

        # 4. Try both exact and partial matching (exact first for precision)
        for exact_mode in [True, False]:
            match_type = "EXACT" if exact_mode else "PARTIAL"
            debug_log(f"   🔍 Trying {match_type} text match: '{element_text}'")

            try:
                # Create base locator
                if is_page_context:
                    if exact_mode:
                        loc = context.get_by_text(element_text, exact=True)
                        base_result = f"page.getByText('{escape_quotes(element_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_text(element_text)
                        base_result = f"page.getByText('{escape_quotes(element_text)}')"
                else:
                    if exact_mode:
                        loc = context.get_by_text(element_text, exact=True)
                        base_result = f"current_frame.getByText('{escape_quotes(element_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_text(element_text)
                        base_result = f"current_frame.getByText('{escape_quotes(element_text)}')"

                # 🎯 Use enhanced validation
                validation_result = await simple_validation(loc, element_data['position'],
                                                            f"Text({match_type}): {element_text[:20]}")

                if validation_result:
                    if isinstance(validation_result, dict):
                        # getByText returned nth info
                        nth_index = validation_result['nth_index']
                        count = validation_result['count']

                        if count == 1:
                            result = base_result  # Single element, no nth needed
                        else:
                            result = base_result + f".nth({nth_index})"  # Add nth()

                        debug_log(f"🏆 getByText {match_type} SUCCESS: {result}")
                        return result
                    else:
                        # Standard validation (shouldn't happen for getByText, but just in case)
                        debug_log(f"🏆 getByText {match_type} SUCCESS: {base_result}")
                        return base_result

            except Exception as e:
                debug_log(f"❌ getByText {match_type} error: {str(e)}")

        debug_log("❌ No working getByText strategy found")
        return None

    async def try_testid_role_combinations(element_data):
        """🚀 Strategy: TestID + Role with attribute-aware selection"""

        closest_parent = element_data.get('closestTestIdParent')
        role = element_data.get('role')
        accessible_names = element_data.get('accessibleNames', [])

        if not closest_parent or not role:
            return None

        # 🎯 Get EXACTLY what we discovered
        test_attribute = closest_parent['attribute']  # 'data-test-id' or 'data-testid'
        testid_value = closest_parent['value']  # 'addToCartButton'

        debug_log(f"   🎯 Using discovered attribute: {test_attribute}='{testid_value}'")

        # Try simple combination first
        try:
            if test_attribute == 'data-testid':
                # Use Playwright's getByTestId() for standard data-testid
                if is_page_context:
                    loc = context.get_by_test_id(testid_value).get_by_role(role)
                    result = f"page.getByTestId('{escape_quotes(testid_value)}').getByRole('{role}')"
                else:
                    loc = context.get_by_test_id(testid_value).get_by_role(role)
                    result = f"current_frame.getByTestId('{escape_quotes(testid_value)}').getByRole('{role}')"
            else:
                # Use CSS selector for non-standard attributes
                if is_page_context:
                    loc = context.locator(f'[{test_attribute}="{testid_value}"]').get_by_role(role)
                    result = f"page.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').getByRole('{role}')"
                else:
                    loc = context.locator(f'[{test_attribute}="{testid_value}"]').get_by_role(role)
                    result = f"current_frame.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').getByRole('{role}')"

            if await simple_validation(loc, element_data['position'], f"TestID({testid_value})+Role"):
                debug_log(f"🏆 TestID+Role SUCCESS: {result}")
                return result

        except Exception as e:
            debug_log(f"❌ TestID+Role simple combination error: {str(e)}")

        # Try with accessible names if simple fails
        for name in accessible_names[:2]:
            try:
                if test_attribute == 'data-testid':
                    if is_page_context:
                        loc = context.get_by_test_id(testid_value).get_by_role(role, name=name)
                        result = f"page.getByTestId('{escape_quotes(testid_value)}').getByRole('{role}', {{name: '{escape_quotes(name)}'}})"
                    else:
                        loc = context.get_by_test_id(testid_value).get_by_role(role, name=name)
                        result = f"current_frame.getByTestId('{escape_quotes(testid_value)}').getByRole('{role}', {{name: '{escape_quotes(name)}'}})"
                else:
                    if is_page_context:
                        loc = context.locator(f'[{test_attribute}="{testid_value}"]').get_by_role(role, name=name)
                        result = f"page.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').getByRole('{role}', {{name: '{escape_quotes(name)}'}})"
                    else:
                        loc = context.locator(f'[{test_attribute}="{testid_value}"]').get_by_role(role, name=name)
                        result = f"current_frame.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').getByRole('{role}', {{name: '{escape_quotes(name)}'}})"

                if await simple_validation(loc, element_data['position'], f"TestID({testid_value})+Role+Name"):
                    debug_log(f"🏆 TestID+Role+Name SUCCESS: {result}")
                    return result
            except Exception as e:
                debug_log(f"❌ TestID+Role+Name error for name '{name}': {str(e)}")

        return None

    async def try_testid_css_selector_combination(element_data):
        """TestID + Simple CSS Selector strategy with attribute-aware selection"""

        closest_parent = element_data.get('closestTestIdParent')
        target_tag = element_data.get('tag')  # 'span', 'button', etc.

        if not closest_parent or not target_tag:
            return None

        # 🎯 Get EXACTLY what we discovered
        test_attribute = closest_parent['attribute']  # 'data-test-id' or 'data-testid'
        testid_value = closest_parent['value']  # 'addToCartButton'

        debug_log(f"   🎯 Using discovered attribute: {test_attribute}='{testid_value}'")

        try:
            # 🎯 Smart method selection based on discovered attribute
            if test_attribute == 'data-testid':
                # Use Playwright's getByTestId() for standard data-testid
                if is_page_context:
                    loc = context.get_by_test_id(testid_value).locator(target_tag)
                    result = f"page.getByTestId('{escape_quotes(testid_value)}').locator('{target_tag}')"
                else:
                    loc = context.get_by_test_id(testid_value).locator(target_tag)
                    result = f"current_frame.getByTestId('{escape_quotes(testid_value)}').locator('{target_tag}')"
            else:
                # Use CSS selector for non-standard attributes (data-test-id, data-cy, etc.)
                if is_page_context:
                    loc = context.locator(f'[{test_attribute}="{testid_value}"]').locator(target_tag)
                    result = f"page.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').locator('{target_tag}')"
                else:
                    loc = context.locator(f'[{test_attribute}="{testid_value}"]').locator(target_tag)
                    result = f"current_frame.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').locator('{target_tag}')"

            # Validate it works
            if await simple_validation(loc, element_data['position'], f"TestID+CSS({target_tag})"):
                debug_log(f"🏆 TestID+CSS SUCCESS: {result}")
                return result
        except Exception as e:
            debug_log(f"❌ TestID+CSS error: {str(e)}")

        return None

    async def try_testid_text_filter_strategy(element_data):
        """🎯 Strategy: getByTestId + filter(has_text) with exact match first, then partial"""
        debug_log("🚀 STRATEGY: Testing getByTestId + filter(has_text)...")

        # 1. Check if element has suitable text for filtering
        element_text = element_data.get('text', '').strip()
        if not element_text:
            debug_log("❌ No text content found")
            return None

        # 2. Normalize text (remove extra whitespace, newlines)
        def normalize_text(text):
            # Remove extra whitespace and normalize
            normalized = ' '.join(text.split())
            # Remove newlines/tabs
            normalized = normalized.replace('\n', ' ').replace('\t', ' ')
            return normalized.strip()

        element_text = normalize_text(element_text)

        # 3. Validate text is good for filtering
        def is_good_filter_text(text):
            # Too short (avoid "OK", "Yes", "No")
            if len(text) < 1:
                debug_log(f"❌ Text too short: '{text}'")
                return False

            # Too long (avoid paragraphs)
            if len(text) > 30:
                debug_log(f"❌ Text too long: {len(text)} chars")
                return False

            return True

        if not is_good_filter_text(element_text):
            debug_log(f"❌ Text not suitable for filtering: '{element_text}'")
            return None

        # 4. Find closest parent with test ID
        closest_parent = element_data.get('closestTestIdParent')
        if not closest_parent:
            debug_log("❌ No parent with test ID found")
            return None

        # 5. Get discovered test attribute and value
        test_attribute = closest_parent['attribute']  # 'data-testid', 'data-test-id', etc.
        testid_value = closest_parent['value']  # 'header-text', 'notification-area', etc.

        debug_log(f"   🎯 Found parent: {test_attribute}='{testid_value}'")
        debug_log(f"   📝 Normalized text: '{element_text}'")

        # 6. Try both exact and partial matching (exact first)
        for exact_mode in [True, False]:
            match_type = "EXACT" if exact_mode else "PARTIAL"
            debug_log(f"   🔍 Trying {match_type} text match: '{element_text}'")

            try:
                # Prepare text filter - exact uses regex with anchors, partial uses plain string
                if exact_mode:
                    # Escape special regex characters and add anchors for exact match
                    escaped_text = re.escape(element_text)
                    text_filter = re.compile(f"^{escaped_text}$")  # ✅ Fixed: no backslash before $
                else:
                    # Plain string for partial match
                    text_filter = element_text

                # Smart method selection based on discovered attribute
                if test_attribute == 'data-testid':
                    # Use Playwright's native getByTestId() for standard data-testid
                    if is_page_context:
                        loc = context.get_by_test_id(testid_value).filter(has_text=text_filter)
                        if exact_mode:
                            result = f"page.getByTestId('{escape_quotes(testid_value)}').filter({{ has_text: re.compile(r'^{escape_quotes(element_text)}$') }})"  # ✅ Fixed
                        else:
                            result = f"page.getByTestId('{escape_quotes(testid_value)}').filter({{ has_text: '{escape_quotes(element_text)}' }})"
                    else:
                        loc = context.get_by_test_id(testid_value).filter(has_text=text_filter)
                        if exact_mode:
                            result = f"current_frame.getByTestId('{escape_quotes(testid_value)}').filter({{ has_text: re.compile(r'^{escape_quotes(element_text)}$') }})"  # ✅ Fixed
                        else:
                            result = f"current_frame.getByTestId('{escape_quotes(testid_value)}').filter({{ has_text: '{escape_quotes(element_text)}' }})"
                else:
                    # Use CSS selector for non-standard attributes (data-test-id, data-cy, etc.)
                    if is_page_context:
                        loc = context.locator(f'[{test_attribute}="{testid_value}"]').filter(has_text=text_filter)
                        if exact_mode:
                            result = f"page.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').filter({{ has_text: re.compile(r'^{escape_quotes(element_text)}$') }})"  # ✅ Fixed
                        else:
                            result = f"page.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').filter({{ has_text: '{escape_quotes(element_text)}' }})"
                    else:
                        loc = context.locator(f'[{test_attribute}="{testid_value}"]').filter(has_text=text_filter)
                        if exact_mode:
                            result = f"current_frame.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').filter({{ has_text: re.compile(r'^{escape_quotes(element_text)}$') }})"  # ✅ Fixed
                        else:
                            result = f"current_frame.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').filter({{ has_text: '{escape_quotes(element_text)}' }})"

                # Validate the locator works
                if await simple_validation(loc, element_data['position'],
                                           f"TestID({testid_value})+{match_type}Filter({element_text[:20]})"):
                    debug_log(f"🏆 TestID+{match_type}Filter SUCCESS: {result}")
                    return result
                else:
                    debug_log(f"❌ TestID+{match_type}Filter validation failed")

            except Exception as e:
                debug_log(f"❌ TestID+{match_type}Filter error: {str(e)}")

        debug_log("❌ No working TestID+TextFilter strategy found")
        return None

    async def try_label_strategy(element_data):
        """🎯 Strategy: getByLabel for form elements with associated labels"""
        debug_log("🚀 STRATEGY: Testing getByLabel...")

        # 1. Check if element has associated label text
        accessible_names = element_data.get('accessibleNames', [])
        if not accessible_names:
            debug_log("❌ No accessible names (labels) found")
            return None

        # 2. Extract potential label text (first accessible name is usually the label)
        label_text = None
        for name in accessible_names:
            # Skip empty or very short names
            name = str(name).strip()
            if name and len(name) >= 2:
                label_text = name
                break

        if not label_text:
            debug_log("❌ No suitable label text found")
            return None

        # 3. Normalize label text
        def normalize_label_text(text):
            # Remove extra whitespace and normalize
            normalized = ' '.join(text.split())
            # Remove newlines/tabs
            normalized = normalized.replace('\n', ' ').replace('\t', ' ')
            # Remove common label suffixes/prefixes
            normalized = normalized.replace('*', '').strip()  # Remove required asterisks
            normalized = normalized.rstrip(':').strip()  # Remove trailing colons
            return normalized

        label_text = normalize_label_text(label_text)
        debug_log(f"   🏷️ Found label text: '{label_text}'")

        # 4. Validate label text is good for targeting
        def is_good_label_text(text):
            # Too short (avoid "*", ":", etc.)
            if len(text) < 1:
                debug_log(f"❌ Label too short: '{text}'")
                return False

            # Too long (avoid long descriptions)
            if len(text) > 80:
                debug_log(f"❌ Label too long: {len(text)} chars")
                return False

            return True

        if not is_good_label_text(label_text):
            debug_log(f"❌ Label text not suitable for targeting: '{label_text}'")
            return None

        # 5. Try both exact and partial matching
        for exact_mode in [True, False]:
            match_type = "EXACT" if exact_mode else "PARTIAL"
            debug_log(f"   🔍 Trying {match_type} label match: '{label_text}'")

            try:
                # Create base locator
                if is_page_context:
                    if exact_mode:
                        loc = context.get_by_label(label_text, exact=True)
                        base_result = f"page.getByLabel('{escape_quotes(label_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_label(label_text)
                        base_result = f"page.getByLabel('{escape_quotes(label_text)}')"
                else:
                    if exact_mode:
                        loc = context.get_by_label(label_text, exact=True)
                        base_result = f"current_frame.getByLabel('{escape_quotes(label_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_label(label_text)
                        base_result = f"current_frame.getByLabel('{escape_quotes(label_text)}')"

                # 🎯 Use enhanced validation (labels might also have duplicates in complex forms)
                validation_result = await simple_validation(loc, element_data['position'],
                                                            f"Label({match_type}): {label_text[:20]}")

                if validation_result:
                    if isinstance(validation_result, dict):
                        # getByLabel returned nth info (multiple labels with same text)
                        nth_index = validation_result['nth_index']
                        count = validation_result['count']

                        if count == 1:
                            result = base_result  # Single element, no nth needed
                        else:
                            result = base_result + f".nth({nth_index})"  # Add nth()

                        debug_log(f"🏆 getByLabel {match_type} SUCCESS: {result}")
                        return result
                    else:
                        # Standard validation
                        debug_log(f"🏆 getByLabel {match_type} SUCCESS: {base_result}")
                        return base_result

            except Exception as e:
                debug_log(f"❌ getByLabel {match_type} error: {str(e)}")

        debug_log("❌ No working getByLabel strategy found")
        return None

    async def try_alt_text_strategy(element_data):
        """🎯 Strategy: getByAltText for images and elements with alt attributes"""
        debug_log("🚀 STRATEGY: Testing getByAltText...")

        # 1. Check if element has alt attribute
        attrs = element_data.get('attrs', {})
        alt_text = attrs.get('alt', '').strip()

        if not alt_text:
            debug_log("❌ No alt attribute found")
            return None

        # 2. Normalize alt text
        def normalize_alt_text(text):
            # Remove extra whitespace and normalize
            normalized = ' '.join(text.split())
            # Remove newlines/tabs
            normalized = normalized.replace('\n', ' ').replace('\t', ' ')
            return normalized.strip()

        alt_text = normalize_alt_text(alt_text)
        debug_log(f"   🖼️ Found alt text: '{alt_text}'")

        # 3. Validate alt text is good for targeting
        def is_good_alt_text(text):
            # Too short (avoid single chars, empty strings)
            if len(text) < 2:
                debug_log(f"❌ Alt text too short: '{text}'")
                return False

            # Too long (avoid very long descriptions)
            if len(text) > 100:
                debug_log(f"❌ Alt text too long: {len(text)} chars")
                return False

            return True

        if not is_good_alt_text(alt_text):
            debug_log(f"❌ Alt text not suitable for targeting: '{alt_text}'")
            return None

        # 4. Try both exact and partial matching
        for exact_mode in [True, False]:
            match_type = "EXACT" if exact_mode else "PARTIAL"
            debug_log(f"   🔍 Trying {match_type} alt text match: '{alt_text}'")

            try:
                # Create base locator
                if is_page_context:
                    if exact_mode:
                        loc = context.get_by_alt_text(alt_text, exact=True)
                        base_result = f"page.getByAltText('{escape_quotes(alt_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_alt_text(alt_text)
                        base_result = f"page.getByAltText('{escape_quotes(alt_text)}')"
                else:
                    if exact_mode:
                        loc = context.get_by_alt_text(alt_text, exact=True)
                        base_result = f"current_frame.getByAltText('{escape_quotes(alt_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_alt_text(alt_text)
                        base_result = f"current_frame.getByAltText('{escape_quotes(alt_text)}')"

                # 🎯 Use standard validation (alt text is usually unique, but support nth() just in case)
                validation_result = await simple_validation(loc, element_data['position'],
                                                            f"AltText({match_type}): {alt_text[:20]}")

                if validation_result:
                    if isinstance(validation_result, dict):
                        # getByAltText returned nth info (rare, but possible for duplicate alt text)
                        nth_index = validation_result['nth_index']
                        count = validation_result['count']

                        if count == 1:
                            result = base_result  # Single element, no nth needed
                        else:
                            result = base_result + f".nth({nth_index})"  # Add nth()

                        debug_log(f"🏆 getByAltText {match_type} SUCCESS: {result}")
                        return result
                    else:
                        # Standard validation (most common case)
                        debug_log(f"🏆 getByAltText {match_type} SUCCESS: {base_result}")
                        return base_result

            except Exception as e:
                debug_log(f"❌ getByAltText {match_type} error: {str(e)}")

        debug_log("❌ No working getByAltText strategy found")
        return None

    async def try_title_strategy(element_data):
        """🎯 Strategy: getByTitle for elements with title attributes (tooltips, help text)"""
        debug_log("🚀 STRATEGY: Testing getByTitle...")

        # 1. Check if element has title attribute
        attrs = element_data.get('attrs', {})
        title_text = attrs.get('title', '').strip()

        if not title_text:
            debug_log("❌ No title attribute found")
            return None

        # 2. Normalize title text
        def normalize_title_text(text):
            # Remove extra whitespace and normalize
            normalized = ' '.join(text.split())
            # Remove newlines/tabs
            normalized = normalized.replace('\n', ' ').replace('\t', ' ')
            return normalized.strip()

        title_text = normalize_title_text(title_text)
        debug_log(f"   💡 Found title text: '{title_text}'")

        # 3. Validate title text is good for targeting
        def is_good_title_text(text):
            # Too short (avoid single chars, empty strings)
            if len(text) < 2:
                debug_log(f"❌ Title text too short: '{text}'")
                return False

            # Too long (avoid very long descriptions, but more lenient than alt text)
            if len(text) > 50:
                debug_log(f"❌ Title text too long: {len(text)} chars")
                return False

            # Check for dynamic patterns in title text
            dynamic_patterns = [
                r'\d{4}-\d{2}-\d{2}',  # Dates: "2024-01-15"
                r'\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}',  # DateTime: "2024-01-15 14:30"
                r'\\$\d+\.\d{2}',  # Prices: "\$29.99"
                r'\d{1,2}:\d{2}',  # Times: "3:45 PM"
                r'ID:\s*\d+',  # IDs: "ID: 123"
                r'#\d+',  # Hash numbers: "#12345"
                r'\b\d{8,}\b',  # Long numbers
                r'[a-f0-9]{8,}',  # Hash-like strings
                r'\b(temp|tmp|generated|auto)_\w+',  # Generated names
                r'Updated:\s*\d{4}-\d{2}-\d{2}',  # Update timestamps
                r'Created:\s*\d+',  # Creation timestamps
                r'Version\s*\d+\.\d+',  # Version numbers
                r'Build\s*\d+',  # Build numbers
                r'User\s*\d+',  # User IDs in tooltips
            ]

            for pattern in dynamic_patterns:
                if re.search(pattern, text):
                    debug_log(f"❌ Title contains dynamic content: '{text}'")
                    return False

            return True

        if not is_good_title_text(title_text):
            debug_log(f"❌ Title text not suitable for targeting: '{title_text}'")
            return None

        # 4. Try both exact and partial matching
        for exact_mode in [True, False]:
            match_type = "EXACT" if exact_mode else "PARTIAL"
            debug_log(f"   🔍 Trying {match_type} title match: '{title_text}'")

            try:
                # Create base locator
                if is_page_context:
                    if exact_mode:
                        loc = context.get_by_title(title_text, exact=True)
                        base_result = f"page.getByTitle('{escape_quotes(title_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_title(title_text)
                        base_result = f"page.getByTitle('{escape_quotes(title_text)}')"
                else:
                    if exact_mode:
                        loc = context.get_by_title(title_text, exact=True)
                        base_result = f"current_frame.getByTitle('{escape_quotes(title_text)}', {{ exact: true }})"
                    else:
                        loc = context.get_by_title(title_text)
                        base_result = f"current_frame.getByTitle('{escape_quotes(title_text)}')"

                # 🎯 Use enhanced validation (titles can sometimes be duplicated)
                validation_result = await simple_validation(loc, element_data['position'],
                                                            f"Title({match_type}): {title_text[:20]}")

                if validation_result:
                    if isinstance(validation_result, dict):
                        # getByTitle returned nth info (multiple elements with same title)
                        nth_index = validation_result['nth_index']
                        count = validation_result['count']

                        if count == 1:
                            result = base_result  # Single element, no nth needed
                        else:
                            result = base_result + f".nth({nth_index})"  # Add nth()

                        debug_log(f"🏆 getByTitle {match_type} SUCCESS: {result}")
                        return result
                    else:
                        # Standard validation (most common case)
                        debug_log(f"🏆 getByTitle {match_type} SUCCESS: {base_result}")
                        return base_result

            except Exception as e:
                debug_log(f"❌ getByTitle {match_type} error: {str(e)}")

        debug_log("❌ No working getByTitle strategy found")
        return None

    async def try_multi_attribute_strategy(element_data):
        """🎯 Strategy: Multi-attribute combinations for specific targeting"""
        debug_log("🚀 STRATEGY: Testing Multi-Attribute combinations...")

        # 1. Extract available attributes
        attrs = element_data.get('attrs', {})
        tag = element_data.get('tag', '')

        if not attrs or len(attrs) < 2:
            debug_log("❌ Not enough attributes for multi-attribute strategy")
            return None

        # 2. Define meaningful attribute groups and their priorities
        def get_meaningful_attributes(attrs, tag):
            """Extract and categorize meaningful attributes"""
            meaningful_attrs = {}

            # Form-related attributes (high priority)
            form_attrs = ['type', 'name', 'placeholder', 'role']
            for attr in form_attrs:
                if attr in attrs and attrs[attr].strip():
                    meaningful_attrs[attr] = attrs[attr].strip()

            # Semantic attributes (high priority)
            semantic_attrs = ['role', 'aria-label', 'aria-describedby', 'aria-expanded']
            for attr in semantic_attrs:
                if attr in attrs and attrs[attr].strip():
                    meaningful_attrs[attr] = attrs[attr].strip()

            # Structural attributes (medium priority)
            if 'class' in attrs and attrs['class'].strip():
                # Extract meaningful class names (not dynamic ones)
                class_value = attrs['class'].strip()
                if is_meaningful_class(class_value):
                    meaningful_attrs['class'] = class_value

            # 🎯 Use existing is_static_id function for ID validation
            if 'id' in attrs and attrs['id'].strip():
                id_value = attrs['id'].strip()
                if is_static_id(id_value):  # Use our existing function
                    meaningful_attrs['id'] = id_value
                    debug_log(f"   ✅ ID '{id_value}' is static - including in combinations")
                else:
                    debug_log(f"   ❌ ID '{id_value}' is dynamic - excluding from combinations")

            # Interactive attributes (medium priority)
            interactive_attrs = ['href', 'target', 'disabled', 'readonly']
            for attr in interactive_attrs:
                if attr in attrs and attrs[attr].strip():
                    meaningful_attrs[attr] = attrs[attr].strip()

            return meaningful_attrs

        def is_meaningful_class(class_value):
            """Check if class names are meaningful and not dynamic"""
            classes = class_value.split()
            meaningful_classes = []

            for cls in classes:
                # Skip dynamic/generated classes
                if re.search(r'\d{3,}|[a-f0-9]{6,}|temp|gen|auto|random', cls):
                    continue
                # Skip very generic classes
                if cls in ['class', 'element', 'item', 'content', 'wrapper', 'container']:
                    continue
                # Keep semantic classes
                if re.match(r'^(btn|button|nav|menu|form|input|text|link|card|modal|dialog)', cls):
                    meaningful_classes.append(cls)
                elif len(cls) > 3 and not cls.isdigit():
                    meaningful_classes.append(cls)

            return ' '.join(meaningful_classes) if meaningful_classes else None

        meaningful_attrs = get_meaningful_attributes(attrs, tag)

        if len(meaningful_attrs) < 2:
            debug_log(f"❌ Not enough meaningful attributes: {list(meaningful_attrs.keys())}")
            return None

        debug_log(f"   🎯 Found meaningful attributes: {list(meaningful_attrs.keys())}")

        # 3. Generate attribute combinations
        def generate_attribute_combinations(attrs, tag):
            """Generate logical attribute combinations"""
            combinations = []

            # Form element combinations (highest priority)
            if tag in ['input', 'textarea', 'select']:
                # type + name
                if 'type' in attrs and 'name' in attrs:
                    combinations.append([('type', attrs['type']), ('name', attrs['name'])])
                # type + placeholder
                if 'type' in attrs and 'placeholder' in attrs:
                    combinations.append([('type', attrs['type']), ('placeholder', attrs['placeholder'])])
                # name + role
                if 'name' in attrs and 'role' in attrs:
                    combinations.append([('name', attrs['name']), ('role', attrs['role'])])

            # Interactive element combinations
            if tag in ['button', 'a', 'div']:
                # role + aria-label
                if 'role' in attrs and 'aria-label' in attrs:
                    combinations.append([('role', attrs['role']), ('aria-label', attrs['aria-label'])])
                # type + class
                if 'type' in attrs and 'class' in attrs:
                    combinations.append([('type', attrs['type']), ('class', attrs['class'])])

            # Semantic combinations (any element)
            if 'role' in attrs and 'class' in attrs:
                combinations.append([('role', attrs['role']), ('class', attrs['class'])])
            if 'aria-label' in attrs and 'class' in attrs:
                combinations.append([('aria-label', attrs['aria-label']), ('class', attrs['class'])])

            # Structural combinations (only with static IDs)
            if 'id' in attrs and 'class' in attrs:
                combinations.append([('id', attrs['id']), ('class', attrs['class'])])
            if 'id' in attrs and 'type' in attrs:
                combinations.append([('id', attrs['id']), ('type', attrs['type'])])

            # General combinations
            if 'type' in attrs and 'class' in attrs and tag not in ['button']:
                combinations.append([('type', attrs['type']), ('class', attrs['class'])])

            return combinations

        combinations = generate_attribute_combinations(meaningful_attrs, tag)

        if not combinations:
            debug_log("❌ No suitable attribute combinations found")
            return None

        debug_log(f"   🔄 Generated {len(combinations)} attribute combinations to test")

        # 4. Test each combination using existing validation
        for i, combination in enumerate(combinations):
            combo_name = " + ".join([f"{attr}={val[:15]}{'...' if len(val) > 15 else ''}" for attr, val in combination])
            debug_log(f"   🔍 Testing combination {i + 1}: {combo_name}")

            try:
                # Build CSS selector
                selector_parts = []
                for attr, value in combination:
                    if attr == 'class':
                        # Handle class attributes specially
                        classes = value.split()
                        for cls in classes[:2]:  # Use max 2 classes to avoid over-specificity
                            selector_parts.append(f'.{cls}')
                    elif attr == 'id':
                        selector_parts.append(f'#{value}')
                    else:
                        # Handle special characters in attribute values
                        escaped_value = value.replace('"', '\\"')
                        selector_parts.append(f'[{attr}="{escaped_value}"]')

                css_selector = ''.join(selector_parts)

                # Create locator
                if is_page_context:
                    loc = context.locator(css_selector)
                    result = f"page.locator('{css_selector}')"
                else:
                    loc = context.locator(css_selector)
                    result = f"current_frame.locator('{css_selector}')"

                # 🎯 USE EXISTING VALIDATION FUNCTION
                if await simple_validation(loc, element_data['position'], f"MultiAttr({combo_name[:20]})"):
                    debug_log(f"🏆 Multi-Attribute SUCCESS: {result}")
                    return result

            except Exception as e:
                debug_log(f"❌ Multi-Attribute combination {i + 1} error: {str(e)}")
                continue

        debug_log("❌ No working multi-attribute combination found")
        return None

    async def try_testid_relative_xpath_strategy(element_data):
        """🎯 Strategy: TestID + Relative XPath using smart anchor detection"""
        debug_log("🚀 STRATEGY: Testing TestID + Relative XPath...")

        try:
            # Use adapted version of your smart XPath logic
            xpath_result = await target_locator.evaluate("""
            (targetElement) => {
                function isUnique(xpath) {
                    try {
                        return document.evaluate('count(' + xpath + ')', document, null, XPathResult.NUMBER_TYPE, null).numberValue === 1;
                    } catch(e) {
                        return false;
                    }
                }

                function getTestIdRelativeXPath(element) {
                    let xpath = '';
                    let foundTestIdAnchor = false;

                    // Walk up the DOM tree
                    for (let current = element; current && current.nodeType == 1; current = current.parentNode) {
                        let tag = current.tagName.toLowerCase();

                        // 🎯 PRIORITY 1: Check for TestID attributes (our anchor points)
                        const testIdAttrs = ['data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id'];

                        for (let attr of testIdAttrs) {
                            if (current.hasAttribute(attr)) {
                                let testIdValue = current.getAttribute(attr);
                                let testXpath = `//${tag}[@${attr}="${testIdValue}"]${xpath ? '/' + xpath : ''}`;

                                console.log('🎯 Testing TestID anchor:', testXpath);

                                if (isUnique(testXpath)) {
                                    console.log('✅ Found unique TestID anchor:', testXpath);
                                    return {
                                        xpath: testXpath,
                                        anchorType: 'testid',
                                        anchorAttr: attr,
                                        anchorValue: testIdValue,
                                        isUnique: true
                                    };
                                }
                            }
                        }

                        // If we haven't found TestID anchor yet, continue building path
                        if (!foundTestIdAnchor) {
                            let siblings = current.parentNode ? current.parentNode.children : [];
                            let hasSameTagSiblings = Array.from(siblings).some(sibling =>
                                sibling !== current && sibling.tagName === current.tagName
                            );

                            // Use position for path building
                            let index = hasSameTagSiblings
                                ? Array.from(siblings).filter(s => s.tagName === current.tagName).indexOf(current) + 1
                                : '';

                            xpath = `${tag}${index ? '[' + index + ']' : ''}${xpath ? '/' + xpath : ''}`;
                        }
                    }

                    // If no TestID anchor found, return null
                    return null;
                }

                return getTestIdRelativeXPath(targetElement);
            }
            """, timeout=3000)

            if not xpath_result or not xpath_result.get('xpath'):
                debug_log("❌ Could not generate TestID-anchored XPath")
                return None

            debug_log(f"   🎯 TestID Anchor Analysis:")
            debug_log(f"      Anchor: {xpath_result['anchorAttr']}='{xpath_result['anchorValue']}'")
            debug_log(f"      Generated XPath: {xpath_result['xpath']}")
            debug_log(f"      Is Unique: {xpath_result['isUnique']}")

            # Create locator with the generated XPathFa
            xpath_selector = xpath_result['xpath']

            if is_page_context:
                loc = context.locator(f'xpath={xpath_selector}')
                result = f"page.locator('xpath={xpath_selector}')"
            else:
                loc = context.locator(f'xpath={xpath_selector}')
                result = f"current_frame.locator('xpath={xpath_selector}')"

            # Validate
            if await simple_validation(loc, element_data['position'], f"TestID+SmartXPath"):
                debug_log(f"🏆 TestID + Smart XPath SUCCESS: {result}")
                return result

        except Exception as e:
            debug_log(f"❌ TestID + Smart XPath error: {str(e)}")

        return None

    async def try_testid_text_combination(element_data):
        """🎯 Strategy: getByTestId + getByText combination"""
        debug_log("🚀 STRATEGY: Testing getByTestId + getByText...")

        # 1. Check if we have both requirements
        closest_parent = element_data.get('closestTestIdParent')
        element_text = element_data.get('text', '').strip()

        if not closest_parent or not element_text:
            debug_log("❌ Missing TestID parent or element text")
            return None

        # 2. Normalize text
        def normalize_text(text):
            normalized = ' '.join(text.split())
            normalized = normalized.replace('\n', ' ').replace('\t', ' ')
            return normalized.strip()

        element_text = normalize_text(element_text)

        # 3. Validate text is suitable
        if len(element_text) < 1 or len(element_text) > 100:
            debug_log(f"❌ Text not suitable: '{element_text}' (length: {len(element_text)})")
            return None

        # 4. Get TestID details
        test_attribute = closest_parent['attribute']  # 'data-testid', 'data-test', etc.
        testid_value = closest_parent['value']  # 'chewy-home-card-autoship'

        debug_log(f"   🎯 TestID: {test_attribute}='{testid_value}'")
        debug_log(f"   📝 Text: '{element_text}'")

        # 5. Try both exact and partial text matching
        for exact_mode in [True, False]:
            match_type = "EXACT" if exact_mode else "PARTIAL"
            debug_log(f"   🔍 Trying {match_type} TestID+Text: '{element_text}'")

            try:
                # 🎯 CONTEXT-AWARE LOCATOR CREATION
                if test_attribute == 'data-testid':
                    # Use native getByTestId() for standard data-testid
                    if is_page_context:
                        if exact_mode:
                            loc = context.get_by_test_id(testid_value).get_by_text(element_text, exact=True)
                            result = f"page.getByTestId('{escape_quotes(testid_value)}').getByText('{escape_quotes(element_text)}', {{ exact: true }})"
                        else:
                            loc = context.get_by_test_id(testid_value).get_by_text(element_text)
                            result = f"page.getByTestId('{escape_quotes(testid_value)}').getByText('{escape_quotes(element_text)}')"
                    else:
                        # 🔥 CURRENT_FRAME CONTEXT
                        if exact_mode:
                            loc = context.get_by_test_id(testid_value).get_by_text(element_text, exact=True)
                            result = f"current_frame.getByTestId('{escape_quotes(testid_value)}').getByText('{escape_quotes(element_text)}', {{ exact: true }})"
                        else:
                            loc = context.get_by_test_id(testid_value).get_by_text(element_text)
                            result = f"current_frame.getByTestId('{escape_quotes(testid_value)}').getByText('{escape_quotes(element_text)}')"

                else:
                    # Use CSS selector for non-standard attributes (data-test, data-cy, etc.)
                    if is_page_context:
                        if exact_mode:
                            loc = context.locator(f'[{test_attribute}="{testid_value}"]').get_by_text(element_text,
                                                                                                      exact=True)
                            result = f"page.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').getByText('{escape_quotes(element_text)}', {{ exact: true }})"
                        else:
                            loc = context.locator(f'[{test_attribute}="{testid_value}"]').get_by_text(element_text)
                            result = f"page.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').getByText('{escape_quotes(element_text)}')"
                    else:
                        # 🔥 CURRENT_FRAME CONTEXT
                        if exact_mode:
                            loc = context.locator(f'[{test_attribute}="{testid_value}"]').get_by_text(element_text,
                                                                                                      exact=True)
                            result = f"current_frame.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').getByText('{escape_quotes(element_text)}', {{ exact: true }})"
                        else:
                            loc = context.locator(f'[{test_attribute}="{testid_value}"]').get_by_text(element_text)
                            result = f"current_frame.locator('[{test_attribute}=\"{escape_quotes(testid_value)}\"]').getByText('{escape_quotes(element_text)}')"

                # 6. Enhanced validation with nth() support
                validation_result = await simple_validation(loc, element_data['position'],
                                                            f"TestID+Text({match_type}): {element_text[:20]}")

                if validation_result:
                    if isinstance(validation_result, dict):
                        # Handle multiple matches with nth()
                        nth_index = validation_result['nth_index']
                        count = validation_result['count']

                        if count == 1:
                            final_result = result  # Single element
                        else:
                            final_result = result + f".nth({nth_index})"  # Add nth()

                        debug_log(f"🏆 TestID+Text {match_type} SUCCESS: {final_result}")
                        return final_result
                    else:
                        # Standard validation
                        debug_log(f"🏆 TestID+Text {match_type} SUCCESS: {result}")
                        return result

            except Exception as e:
                debug_log(f"❌ TestID+Text {match_type} error: {str(e)}")

        debug_log("❌ No working TestID+Text combination found")
        return None

    async def try_css_text_filter_strategy(element_data):
        """🎯 Strategy: CSS Tag Selector + Text Filter"""
        debug_log("🚀 STRATEGY: Testing CSS Tag + Text Filter...")

        # 1. Get element details
        tag = element_data.get('tag', '').strip()
        element_text = element_data.get('text', '').strip()

        if not tag or not element_text:
            debug_log("❌ Missing tag or text for CSS+Filter strategy")
            return None

        # 2. Normalize text
        def normalize_text(text):
            normalized = ' '.join(text.split())
            return normalized.replace('\n', ' ').replace('\t', ' ').strip()

        element_text = normalize_text(element_text)

        # 3. Validate text suitability
        if len(element_text) < 1 or len(element_text) > 50:
            debug_log(f"❌ Text not suitable for filtering: '{element_text}' (length: {len(element_text)})")
            return None

        # # 4. Skip if tag is too generic (would match too many elements)
        # generic_tags = ['div', 'span', 'p', 'section', 'article']
        # if tag in generic_tags:
        #     debug_log(f"❌ Tag '{tag}' too generic for CSS+Filter strategy")
        #     return None

        debug_log(f"   🏷️ Tag: '{tag}'")
        debug_log(f"   📝 Text: '{element_text}'")

        # 5. Try both exact and partial text matching
        for exact_mode in [True, False]:
            match_type = "EXACT" if exact_mode else "PARTIAL"
            debug_log(f"   🔍 Trying {match_type} CSS+Filter: {tag} + '{element_text}'")

            try:
                # Prepare text filter
                if exact_mode:
                    # Use regex for exact match
                    import re
                    escaped_text = re.escape(element_text)
                    text_filter = re.compile(f"^{escaped_text}$")
                else:
                    # Plain string for partial match
                    text_filter = element_text

                # 🎯 CONTEXT-AWARE LOCATOR CREATION
                if is_page_context:
                    loc = context.locator(tag).filter(has_text=text_filter)
                    if exact_mode:
                        result = f"page.locator('{tag}').filter({{ hasText: /^{escape_quotes(element_text)}$/ }})"
                    else:
                        result = f"page.locator('{tag}').filter({{ hasText: '{escape_quotes(element_text)}' }})"
                else:
                    # 🔥 CURRENT_FRAME CONTEXT
                    loc = context.locator(tag).filter(has_text=text_filter)
                    if exact_mode:
                        result = f"current_frame.locator('{tag}').filter({{ hasText: /^{escape_quotes(element_text)}$/ }})"
                    else:
                        result = f"current_frame.locator('{tag}').filter({{ hasText: '{escape_quotes(element_text)}' }})"

                # 6. Enhanced validation with nth() support
                validation_result = await simple_validation(loc, element_data['position'],
                                                            f"CSS+Filter({match_type}): {tag}+{element_text[:15]}")

                if validation_result:
                    if isinstance(validation_result, dict):
                        # Handle multiple matches
                        nth_index = validation_result['nth_index']
                        count = validation_result['count']

                        if count == 1:
                            final_result = result
                        else:
                            final_result = result + f".nth({nth_index})"

                        debug_log(f"🏆 CSS+Filter {match_type} SUCCESS: {final_result}")
                        return final_result
                    else:
                        debug_log(f"🏆 CSS+Filter {match_type} SUCCESS: {result}")
                        return result

            except Exception as e:
                debug_log(f"❌ CSS+Filter {match_type} error: {str(e)}")

        debug_log("❌ No working CSS+Filter strategy found")
        return None

    async def try_label_parent_combinations(element_data):
        """🎯 Strategy: Find parent label + target element combinations"""
        debug_log("🚀 STRATEGY: Testing Label Parent Combinations...")

        # 1. Find closest label parent (similar to closestTestIdParent)
        label_parent_info = await target_locator.evaluate("""
        (targetElement) => {
            // Find closest label parent (up to 3 levels)
            let currentElement = targetElement.parentElement;

            for (let level = 1; level <= 3; level++) {
                if (!currentElement) break;

                if (currentElement.tagName.toLowerCase() === 'label') {
                    const labelText = currentElement.textContent?.trim();
                    if (labelText && labelText.length >= 3 && labelText.length <= 100) {
                        return {
                            hasLabelParent: true,
                            labelText: labelText,
                            level: level,
                            targetTag: targetElement.tagName.toLowerCase(),
                            targetRole: targetElement.getAttribute('role') || getImplicitRole(targetElement)
                        };
                    }
                }

                currentElement = currentElement.parentElement;
            }

            // Helper function for implicit roles
            function getImplicitRole(el) {
                const tag = el.tagName.toLowerCase();
                if (tag === 'input') {
                    const type = el.getAttribute('type') || 'text';
                    return type === 'button' ? 'button' :
                           type === 'checkbox' ? 'checkbox' :
                           type === 'radio' ? 'radio' : 'textbox';
                }
                return tag === 'button' ? 'button' :
                       tag === 'a' ? 'link' : null;
            }

            return { hasLabelParent: false };
        }
        """, timeout=3000)

        if not label_parent_info.get('hasLabelParent'):
            debug_log("❌ No suitable label parent found")
            return None

        label_text = label_parent_info['labelText']
        target_tag = label_parent_info['targetTag']
        target_role = label_parent_info['targetRole']
        level = label_parent_info['level']

        debug_log(f"   🏷️ Found label parent (level {level}): '{label_text[:30]}...'")
        debug_log(f"   🎯 Target tag: '{target_tag}', role: '{target_role}'")

        # 2. Try different combination strategies
        combinations_to_try = []

        # Strategy A: label.filter + getByRole (PREFERRED - Semantic)
        if target_role:
            combinations_to_try.append({
                'type': 'label_filter_role',
                'role': target_role
            })

        # Strategy B: label.filter + locator(tag) (Fallback)
        combinations_to_try.append({
            'type': 'label_filter_tag',
            'tag': target_tag
        })

        # Strategy C: label.filter + specific input type
        if target_tag == 'input':
            input_type = await target_locator.get_attribute('type') or 'text'
            combinations_to_try.append({
                'type': 'label_filter_input_type',
                'input_type': input_type
            })

        # 3. Test each combination
        for combo in combinations_to_try:
            try:
                combo_type = combo['type']
                debug_log(f"   🔍 Testing {combo_type}...")

                if combo_type == 'label_filter_role':
                    # PREFERRED: page.locator('label').filter({hasText: 'text'}).getByRole('radio')
                    role = combo['role']
                    if is_page_context:
                        loc = context.locator('label').filter(has_text=label_text).get_by_role(role)
                        result = f"page.locator('label').filter({{ hasText: '{escape_quotes(label_text)}' }}).getByRole('{role}')"
                    else:
                        loc = context.locator('label').filter(has_text=label_text).get_by_role(role)
                        result = f"current_frame.locator('label').filter({{ hasText: '{escape_quotes(label_text)}' }}).getByRole('{role}')"

                elif combo_type == 'label_filter_tag':
                    # FALLBACK: page.locator('label').filter({hasText: 'text'}).locator('input')
                    tag = combo['tag']
                    if is_page_context:
                        loc = context.locator('label').filter(has_text=label_text).locator(tag)
                        result = f"page.locator('label').filter({{ hasText: '{escape_quotes(label_text)}' }}).locator('{tag}')"
                    else:
                        loc = context.locator('label').filter(has_text=label_text).locator(tag)
                        result = f"current_frame.locator('label').filter({{ hasText: '{escape_quotes(label_text)}' }}).locator('{tag}')"

                elif combo_type == 'label_filter_input_type':
                    # SPECIFIC: page.locator('label').filter({hasText: 'text'}).locator('input[type="radio"]')
                    input_type = combo['input_type']
                    if is_page_context:
                        loc = context.locator('label').filter(has_text=label_text).locator(
                            f'input[type="{input_type}"]')
                        result = f"page.locator('label').filter({{ hasText: '{escape_quotes(label_text)}' }}).locator('input[type=\"{input_type}\"]')"
                    else:
                        loc = context.locator('label').filter(has_text=label_text).locator(
                            f'input[type="{input_type}"]')
                        result = f"current_frame.locator('label').filter({{ hasText: '{escape_quotes(label_text)}' }}).locator('input[type=\"{input_type}\"]')"

                # Validate the combination
                if await simple_validation(loc, element_data['position'], f"LabelParent+{combo_type}"):
                    debug_log(f"🏆 Label Parent Combination SUCCESS: {result}")
                    return result

            except Exception as e:
                debug_log(f"❌ Label combination {combo_type} failed: {str(e)}")

        debug_log("❌ No working label parent combination found")
        return None

    async def try_label_text_combination(element_data):
        """🎯 Strategy: getByLabel + getByText combination"""
        debug_log("🚀 STRATEGY: Testing getByLabel + getByText combination...")

        # 1. Get target element text that we want to find
        element_text = element_data.get('text', '').strip()

        if not element_text:
            debug_log("❌ No element text found for Label+Text combination")
            return None

        # Normalize text
        def normalize_text(text):
            normalized = ' '.join(text.split())
            return normalized.replace('\n', ' ').replace('\t', ' ').strip()

        element_text = normalize_text(element_text)

        # Validate text is suitable
        if len(element_text) < 2 or len(element_text) > 80:
            debug_log(f"❌ Element text not suitable: '{element_text}' (length: {len(element_text)})")
            return None

        debug_log(f"   📝 Target text: '{element_text}'")

        # 2. Find label candidates associated with this element
        try:
            label_candidates = await target_locator.evaluate("""
            (targetEl) => {
                const candidates = [];

                // Method 1: Find associated label via 'for' attribute
                const elementId = targetEl.id;
                if (elementId) {
                    const associatedLabel = document.querySelector(`label[for="${elementId}"]`);
                    if (associatedLabel) {
                        const labelText = associatedLabel.textContent?.trim() || '';
                        candidates.push({
                            type: 'associated',
                            labelText: labelText,
                            priority: 1
                        });
                    }
                }

                // Method 2: Find wrapping label (parent label element)
                const wrappingLabel = targetEl.closest('label');
                if (wrappingLabel) {
                    const labelText = wrappingLabel.textContent?.trim() || '';
                    // Remove the target element's text from label text to get pure label text
                    let pureLabelText = labelText;
                    const targetText = targetEl.textContent?.trim() || '';
                    if (targetText && labelText.includes(targetText)) {
                        pureLabelText = labelText.replace(targetText, '').trim();
                    }

                    candidates.push({
                        type: 'wrapping',
                        labelText: pureLabelText,
                        fullLabelText: labelText,
                        priority: 2
                    });

                    // Special case: Empty label (when label has no text besides target element)
                    if (!pureLabelText || pureLabelText.length < 2) {
                        candidates.push({
                            type: 'empty_wrapping',
                            labelText: '',
                            priority: 3
                        });
                    }
                }

                // Method 3: Find fieldset with legend
                const fieldset = targetEl.closest('fieldset');
                if (fieldset) {
                    const legend = fieldset.querySelector('legend');
                    if (legend) {
                        const legendText = legend.textContent?.trim() || '';
                        candidates.push({
                            type: 'fieldset_legend',
                            labelText: legendText,
                            priority: 4
                        });
                    }
                }

                // Method 4: Find parent with aria-label
                const ariaLabelParent = targetEl.closest('[aria-label]');
                if (ariaLabelParent && ariaLabelParent !== targetEl) {
                    const ariaLabel = ariaLabelParent.getAttribute('aria-label');
                    if (ariaLabel && ariaLabel.trim()) {
                        candidates.push({
                            type: 'aria_label_parent',
                            labelText: ariaLabel.trim(),
                            priority: 5
                        });
                    }
                }

                // Sort by priority (lower number = higher priority)
                return candidates.sort((a, b) => a.priority - b.priority);
            }
            """, timeout=3000)

        except Exception as e:
            debug_log(f"❌ Failed to extract label candidates: {str(e)}")
            return None

        if not label_candidates or len(label_candidates) == 0:
            debug_log("❌ No label candidates found")
            return None

        debug_log(f"   🏷️ Found {len(label_candidates)} label candidates")
        for i, candidate in enumerate(label_candidates):
            debug_log(
                f"      {i + 1}. {candidate['type']}: '{candidate['labelText'][:30]}{'...' if len(candidate['labelText']) > 30 else ''}'")

        # 3. Try different label + text combinations
        for candidate in label_candidates:
            label_text = candidate['labelText']
            candidate_type = candidate['type']

            debug_log(f"   🔍 Testing {candidate_type} with label: '{label_text}'")

            # Define combinations to try (label_text, label_exact, element_text, text_exact)
            combinations = []

            if label_text:  # Non-empty label text
                combinations.extend([
                    (label_text, True, element_text, True),  # Both exact
                    (label_text, True, element_text, False),  # Label exact, text partial
                    (label_text, False, element_text, True),  # Label partial, text exact
                    (label_text, False, element_text, False)  # Both partial
                ])
            else:  # Empty label text (special case)
                combinations.extend([
                    ('', True, element_text, True),  # Empty exact + text exact
                    ('', True, element_text, False)  # Empty exact + text partial
                ])

            # Try each combination
            for label_val, label_exact, text_val, text_exact in combinations:
                debug_log(
                    f"     🧪 Trying: label='{label_val}' (exact={label_exact}) + text='{text_val}' (exact={text_exact})")

                try:
                    # Build locator based on context
                    if is_page_context:
                        # Page context
                        if label_exact and text_exact:
                            loc = context.get_by_label(label_val, exact=True).get_by_text(text_val, exact=True)
                            result = f"page.getByLabel('{escape_quotes(label_val)}', {{ exact: true }}).getByText('{escape_quotes(text_val)}', {{ exact: true }})"
                        elif label_exact and not text_exact:
                            loc = context.get_by_label(label_val, exact=True).get_by_text(text_val)
                            result = f"page.getByLabel('{escape_quotes(label_val)}', {{ exact: true }}).getByText('{escape_quotes(text_val)}')"
                        elif not label_exact and text_exact:
                            loc = context.get_by_label(label_val).get_by_text(text_val, exact=True)
                            result = f"page.getByLabel('{escape_quotes(label_val)}').getByText('{escape_quotes(text_val)}', {{ exact: true }})"
                        else:  # Both partial
                            loc = context.get_by_label(label_val).get_by_text(text_val)
                            result = f"page.getByLabel('{escape_quotes(label_val)}').getByText('{escape_quotes(text_val)}')"
                    else:
                        # Frame context
                        if label_exact and text_exact:
                            loc = context.get_by_label(label_val, exact=True).get_by_text(text_val, exact=True)
                            result = f"current_frame.getByLabel('{escape_quotes(label_val)}', {{ exact: true }}).getByText('{escape_quotes(text_val)}', {{ exact: true }})"
                        elif label_exact and not text_exact:
                            loc = context.get_by_label(label_val, exact=True).get_by_text(text_val)
                            result = f"current_frame.getByLabel('{escape_quotes(label_val)}', {{ exact: true }}).getByText('{escape_quotes(text_val)}')"
                        elif not label_exact and text_exact:
                            loc = context.get_by_label(label_val).get_by_text(text_val, exact=True)
                            result = f"current_frame.getByLabel('{escape_quotes(label_val)}').getByText('{escape_quotes(text_val)}', {{ exact: true }})"
                        else:  # Both partial
                            loc = context.get_by_label(label_val).get_by_text(text_val)
                            result = f"current_frame.getByLabel('{escape_quotes(label_val)}').getByText('{escape_quotes(text_val)}')"

                    # Validate the combination
                    validation_result = await simple_validation(loc, element_data['position'],
                                                                f"Label+Text({candidate_type})")

                    if validation_result:
                        if isinstance(validation_result, dict):
                            # Handle multiple matches with nth()
                            nth_index = validation_result['nth_index']
                            count = validation_result['count']

                            if count == 1:
                                final_result = result
                            else:
                                final_result = result + f".nth({nth_index})"

                            debug_log(f"🏆 getByLabel+getByText SUCCESS: {final_result}")
                            return final_result
                        else:
                            # Standard single match
                            debug_log(f"🏆 getByLabel+getByText SUCCESS: {result}")
                            return result

                except Exception as e:
                    debug_log(f"     ❌ Combination failed: {str(e)}")
                    continue

        debug_log("❌ No working getByLabel+getByText combination found")
        return None

    async def try_text_to_target_direct_xpath_strategy(element_data):
        """🎯 Strategy: Generate direct XPath from text to target
        Formats:
        - //*[text()="anchor"]/following-sibling::tag/path/to/target
        - //*[text()="anchor"]/parent::tag/path/to/target
        - //*[text()="anchor"]//path/to/target
        """
        debug_log("🚀 STRATEGY: Testing Text-to-Target Direct XPath...")

        anchor_analysis = await target_locator.evaluate("""
        (targetElement) => {
            const results = [];
            const container = targetElement.closest('div, section, tr, form, article, body') || document.body;

            // Find all potential text anchors
            const walker = document.createTreeWalker(
                container,
                NodeFilter.SHOW_TEXT,
                null,
                false
            );

            const textCandidates = [];
            let node;
            while (node = walker.nextNode()) {
                const text = node.textContent.trim();
                const textElement = node.parentElement;

                // Filter good text anchors
                if (text.length >= 3 &&
                    text.length <= 50 &&
                    textElement !== targetElement &&
                    !targetElement.contains(textElement) &&
                    !textElement.contains(targetElement)) {

                    textCandidates.push({
                        text: text,
                        element: textElement
                    });
                }
            }

            // For each text candidate, try all three approaches
            for (let candidate of textCandidates) {
                const approaches = generateAllThreeApproaches(candidate.element, targetElement, candidate.text);
                results.push(...approaches);
            }

            function generateAllThreeApproaches(textEl, targetEl, anchorText) {
                const approaches = [];

                // 🎯 APPROACH 1: Following Sibling
                const followingSiblingPath = buildFollowingSiblingPath(textEl, targetEl);
                if (followingSiblingPath) {
                    const xpath = `//*[text()="${anchorText}"]${followingSiblingPath}`;
                    if (testXPath(xpath, targetEl)) {
                        approaches.push({
                            anchorText: anchorText,
                            xpath: xpath,
                            approach: 'following-sibling'
                        });
                    }
                }

                // 🎯 APPROACH 2: Parent/Ancestor Path
                const parentPath = buildParentPath(textEl, targetEl);
                if (parentPath) {
                    const xpath = `//*[text()="\${anchorText}"]\${parentPath}`;
                    if (testXPath(xpath, targetEl)) {
                        approaches.push({
                            anchorText: anchorText,
                            xpath: xpath,
                            approach: 'parent'
                        });
                    }
                }

                // 🎯 APPROACH 3: Descendant Path
                const descendantPath = buildDescendantPath(textEl, targetEl);
                if (descendantPath) {
                    const xpath = `//*[text()="\${anchorText}"]\${descendantPath}`;
                    if (testXPath(xpath, targetEl)) {
                        approaches.push({
                            anchorText: anchorText,
                            xpath: xpath,
                            approach: 'descendant'
                        });
                    }
                }

                return approaches;
            }

            // 🎯 APPROACH 1: Following Sibling Path Builder
            function buildFollowingSiblingPath(textEl, targetEl) {
                let currentSibling = textEl.nextElementSibling;

                while (currentSibling) {
                    if (currentSibling === targetEl) {
                        // Direct following sibling
                        return `/following-sibling::\${targetEl.tagName.toLowerCase()}`;
                    }

                    if (currentSibling.contains(targetEl)) {
                        // Target is inside a following sibling
                        const siblingTag = currentSibling.tagName.toLowerCase();
                        const pathInsideSibling = buildDirectPath(currentSibling, targetEl);
                        return `/following-sibling::\${siblingTag}\${pathInsideSibling}`;
                    }

                    currentSibling = currentSibling.nextElementSibling;
                }

                return null;
            }

            // 🎯 APPROACH 2: Parent Path Builder
            function buildParentPath(textEl, targetEl) {
                // Try going up 1-3 levels and then finding target
                for (let level = 1; level <= 3; level++) {
                    let ancestor = textEl;
                    let parentPath = '';

                    // Go up 'level' times
                    for (let i = 0; i < level; i++) {
                        ancestor = ancestor.parentElement;
                        if (!ancestor) break;

                        if (i === 0) {
                            parentPath = `/parent::\${ancestor.tagName.toLowerCase()}`;
                        } else {
                            parentPath += `/parent::\${ancestor.tagName.toLowerCase()}`;
                        }
                    }

                    if (ancestor && ancestor.contains(targetEl)) {
                        // Found an ancestor that contains target
                        const pathToTarget = buildDirectPath(ancestor, targetEl);
                        return `\${parentPath}\${pathToTarget}`;
                    }
                }

                return null;
            }

            // 🎯 APPROACH 3: Descendant Path Builder
            function buildDescendantPath(textEl, targetEl) {
                // Find common ancestor of text and target
                const commonAncestor = findCommonAncestor(textEl, targetEl);

                if (commonAncestor && commonAncestor !== textEl) {
                    // Use descendant search from text element context
                    const pathToTarget = buildDirectPath(commonAncestor, targetEl);
                    return `/\${pathToTarget.substring(1)}`; // Remove first slash for descendant
                }

                return null;
            }

            // Helper: Build direct path from ancestor to descendant
            function buildDirectPath(ancestor, descendant) {
                if (ancestor === descendant) return '';
                if (!ancestor.contains(descendant)) return null;

                const path = [];
                let current = descendant;

                while (current && current !== ancestor) {
                    const tag = current.tagName.toLowerCase();
                    const parent = current.parentElement;

                    if (!parent) break;

                    // Get siblings of same tag
                    const siblings = Array.from(parent.children)
                        .filter(el => el.tagName.toLowerCase() === tag);

                    if (siblings.length > 1) {
                        // Multiple siblings of same tag, need index
                        const index = siblings.indexOf(current) + 1;
                        path.unshift(`${tag}[${index}]`);
                    } else {
                        // Only child of this tag type
                        path.unshift(tag);
                    }

                    current = parent;
                }

                return path.length > 0 ? '/' + path.join('/') : '';
            }

            // Helper: Find common ancestor
            function findCommonAncestor(el1, el2) {
                const ancestors1 = [];
                let current = el1;
                while (current) {
                    ancestors1.push(current);
                    current = current.parentElement;
                }

                current = el2;
                while (current) {
                    if (ancestors1.includes(current)) {
                        return current;
                    }
                    current = current.parentElement;
                }
                return null;
            }

            // Helper: Test if XPath works
            function testXPath(xpath, expectedElement) {
                try {
                    const result = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                    return result.singleNodeValue === expectedElement;
                } catch(e) {
                    return false;
                }
            }

            // Sort results by approach preference (following-sibling > parent > descendant)
            const approachPriority = { 'following-sibling': 1, 'parent': 2, 'descendant': 3 };
            return results.sort((a, b) => approachPriority[a.approach] - approachPriority[b.approach]);
        }
        """, timeout=7000)

        if not anchor_analysis or len(anchor_analysis) == 0:
            debug_log("❌ No suitable text-to-target XPath found")
            return None

        debug_log(f"   🎯 Found {len(anchor_analysis)} text-to-target XPath options")

        # Test each generated XPath in priority order
        for analysis in anchor_analysis:
            anchor_text = analysis['anchorText']
            xpath_expr = analysis['xpath']
            approach = analysis['approach']

            debug_log(f"   🔍 Testing {approach}: {xpath_expr}")

            try:
                # Create XPath locator
                if is_page_context:
                    loc = context.locator(f'xpath={xpath_expr}')
                    result = f"page.locator('xpath={xpath_expr}')"
                else:
                    loc = context.locator(f'xpath={xpath_expr}')
                    result = f"current_frame.locator('xpath={xpath_expr}')"

                # Validate the XPath works
                if await simple_validation(loc, element_data['position'], f"TextDirect({approach})"):
                    debug_log(f"🏆 Text-to-Target XPath SUCCESS ({approach}): {result}")
                    return result

            except Exception as e:
                debug_log(f"❌ XPath failed for {approach} '{anchor_text}': {str(e)}")
                continue

        debug_log("❌ No working text-to-target XPath found")
        return None

    # 🎯 MAIN EXECUTION
    try:
        debug_log("🎯 Starting locator generation with getByRole strategy...")
        debug_log("📋 Strategies: 1) getByTestId → 2) Static ID → 3) getByRole → 4) Fallback XPath")

        if check_timeout():
            debug_log("⏰ Timeout at start")
            return get_fallback_locator()

        # STEP 1: Extract element data
        debug_log("📊 STEP 1: Extracting element data...")
        element_data = await extract_comprehensive_element_data(target_locator)
        if not element_data:
            debug_log("❌ Failed to extract element data")
            return get_fallback_locator()

        if check_timeout():
            debug_log("⏰ Timeout after data extraction")
            return get_fallback_locator()

        # STEP 2: Try getByTestId strategy
        debug_log("🎯 STEP 2: Trying getByTestId strategy...")
        testid_result = await try_testid_strategy(element_data)
        if testid_result:
            debug_log(f"🎉 FINAL SUCCESS with getByTestId: {testid_result}")
            print("\n".join(debug_info))
            return testid_result

        if check_timeout():
            debug_log("⏰ Timeout after TestID strategy")
            return get_fallback_locator()

        # STEP 3: Try Static ID strategy
        debug_log("🎯 STEP 3: Trying Static ID strategy...")
        static_id_result = await try_static_id_strategy(element_data)
        if static_id_result:
            debug_log(f"🎉 FINAL SUCCESS with Static ID: {static_id_result}")
            print("\n".join(debug_info))
            return static_id_result

        debug_log("🎯 STEP 4: Trying placeholder strategy...")
        placeholder_result = await try_placeholder_strategy(element_data)
        if testid_result:
            debug_log(f"🎉 FINAL SUCCESS with getByTestId: {placeholder_result}")
            print("\n".join(debug_info))
            return placeholder_result

        debug_log("🎯 STEP 5: Trying Static getByRole strategy...")
        role_result = await try_role_strategy(element_data)
        if role_result:
            debug_log(f"🎉 FINAL SUCCESS with getByRole: {role_result}")
            print("\n".join(debug_info))
            return role_result

        # STEP 4: Try testid+getByRole combo strategy
        debug_log("🎯 STEP 7: Trying getBytestid+getByRole combo strategy...")
        combo_role_result = await try_testid_role_combinations(element_data)
        if combo_role_result:
            debug_log(f"🎉 FINAL SUCCESS with getByRole: {combo_role_result}")
            print("\n".join(debug_info))
            return combo_role_result

        debug_log("🎯 STEP 7: Trying testid_text combo strategy...")
        testid_text_combo_result = await try_testid_text_combination(element_data)
        if testid_text_combo_result:
            debug_log(f"🎉 FINAL SUCCESS with getByRole: {testid_text_combo_result}")
            print("\n".join(debug_info))
            return testid_text_combo_result

        debug_log("🎯 STEP 8: Trying testid_css_selector_combination strategy...")
        testid_css_selector_combo = await try_testid_css_selector_combination(element_data)
        if testid_css_selector_combo:
            debug_log(f"🎉 FINAL SUCCESS with testid_css_selector_combination: {testid_css_selector_combo}")
            print("\n".join(debug_info))
            return testid_css_selector_combo

        debug_log("🎯 STEP 9: Trying testid_text_filter_combination strategy...")
        testid_text_filter_combo = await try_testid_text_filter_strategy(element_data)
        if testid_text_filter_combo:
            debug_log(f"🎉 FINAL SUCCESS with testid_text_filter_combination  {testid_text_filter_combo}")
            print("\n".join(debug_info))
            return testid_text_filter_combo

        debug_log("🎯 STEP 10: Trying getByText strategy...")
        text_result = await try_text_strategy(element_data)
        if text_result:
            debug_log(f"🎉 FINAL SUCCESS with getByText: {text_result}")
            print("\n".join(debug_info))
            return text_result

        debug_log("🎯 STEP 11: Trying getBylabel strategy...")
        label_result = await try_label_strategy(element_data)
        if label_result:
            debug_log(f"🎉 FINAL SUCCESS with getByText  {label_result}")
            print("\n".join(debug_info))
            return label_result

        debug_log("🎯 STEP 11: Trying css_text_filter strategy...")
        css_text_filter_result = await try_css_text_filter_strategy(element_data)
        if css_text_filter_result:
            debug_log(f"🎉 FINAL SUCCESS with getByText  {css_text_filter_result}")
            print("\n".join(debug_info))
            return css_text_filter_result

        debug_log("🎯 STEP 11: Trying try_label_parent_combinations strategy...")
        label_parent_result = await try_label_parent_combinations(element_data)
        if label_parent_result:
            debug_log(f"🎉 FINAL SUCCESS with getByText  {label_parent_result}")
            print("\n".join(debug_info))
            return label_parent_result

        debug_log("🎯 STEP 11: Trying try_label_text_combinations strategy...")
        label_text_result = await try_label_text_combination(element_data)
        if label_text_result:
            debug_log(f"🎉 FINAL SUCCESS with getByText  {label_text_result}")
            print("\n".join(debug_info))
            return label_text_result

        debug_log("🎯 STEP 12: Trying getByAltText strategy...")
        alt_result = await try_alt_text_strategy(element_data)
        if alt_result:
            debug_log(f"🎉 FINAL SUCCESS with getByAltText: {alt_result}")
            print("\n".join(debug_info))
            return alt_result

        debug_log("🎯 STEP 13: Trying getByTitle strategy...")
        title_result = await try_title_strategy(element_data)
        if title_result:
            debug_log(f"🎉 FINAL SUCCESS with getByAltText: {title_result}")
            print("\n".join(debug_info))
            return title_result

        debug_log("🎯 STEP 14: Trying multi_attribute strategy...")
        multi_attribute_result = await try_multi_attribute_strategy(element_data)
        if multi_attribute_result:
            debug_log(f"🎉 FINAL SUCCESS with getByAltText: {multi_attribute_result}")
            print("\n".join(debug_info))
            return multi_attribute_result

        debug_log("🎯 STEP 15: Trying testid_relative_xpath_result strategy...")
        testid_relative_xpath_result = await try_testid_relative_xpath_strategy(element_data)
        if testid_relative_xpath_result:
            debug_log(f"🎉 FINAL SUCCESS with getByAltText: {testid_relative_xpath_result}")
            print("\n".join(debug_info))
            return testid_relative_xpath_result

        debug_log("🎯 STEP 15: Trying try_text_to_target_direct_xpath_strategy...")
        text_to_target_direct_xpath_result = await try_text_to_target_direct_xpath_strategy(element_data)
        if text_to_target_direct_xpath_result:
            debug_log(f"🎉 FINAL SUCCESS with getByAltText: {text_to_target_direct_xpath_result}")
            print("\n".join(debug_info))
            return text_to_target_direct_xpath_result

        # STEP 5: All strategies failed - use fallback
        debug_log("❌ All strategies failed - using fallback XPath")
        debug_log(f"🔄 FALLBACK: {get_fallback_locator()}")
        print("\n".join(debug_info))
        return get_fallback_locator()

    except Exception as e:
        debug_log(f"❌ Main execution error: {str(e)}")
        print("\n".join(debug_info))
        return get_fallback_locator()

# async def generate_playwright_locator(context, target_locator, abs_xpath, timeout_seconds=15):
#     """
#     🎯 WORKING Playwright locator generator - Simplified and Reliable
#     """
#     import re
#     import time
#     from playwright.async_api import Page
#
#     start_time = time.time()
#     debug_info = []
#
#     def debug_log(message):
#         debug_info.append(f"[{time.time() - start_time:.2f}s] {message}")
#         print(f"[{time.time() - start_time:.2f}s] {message}")  # Also print for real-time debugging
#
#     def check_timeout():
#         return (time.time() - start_time) > timeout_seconds
#
#     # Context detection
#     is_page_context = isinstance(context, Page)
#     context_name = "page" if is_page_context else "current_frame"
#
#     debug_log(f"🎯 Context type: {'Page' if is_page_context else 'FrameLocator'}")
#
#     def get_fallback_locator():
#         return f"{context_name}.locator('xpath={abs_xpath}')"
#
#     def handle_timeout():
#         debug_log("⏰ Timeout reached - returning fallback XPath")
#         return get_fallback_locator()
#
#     async def extract_element_data(target_loc):
#         debug_log("🔍 Extracting element data...")
#         try:
#             data = await target_loc.evaluate("""
#             (el) => {
#                 const attrs = {};
#                 const allAttrs = ['data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id', 'id', 'name', 'class', 'placeholder', 'aria-label', 'title', 'alt', 'type', 'role', 'href', 'value'];
#
#                 allAttrs.forEach(attr => {
#                     const val = el.getAttribute(attr);
#                     if (val && val.trim()) attrs[attr] = val.trim();
#                 });
#
#                 const role = el.getAttribute('role') ||
#                     (el.tagName.toLowerCase() === 'button' ? 'button' :
#                      el.tagName.toLowerCase() === 'a' ? 'link' :
#                      el.tagName.toLowerCase() === 'input' ? 'textbox' : null);
#
#                 const text = el.textContent?.trim() || '';
#
#                 return {
#                     tag: el.tagName.toLowerCase(),
#                     text: text,
#                     attrs: attrs,
#                     role: role,
#                     position: {
#                         x: Math.round(el.getBoundingClientRect().x),
#                         y: Math.round(el.getBoundingClientRect().y),
#                         w: Math.round(el.getBoundingClientRect().width),
#                         h: Math.round(el.getBoundingClientRect().height)
#                     }
#                 };
#             }
#             """, timeout=5000)
#
#             debug_log(
#                 f"✅ Element data: Tag={data.get('tag')}, Role={data.get('role')}, Attrs={list(data.get('attrs', {}).keys())}")
#             return data
#         except Exception as e:
#             debug_log(f"❌ Element data extraction failed: {str(e)}")
#             return None
#
#     # 🎯 SIMPLE BUT EFFECTIVE VALIDATION
#     async def simple_validation(test_locator, target_position, strategy_name):
#         """Simple 3-step validation: Count → Position → Success"""
#         try:
#             debug_log(f"🔍 Validating {strategy_name}...")
#
#             # Step 1: Count check
#             count = await test_locator.count()
#             debug_log(f"   {strategy_name}: Found {count} elements")
#
#             if count == 0:
#                 debug_log(f"❌ {strategy_name}: No elements")
#                 return False
#             elif count > 10:
#                 debug_log(f"❌ {strategy_name}: Too many elements ({count})")
#                 return False
#
#             # Step 2: Position check
#             try:
#
#                 found_pos = await test_locator.first.evaluate("""
#                 (el) => ({
#                     x: Math.round(el.getBoundingClientRect().x),
#                     y: Math.round(el.getBoundingClientRect().y),
#                     w: Math.round(el.getBoundingClientRect().width),
#                     h: Math.round(el.getBoundingClientRect().height)
#                 })
#                 """, timeout=2000)
#
#                 pos_diff_x = abs(found_pos['x'] - target_position['x'])
#                 pos_diff_y = abs(found_pos['y'] - target_position['y'])
#
#                 if pos_diff_x <= 20 and pos_diff_y <= 20:
#                     debug_log("✅ Validation passed!")
#                     return True
#
#             except Exception as e:
#                 debug_log(f"❌ {strategy_name}: Position check failed: {str(e)}")
#                 return False
#
#         except Exception as e:
#             debug_log(f"❌ {strategy_name}: Validation failed: {str(e)}")
#             return False
#
#     def escape_quotes(text):
#         return str(text).replace("'", "\\'").replace('"', '\\"') if text else ""
#
#     def is_static_id(id_value):
#         if not id_value or len(id_value) < 2:
#             return False
#         dynamic_patterns = dynamic_patterns = [
#         # Long consecutive digits
#         r'\d{4,}',
#
#         # Hex strings
#         r'[a-f0-9]{8,}',
#
#         # Known dynamic keywords
#         r'(temp|gen|auto|uuid|guid)',
#
#         # Pure numbers
#         r'^[0-9]+\$',
#
#         # 🔥 Ends with numbers (most common dynamic pattern)
#         r'-\d+\$',               # "card-12"
#         r'_\d+\$',               # "element_45"
#         r'\d+\$',                # "item123" (no separator)
#
#         # 🔥 Common framework patterns
#         r'^(kib|react|mui|ant|el)-.*-\d+\$',
#
#         # 🔥 Contains incremental patterns
#         r'(card|item|element|row|col|section|panel|tab|button|input|div|component|widget)-?\d+',
#
#         # 🔥 Number sandwich patterns
#         r'\w+-\d+-\w+',         # "prefix-123-suffix"
#         r'\w+_\d+_\w+',         # "prefix_123_suffix"
#
#         # 🔥 UUID-like patterns
#         r'^[a-f0-9-]{32,}\$',    # Long hex with dashes
#     ]
#         return not any(re.search(pattern, id_value.lower()) for pattern in dynamic_patterns)
#
#     # 🎯 STRATEGY FUNCTIONS - SIMPLIFIED
#     async def try_testid_strategy(element_data):
#         debug_log("🚀 Trying TestID strategy...")
#         test_attributes = ['data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id']
#
#         for attr in test_attributes:
#             testid_value = element_data['attrs'].get(attr)
#             if testid_value:
#                 try:
#                     debug_log(f"   Testing TestID: {testid_value}")
#                     loc = context.get_by_test_id(testid_value)
#                     if await simple_validation(loc, element_data['position'], f"TestID({testid_value})"):
#                         result = f"page.getByTestId('{escape_quotes(testid_value)}')" if is_page_context else f"current_frame.getByTestId('{escape_quotes(testid_value)}')"
#                         debug_log(f"🏆 TestID SUCCESS: {result}")
#                         return result
#                 except Exception as e:
#                     debug_log(f"❌ TestID error: {str(e)}")
#         return None
#
#     async def try_static_id_strategy(element_data):
#         debug_log("🚀 Trying Static ID strategy...")
#         id_val = element_data['attrs'].get('id')
#         if id_val and is_static_id(id_val):
#             try:
#                 debug_log(f"   Testing Static ID: {id_val}")
#                 loc = context.locator(f'#{id_val}')
#                 if await simple_validation(loc, element_data['position'], f"StaticID({id_val})"):
#                     result = f"page.locator('#{escape_quotes(id_val)}')" if is_page_context else f"current_frame.locator('#{escape_quotes(id_val)}')"
#                     debug_log(f"🏆 Static ID SUCCESS: {result}")
#                     return result
#             except Exception as e:
#                 debug_log(f"❌ Static ID error: {str(e)}")
#         return None
#
#     async def try_role_strategy(element_data):
#         """
#         Role strategy that generates getByRole locators with accessible names only
#         """
#         debug_log("🚀 Trying Role strategy...")
#
#         # Determine the role (explicit or implicit)
#         role = determine_element_role(element_data)
#
#         if not role:
#             debug_log("   No role could be determined for this element")
#             return None
#
#         try:
#             debug_log(f"   Detected Role: {role}")
#
#             # Get accessible name
#             accessible_name = extract_accessible_name(element_data)
#
#             if accessible_name:
#                 # Try role with name
#                 debug_log(f"   Testing role with name: {accessible_name}")
#                 name_locator = context.get_by_role(role, name=accessible_name)
#
#                 if await simple_validation(name_locator, element_data['position'],
#                                            f"Role({role}, name='{accessible_name}')"):
#                     result = f"page.getByRole('{role}', {{ name: '{accessible_name}' }})"
#                     if not is_page_context:
#                         result = result.replace('page.', 'current_frame.')
#                     debug_log(f"🏆 Role SUCCESS: {result}")
#                     return result
#             else:
#                 debug_log("   No accessible name found, skipping role strategy")
#
#         except Exception as e:
#             debug_log(f"❌ Role error: {str(e)}")
#
#         return None
#
#     def determine_element_role(element_data):
#         """
#         Determine the role of an element from explicit role attribute or implicit role
#         """
#         # First check for explicit role attribute
#         explicit_role = element_data.get('role')
#         if explicit_role:
#             return explicit_role
#
#         # If no explicit role, determine implicit role
#         return get_implicit_role(element_data)
#
#     def get_implicit_role(element_data):
#         """
#         Determine implicit role based on HTML element and attributes
#         """
#         tag_name = element_data.get('tag_name', '').lower()
#         element_type = element_data.get('type', '').lower()
#
#         # Input elements - role depends on type attribute
#         if tag_name == 'input':
#             input_role_mapping = {
#                 'button': 'button',
#                 'submit': 'button',
#                 'reset': 'button',
#                 'checkbox': 'checkbox',
#                 'radio': 'radio',
#                 'range': 'slider',
#                 'number': 'spinbutton',
#                 'search': 'searchbox',
#                 'text': 'textbox',
#                 'email': 'textbox',
#                 'password': 'textbox',
#                 'tel': 'textbox',
#                 'url': 'textbox'
#             }
#             return input_role_mapping.get(element_type, 'textbox')
#
#         # Other elements
#         elif tag_name == 'button':
#             return 'button'
#         elif tag_name == 'a' and element_data.get('href'):
#             return 'link'
#         elif tag_name == 'img' and element_data.get('alt') is not None:
#             return 'img'
#         elif tag_name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
#             return 'heading'
#         elif tag_name == 'select':
#             return 'combobox'
#         elif tag_name == 'textarea':
#             return 'textbox'
#
#         return None
#
#     def extract_accessible_name(element_data):
#         """
#         Extract accessible name from various sources in priority order
#         """
#         # Priority order for accessible name
#         name_sources = [
#             ('aria-label', 'aria-label'),  # From aria-label
#             ('label_text', 'label text'),  # From associated label
#             ('alt', 'alt text'),  # From alt text (images)
#             ('text_content', 'element content'),  # From element content
#             ('title', 'title'),  # From title attribute
#             ('value', 'value'),  # From value (buttons)
#             ('placeholder', 'placeholder')  # From placeholder
#         ]
#
#         for source_key, source_desc in name_sources:
#             print("source_key:", source_key)
#             name = element_data.get(source_key)
#             if name and name.strip():
#                 clean_name = name.strip()
#                 debug_log(f"   Found accessible name from {source_desc}: '{clean_name}'")
#                 return clean_name
#
#         debug_log("   No accessible name found")
#         return None
#
#     async def try_text_strategy(element_data):
#         debug_log("🚀 Trying Text strategy...")
#         text_val = element_data.get('text', '').strip()
#         if text_val and len(text_val) >= 2 and len(text_val) <= 50:
#             try:
#                 debug_log(f"   Testing Text: '{text_val}'")
#                 loc = context.get_by_text(text_val)
#                 if await simple_validation(loc, element_data['position'], f"Text({text_val[:20]})"):
#                     result = f"page.getByText('{escape_quotes(text_val)}')" if is_page_context else f"current_frame.getByText('{escape_quotes(text_val)}')"
#                     debug_log(f"🏆 Text SUCCESS: {result}")
#                     return result
#             except Exception as e:
#                 debug_log(f"❌ Text error: {str(e)}")
#         return None
#
#     # 🎯 MAIN EXECUTION - SIMPLE AND CLEAN
#     try:
#         debug_log("🎯 Starting locator generation...")
#
#         if check_timeout():
#             return handle_timeout()
#
#         # Extract element data
#         element_data = await extract_element_data(target_locator)
#         if not element_data:
#             debug_log("❌ Could not extract element data")
#             return handle_timeout()
#
#         if check_timeout():
#             return handle_timeout()
#
#         # Try strategies in order
#         strategies = [
#             try_testid_strategy,
#             try_static_id_strategy,
#             try_role_strategy,
#             try_text_strategy
#         ]
#
#         for strategy_func in strategies:
#             if check_timeout():
#                 return handle_timeout()
#
#             try:
#                 result = await strategy_func(element_data)
#                 if result:
#                     debug_log(f"🎉 FINAL SUCCESS: {result}")
#                     print("\n".join(debug_info))
#                     return result
#             except Exception as e:
#                 debug_log(f"❌ Strategy error: {str(e)}")
#                 continue
#
#         debug_log("❌ All strategies failed - using fallback")
#         print("\n".join(debug_info))
#         return handle_timeout()
#
#     except Exception as e:
#         debug_log(f"❌ Main execution error: {str(e)}")
#         print("\n".join(debug_info))
#         return handle_timeout()


# async def generate_playwright_locator(context, target_locator, abs_xpath, timeout_seconds=5):
#     """
#     Explicit type checking for Page vs FrameLocator - Better approach!
#     """
#     import re
#     import time
#     import asyncio
#     from playwright.async_api import Page
#
#     start_time = time.time()
#     debug_info = []
#
#     def debug_log(message):
#         debug_info.append(f"[{time.time() - start_time:.2f}s] {message}")
#
#     def check_timeout():
#         return (time.time() - start_time) > timeout_seconds
#
#     # ✅ BETTER: Explicit type checking and appropriate locator strings
#     is_page_context = isinstance(context, Page)
#     context_name = "page" if is_page_context else "current_frame"
#
#     debug_log(f"🎯 Context type: {'Page' if is_page_context else 'FrameLocator'}")
#
#     def get_fallback_locator():
#         return f"{context_name}.locator('xpath={abs_xpath}')"
#
#     async def extract_comprehensive_element_data(target_loc):
#         debug_log("🔍 Starting element data extraction...")
#         try:
#             data = await target_loc.evaluate("""
#             (el) => {
#                 try {
#                     const attrs = {};
#                     const allAttrs = [
#                         'data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id',
#                         'id', 'name', 'class', 'placeholder', 'aria-label', 'title', 'alt', 'type', 'role', 'href', 'value'
#                     ];
#
#                     allAttrs.forEach(attr => {
#                         const val = el.getAttribute(attr);
#                         if (val && val.trim()) attrs[attr] = val.trim();
#                     });
#
#                     const role = el.getAttribute('role') ||
#                         (el.tagName.toLowerCase() === 'button' ? 'button' :
#                          el.tagName.toLowerCase() === 'a' ? 'link' :
#                          el.tagName.toLowerCase() === 'input' ? 'textbox' : null);
#
#                     const text = el.textContent?.trim() || '';
#                     const accessibleNames = [];
#                     if (attrs['aria-label']) accessibleNames.push(attrs['aria-label']);
#                     if (attrs.title) accessibleNames.push(attrs.title);
#                     if (text && text.length <= 50) accessibleNames.push(text);
#                     if (attrs.placeholder) accessibleNames.push(attrs.placeholder);
#
#                     return {
#                         tag: el.tagName.toLowerCase(),
#                         text: text,
#                         attrs: attrs,
#                         role: role,
#                         accessibleNames: [...new Set(accessibleNames)].filter(n => n && n.length >= 1),
#                         position: {
#                             x: Math.round(el.getBoundingClientRect().x),
#                             y: Math.round(el.getBoundingClientRect().y),
#                             w: Math.round(el.getBoundingClientRect().width),
#                             h: Math.round(el.getBoundingClientRect().height)
#                         }
#                     };
#                 } catch(e) {
#                     return {error: e.message};
#                 }
#             }
#             """, timeout=3000)
#
#             if data and data.get('error'):
#                 debug_log(f"❌ Element data extraction failed: {data['error']}")
#                 return None
#
#             debug_log(f"✅ Element data extracted successfully:")
#             debug_log(f"   Tag: {data.get('tag')}")
#             debug_log(f"   Role: {data.get('role')}")
#             debug_log(f"   Attrs: {list(data.get('attrs', {}).keys())}")
#
#             return data
#         except Exception as e:
#             debug_log(f"❌ Element data extraction exception: {str(e)}")
#             return None
#
#     async def validate_locator_super_relaxed(test_locator, target_position):
#         try:
#             debug_log(f"🔍 Validating locator...")
#             count = await test_locator.count()
#             debug_log(f"   Count: {count}")
#
#             if count != 1:
#                 debug_log(f"❌ Validation failed: Expected 1 element, found {count}")
#                 return False
#
#             try:
#                 found_pos = await test_locator.first.evaluate("""
#                 (el) => ({
#                     x: Math.round(el.getBoundingClientRect().x),
#                     y: Math.round(el.getBoundingClientRect().y),
#                     w: Math.round(el.getBoundingClientRect().width),
#                     h: Math.round(el.getBoundingClientRect().height)
#                 })
#                 """, timeout=2000)
#
#                 pos_diff_x = abs(found_pos['x'] - target_position['x'])
#                 pos_diff_y = abs(found_pos['y'] - target_position['y'])
#
#                 if pos_diff_x <= 20 and pos_diff_y <= 20:
#                     debug_log("✅ Validation passed!")
#                     return True
#                 else:
#                     debug_log(f"❌ Validation failed: Position difference too large")
#                     return False
#
#             except Exception as e:
#                 debug_log(f"❌ Position validation error: {str(e)}")
#                 return False
#
#         except Exception as e:
#             debug_log(f"❌ Validation exception: {str(e)}")
#             return False
#
#     def escape_quotes(text):
#         return str(text).replace("'", "\\'").replace('"', '\\"') if text else ""
#
#     # ✅ BETTER: Explicit handling for Page context
#     async def handle_page_context(element_data):
#         debug_log("🚀 Handling PAGE context...")
#
#         # Phase 1: TestID for Page
#         test_attributes = ['data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id']
#         for attr in test_attributes:
#             if element_data['attrs'].get(attr):
#                 testid_value = element_data['attrs'][attr]
#                 debug_log(f"   Testing page.getByTestId('{testid_value}')")
#                 try:
#                     loc = context.get_by_test_id(testid_value)
#                     if await validate_locator_super_relaxed(loc, element_data['position']):
#                         result = f"page.getByTestId('{escape_quotes(testid_value)}')"
#                         debug_log(f"✅ PAGE TestID SUCCESS: {result}")
#                         return result
#                 except Exception as e:
#                     debug_log(f"❌ Page TestID error: {str(e)}")
#
#         # Phase 2: ID for Page
#         id_val = element_data['attrs'].get('id')
#         if id_val and len(id_val) >= 2:
#             debug_log(f"   Testing page.locator('#{id_val}')")
#             try:
#                 loc = context.locator(f'#{id_val}')
#                 if await validate_locator_super_relaxed(loc, element_data['position']):
#                     result = f"page.locator('#{escape_quotes(id_val)}')"
#                     debug_log(f"✅ PAGE ID SUCCESS: {result}")
#                     return result
#             except Exception as e:
#                 debug_log(f"❌ Page ID error: {str(e)}")
#
#         # Phase 3: Role for Page
#         if element_data.get('role'):
#             role = element_data['role']
#             # Try with accessible names first
#             if element_data.get('accessibleNames'):
#                 for name in element_data['accessibleNames'][:2]:
#                     debug_log(f"   Testing page.getByRole('{role}', name='{name}')")
#                     try:
#                         loc = context.get_by_role(role, name=name)
#                         if await validate_locator_super_relaxed(loc, element_data['position']):
#                             result = f"page.getByRole('{role}', {{name: '{escape_quotes(name)}'}})"
#                             debug_log(f"✅ PAGE Role+Name SUCCESS: {result}")
#                             return result
#                     except Exception as e:
#                         debug_log(f"❌ Page Role+Name error: {str(e)}")
#
#             # Try role without name
#             debug_log(f"   Testing page.getByRole('{role}')")
#             try:
#                 loc = context.get_by_role(role)
#                 if await validate_locator_super_relaxed(loc, element_data['position']):
#                     result = f"page.getByRole('{role}')"
#                     debug_log(f"✅ PAGE Role SUCCESS: {result}")
#                     return result
#             except Exception as e:
#                 debug_log(f"❌ Page Role error: {str(e)}")
#
#         debug_log("❌ All PAGE strategies failed")
#         return None
#
#     # ✅ BETTER: Explicit handling for FrameLocator context
#     async def handle_frame_context(element_data):
#         debug_log("🚀 Handling FRAME context...")
#
#         # Phase 1: TestID for Frame
#         test_attributes = ['data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id']
#         for attr in test_attributes:
#             if element_data['attrs'].get(attr):
#                 testid_value = element_data['attrs'][attr]
#                 debug_log(f"   Testing current_frame.getByTestId('{testid_value}')")
#                 try:
#                     loc = context.get_by_test_id(testid_value)
#                     if await validate_locator_super_relaxed(loc, element_data['position']):
#                         result = f"current_frame.getByTestId('{escape_quotes(testid_value)}')"
#                         debug_log(f"✅ FRAME TestID SUCCESS: {result}")
#                         return result
#                 except Exception as e:
#                     debug_log(f"❌ Frame TestID error: {str(e)}")
#
#         # Phase 2: ID for Frame
#         id_val = element_data['attrs'].get('id')
#         if id_val and len(id_val) >= 2:
#             debug_log(f"   Testing current_frame.locator('#{id_val}')")
#             try:
#                 loc = context.locator(f'#{id_val}')
#                 if await validate_locator_super_relaxed(loc, element_data['position']):
#                     result = f"current_frame.locator('#{escape_quotes(id_val)}')"
#                     debug_log(f"✅ FRAME ID SUCCESS: {result}")
#                     return result
#             except Exception as e:
#                 debug_log(f"❌ Frame ID error: {str(e)}")
#
#         # Phase 3: Role for Frame
#         if element_data.get('role'):
#             role = element_data['role']
#             # Try with accessible names first
#             if element_data.get('accessibleNames'):
#                 for name in element_data['accessibleNames'][:2]:
#                     debug_log(f"   Testing current_frame.getByRole('{role}', name='{name}')")
#                     try:
#                         loc = context.get_by_role(role, name=name)
#                         if await validate_locator_super_relaxed(loc, element_data['position']):
#                             result = f"current_frame.getByRole('{role}', {{name: '{escape_quotes(name)}'}})"
#                             debug_log(f"✅ FRAME Role+Name SUCCESS: {result}")
#                             return result
#                     except Exception as e:
#                         debug_log(f"❌ Frame Role+Name error: {str(e)}")
#
#             # Try role without name
#             debug_log(f"   Testing current_frame.getByRole('{role}')")
#             try:
#                 loc = context.get_by_role(role)
#                 if await validate_locator_super_relaxed(loc, element_data['position']):
#                     result = f"current_frame.getByRole('{role}')"
#                     debug_log(f"✅ FRAME Role SUCCESS: {result}")
#                     return result
#             except Exception as e:
#                 debug_log(f"❌ Frame Role error: {str(e)}")
#
#         debug_log("❌ All FRAME strategies failed")
#         return None
#
#     # MAIN EXECUTION WITH EXPLICIT TYPE HANDLING
#     try:
#         debug_log("🎯 Starting locator generation...")
#
#         element_data = await extract_comprehensive_element_data(target_locator)
#         if not element_data:
#             debug_log("❌ Element data extraction failed completely")
#             print("\n".join(debug_info))
#             return get_fallback_locator()
#
#         # ✅ EXPLICIT TYPE-BASED HANDLING
#         if is_page_context:
#             result = await handle_page_context(element_data)
#         else:
#             result = await handle_frame_context(element_data)
#
#         if result:
#             debug_log(f"🎉 FINAL SUCCESS: {result}")
#             print("\n".join(debug_info))
#             return result
#         else:
#             debug_log("❌ ALL STRATEGIES FAILED - Using fallback")
#             print("\n".join(debug_info))
#             return get_fallback_locator()
#
#     except Exception as e:
#         debug_log(f"❌ Main execution error: {str(e)}")
#         print("\n".join(debug_info))
#         return get_fallback_locator()

# async def generate_playwright_locator(page, target_locator, abs_xpath, timeout_seconds=5):
#     """
#     DEBUG VERSION - Shows exactly why it's falling back to absolute XPath
#     """
#     import re
#     import time
#     import asyncio
#
#     start_time = time.time()
#     debug_info = []
#
#     def debug_log(message):
#         debug_info.append(f"[{time.time() - start_time:.2f}s] {message}")
#
#     def check_timeout():
#         return (time.time() - start_time) > timeout_seconds
#
#     def get_fallback_locator():
#         return f"page.locator('xpath={abs_xpath}')"
#
#     async def extract_comprehensive_element_data(target_loc):
#         debug_log("🔍 Starting element data extraction...")
#         try:
#             data = await target_loc.evaluate("""
#             (el) => {
#                 try {
#                     const attrs = {};
#                     const allAttrs = [
#                         'data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id',
#                         'id', 'name', 'class', 'placeholder', 'aria-label', 'title', 'alt', 'type', 'role', 'href', 'value'
#                     ];
#
#                     allAttrs.forEach(attr => {
#                         const val = el.getAttribute(attr);
#                         if (val && val.trim()) attrs[attr] = val.trim();
#                     });
#
#                     const role = el.getAttribute('role') ||
#                         (el.tagName.toLowerCase() === 'button' ? 'button' :
#                          el.tagName.toLowerCase() === 'a' ? 'link' :
#                          el.tagName.toLowerCase() === 'input' ? 'textbox' : null);
#
#                     const text = el.textContent?.trim() || '';
#                     const accessibleNames = [];
#                     if (attrs['aria-label']) accessibleNames.push(attrs['aria-label']);
#                     if (attrs.title) accessibleNames.push(attrs.title);
#                     if (text && text.length <= 50) accessibleNames.push(text);
#                     if (attrs.placeholder) accessibleNames.push(attrs.placeholder);
#
#                     return {
#                         tag: el.tagName.toLowerCase(),
#                         text: text,
#                         attrs: attrs,
#                         role: role,
#                         accessibleNames: [...new Set(accessibleNames)].filter(n => n && n.length >= 1),
#                         position: {
#                             x: Math.round(el.getBoundingClientRect().x),
#                             y: Math.round(el.getBoundingClientRect().y),
#                             w: Math.round(el.getBoundingClientRect().width),
#                             h: Math.round(el.getBoundingClientRect().height)
#                         }
#                     };
#                 } catch(e) {
#                     return {error: e.message};
#                 }
#             }
#             """, timeout=3000)
#
#             if data and data.get('error'):
#                 debug_log(f"❌ Element data extraction failed: {data['error']}")
#                 return None
#
#             debug_log(f"✅ Element data extracted successfully:")
#             debug_log(f"   Tag: {data.get('tag')}")
#             debug_log(f"   Role: {data.get('role')}")
#             debug_log(f"   Text: '{data.get('text', '')[:30]}{'...' if len(data.get('text', '')) > 30 else ''}'")
#             debug_log(f"   Attrs: {list(data.get('attrs', {}).keys())}")
#             debug_log(f"   Position: {data.get('position')}")
#             debug_log(f"   AccessibleNames: {data.get('accessibleNames')}")
#
#             return data
#         except Exception as e:
#             debug_log(f"❌ Element data extraction exception: {str(e)}")
#             return None
#
#     async def validate_locator_super_relaxed(test_locator, target_position):
#         """SUPER RELAXED validation for testing"""
#         try:
#             debug_log(f"🔍 Validating locator...")
#
#             count = await test_locator.count()
#             debug_log(f"   Count: {count}")
#
#             if count != 1:
#                 debug_log(f"❌ Validation failed: Expected 1 element, found {count}")
#                 return False
#
#             try:
#                 found_pos = await test_locator.first.evaluate("""
#                 (el) => ({
#                     x: Math.round(el.getBoundingClientRect().x),
#                     y: Math.round(el.getBoundingClientRect().y),
#                     w: Math.round(el.getBoundingClientRect().width),
#                     h: Math.round(el.getBoundingClientRect().height)
#                 })
#                 """, timeout=2000)
#
#                 debug_log(f"   Target position: {target_position}")
#                 debug_log(f"   Found position: {found_pos}")
#
#                 # SUPER RELAXED: ±20 pixels, ±50% size tolerance
#                 pos_diff_x = abs(found_pos['x'] - target_position['x'])
#                 pos_diff_y = abs(found_pos['y'] - target_position['y'])
#
#                 debug_log(f"   Position difference: X={pos_diff_x}, Y={pos_diff_y}")
#
#                 if pos_diff_x <= 20 and pos_diff_y <= 20:
#                     debug_log("✅ Validation passed!")
#                     return True
#                 else:
#                     debug_log(f"❌ Validation failed: Position difference too large")
#                     return False
#
#             except Exception as e:
#                 debug_log(f"❌ Position validation error: {str(e)}")
#                 return False
#
#         except Exception as e:
#             debug_log(f"❌ Validation exception: {str(e)}")
#             return False
#
#     def escape_quotes(text):
#         return str(text).replace("'", "\\'").replace('"', '\\"') if text else ""
#
#     async def phase1_testid_debug(element_data):
#         debug_log("🚀 PHASE 1: Testing TestID strategies...")
#
#         if not element_data or not element_data.get('attrs'):
#             debug_log("❌ Phase 1: No element data or attrs")
#             return None
#
#         test_attributes = ['data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id']
#
#         found_testids = []
#         for attr in test_attributes:
#             if element_data['attrs'].get(attr):
#                 found_testids.append(f"{attr}='{element_data['attrs'][attr]}'")
#
#         if found_testids:
#             debug_log(f"   Found test IDs: {found_testids}")
#         else:
#             debug_log("   No test IDs found")
#             return None
#
#         for attr in test_attributes:
#             if element_data['attrs'].get(attr):
#                 testid_value = element_data['attrs'][attr]
#                 debug_log(f"   Testing {attr}: '{testid_value}'")
#
#                 try:
#                     loc = page.get_by_test_id(testid_value)
#                     if await validate_locator_super_relaxed(loc, element_data['position']):
#                         result = f"page.getByTestId('{escape_quotes(testid_value)}')"
#                         debug_log(f"✅ Phase 1 SUCCESS: {result}")
#                         return result
#                     else:
#                         debug_log(f"❌ {attr} validation failed")
#                 except Exception as e:
#                     debug_log(f"❌ {attr} error: {str(e)}")
#
#         debug_log("❌ Phase 1: All testid strategies failed")
#         return None
#
#     async def phase2_id_debug(element_data):
#         debug_log("🚀 PHASE 2: Testing ID strategy...")
#
#         if not element_data or not element_data.get('attrs'):
#             debug_log("❌ Phase 2: No element data")
#             return None
#
#         id_val = element_data['attrs'].get('id')
#         if not id_val:
#             debug_log("❌ Phase 2: No ID found")
#             return None
#
#         debug_log(f"   Found ID: '{id_val}'")
#
#         # VERY RELAXED ID check - accept almost anything
#         if len(id_val) >= 2:
#             debug_log(f"   ID accepted (length: {len(id_val)})")
#             try:
#                 loc = page.locator(f'#{id_val}')
#                 if await validate_locator_super_relaxed(loc, element_data['position']):
#                     result = f"page.locator('#{escape_quotes(id_val)}')"
#                     debug_log(f"✅ Phase 2 SUCCESS: {result}")
#                     return result
#                 else:
#                     debug_log(f"❌ ID validation failed")
#             except Exception as e:
#                 debug_log(f"❌ ID error: {str(e)}")
#         else:
#             debug_log(f"❌ ID rejected: too short ({len(id_val)} chars)")
#
#         debug_log("❌ Phase 2: ID strategy failed")
#         return None
#
#     async def phase3_role_debug(element_data):
#         debug_log("🚀 PHASE 3: Testing Role strategy...")
#
#         if not element_data:
#             debug_log("❌ Phase 3: No element data")
#             return None
#
#         if not element_data.get('role'):
#             debug_log("❌ Phase 3: No role found")
#             return None
#
#         role = element_data['role']
#         debug_log(f"   Found role: '{role}'")
#
#         # Try role with names
#         if element_data.get('accessibleNames'):
#             debug_log(f"   Accessible names: {element_data['accessibleNames']}")
#
#             for name in element_data['accessibleNames'][:2]:
#                 debug_log(f"   Testing role '{role}' with name '{name}'")
#                 try:
#                     loc = page.get_by_role(role, name=name)
#                     if await validate_locator_super_relaxed(loc, element_data['position']):
#                         result = f"page.getByRole('{role}', {{name: '{escape_quotes(name)}'}})"
#                         debug_log(f"✅ Phase 3 SUCCESS: {result}")
#                         return result
#                     else:
#                         debug_log(f"❌ Role+name validation failed")
#                 except Exception as e:
#                     debug_log(f"❌ Role+name error: {str(e)}")
#
#         # Try role without name
#         debug_log(f"   Testing role '{role}' without name")
#         try:
#             loc = page.get_by_role(role)
#             if await validate_locator_super_relaxed(loc, element_data['position']):
#                 result = f"page.getByRole('{role}')"
#                 debug_log(f"✅ Phase 3 SUCCESS: {result}")
#                 return result
#             else:
#                 debug_log(f"❌ Role-only validation failed")
#         except Exception as e:
#             debug_log(f"❌ Role-only error: {str(e)}")
#
#         debug_log("❌ Phase 3: Role strategy failed")
#         return None
#
#     # MAIN EXECUTION WITH DEBUG
#     try:
#         debug_log("🎯 Starting locator generation...")
#
#         element_data = await extract_comprehensive_element_data(target_locator)
#
#         if not element_data:
#             debug_log("❌ Element data extraction failed completely")
#             print("\n".join(debug_info))  # Print debug info
#             return get_fallback_locator()
#
#         # Try each phase with debug
#         phases = [
#             ("TestID", phase1_testid_debug),
#             ("ID", phase2_id_debug),
#             ("Role", phase3_role_debug)
#         ]
#
#         for phase_name, phase_func in phases:
#             if check_timeout():
#                 debug_log(f"⏱️ Timeout reached before {phase_name}")
#                 break
#
#             try:
#                 result = await phase_func(element_data)
#                 if result:
#                     debug_log(f"🎉 FINAL SUCCESS: {result}")
#                     print("\n".join(debug_info))  # Print debug info
#                     return result
#             except Exception as e:
#                 debug_log(f"❌ {phase_name} exception: {str(e)}")
#
#         debug_log("❌ ALL PHASES FAILED - Using fallback")
#         print("\n".join(debug_info))  # Print debug info
#         return get_fallback_locator()
#
#     except Exception as e:
#         debug_log(f"❌ Main execution error: {str(e)}")
#         print("\n".join(debug_info))  # Print debug info
#         return get_fallback_locator()
# async def generate_playwright_locator(page, target_locator, abs_xpath, timeout_seconds=5):
# """
# Complete function with relaxed validation for better success rates
# Returns STRING representation of correct Playwright locators
# GUARANTEED: Never returns None, always returns a locator string
# """
# import re
# import time
# import asyncio
#
# start_time = time.time()
#
# def check_timeout():
#     return (time.time() - start_time) > timeout_seconds
#
# def get_fallback_locator():
#     return f"page.locator('xpath={abs_xpath}')"
#
# async def extract_comprehensive_element_data(target_loc):
#     try:
#         return await target_loc.evaluate("""
#         (el) => {
#             const attrs = {};
#             const allAttrs = [
#                 'data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id',
#                 'id', 'name', 'class', 'placeholder', 'aria-label', 'aria-labelledby',
#                 'title', 'alt', 'type', 'role', 'href', 'value', 'for'
#             ];
#
#             allAttrs.forEach(attr => {
#                 const val = el.getAttribute(attr);
#                 if (val && val.trim()) attrs[attr] = val.trim();
#             });
#
#             const getRole = () => {
#                 if (attrs.role) return attrs.role;
#                 const tag = el.tagName.toLowerCase();
#                 const type = (attrs.type || '').toLowerCase();
#
#                 if (tag === 'button') return 'button';
#                 if (tag === 'a') return attrs.href ? 'link' : null;
#                 if (tag === 'input') {
#                     const roleMap = {
#                         'button': 'button', 'submit': 'button', 'reset': 'button',
#                         'checkbox': 'checkbox', 'radio': 'radio', 'search': 'searchbox',
#                         'email': 'textbox', 'password': 'textbox', 'tel': 'textbox'
#                     };
#                     return roleMap[type] || 'textbox';
#                 }
#                 if (tag === 'select') return 'combobox';
#                 if (tag === 'textarea') return 'textbox';
#                 if (tag === 'img') return 'img';
#                 if (['h1','h2','h3','h4','h5','h6'].includes(tag)) return 'heading';
#                 return null;
#             };
#
#             const getAccessibleNames = () => {
#                 const names = [];
#                 if (attrs['aria-label']) names.push(attrs['aria-label']);
#                 if (attrs.title) names.push(attrs.title);
#                 const text = el.textContent?.trim();
#                 if (text && text.length <= 50) {
#                     names.push(text);
#                     if (text.length > 10) names.push(text.substring(0, 30));
#                 }
#                 if (attrs.value) names.push(attrs.value);
#                 if (attrs.alt) names.push(attrs.alt);
#                 if (attrs.placeholder) names.push(attrs.placeholder);
#                 return [...new Set(names)].filter(n => n && n.length >= 1);
#             };
#
#             const getParentContext = () => {
#                 const parentData = {testids: [], roles: [], ids: []};
#
#                 let parent = el.parentElement;
#                 for (let i = 0; i < 3 && parent; i++) {
#                     const testIdAttrs = ['data-testid', 'data-test', 'data-cy'];
#                     for (const attr of testIdAttrs) {
#                         const val = parent.getAttribute(attr);
#                         if (val && val.trim() && !parentData.testids.includes(val.trim())) {
#                             parentData.testids.push(val.trim());
#                         }
#                     }
#
#                     const role = parent.getAttribute('role');
#                     if (role && !parentData.roles.includes(role)) {
#                         parentData.roles.push(role);
#                     }
#
#                     const tag = parent.tagName.toLowerCase();
#                     if (tag === 'form' && !parentData.roles.includes('form')) {
#                         parentData.roles.push('form');
#                     }
#                     if (tag === 'nav' && !parentData.roles.includes('navigation')) {
#                         parentData.roles.push('navigation');
#                     }
#
#                     const id = parent.id;
#                     if (id && id.length >= 3 && id.length <= 25 && !parentData.ids.includes(id)) {
#                         parentData.ids.push(id);
#                     }
#
#                     parent = parent.parentElement;
#                 }
#
#                 return parentData;
#             };
#
#             const getXPathContext = () => {
#                 const xpathData = {
#                     idPatterns: null,
#                     stableClasses: [],
#                     textPatterns: []
#                 };
#
#                 if (attrs.id) {
#                     const id = attrs.id;
#                     xpathData.idPatterns = {
#                         original: id,
#                         prefix: id.split(/[_-]/)[0] || '',
#                         suffix: id.split(/[_-]/).pop() || '',
#                         parts: id.split(/[_-]/),
#                         isDynamic: /\\d{3,}|[a-f0-9]{8,}/.test(id)
#                     };
#                 }
#
#                 if (attrs.class) {
#                     const classes = attrs.class.split(' ');
#                     xpathData.stableClasses = classes.filter(cls =>
#                         cls.length >= 4 &&
#                         !(/\\d{3,}/.test(cls)) &&
#                         !(/[a-f0-9]{6,}/.test(cls))
#                     );
#                 }
#
#                 const text = el.textContent?.trim();
#                 if (text) {
#                     xpathData.textPatterns = [
#                         text,
#                         text.substring(0, 20),
#                         text.split(' ')[0]
#                     ].filter(t => t && t.length >= 3);
#                 }
#
#                 return xpathData;
#             };
#
#             return {
#                 tag: el.tagName.toLowerCase(),
#                 text: el.textContent?.trim() || '',
#                 attrs: attrs,
#                 role: getRole(),
#                 accessibleNames: getAccessibleNames(),
#                 parentContext: getParentContext(),
#                 xpathContext: getXPathContext(),
#                 position: {
#                     x: Math.round(el.getBoundingClientRect().x),
#                     y: Math.round(el.getBoundingClientRect().y),
#                     w: Math.round(el.getBoundingClientRect().width),
#                     h: Math.round(el.getBoundingClientRect().height)
#                 }
#             };
#         }
#         """, timeout=3000)
#     except:
#         return None
#
# async def validate_locator_relaxed(test_locator, target_position):
#     try:
#         if check_timeout():
#             return False
#
#         count = await test_locator.count()
#         if count != 1:
#             return False
#
#         for retry in range(2):
#             try:
#                 found_pos = await test_locator.first.evaluate("""
#                 (el) => ({
#                     x: Math.round(el.getBoundingClientRect().x),
#                     y: Math.round(el.getBoundingClientRect().y),
#                     w: Math.round(el.getBoundingClientRect().width),
#                     h: Math.round(el.getBoundingClientRect().height)
#                 })
#                 """, timeout=1000)
#
#                 if found_pos:
#                     break
#             except:
#                 if retry == 0:
#                     await asyncio.sleep(0.1)
#                 continue
#         else:
#             return False
#
#         position_match = (abs(found_pos['x'] - target_position['x']) <= 8 and
#                           abs(found_pos['y'] - target_position['y']) <= 8)
#
#         if target_position['w'] > 0 and target_position['h'] > 0:
#             width_tolerance = max(5, target_position['w'] * 0.15)
#             height_tolerance = max(3, target_position['h'] * 0.15)
#
#             size_match = (abs(found_pos['w'] - target_position['w']) <= width_tolerance and
#                           abs(found_pos['h'] - target_position['h']) <= height_tolerance)
#         else:
#             size_match = True
#
#         return position_match and size_match
#
#     except:
#         return False
#
# def is_id_stable_simple(id_value):
#     if not id_value or len(id_value.strip()) == 0:
#         return False
#
#     id_val = id_value.strip()
#
#     if len(id_val) < 3 or len(id_val) > 30:
#         return False
#
#     if re.search(r'\d{4,}', id_val):
#         return False
#
#     if re.search(r'[a-f0-9]{8,}', id_val, re.IGNORECASE):
#         return False
#
#     dynamic_words = ['temp', 'auto', 'random', 'guid', 'generated']
#     if any(word in id_val.lower() for word in dynamic_words):
#         return False
#
#     return True
#
# def escape_quotes(text):
#     return str(text).replace("'", "\\'").replace('"', '\\"') if text else ""
#
# async def phase1_testid(element_data):
#     if not element_data or not element_data.get('attrs'):
#         return None
#
#     test_attributes = [
#         'data-testid', 'data-test', 'data-cy', 'data-automation', 'data-qa', 'data-test-id'
#     ]
#
#     async def test_single_testid(attr):
#         if element_data['attrs'].get(attr):
#             testid_value = element_data['attrs'][attr]
#             try:
#                 loc = page.get_by_test_id(testid_value)
#                 if await validate_locator_relaxed(loc, element_data['position']):
#                     return f"page.getByTestId('{escape_quotes(testid_value)}')"
#             except:
#                 pass
#         return None
#
#     try:
#         tasks = [test_single_testid(attr) for attr in test_attributes]
#         results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=0.8)
#
#         for result in results:
#             if result and not isinstance(result, Exception):
#                 return result
#     except asyncio.TimeoutError:
#         pass
#     return None
#
# async def phase2_stable_id(element_data):
#     if not element_data or not element_data.get('attrs'):
#         return None
#
#     id_val = element_data['attrs'].get('id')
#     if not id_val:
#         return None
#
#     if is_id_stable_simple(id_val):
#         try:
#             loc = page.locator(f'#{id_val}')
#             if await validate_locator_relaxed(loc, element_data['position']):
#                 return f"page.locator('#{escape_quotes(id_val)}')"
#         except:
#             pass
#     return None
#
# async def phase3_parent_child(element_data):
#     if not element_data or not element_data.get('parentContext'):
#         return None
#
#     combinations = []
#     parent_ctx = element_data.get('parentContext', {})
#
#     for parent_testid in parent_ctx.get('testids', [])[:2]:
#         try:
#             parent_loc = page.get_by_test_id(parent_testid)
#
#             if element_data['role']:
#                 if element_data['accessibleNames']:
#                     for name in element_data['accessibleNames'][:2]:
#                         combinations.append({
#                             'locator': parent_loc.get_by_role(element_data['role'], name=name),
#                             'string': f"page.getByTestId('{escape_quotes(parent_testid)}').getByRole('{element_data['role']}', {{name: '{escape_quotes(name)}'}})"
#                         })
#
#                 combinations.append({
#                     'locator': parent_loc.get_by_role(element_data['role']),
#                     'string': f"page.getByTestId('{escape_quotes(parent_testid)}').getByRole('{element_data['role']}')"
#                 })
#
#             for text in element_data['accessibleNames'][:2]:
#                 if 3 <= len(text) <= 30:
#                     combinations.append({
#                         'locator': parent_loc.get_by_text(text),
#                         'string': f"page.getByTestId('{escape_quotes(parent_testid)}').getByText('{escape_quotes(text)}')"
#                     })
#
#         except:
#             continue
#
#     for parent_role in parent_ctx.get('roles', [])[:2]:
#         try:
#             parent_loc = page.get_by_role(parent_role)
#
#             for testid_attr in ['data-testid', 'data-test', 'data-cy']:
#                 if element_data['attrs'].get(testid_attr):
#                     testid_val = element_data['attrs'][testid_attr]
#                     combinations.append({
#                         'locator': parent_loc.get_by_test_id(testid_val),
#                         'string': f"page.getByRole('{parent_role}').getByTestId('{escape_quotes(testid_val)}')"
#                     })
#                     break
#
#             if element_data['role'] and element_data['role'] != parent_role:
#                 combinations.append({
#                     'locator': parent_loc.get_by_role(element_data['role']),
#                     'string': f"page.getByRole('{parent_role}').getByRole('{element_data['role']}')"
#                 })
#
#         except:
#             continue
#
#     async def test_combination(combo):
#         try:
#             if await validate_locator_relaxed(combo['locator'], element_data['position']):
#                 return combo['string']
#         except:
#             pass
#         return None
#
#     if combinations:
#         try:
#             tasks = [test_combination(combo) for combo in combinations[:8]]
#             results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1.0)
#
#             for result in results:
#                 if result and not isinstance(result, Exception):
#                     return result
#         except asyncio.TimeoutError:
#             pass
#
#     return None
#
# async def phase4_standard_methods(element_data):
#     if not element_data:
#         return None
#
#     async def test_role():
#         if element_data['role'] and element_data['accessibleNames']:
#             for name in element_data['accessibleNames'][:3]:
#                 try:
#                     loc = page.get_by_role(element_data['role'], name=name)
#                     if await validate_locator_relaxed(loc, element_data['position']):
#                         return f"page.getByRole('{element_data['role']}', {{name: '{escape_quotes(name)}'}})"
#                 except:
#                     continue
#
#             try:
#                 loc = page.get_by_role(element_data['role'])
#                 if await validate_locator_relaxed(loc, element_data['position']):
#                     return f"page.getByRole('{element_data['role']}')"
#             except:
#                 pass
#         return None
#
#     async def test_other_methods():
#         methods_to_try = []
#
#         if element_data['attrs'].get('aria-label'):
#             methods_to_try.append(('getByLabel', element_data['attrs']['aria-label']))
#
#         if element_data['attrs'].get('placeholder'):
#             methods_to_try.append(('getByPlaceholder', element_data['attrs']['placeholder']))
#
#         for text in element_data['accessibleNames'][:2]:
#             if 3 <= len(text) <= 40:
#                 methods_to_try.append(('getByText', text))
#
#         if element_data['attrs'].get('alt'):
#             methods_to_try.append(('getByAltText', element_data['attrs']['alt']))
#
#         for method_name, value in methods_to_try:
#             try:
#                 if method_name == 'getByLabel':
#                     loc = page.get_by_label(value)
#                 elif method_name == 'getByPlaceholder':
#                     loc = page.get_by_placeholder(value)
#                 elif method_name == 'getByText':
#                     loc = page.get_by_text(value, exact=True)
#                 elif method_name == 'getByAltText':
#                     loc = page.get_by_alt_text(value)
#                 else:
#                     continue
#
#                 if await validate_locator_relaxed(loc, element_data['position']):
#                     return f"page.{method_name}('{escape_quotes(value)}')"
#             except:
#                 continue
#         return None
#
#     try:
#         role_task = asyncio.create_task(test_role())
#         other_task = asyncio.create_task(test_other_methods())
#
#         done, pending = await asyncio.wait(
#             [role_task, other_task],
#             timeout=1.0,
#             return_when=asyncio.FIRST_COMPLETED
#         )
#
#         for task in pending:
#             task.cancel()
#
#         for task in done:
#             try:
#                 result = await task
#                 if result:
#                     return result
#             except:
#                 continue
#     except asyncio.TimeoutError:
#         pass
#     return None
#
# async def phase5_relative_xpath(element_data):
#     if not element_data or not element_data.get('xpathContext'):
#         return None
#
#     xpath_ctx = element_data['xpathContext']
#     tag = element_data['tag']
#     attrs = element_data['attrs']
#
#     def generate_xpath_patterns():
#         patterns = []
#
#         if xpath_ctx['idPatterns'] and xpath_ctx['idPatterns']['isDynamic']:
#             id_data = xpath_ctx['idPatterns']
#
#             if id_data['prefix'] and len(id_data['prefix']) >= 3:
#                 patterns.append(f"//{tag}[starts-with(@id, '{id_data['prefix']}')]")
#
#             if id_data['suffix'] and len(id_data['suffix']) >= 3:
#                 patterns.append(f"//{tag}[ends-with(@id, '{id_data['suffix']}')]")
#
#         for stable_class in xpath_ctx['stableClasses'][:2]:
#             patterns.append(f"//{tag}[contains(@class, '{stable_class}')]")
#
#         for text_pattern in xpath_ctx['textPatterns'][:2]:
#             if 5 <= len(text_pattern) <= 30:
#                 patterns.append(f"//{tag}[contains(text(), '{text_pattern}')]")
#
#         if attrs.get('name') and attrs.get('type'):
#             patterns.append(f"//{tag}[@name='{attrs['name']}' and @type='{attrs['type']}']")
#
#         return patterns[:6]
#
#     async def test_xpath_pattern(xpath_pattern):
#         try:
#             loc = page.locator(f'xpath={xpath_pattern}')
#             if await validate_locator_relaxed(loc, element_data['position']):
#                 return f"page.locator('xpath={xpath_pattern}')"
#         except:
#             pass
#         return None
#
#     patterns = generate_xpath_patterns()
#
#     if patterns:
#         try:
#             tasks = [test_xpath_pattern(pattern) for pattern in patterns]
#             results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=0.8)
#
#             for result in results:
#                 if result and not isinstance(result, Exception):
#                     return result
#         except asyncio.TimeoutError:
#             pass
#
#     return None
#
# # MAIN EXECUTION
# try:
#     element_data = await extract_comprehensive_element_data(target_locator)
#
#     if not element_data:
#         return get_fallback_locator()
#
#     phases = [
#         ("TestID", phase1_testid),
#         ("Stable ID", phase2_stable_id),
#         ("Parent-Child", phase3_parent_child),
#         ("Standard Methods", phase4_standard_methods),
#         ("Relative XPath", phase5_relative_xpath)
#     ]
#
#     for phase_name, phase_func in phases:
#         if check_timeout():
#             break
#
#         try:
#             result = await phase_func(element_data)
#             if result:
#                 return result
#         except Exception:
#             continue
#
#     return get_fallback_locator()
#
# except Exception as e:
#     return get_fallback_locator()

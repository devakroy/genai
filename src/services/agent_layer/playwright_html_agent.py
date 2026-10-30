import asyncio
import os
from typing import Union, List, Dict
from playwright.async_api import async_playwright, Page, Frame, ElementHandle
import logging

# Initialize logger
logger = logging.getLogger(__name__)

async def get_element_xpath(page_or_frame: Union[Page, Frame], element: ElementHandle) -> Union[str, Dict]:
    """Generate XPath for element, handle PayPal logo special case - EXACT same as Selenium"""
    # First check if this is an img element with PayPal logo class
    tag_name = await element.evaluate("(el) => el.tagName.toLowerCase()")

    if tag_name == "img":
        class_attr = await element.get_attribute("class") or ""
        if "paypal-logo" in class_attr.lower():
            # For PayPal logo images, return both xpath and special text content
            xpath = await page_or_frame.evaluate("""
                (element) => {
                    function getElementXPath(elt) {
                        var path = "";
                        for (; elt && elt.nodeType == 1; elt = elt.parentNode) {
                            var idx = getElementIdx(elt);
                            var xname = elt.tagName;
                            if (idx > 1)
                                xname += "[" + idx + "]";
                            path = "/" + xname + path;
                        }
                        return path.toLowerCase();
                    }

                    function getElementIdx(elt) {
                        var count = 1;
                        for (var sib = elt.previousSibling; sib; sib = sib.previousSibling) {
                            if (sib.nodeType == 1 && sib.tagName == elt.tagName)
                                count++;
                        }
                        return count;
                    }

                    return getElementXPath(element);
                }
            """, element)
            return {"xpath": xpath, "text": "PayPal"}

    # For non-PayPal logo images or other elements, just return the xpath
    return await page_or_frame.evaluate("""
        (element) => {
            function getElementXPath(elt) {
                var path = "";
                for (; elt && elt.nodeType == 1; elt = elt.parentNode) {
                    var idx = getElementIdx(elt);
                    var xname = elt.tagName;
                    if (idx > 1)
                        xname += "[" + idx + "]";
                    path = "/" + xname + path;
                }
                return path.toLowerCase();
            }

            function getElementIdx(elt) {
                var count = 1;
                for (var sib = elt.previousSibling; sib; sib = sib.previousSibling) {
                    if (sib.nodeType == 1 && sib.tagName == elt.tagName)
                        count++;
                }
                return count;
            }

            return getElementXPath(element);
        }
    """, element)


async def get_direct_text_content(page_or_frame: Union[Page, Frame], element: ElementHandle) -> str:
    """Get only direct text content of an element, not from children - EXACT same as Selenium"""
    return await page_or_frame.evaluate("""
        (element) => {
            var directText = '';
            // Check for direct text nodes only (not from children)
            for (var i = 0; i < element.childNodes.length; i++) {
                var node = element.childNodes[i];
                if (node.nodeType === 3) {  // Text node (Node.TEXT_NODE = 3)
                    var text = node.textContent.trim();
                    if (text) {
                        directText += text + ' ';
                    }
                }
            }
            return directText.trim();
        }
    """, element)


async def is_clickable_div(page_or_frame: Union[Page, Frame], element: ElementHandle) -> bool:
    """Only consider div clickable if it has role AND pointer cursor - EXACT same as Selenium"""
    # First check: Must have clickable role (link or button only)
    role = await element.get_attribute('role')
    has_clickable_role = role in ['link', 'button']

    # Second check: Must have pointer cursor
    try:
        cursor_style = await page_or_frame.evaluate("(element) => window.getComputedStyle(element).cursor", element)
        has_pointer_cursor = cursor_style == 'pointer'
    except:
        has_pointer_cursor = False

    # Both conditions must be met
    return has_clickable_role and has_pointer_cursor


async def get_clickable_div_text(page_or_frame: Union[Page, Frame], element: ElementHandle) -> str:
    """Get text for clickable div - Priority: aria-label > direct text > child text - EXACT same as Selenium"""
    # First priority: Check for aria-label
    aria_label = await element.get_attribute('aria-label')
    if aria_label and aria_label.strip():
        return aria_label.strip()

    # Second priority: Direct text content
    direct_text = await get_direct_text_content(page_or_frame, element)
    if direct_text and direct_text.strip():
        return direct_text.strip()

    # Third priority: Get text from child elements
    return await page_or_frame.evaluate("""
        (element) => {
            var texts = [];
            // Get text from children
            for (var i = 0; i < element.children.length; i++) {
                var child = element.children[i];

                if (child.tagName === 'SPAN' || child.tagName === 'P' || child.tagName === 'DIV') {
                    var childText = child.textContent.trim();
                    if (childText) texts.push(childText);
                }
                else if (child.tagName === 'IMG') {
                    var altText = '';
                    var className = child.className || '';
                    if (className.toLowerCase().includes('paypal-logo')) {
                        altText = 'PayPal';
                    }
                    if (!altText) {
                        altText = child.alt || child.title || '';
                    }
                    if (altText) texts.push(altText);
                }
                else if (child.tagName === 'INPUT') {
                    var inputText = child.placeholder || child.value || '';
                    if (inputText) texts.push(inputText);
                }
            }
            return texts.join(' ').trim();
        }
    """, element)


async def get_iframe_details(page: Page) -> List[Dict]:
    """EXACT same logic as Selenium version"""
    all_details = []

    try:
        # First, collect all labels with their associated elements
        label_map = await page.evaluate("""
            () => {
                const map = {};
                const labels = document.querySelectorAll('label[for]');
                labels.forEach(label => {
                    const forId = label.getAttribute('for');
                    map[forId] = label.textContent.trim();
                });
                return map;
            }
        """)

        async def process_iframe(current_page_or_frame: Union[Page, Frame], iframe_element: ElementHandle,
                                 parent_xpath: List[str], iframe_id: str = None):
            """Process individual iframe"""
            try:
                # Get iframe xpath
                iframe_xpath = await get_element_xpath(current_page_or_frame, iframe_element)

                # Handle iframe xpath result
                if isinstance(iframe_xpath, dict):
                    iframe_xpath_str = iframe_xpath["xpath"]
                else:
                    iframe_xpath_str = iframe_xpath

                # Get container ID if available
                container_id = iframe_id
                if not container_id:
                    try:
                        container_id = await iframe_element.evaluate("(el) => el.parentElement?.id")
                    except:
                        container_id = None

                # Get the frame content directly from the element handle
                try:
                    frame = await iframe_element.content_frame()
                    if not frame:
                        logger.info("Could not access iframe content")
                        return
                except Exception as e:
                    logger.info(f"Error getting iframe content: {str(e)}")
                    return

                current_xpath = parent_xpath + [iframe_xpath_str]

                try:
                    # Wait for elements
                    await frame.wait_for_load_state('domcontentloaded', timeout=10000)

                    # Get all elements using frame.query_selector_all instead of locator
                    try:
                        element_handles = await frame.query_selector_all("*")
                    except Exception as e:
                        logger.info(f"Error querying elements in iframe: {str(e)}")
                        return

                    for element_handle in element_handles:
                        try:
                            if not element_handle:
                                continue

                            # Get rect
                            rect = await element_handle.bounding_box()
                            if not rect or rect['width'] <= 0 or rect['height'] <= 0:
                                continue

                            xpath_result = await get_element_xpath(frame, element_handle)

                            # Handle the case where xpath_result is a dict (PayPal logo case)
                            if isinstance(xpath_result, dict):
                                xpath = xpath_result["xpath"]
                                element_text = xpath_result["text"]
                            else:
                                xpath = xpath_result
                                tag_name = await element_handle.evaluate("(el) => el.tagName.toLowerCase()")

                                # Skip html/body tags
                                if tag_name in ["html", "body"]:
                                    continue

                                # Regular element text handling
                                element_text = ""
                                if tag_name == "input":
                                    element_text = (await element_handle.get_attribute('placeholder') or
                                                    await element_handle.get_attribute('value') or
                                                    await element_handle.get_attribute('name') or
                                                    await element_handle.get_attribute('title') or "")

                                    if not element_text and container_id and container_id in label_map:
                                        element_text = label_map[container_id]

                                    # Get input type to check if it's a regular input field
                                    input_type = (await element_handle.get_attribute('type') or 'text').lower()

                                    # Skip button-type inputs
                                    if input_type not in ['submit', 'button', 'reset', 'hidden']:
                                        # STRICT PRIORITY: Get attributes first
                                        placeholder = await element_handle.get_attribute('placeholder')
                                        aria_label = await element_handle.get_attribute('aria-label')
                                        name = await element_handle.get_attribute('name')
                                        input_id = await element_handle.get_attribute('id')
                                        title = await element_handle.get_attribute('title')

                                        # Use strict priority for BOTH element_text AND xpath (must match)
                                        element_text = ""
                                        if placeholder:
                                            xpath = f"//input[@placeholder='{placeholder}']"
                                            element_text = placeholder
                                        elif aria_label:
                                            xpath = f"//input[@aria-label='{aria_label}']"
                                            element_text = aria_label
                                        elif name:
                                            xpath = f"//input[@name='{name}']"
                                            element_text = name
                                        elif input_id:
                                            xpath = f"//input[@id='{input_id}']"
                                            element_text = input_id
                                        elif title:
                                            element_text = title

                                        # Fallback to label map if still no text
                                        if not element_text and container_id and container_id in label_map:
                                            element_text = label_map[container_id]
                                    else:
                                        # For button-type inputs, use value attribute
                                        element_text = (await element_handle.get_attribute('value') or
                                                       await element_handle.get_attribute('aria-label') or
                                                       await element_handle.get_attribute('title') or "")
                                elif tag_name == "div":
                                    if await is_clickable_div(frame, element_handle):
                                        element_text = await get_clickable_div_text(frame, element_handle)
                                    else:
                                        element_text = await get_direct_text_content(frame, element_handle)

                                elif tag_name in ["p", "h1", "h2", "h3"]:
                                    element_text = await get_direct_text_content(frame, element_handle)

                                else:
                                    text_content = await element_handle.text_content() or ""
                                    title = await element_handle.get_attribute('title') or ""
                                    element_text = text_content.strip() or title.strip()

                            # Calculate coordinates
                            coordinates = {
                                'x': rect['x'],
                                'y': rect['y'],
                                'width': rect['width'],
                                'height': rect['height'],
                                'center_x': rect['x'] + rect['width'] / 2,
                                'center_y': rect['y'] + rect['height'] / 2
                            }

                            # Add to results
                            all_details.append({
                                'tagName': "iframe -> " + tag_name,
                                'iframe_xpath': current_xpath,
                                'xpath': xpath,
                                'element': element_text.strip() if element_text else "",
                                'coordinates': coordinates
                            })

                        except Exception as e:
                            logger.info(f"Error processing element in iframe: {str(e)}")
                            continue

                    # Process nested iframes
                    try:
                        nested_iframe_elements = await frame.query_selector_all("iframe")
                        for nested_iframe_element in nested_iframe_elements:
                            try:
                                await process_iframe(frame, nested_iframe_element, current_xpath)
                            except Exception as e:
                                logger.info(f"Error processing nested iframe: {str(e)}")
                                continue
                    except Exception as e:
                        logger.info(f"Error getting nested iframes: {str(e)}")

                except Exception as e:
                    logger.info(f"Failed to process iframe content: {str(e)}")

            except Exception as e:
                logger.info(f"Failed to process iframe: {str(e)}")

        # Handle special cases for payment iframes where label is outside the iframe
        try:
            special_cases = await page.evaluate("""
                () => {
                    const result = [];
                    document.querySelectorAll('[id]').forEach(element => {
                        const label = document.querySelector('label[for="' + element.id + '"]');
                        const iframe = element.querySelector('iframe');
                        if (label && iframe) {
                            result.push({
                                id: element.id,
                                label: label.textContent.trim()
                            });
                        }
                    });
                    return result;
                }
            """)

            # Process special cases first
            for case in special_cases:
                try:
                    iframe_elements = await page.query_selector_all(f"#{case['id']} iframe")
                    if iframe_elements:
                        await process_iframe(page, iframe_elements[0], [], case['id'])
                except Exception as e:
                    logger.info(f"Error processing special case iframe {case['id']}: {str(e)}")

        except Exception as e:
            logger.info(f"Error processing special cases: {str(e)}")

        # Process remaining iframes
        try:
            # Get all top-level iframes
            all_iframe_elements = await page.query_selector_all('iframe')

            # Get special case IDs to skip them
            special_case_ids = set()
            try:
                special_case_containers = await page.evaluate("""
                    () => {
                        const containers = [];
                        document.querySelectorAll('[id]').forEach(element => {
                            const label = document.querySelector('label[for="' + element.id + '"]');
                            const iframe = element.querySelector('iframe');
                            if (label && iframe) {
                                containers.push(element.id);
                            }
                        });
                        return containers;
                    }
                """)
                special_case_ids = set(special_case_containers)
            except Exception as e:
                logger.info(f"Error getting special case IDs: {str(e)}")

            # Process each iframe
            for iframe_element in all_iframe_elements:
                try:
                    # Check if this iframe is part of a special case
                    is_special_case = False
                    try:
                        parent_id = await iframe_element.evaluate("(el) => el.parentElement?.id")
                        if parent_id and parent_id in special_case_ids:
                            is_special_case = True
                    except:
                        pass

                    if not is_special_case:
                        await process_iframe(page, iframe_element, [])

                except Exception as e:
                    logger.info(f"Error processing top-level iframe: {str(e)}")
                    continue

        except Exception as e:
            logger.info(f"Error processing top-level iframes: {str(e)}")

    except Exception as e:
        logger.info(f"Error in get_iframe_details: {str(e)}")

    # Always return a list, never None
    # logger.info("all details:", all_details)
    return all_details


async def inject_html_context_retrieval(page):
    """Keep exact same template literals as Selenium version"""
    js_file_path = os.path.join(os.path.dirname(__file__), "html_context_extractor.js")
    with open(js_file_path, 'r', encoding='utf-8') as file:
        js_script = file.read()

    if page is None:
        logger.info("Error: 'page' object is None")
        return None

    try:
        details = await page.evaluate(js_script)
        return details
    except Exception as e:
        logger.info(f"Error in inject_html_context_retrieval: {str(e)}")
        return None


async def inject_mutation_observer(page: Page):
    observer_script = """
    (function() {
      function generateXPath(element) {
        if (!element) return '';
        if (element === document.body) {
          return '/html/body';
        }

        let index = 1;
        let sibling = element.previousElementSibling;
        while (sibling) {
          if (sibling.nodeName === element.nodeName) {
            index++;
          }
          sibling = sibling.previousElementSibling;
        }
        const tagName = element.nodeName.toLowerCase();
        return `\${generateXPath(element.parentNode)}/\${tagName}[\${index}]`;
      }

      function processNode(node) {
        const rect = node.getBoundingClientRect();
        let elementName = node.getAttribute('aria-label') || node.getAttribute('alt') || node.getAttribute('title') || node.tagName.toLowerCase();

        if (node.tagName.toLowerCase() === 'input' || node.tagName.toLowerCase() === 'textarea') {
          elementName = node.getAttribute('placeholder') || node.getAttribute('aria-label') || node.getAttribute('alt') || node.getAttribute('title') || '';
          const label = node.closest('label') || document.querySelector(`label[for="\${node.id}"]`);
          if (label) {
            elementName = label.innerText || elementName;
          }
        } else if (node.tagName.toLowerCase() === 'button') {
          elementName = node.getAttribute('aria-label') || node.getAttribute('alt') || node.getAttribute('title') || node.innerText || '';
        }

        return {
          tagName: node.tagName.toLowerCase(),
          xpath: generateXPath(node),
          element: elementName.trim(),
          coordinates: {
            x: rect.x,
            y: rect.y,
            width: rect.width,
            height: rect.height,
            center_x: rect.x + rect.width / 2,
            center_y: rect.y + rect.height / 2
          }
        };
      }

      function getAllElements(node) {
        const interactableTags = ['button', 'a', 'input', 'textarea', 'select', 'option', 'label'];
        const elements = [];
        if (node.nodeType === 1 && interactableTags.includes(node.tagName.toLowerCase())) {
          elements.push(processNode(node));
        }
        node.querySelectorAll(interactableTags.join(',')).forEach(child => {
          elements.push(processNode(child));
        });
        return elements;
      }

      function extractAllElements() {
        const allElements = [];
        document.querySelectorAll('*').forEach(node => {
          allElements.push(...getAllElements(node));
        });
        return allElements;
      }

      const observer = new MutationObserver((mutations) => {
        const newElements = [];
        mutations.forEach((mutation) => {
          mutation.addedNodes.forEach((node) => {
            setTimeout(() => {
              newElements.push(...getAllElements(node));
            }, 100); // Adjust the delay as needed
          });
        });

        setTimeout(() => {
          if (newElements.length > 20) {
            const allElements = extractAllElements();
            console.log('All Elements:', JSON.stringify(allElements, null, 2));
            window.newElements = allElements;
          } else if (newElements.length > 0) {
            console.log('New Elements:', JSON.stringify(newElements, null, 2));
            window.newElements = newElements;
          }
        }, 150); // Adjust the delay as needed
      });

      observer.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
    })();
    """
    await page.evaluate(observer_script)



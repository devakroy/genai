(function extractHtmlContext() {
    return (function waitForDomStableAndRunSync(callback, timeout) {
        let done = false;
        let result;

        const observer = new MutationObserver(() => {
            clearTimeout(window.__stabilizeTimer);
            window.__stabilizeTimer = setTimeout(() => {
                observer.disconnect();
                result = callback();
                done = true;
            }, 500);
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });

        setTimeout(() => {
            if (!done) {
                observer.disconnect();
                result = callback();
            }
        }, timeout || 5000);

        const start = Date.now();
        while (!done && (Date.now() - start) < (timeout || 5000)) {}

        return result || callback();
    })(() => {
        try {
            return (function() {

                function extractDetails() {
                    console.log("Inside extractDetails function");

                    const allElements = document.querySelectorAll('*');
                    console.log("Total elements found:", allElements.length);
                    try {
                        const safeQuerySelector = (selector) => {
                            try {
                                // If the selector is an ID selector and contains invalid characters, use getElementById instead
                                if (selector.startsWith('#')) {
                                    const id = selector.slice(1);
                                    return document.getElementById(id);
                                }
                                return document.querySelector(selector);
                            } catch (error) {
                                console.warn(`Invalid selector: ${selector}`, error);
                                return null;
                            }
                        };

                        const escapeSelector = (selector) => {
                            return CSS.escape(selector);
                        };

                        const generateXPath = (element) => {
                            if (!element) return '';
                            if (element === document.body) {
                                return '/html/body';
                            }
                            let index = 0;
                            let sibling = element.previousElementSibling;
                            while (sibling) {
                                if (sibling.nodeName === element.nodeName) {
                                    index++;
                                }
                                sibling = sibling.previousElementSibling;
                            }
                            const tagName = element.nodeName.toLowerCase();
                            const nth = index ? `[${index + 1}]` : '';
                            return `${generateXPath(element.parentNode)}/${tagName}${nth}`;
                        };

                        const isValidXPath = (xpath) => {
                            try {
                                document.evaluate(xpath, document, null, XPathResult.ANY_TYPE, null);
                                return true;
                            } catch (e) {
                                return false;
                            }
                        };

                        const cleanTextContent = (text) => {
                            return text.replace(/\\s+/g, ' ').trim();
                        };

                        const findContextualLabel = (inputElement) => {
                            // Strategy 1: Check if input is wrapped in a label
                            const parentLabel = inputElement.closest('label');
                            if (parentLabel) {
                                // Extract text excluding the input itself
                                const clone = parentLabel.cloneNode(true);
                                const inputClone = clone.querySelector('input');
                                if (inputClone) inputClone.remove();
                                const text = cleanTextContent(clone.textContent);
                                if (text) return {
                                    textContent: text
                                };
                            }


                            // Strategy 2: Look for associated label by ID
                            if (inputElement.id) {
                                const allLabels = document.querySelectorAll('label');
                                let associatedLabel = null;

                                for (const label of allLabels) {
                                    if (label.getAttribute('for') === inputElement.id) {
                                        associatedLabel = label;
                                        break;
                                    }
                                }

                                if (associatedLabel) {
                                    // Check if label contains SVG with aria-label
                                    const svg = associatedLabel.querySelector('svg[aria-label]');
                                    if (svg) {
                                        const svgLabel = svg.getAttribute('aria-label');
                                        if (svgLabel) {
                                            return {
                                                textContent: svgLabel
                                            };
                                        }
                                    }

                                    // Original text extraction
                                    const labelText = cleanTextContent(associatedLabel.textContent);
                                    if (labelText) {
                                        return {
                                            textContent: labelText
                                        };
                                    }
                                }
                            }

                            // Strategy 3: Find contextual container and search within it
                            const container = inputElement.closest(
                                'form, fieldset, [class*="form"], [class*="field"], [class*="input"], [class*="control"], ' +
                                '.MuiGrid-container, .form-group, .input-group, [role="group"]'
                            );

                            if (container) {
                                // Look for labels within the same container
                                const labels = Array.from(container.querySelectorAll('label'));

                                // Find the closest preceding label within container
                                let closestLabel = null;
                                let shortestDistance = Infinity;

                                labels.forEach(label => {
                                    if (isElementBefore(label, inputElement)) {
                                        const distance = getElementDistance(label, inputElement);
                                        if (distance < shortestDistance) {
                                            shortestDistance = distance;
                                            closestLabel = label;
                                        }
                                    }
                                });

                                if (closestLabel) return closestLabel;

                                // Fallback: Look for meaningful text elements within container
                                const textElements = container.querySelectorAll('p, span:not([aria-hidden="true"]), div');
                                for (const element of textElements) {
                                    const text = element.textContent?.trim();
                                    if (text && text.length > 0 && text.length < 100 && isElementBefore(element, inputElement)) {
                                        const distance = getElementDistance(element, inputElement);
                                        if (distance < 50) { // Reasonable proximity
                                            return {
                                                textContent: text
                                            };
                                        }
                                    }
                                }
                            }

                            return null;
                        };

                        // Helper function to check if element1 comes before element2
                        const isElementBefore = (element1, element2) => {
                            return element1.compareDocumentPosition(element2) & Node.DOCUMENT_POSITION_FOLLOWING;
                        };

                        // Helper function to calculate DOM distance between elements
                        const getElementDistance = (element1, element2) => {
                            const rect1 = element1.getBoundingClientRect();
                            const rect2 = element2.getBoundingClientRect();

                            // Calculate both DOM tree distance and visual distance
                            const visualDistance = Math.abs(rect1.top - rect2.top) + Math.abs(rect1.left - rect2.left);

                            // Find common ancestor depth
                            let ancestor1 = element1;
                            let depth = 0;
                            while (ancestor1 && !ancestor1.contains(element2)) {
                                ancestor1 = ancestor1.parentElement;
                                depth++;
                            }

                            return visualDistance + (depth * 10); // Weight DOM structure more than visual distance
                        };

                        const allElements = document.querySelectorAll('*');
                        const elementMap = new Map();

                        allElements.forEach((element) => {
                            const tagName = element.tagName.toLowerCase();

                            if (['script', 'style', 'fieldset'].includes(tagName)) {
                                return;
                            }
                            
                            // ✅ SPECIAL DEBUG: Track table-related elements specifically
                            if (tagName === 'td' || tagName === 'th' || tagName === 'a') {
                                const elementText = cleanTextContent(element.innerText || element.textContent || '').trim();
                                console.log(`DEBUG: Processing ${tagName.toUpperCase()} element:`, {
                                    text: elementText,
                                    isNumeric: /^\d+$/.test(elementText),
                                    hasAnchor: tagName === 'td' ? !!element.querySelector('a') : false,
                                    anchorText: tagName === 'td' && element.querySelector('a') ? 
                                        cleanTextContent(element.querySelector('a').innerText || '').trim() : null
                                });
                            }
                            const isAriaHidden = (element) => {
                                let current = element;

                                // Check the element itself and all parents
                                while (current) {
                                    if (current.getAttribute('aria-hidden') === 'true') {
                                        return true;
                                    }
                                    current = current.parentElement;
                                }
                                return false;
                            };

                            let label = cleanTextContent(element.innerText || element.textContent || '');
                            const elementXPath = generateXPath(element);
                            const rect = element.getBoundingClientRect();

                            if (['html', 'body'].includes(tagName) && label.length > 100) {
                                return;
                            }

                            if (tagName === 'label') {
                                const forAttribute = element.getAttribute('for');
                                if (forAttribute) {
                                    const associatedElement = safeQuerySelector(`#${escapeSelector(forAttribute)}`);
                                    if (associatedElement) {
                                        const associatedXPath = generateXPath(associatedElement);
                                        if (isValidXPath(associatedXPath)) {
                                            elementMap.set(associatedXPath, {
                                                tagName: associatedElement.tagName.toLowerCase(),
                                                xpath: associatedXPath,
                                                element: cleanTextContent(element.innerText.trim()),
                                                coordinates: {
                                                    x: rect.x,
                                                    y: rect.y,
                                                    width: rect.width,
                                                    height: rect.height,
                                                    center_x: rect.x + rect.width / 2,
                                                    center_y: rect.y + rect.height / 2
                                                }
                                            });
                                        }
                                    }
                                }
                            } else if (tagName === 'select') {
                                let parentLabel = '';

                                parentLabel = element.closest('label') ? cleanTextContent(element.closest('label').innerText.split('\\n')[0].trim()) : '';

                                if (!parentLabel && element.id) {
                                    const labelForElement = document.querySelector('label[for="' + CSS.escape(element.id) + '"]');

                                    if (labelForElement) {
                                        parentLabel = cleanTextContent(labelForElement.innerText);
                                    }
                                }
                                if (!parentLabel) {

                                    // Try preceding spans first (3 closest)
                                    const precedingSpans = document.evaluate('preceding::span[text()]', element, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                                    console.log('Preceding spans found:', precedingSpans.snapshotLength);

                                    for (let i = Math.max(0, precedingSpans.snapshotLength - 3); i < precedingSpans.snapshotLength; i++) {
                                        const span = precedingSpans.snapshotItem(i);
                                        console.log('Checking preceding span:', span, 'Text:', span.textContent.trim());
                                        if (span.textContent.trim() && span.textContent.length < 50) {
                                            parentLabel = cleanTextContent(span.textContent);
                                            console.log('Found parentLabel from preceding span:', parentLabel);
                                            break;
                                        }
                                    }

                                    // If not found, try following spans (3 closest)
                                    if (!parentLabel) {
                                        const followingSpans = document.evaluate('following::span[text()]', element, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);

                                        for (let i = 0; i < Math.min(3, followingSpans.snapshotLength); i++) {
                                            const span = followingSpans.snapshotItem(i);
                                            console.log('Checking following span:', span, 'Text:', span.textContent.trim());
                                            if (span.textContent.trim() && span.textContent.length < 50) {
                                                parentLabel = cleanTextContent(span.textContent);
                                                console.log('Found parentLabel from following span:', parentLabel);
                                                break;
                                            }
                                        }
                                    }

                                }

                                if (isValidXPath(elementXPath)) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: parentLabel,
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                                Array.from(element.options).forEach((option, index) => {
                                    const optionRect = option.getBoundingClientRect();
                                    const optionXPath = `${elementXPath}/option[${index + 1}]`;
                                    if (isValidXPath(optionXPath)) {
                                        elementMap.set(`${elementXPath}${optionXPath}`, {
                                            tagName: 'option',
                                            element: `${cleanTextContent(option.text)}`,
                                            xpath: optionXPath,
                                            coordinates: {
                                                x: optionRect.x,
                                                y: optionRect.y,
                                                width: optionRect.width,
                                                height: optionRect.height,
                                                center_x: optionRect.x + optionRect.width / 2,
                                                center_y: optionRect.y + optionRect.height / 2
                                            }
                                        });
                                    }
                                });
                            } else if (tagName === 'p') {
                                let directText = '';
                                let fullText = '';

                                // Extract direct text nodes (not from children)
                                for (let i = 0; i < element.childNodes.length; i++) {
                                    const node = element.childNodes[i];
                                    if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                                        directText += node.textContent + ' ';
                                    }
                                }

                                // Clean up direct text
                                directText = cleanTextContent(directText);

                                // Get full text content including children
                                fullText = cleanTextContent(element.textContent || '');

                                // Decide which text to use based on availability and relevance
                                let finalText = '';

                                // If direct text exists and is substantial, use it
                                if (directText && directText.length > 1) {
                                    finalText = directText;
                                }
                                // If no direct text or it's minimal, use full text including children
                                else if (fullText && fullText.length > 1) {
                                    finalText = fullText;
                                }

                                if (finalText && finalText.trim().split(/\s+/).length > 30) {
                                    return;
                                }

                                // Only add if we have meaningful text content
                                if (finalText && isValidXPath(elementXPath)) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: finalText,
                                        hasDirectText: directText.length > 0, // Flag to indicate if it has direct text
                                        hasChildText: fullText.length > directText.length, // Flag to indicate if it has child text
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                            } else if (tagName === 'li') {
                                const parentUl = element.closest('ul, ol');

                                // Compact clickability check
                                const isClickable = (
                                    // Parent UL makes LI clickable
                                    parentUl && (
                                        ['menu', 'menubar', 'listbox', 'tablist', 'tree', 'grid'].includes(parentUl.getAttribute('role')) ||
                                        parentUl.onclick || parentUl.getAttribute('onclick') ||
                                        parentUl.closest('.dropdown, .menu, .autocomplete, .search-results, .filters')
                                    ) ||

                                    // LI specific clickable properties
                                    ['option', 'menuitem', 'button', 'tab', 'treeitem', 'gridcell', 'row', 'link', 'checkbox', 'radio'].includes(element.getAttribute('role')) ||
                                    element.onclick || element.getAttribute('onclick') ||
                                    element.style.cursor === 'pointer' || window.getComputedStyle(element).cursor === 'pointer' ||
                                    (element.hasAttribute('tabindex') && element.getAttribute('tabindex') !== '-1') ||
                                    element.className.match(/clickable|selectable|interactive|product-item|menu-item|dropdown-item|filter-item|cart-item/)
                                );

                                if (isClickable) {
                                    let textContent = '';

                                    // 1. First priority: aria-label
                                    textContent = element.getAttribute('aria-label');

                                    // 2. Second priority: direct text content
                                    if (!textContent) {
                                        let directText = '';
                                        for (let i = 0; i < element.childNodes.length; i++) {
                                            const node = element.childNodes[i];
                                            if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                                                directText += node.textContent + ' ';
                                            }
                                        }
                                        textContent = cleanTextContent(directText);
                                    }

                                    // 3. Third priority: child text content (excluding aria-hidden elements)
                                    if (!textContent) {
                                        const childElements = element.querySelectorAll('*:not([aria-hidden="true"])');
                                        for (const child of childElements) {
                                            if (child.textContent && child.textContent.trim()) {
                                                textContent = cleanTextContent(child.textContent);
                                                break;
                                            }
                                        }
                                    }

                                    // Clean the final text content
                                    textContent = textContent ? cleanTextContent(textContent) : '';

                                    if (textContent && isValidXPath(elementXPath)) {
                                        elementMap.set(elementXPath, {
                                            tagName: tagName,
                                            xpath: elementXPath,
                                            element: textContent,
                                            clickable: 'yes',
                                            coordinates: {
                                                x: rect.x,
                                                y: rect.y,
                                                width: rect.width,
                                                height: rect.height,
                                                center_x: rect.x + rect.width / 2,
                                                center_y: rect.y + rect.height / 2
                                            }
                                        });
                                    }
                                }
                            } else if (tagName === 'div') {
                                // Check if this is an error message div by class name
                                const isErrorMessage = element.className &&
                                    element.className.indexOf('error') !== -1;

                                // Check if this div has an interactive role
                                const role = element.getAttribute('role');
                                const isInteractiveRole = role && [
                                    'button', 'tab', 'link', 'menuitem', 'option',
                                    'checkbox', 'radio', 'switch', 'combobox',
                                    'listbox', 'gridcell', 'columnheader', 'rowheader',
                                    'treeitem', 'disclosure', 'slider'
                                ].includes(role.toLowerCase());

                                if (isErrorMessage) {
                                    // For error messages, get the full text content
                                    const errorText = element.textContent.trim();
                                    if (errorText && isValidXPath(elementXPath)) {
                                        elementMap.set(elementXPath, {
                                            tagName: tagName,
                                            xpath: elementXPath,
                                            element: errorText,
                                            isErrorMessage: true, // Flag as error message
                                            coordinates: {
                                                x: rect.x,
                                                y: rect.y,
                                                width: rect.width,
                                                height: rect.height,
                                                center_x: rect.x + rect.width / 2,
                                                center_y: rect.y + rect.height / 2
                                            }
                                        });
                                    }
                                } else if (isInteractiveRole) {
                                    // Handle interactive divs with roles
                                    let roleLabel = element.getAttribute('aria-label') ||
                                        element.getAttribute('aria-labelledby') ||
                                        cleanTextContent(element.innerText || element.textContent || '') ||
                                        element.getAttribute('title') ||
                                        element.getAttribute('data-tooltip') ||
                                        '';

                                    // If aria-labelledby is used, get the referenced element's text
                                    if (element.getAttribute('aria-labelledby')) {
                                        const labelId = element.getAttribute('aria-labelledby');
                                        const labelElement = document.getElementById(labelId);
                                        if (labelElement) {
                                            roleLabel = cleanTextContent(labelElement.textContent);
                                        }
                                    }

                                    if (roleLabel && isValidXPath(elementXPath)) {
                                        elementMap.set(elementXPath, {
                                            tagName: tagName,
                                            xpath: elementXPath,
                                            element: roleLabel,
                                            role: role, // Store the role for reference
                                            isInteractive: true, // Flag as interactive
                                            coordinates: {
                                                x: rect.x,
                                                y: rect.y,
                                                width: rect.width,
                                                height: rect.height,
                                                center_x: rect.x + rect.width / 2,
                                                center_y: rect.y + rect.height / 2
                                            }
                                        });
                                    }
                                } else {
                                    // Check if this div has a meaningful ID first
                                    const id = element.getAttribute('id');
                                    const isMeaningfulId = id && (
                                        id.includes('container') ||
                                        id.includes('grid') ||
                                        id.includes('wrapper') ||
                                        id.includes('panel') ||
                                        id.includes('section') ||
                                        id.includes('area') ||
                                        id.includes('zone') ||
                                        id.includes('content') ||
                                        id.includes('main') ||
                                        id.includes('header') ||
                                        id.includes('footer') ||
                                        id.includes('nav') ||
                                        id.includes('sidebar') ||
                                        id.includes('modal') ||
                                        id.includes('dialog') ||
                                        id.includes('popup') ||
                                        id.includes('menu') ||
                                        id.includes('dropdown') ||
                                        id.includes('tab') ||
                                        id.includes('accordion') ||
                                        id.includes('carousel') ||
                                        id.includes('slider') ||
                                        id.includes('gallery') ||
                                        id.includes('list') ||
                                        id.includes('table') ||
                                        id.includes('form') ||
                                        id.includes('input') ||
                                        id.includes('button') ||
                                        id.includes('control') ||
                                        id.includes('widget') ||
                                        id.includes('component') ||
                                        id.includes('element') ||
                                        id.includes('item') ||
                                        id.includes('row') ||
                                        id.includes('column') ||
                                        id.includes('cell') ||
                                        id.includes('slot') ||
                                        id.includes('time') ||
                                        id.includes('schedule') ||
                                        id.includes('calendar') ||
                                        id.includes('picker') ||
                                        id.includes('selector')
                                    );

                                    if (isMeaningfulId && isValidXPath(elementXPath)) {
                                        // For divs with meaningful IDs, use the ID as the element label
                                        elementMap.set(elementXPath, {
                                            tagName: tagName,
                                            xpath: elementXPath,
                                            element: id,
                                            hasMeaningfulId: true,
                                            coordinates: {
                                                x: rect.x,
                                                y: rect.y,
                                                width: rect.width,
                                                height: rect.height,
                                                center_x: rect.x + rect.width / 2,
                                                center_y: rect.y + rect.height / 2
                                            }
                                        });
                                    } else {
                                    // Original code for non-error divs
                                    let hasDirectTextContent = false;
                                    let directText = '';
                                    // Check for direct text nodes (not from children)
                                    for (let i = 0; i < element.childNodes.length; i++) {
                                        const node = element.childNodes[i];
                                        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                                            hasDirectTextContent = true;
                                            directText += node.textContent + ' ';
                                        }
                                    }
                                    // Clean up the collected direct text
                                    directText = cleanTextContent(directText);
                                    // Only add div if it has direct text content (not just from children)
                                    if (hasDirectTextContent && directText) {
                                        if (isValidXPath(elementXPath)) {
                                            elementMap.set(elementXPath, {
                                                tagName: tagName,
                                                xpath: elementXPath,
                                                element: directText,
                                                coordinates: {
                                                    x: rect.x,
                                                    y: rect.y,
                                                    width: rect.width,
                                                    height: rect.height,
                                                    center_x: rect.x + rect.width / 2,
                                                    center_y: rect.y + rect.height / 2
                                                }
                                            });
                                        }
                                    }
                                    }
                                }
                            } else if (tagName === 'input' && element.type === 'radio') {
                                const radioWrapper = element.closest('label');
                                const associatedLabel = radioWrapper ? radioWrapper.textContent : '';
                                label = cleanTextContent(associatedLabel);
                                if (isValidXPath(elementXPath)) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: label,
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                            } 
                            else if (tagName === 'input') {
                                const inputType = element.getAttribute('type')?.toLowerCase() || 'text';
                                if (['submit', 'button', 'reset'].includes(inputType)) {
                                    const label = cleanTextContent(
                                        element.getAttribute('value') ||
                                        element.getAttribute('aria-label') ||
                                        element.getAttribute('title') ||
                                        ''
                                    );
                                    if (isValidXPath(elementXPath)) {
                                        elementMap.set(elementXPath, {
                                            tagName: 'button',
                                            xpath: elementXPath,
                                            element: label,
                                            coordinates: {
                                                x: rect.x,
                                                y: rect.y,
                                                width: rect.width,
                                                height: rect.height,
                                                center_x: rect.x + rect.width / 2,
                                                center_y: rect.y + rect.height / 2
                                            }
                                        });
                                    }
                                    return;
                                }
                                if (inputType === 'hidden') {
                                    return;
                                }
                                
                                // For regular input fields, generate attribute-based XPath and extract label
                                // CRITICAL: Prioritize placeholder > aria-label > name > id
                                const placeholderAttr = element.getAttribute('placeholder');
                                const ariaLabelAttr = element.getAttribute('aria-label');
                                const titleAttr = element.getAttribute('title');
                                const nameAttr = element.getAttribute('name');

                                let inputLabel = '';
                                let inputXPath = elementXPath;

                                // STRICT PRIORITY: Only use ONE attribute, never fallback if higher priority exists
                                if (placeholderAttr && placeholderAttr.trim()) {
                                    // Placeholder is highest priority - use it exclusively
                                    inputXPath = `//input[@placeholder="${placeholderAttr}"]`;
                                    inputLabel = placeholderAttr;
                                } else if (ariaLabelAttr && ariaLabelAttr.trim()) {
                                    // Aria-label is second priority
                                    inputXPath = `//input[@aria-label="${ariaLabelAttr}"]`;
                                    inputLabel = ariaLabelAttr;
                                } else if (nameAttr && nameAttr.trim()) {
                                    // Name is third priority (only if no placeholder/aria-label)
                                    inputXPath = `//input[@name="${nameAttr}"]`;
                                    inputLabel = nameAttr;
                                } else if (element.id && element.id.trim()) {
                                    // ID is last resort
                                    inputXPath = `//input[@id="${element.id}"]`;
                                    inputLabel = element.id;
                                } else if (titleAttr && titleAttr.trim()) {
                                    // Title as final fallback
                                    inputLabel = titleAttr;
                                }

                                if (inputLabel && isValidXPath(inputXPath)) {
                                    console.log(`[INPUT EXTRACTION] Adding input via attribute-based XPath:`, {
                                        xpath: inputXPath,
                                        element: cleanTextContent(inputLabel),
                                        placeholder: placeholderAttr,
                                        name: nameAttr
                                    });
                                    elementMap.set(inputXPath, {
                                        tagName: tagName,
                                        xpath: inputXPath,
                                        element: cleanTextContent(inputLabel),
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                                return;
                            }
                            else {

                                if (!label) {
                                    label = element.getAttribute('aria-label') || element.getAttribute('placeholder') || element.getAttribute('title') || '';
                                } else {
                                    // Concatenate text content of parent element if it contains a <b> tag
                                    const parentElement = element.closest('div');
                                    if (parentElement && parentElement.querySelector('b')) {
                                        label = cleanTextContent(parentElement.textContent);
                                    }
                                }
                                if (isValidXPath(elementXPath) && (!elementMap.has(elementXPath) || (elementMap.has(elementXPath) && label))) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: label,
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                            }

                            // Inside the allElements.forEach loop, modify the button handling part:
                            if (tagName === 'button') {
                                // Prioritize aria-label for buttons
                                label = element.getAttribute('aria-label') ||
                                    cleanTextContent(element.innerText || element.textContent || '') ||
                                    element.getAttribute('title') ||
                                    element.getAttribute('name') ||
                                    element.getAttribute('data-testid') ||
                                    '';

                                elementMap.set(elementXPath, {
                                    tagName: tagName,
                                    xpath: elementXPath,
                                    element: label,
                                    coordinates: {
                                        x: rect.x,
                                        y: rect.y,
                                        width: rect.width,
                                        height: rect.height,
                                        center_x: rect.x + rect.width / 2,
                                        center_y: rect.y + rect.height / 2
                                    }
                                });
                            }
                            
                            // ✅ ADD PROPER ANCHOR TAG PROCESSING - Fix for customer ID links
                            if (tagName === 'a') {
                                // Extract text content from anchor tags (customer IDs, links, etc.)
                                label = element.getAttribute('aria-label') ||
                                    cleanTextContent(element.innerText || element.textContent || '') ||
                                    element.getAttribute('title') ||
                                    element.getAttribute('alt') ||
                                    '';

                                // Debug logging for anchor tags
                                console.log("DEBUG: Anchor tag extracted:", {
                                    text: label,
                                    xpath: elementXPath,
                                    isNumeric: /^\d+$/.test(label?.trim() || ''),
                                    trimmed: label?.trim()
                                });

                                // Only add if we have meaningful text content
                                if (label && label.trim()) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: label,
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                            }
                            
                            // ✅ ADD TABLE CELL PROCESSING - Handle customer IDs in table cells
                            if (tagName === 'td' || tagName === 'th') {
                                // Extract text content from table cells
                                label = cleanTextContent(element.innerText || element.textContent || '').trim();
                                
                                // Debug logging for ALL table cells to track processing
                                console.log("DEBUG: Processing table cell:", {
                                    tagName: tagName,
                                    text: label,
                                    xpath: elementXPath,
                                    hasContent: !!label
                                });
                                
                                // Check if this cell contains a customer ID (numeric content)
                                const isNumericContent = /^\d+$/.test(label);
                                
                                // Debug logging for table cells with potential customer IDs
                                if (isNumericContent) {
                                    console.log("DEBUG: Table cell with numeric content:", {
                                        text: label,
                                        xpath: elementXPath,
                                        tagName: tagName
                                    });
                                }
                                
                                // Check for nested anchor tags within the cell
                                const nestedAnchor = element.querySelector('a');
                                if (nestedAnchor) {
                                    const anchorText = cleanTextContent(nestedAnchor.innerText || nestedAnchor.textContent || '').trim();
                                    console.log("DEBUG: Table cell contains anchor:", {
                                        cellText: label,
                                        anchorText: anchorText,
                                        isNumeric: /^\d+$/.test(anchorText),
                                        xpath: elementXPath
                                    });
                                    if (anchorText && /^\d+$/.test(anchorText)) {
                                        console.log("DEBUG: Table cell contains customer ID anchor:", {
                                            cellText: label,
                                            anchorText: anchorText,
                                            xpath: elementXPath
                                        });
                                        // Use the anchor text as the element text for customer ID links
                                        label = anchorText;
                                    }
                                }

                                // Only add table cells with meaningful content
                                if (label && label.trim()) {
                                    console.log("DEBUG: Adding table cell to elementMap:", {
                                        tagName: tagName,
                                        text: label,
                                        xpath: elementXPath,
                                        isNumeric: /^\d+$/.test(label)
                                    });
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: label,
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                            }
                            
                            if (tagName === 'img') {
                                // Prioritize aria-label for buttons
                                label = element.getAttribute('alt') ||
                                    cleanTextContent(element.innerText || element.textContent || '') ||
                                    element.getAttribute('aria-label') ||
                                    element.getAttribute('title') ||
                                    element.getAttribute('name') ||
                                    element.getAttribute('data-testid') ||

                                    '';

                                elementMap.set(elementXPath, {
                                    tagName: tagName,
                                    xpath: elementXPath,
                                    element: label,
                                    coordinates: {
                                        x: rect.x,
                                        y: rect.y,
                                        width: rect.width,
                                        height: rect.height,
                                        center_x: rect.x + rect.width / 2,
                                        center_y: rect.y + rect.height / 2
                                    }
                                });
                            }
                            // Handle special characters
                            if (['span', 'a'].includes(tagName) && element.style.fontFamily) {
                                const parentElement = element.closest('a');
                                const title = parentElement ? parentElement.getAttribute('title') : '';
                                const ariaLabel = parentElement ? parentElement.getAttribute('aria-label') : '';
                                label = `${title || ariaLabel}`.trim();
                                if (isValidXPath(elementXPath)) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: label,
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                            }
                        });

                        function getDomLevels(el1, el2) {
                            let levels = 0;
                            let current = el1;
                            while (current && current !== el2 && levels < 10) {
                                current = current.parentElement;
                                levels++;
                                if (current && current.contains(el2)) break;
                            }
                            return levels;
                        }

                        // Modify the part where we assign nearest label's text to input elements
                        elementMap.forEach((value, key) => {
                            if (value.tagName === 'input' && !value.element) {
                                // First check for value attribute
                                const inputElement = document.evaluate(key, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;


                                const labelElement = findContextualLabel(inputElement);
                                if (labelElement) {
                                    let labelText = '';

                                    if (labelElement.textContent && typeof labelElement.querySelector !== 'function') {
                                        labelText = cleanTextContent(labelElement.textContent);
                                    } else if (labelElement.querySelector && labelElement.innerText !== undefined) {
                                        labelText = labelElement.querySelector('span[id\$="-label"]') ?
                                            cleanTextContent(labelElement.querySelector('span[id\$="-label"]').innerText.trim()) :
                                            cleanTextContent(labelElement.innerText.trim());
                                    } else if (labelElement.textContent) {
                                        labelText = cleanTextContent(labelElement.textContent);
                                    }

                                    if (labelText) {
                                        value.element = labelText;
                                        return;
                                    }
                                }

                                // If still no label found, check nearby text nodes and elements
                                const parentElement = inputElement?.parentElement;
                                if (parentElement) {
                                    // Check preceding sibling
                                    const precedingSibling = inputElement.previousElementSibling;
                                    if (precedingSibling && precedingSibling.textContent.trim()) {
                                        value.element = cleanTextContent(precedingSibling.textContent);
                                        return;
                                    }

                                    // Check following sibling
                                    const followingSibling = inputElement.nextElementSibling;
                                    if (followingSibling && followingSibling.textContent.trim()) {
                                        value.element = cleanTextContent(followingSibling.textContent);
                                        return;
                                    }

                                    // Check parent's text content
                                    const parentText = Array.from(parentElement.childNodes)
                                        .filter(node => node.nodeType === 3) // Text nodes only
                                        .map(node => node.textContent.trim())
                                        .filter(text => text.length > 0)[0];

                                    if (parentText) {
                                        value.element = cleanTextContent(parentText);
                                    }
                                }
                                if (inputElement && inputElement.type === 'checkbox') {

                                    // Direct approach - find the container and p tag
                                    const container = inputElement.parentElement?.parentElement;
                                    if (container) {
                                        const pTag = container.querySelector('p');
                                        if (pTag) {
                                            const pText = pTag.textContent?.trim();
                                            if (pText) {
                                                value.element = cleanTextContent(pText);
                                            }
                                        }
                                    }
                                }

                                if (inputElement) {
                                    // Look up 2-3 levels for a container
                                    let container = inputElement.parentElement?.parentElement;
                                    if (container) {
                                        // Find text elements within container that aren't direct siblings
                                        const textElements = container.querySelectorAll('p, span, div');
                                        for (const textEl of textElements) {
                                            if (textEl.textContent?.trim() && !inputElement.parentElement.contains(textEl)) {
                                                value.element = cleanTextContent(textEl.textContent);
                                                return;
                                            }
                                        }
                                    }
                                }
                                if (inputElement) {
                                    const searchArea = inputElement.closest('form, fieldset, [class*="form"], [role="group"]') ||
                                        inputElement.parentElement?.parentElement?.parentElement;

                                    if (searchArea) {
                                        const candidates = [];
                                        const textElements = searchArea.querySelectorAll('p, span, div, label');

                                        textElements.forEach(textEl => {
                                            if (!textEl.textContent?.trim()) return;

                                            let score = 0;

                                            // Basic proximity scoring using your existing function
                                            if (typeof getElementDistance === 'function') {
                                                const distance = getElementDistance(textEl, inputElement);
                                                score += Math.max(0, 1 - distance / 100);
                                            } else {
                                                // Simple DOM level scoring
                                                const levels = getDomLevels(textEl, inputElement);
                                                score += Math.max(0, 1 - levels / 5);
                                            }

                                            // Prefer elements that come before input
                                            if (typeof isElementBefore === 'function' && isElementBefore(textEl, inputElement)) {
                                                score += 0.3;
                                            }

                                            // Text length preference (not too long, not too short)
                                            const textLength = textEl.textContent.trim().length;
                                            if (textLength > 5 && textLength < 100) {
                                                score += 0.2;
                                            }

                                            if (score > 0.2) {
                                                candidates.push({
                                                    element: textEl,
                                                    score
                                                });
                                            }
                                        });

                                        if (candidates.length > 0) {
                                            const bestCandidate = candidates.sort((a, b) => b.score - a.score)[0];
                                            value.element = cleanTextContent(bestCandidate.element.textContent);
                                            return;
                                        }
                                    }
                                }
                                if (inputElement && inputElement.getAttribute('value')) {
                                    value.element = cleanTextContent(inputElement.getAttribute('value'));
                                    return; // Exit early if value attribute is found
                                }
                            }
                        });


                        // Function to check if an element or any of its parents have aria-hidden="true"
                        function isAriaHidden(element) {
                            while (element) {
                                if (element.getAttribute('aria-disabled') === 'true') {
                                    return true;
                                }
                                element = element.parentElement;
                            }
                            while (element) {
                                if (element.getAttribute('aria-hidden') === 'true') {
                                    return true;
                                }
                                element = element.parentElement;
                            }

                            return false;
                        }


                        // Function to check if an element is visible in the viewport and not hidden by CSS or aria-hidden
                        function isVisible(element) {
                            if (element.tagName.toLowerCase() === 'option') {
                                return true; // Always consider options as visible
                            }
                            
                            // ✅ SPECIAL DEBUG: Track visibility for customer ID elements
                            const elementText = cleanTextContent(element.innerText || element.textContent || '').trim();
                            const isNumeric = /^\d+$/.test(elementText);
                            const isCustomerIDElement = isNumeric && (element.tagName.toLowerCase() === 'a' || element.tagName.toLowerCase() === 'td');
                            
                            if (isCustomerIDElement) {
                                console.log("DEBUG: Checking visibility for customer ID element:", {
                                    tagName: element.tagName.toLowerCase(),
                                    text: elementText,
                                    element: element
                                });
                            }
                            
                            // Check for active modal/dialog - focused on your pattern and similar
                            const activeModal = document.querySelector(
                                '[role="dialog"][aria-modal="true"], ' + // Your kib-modal pattern
                                '.kib-modal__container[role="dialog"], ' + // Backup for kib-modal
                                '.modal.show, .modal.fade.show, ' + // Bootstrap modals
                                '.MuiDialog-root.MuiModal-root' // Material-UI modals
                            );

                            if (activeModal) {
                                const modalStyle = window.getComputedStyle(activeModal);
                                if (modalStyle.display !== 'none' && modalStyle.visibility !== 'hidden') {
                                    // Modal is active - only show elements inside the modal
                                    return activeModal.contains(element);
                                }
                            }

                            const rect = element.getBoundingClientRect();
                            const style = window.getComputedStyle(element);
                            const isVisibleResult = (
                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                !element.closest('.styles_visuallyHidden__lnc9P') &&
                                !element.closest('.styles_showOnFocusOnly__1HndG') &&
                                !isAriaHidden(element)
                            );
                            
                            // ✅ SPECIAL DEBUG: Log visibility result for customer ID elements
                            if (isCustomerIDElement) {
                                console.log("DEBUG: Visibility result for customer ID element:", {
                                    tagName: element.tagName.toLowerCase(),
                                    text: elementText,
                                    visible: isVisibleResult,
                                    display: style.display,
                                    visibility: style.visibility,
                                    rect: {
                                        width: rect.width,
                                        height: rect.height,
                                        x: rect.x,
                                        y: rect.y
                                    }
                                });
                            }
                            
                            return isVisibleResult;
                        }

                        const filteredElements = Array.from(elementMap.values()).reduce((acc, element) => {
                            const existingElement = acc.find(e => e.element === element.element && e.xpath.startsWith(element.xpath.slice(0, Math.floor(element.xpath.length * 0.9))));
                            if (existingElement) {

                                // ✅ CRITICAL: For customer ID conflicts, prefer anchor tags over table cells
                                if (/^\d+$/.test(element.element.trim())) {
                                    // If current element is anchor tag and existing is table cell, replace with anchor
                                    if (element.tagName === 'a' && (existingElement.tagName === 'td' || existingElement.tagName === 'th')) {
                                        acc = acc.filter(e => e !== existingElement); // Remove table cell
                                        acc.push(element); // Add anchor tag
                                        console.log("DEBUG: Replaced table cell with anchor tag for customer ID:", element.element);
                                        return acc;
                                    }
                                    // If current element is table cell and existing is anchor, keep anchor
                                    if ((element.tagName === 'td' || element.tagName === 'th') && existingElement.tagName === 'a') {
                                        console.log("DEBUG: Keeping anchor tag over table cell for customer ID:", existingElement.element);
                                        return acc; // Keep existing anchor, skip table cell
                                    }
                                }

                                if (element.tagName === 'select' || existingElement.tagName === 'select') {
                                    acc.push(element);
                                    return acc;
                                }
                                if (element.tagName === 'li' || existingElement.tagName === 'li') {
                                    acc.push(element);
                                    return acc;
                                }
                                if (element.tagName === 'p' || existingElement.tagName === 'p') {
                                    acc.push(element);
                                    return acc;
                                }
                                if (element.tagName === 'img' || existingElement.tagName === 'img') {
                                    acc.push(element);
                                    return acc;
                                }
                                
                                // ✅ CRITICAL: Anchor tag handling - ensure customer ID links are preserved
                                if (element.tagName === 'a' || existingElement.tagName === 'a') {
                                    acc.push(element);
                                    return acc;
                                }
                                
                                // ✅ CRITICAL: Table cell handling - preserve customer ID table cells
                                if (element.tagName === 'td' || existingElement.tagName === 'td' || 
                                    element.tagName === 'th' || existingElement.tagName === 'th') {
                                    acc.push(element);
                                    return acc;
                                }
                                
                                // NEW LOGIC: If current element is button, replace existing
                                if (element.tagName === 'button') {
                                    acc = acc.filter(e => e !== existingElement); // Remove existing
                                    acc.push(element); // Add button
                                    return acc;
                                }

                                // NEW LOGIC: If existing is button, keep it and skip current
                                if (existingElement.tagName === 'button') {
                                    return acc; // Keep existing button, skip current element
                                }
                                if (existingElement.tagName.toLowerCase() === 'input') {
                                    // Keep both the existing input element and the current element
                                    acc.push(existingElement);
                                    acc.push(element);
                                } else if (element.tagName.toLowerCase() === 'input') {
                                    // Ensure input elements are always included
                                    acc.push(element);
                                } else if (element.xpath.length > existingElement.xpath.length) {
                                    acc = acc.filter(e => e !== existingElement);
                                    acc.push(element);
                                }
                            } else {
                                acc.push(element);
                            }
                            return acc;
                        }, []);


                        const uniqueElements = filteredElements.map((element) => {
                            const elements = document.evaluate(element.xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                            if (elements.snapshotLength > 1) {
                                for (let i = 0; i < elements.snapshotLength; i++) {
                                    const el = elements.snapshotItem(i);
                                    if (el.innerText.trim() === element.element) {
                                        element.xpath = `${element.xpath}[${i + 1}]`;
                                        break;
                                    }
                                }
                            }
                            return element;
                        });


                        const finalElements = uniqueElements.filter((element, index, self) => {
                            if (element.tagName === 'button' ||
                                element.tagName === 'a' ||
                                element.tagName === 'input' ||
                                element.tagName === 'p' ||
                                element.tagName === 'select' ||
                                element.tagName === 'li' ||
                                element.tagName === 'img' ||
                                element.tagName === 'td' ||    // ✅ ADD TABLE CELLS
                                element.tagName === 'th' ||    // ✅ ADD TABLE HEADERS
                                element.tagName === 'div' ||
                                element.isErrorMessage === true ||
                                element.hasMeaningfulId === true) { // Add meaningful ID condition
                                return true;
                            }
                            return !self.some((e) => e.xpath.startsWith(element.xpath) && e.xpath !== element.xpath);
                        });
                        // Perform visibility check at the end with lenient handling for anchor tags
                        const visibleFinalElements = finalElements.filter(element => {
                            const el = document.evaluate(element.xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                            if (!el) return false;
                            
                            // For anchor tags containing customer IDs, be more lenient with visibility
                            if (element.tagName === 'a' && element.element && /^\d+$/.test(element.element.trim())) {
                                console.log("DEBUG: Customer ID anchor tag found:", element.element, "xpath:", element.xpath, "visible:", isVisible(el));
                                // Only check if element exists, not strict visibility for customer ID links
                                return true;
                            }
                            
                            // For table cells containing customer IDs, be more lenient with visibility  
                            if ((element.tagName === 'td' || element.tagName === 'th') && element.element && /^\d+$/.test(element.element.trim())) {
                                console.log("DEBUG: Customer ID table cell found:", element.element, "xpath:", element.xpath, "visible:", isVisible(el));
                                // Only check if element exists, not strict visibility for customer ID cells
                                return true;
                            }
                            
                            return element.tagName === 'option' || isVisible(el);
                        });
                        console.log("DEBUG: Filtered elements:", elementMap.size);
                        console.log("DEBUG: Final visible elements:", visibleFinalElements.length);
                        console.log("DEBUG: Anchor tags in final elements:", visibleFinalElements.filter(e => e.tagName === 'a').length);

                        return visibleFinalElements;

                    } catch (error) {
                        console.warn('Error in extractDetails:', error);
                        return [];
                    }
                }

                return extractDetails();
            })();
        } catch (error) {
            console.error('Error in inject_html_context_retrieval:', error);
            return [];
        }
    }, 5000);
})();


/*
Previous Java script version

(function extractHtmlContext() {
    return (function waitForDomStableAndRunSync(callback, timeout) {
        let done = false;
        let result;

        const observer = new MutationObserver(() => {
            clearTimeout(window.__stabilizeTimer);
            window.__stabilizeTimer = setTimeout(() => {
                observer.disconnect();
                result = callback();
                done = true;
            }, 500);
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });

        setTimeout(() => {
            if (!done) {
                observer.disconnect();
                result = callback();
            }
        }, timeout || 5000);

        const start = Date.now();
        while (!done && (Date.now() - start) < (timeout || 5000)) { }

        return result || callback();
    })(() => {
        try {
            return (function () {
                function extractDetails() {
                    console.log("Inside extractDetails function");

                    const allElements = document.querySelectorAll('*');
                    console.log("Total elements found:", allElements.length);
                    try {
                        const safeQuerySelector = (selector) => {
                            try {
                                // If the selector is an ID selector and contains invalid characters, use getElementById instead
                                if (selector.startsWith('#')) {
                                    const id = selector.slice(1);
                                    return document.getElementById(id);
                                }
                                return document.querySelector(selector);
                            } catch (error) {
                                console.warn(`Invalid selector: ${selector}`, error);
                                return null;
                            }
                        };

                        const escapeSelector = (selector) => {
                            return CSS.escape(selector);
                        };

                        const generateXPath = (element) => {
                            if (!element) return '';
                            if (element === document.body) {
                                return '/html/body';
                            }
                            let index = 0;
                            let sibling = element.previousElementSibling;
                            while (sibling) {
                                if (sibling.nodeName === element.nodeName) {
                                    index++;
                                }
                                sibling = sibling.previousElementSibling;
                            }
                            const tagName = element.nodeName.toLowerCase();
                            const nth = index ? `[${index + 1}]` : '';
                            return `${generateXPath(element.parentNode)}/${tagName}${nth}`;
                        };

                        const isValidXPath = (xpath) => {
                            try {
                                document.evaluate(xpath, document, null, XPathResult.ANY_TYPE, null);
                                return true;
                            } catch (e) {
                                return false;
                            }
                        };

                        const cleanTextContent = (text) => {
                            return text.replace(/\\s+/g, ' ').trim();
                        };

                        const allElements = document.querySelectorAll('*');
                        const elementMap = new Map();

                        allElements.forEach((element) => {
                            const tagName = element.tagName.toLowerCase();

                            if (['script', 'style'].includes(tagName)) {
                                return;
                            }
                            const isAriaHidden = (element) => {
                                let current = element;

                                // Check the element itself and all parents
                                while (current) {
                                    if (current.getAttribute('aria-hidden') === 'true') {
                                        return true;
                                    }
                                    current = current.parentElement;
                                }
                                return false;
                            };

                            let label = cleanTextContent(element.innerText || element.textContent || '');
                            const elementXPath = generateXPath(element);
                            const rect = element.getBoundingClientRect();

                            if (['html', 'body'].includes(tagName) && label.length > 100) {
                                return;
                            }

                            if (tagName === 'label') {
                                const forAttribute = element.getAttribute('for');
                                if (forAttribute) {
                                    const associatedElement = safeQuerySelector(`#${escapeSelector(forAttribute)}`);
                                    if (associatedElement) {
                                        const associatedXPath = generateXPath(associatedElement);
                                        if (isValidXPath(associatedXPath)) {
                                            elementMap.set(associatedXPath, {
                                                tagName: associatedElement.tagName.toLowerCase(),
                                                xpath: associatedXPath,
                                                element: cleanTextContent(element.innerText.trim()),
                                                coordinates: {
                                                    x: rect.x,
                                                    y: rect.y,
                                                    width: rect.width,
                                                    height: rect.height,
                                                    center_x: rect.x + rect.width / 2,
                                                    center_y: rect.y + rect.height / 2
                                                }
                                            });
                                        }
                                    }
                                }
                            } else if (tagName === 'select') {
                                let parentLabel = '';

                                parentLabel = element.closest('label') ? cleanTextContent(element.closest('label').innerText.split('\\n')[0].trim()) : '';

                                if (!parentLabel && element.id) {
                                    const labelForElement = document.querySelector('label[for="' + CSS.escape(element.id) + '"]');

                                    if (labelForElement) {
                                        parentLabel = cleanTextContent(labelForElement.innerText);
                                    }
                                }
                                if (!parentLabel) {

                                    // Try preceding spans first (3 closest)
                                    const precedingSpans = document.evaluate('preceding::span[text()]', element, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                                    console.log('Preceding spans found:', precedingSpans.snapshotLength);

                                    for (let i = Math.max(0, precedingSpans.snapshotLength - 3); i < precedingSpans.snapshotLength; i++) {
                                        const span = precedingSpans.snapshotItem(i);
                                        console.log('Checking preceding span:', span, 'Text:', span.textContent.trim());
                                        if (span.textContent.trim() && span.textContent.length < 50) {
                                            parentLabel = cleanTextContent(span.textContent);
                                            console.log('Found parentLabel from preceding span:', parentLabel);
                                            break;
                                        }
                                    }

                                    // If not found, try following spans (3 closest)
                                    if (!parentLabel) {
                                        const followingSpans = document.evaluate('following::span[text()]', element, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);

                                        for (let i = 0; i < Math.min(3, followingSpans.snapshotLength); i++) {
                                            const span = followingSpans.snapshotItem(i);
                                            console.log('Checking following span:', span, 'Text:', span.textContent.trim());
                                            if (span.textContent.trim() && span.textContent.length < 50) {
                                                parentLabel = cleanTextContent(span.textContent);
                                                console.log('Found parentLabel from following span:', parentLabel);
                                                break;
                                            }
                                        }
                                    }

                                }

                                if (isValidXPath(elementXPath)) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: parentLabel,
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                                Array.from(element.options).forEach((option, index) => {
                                    const optionRect = option.getBoundingClientRect();
                                    const optionXPath = `${elementXPath}/option[${index + 1}]`;
                                    if (isValidXPath(optionXPath)) {
                                        elementMap.set(`${elementXPath}${optionXPath}`, {
                                            tagName: 'option',
                                            element: `${cleanTextContent(option.text)}`,
                                            xpath: optionXPath,
                                            coordinates: {
                                                x: optionRect.x,
                                                y: optionRect.y,
                                                width: optionRect.width,
                                                height: optionRect.height,
                                                center_x: optionRect.x + optionRect.width / 2,
                                                center_y: optionRect.y + optionRect.height / 2
                                            }
                                        });
                                    }
                                });
                            } else if (tagName === 'p') {
                                let directText = '';
                                let fullText = '';

                                // Extract direct text nodes (not from children)
                                for (let i = 0; i < element.childNodes.length; i++) {
                                    const node = element.childNodes[i];
                                    if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                                        directText += node.textContent + ' ';
                                    }
                                }

                                // Clean up direct text
                                directText = cleanTextContent(directText);

                                // Get full text content including children
                                fullText = cleanTextContent(element.textContent || '');

                                // Decide which text to use based on availability and relevance
                                let finalText = '';

                                // If direct text exists and is substantial, use it
                                if (directText && directText.length > 1) {
                                    finalText = directText;
                                }
                                // If no direct text or it's minimal, use full text including children
                                else if (fullText && fullText.length > 1) {
                                    finalText = fullText;
                                }
                                if (finalText && finalText.trim().split(/\s+/).length > 30) {
                                    return;
                                }

                                // Only add if we have meaningful text content
                                if (finalText && isValidXPath(elementXPath)) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: finalText,
                                        hasDirectText: directText.length > 0, // Flag to indicate if it has direct text
                                        hasChildText: fullText.length > directText.length, // Flag to indicate if it has child text
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                            } else if (tagName === 'li') {
                                // Only check for clickability if LI has explicit interactive attributes
                                const isClickable = (
                                    // LI has interactive ARIA roles
                                    ['option', 'menuitem', 'button', 'tab', 'treeitem', 'gridcell', 'row', 'link', 'checkbox', 'radio'].includes(element.getAttribute('role')) ||

                                    // LI has click handlers
                                    element.onclick || element.getAttribute('onclick') ||

                                    // LI has tabindex (focusable)
                                    (element.hasAttribute('tabindex') && element.getAttribute('tabindex') !== '-1') ||

                                    // LI has explicit interactive data attributes
                                    element.hasAttribute('data-toggle') ||
                                    element.hasAttribute('data-action') ||
                                    element.hasAttribute('data-click') ||

                                    // LI has aria attributes indicating interactivity
                                    element.hasAttribute('aria-pressed') ||
                                    element.hasAttribute('aria-expanded') ||
                                    element.hasAttribute('aria-selected')
                                );


                                if (isClickable) {
                                    let textContent = '';

                                    // 1. First priority: aria-label
                                    textContent = element.getAttribute('aria-label');

                                    // 2. Second priority: direct text content
                                    if (!textContent) {
                                        let directText = '';
                                        for (let i = 0; i < element.childNodes.length; i++) {
                                            const node = element.childNodes[i];
                                            if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                                                directText += node.textContent + ' ';
                                            }
                                        }
                                        textContent = cleanTextContent(directText);
                                    }

                                    // 3. Third priority: child text content (excluding aria-hidden elements)
                                    if (!textContent) {
                                        const childElements = element.querySelectorAll('*:not([aria-hidden="true"])');
                                        for (const child of childElements) {
                                            if (child.textContent && child.textContent.trim()) {
                                                textContent = cleanTextContent(child.textContent);
                                                break;
                                            }
                                        }
                                    }

                                    // Clean the final text content
                                    textContent = textContent ? cleanTextContent(textContent) : '';

                                    if (textContent && isValidXPath(elementXPath)) {
                                        elementMap.set(elementXPath, {
                                            tagName: tagName,
                                            xpath: elementXPath,
                                            element: textContent,
                                            clickable: 'yes',
                                            coordinates: {
                                                x: rect.x,
                                                y: rect.y,
                                                width: rect.width,
                                                height: rect.height,
                                                center_x: rect.x + rect.width / 2,
                                                center_y: rect.y + rect.height / 2
                                            }
                                        });
                                    }
                                }
                            } else if (tagName === 'div') {
                                // Check if this is an error message div by class name
                                const isErrorMessage = element.className &&
                                    element.className.indexOf('error') !== -1;

                                // Check if this div has an interactive role
                                const role = element.getAttribute('role');
                                const isInteractiveRole = role && [
                                    'button', 'tab', 'link', 'menuitem', 'option',
                                    'checkbox', 'radio', 'switch', 'combobox',
                                    'listbox', 'gridcell', 'columnheader', 'rowheader',
                                    'treeitem', 'disclosure', 'slider'
                                ].includes(role.toLowerCase());

                                if (isErrorMessage) {
                                    // For error messages, get the full text content
                                    const errorText = element.textContent.trim();
                                    if (errorText && isValidXPath(elementXPath)) {
                                        elementMap.set(elementXPath, {
                                            tagName: tagName,
                                            xpath: elementXPath,
                                            element: errorText,
                                            isErrorMessage: true, // Flag as error message
                                            coordinates: {
                                                x: rect.x,
                                                y: rect.y,
                                                width: rect.width,
                                                height: rect.height,
                                                center_x: rect.x + rect.width / 2,
                                                center_y: rect.y + rect.height / 2
                                            }
                                        });
                                    }
                                } else if (isInteractiveRole) {
                                    // Handle interactive divs with roles
                                    let roleLabel = element.getAttribute('aria-label') ||
                                        element.getAttribute('aria-labelledby') ||
                                        cleanTextContent(element.innerText || element.textContent || '') ||
                                        element.getAttribute('title') ||
                                        element.getAttribute('data-tooltip') ||
                                        '';

                                    // If aria-labelledby is used, get the referenced element's text
                                    if (element.getAttribute('aria-labelledby')) {
                                        const labelId = element.getAttribute('aria-labelledby');
                                        const labelElement = document.getElementById(labelId);
                                        if (labelElement) {
                                            roleLabel = cleanTextContent(labelElement.textContent);
                                        }
                                    }

                                    if (roleLabel && isValidXPath(elementXPath)) {
                                        elementMap.set(elementXPath, {
                                            tagName: tagName,
                                            xpath: elementXPath,
                                            element: roleLabel,
                                            role: role, // Store the role for reference
                                            isInteractive: true, // Flag as interactive
                                            coordinates: {
                                                x: rect.x,
                                                y: rect.y,
                                                width: rect.width,
                                                height: rect.height,
                                                center_x: rect.x + rect.width / 2,
                                                center_y: rect.y + rect.height / 2
                                            }
                                        });
                                    }
                                } else {
                                    // Original code for non-error divs
                                    let hasDirectTextContent = false;
                                    let directText = '';
                                    // Check for direct text nodes (not from children)
                                    for (let i = 0; i < element.childNodes.length; i++) {
                                        const node = element.childNodes[i];
                                        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                                            hasDirectTextContent = true;
                                            directText += node.textContent + ' ';
                                        }
                                    }
                                    // Clean up the collected direct text
                                    directText = cleanTextContent(directText);
                                    // Only add div if it has direct text content (not just from children)
                                    if (hasDirectTextContent && directText) {
                                        if (isValidXPath(elementXPath)) {
                                            elementMap.set(elementXPath, {
                                                tagName: tagName,
                                                xpath: elementXPath,
                                                element: directText,
                                                coordinates: {
                                                    x: rect.x,
                                                    y: rect.y,
                                                    width: rect.width,
                                                    height: rect.height,
                                                    center_x: rect.x + rect.width / 2,
                                                    center_y: rect.y + rect.height / 2
                                                }
                                            });
                                        }
                                    }
                                }
                            } else if (tagName === 'input' && element.type === 'radio') {
                                const radioWrapper = element.closest('label');
                                const associatedLabel = radioWrapper ? radioWrapper.textContent : '';
                                label = cleanTextContent(associatedLabel);
                                if (isValidXPath(elementXPath)) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: label,
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                            } else {
                                if (isAriaHidden(element)) {
                                    return;
                                }
                                if (!label) {
                                    label = element.getAttribute('aria-label') || element.getAttribute('placeholder') || element.getAttribute('title') || '';
                                } else {
                                    // Concatenate text content of parent element if it contains a <b> tag
                                    const parentElement = element.closest('div');
                                    if (parentElement && parentElement.querySelector('b')) {
                                        label = cleanTextContent(parentElement.textContent);
                                    }
                                }
                                if (isValidXPath(elementXPath) && (!elementMap.has(elementXPath) || (elementMap.has(elementXPath) && label))) {
                                    elementMap.set(elementXPath, {
                                        tagName: tagName,
                                        xpath: elementXPath,
                                        element: label,
                                        coordinates: {
                                            x: rect.x,
                                            y: rect.y,
                                            width: rect.width,
                                            height: rect.height,
                                            center_x: rect.x + rect.width / 2,
                                            center_y: rect.y + rect.height / 2
                                        }
                                    });
                                }
                            }

                            // Inside the allElements.forEach loop, modify the button handling part:
                            if (tagName === 'button') {
                                // Prioritize aria-label for buttons
                                label = element.getAttribute('aria-label') ||
                                    cleanTextContent(element.innerText || element.textContent || '') ||
                                    element.getAttribute('title') ||
                                    element.getAttribute('name') ||
                                    '';

                                elementMap.set(elementXPath, {
                                    tagName: tagName,
                                    xpath: elementXPath,
                                    element: label,
                                    coordinates: {
                                        x: rect.x,
                                        y: rect.y,
                                        width: rect.width,
                                        height: rect.height,
                                        center_x: rect.x + rect.width / 2,
                                        center_y: rect.y + rect.height / 2
                                    }
                                });
                            }

                            function extractVisibleText(node) {
                                let text = '';
                                for (const child of node.childNodes) {
                                    if (child.nodeType === Node.TEXT_NODE) {
                                        text += child.textContent;
                                    } else if (
                                        child.nodeType === Node.ELEMENT_NODE &&
                                        !['svg', 'path', 'g', 'use', 'style'].includes(child.tagName.toLowerCase())
                                    ) {
                                        text += ' ' + extractVisibleText(child);
                                    }
                                }
                                return text;
                            }

                            if (['span', 'a', 'button'].includes(tagName)) {
                                const isLikelyPureIconSpan =
                                    tagName === 'span' &&
                                    element.children.length === 1 && ['svg', 'path', 'g', 'use'].includes(element.children[0]?.tagName?.toLowerCase());

                                if (!isLikelyPureIconSpan) {
                                    const ancestor = element.closest('button, a, label') || element;

                                    let label = '';
                                    try {
                                        const clone = ancestor.cloneNode(true);

                                        // Only remove decorative elements (icons), not real content
                                        clone.querySelectorAll('svg, path, g, use, style').forEach(n => n.remove());

                                        label = cleanTextContent(extractVisibleText(clone));
                                        label = label.replace(/([a-z])([A-Z])/g, '$1 $2');
                                    } catch (e) {
                                        label = cleanTextContent(extractVisibleText(ancestor));
                                        label = label.replace(/([a-z])([A-Z])/g, '$1 $2');
                                    }

                                    const stableXPath = generateXPath(ancestor);
                                    if (label && isValidXPath(stableXPath)) {
                                        elementMap.set(stableXPath, {
                                            tagName: ancestor.tagName.toLowerCase(),
                                            xpath: stableXPath,
                                            element: label.trim(),
                                            coordinates: {
                                                x: rect.x,
                                                y: rect.y,
                                                width: rect.width,
                                                height: rect.height,
                                                center_x: rect.x + rect.width / 2,
                                                center_y: rect.y + rect.height / 2
                                            }
                                        });
                                    }
                                }
                            }
                        });

                        // Modify the part where we assign nearest label's text to input elements
                        elementMap.forEach((value, key) => {
                            if (value.tagName === 'input' && !value.element) {
                                // First check for value attribute
                                const inputElement = document.evaluate(key, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                                if (inputElement && inputElement.getAttribute('value')) {
                                    value.element = cleanTextContent(inputElement.getAttribute('value'));
                                    return; // Exit early if value attribute is found
                                }

                                // Then check for associated label
                                const labelElement = document.evaluate(key + "/preceding::label[1]", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                                if (labelElement) {
                                    const labelText = labelElement.querySelector('span[id\$="-label"]') ?
                                        cleanTextContent(labelElement.querySelector('span[id\$="-label"]').innerText.trim()) :
                                        cleanTextContent(labelElement.innerText.trim());
                                    if (labelText) {
                                        value.element = labelText;
                                        return;
                                    }
                                }

                                // Special handling for phone input fields
                                if (inputElement && (inputElement.getAttribute('name') === 'phone' || inputElement.getAttribute('type') === 'tel')) {
                                    // Try to find the label by looking for label element with specific class or content
                                    const labelSpan = inputElement.closest('.kib-input-text')?.querySelector('.kib-input-text__label-content');
                                    if (labelSpan && labelSpan.textContent.trim()) {
                                        value.element = cleanTextContent(labelSpan.textContent);
                                        return;
                                    }
                                    // Fallback to generic phone number
                                    value.element = 'Phone Number';
                                    return;
                                }

                                // If still no label found, check nearby text nodes and elements
                                const parentElement = inputElement?.parentElement;
                                if (parentElement) {
                                    // Check preceding sibling
                                    const precedingSibling = inputElement.previousElementSibling;
                                    if (precedingSibling && precedingSibling.textContent.trim()) {
                                        value.element = cleanTextContent(precedingSibling.textContent);
                                        return;
                                    }

                                    // Check following sibling
                                    const followingSibling = inputElement.nextElementSibling;
                                    if (followingSibling && followingSibling.textContent.trim()) {
                                        value.element = cleanTextContent(followingSibling.textContent);
                                        return;
                                    }

                                    // Check parent's text content
                                    const parentText = Array.from(parentElement.childNodes)
                                        .filter(node => node.nodeType === 3) // Text nodes only
                                        .map(node => node.textContent.trim())
                                        .filter(text => text.length > 0)[0];

                                    if (parentText) {
                                        value.element = cleanTextContent(parentText);
                                    }
                                }
                            }
                        });

                        // Function to check if an element or any of its parents have aria-hidden="true"
                        function isAriaHidden(element) {
                            while (element) {
                                if (element.getAttribute('aria-disabled') === 'true') {
                                    return true;
                                }
                                element = element.parentElement;
                            }
                            while (element) {
                                if (element.getAttribute('aria-hidden') === 'true') {
                                    return true;
                                }
                                element = element.parentElement;
                            }

                            return false;
                        }


                        // Function to check if an element is visible in the viewport and not hidden by CSS or aria-hidden
                        function isVisible(element) {
                            if (element.tagName.toLowerCase() === 'option') {
                                return true; // Always consider options as visible
                            }

                            const rect = element.getBoundingClientRect();
                            const style = window.getComputedStyle(element);
                            return (

                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                !element.closest('.styles_visuallyHidden__lnc9P') &&
                                !element.closest('.styles_showOnFocusOnly__1HndG') &&
                                !isAriaHidden(element)
                            );
                        }

                        const filteredElements = Array.from(elementMap.values()).reduce((acc, element) => {
                            const existingElement = acc.find(e => e.element === element.element && e.xpath.startsWith(element.xpath.slice(0, Math.floor(element.xpath.length * 0.9))));
                            if (existingElement) {

                                if (element.tagName === 'select' || existingElement.tagName === 'select') {
                                    acc.push(element);
                                    return acc;
                                }
                                if (element.tagName === 'li' || existingElement.tagName === 'li') {
                                    acc.push(element);
                                    return acc;
                                }
                                if (element.tagName === 'p' || existingElement.tagName === 'p') {
                                    acc.push(element);
                                    return acc;
                                }
                                
                                // ✅ CRITICAL: Anchor tag handling - ensure customer ID links are preserved
                                if (element.tagName === 'a' || existingElement.tagName === 'a') {
                                    acc.push(element);
                                    return acc;
                                }
                                
                                // ✅ CRITICAL: Table cell handling - preserve customer ID table cells
                                if (element.tagName === 'td' || existingElement.tagName === 'td' || 
                                    element.tagName === 'th' || existingElement.tagName === 'th') {
                                    acc.push(element);
                                    return acc;
                                }
                                
                                if (existingElement.tagName.toLowerCase() === 'input') {
                                    // Keep both the existing input element and the current element
                                    acc.push(existingElement);
                                    acc.push(element);
                                } else if (element.tagName.toLowerCase() === 'input') {
                                    // Ensure input elements are always included
                                    acc.push(element);
                                } else if (element.xpath.length > existingElement.xpath.length) {
                                    acc = acc.filter(e => e !== existingElement);
                                    acc.push(element);
                                }
                            } else {
                                acc.push(element);
                            }
                            return acc;
                        }, []);


                        const uniqueElements = filteredElements.map((element) => {
                            const elements = document.evaluate(element.xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                            if (elements.snapshotLength > 1) {
                                for (let i = 0; i < elements.snapshotLength; i++) {
                                    const el = elements.snapshotItem(i);
                                    if (el.innerText.trim() === element.element) {
                                        element.xpath = `${element.xpath}[${i + 1}]`;
                                        break;
                                    }
                                }
                            }
                            return element;
                        });


                        const finalElements = uniqueElements.filter((element, index, self) => {
                            if (element.tagName === 'button' ||
                                element.tagName === 'a' ||
                                element.tagName === 'input' ||
                                element.tagName === 'p' ||
                                element.tagName === 'select' ||
                                element.tagName === 'li' ||
                                element.isErrorMessage === true) { // Add this condition
                                return true;
                            }
                            return !self.some((e) => e.xpath.startsWith(element.xpath) && e.xpath !== element.xpath);
                        });
                        // Perform visibility check at the end with lenient handling for anchor tags
                        const visibleFinalElements = finalElements.filter(element => {
                            const el = document.evaluate(element.xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                            if (!el) return false;
                            
                            // For anchor tags containing customer IDs, be more lenient with visibility
                            if (element.tagName === 'a' && element.element && /^\d+$/.test(element.element.trim())) {
                                console.log("DEBUG: Customer ID anchor tag found:", element.element, "xpath:", element.xpath, "visible:", isVisible(el));
                                // Only check if element exists, not strict visibility for customer ID links
                                return true;
                            }
                            
                            // For table cells containing customer IDs, be more lenient with visibility  
                            if ((element.tagName === 'td' || element.tagName === 'th') && element.element && /^\d+$/.test(element.element.trim())) {
                                console.log("DEBUG: Customer ID table cell found:", element.element, "xpath:", element.xpath, "visible:", isVisible(el));
                                // Only check if element exists, not strict visibility for customer ID cells
                                return true;
                            }
                            
                            return element.tagName === 'option' || isVisible(el);
                        });
                        console.log("DEBUG: Filtered elements:", elementMap.size);
                        console.log("DEBUG: Final visible elements:", visibleFinalElements.length);
                        console.log("DEBUG: Anchor tags in final elements:", visibleFinalElements.filter(e => e.tagName === 'a').length);
                        
                        // Debug: Show all anchor tags that made it to final elements
                        const anchorElements = visibleFinalElements.filter(e => e.tagName === 'a');
                        if (anchorElements.length > 0) {
                            console.log("DEBUG: Final anchor elements:", anchorElements.map(e => ({
                                text: e.element,
                                xpath: e.xpath
                            })));
                        } else {
                            console.log("DEBUG: No anchor tags in final elements - checking elementMap...");
                            const allAnchors = Array.from(elementMap.values()).filter(e => e.tagName === 'a');
                            console.log("DEBUG: Anchor tags in elementMap:", allAnchors.length, allAnchors.slice(0, 3).map(e => ({
                                text: e.element,
                                xpath: e.xpath
                            })));
                        }
                        
                        // Debug: Show all table cells that made it to final elements
                        const tableCells = visibleFinalElements.filter(e => e.tagName === 'td' || e.tagName === 'th');
                        console.log("DEBUG: Final table cells:", tableCells.length, tableCells.map(e => ({
                            text: e.element,
                            xpath: e.xpath,
                            tagName: e.tagName,
                            isNumeric: /^\d+$/.test(e.element)
                        })));
                        
                        // Debug: Show numeric table cells specifically
                        const numericCells = tableCells.filter(e => /^\d+$/.test(e.element));
                        if (numericCells.length > 0) {
                            console.log("DEBUG: Numeric table cells found:", numericCells.map(e => ({
                                text: e.element,
                                xpath: e.xpath
                            })));
                        } else {
                            console.log("DEBUG: No numeric table cells in final elements");
                        }

                        return visibleFinalElements;
                    } catch (error) {
                        console.warn('Error in extractDetails:', error);
                        return [];
                    }
                }

                return extractDetails();
            })();

        } catch (error) {
            console.error('Error in inject_html_context_retrieval:', error);
            return [];
        }
    }, 5000);
})();

*/
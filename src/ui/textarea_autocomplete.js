/**
 * Textarea Autocomplete Service
 * Provides real-time step suggestions as user types in textarea
 * Fetches suggestions from browser local storage based on selected module
 */

console.log('📦 loading textarea_autocomplete.js');

// Global state for autocomplete
let autocompleteState = {
    isOpen: false,
    selectedIndex: -1,
    filteredSuggestions: [],
    currentModule: null,
    currentSearchTerm: '',
    textareaEl: null,
    containerEl: null,
    inputDebounceTimer: null,
    suppressOpenUntil: 0,
};

/**
 * Initialize textarea autocomplete functionality
 * Should be called after DOM is ready and migration data is loaded
 */
function initializeTextareaAutocomplete() {
    initializeTextareaAutocompleteFor('csWorkflowSteps', 'csProjectDomain');
}

/**
 * Initialize autocomplete for a specific textarea and project domain by element IDs.
 * Use this for Create Workflow (e.g. initializeTextareaAutocompleteFor('createWorkflow', 'selectDomain')).
 */
function initializeTextareaAutocompleteFor(textareaId, projectDomainId) {
    const textarea = document.getElementById(textareaId);
    const projectDomain = document.getElementById(projectDomainId);
    if (!textarea || !projectDomain) {
        console.warn('⚠ Textarea or project domain element not found:', { textareaId, projectDomainId });
        return;
    }
    if (textarea.dataset.autocompleteInitialized === 'true') {
        return;
    }
    autocompleteState.textareaEl = textarea;
    const container = createAutocompleteContainer(textarea);
    autocompleteState.containerEl = container;
    textarea.dataset.autocompleteInitialized = 'true';

    textarea.addEventListener('input', (e) => {
        handleTextareaInput(e, projectDomain);
    });
    textarea.addEventListener('keydown', (e) => {
        handleTextareaKeydown(e);
    });
    document.addEventListener('click', (e) => {
        if (e.target !== textarea && !e.target.closest('.autocomplete-suggestions')) {
            closeAutocomplete();
        }
    });
}

/**
 * Create the autocomplete suggestions container below textarea
 * @returns {HTMLElement} The container element
 */
function createAutocompleteContainer(textarea) {
    const wrapper = textarea.parentElement;
    const existingContainer = wrapper.querySelector('.autocomplete-suggestions');
    if (existingContainer) {
        return existingContainer;
    }

    const container = document.createElement('div');
    container.id = 'autocomplete-container';
    container.className = 'autocomplete-suggestions';
    container.style.cssText = `
        position: absolute;
        top: 100%;
        left: 0;
        right: 0;
        background: #fff;
        border: 1px solid #ddd;
        border-top: none;
        border-radius: 0 0 4px 4px;
        max-height: 300px;
        overflow-y: auto;
        z-index: 1000;
        display: none;
        box-shadow: 0 4px 8px rgba(0,0,0,0.08);
    `;

    wrapper.style.position = 'relative';
    wrapper.appendChild(container);
    return container;
}

/**
 * Handle textarea input event - filter and show suggestions
 */
function handleTextareaInput(event, projectDomain) {
    clearTimeout(autocompleteState.inputDebounceTimer);
    autocompleteState.inputDebounceTimer = setTimeout(function () {
        doHandleTextareaInput(event, projectDomain);
    }, 120);
}

function doHandleTextareaInput(event, projectDomain) {
    const textarea = event.target;
    const module = projectDomain.value;

    if (autocompleteState.suppressOpenUntil && Date.now() < autocompleteState.suppressOpenUntil) {
        closeAutocomplete();
        return;
    }

    if (!module) {
        closeAutocomplete();
        return;
    }

    if (typeof getStepsForModuleFromLocalStorage !== 'function') {
        closeAutocomplete();
        return;
    }

    // Get the text from the last line being typed
    const text = textarea.value;
    const lines = text.split('\n');
    const currentLine = lines[lines.length - 1];

    // Get the search term (text after last space or special chars)
    const searchTerm = currentLine.trim();

    if (searchTerm.length < 2) {
        closeAutocomplete();
        return;
    }

    // Fetch suggestions from local storage
    const allSteps = getStepsForModuleFromLocalStorage(module);

    if (!allSteps || allSteps.length === 0) {
        closeAutocomplete();
        return;
    }

    // Filter steps based on search term (case-insensitive)
    const filtered = allSteps.filter(step => {
        // Search in step field (CSV) or keyStep (API) or cmnSteps array
        const stepText = step.step || '';
        const keyStepText = step.keyStep || '';
        const cmnStepsText = Array.isArray(step.cmnSteps) ? step.cmnSteps.join(' ') : (step.cmnSteps || '');
        const searchText = (stepText + ' ' + keyStepText + ' ' + cmnStepsText).toLowerCase();
        return searchText.includes(searchTerm.toLowerCase());
    }).slice(0, 10); // Limit to 10 suggestions

    if (filtered.length === 0) {
        closeAutocomplete();
        return;
    }

    // Update state and show suggestions
    autocompleteState.filteredSuggestions = filtered;
    autocompleteState.currentModule = module;
    autocompleteState.currentSearchTerm = searchTerm;
    autocompleteState.selectedIndex = -1;

    renderSuggestions(textarea, filtered);
    openAutocomplete(textarea);
}

/**
 * Handle textarea keyboard navigation
 */
function handleTextareaKeydown(event) {
    if (!autocompleteState.isOpen) {
        return;
    }

    const { filteredSuggestions, selectedIndex } = autocompleteState;
    const suggestionsCount = filteredSuggestions.length;

    switch (event.key) {
        case 'ArrowDown':
            event.preventDefault();
            autocompleteState.selectedIndex = Math.min(selectedIndex + 1, suggestionsCount - 1);
            updateSuggestionsUI();
            break;

        case 'ArrowUp':
            event.preventDefault();
            autocompleteState.selectedIndex = Math.max(selectedIndex - 1, -1);
            updateSuggestionsUI();
            break;

        case 'Enter':
            event.preventDefault();
            if (selectedIndex >= 0) {
                selectSuggestion(selectedIndex);
            }
            break;

        case 'Escape':
            event.preventDefault();
            closeAutocomplete();
            break;

        default:
            break;
    }
}

/**
 * Render suggestions in the autocomplete container
 */
function renderSuggestions(textarea, suggestions) {
    const container = autocompleteState.containerEl;
    if (!container) return;

    container.innerHTML = '';

    // Dynamically adjust styling based on number of suggestions
    const suggestionCount = suggestions.length;
    const padding = suggestionCount > 5 ? '8px' : '10px';
    const fontSize = suggestionCount > 8 ? '12px' : '13px';
    const maxHeight = suggestionCount > 10 ? '300px' : (suggestionCount * 40) + 'px';
    
    container.style.maxHeight = maxHeight;

    suggestions.forEach((step, index) => {
        const item = document.createElement('div');
        item.className = 'autocomplete-item';
        item.dataset.index = index;
        item.style.cssText = `
            padding: ${padding} 12px;
            cursor: pointer;
            border-bottom: 1px solid #f5f5f5;
            font-size: ${fontSize};
            color: #222;
            transition: background-color 0.1s ease, padding 0.1s ease;
        `;

        // Use step field from CSV or keyStep from API as the display label
        const displayText = step.keyStep || step.step || '';
        const preview = displayText.substring(0, 70) + (displayText.length > 70 ? '...' : '');
        item.innerHTML = `<span style="color: #1a73e8;">${escapeHtml(preview)}</span>`;

        item.addEventListener('mouseenter', () => {
            autocompleteState.selectedIndex = index;
            updateSuggestionsUI();
        });

        item.addEventListener('click', (e) => {
            e.preventDefault();
            selectSuggestion(index);
        });

        container.appendChild(item);
    });
}

/**
 * Update visual selection in suggestions UI
 */
function updateSuggestionsUI() {
    const items = document.querySelectorAll('.autocomplete-item');
    const { selectedIndex } = autocompleteState;

    items.forEach((item, index) => {
        if (index === selectedIndex) {
            item.style.backgroundColor = '#f1f3f4';
            item.style.fontWeight = '500';
        } else {
            item.style.backgroundColor = 'transparent';
            item.style.fontWeight = 'normal';
        }
    });
}

/**
 * Insert selected suggestion into textarea
 */
function selectSuggestion(index) {
    const suggestion = autocompleteState.filteredSuggestions[index];
    if (!suggestion) return;

    const textarea = autocompleteState.textareaEl;
    if (!textarea) return;

    // Handle both CSV format (step field) and API format (cmnSteps array)
    let stepText = suggestion.step || suggestion.cmnSteps || suggestion.steps || '';
    if (Array.isArray(stepText)) {
        stepText = stepText.join('\n');
    }
    stepText = String(stepText || '');

    // Get current text and replace the current line with the suggestion
    const text = textarea.value;
    const lines = text.split('\n');
    
    // Replace the last line with the suggestion
    lines[lines.length - 1] = stepText;
    textarea.value = lines.join('\n') + '\n';

    // Trigger change event for any listeners
    textarea.dispatchEvent(new Event('change', { bubbles: true }));
    textarea.focus();

    closeAutocomplete();
    autocompleteState.suppressOpenUntil = Date.now() + 400;
}

/**
 * Open autocomplete dropdown
 */
function openAutocomplete(textarea) {
    const container = autocompleteState.containerEl;
    if (!container) return;

    container.style.display = 'block';
    autocompleteState.isOpen = true;
}

/**
 * Close autocomplete dropdown
 */
function closeAutocomplete() {
    const container = autocompleteState.containerEl;
    if (!container) return;

    container.style.display = 'none';
    container.innerHTML = '';
    autocompleteState.isOpen = false;
    autocompleteState.selectedIndex = -1;
    autocompleteState.filteredSuggestions = [];
}

/**
 * Clear and reset autocomplete state
 */
function resetAutocompleteState() {
    clearTimeout(autocompleteState.inputDebounceTimer);
    autocompleteState = {
        isOpen: false,
        selectedIndex: -1,
        filteredSuggestions: [],
        currentModule: null,
        currentSearchTerm: '',
        textareaEl: autocompleteState.textareaEl,
        containerEl: autocompleteState.containerEl,
        inputDebounceTimer: null,
        suppressOpenUntil: 0,
    };
    closeAutocomplete();
}

/**
 * Get current autocomplete state (useful for debugging)
 */
function getAutocompleteState() {
    return {
        ...autocompleteState,
        suggestionsCount: autocompleteState.filteredSuggestions.length,
    };
}

/**
 * Utility: Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Add custom styling for autocomplete (run once on page load)
 */
function addAutocompleteStyles() {
    if (document.getElementById('autocomplete-styles')) {
        return; // Already added
    }

    const style = document.createElement('style');
    style.id = 'autocomplete-styles';
    style.textContent = `
        .autocomplete-suggestions {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
        }

        .autocomplete-item {
            word-break: break-word;
            white-space: normal;
        }

        .autocomplete-item:hover {
            background-color: #e8f0ff !important;
        }

        .autocomplete-item:last-child {
            border-bottom: none;
        }

        /* Scrollbar styling */
        .autocomplete-suggestions::-webkit-scrollbar {
            width: 8px;
        }

        .autocomplete-suggestions::-webkit-scrollbar-track {
            background: #f1f1f1;
        }

        .autocomplete-suggestions::-webkit-scrollbar-thumb {
            background: #888;
            border-radius: 4px;
        }

        .autocomplete-suggestions::-webkit-scrollbar-thumb:hover {
            background: #555;
        }
    `;

    document.head.appendChild(style);
    console.log('✓ Autocomplete styles added');
}

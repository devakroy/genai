/**
 * Create Steps UI - Project Domain and Automation Script Type only
 */

const API_BASE_URL = window.location.origin;
let selectDomainInput, automationScriptTypeInput, conditionalFields, workflowNameInput, createWorkflowInput, commonStepsListEl;
/** Steps added by drag-and-drop; on Save, these get "# step-macro: " prefix in the downloaded file only. */
const draggedStepLines = new Set();

document.addEventListener('DOMContentLoaded', async () => {
    var saveWorkflowBtnEl = document.getElementById('saveWorkflowBtn');
    if (saveWorkflowBtnEl) {
        saveWorkflowBtnEl.disabled = true;
        saveWorkflowBtnEl.setAttribute('disabled', 'disabled');
    }
    selectDomainInput = document.getElementById('selectDomain');
    automationScriptTypeInput = document.getElementById('automationScriptType');
    conditionalFields = document.getElementById('conditionalFields');
    workflowNameInput = document.getElementById('workflowName');
    createWorkflowInput = document.getElementById('createWorkflow');
    commonStepsListEl = document.getElementById('commonStepsList');
    populateProjectDomainDropdown();
    setupConditionalFields();
    setupDragAndDropKeyLineToWorkflow();
    setupKeyLineClickQuery();
    setupReuseWorkflowStepsLink();
    setupAddSelectedStepsListener();
    setupSaveWorkflowButton();
    setupShowStepsPreviewButton();
    if (typeof loadMigrationFileToLocalStorage === 'function') {
        await loadMigrationFileToLocalStorage();
    }
    if (typeof addAutocompleteStyles === 'function') addAutocompleteStyles();
    if (typeof initializeTextareaAutocompleteFor === 'function') {
        initializeTextareaAutocompleteFor('createWorkflow', 'selectDomain');
    }
});

/**
 * Build BDD Gherkin feature file content from form fields.
 * Order: feature tag (above) -> Feature: feature name -> scenario tag -> Scenario: scenario name -> steps.
 */
function buildBddFeatureContent(featureTag, featureName, scenarioTag, scenarioName, stepsText, draggedStepsSet) {
    const lines = [];
    const fmtTag = (t) => (t || '').trim().replace(/^@/, '');
    const featureTags = (featureTag || '')
        .split(/[\s,]+/)
        .map(s => fmtTag(s))
        .filter(Boolean)
        .map(t => (t.startsWith('@') ? t : '@' + t));
    featureTags.forEach(t => lines.push(t));
    if (lines.length) lines.push('');
    const fName = (featureName || 'Feature').trim();
    lines.push('Feature: ' + fName);
    lines.push('');
    const scenarioTags = (scenarioTag || '')
        .split(/[\s,]+/)
        .map(s => fmtTag(s))
        .filter(Boolean)
        .map(t => (t.startsWith('@') ? t : '@' + t));
    scenarioTags.forEach(t => lines.push(t));
    const sName = (scenarioName || 'Scenario').trim();
    lines.push('Scenario: ' + sName);
    const steps = (stepsText || '')
        .split(/\r?\n/)
        .map(s => s.trim())
        .filter(Boolean);
    steps.forEach(s => {
        if (s.startsWith('#step-macro: ')) {
            lines.push('  # step-macro: ' + s.slice(14).trim());
        } else {
            lines.push(draggedStepsSet && draggedStepsSet.has(s) ? '  # step-macro: ' + s : '  ' + s);
        }
    });
    return lines.join('\n');
}

/**
 * Save button: download textarea content as .txt (Playwright) or .feature (BDD Cucumber),
 * filename = Workflow Name + extension. For BDD Cucumber, uses feature/scenario fields and template.
 */
function setupSaveWorkflowButton() {
    const btn = document.getElementById('saveWorkflowBtn');
    if (btn) btn.disabled = true;
    const workflowNameErrorEl = document.getElementById('workflowNameError');
    const featureNameErrorEl = document.getElementById('featureNameError');
    const featureTagErrorEl = document.getElementById('featureTagError');
    const scenarioNameErrorEl = document.getElementById('scenarioNameError');
    const scenarioTagErrorEl = document.getElementById('scenarioTagError');
    const createWorkflowErrorEl = document.getElementById('createWorkflowError');
    const featureNameInput = document.getElementById('featureName');
    const featureTagInput = document.getElementById('featureTag');
    const scenarioNameInput = document.getElementById('scenarioName');
    const scenarioTagInput = document.getElementById('scenarioTag');
    if (!btn || !workflowNameInput || !createWorkflowInput || !automationScriptTypeInput) return;
    if (workflowNameInput) {
        workflowNameInput.addEventListener('input', function () {
            if (workflowNameErrorEl) workflowNameErrorEl.textContent = '';
        });
    }
    [featureNameInput, featureTagInput, scenarioNameInput, scenarioTagInput].forEach(function (input, i) {
        if (!input) return;
        const errEl = [featureNameErrorEl, featureTagErrorEl, scenarioNameErrorEl, scenarioTagErrorEl][i];
        input.addEventListener('input', function () { if (errEl) errEl.textContent = ''; });
    });
    if (createWorkflowInput) {
        createWorkflowInput.addEventListener('input', function () {
            const el = document.getElementById('createWorkflowError');
            if (el) el.textContent = '';
        });
    }
    btn.addEventListener('click', function () {
        const createWorkflowErr = document.getElementById('createWorkflowError');
        if (workflowNameErrorEl) workflowNameErrorEl.textContent = '';
        if (featureNameErrorEl) featureNameErrorEl.textContent = '';
        if (featureTagErrorEl) featureTagErrorEl.textContent = '';
        if (scenarioNameErrorEl) scenarioNameErrorEl.textContent = '';
        if (scenarioTagErrorEl) scenarioTagErrorEl.textContent = '';
        if (createWorkflowErr) createWorkflowErr.textContent = '';
        const workflowName = (workflowNameInput.value || '').trim();
        const scriptType = (automationScriptTypeInput.value || '').trim();
        const stepsText = (createWorkflowInput.value || '').trim();
        let hasError = false;
        if (!workflowName) {
            if (workflowNameErrorEl) workflowNameErrorEl.textContent = 'Please enter a workflow name.';
            hasError = true;
        }
        if (!scriptType) {
            alert('Please select Automation Script Type (Playwright or BDD Cucumber) first.');
            return;
        }
        if (!stepsText) {
            if (createWorkflowErr) createWorkflowErr.textContent = 'Please enter a workflow.';
            hasError = true;
        }
        let content;
        if (scriptType === 'BDD Cucumber') {
            const featureName = (featureNameInput && featureNameInput.value) ? featureNameInput.value.trim() : '';
            const featureTag = (featureTagInput && featureTagInput.value) ? featureTagInput.value.trim() : '';
            const scenarioName = (scenarioNameInput && scenarioNameInput.value) ? scenarioNameInput.value.trim() : '';
            const scenarioTag = (scenarioTagInput && scenarioTagInput.value) ? scenarioTagInput.value.trim() : '';
            if (!featureName) { if (featureNameErrorEl) featureNameErrorEl.textContent = 'Please enter a feature name.'; hasError = true; }
            if (!featureTag) { if (featureTagErrorEl) featureTagErrorEl.textContent = 'Please enter a feature tag.'; hasError = true; }
            if (!scenarioName) { if (scenarioNameErrorEl) scenarioNameErrorEl.textContent = 'Please enter a scenario name.'; hasError = true; }
            if (!scenarioTag) { if (scenarioTagErrorEl) scenarioTagErrorEl.textContent = 'Please enter a scenario tag.'; hasError = true; }
            if (hasError) return;
            content = buildBddFeatureContent(featureTag, featureName, scenarioTag, scenarioName, stepsText, draggedStepLines);
        } else {
            if (hasError) return;
            content = buildBddFeatureContent('@Feature1', 'Feature 1', '@Scenario1', 'Scenario 1', stepsText, draggedStepLines);
        }
        const safeName = workflowName.replace(/[/\\:*?"<>|]/g, '_').trim() || 'workflow';
        const filename = safeName + '.feature';
        const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    });
}

/**
 * Show Steps Preview: open Script Design Preview modal first, then parent calls generateWorkflow and shows result in the modal.
 */
function setupShowStepsPreviewButton() {
    const btn = document.getElementById('showStepsPreviewBtn');
    if (!btn || !createWorkflowInput || !selectDomainInput) return;
    btn.addEventListener('click', async function () {
        const textareaContent = (createWorkflowInput.value || '').trim();
        const module = (selectDomainInput.value || '').trim();
        if (!textareaContent) {
            alert('Please add steps in the Create Workflow text area.');
            return;
        }
        if (!module) {
            alert('Please select a Project Domain.');
            return;
        }
        const saveBtn = document.getElementById('saveWorkflowBtn');
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.removeAttribute('disabled');
        }
        const requestPayload = { unformattedrequest: textareaContent, module: module };
        if (window.parent !== window && typeof window.parent.postMessage === 'function') {
            window.parent.postMessage({
                type: 'OPEN_SCRIPT_PREVIEW_MODAL_AND_FETCH',
                unformattedrequest: textareaContent,
                module: module
            }, '*');
        } else if (typeof openScriptPreviewModalAndFetch === 'function') {
            openScriptPreviewModalAndFetch(textareaContent, module);
        }
    });
}

/**
 * Listen for postMessage from parent when user clicks "Add Selected Steps" in the Step Details modal.
 * Moves the selected steps into the Create Workflow textarea (appends to existing content).
 */
function setupAddSelectedStepsListener() {
    window.addEventListener('message', function (event) {
        if (event.data && event.data.type === 'ADD_SELECTED_STEPS' && Array.isArray(event.data.steps)) {
            const steps = event.data.steps.filter(s => s != null && String(s).trim());
            if (!createWorkflowInput || steps.length === 0) return;
            const toAdd = steps.join('\n');
            const current = (createWorkflowInput.value || '').trim();
            createWorkflowInput.value = current ? current + '\n' + toAdd : toAdd;
        }
        if (event.data && event.data.type === 'SET_CREATE_WORKFLOW_CONTENT' && Array.isArray(event.data.steps)) {
            if (createWorkflowInput) {
                createWorkflowInput.value = event.data.steps.map(s => (s != null ? String(s).trim() : '')).filter(Boolean).join('\n');
            }
        }
    });
}

function setupConditionalFields() {
    function check() {
        const both = selectDomainInput?.value && automationScriptTypeInput?.value;
        if (conditionalFields) conditionalFields.style.display = both ? 'block' : 'none';
        if (!both && workflowNameInput) workflowNameInput.value = '';
        if (!both && createWorkflowInput) createWorkflowInput.value = '';
        const bddFields = document.getElementById('bddCucumberFields');
        const isBddCucumber = automationScriptTypeInput?.value === 'BDD Cucumber';
        if (bddFields) bddFields.style.display = both && isBddCucumber ? 'block' : 'none';
        if (isBddCucumber) {
            const saveBtn = document.getElementById('saveWorkflowBtn');
            if (saveBtn) { saveBtn.disabled = true; saveBtn.setAttribute('disabled', 'disabled'); }
        }
        if (!(both && isBddCucumber)) {
            ['featureName', 'featureTag', 'scenarioName', 'scenarioTag'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.value = '';
            });
        }
        if (both) {
            fetchCommonStepsForModule(selectDomainInput.value);
            if (typeof addAutocompleteStyles === 'function') addAutocompleteStyles();
            if (typeof initializeTextareaAutocompleteFor === 'function') {
                const ta = document.getElementById('createWorkflow');
                if (ta && ta.dataset.autocompleteInitialized !== 'true') {
                    initializeTextareaAutocompleteFor('createWorkflow', 'selectDomain');
                }
            }
        } else {
            renderCommonStepsList([]);
        }
    }
    selectDomainInput?.addEventListener('change', check);
    automationScriptTypeInput?.addEventListener('change', check);
    check();
}

/**
 * Fetch common steps for the selected module from GET /common-steps/query?module=<module>
 */
async function fetchCommonStepsForModule(module) {
    if (!commonStepsListEl || !module) return;
    commonStepsListEl.innerHTML = '<span class="common-steps-loading">Loading...</span>';
    try {
        const url = `${API_BASE_URL}/common-steps/query?module=${encodeURIComponent(module)}`;
        const response = await fetch(url);
        if (response.ok) {
            const rows = await response.json();
            renderCommonStepsList(Array.isArray(rows) ? rows : []);
        } else {
            renderCommonStepsList([]);
        }
    } catch (err) {
        console.error('Error fetching common steps:', err);
        renderCommonStepsList([]);
    }
}

function renderCommonStepsList(rows) {
    if (!commonStepsListEl) return;
    if (!rows || rows.length === 0) {
        commonStepsListEl.innerHTML = '<span class="common-steps-empty">No common steps for this module.</span>';
        return;
    }
    commonStepsListEl.innerHTML = rows.map(r => {
        const keyStep = (r && r.keyStep != null) ? String(r.keyStep) : '';
        if (!keyStep) return '';
        return `<span class="common-step-item" draggable="true" data-key-step="${escapeAttr(keyStep)}">${escapeHtml(keyStep)}</span>`;
    }).filter(Boolean).join('');
}

/**
 * Extract step strings from GET /common-steps/query response (array of rows with cmnSteps).
 * @param {*} data - JSON response: array of { keyStep, cmnSteps: string[] } or similar
 * @returns {string[]} - Flat array of step text strings
 */
function extractStepsFromQueryResponse(data) {
    if (!data) return [];
    const rows = Array.isArray(data) ? data : (data.steps != null ? data.steps : []);
    const steps = [];
    const excludeSuffix = /\s*-\s*Step execution Completed\s*$/i;
    rows.forEach(row => {
        const cmn = row && (row.cmnSteps != null ? row.cmnSteps : row.steps);
        if (Array.isArray(cmn)) {
            cmn.forEach(s => {
                if (s != null) {
                    let t = String(s).trim();
                    if (t) {
                        t = t.replace(excludeSuffix, '').trim();
                        if (t) steps.push(t);
                    }
                }
            });
        }
    });
    return steps;
}

/**
 * When user clicks a key line in the Common Steps list, call GET /common-steps/query
 * with module = selected Project Domain and keyStep = clicked key line.
 */
function setupKeyLineClickQuery() {
    if (!commonStepsListEl || !selectDomainInput) return;
    commonStepsListEl.addEventListener('click', async (e) => {
        const item = e.target.closest('.common-step-item');
        if (!item) return;
        const keyStep = (item.textContent || item.getAttribute('data-key-step') || '').trim();
        const module = selectDomainInput.value || '';
        if (!module || !keyStep) return;
        try {
            const url = `${API_BASE_URL}/common-steps/query?module=${encodeURIComponent(module)}&keyStep=${encodeURIComponent(keyStep)}`;
            console.log('[common-steps/query] REQUEST:', { method: 'GET', url, module, keyStep });
            const response = await fetch(url);
            let data = null;
            if (response.ok) {
                try {
                    data = await response.json();
                } catch (_) {
                    data = await response.text();
                }
            }
            console.log('[common-steps/query] RESPONSE:', { status: response.status, statusText: response.statusText, data });

            // Show steps in the same Step Details modal (parent window) in the pattern: numbered, green box, ✓, " - Step execution Completed"
            const steps = extractStepsFromQueryResponse(data);
            const title = 'Step Details:';
            if (window.parent !== window && typeof window.parent.postMessage === 'function') {
                window.parent.postMessage({ type: 'SHOW_COMMON_STEPS_MODAL', title, steps }, '*');
            } else if (typeof showCommonStepsModal === 'function') {
                showCommonStepsModal(title, steps);
            }
        } catch (err) {
            console.error('Error fetching common steps by key line:', err);
        }
    });
}

/**
 * "Please click here to re-use the successful workflow steps for selected module":
 * calls GET /Web-Automation/stepbyModule?module=<module> and populates the Create Workflow textarea.
 */
function setupReuseWorkflowStepsLink() {
    const link = document.getElementById('reuseWorkflowStepsLink');
    if (!link || !selectDomainInput || !createWorkflowInput) return;
    link.addEventListener('click', function (e) {
        e.preventDefault();
        const module = (selectDomainInput.value || '').trim();
        if (!module) {
            alert('Please select a Project Domain first.');
            return;
        }
        if (window.parent !== window && typeof window.parent.postMessage === 'function') {
            window.parent.postMessage({ type: 'OPEN_REUSE_STEPS_MODAL_AND_FETCH', module: module }, '*');
        } else {
            if (typeof openReuseStepsModalAndFetch === 'function') {
                openReuseStepsModalAndFetch(module);
            }
        }
    });
}

/**
 * Allow dragging a key line from the common steps list and dropping into the Create Workflow textarea.
 */
function setupDragAndDropKeyLineToWorkflow() {
    if (!commonStepsListEl || !createWorkflowInput) return;

    commonStepsListEl.addEventListener('dragstart', (e) => {
        const item = e.target.closest('.common-step-item');
        if (!item) return;
        e.dataTransfer.setData('text/plain', item.textContent || item.getAttribute('data-key-step') || '');
        e.dataTransfer.effectAllowed = 'copy';
    });

    createWorkflowInput.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
    });

    createWorkflowInput.addEventListener('drop', (e) => {
        e.preventDefault();
        const text = e.dataTransfer.getData('text/plain');
        if (text == null || text === '') return;
        const lines = text.split(/\r?\n/).map(function (line) {
            const t = line.trim();
            if (t) draggedStepLines.add(t);
            return t;
        }).filter(Boolean);
        const formatted = lines.map(function (l) { return '#step-macro: ' + l.replace(/\.\s*$/, '') + '.'; }).join('\n');
        const ta = createWorkflowInput;
        const current = (ta.value || '').trim();
        const insert = current ? current + '\n' + formatted : formatted;
        ta.value = insert;
        ta.selectionStart = ta.selectionEnd = insert.length;
        ta.focus();
    });
}

function escapeAttr(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML.replace(/"/g, '&quot;');
}
function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
}

/**
 * Fetch domains from API and populate the Project Domain dropdown
 */
async function populateProjectDomainDropdown() {
    try {
        const response = await fetch(`${API_BASE_URL}/common-steps/domains`);
        if (response.ok) {
            const domains = await response.json();
            if (selectDomainInput && Array.isArray(domains) && domains.length > 0) {
                selectDomainInput.innerHTML = '<option value="" selected disabled>-- Select Domain --</option>';
                domains.sort().forEach(domain => {
                    const option = document.createElement('option');
                    option.value = domain;
                    option.textContent = domain;
                    selectDomainInput.appendChild(option);
                });
            }
        }
    } catch (error) {
        console.error('Error fetching domains:', error);
    }
}

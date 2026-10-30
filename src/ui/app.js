/**
 * TestGenie UI - Frontend Application
 */

// Store uploaded JSON files
let uploadedJsonFiles = [];
/** True only after POST /Web-Automation/validatefeaturefile succeeds for the current file */
let featureFilePassedValidation = false;
/** Full plain-text validation error; available for hyperlink follow-up behavior */
let lastFeatureValidationErrorFullText = '';

// API Base URL
const API_BASE_URL = window.location.origin;

/** POST /Web-Automation/validatefeaturefile (OpenAPI: validate_feature_file_Web_Automation_validatefeaturefile_post) */
async function validateFeatureFileSelection(file) {
    const formData = new FormData();
    formData.append('feature_file', file);
    const response = await fetch(`${API_BASE_URL}/Web-Automation/validatefeaturefile`, {
        method: 'POST',
        body: formData
    });
    let data;
    try {
        data = await response.json();
    } catch (_) {
        throw new Error('Validation service returned an invalid response.');
    }
    return data;
}

/** Multi-line plain text for validation errors (beside Browse + click hook) */
function validationFailureFullPlainText(data, err) {
    if (err && err.message) return err.message;
    if (!data) return 'Validation failed.';
    const msg = (data.message || '').trim();
    const issues = data.issues || [];
    const issueLines = issues.map((issue) => {
        if (typeof issue === 'string') return issue;
        return Object.entries(issue).map(([k, v]) => `${k}: ${v}`).join(', ');
    });
    if (msg && issueLines.length) return [msg, ...issueLines].join('\n');
    if (msg) return msg;
    if (issueLines.length) return issueLines.join('\n');
    return 'Invalid feature file.';
}

function clearFeatureFileValidationErrorBesideBrowse() {
    if (featureFileValidationErrorEl) {
        featureFileValidationErrorEl.innerHTML = '';
        featureFileValidationErrorEl.hidden = true;
    }
    lastFeatureValidationErrorFullText = '';
    closeFeatureFileValidationErrorModal();
}

function renderFeatureFileValidationErrorBesideBrowse(fullPlainText) {
    if (!featureFileValidationErrorEl) return;
    lastFeatureValidationErrorFullText = fullPlainText;
    featureFileValidationErrorEl.innerHTML = '';
    const a = document.createElement('a');
    a.href = '#';
    a.className = 'feature-file-validation-error-link';
    a.textContent = 'Validation failed ...';
    a.title = 'View full validation message';
    a.addEventListener('click', onFeatureFileValidationErrorLinkClick);
    featureFileValidationErrorEl.appendChild(a);
    featureFileValidationErrorEl.hidden = false;
}

function openFeatureFileValidationErrorModal() {
    const modal = document.getElementById('featureFileValidationModal');
    const body = document.getElementById('featureFileValidationModalBody');
    if (!modal || !body) return;
    body.textContent = lastFeatureValidationErrorFullText || '';
    modal.classList.add('visible');
}

function closeFeatureFileValidationErrorModal() {
    const modal = document.getElementById('featureFileValidationModal');
    if (modal) modal.classList.remove('visible');
}

function onFeatureFileValidationErrorLinkClick(ev) {
    ev.preventDefault();
    openFeatureFileValidationErrorModal();
}

// Helper function to clear execution status containers when navigating away
function clearExecutionStatusContainers() {
    const inProgressContainer = document.getElementById('esInProgressContainer');
    const completedContainer = document.getElementById('esCompletedContainer');
    
    if (inProgressContainer) {
        inProgressContainer.innerHTML = '<div class="es-no-data">No data available</div>';
    }
    if (completedContainer) {
        completedContainer.innerHTML = '<div class="es-no-data">No data available</div>';
    }
}

// DOM Elements - declared globally but assigned after DOM loads
let featureFileInput, featureFileName, browseFeatureBtn, featureFileValidationErrorEl, includeApiStepsCheckbox;
let jsonUploadSection, jsonFileInput, jsonFileDisplay, uploadedFilesList;
let dryRunStrategyInput, selectDomainInput, bddFormatCheckbox;
let pushToGithubCheckbox, branchSection, branchNameInput;
let generateBtn, resultSection, resultTitle, resultContent;
let showPreviewCheckbox, scriptPreviewModal, scriptPreviewBody, scriptPreviewTableBody, scriptPreviewCloseBtn, scriptPreviewSubmitBtn, scriptPreviewReGenerateBtn;
let lastApiResponse = null;
/** When Script Design Preview is opened from Create Steps (Show Steps Preview), store request payload for Re-generate */
let _generateWorkflowPreviewPayload = null;

// Common Steps DOM and state
let commonStepsSection, csTabs, csPanels;
let csKeyLine, csSteps, csDomain, csHasApi, csDefaultJson, csApiRequests, csApiFiles;
let csPreviewBtn, csSaveBtn;
let csSearchDomain, csSearchKey, csSearchLimit, csSearchBtn, csResultsArea;
let csViewDeleteBtn, csViewRefreshBtn, csSelectedCount;
let csSelectedIds = new Set();
// Overlay elements
let csPreviewOverlay, csOverlayBody, csOverlayClose;
// Banner
let csBanner;
// Reset
let csResetBtn;

// Create Steps: section container only; full UI/JS in create-steps.html and create-steps.js (loaded via iframe)
let createStepsSection;

// Helper function to hide help section
function hideHelpSection() {
    const helpSection = document.getElementById('helpSection');
    if (helpSection) helpSection.style.display = 'none';
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    // Get DOM elements after page loads
    featureFileInput = document.getElementById('featureFile');
    featureFileName = document.getElementById('featureFileName');
    browseFeatureBtn = document.getElementById('browseFeatureBtn');
    featureFileValidationErrorEl = document.getElementById('featureFileValidationError');
    includeApiStepsCheckbox = document.getElementById('includeApiSteps');
    jsonUploadSection = document.getElementById('jsonUploadSection');
    jsonFileInput = document.getElementById('jsonFileInput');
    jsonFileDisplay = document.getElementById('jsonFileDisplay');
    uploadedFilesList = document.getElementById('uploadedFilesList');
    dryRunStrategyInput = document.getElementById('dryRunStrategy');
    selectDomainInput = document.getElementById('selectDomain');
    bddFormatCheckbox = document.getElementById('bddFormat');
    pushToGithubCheckbox = document.getElementById('pushToGithub');
    branchSection = document.getElementById('branchSection');
    branchNameInput = document.getElementById('branchName');
    generateBtn = document.getElementById('generateBtn');
    resultSection = document.getElementById('resultSection');
    resultTitle = document.getElementById('resultTitle');
    resultContent = document.getElementById('resultContent');
    showPreviewCheckbox = document.getElementById('showPreview');
    scriptPreviewModal = document.getElementById('scriptPreviewModal');
    scriptPreviewBody = document.getElementById('scriptPreviewBody');
    scriptPreviewTableBody = document.getElementById('scriptPreviewTableBody');
    scriptPreviewCloseBtn = document.getElementById('scriptPreviewCloseBtn');
    scriptPreviewSubmitBtn = document.getElementById('scriptPreviewSubmitBtn');
    scriptPreviewReGenerateBtn = document.getElementById('scriptPreviewReGenerateBtn');

    const featureFileValidationModal = document.getElementById('featureFileValidationModal');
    const featureFileValidationModalCloseBtn = document.getElementById('featureFileValidationModalCloseBtn');
    if (featureFileValidationModalCloseBtn && featureFileValidationModal) {
        featureFileValidationModalCloseBtn.addEventListener('click', () => {
            clearFeatureFileValidationErrorBesideBrowse();
        });
    }

    // Common Steps elements
    commonStepsSection = document.getElementById('commonStepsSection');
    csTabs = document.querySelectorAll('.cs-tab');
    csPanels = document.querySelectorAll('.cs-panel');
    csKeyLine = document.getElementById('csKeyLine');
    csSteps = document.getElementById('csSteps');
    csDomain = document.getElementById('csDomain');
    csHasApi = document.getElementById('csHasApi');
    csDefaultJson = document.getElementById('csDefaultJson');
    csApiRequests = document.getElementById('csApiRequests');
    csApiFiles = document.getElementById('csApiFiles');
    csPreviewBtn = document.getElementById('csPreviewBtn');
    csSaveBtn = document.getElementById('csSaveBtn');
    csSearchDomain = document.getElementById('csSearchDomain');
    csSearchKey = document.getElementById('csSearchKey');
    csSearchLimit = document.getElementById('csSearchLimit');
    csSearchBtn = document.getElementById('csSearchBtn');
    csResultsArea = document.getElementById('csResultsArea');
    csViewDeleteBtn = document.getElementById('csViewDeleteBtn');
    csViewRefreshBtn = document.getElementById('csViewRefreshBtn');
    csSelectedCount = document.getElementById('csSelectedCount');
    csPreviewOverlay = document.getElementById('csPreviewOverlay');
    csOverlayBody = document.getElementById('csOverlayBody');
    csOverlayClose = document.getElementById('csOverlayClose');
    csBanner = document.getElementById('csBanner');
    csResetBtn = document.getElementById('csResetBtn');

    // Create Steps: only the section container (content is create-steps.html in iframe)
    createStepsSection = document.getElementById('createStepsSection');

    // Populate dropdowns from API (Generate Scripts, Common Steps, etc. — not Create Steps; that page has its own)
    populateCommonStepsDomains();
    populateProjectDomainDropdown();

    // Debug: Log if elements are found
    console.log('DOM Elements loaded:', {
        browseFeatureBtn: !!browseFeatureBtn,
        featureFileInput: !!featureFileInput
    });

    // Browse button click - trigger file input
    if (browseFeatureBtn && featureFileInput) {
        browseFeatureBtn.addEventListener('click', (e) => {
            e.preventDefault();
            console.log('Browse button clicked');
            clearFeatureFileValidationErrorBesideBrowse();
            lastFeatureValidationErrorFullText = '';
            featureFileInput.click();
        });
    }

    // Feature file input change (after Browse): validate via Web-Automation validatefeaturefile
    if (featureFileInput) {
        featureFileInput.addEventListener('change', async (e) => {
            if (e.target.files.length === 0) {
                featureFileName.textContent = 'No file uploaded';
                lastFeatureValidationErrorFullText = '';
                clearFeatureFileValidationErrorBesideBrowse();
                featureFilePassedValidation = false;
                if (generateBtn) generateBtn.disabled = true;
                return;
            }
            const file = e.target.files[0];
            lastFeatureValidationErrorFullText = '';
            clearFeatureFileValidationErrorBesideBrowse();
            featureFilePassedValidation = false;
            if (generateBtn) generateBtn.disabled = true;
            featureFileName.textContent = file.name;
            console.log('Feature file selected:', file.name);

            if (browseFeatureBtn) browseFeatureBtn.disabled = true;
            featureFileName.textContent = `${file.name} (validating…)`;

            try {
                const data = await validateFeatureFileSelection(file);
                if (!data.is_valid) {
                    featureFileName.textContent = 'No file uploaded';
                    renderFeatureFileValidationErrorBesideBrowse(validationFailureFullPlainText(data));
                    e.target.value = '';
                    return;
                }
                clearFeatureFileValidationErrorBesideBrowse();
                lastFeatureValidationErrorFullText = '';
                hideResult();
                featureFileName.textContent = file.name;
                featureFilePassedValidation = true;
                if (generateBtn) generateBtn.disabled = false;
                console.log('Feature file validation OK:', data.message);
            } catch (err) {
                console.error('validatefeaturefile error:', err);
                featureFileName.textContent = 'No file uploaded';
                renderFeatureFileValidationErrorBesideBrowse(validationFailureFullPlainText(null, err));
                e.target.value = '';
            } finally {
                if (browseFeatureBtn) browseFeatureBtn.disabled = false;
            }
        });
    }

    // Include API Steps checkbox toggle
    if (includeApiStepsCheckbox) {
        includeApiStepsCheckbox.addEventListener('change', (e) => {
            if (e.target.checked) {
                jsonUploadSection.classList.add('visible');
            } else {
                jsonUploadSection.classList.remove('visible');
            }
            console.log('Include API Steps:', e.target.checked);
        });
    }

    // JSON file input change (+ button clicked)
    if (jsonFileInput) {
        jsonFileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                for (const file of e.target.files) {
                    // Avoid duplicates
                    if (!uploadedJsonFiles.some(f => f.name === file.name)) {
                        uploadedJsonFiles.push(file);
                        console.log('JSON file added:', file.name);
                    }
                }
                renderUploadedFiles();
                jsonFileDisplay.textContent = `${uploadedJsonFiles.length} file(s) added`;
            }
            // Reset input to allow re-selecting same file
            e.target.value = '';
        });
    }

    // Navigation tabs
    document.querySelectorAll('.nav-tab').forEach((tab, index) => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            console.log('Tab selected:', tab.textContent.replace(/\n/g, ' '));

            // Get section elements
            const generateScriptsSection = document.getElementById('generateScriptsSection');
            const executionLogsSection = document.getElementById('executionLogsSection');
            const analyticsSection = document.getElementById('analyticsSection');
            const commonStepsSection = document.getElementById('commonStepsSection');
            const executionStatusSection = document.getElementById('executionStatusSection');
            const createStepsSection = document.getElementById('createStepsSection');
            const helpSection = document.getElementById('helpSection');
            const autoHealSection = document.getElementById('autoHealSection');
            
            // Clear all sections first
            if (createStepsSection) createStepsSection.style.display = 'none';
            if (generateScriptsSection) generateScriptsSection.style.display = 'none';
            if (executionLogsSection) executionLogsSection.style.display = 'none';
            if (analyticsSection) analyticsSection.classList.remove('visible');
            if (commonStepsSection) commonStepsSection.style.display = 'none';
            if (executionStatusSection) executionStatusSection.style.display = 'none';
            if (autoHealSection) autoHealSection.style.display = 'none';
            if (helpSection) helpSection.style.display = 'none';
            
            // Clear sidebar highlights
            const sidebarItems = document.querySelectorAll('.sidebar-item');
            sidebarItems.forEach(item => item.classList.remove('active'));
            
            // Handle different tabs
            if (index === 0) {
                // Web-UI Automation tab - Default to Create Steps
                if (createStepsSection) createStepsSection.style.display = 'block';
                if (sidebarItems[0]) sidebarItems[0].classList.add('active');
            } else if (index === 1) {
                // Analytics tab
                if (analyticsSection) analyticsSection.classList.add('visible');
                loadAnalyticsDashboard();
            } else if (index === 2) {
                // Help tab
                if (helpSection) helpSection.style.display = 'block';
            }
        });
    });

    // Sidebar items
    document.querySelectorAll('.sidebar-item').forEach(item => {
        item.addEventListener('click', () => {
            if (item.classList.contains('disabled')) {
                return;
            }
            document.querySelectorAll('.sidebar-item').forEach(i => i.classList.remove('active'));
            item.classList.add('active');
            
            const sectionName = item.textContent.trim();
            const normalized = sectionName.replace(/\s+/g, ' ').toLowerCase();
            console.log('Sidebar selected:', sectionName);
            
            // Get elements inside the event handler to ensure they exist
            const createStepsSection = document.getElementById('createStepsSection');
            const generateScriptsSection = document.getElementById('generateScriptsSection');
            const executionLogsSection = document.getElementById('executionLogsSection');
            const analyticsSection = document.getElementById('analyticsSection');
            const commonStepsSection = document.getElementById('commonStepsSection');
            const executionStatusSection = document.getElementById('executionStatusSection');
            const autoHealSection = document.getElementById('autoHealSection');
            const helpSection = document.getElementById('helpSection');
            const navTabs = document.querySelectorAll('.nav-tab');
            
            console.log('Elements found:', {
                createSteps: !!createStepsSection,
                generateScripts: !!generateScriptsSection,
                executionLogs: !!executionLogsSection,
                analytics: !!analyticsSection,
                commonSteps: !!commonStepsSection,
                executionStatus: !!executionStatusSection,
                autoHeal: !!autoHealSection,
                help: !!helpSection
            });
            
            // Clear all sections first
            if (createStepsSection) createStepsSection.style.display = 'none';
            if (generateScriptsSection) generateScriptsSection.style.display = 'none';
            if (executionLogsSection) executionLogsSection.style.display = 'none';
            if (analyticsSection) analyticsSection.classList.remove('visible');
            if (commonStepsSection) commonStepsSection.style.display = 'none';
            if (executionStatusSection) executionStatusSection.style.display = 'none';
            if (autoHealSection) autoHealSection.style.display = 'none';
            if (helpSection) helpSection.style.display = 'none';
            
            // Sync nav tabs to "Web-UI Automation" (first tab) for sidebar selections
            navTabs.forEach(t => t.classList.remove('active'));
            if (navTabs[0]) navTabs[0].classList.add('active');
            
            // Show/hide sections based on selection
            if (normalized === 'create steps') {
                clearExecutionStatusContainers();
                if (createStepsSection) createStepsSection.style.display = 'block';
            } else if (normalized === 'generate scripts') {
                clearExecutionStatusContainers();
                if (generateScriptsSection) generateScriptsSection.style.display = 'block';
            } else if (normalized === 'execution logs') {
                clearExecutionStatusContainers();
                if (executionLogsSection) executionLogsSection.style.display = 'block';
            } else if (normalized === 'execution status') {
                if (executionStatusSection) executionStatusSection.style.display = 'block';
                loadExecutionStatusForToday();
            } else if (normalized.includes('common steps')) {
                clearExecutionStatusContainers();
                if (commonStepsSection) commonStepsSection.style.display = 'block';
                switchCSTab('add');
            } else if (normalized.includes('auto-heal')) {
                clearExecutionStatusContainers();
                if (autoHealSection) autoHealSection.style.display = 'block';
                initializeAutoHeal();
            } else {
                // Fallback: default to Generate Scripts to avoid empty view
                if (generateScriptsSection) generateScriptsSection.style.display = 'block';
            }
        });
    });

    // Common Steps sub-tabs
    csTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            csTabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            const target = tab.getAttribute('data-cs-tab');
            switchCSTab(target);
        });
    });

    // Common Steps actions
    if (csPreviewBtn) {
        csPreviewBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            await handleCSPreview();
        });
    }
    if (csSaveBtn) {
        csSaveBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            await handleCSSave();
        });
    }
    if (csSearchBtn) {
        csSearchBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            await handleCSSearch();
        });
    }
    if (csViewDeleteBtn) {
        csViewDeleteBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            await handleCSDelete();
        });
    }
    if (csViewRefreshBtn) {
        csViewRefreshBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            await handleCSSearch();
        });
    }
    if (csOverlayClose) {
        csOverlayClose.addEventListener('click', () => {
            hideCSOverlay();
        });
    }
    if (csResetBtn) {
        csResetBtn.addEventListener('click', () => {
            resetCSForm();
        });
    }

    // Push to GitHub checkbox toggle
    if (pushToGithubCheckbox) {
        pushToGithubCheckbox.addEventListener('change', (e) => {
            if (e.target.checked) {
                branchSection.classList.add('visible');
            } else {
                branchSection.classList.remove('visible');
            }
            console.log('Push to GitHub:', e.target.checked);
        });
    }
    // Script Preview Modal submit button
    if (scriptPreviewSubmitBtn) {
        scriptPreviewSubmitBtn.addEventListener('click', async () => {
            if (_generateWorkflowPreviewPayload && scriptPreviewTableBody) {
                const textareas = Array.from(scriptPreviewTableBody.querySelectorAll('.script-preview-fullstep-edit'));
                const lines = textareas
                    .sort((a, b) => (parseInt(a.getAttribute('data-index'), 10) || 0) - (parseInt(b.getAttribute('data-index'), 10) || 0))
                    .map(ta => (ta.value || '').trim())
                    .filter(Boolean);
                hideScriptPreviewModal();
                _generateWorkflowPreviewPayload = null;
                const createStepsFrame = document.querySelector('#createStepsSection iframe');
                if (createStepsFrame && createStepsFrame.contentWindow) {
                    createStepsFrame.contentWindow.postMessage({ type: 'SET_CREATE_WORKFLOW_CONTENT', steps: lines }, '*');
                }
            } else {
                hideScriptPreviewModal();
                if (_generateWorkflowPreviewPayload) {
                    _generateWorkflowPreviewPayload = null;
                } else {
                    await executeScriptGeneration();
                }
            }
        });
    }

    // Script Preview Modal Re-generate: collect Full Step column (all rows: edited, non-edited, new), call generateWorkflow
    if (scriptPreviewReGenerateBtn) {
        scriptPreviewReGenerateBtn.addEventListener('click', async () => {
            if (!_generateWorkflowPreviewPayload || !_generateWorkflowPreviewPayload.module) return;
            if (!scriptPreviewTableBody) return;
            const textareas = Array.from(scriptPreviewTableBody.querySelectorAll('.script-preview-fullstep-edit'));
            const lines = textareas
                .sort((a, b) => (parseInt(a.getAttribute('data-index'), 10) || 0) - (parseInt(b.getAttribute('data-index'), 10) || 0))
                .map(ta => (ta.value || '').trim());
            const unformattedrequest = lines.filter(Boolean).join(', ');
            const module = _generateWorkflowPreviewPayload.module.trim();
            if (!unformattedrequest.trim() || !module) return;
            const requestPayload = { unformattedrequest, module };
            showScriptPreviewModalLoading();
            try {
                const response = await fetch(`${API_BASE_URL}/Web-Automation/generateWorkflow`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(requestPayload)
                });
                const data = await response.json().catch(() => ({}));
                if (data.status === 'ERROR' || !Array.isArray(data.formattedWorkflow)) {
                    const errorSteps = [{ stepNo: 1, description: data.error || 'Failed to generate steps preview. Please try again.', action: '', confidence: '' }];
                    showScriptPreviewModalWithSteps(errorSteps, requestPayload);
                    return;
                }
                showScriptPreviewModalWithSteps(data.formattedWorkflow, requestPayload);
            } catch (err) {
                console.error('Re-generate steps preview error:', err);
                const errorSteps = [{ stepNo: 1, description: 'Failed to load steps preview. Please try again.', action: '', confidence: '' }];
                showScriptPreviewModalWithSteps(errorSteps, requestPayload);
            }
        });
    }
    // Script Preview Modal: + inserts row, - deletes row
    if (scriptPreviewTableBody) {
        scriptPreviewTableBody.addEventListener('click', function (e) {
            if (!e.target) return;
            var tr = e.target.closest('tr');
            if (!tr || !tr.cells || tr.cells.length < 5) return;
            if (e.target.classList.contains('script-preview-plus-btn')) {
                var currentStepNum = parseInt(tr.cells[0].textContent, 10) || 0;
                var nextNum = currentStepNum + 1;
                var newRowHtml = '<tr>' +
                    '<td style="padding: 8px; border: 1px solid #e5e7eb; font-weight: bold; width: 50px;">' + nextNum + '</td>' +
                    '<td style="padding: 4px; border: 1px solid #e5e7eb;"><textarea class="script-preview-fullstep-edit" data-index="0" rows="2" style="width: 100%; padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px; font-family: inherit; resize: vertical; box-sizing: border-box;"></textarea></td>' +
                    '<td style="padding: 8px; border: 1px solid #e5e7eb;"></td>' +
                    '<td style="padding: 8px; border: 1px solid #e5e7eb; text-align: center;"></td>' +
                    '<td class="script-preview-plus-minus" style="padding: 8px; border: 1px solid #e5e7eb;"><span class="script-preview-plus-btn" role="button" tabindex="0">+</span><span class="script-preview-minus-btn" role="button" tabindex="0">-</span></td>' +
                    '</tr>';
                tr.insertAdjacentHTML('afterend', newRowHtml);
            } else if (e.target.classList.contains('script-preview-minus-btn')) {
                if (e.target.classList.contains('script-preview-minus-disabled')) return;
                var dataRows = scriptPreviewTableBody.querySelectorAll('tr');
                var dataRowCount = 0;
                for (var r = 0; r < dataRows.length; r++) { if (dataRows[r].cells && dataRows[r].cells.length >= 5) dataRowCount++; }
                if (dataRowCount <= 1) return;
                tr.remove();
            } else {
                return;
            }
            var rows = scriptPreviewTableBody.querySelectorAll('tr');
            rows.forEach(function (row, i) {
                if (row.cells.length < 5) return;
                row.cells[0].textContent = i + 1;
                var ta = row.querySelector('.script-preview-fullstep-edit');
                if (ta) ta.setAttribute('data-index', String(i));
            });
            updateScriptPreviewMinusButtons();
        });
    }
    // Script Preview Modal close button
    if (scriptPreviewCloseBtn) {
        scriptPreviewCloseBtn.addEventListener('click', () => {
            hideScriptPreviewModal();
            if (showPreviewCheckbox) {
                showPreviewCheckbox.checked = false;
            }
        });
    }

    // Close modal when clicking outside
    if (scriptPreviewModal) {
        scriptPreviewModal.addEventListener('click', (e) => {
            if (e.target === scriptPreviewModal) {
                hideScriptPreviewModal();
                if (showPreviewCheckbox) {
                    showPreviewCheckbox.checked = false;
                }
            }
        });
    }

    // Generate button click
    if (generateBtn) {
        generateBtn.addEventListener('click', async () => {
            await handleGenerate();
        });
    }
    
    // Download automation button - use event delegation
    document.addEventListener('click', async (e) => {
        if (e.target && e.target.id === 'downloadAutomationBtn') {
            const automationFileName = document.getElementById('automationFileName');
            const filename = automationFileName ? automationFileName.value.trim() : '';
            if (!filename) {
                alert('Please enter a filename for the automation download.');
                return;
            }
            await handleDownloadAutomation(filename);
        }
        
        if (e.target && e.target.id === 'downloadRecordingsBtn') {
            const recordingsFileName = document.getElementById('recordingsFileName');
            const filename = recordingsFileName ? recordingsFileName.value.trim() : '';
            if (!filename) {
                alert('Please enter a filename for the recordings download.');
                return;
            }
            await handleDownloadRecordings(filename);
        }
        
        // Handle async Run ID links from formatAsyncResponse
        if (e.target && e.target.classList.contains('async-runid-link')) {
            e.preventDefault();
            const runid = e.target.getAttribute('data-runid');
            if (runid) {
                await showStepDetails(runid);
            }
        }
    });
});

// ---- Common Steps helpers ----
function switchCSTab(target) {
    if (!csPanels || csPanels.length === 0) return;
    csPanels.forEach(p => {
        if (p.getAttribute('data-cs-panel') === target) {
            p.classList.add('visible');
        } else {
            p.classList.remove('visible');
        }
    });
    if (commonStepsSection) {
        commonStepsSection.style.display = 'block';
    }
}

async function populateCommonStepsDomains() {
    const fill = (sel, items) => {
        if (!sel) return;
        const unique = Array.from(new Set(items.filter(Boolean)));
        sel.innerHTML = '<option value="">-- Select --</option>' + unique.map(v => `<option value="${v}">${v}</option>`).join('');
    };
    try {
        const resp = await fetch(`${API_BASE_URL}/common-steps/domains`);
        if (resp.ok) {
            const domains = await resp.json();
            fill(csDomain, domains);
            fill(csSearchDomain, domains);
            return;
        }
        console.warn('Domain API returned non-OK, falling back to static options');
    } catch (e) {
        console.warn('Domain API failed, falling back to static options', e);
    }
    // Fallback to static options present in Generate Scripts dropdown
    try {
        const options = Array.from(selectDomainInput ? selectDomainInput.options : [])
            .map(o => o.value)
            .filter(v => v);
        fill(csDomain, options);
        fill(csSearchDomain, options);
    } catch (e) {
        console.warn('Could not populate CS domain selects', e);
    }
}

function buildCSFormData() {
    const fd = new FormData();
    fd.append('keyStep', (csKeyLine?.value || '').trim());
    fd.append('cmnSteps', (csSteps?.value || '').trim());
    const moduleVal = csDomain?.value || '';
    if (moduleVal) fd.append('module', moduleVal);
    if (csHasApi?.checked) fd.append('hasAPIStep', '1');
    const defaultParam = (csDefaultJson?.value || '').trim();
    if (defaultParam) fd.append('defaultParam', defaultParam);
    const apiRequests = (csApiRequests?.value || '').trim();
    if (apiRequests) fd.append('apiRequests', apiRequests);
    if (csApiFiles && csApiFiles.files && csApiFiles.files.length > 0) {
        Array.from(csApiFiles.files).forEach(f => fd.append('apiFiles', f));
    }
    return fd;
}

async function handleCSPreview() {
    if (!csKeyLine || !csSteps || !csDomain) return;
    if (!csKeyLine.value.trim() || !csSteps.value.trim() || !csDomain.value) {
        showCSBanner('Please provide Key line, Common steps, and Domain.', true);
        return;
    }
    showCSOverlay('<div class="loading"></div> Generating preview...');
    const fd = buildCSFormData();
    try {
        const resp = await fetch(`${API_BASE_URL}/common-steps/ui/preview`, { method: 'POST', body: fd });
        const html = await resp.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const body = doc.body || doc.querySelector('body');
        // Remove scripts for safety
        body.querySelectorAll('script').forEach(s => s.remove());
        // Remove "Add another" links
        body.querySelectorAll('a[href="/common-steps/ui/preview"]').forEach(a => a.remove());
        showCSOverlay(body.innerHTML || html);
        attachOverlayInterceptors();
    } catch (e) {
        console.error('Preview error:', e);
        showCSOverlay('<div style="color:#cc0000;">Failed to generate preview.</div>');
    }
}

async function handleCSSave() {
    if (!csKeyLine || !csSteps || !csDomain) return;
    if (!csKeyLine.value.trim() || !csSteps.value.trim() || !csDomain.value) {
        alert('Please provide Key line, Common steps, and Domain.');
        return;
    }
    // Require preview first so we always have server-generated record_json
    // Prefer overlay hidden field if overlay is open
    const previewHidden = (csOverlayBody && csPreviewOverlay && csPreviewOverlay.classList.contains('visible') && csOverlayBody.querySelector('textarea[name="record_json"], input[name="record_json"]'));
    if (!previewHidden || !previewHidden.value) {
        alert('Please click Preview first, then Save to database.');
        return;
    }

    csSaveBtn.disabled = true;
    csSaveBtn.textContent = 'Saving...';

    try {
        const fd2 = new FormData();
        fd2.append('record_json', previewHidden.value);
        const resp = await fetch(`${API_BASE_URL}/common-steps/ui/save`, { method: 'POST', body: fd2 });
        const html = await resp.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const body = doc.body || doc.querySelector('body');
        body.querySelectorAll('script').forEach(s => s.remove());
        if (resp.ok) {
            // Success: hide overlay, show banner
            hideCSOverlay();
            showCSBanner('Saved to database.', false);
        } else {
            // Failure: keep overlay open, render server details, show error banner
            // Remove "Add another" links
            body.querySelectorAll('a[href="/common-steps/ui/preview"]').forEach(a => a.remove());
            showCSOverlay(body.innerHTML || html);
            attachOverlayInterceptors();
            showCSBanner('Save failed. See details in preview.', true);
        }
    } catch (e) {
        console.error('Save error:', e);
        alert('Save failed due to a network error.');
    } finally {
        csSaveBtn.disabled = false;
        csSaveBtn.textContent = 'Save';
    }
}

function attachOverlayInterceptors() {
    if (!csOverlayBody) return;
    // Intercept anchor clicks within preview so we don't navigate away
    csOverlayBody.querySelectorAll('a').forEach(a => {
        a.addEventListener('click', (e) => {
            const href = a.getAttribute('href') || '';
            if (!href) return;
            // Handle "Add another row" or preview UI links by staying in SPA
            if (href.startsWith('/common-steps/ui/preview')) {
                e.preventDefault();
                hideCSOverlay();
            } else if (href.startsWith('/common-steps/ui/delete') || href.startsWith('/common-steps/ui/query')) {
                e.preventDefault();
                // Route to View/Search tab
                switchCSTab('view');
                // Optionally trigger a fresh search if a domain is present
                handleCSSearch();
            } else if (href.startsWith('/common-steps')) {
                // Prevent leaving TestGenie for any other common-steps UI links
                e.preventDefault();
            }
        });
    });
    // Intercept form submissions inside injected HTML and post via fetch
    csOverlayBody.querySelectorAll('form').forEach(f => {
        f.addEventListener('submit', async (e) => {
            e.preventDefault();
            try {
                const hidden = f.querySelector('textarea[name="record_json"], input[name="record_json"]');
                if (!hidden || !hidden.value) {
                    // Fallback to outer Save routine
                    await handleCSSave();
                    return;
                }
                const fd = new FormData();
                fd.append('record_json', hidden.value);
                const resp = await fetch(`${API_BASE_URL}/common-steps/ui/save`, { method: 'POST', body: fd });
                const html = await resp.text();
                const doc = new DOMParser().parseFromString(html, 'text/html');
                const body = doc.body || doc.querySelector('body');
                body.querySelectorAll('script').forEach(s => s.remove());
                if (resp.ok) {
                    hideCSOverlay();
                    showCSBanner('Saved to database.', false);
                } else {
                    // Stay in overlay and show server-provided error details
                    body.querySelectorAll('a[href="/common-steps/ui/preview"]').forEach(a => a.remove());
                    showCSOverlay(body.innerHTML || html);
                    attachOverlayInterceptors();
                    showCSBanner('Save failed. See details in preview.', true);
                }
            } catch (err) {
                console.error('Embedded form save error:', err);
                alert('Save failed due to a network error.');
            }
        });
    });
}

function showCSOverlay(html) {
    if (!csPreviewOverlay || !csOverlayBody) return;
    csOverlayBody.innerHTML = html || '';
    csPreviewOverlay.classList.add('visible');
    csPreviewOverlay.style.display = 'flex';
}

function hideCSOverlay() {
    if (!csPreviewOverlay || !csOverlayBody) return;
    csOverlayBody.innerHTML = '';
    csPreviewOverlay.classList.remove('visible');
    csPreviewOverlay.style.display = 'none';
}

function showCSBanner(text, isError) {
    if (!csBanner) return;
    csBanner.textContent = text || '';
    csBanner.classList.remove('success', 'error');
    csBanner.classList.add(isError ? 'error' : 'success');
    csBanner.style.display = 'block';
    setTimeout(() => {
        if (csBanner) csBanner.style.display = 'none';
    }, 3500);
}

function resetCSForm() {
    // Optional confirm to prevent accidental wipe
    if (!confirm('Clear the Common Steps form?')) return;
    if (csKeyLine) csKeyLine.value = '';
    if (csSteps) csSteps.value = '';
    if (csDomain) csDomain.value = '';
    if (csHasApi) csHasApi.checked = false;
    if (csDefaultJson) csDefaultJson.value = '';
    if (csApiRequests) csApiRequests.value = '';
    if (csApiFiles) csApiFiles.value = '';
    // Hide banner and overlay, if any
    if (csBanner) csBanner.style.display = 'none';
    hideCSOverlay();
    // Return to Add tab to keep UX consistent
    switchCSTab('add');
    // Scroll to top of form
    window.scrollTo({ top: 0, behavior: 'smooth' });
}
async function handleCSSearch() {
    const moduleVal = csSearchDomain?.value || '';
    const keyVal = (csSearchKey?.value || '').trim();
    const limitVal = parseInt(csSearchLimit?.value || '50', 10) || 50;
    if (!moduleVal) {
        csResultsArea.innerHTML = '<div style="color:#cc0000;">Please select a Domain to search.</div>';
        return;
    }
    // Clear any previous selections when running a new search to avoid stale deletes
    csSelectedIds.clear();
    if (typeof updateCSSelectedCount === 'function') {
        updateCSSelectedCount();
    }
    csResultsArea.innerHTML = '<div class="loading"></div> Loading results...';
    const params = new URLSearchParams();
    params.set('module', moduleVal);
    if (keyVal) params.set('keyStep', keyVal);
    params.set('limit', String(limitVal));
    try {
        const resp = await fetch(`${API_BASE_URL}/common-steps/ui/delete?${params.toString()}`, { method: 'GET' });
        const html = await resp.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        // Prefer the table element
        const table = doc.querySelector('table');
        if (table) {
            // Remove any form and use only table markup
            csResultsArea.innerHTML = '';
            // Add styling hook
            table.classList.add('cs-results-table');
            csResultsArea.appendChild(table);
        } else {
            // Detect "no results" from delete UI page and show a friendly message instead of rendering that page
            const emptyMsg = doc.querySelector('.empty');
            if (emptyMsg) {
                csResultsArea.innerHTML = '<div style="color:#444;">Sorry! There are no step macros available for the selected domain. Please add the step macros for the selected domain first.</div>';
            } else {
                const body = doc.body || doc.querySelector('body');
                body.querySelectorAll('script').forEach(s => s.remove());
                csResultsArea.innerHTML = body.innerHTML || html;
            }
        }
        // Wire selection events and select-all
        updateCSSelectionsFromTable(csResultsArea);
        addSelectAllControl(csResultsArea);
        addRowDeleteActions(csResultsArea);
    } catch (e) {
        console.error('Search error:', e);
        csResultsArea.innerHTML = '<div style="color:#cc0000;">Failed to load results.</div>';
    }
}

function updateCSSelectionsFromTable(container) {
    try {
        const checkboxes = container.querySelectorAll('input[type="checkbox"][name="ids"]');
        checkboxes.forEach(cb => {
            const id = parseInt(cb.value, 10);
            if (isNaN(id)) return;
            // Initialize checked state from set
            cb.checked = csSelectedIds.has(id);
            cb.addEventListener('change', () => {
                if (cb.checked) {
                    csSelectedIds.add(id);
                } else {
                    csSelectedIds.delete(id);
                }
                updateCSSelectedCount();
            });
        });
        updateCSSelectedCount();
    } catch (e) {
        console.warn('Failed to wire selection events', e);
    }
}

function addRowDeleteActions(container) {
    try {
        const table = container.querySelector('table');
        if (!table) return;
        if (!table.tHead || !table.tBodies || table.tBodies.length === 0) return;

        // Add header cell if not already added
        if (!table.dataset.csActionsAdded) {
            const headerRow = table.tHead.querySelector('tr');
            if (headerRow) {
                const th = document.createElement('th');
                th.textContent = 'Actions';
                th.style.textAlign = 'right';
                headerRow.appendChild(th);
            }
            table.dataset.csActionsAdded = '1';
        }

        // Add action cell to each row
        const rows = table.tBodies[0].querySelectorAll('tr');
        rows.forEach(tr => {
            // Avoid double-add
            if (tr.querySelector('td.cs-actions-cell')) return;

            // Find the row id from the checkbox
            const cb = tr.querySelector('input[type="checkbox"][name="ids"]');
            const id = cb ? parseInt(cb.value, 10) : NaN;
            const td = document.createElement('td');
            td.className = 'cs-actions-cell';
            td.style.textAlign = 'right';
            const btn = document.createElement('button');
            btn.className = 'cs-btn';
            btn.style.background = '#dc3545';
            btn.style.color = '#fff';
            btn.style.padding = '6px 10px';
            btn.style.fontSize = '12px';
            btn.title = 'Delete row';
            btn.textContent = '🗑 Delete';
            btn.disabled = isNaN(id);
            btn.addEventListener('click', async (e) => {
                e.preventDefault();
                if (isNaN(id)) return;
                await handleCSSingleDelete(id, btn);
            });
            td.appendChild(btn);
            tr.appendChild(td);
        });
    } catch (e) {
        console.warn('Failed to add row actions', e);
    }
}

async function handleCSSingleDelete(id, btn) {
    if (!confirm('Delete this row?')) return;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Deleting...';
    }
    try {
        const resp = await fetch(`${API_BASE_URL}/common-steps`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: [id] })
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
            // Remove from selected set if present
            csSelectedIds.delete(id);
            updateCSSelectedCount();
            await handleCSSearch();
        } else {
            alert('Delete failed.');
            if (btn) { btn.disabled = false; btn.textContent = '🗑 Delete'; }
        }
    } catch (e) {
        console.error('Single delete error:', e);
        alert('Delete failed due to a network error.');
        if (btn) { btn.disabled = false; btn.textContent = '🗑 Delete'; }
    }
}

function updateCSSelectedCount() {
    const count = csSelectedIds.size;
    if (csSelectedCount) csSelectedCount.textContent = `${count} selected`;
    if (csViewDeleteBtn) csViewDeleteBtn.disabled = count === 0;
}

function addSelectAllControl(container) {
    try {
        const table = container.querySelector('table');
        if (!table) return;
        const theadRow = table.querySelector('thead tr');
        if (!theadRow) return;
        const firstHeaderCell = theadRow.querySelector('th');
        if (!firstHeaderCell) return;
        if (firstHeaderCell.querySelector('input[type="checkbox"]')) return;
        const selectAll = document.createElement('input');
        selectAll.type = 'checkbox';
        selectAll.title = 'Select all';
        firstHeaderCell.appendChild(selectAll);
        selectAll.addEventListener('change', () => {
            const rowCbs = container.querySelectorAll('input[type="checkbox"][name="ids"]');
            rowCbs.forEach(cb => {
                const id = parseInt(cb.value, 10);
                if (isNaN(id)) return;
                cb.checked = selectAll.checked;
                if (selectAll.checked) csSelectedIds.add(id); else csSelectedIds.delete(id);
            });
            updateCSSelectedCount();
        });
    } catch (e) {
        console.warn('Failed to add select-all', e);
    }
}

async function handleCSDelete() {
    if (csSelectedIds.size === 0) {
        alert('Select at least one row to delete.');
        return;
    }
    if (!confirm(`Delete ${csSelectedIds.size} row(s)?`)) return;
    if (csViewDeleteBtn) {
        csViewDeleteBtn.disabled = true;
        csViewDeleteBtn.textContent = 'Deleting...';
    }
    try {
        const resp = await fetch(`${API_BASE_URL}/common-steps`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: Array.from(csSelectedIds) })
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
            alert(`Deleted ${data.deleted ?? csSelectedIds.size} record(s).`);
            // Refresh results and clear selection
            csSelectedIds.clear();
            updateCSSelectedCount();
            await handleCSSearch();
        } else {
            alert('Delete failed.');
        }
    } catch (e) {
        console.error('Delete error:', e);
        alert('Delete failed due to a network error.');
    } finally {
        if (csViewDeleteBtn) {
            csViewDeleteBtn.disabled = false;
            csViewDeleteBtn.textContent = 'Delete selected';
        }
    }
}

// Handle Generate button click
async function handleGenerate() {
    // Validate required fields
    if (!featureFileInput || !featureFileInput.files || featureFileInput.files.length === 0) {
        showResult('error', 'Validation Error', 'Please upload a feature file.');
        return;
    }

    const pushToGithub = pushToGithubCheckbox.checked;
    const domain = selectDomainInput.value;

    // Domain is now ALWAYS required (regardless of push_to_github)
    if (!domain || domain === '' || domain === 'SELECT_DOMAIN') {
        showResult('error', 'Validation Error', 'Please select a Project Domain. This field is mandatory.');
        return;
    }

    // If preview is checked, get preview steps only
    if (showPreviewCheckbox && showPreviewCheckbox.checked) {
        await getScriptPreview();
        return;
    }

    // Otherwise, execute script generation directly
    await executeScriptGeneration();
}

// Get script preview (preview_mode=true) - returns preview steps only
async function getScriptPreview() {
    // Validate required fields
    if (!featureFileInput || !featureFileInput.files || featureFileInput.files.length === 0) {
        showResult('error', 'Validation Error', 'Please upload a feature file.');
        return;
    }

    const pushToGithub = pushToGithubCheckbox.checked;
    const domain = selectDomainInput.value;

    // Domain is now ALWAYS required
    if (!domain || domain === '' || domain === 'SELECT_DOMAIN') {
        showResult('error', 'Validation Error', 'Please select a Project Domain. This field is mandatory.');
        return;
    }

    // Build FormData
    const formData = new FormData();
    
    // Feature file (required)
    formData.append('feature_file', featureFileInput.files[0]);
    
    // JSON files (if Include API Steps is checked)
    if (includeApiStepsCheckbox.checked && uploadedJsonFiles.length > 0) {
        uploadedJsonFiles.forEach(file => {
            formData.append('json_files', file);
        });
    }
    
    // Form fields
    formData.append('push_to_github', pushToGithub.toString());
    
    if (domain) {
        formData.append('domain_selection', domain);
    }
    
    // BDD format checkbox maps to generate_step_definitions
    formData.append('generate_step_definitions', bddFormatCheckbox.checked.toString());
    
    // Dry run strategy
    formData.append('dry_run_strategy', dryRunStrategyInput.value);
    
    // Branch name (optional)
    const branchName = branchNameInput.value.trim();
    if (branchName) {
        formData.append('branch_name', branchName);
    }

    // **KEY: Set preview_mode=true to get preview steps only**
    formData.append('preview_mode', 'true');

    // Show loading state
    setLoading(true);
    hideResult();

    try {
        console.log('Calling API with preview_mode=true...');
        
        // Use async endpoint for preview
        const endpoint = `${API_BASE_URL}/Web-Automation/upload-feature-file-async`;

        const response = await fetch(endpoint, {
            method: 'POST',
            body: formData
        });

        const data = await response.json();
        
        // Store preview response
        lastApiResponse = data;
        console.log('Preview Response:', data);

        if (response.ok) {
            // Show preview modal with test steps
            showScriptPreviewModal();
        } else {
            const formattedError = formatErrorResponse(data);
            showResult('error', 'Preview Generation Failure', formattedError);
        }
    } catch (error) {
        console.error('Preview API Error:', error);
        const networkError = `<ul><li>Detailed failure log: ${escapeHtml(error.message || 'Failed to connect to the server.')}</li></ul>`;
        showResult('error', 'Preview Generation Failure', networkError);
    } finally {
        setLoading(false);
    }
}

// Execute script generation (called from Generate button or Submit button in modal)
async function executeScriptGeneration() {
    // Validate required fields
    if (!featureFileInput || !featureFileInput.files || featureFileInput.files.length === 0) {
        showResult('error', 'Validation Error', 'Please upload a feature file.');
        return;
    }

    const pushToGithub = pushToGithubCheckbox.checked;
    const domain = selectDomainInput.value;

    // Domain is now ALWAYS required (regardless of push_to_github)
    if (!domain || domain === '' || domain === 'SELECT_DOMAIN') {
        showResult('error', 'Validation Error', 'Please select a Project Domain. This field is mandatory.');
        return;
    }

    // Build FormData
    const formData = new FormData();
    
    // Feature file (required)
    formData.append('feature_file', featureFileInput.files[0]);
    
    // JSON files (if Include API Steps is checked)
    if (includeApiStepsCheckbox.checked && uploadedJsonFiles.length > 0) {
        uploadedJsonFiles.forEach(file => {
            formData.append('json_files', file);
        });
    }
    
    // Form fields
    formData.append('push_to_github', pushToGithub.toString());
    
    if (domain) {
        formData.append('domain_selection', domain);
    }
    
    // BDD format checkbox maps to generate_step_definitions
    formData.append('generate_step_definitions', bddFormatCheckbox.checked.toString());
    
    // Dry run strategy
    formData.append('dry_run_strategy', dryRunStrategyInput.value);
    
    // Branch name (optional)
    const branchName = branchNameInput.value.trim();
    if (branchName) {
        formData.append('branch_name', branchName);
    }

    // Show loading state
    setLoading(true);
    hideResult();

    try {
        console.log('Calling API with FormData...');
        console.log('Dry Run Strategy:', dryRunStrategyInput.value);
        console.log('BDD Format (generate_step_definitions):', bddFormatCheckbox.checked);
        console.log('Include API Steps:', includeApiStepsCheckbox.checked);
        console.log('JSON Files:', uploadedJsonFiles.map(f => f.name));
        console.log('Run In Background: true (always enabled)');

        // Always use async endpoint (run in background mode)
        const endpoint = `${API_BASE_URL}/Web-Automation/upload-feature-file-async`;

        const response = await fetch(endpoint, {
            method: 'POST',
            body: formData
        });

        const data = await response.json();

        // Handle async mode (202 status)
        if (response.status === 202) {
            const asyncMessage = formatAsyncResponse(data);
            showResult('success', 'Request Accepted - Processing in Background', asyncMessage);
        } else if (response.ok) {
            const formattedMessage = formatSuccessResponse(data);
            showResult('success', 'Script Generation Success', formattedMessage);
        } else {
            const formattedError = formatErrorResponse(data);
            showResult('error', 'Script Generation Failure', formattedError);
        }
    } catch (error) {
        console.error('API Error:', error);
        const networkError = `<ul><li>Detailed failure log: ${escapeHtml(error.message || 'Failed to connect to the server.')}</li></ul>`;
        showResult('error', 'Script Generation Failure', networkError);
    } finally {
        setLoading(false);
    }
}

// Format async response (202 status) into user-friendly message
function formatAsyncResponse(data) {
    const items = [];
    
    // Run ID - make it clickable using data attribute to avoid XSS
    if (data.runid) {
        items.push(`Run ID: <a href="javascript:void(0)" data-runid="${escapeHtml(data.runid)}" class="es-runid-link async-runid-link" style="color: #2d5a87; text-decoration: underline; cursor: pointer; font-weight: 600;">${escapeHtml(data.runid)}</a>`);
    }
    
    // Submission timestamps
    if (data.submittedAt) {
        items.push(`Submitted At (Eastern): <strong>${data.submittedAt}</strong>`);
    }
    if (data.submittedAtUtc) {
        items.push(`Submitted At (UTC): <strong>${data.submittedAtUtc}</strong>`);
    }
    
    // Message
    if (data.message) {
        items.push(data.message);
    }
    
    // Information about async processing
    items.push('Your request is being processed in the background. You will receive an email notification once processing is complete.');
    
    return `<ul>${items.map(item => `<li>${item}</li>`).join('')}</ul>`;
}

// Format success response into user-friendly message
function formatSuccessResponse(data) {
    const items = [];
    
    // Check if pushed to GitHub - only add PR link if available
    if (data.github_repo) {
        // Try to extract PR URL from message if available
        const messageMatch = data.message && data.message.match(/PR:\s*(https?:\/\/[^\s|]+)/);
        const prUrl = messageMatch ? messageMatch[1] : null;
        
        if (prUrl) {
            items.push(`Pull Request: <a href="${prUrl}" target="_blank">${prUrl}</a>`);
        }
    }
    
    // Download link for scripts - always show this message
    if (data.download_url) {
        // Use provided download URL
        items.push(`Please download the scripts using the filename <strong>${data.zip_filename || data.jira_id}</strong> within next 10 minutes: <a href="${data.download_url}" target="_blank">Download Scripts</a>`);
    } else if (data.jira_id) {
        // Construct download URL from jira_id (works for both local and GitHub push)
        const downloadUrl = `${API_BASE_URL}/Web-Automation/download/${data.jira_id}`;
        const filename = data.zip_filename || `${data.jira_id}_playwright_scripts.zip`;
        items.push(`Please download the scripts using the filename <strong>${filename}</strong> within next 10 minutes: <a href="${downloadUrl}" target="_blank">Download Scripts</a>`);
    }
    
    // Recording links
    if (data.recordings && data.recordings.length > 0) {
        data.recordings.forEach((rec, index) => {
            const recName = rec.zip_filename || rec.scenario_name || `Recording ${index + 1}`;
            const downloadLink = rec.zip_url || `${API_BASE_URL}/Web-Automation/recordings/download/${rec.zip_filename}`;
            items.push(`Please download the dry run recording from here: <a href="${downloadLink}" target="_blank">${recName}</a>`);
        });
    }
    
    // If no specific items, show the raw message
    if (items.length === 0 && data.message) {
        items.push(data.message);
    }
    
    return `<ul>${items.map(item => `<li>${item}</li>`).join('')}</ul>`;
}

// Format error response into user-friendly message
function formatErrorResponse(data) {
    const items = [];
    
    // Extract error details from the response structure
    const errorData = data.detail || data.error || data;
    
    // Failure Step
    let failureStep = 'N/A';
    if (errorData.failure_step) {
        failureStep = errorData.failure_step;
    } else if (data.failure_step) {
        failureStep = data.failure_step;
    }
    items.push(`Failure Steps: <strong>${escapeHtml(failureStep)}</strong>`);
    
    // Raw message / Detailed failure log
    let rawMessage = '';
    if (errorData.technical_details && errorData.technical_details.raw_message) {
        rawMessage = errorData.technical_details.raw_message;
    } else if (errorData.message) {
        rawMessage = errorData.message;
    } else if (typeof errorData === 'string') {
        rawMessage = errorData;
    } else if (data.message) {
        rawMessage = data.message;
    } else {
        rawMessage = JSON.stringify(data, null, 2);
    }
    items.push(`Detailed failure log: ${escapeHtml(rawMessage)}`);
    
    // Recording links (if available for failed scenarios) - last item
    // Check multiple locations where recording might be stored
    const recordings = errorData.recordings || data.recordings || [];
    
    // Also check for recording_filename in technical_details (error responses)
    let recordingFilename = null;
    if (errorData.technical_details && errorData.technical_details.recording_filename) {
        recordingFilename = errorData.technical_details.recording_filename;
    } else if (data.technical_details && data.technical_details.recording_filename) {
        recordingFilename = data.technical_details.recording_filename;
    } else if (errorData.context && errorData.context.recording_filename) {
        recordingFilename = errorData.context.recording_filename;
    } else if (errorData.context && errorData.context.zip_filename) {
        recordingFilename = errorData.context.zip_filename;
    }
    
    if (recordings.length > 0) {
        recordings.forEach((rec, index) => {
            const recName = rec.zip_filename || rec.scenario_name || `Recording ${index + 1}`;
            const downloadLink = rec.zip_url || `${API_BASE_URL}/Web-Automation/recordings/download/${rec.zip_filename}`;
            items.push(`Please download the dry run recording from here: <a href="${downloadLink}" target="_blank">${recName}</a>`);
        });
    } else if (recordingFilename) {
        // Handle comma-separated multiple recordings
        const filenames = recordingFilename.split(',').map(f => f.trim()).filter(f => f);
        filenames.forEach(filename => {
            const downloadLink = `${API_BASE_URL}/Web-Automation/recordings/download/${filename}`;
            items.push(`Please download the dry run recording from here: <a href="${downloadLink}" target="_blank">${filename}</a>`);
        });
    }
    
    return `<ul>${items.map(item => `<li>${item}</li>`).join('')}</ul>`;
}

// Escape HTML to prevent XSS
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Show result section
function showResult(type, title, content) {
    resultSection.classList.remove('success', 'error');
    resultSection.classList.add(type, 'visible');
    resultTitle.textContent = title;
    resultContent.innerHTML = content;
}

// Hide result section
function hideResult() {
    resultSection.classList.remove('visible', 'success', 'error');
}

// Set loading state
function setLoading(loading) {
    if (!generateBtn) return;
    if (loading) {
        generateBtn.disabled = true;
        generateBtn.innerHTML = '<span class="loading"></span>Generating...';
    } else {
        generateBtn.innerHTML = 'Generate';
        generateBtn.disabled = !featureFilePassedValidation;
    }
}

// Render uploaded files list
function renderUploadedFiles() {
    uploadedFilesList.innerHTML = '';
    uploadedJsonFiles.forEach((file, index) => {
        const item = document.createElement('div');
        item.className = 'uploaded-file-item';
        item.innerHTML = `
            <span class="file-name">${file.name}</span>
            <button class="remove-file-btn" data-index="${index}">Remove</button>
        `;
        uploadedFilesList.appendChild(item);
    });

    // Attach remove handlers
    document.querySelectorAll('.remove-file-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const index = parseInt(e.target.getAttribute('data-index'));
            const removed = uploadedJsonFiles.splice(index, 1);
            console.log('JSON file removed:', removed[0].name);
            renderUploadedFiles();
            jsonFileDisplay.textContent = uploadedJsonFiles.length > 0 
                ? `${uploadedJsonFiles.length} file(s) added` 
                : 'No file selected';
        });
    });
}

// Remove JSON file
function removeJsonFile(index) {
    uploadedJsonFiles.splice(index, 1);
    renderUploadedFiles();
    
    if (jsonFileDisplay) {
        if (uploadedJsonFiles.length === 0) {
            jsonFileDisplay.textContent = 'No files selected';
        } else {
            jsonFileDisplay.textContent = `${uploadedJsonFiles.length} file(s) added`;
        }
    }
}

// Helper functions for error display
function showError(elementId, message) {
    const errorElement = document.getElementById(elementId);
    if (errorElement) {
        errorElement.textContent = message;
        errorElement.style.display = 'block';
    }
}

function hideError(elementId) {
    const errorElement = document.getElementById(elementId);
    if (errorElement) {
        errorElement.style.display = 'none';
    }
}

// Handle automation download
async function handleDownloadAutomation(filename) {
    console.log('handleDownloadAutomation called with:', filename);
    try {
        // Hide any previous errors
        hideError('automationError');
        
        // Set button to loading state
        const btn = document.getElementById('downloadAutomationBtn');
        btn.disabled = true;
        btn.textContent = 'Downloading...';
        
        // Clean filename - remove .zip extension if present and extract JIRA ID
        let cleanFilename = filename.trim();
        if (cleanFilename.toLowerCase().endsWith('.zip')) {
            cleanFilename = cleanFilename.slice(0, -4);
        }
        
        // Extract JIRA ID from formats like "GC_playwright_scripts" -> "GC"
        let jiraId = cleanFilename;
        if (cleanFilename.includes('_playwright_scripts')) {
            jiraId = cleanFilename.replace('_playwright_scripts', '');
        }
        
        console.log('Making request to:', `${API_BASE_URL}/Web-Automation/download/${jiraId}`);
        
        // Make API call to download automation files using JIRA ID
        const response = await fetch(`${API_BASE_URL}/Web-Automation/download/${jiraId}`);
        
        console.log('Response status:', response.status);
        
        if (response.ok) {
            // Create download link
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            // Use original filename if it was valid, otherwise construct from JIRA ID
            const downloadName = filename.toLowerCase().endsWith('.zip') ? filename : `${jiraId}_playwright_scripts.zip`;
            a.download = downloadName;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
        } else {
            console.log('Showing inline error for status:', response.status);
            // Don't try to parse JSON error response, just show simple inline error
            showError('automationError', 'Please provide correct filename');
        }
    } catch (error) {
        console.error('Download error:', error);
        console.log('Showing inline error for exception');
        showError('automationError', 'Please provide correct filename');
    } finally {
        // Reset button state
        const btn = document.getElementById('downloadAutomationBtn');
        btn.disabled = false;
        btn.textContent = 'Download';
    }
}

// Handle recordings download
async function handleDownloadRecordings(filename) {
    try {
        // Hide any previous errors
        hideError('recordingsError');
        
        // Set button to loading state
        const btn = document.getElementById('downloadRecordingsBtn');
        btn.disabled = true;
        btn.textContent = 'Downloading...';
        
        // Make API call to download recording files
        const response = await fetch(`${API_BASE_URL}/Web-Automation/recordings/download/${filename}`);
        
        if (response.ok) {
            // Create download link
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
        } else {
            showError('recordingsError', 'Please provide correct filename');
        }
    } catch (error) {
        console.error('Download error:', error);
        showError('recordingsError', 'Please provide correct filename');
    } finally {
        // Reset button state
        const btn = document.getElementById('downloadRecordingsBtn');
        btn.disabled = false;
        btn.textContent = 'Download';
    }
}

// ============= ANALYTICS DASHBOARD =============

let analyticsCharts = {};

// Get today's date in YYYY-MM-DD format
const getTodayDate = () => new Date().toISOString().split('T')[0];

// Apply date filter with validation
async function applyDateFilter() {
    const startDate = document.getElementById('startDateFilter').value;
    const endDate = document.getElementById('endDateFilter').value;
    
    if (startDate && endDate && startDate > endDate) {
        alert('Start date cannot be after end date');
        return;
    }
    await loadAnalyticsDashboard(startDate, endDate);
}

// Reset to today's date
async function resetDateFilter() {
    const today = getTodayDate();
    document.getElementById('startDateFilter').value = today;
    document.getElementById('endDateFilter').value = today;
    await loadAnalyticsDashboard(today, today);
}

async function loadAnalyticsDashboard(startDate, endDate) {
    const loadingEl = document.getElementById('analyticsLoading');
    const contentEl = document.getElementById('analyticsContent');
    
    try {
        // Default to today if no dates provided
        const today = getTodayDate();
        startDate = startDate || today;
        endDate = endDate || today;
        
        // Set date inputs if not already set
        const startInput = document.getElementById('startDateFilter');
        const endInput = document.getElementById('endDateFilter');
        if (startInput && !startInput.value) startInput.value = today;
        if (endInput && !endInput.value) endInput.value = today;
        
        loadingEl.innerHTML = '<div class="loading"></div><p>Loading analytics data...</p>';
        loadingEl.style.display = 'block';
        contentEl.style.display = 'none';
        
        // Build URL with date parameters
        const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
        const response = await fetch(`${API_BASE_URL}/Web-Automation/analytics?${params}`);
        
        if (!response.ok) throw new Error(`API returned ${response.status}`);
        
        const data = await response.json();
        
        if (!data.success || !data.records?.length) {
            loadingEl.innerHTML = `<p style="color: #666;">No data for ${startDate} to ${endDate}. Try a different date range!</p>`;
            return;
        }
        
        // Show content and render charts
        loadingEl.style.display = 'none';
        contentEl.style.display = 'block';
        
        // Format date range for display
        const dateRange = startDate === endDate ? startDate : `${startDate} to ${endDate}`;
        
        renderStatsCards(data.records, dateRange);
        renderSuccessRateChart(data.records);
        renderCrawlerComparisonChart(data.records);
        renderTrendChart(data.records);
        renderModuleChart(data.records);
        renderExecutionTimeChart(data.records);
        renderGithubPushChart(data.records);
        renderDataTable(data.records);
        
    } catch (error) {
        console.error('Error loading analytics:', error);
        loadingEl.style.display = 'block';
        contentEl.style.display = 'none';
        loadingEl.innerHTML = '<p style="color: #dc3545;">❌ Failed to load analytics data. Please try again.</p>';
    }
}

function renderStatsCards(records, dateRange) {
    const container = document.getElementById('statsCards');
    
    const total = records.length;
    const successful = records.filter(r => r.scrpt_gen_success).length;
    const failed = total - successful;
    const successRate = total > 0 ? ((successful / total) * 100).toFixed(1) : 0;
    const avgTime = total > 0 ? (records.reduce((sum, r) => sum + (r.time_taken || 0), 0) / total).toFixed(1) : 0;
    const totalScenarios = records.reduce((sum, r) => sum + (r.scn_count || 0), 0);
    const githubPushed = records.filter(r => r.push_to_github).length;
    
    container.innerHTML = `
        <div class="stat-card" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);">
            <h3>Total Executions</h3>
            <div class="stat-value">${total}</div>
            <div class="stat-label">${dateRange}</div>
        </div>
        <div class="stat-card" style="background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);">
            <h3>Success Rate</h3>
            <div class="stat-value">${successRate}%</div>
            <div class="stat-label">${successful} succeeded, ${failed} failed</div>
        </div>
        <div class="stat-card" style="background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);">
            <h3>Avg Execution Time</h3>
            <div class="stat-value">${avgTime}s</div>
            <div class="stat-label">Per execution</div>
        </div>
        <div class="stat-card" style="background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%);">
            <h3>Total Scenarios</h3>
            <div class="stat-value">${totalScenarios}</div>
            <div class="stat-label">Processed</div>
        </div>
        <div class="stat-card" style="background: linear-gradient(135deg, #fa709a 0%, #fee140 100%);">
            <h3>GitHub Pushes</h3>
            <div class="stat-value">${githubPushed}</div>
            <div class="stat-label">Auto-pushed to repo</div>
        </div>
    `;
}

function renderSuccessRateChart(records) {
    const successful = records.filter(r => r.scrpt_gen_success).length;
    const failed = records.length - successful;
    
    destroyChart('successRateChart');
    
    const ctx = document.getElementById('successRateChart').getContext('2d');
    analyticsCharts.successRateChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['Successful', 'Failed'],
            datasets: [{
                data: [successful, failed],
                backgroundColor: ['#28a745', '#dc3545'],
                borderWidth: 2,
                borderColor: '#fff'
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom'
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            const total = successful + failed;
                            const percentage = ((context.parsed / total) * 100).toFixed(1);
                            return `${context.label}: ${context.parsed} (${percentage}%)`;
                        }
                    }
                }
            }
        }
    });
}

function renderCrawlerComparisonChart(records) {
    const mcpRecords = records.filter(r => r.crl_type === 'MCP');
    const jsRecords = records.filter(r => r.crl_type === 'Javascript');
    
    const mcpSuccess = mcpRecords.filter(r => r.scrpt_gen_success).length;
    const mcpFailed = mcpRecords.length - mcpSuccess;
    const jsSuccess = jsRecords.filter(r => r.scrpt_gen_success).length;
    const jsFailed = jsRecords.length - jsSuccess;
    
    destroyChart('crawlerComparisonChart');
    
    const ctx = document.getElementById('crawlerComparisonChart').getContext('2d');
    analyticsCharts.crawlerComparisonChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: ['MCP', 'Javascript'],
            datasets: [
                {
                    label: 'Successful',
                    data: [mcpSuccess, jsSuccess],
                    backgroundColor: '#28a745'
                },
                {
                    label: 'Failed',
                    data: [mcpFailed, jsFailed],
                    backgroundColor: '#dc3545'
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom'
                }
            },
            scales: {
                x: {
                    stacked: true
                },
                y: {
                    stacked: true,
                    beginAtZero: true
                }
            }
        }
    });
}

function renderTrendChart(records) {
    // Group by date
    const dateGroups = {};
    records.forEach(r => {
        const date = r.execution_dt || 'Unknown';
        if (!dateGroups[date]) {
            dateGroups[date] = { total: 0, successful: 0 };
        }
        dateGroups[date].total++;
        if (r.scrpt_gen_success) {
            dateGroups[date].successful++;
        }
    });
    
    // Sort dates and take last 30
    const sortedDates = Object.keys(dateGroups).sort().slice(-30);
    const totals = sortedDates.map(d => dateGroups[d].total);
    const successes = sortedDates.map(d => dateGroups[d].successful);
    
    destroyChart('trendChart');
    
    const ctx = document.getElementById('trendChart').getContext('2d');
    analyticsCharts.trendChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: sortedDates,
            datasets: [
                {
                    label: 'Total Executions',
                    data: totals,
                    borderColor: '#667eea',
                    backgroundColor: 'rgba(102, 126, 234, 0.1)',
                    fill: true,
                    tension: 0.4
                },
                {
                    label: 'Successful',
                    data: successes,
                    borderColor: '#28a745',
                    backgroundColor: 'rgba(40, 167, 69, 0.1)',
                    fill: true,
                    tension: 0.4
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom'
                }
            },
            scales: {
                y: {
                    beginAtZero: true
                }
            }
        }
    });
}

function renderModuleChart(records) {
    // Group by module
    const moduleGroups = {};
    records.forEach(r => {
        const module = r.module || 'Unknown';
        if (!moduleGroups[module]) {
            moduleGroups[module] = { total: 0, successful: 0 };
        }
        moduleGroups[module].total++;
        if (r.scrpt_gen_success) {
            moduleGroups[module].successful++;
        }
    });
    
    const modules = Object.keys(moduleGroups);
    const totals = modules.map(m => moduleGroups[m].total);
    const successes = modules.map(m => moduleGroups[m].successful);
    
    destroyChart('moduleChart');
    
    const ctx = document.getElementById('moduleChart').getContext('2d');
    analyticsCharts.moduleChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: modules,
            datasets: [
                {
                    label: 'Total',
                    data: totals,
                    backgroundColor: '#667eea'
                },
                {
                    label: 'Successful',
                    data: successes,
                    backgroundColor: '#28a745'
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom'
                }
            },
            scales: {
                y: {
                    beginAtZero: true
                }
            }
        }
    });
}

function renderExecutionTimeChart(records) {
    // Group by module and calculate average time
    const moduleGroups = {};
    records.forEach(r => {
        const module = r.module || 'Unknown';
        if (!moduleGroups[module]) {
            moduleGroups[module] = { totalTime: 0, count: 0 };
        }
        moduleGroups[module].totalTime += (r.time_taken || 0);
        moduleGroups[module].count++;
    });
    
    const modules = Object.keys(moduleGroups);
    const avgTimes = modules.map(m =>
        moduleGroups[m].count > 0 ? (moduleGroups[m].totalTime / moduleGroups[m].count).toFixed(1) : 0
    );
    
    destroyChart('executionTimeChart');
    
    const ctx = document.getElementById('executionTimeChart').getContext('2d');
    analyticsCharts.executionTimeChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: modules,
            datasets: [{
                label: 'Avg Time (seconds)',
                data: avgTimes,
                backgroundColor: '#f39c12'
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom'
                }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    title: {
                        display: true,
                        text: 'Seconds'
                    }
                }
            }
        }
    });
}

function renderGithubPushChart(records) {
    const pushed = records.filter(r => r.push_to_github).length;
    const notPushed = records.length - pushed;
    
    destroyChart('githubPushChart');
    
    const ctx = document.getElementById('githubPushChart').getContext('2d');
    analyticsCharts.githubPushChart = new Chart(ctx, {
        type: 'pie',
        data: {
            labels: ['Pushed to GitHub', 'Local Only'],
            datasets: [{
                data: [pushed, notPushed],
                backgroundColor: ['#6e5494', '#95a5a6'],
                borderWidth: 2,
                borderColor: '#fff'
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom'
                }
            }
        }
    });
}

function renderDataTable(records) {
    const tbody = document.getElementById('analyticsTableBody');
    
    // Show only last 50 records
    const recentRecords = records.slice(0, 50);
    
    tbody.innerHTML = recentRecords.map(r => `
        <tr>
            <td>${escapeHtml(String(r.id || 'N/A'))}</td>
            <td>${escapeHtml(r.feature_nm || 'N/A')}</td>
            <td>${escapeHtml(r.module || 'N/A')}</td>
            <td>${escapeHtml(String(r.scn_count || 0))}</td>
            <td>${escapeHtml(String(r.time_taken || 0))}</td>
            <td>${escapeHtml(r.execution_dt || 'N/A')}</td>
            <td>
                <span class="badge ${r.scrpt_gen_success ? 'badge-success' : 'badge-failed'}">
                    ${r.scrpt_gen_success ? '✓ Success' : '✗ Failed'}
                </span>
            </td>
            <td>
                <span class="badge ${r.crl_type === 'MCP' ? 'badge-mcp' : 'badge-js'}">
                    ${escapeHtml(r.crl_type || 'N/A')}
                </span>
            </td>
            <td>${r.push_to_github ? '✓' : '✗'}</td>
        </tr>
    `).join('');
}

function destroyChart(chartId) {
    if (analyticsCharts[chartId]) {
        analyticsCharts[chartId].destroy();
        delete analyticsCharts[chartId];
    }
}

// ============= SCRIPT PREVIEW MODAL =============

function showScriptPreviewModal() {
    _generateWorkflowPreviewPayload = null;
    if (scriptPreviewReGenerateBtn) scriptPreviewReGenerateBtn.style.display = 'none';
    var overlay = document.getElementById('scriptPreviewLoadingOverlay');
    if (overlay) overlay.style.display = 'none';
    var footer = document.getElementById('scriptPreviewModalFooter');
    if (footer) footer.style.display = '';
    if (!scriptPreviewModal) return;

    // From Generate Scripts: hide +/− column (view-only, no add/delete rows)
    var plusMinusHeader = document.getElementById('scriptPreviewPlusMinusHeader');
    if (plusMinusHeader) plusMinusHeader.style.display = 'none';

    // Display the preview steps from API response (4 columns, no +/−)
    if (scriptPreviewTableBody) {
        if (lastApiResponse && lastApiResponse.testSteps && Array.isArray(lastApiResponse.testSteps)) {
            const steps = lastApiResponse.testSteps;
            let html = '';
            steps.forEach((step, index) => {
                const confNum = step.confidence != null ? parseFloat(String(step.confidence).replace('%', '')) : NaN;
                const confidenceDisplay = step.confidence != null && !isNaN(confNum) ? confNum.toFixed(0) + '%' : 'N/A';
                const confidenceStyle = (!isNaN(confNum) && confNum < 80) ? ' color: red;' : '';
                html += `
                    <tr>
                        <td style="padding: 8px; border: 1px solid #e5e7eb; font-weight: bold; width: 50px;">${step.stepNo || index + 1}</td>
                        <td style="padding: 8px; border: 1px solid #e5e7eb;">${escapeHtml(step.fullStep || '')}</td>
                        <td style="padding: 8px; border: 1px solid #e5e7eb;">${escapeHtml(step.action || '')}</td>
                        <td style="padding: 8px; border: 1px solid #e5e7eb; text-align: center;${confidenceStyle}">${confidenceDisplay}</td>
                    </tr>
                `;
            });
            scriptPreviewTableBody.innerHTML = html;
        } else {
            scriptPreviewTableBody.innerHTML = `
                <tr>
                    <td colspan="4" style="padding: 12px; border: 1px solid #e5e7eb; background: #fafcff; color: #999;">
                        No preview steps available. Please try again.
                    </td>
                </tr>
            `;
        }
    }
    scriptPreviewModal.classList.add('visible');
    scriptPreviewModal.style.display = 'flex';
}

/**
 * Show Script Design Preview modal in loading state: headers, 4 empty rows, and buttons (all blurred); spinner clear.
 */
function showScriptPreviewModalLoading() {
    if (!scriptPreviewModal || !scriptPreviewTableBody) return;
    var plusMinusHeader = document.getElementById('scriptPreviewPlusMinusHeader');
    if (plusMinusHeader) plusMinusHeader.style.display = '';
    var overlay = document.getElementById('scriptPreviewLoadingOverlay');
    if (overlay) overlay.style.display = 'flex';
    var emptyRow = '<tr><td style="padding: 8px; border: 1px solid #e5e7eb;"></td><td style="padding: 8px; border: 1px solid #e5e7eb;"></td><td style="padding: 8px; border: 1px solid #e5e7eb;"></td><td style="padding: 8px; border: 1px solid #e5e7eb;"></td><td class="script-preview-plus-minus" style="padding: 8px; border: 1px solid #e5e7eb;"><span class="script-preview-plus-btn" role="button" tabindex="0">+</span><span class="script-preview-minus-btn" role="button" tabindex="0">-</span></td></tr>';
    scriptPreviewTableBody.innerHTML = emptyRow + emptyRow + emptyRow + emptyRow;
    scriptPreviewModal.classList.add('visible');
    scriptPreviewModal.style.display = 'flex';
}

/**
 * Enable/disable minus buttons in Script Preview table: disabled when only one data row exists.
 */
function updateScriptPreviewMinusButtons() {
    if (!scriptPreviewTableBody) return;
    var rows = scriptPreviewTableBody.querySelectorAll('tr');
    var dataRowCount = 0;
    for (var i = 0; i < rows.length; i++) { if (rows[i].cells && rows[i].cells.length >= 5) dataRowCount++; }
    var minusBtns = scriptPreviewTableBody.querySelectorAll('.script-preview-minus-btn');
    for (var j = 0; j < minusBtns.length; j++) {
        var btn = minusBtns[j];
        if (dataRowCount <= 1) {
            btn.classList.add('script-preview-minus-disabled');
            btn.setAttribute('aria-disabled', 'true');
            btn.setAttribute('tabindex', '-1');
        } else {
            btn.classList.remove('script-preview-minus-disabled');
            btn.removeAttribute('aria-disabled');
            btn.setAttribute('tabindex', '0');
        }
    }
}

/**
 * Populate Script Design Preview modal with generateWorkflow steps (4 columns) and show Re-generate.
 * @param {Array<{stepNo, description, action, confidence}>} steps - formattedWorkflow from generateWorkflow API
 * @param {{unformattedrequest: string, module: string}} requestPayload - stored for Re-generate
 */
function showScriptPreviewModalWithSteps(steps, requestPayload) {
    if (!scriptPreviewModal || !scriptPreviewTableBody) return;
    var overlay = document.getElementById('scriptPreviewLoadingOverlay');
    if (overlay) overlay.style.display = 'none';
    var footer = document.getElementById('scriptPreviewModalFooter');
    if (footer) footer.style.display = '';
    _generateWorkflowPreviewPayload = requestPayload || null;
    if (scriptPreviewReGenerateBtn) scriptPreviewReGenerateBtn.style.display = requestPayload ? 'inline-block' : 'none';
    // From Create Steps (Show Steps Preview): show +/− column
    var plusMinusHeader = document.getElementById('scriptPreviewPlusMinusHeader');
    if (plusMinusHeader) plusMinusHeader.style.display = '';

    let html = '';
    if (Array.isArray(steps) && steps.length) {
        steps.forEach((step, index) => {
            const fullStep = step.description != null ? step.description : (step.fullStep || '');
            const confNum = step.confidence != null ? parseFloat(String(step.confidence).replace('%', '')) : NaN;
            const confidenceDisplay = step.confidence != null && !isNaN(confNum) ? confNum.toFixed(0) + '%' : 'N/A';
            const confidenceStyle = (!isNaN(confNum) && confNum < 80) ? ' color: red;' : '';
            const fullStepEscaped = escapeHtml(fullStep);
            html += `
                <tr>
                    <td style="padding: 8px; border: 1px solid #e5e7eb; font-weight: bold; width: 50px;">${step.stepNo != null ? step.stepNo : index + 1}</td>
                    <td style="padding: 4px; border: 1px solid #e5e7eb;"><textarea class="script-preview-fullstep-edit" data-index="${index}" rows="2" style="width: 100%; padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px; font-family: inherit; resize: vertical; box-sizing: border-box;">${fullStepEscaped}</textarea></td>
                    <td style="padding: 8px; border: 1px solid #e5e7eb;">${escapeHtml(step.action || '')}</td>
                    <td style="padding: 8px; border: 1px solid #e5e7eb; text-align: center;${confidenceStyle}">${confidenceDisplay}</td>
                    <td class="script-preview-plus-minus" style="padding: 8px; border: 1px solid #e5e7eb;"><span class="script-preview-plus-btn" role="button" tabindex="0">+</span><span class="script-preview-minus-btn" role="button" tabindex="0">-</span></td>
                </tr>
            `;
        });
        scriptPreviewTableBody.innerHTML = html;
        updateScriptPreviewMinusButtons();
    } else {
        scriptPreviewTableBody.innerHTML = `
            <tr>
                <td colspan="5" style="padding: 12px; border: 1px solid #e5e7eb; background: #fafcff; color: #999;">
                    No steps to display.
                </td>
            </tr>
        `;
    }
    scriptPreviewModal.classList.add('visible');
    scriptPreviewModal.style.display = 'flex';
}

function hideScriptPreviewModal() {
    if (!scriptPreviewModal) return;
    _generateWorkflowPreviewPayload = null;
    if (scriptPreviewReGenerateBtn) scriptPreviewReGenerateBtn.style.display = 'none';
    var overlay = document.getElementById('scriptPreviewLoadingOverlay');
    if (overlay) overlay.style.display = 'none';
    var footer = document.getElementById('scriptPreviewModalFooter');
    if (footer) footer.style.display = '';
    scriptPreviewModal.classList.remove('visible');
    scriptPreviewModal.style.display = 'none';
}
/**
 * Fetch domains from onboardingConfiguration.json via API and populate the main Project Domain dropdown
 */
async function populateProjectDomainDropdown() {
    try {
        console.log('Fetching domains from API...');
        const response = await fetch(`${API_BASE_URL}/common-steps/domains`);
        
        if (response.ok) {
            const domains = await response.json();
            console.log('Domains fetched successfully:', domains);
            
            if (selectDomainInput && Array.isArray(domains) && domains.length > 0) {
                // Clear existing options except the default "-- Select Domain --" option
                selectDomainInput.innerHTML = '<option value="" selected disabled>-- Select Domain --</option>';
                
                // Sort domains alphabetically and add them as options
                const sortedDomains = domains.sort();
                sortedDomains.forEach(domain => {
                    const option = document.createElement('option');
                    option.value = domain;
                    option.textContent = domain;
                    selectDomainInput.appendChild(option);
                });
                
                console.log(`Successfully populated ${domains.length} domains in Project Domain dropdown`);
            } else {
                console.warn('No domains received from API or selectDomainInput not found');
            }
        } else {
            console.error('Failed to fetch domains from API:', response.status, response.statusText);
            // Keep existing hardcoded options as fallback
        }
    } catch (error) {
        console.error('Error fetching domains:', error);
        // Keep existing hardcoded options as fallback
    }
}

// ============= AUTO-HEAL FUNCTIONS (GLOBAL SCOPE) =============

let ahAllData = [];
let ahCurrentPage = 1;
const AH_ITEMS_PER_PAGE = 10;

// Initialize Auto-Heal when tab is clicked
function initializeAutoHeal() {
    const ahModuleSelect = document.getElementById('ahModuleSelect');
    const ahSearchBtn = document.getElementById('ahSearchBtn');
    
    // Always populate modules when tab is clicked
    populateAutoHealModules();
    
    if (ahSearchBtn) {
        ahSearchBtn.onclick = fetchAutoHealData;
    }
}

async function populateAutoHealModules() {
    const ahModuleSelect = document.getElementById('ahModuleSelect');
    if (!ahModuleSelect) return;
    
    try {
        const response = await fetch(`${API_BASE_URL}/common-steps/domains`);
        if (response.ok) {
            const modules = await response.json();
            console.log('Auto-Heal modules fetched:', modules);
            
            // Clear existing options except the default
            ahModuleSelect.innerHTML = '<option value="">-- Select Module --</option>';
            
            // Add modules as options
            if (Array.isArray(modules)) {
                modules.forEach(module => {
                    const option = document.createElement('option');
                    option.value = module;
                    option.textContent = module;
                    ahModuleSelect.appendChild(option);
                });
            }
        }
    } catch (error) {
        console.warn('Failed to populate auto-heal module dropdown:', error);
    }
}

async function fetchAutoHealData() {
    const moduleName = document.getElementById('ahModuleSelect').value;

    if (!moduleName) {
        alert('Please select a module');
        return;
    }

    showAutoHealLoading();

    try {
        const params = new URLSearchParams({
            modulename: moduleName,
            responsetype: 'all'
        });

        const response = await fetch(
            `${API_BASE_URL}/Web-Automation/getAutoHealData?${params.toString()}`
        );

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to fetch data');
        }

        const data = await response.json();
        ahAllData = data.autohealResponse || [];
        ahCurrentPage = 1;
        displayAutoHealResults();
    } catch (error) {
        console.error('Error fetching auto-heal data:', error);
        showAutoHealError(error.message || 'Failed to fetch auto-heal data');
    }
}

function displayAutoHealResults() {
     const container = document.getElementById('ahResultsContainer');

     if (ahAllData.length === 0) {
         container.innerHTML = `
             <div style="margin-bottom: 32px; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                 <div style="text-align: center; padding: 40px; color: #666; font-style: italic;">No results found</div>
             </div>
         `;
         return;
     }

     const startIdx = (ahCurrentPage - 1) * AH_ITEMS_PER_PAGE;
     const endIdx = startIdx + AH_ITEMS_PER_PAGE;
     const pageData = ahAllData.slice(startIdx, endIdx);

     let html = `
         <div style="margin-bottom: 32px; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
             <div style="font-size: 17px; font-weight: 600; color: #1a3a5c; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 2px solid #e0e0e0;">
                 Auto-Heal Results (${startIdx + 1} to ${Math.min(endIdx, ahAllData.length)} of ${ahAllData.length})
             </div>
             <table style="width: 100%; border-collapse: collapse; background: #fff;">
                 <thead>
                     <tr>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Feature Tag</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Feature Name</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Scenario Tag</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Scenario Name</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Feature Steps</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Existing Completed scripts</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Updated Scripts</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Existing Locator Map</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">New Locator Map</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Status</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px; background: #f0f0f0; font-weight: 600;">Auto-Heal Action</th>
                         <th style="border: 1px solid #ddd; padding: 10px 12px; text-align: center; font-size: 13px; background: #f0f0f0; font-weight: 600;">User Action</th>
                     </tr>
                 </thead>
                 <tbody>
      `;

      pageData.forEach((item, index) => {
          const actualIndex = startIdx + index;
          html += `
              <tr style="border-bottom: 1px solid #ddd;">
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px;">${escapeHtml(item.feature_tag || 'N/A')}</td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px;">${escapeHtml(item.feature_name || 'N/A')}</td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px;">${escapeHtml(item.scenario_tag || 'N/A')}</td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px;">${escapeHtml(item.scenario_name || 'N/A')}</td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: center; font-size: 13px;">
                      ${item.feature_steps ?
                          `<a href="javascript:void(0)" onclick="downloadAutoHealFeatureSteps(${actualIndex})" class="es-download-link">Download</a>` :
                          '<span style="color: #999;">N/A</span>'}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: center; font-size: 13px;">
                      ${item.complete_script ?
                          `<a href="javascript:void(0)" onclick="downloadAutoHealCompleteScript(${actualIndex})" class="es-download-link">Download</a>` :
                          '<span style="color: #999;">N/A</span>'}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: center; font-size: 13px;">
                      ${item.updated_complete_script ?
                          `<a href="javascript:void(0)" onclick="downloadAutoHealUpdatedScript(${actualIndex})" class="es-download-link">Download</a>` :
                          ''}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: center; font-size: 13px;">
                      ${item.old_step_loc_map ?
                          `<a href="javascript:void(0)" onclick="showAutoHealLocatorMap(${actualIndex}, 'old')" style="color: #2d5a87; text-decoration: underline; cursor: pointer;">CLICK HERE</a>` :
                          ''}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: center; font-size: 13px;">
                      ${item.updated_step_loc_map ?
                          `<a href="javascript:void(0)" onclick="showAutoHealLocatorMap(${actualIndex}, 'updated')" style="color: #2d5a87; text-decoration: underline; cursor: pointer;">CLICK HERE</a>` :
                          ''}
                  </td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: left; font-size: 13px;">${escapeHtml(item.status || 'N/A')}</td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: center; font-size: 13px;">
                      <button onclick="triggerAutoHeal(${actualIndex})" ${item.updated_complete_script ? 'disabled' : ''} style="padding: 6px 12px; font-size: 12px; background: ${item.updated_complete_script ? '#cccccc' : '#28a745'}; color: #fff; border: none; border-radius: 4px; cursor: ${item.updated_complete_script ? 'not-allowed' : 'pointer'}; font-weight: 600;">AUTO HEAL</button>
                  </td>
                  <td style="border: 1px solid #ddd; padding: 10px 12px; text-align: center; font-size: 13px;">
                      ${item.updated_complete_script ?
                          `<div style="display: flex; flex-direction: column; gap: 6px; justify-content: center; align-items: center;">
                              <button onclick="handlePushAutohealingData(${actualIndex}, 'accept')" style="padding: 5px 10px; font-size: 11px; background: #28a745; color: #fff; border: none; border-radius: 3px; cursor: pointer; font-weight: 600; white-space: nowrap; width: 100%;">✓ Accept</button>
                              <button onclick="handlePushAutohealingData(${actualIndex}, 'reject')" style="padding: 5px 10px; font-size: 11px; background: #dc3545; color: #fff; border: none; border-radius: 3px; cursor: pointer; font-weight: 600; white-space: nowrap; width: 100%;">✕ Reject</button>
                          </div>` :
                          '<span style="color: #999; font-size: 11px;">N/A</span>'}
                  </td>
              </tr>
          `;
      });

    html += `
                </tbody>
            </table>
    `;

    // Add pagination
    const totalPages = Math.ceil(ahAllData.length / AH_ITEMS_PER_PAGE);
    if (totalPages > 1) {
        html += `
            <div style="display: flex; justify-content: center; align-items: center; gap: 10px; padding: 20px; background: #f8f9fa; border-top: 1px solid #e0e0e0;">
                <button onclick="ahPreviousPage()" ${ahCurrentPage === 1 ? 'disabled' : ''} style="padding: 8px 12px; border: 1px solid #ddd; background: white; border-radius: 4px; cursor: pointer; font-size: 14px;">← Previous</button>
        `;
        for (let i = 1; i <= totalPages; i++) {
            const isActive = i === ahCurrentPage;
            html += `<button onclick="ahGoToPage(${i})" style="padding: 8px 12px; border: 1px solid #ddd; background: ${isActive ? '#2d5a87' : 'white'}; color: ${isActive ? 'white' : 'black'}; border-radius: 4px; cursor: pointer; font-size: 14px; border-color: ${isActive ? '#2d5a87' : '#ddd'};">${i}</button>`;
        }
        html += `
                <button onclick="ahNextPage()" ${ahCurrentPage === totalPages ? 'disabled' : ''} style="padding: 8px 12px; border: 1px solid #ddd; background: white; border-radius: 4px; cursor: pointer; font-size: 14px;">Next →</button>
            </div>
        `;
    }

    html += `</div>`;
    container.innerHTML = html;
}

async function showAutoHealLocatorMap(index, type) {
     const item = ahAllData[index];
     
     if (!item) {
         alert('Item not found');
         return;
     }
     
     try {
         // Show loading state
         const loadingModal = document.createElement('div');
         loadingModal.className = 'es-modal-overlay visible';
         loadingModal.id = 'ahLocatorMapModal';
         loadingModal.style.display = 'flex';
         loadingModal.innerHTML = `
             <div class="es-modal">
                 <div class="es-modal-header">
                     <h4 class="es-modal-title">Loading Locator Map...</h4>
                 </div>
                 <div class="es-modal-body" style="text-align: center; padding: 40px;">
                     <span class="loading"></span>
                     <p>Fetching locator map data...</p>
                 </div>
             </div>
         `;
         document.body.appendChild(loadingModal);
         
         // Fetch the locator map data from API using getAutoHealData with responsetype
         const responseType = type === 'old' ? 'previousmap' : 'updatedmap';
         const params = new URLSearchParams({
             modulename: item.module_name,
             responsetype: responseType,
             featuretags: item.feature_tag || '',
             scenariotags: item.scenario_tag || '',
             scenarioname: item.scenario_name || ''
         });
         
         const response = await fetch(
             `${API_BASE_URL}/Web-Automation/getAutoHealData?${params.toString()}`
         );
         
         if (!response.ok) {
             const error = await response.json();
             throw new Error(error.detail || 'Failed to fetch locator map');
         }
         
         const data = await response.json();
         const mapDataArray = data.autohealResponse || [];
         
         if (!mapDataArray || mapDataArray.length === 0) {
             throw new Error('No locator map data available');
         }
         
         // Extract the map data from the response
         const mapData = responseType === 'previousmap'
             ? mapDataArray[0].old_step_loc_map
             : mapDataArray[0].updated_step_loc_map;
         
         if (!mapData) {
             throw new Error('No locator map data available');
         }
         
         const title = type === 'old' ? 'Existing Locator Map' : 'New Locator Map';
         
         // Parse the JSON data
         let stepsArray = [];
         try {
             let parsedData = mapData;
             if (typeof mapData === 'string') {
                 parsedData = JSON.parse(mapData);
             }
             
             // Extract steps array from the structure
             if (parsedData && parsedData.steps && Array.isArray(parsedData.steps)) {
                 stepsArray = parsedData.steps;
             } else if (Array.isArray(parsedData)) {
                 stepsArray = parsedData;
             }
         } catch (e) {
             console.error('Error parsing locator map:', e);
         }
         
         // Build table HTML
         let tableHtml = `
             <table style="width: 100%; border-collapse: collapse; background: white;">
                 <thead>
                     <tr style="background: #f0f0f0;">
                         <th style="border: 1px solid #ddd; padding: 12px; text-align: left; font-weight: 600; color: #333;">Step Name</th>
                         <th style="border: 1px solid #ddd; padding: 12px; text-align: left; font-weight: 600; color: #333;">Locator</th>
                     </tr>
                 </thead>
                 <tbody>
         `;
         
         if (stepsArray.length > 0) {
             stepsArray.forEach((step, idx) => {
                 const stepName = escapeHtml(step.stepname || 'N/A');
                 const locator = step.locator ? escapeHtml(step.locator) : '<span style="color: #999;">N/A</span>';
                 const bgColor = idx % 2 === 0 ? '#ffffff' : '#f9f9f9';
                 
                 tableHtml += `
                     <tr style="background: ${bgColor};">
                         <td style="border: 1px solid #ddd; padding: 10px 12px; font-size: 13px; color: #333; word-break: break-word;">${stepName}</td>
                         <td style="border: 1px solid #ddd; padding: 10px 12px; font-size: 12px; color: #333; font-family: 'Courier New', monospace; word-break: break-all;">${locator}</td>
                     </tr>
                 `;
             });
         } else {
             tableHtml += `
                 <tr>
                     <td colspan="2" style="border: 1px solid #ddd; padding: 20px; text-align: center; color: #999;">No steps available</td>
                 </tr>
             `;
         }
         
         tableHtml += `
                 </tbody>
             </table>
         `;
         
         let html = `
             <div class="es-modal" style="max-width: 1000px;">
                 <div class="es-modal-header">
                     <h4 class="es-modal-title">${title}</h4>
                 </div>
                 <div class="es-modal-body" style="max-height: 600px; overflow-y: auto;">
                     ${tableHtml}
                 </div>
                 <div class="es-modal-footer">
                     <button onclick="closeAutoHealLocatorMap()" class="es-modal-ok-btn">Close</button>
                 </div>
             </div>
         `;
         
         // Remove loading modal
         const existingModal = document.getElementById('ahLocatorMapModal');
         if (existingModal) {
             existingModal.remove();
         }
         
         // Create new modal with data
         const modal = document.createElement('div');
         modal.className = 'es-modal-overlay visible';
         modal.id = 'ahLocatorMapModal';
         modal.style.display = 'flex';
         modal.innerHTML = html;
         document.body.appendChild(modal);
         
         modal.onclick = function(e) {
             if (e.target === modal) {
                 closeAutoHealLocatorMap();
             }
         };
     } catch (error) {
         console.error('Error fetching locator map:', error);
         const existingModal = document.getElementById('ahLocatorMapModal');
         if (existingModal) {
             existingModal.remove();
         }
         alert('Failed to fetch locator map: ' + error.message);
     }
 }

function closeAutoHealLocatorMap() {
    const modal = document.getElementById('ahLocatorMapModal');
    if (modal) {
        modal.remove();
    }
}

async function triggerAutoHeal(index) {
    const item = ahAllData[index];
    if (!item) {
        alert('Item not found');
        return;
    }
    
    // Show confirmation modal instead of browser confirm dialog
    showAutoHealConfirmModal(index);
}

function showAutoHealConfirmModal(index) {
    const item = ahAllData[index];
    
    // Create modal overlay
    const modal = document.createElement('div');
    modal.className = 'es-modal-overlay visible';
    modal.id = 'ahConfirmModal';
    modal.style.display = 'flex';
    
    const html = `
        <div class="es-modal" style="max-width: 500px;">
            <div class="es-modal-header">
                <h4 class="es-modal-title">Confirm Auto-Heal</h4>
            </div>
            <div class="es-modal-body">
                <div style="margin-bottom: 20px;">
                    <p style="color: #333; font-size: 14px; line-height: 1.6;">
                        Are you sure you want to trigger Auto-Heal for this scenario?
                    </p>
                </div>
                <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 4px solid #2d5a87; margin-bottom: 20px;">
                    <div style="margin-bottom: 12px;">
                        <div style="font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; margin-bottom: 4px;">Feature Name</div>
                        <div style="font-size: 13px; color: #333;">${escapeHtml(item.feature_name || 'N/A')}</div>
                    </div>
                    <div style="margin-bottom: 12px;">
                        <div style="font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; margin-bottom: 4px;">Scenario Name</div>
                        <div style="font-size: 13px; color: #333;">${escapeHtml(item.scenario_name || 'N/A')}</div>
                    </div>
                    <div style="margin-bottom: 12px;">
                        <div style="font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; margin-bottom: 4px;">Module</div>
                        <div style="font-size: 13px; color: #333;">${escapeHtml(item.module_name || 'N/A')}</div>
                    </div>
                    <div>
                        <div style="font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; margin-bottom: 4px;">Status</div>
                        <div style="font-size: 13px; color: #333;">${escapeHtml(item.status || 'N/A')}</div>
                    </div>
                </div>
                <p style="color: #666; font-size: 12px; margin: 0;">
                    This action will trigger the auto-heal process for the selected scenario.
                </p>
            </div>
            <div class="es-modal-footer">
                <button onclick="closeAutoHealConfirmModal()" class="es-modal-cancel-btn" style="padding: 8px 16px; margin-right: 8px; background: #6c757d; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 600;">Cancel</button>
                <button onclick="executeAutoHeal(${index})" class="es-modal-ok-btn" style="padding: 8px 16px; background: #28a745; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 600;">Confirm</button>
            </div>
        </div>
    `;
    
    modal.innerHTML = html;
    document.body.appendChild(modal);
    
    // Close modal when clicking outside
    modal.onclick = function(e) {
        if (e.target === modal) {
            closeAutoHealConfirmModal();
        }
    };
}

function closeAutoHealConfirmModal() {
    const modal = document.getElementById('ahConfirmModal');
    if (modal) {
        modal.remove();
    }
}

async function executeAutoHeal(index) {
    const item = ahAllData[index];
    
    // Close the confirmation modal
    closeAutoHealConfirmModal();
    
    try {
        const params = new URLSearchParams({
            modulename: item.module_name,
            featuretags: item.feature_tag || '',
            scenariotags: item.scenario_tag || '',
            scenarioname: item.scenario_name || ''
        });
        
        const response = await fetch(
            `${API_BASE_URL}/Web-Automation/autoheal?${params.toString()}`
        );
        
        const data = await response.json();
        
        if (response.ok || response.status === 202) {
            showAutoHealResultModal('success', 'Auto-Heal Triggered Successfully', data);
        } else {
            showAutoHealResultModal('error', 'Auto-Heal Failed', data);
        }
    } catch (error) {
        console.error('Error triggering auto-heal:', error);
        showAutoHealResultModal('error', 'Auto-Heal Error', { message: error.message });
    }
}

function showAutoHealResultModal(type, title, data) {
    const modal = document.createElement('div');
    modal.className = 'es-modal-overlay visible';
    modal.id = 'ahResultModal';
    modal.style.display = 'flex';
    
    const isSuccess = type === 'success';
    const bgColor = isSuccess ? '#d4edda' : '#f8d7da';
    const borderColor = isSuccess ? '#c3e6cb' : '#f5c6cb';
    const textColor = isSuccess ? '#155724' : '#721c24';
    const iconColor = isSuccess ? '#28a745' : '#dc3545';
    const icon = isSuccess ? '✓' : '✕';
    
    let bodyContent = '';
    
    if (isSuccess) {
        bodyContent = `
            <div style="background: ${bgColor}; padding: 20px; border-radius: 8px; border-left: 4px solid ${iconColor}; margin-bottom: 20px;">
                <div style="display: flex; align-items: flex-start; gap: 12px;">
                    <div style="font-size: 24px; color: ${iconColor}; font-weight: bold; flex-shrink: 0;">${icon}</div>
                    <div>
                        <div style="font-weight: 600; color: ${textColor}; margin-bottom: 8px;">Auto-Heal Triggered Successfully!</div>
                        <div style="color: ${textColor}; font-size: 13px; line-height: 1.6;">
                            <div>${escapeHtml(data.message || 'Auto-Heal process has been started.')}</div>
                            ${data.runid ? `<div style="margin-top: 8px;"><strong>Run ID:</strong> <span style="font-family: 'Courier New', monospace;">${escapeHtml(data.runid)}</span></div>` : ''}
                        </div>
                    </div>
                </div>
            </div>
        `;
    } else {
        bodyContent = `
            <div style="background: ${bgColor}; padding: 20px; border-radius: 8px; border-left: 4px solid ${iconColor}; margin-bottom: 20px;">
                <div style="display: flex; align-items: flex-start; gap: 12px;">
                    <div style="font-size: 24px; color: ${iconColor}; font-weight: bold; flex-shrink: 0;">${icon}</div>
                    <div>
                        <div style="font-weight: 600; color: ${textColor}; margin-bottom: 8px;">Auto-Heal Failed</div>
                        <div style="color: ${textColor}; font-size: 13px; line-height: 1.6;">
                            <strong>Error:</strong><br/>
                            <div style="background: white; padding: 8px; border-radius: 4px; margin-top: 4px; font-family: 'Courier New', monospace; font-size: 12px; word-break: break-word;">
                                ${escapeHtml(data.message || 'Unknown error')}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }
    
    const html = `
        <div class="es-modal" style="max-width: 600px;">
            <div class="es-modal-header">
                <h4 class="es-modal-title">${title}</h4>
            </div>
            <div class="es-modal-body">
                ${bodyContent}
            </div>
            <div class="es-modal-footer">
                <button onclick="closeAutoHealResultModal()" class="es-modal-ok-btn" style="padding: 8px 16px; background: #2d5a87; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 600;">Close</button>
            </div>
        </div>
    `;
    
    modal.innerHTML = html;
    document.body.appendChild(modal);
    
    // Close modal when clicking outside
    modal.onclick = function(e) {
        if (e.target === modal) {
            closeAutoHealResultModal();
        }
    };
}

async function handlePushAutohealingData(index, action) {
    const item = ahAllData[index];
    
    if (!item) {
        alert('Item not found');
        return;
    }
    
    // Show confirmation modal
    const modal = document.createElement('div');
    modal.className = 'es-modal-overlay visible';
    modal.id = 'ahPushConfirmModal';
    modal.style.display = 'flex';
    
    const actionText = action === 'accept' ? 'Accept' : 'Reject';
    const actionMessage = action === 'accept' 
        ? 'Apply the auto-healed script and replace the existing complete script?'
        : 'Discard the auto-healed script without applying it?';
    const btnBgColor = action === 'accept' ? '#28a745' : '#dc3545';
    
    const html = `
        <div class="es-modal" style="max-width: 500px;">
            <div class="es-modal-header">
                <h4 class="es-modal-title">Confirm ${actionText}</h4>
            </div>
            <div class="es-modal-body">
                <div style="margin-bottom: 20px;">
                    <p style="color: #333; font-size: 14px; line-height: 1.6;">
                        Are you sure you want to ${actionText.toLowerCase()} the auto-healing changes?
                    </p>
                    <p style="color: #666; font-size: 13px; line-height: 1.5; margin-top: 10px;">
                        ${actionMessage}
                    </p>
                </div>
                <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 4px solid #2d5a87; margin-bottom: 20px;">
                    <div style="margin-bottom: 12px;">
                        <div style="font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; margin-bottom: 4px;">Module</div>
                        <div style="font-size: 13px; color: #333;">${escapeHtml(item.module_name || 'N/A')}</div>
                    </div>
                    <div style="margin-bottom: 12px;">
                        <div style="font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; margin-bottom: 4px;">Feature</div>
                        <div style="font-size: 13px; color: #333;">${escapeHtml(item.feature_name || 'N/A')}</div>
                    </div>
                    <div>
                        <div style="font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; margin-bottom: 4px;">Scenario</div>
                        <div style="font-size: 13px; color: #333;">${escapeHtml(item.scenario_name || 'N/A')}</div>
                    </div>
                </div>
            </div>
            <div class="es-modal-footer">
                <button onclick="closeAutoHealPushConfirmModal()" class="es-modal-cancel-btn" style="padding: 8px 16px; margin-right: 8px; background: #6c757d; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 600;">Cancel</button>
                <button onclick="executeAutoHealingDataAction(${index}, '${action}')" class="es-modal-ok-btn" style="padding: 8px 16px; background: ${btnBgColor}; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 600;">${actionText}</button>
            </div>
        </div>
    `;
    
    modal.innerHTML = html;
    document.body.appendChild(modal);
    
    // Close modal when clicking outside
    modal.onclick = function(e) {
        if (e.target === modal) {
            closeAutoHealPushConfirmModal();
        }
    };
}

function closeAutoHealPushConfirmModal() {
    const modal = document.getElementById('ahPushConfirmModal');
    if (modal) {
        modal.remove();
    }
}

async function executeAutoHealingDataAction(index, action) {
    const item = ahAllData[index];
    
    // Close the confirmation modal
    closeAutoHealPushConfirmModal();
    
    try {
        const params = new URLSearchParams({
            modulename: item.module_name,
            featuretags: item.feature_tag || '',
            scenariotags: item.scenario_tag || '',
            scenarioname: item.scenario_name || '',
            action: action
        });
        
        const response = await fetch(
            `${API_BASE_URL}/Web-Automation/pushAutohealingData?${params.toString()}`,
            {
                method: 'POST'
            }
        );
        
        const data = await response.json();
        
        if (response.ok) {
            showAutoHealPushResultModal('success', `Auto-Healing ${action === 'accept' ? 'Accepted' : 'Rejected'} Successfully`, data);
            // Refresh the data after successful action
            setTimeout(() => {
                fetchAutoHealData();
            }, 1500);
        } else {
            showAutoHealPushResultModal('error', `Auto-Healing ${action === 'accept' ? 'Accept' : 'Reject'} Failed`, data);
        }
    } catch (error) {
        console.error('Error processing auto-healing data:', error);
        showAutoHealPushResultModal('error', 'Error Processing Auto-Healing', { message: error.message });
    }
}

function showAutoHealPushResultModal(type, title, data) {
    const modal = document.createElement('div');
    modal.className = 'es-modal-overlay visible';
    modal.id = 'ahPushResultModal';
    modal.style.display = 'flex';
    
    const isSuccess = type === 'success';
    const bgColor = isSuccess ? '#d4edda' : '#f8d7da';
    const borderColor = isSuccess ? '#c3e6cb' : '#f5c6cb';
    const textColor = isSuccess ? '#155724' : '#721c24';
    const iconColor = isSuccess ? '#28a745' : '#dc3545';
    const icon = isSuccess ? '✓' : '✕';
    
    let bodyContent = '';
    
    if (isSuccess) {
        bodyContent = `
            <div style="background: ${bgColor}; padding: 20px; border-radius: 8px; border-left: 4px solid ${iconColor}; margin-bottom: 20px;">
                <div style="display: flex; align-items: flex-start; gap: 12px;">
                    <div style="font-size: 24px; color: ${iconColor}; font-weight: bold; flex-shrink: 0;">${icon}</div>
                    <div>
                        <div style="font-weight: 600; color: ${textColor}; margin-bottom: 8px;">Action completed successfully!</div>
                        <div style="color: ${textColor}; font-size: 13px; line-height: 1.6;">
                            <div>${escapeHtml(data.message || 'The action has been processed successfully.')}</div>
                        </div>
                    </div>
                </div>
            </div>
        `;
    } else {
        bodyContent = `
            <div style="background: ${bgColor}; padding: 20px; border-radius: 8px; border-left: 4px solid ${iconColor}; margin-bottom: 20px;">
                <div style="display: flex; align-items: flex-start; gap: 12px;">
                    <div style="font-size: 24px; color: ${iconColor}; font-weight: bold; flex-shrink: 0;">${icon}</div>
                    <div>
                        <div style="font-weight: 600; color: ${textColor}; margin-bottom: 8px;">Action failed</div>
                        <div style="color: ${textColor}; font-size: 13px; line-height: 1.6;">
                            <strong>Error:</strong><br/>
                            <div style="background: white; padding: 8px; border-radius: 4px; margin-top: 4px; font-family: 'Courier New', monospace; font-size: 12px; word-break: break-word;">
                                ${escapeHtml(data.message || 'Unknown error')}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }
    
    const html = `
        <div class="es-modal" style="max-width: 600px;">
            <div class="es-modal-header">
                <h4 class="es-modal-title">${title}</h4>
            </div>
            <div class="es-modal-body">
                ${bodyContent}
            </div>
            <div class="es-modal-footer">
                <button onclick="closeAutoHealPushResultModal()" class="es-modal-ok-btn" style="padding: 8px 16px; background: #2d5a87; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 600;">Close</button>
            </div>
        </div>
    `;
    
    modal.innerHTML = html;
    document.body.appendChild(modal);
    
    // Close modal when clicking outside
    modal.onclick = function(e) {
        if (e.target === modal) {
            closeAutoHealPushResultModal();
        }
    };
}

function closeAutoHealPushResultModal() {
    const modal = document.getElementById('ahPushResultModal');
    if (modal) {
        modal.remove();
    }
}

function closeAutoHealResultModal() {
    const modal = document.getElementById('ahResultModal');
    if (modal) {
        modal.remove();
    }
}

async function downloadAutoHealUpdatedScript(index) {
    const item = ahAllData[index];
    if (!item.updated_complete_script) {
        alert('Updated script not available');
        return;
    }
    
    try {
        // Make API call to get updated scripts using feature tags, scenario tags, and scenario name
        const params = new URLSearchParams({
            modulename: item.module_name,
            responsetype: 'updatedscrips',
            featuretags: item.feature_tag || '',
            scenariotags: item.scenario_tag || '',
            scenarioname: item.scenario_name || ''
        });
        
        const response = await fetch(
            `${API_BASE_URL}/Web-Automation/getAutoHealData?${params.toString()}`
        );
        
        if (!response.ok) {
            throw new Error('Failed to fetch updated script');
        }
        
        const data = await response.json();
        const updatedScriptData = data.autohealResponse && data.autohealResponse.length > 0
            ? data.autohealResponse[0].updated_complete_script
            : null;
        
        if (!updatedScriptData) {
            alert('Updated script not available');
            return;
        }
        
        const content = updatedScriptData;
        const filename = `${item.scenario_name}_updated_script_${new Date().getTime()}.ts`;

        const blob = new Blob([content], { type: 'text/plain' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
    } catch (error) {
        console.error('Error downloading updated script:', error);
        alert('Failed to download updated script: ' + error.message);
    }
}

 async function downloadAutoHealFeatureSteps(index) {
     const item = ahAllData[index];
     if (!item) {
         alert('Item not found');
         return;
     }
     
     try {
         // Make API call to get feature steps using feature tags, scenario tags, and scenario name
         const params = new URLSearchParams({
             modulename: item.module_name,
             responsetype: 'feature',
             featuretags: item.feature_tag || '',
             scenariotags: item.scenario_tag || '',
             scenarioname: item.scenario_name || ''
         });
         
         const response = await fetch(
             `${API_BASE_URL}/Web-Automation/getAutoHealData?${params.toString()}`
         );
         
         if (!response.ok) {
             throw new Error('Failed to fetch feature steps');
         }
         
         const data = await response.json();
         const featureStepsData = data.autohealResponse && data.autohealResponse.length > 0
             ? data.autohealResponse[0].feature_steps
             : null;
         
         if (!featureStepsData) {
             alert('Feature steps not available');
             return;
         }
         
         const content = typeof featureStepsData === 'string' ? featureStepsData : JSON.stringify(featureStepsData, null, 2);
         const filename = `${item.scenario_name}_feature_steps_${new Date().getTime()}.txt`;

         const blob = new Blob([content], { type: 'text/plain' });
         const url = window.URL.createObjectURL(blob);
         const a = document.createElement('a');
         a.href = url;
         a.download = filename;
         document.body.appendChild(a);
         a.click();
         window.URL.revokeObjectURL(url);
         document.body.removeChild(a);
     } catch (error) {
         console.error('Error downloading feature steps:', error);
         alert('Failed to download feature steps: ' + error.message);
     }
 }

 async function downloadAutoHealCompleteScript(index) {
     const item = ahAllData[index];
     if (!item) {
         alert('Item not found');
         return;
     }
     
     try {
         // Make API call to get complete scripts using feature tags, scenario tags, and scenario name
         const params = new URLSearchParams({
             modulename: item.module_name,
             responsetype: 'completeScripts',
             featuretags: item.feature_tag || '',
             scenariotags: item.scenario_tag || '',
             scenarioname: item.scenario_name || ''
         });
         
         const response = await fetch(
             `${API_BASE_URL}/Web-Automation/getAutoHealData?${params.toString()}`
         );
         
         if (!response.ok) {
             throw new Error('Failed to fetch complete scripts');
         }
         
         const data = await response.json();
         const completeScriptData = data.autohealResponse && data.autohealResponse.length > 0
             ? data.autohealResponse[0].complete_script
             : null;
         
         if (!completeScriptData) {
             alert('Complete script not available');
             return;
         }
         
         const content = completeScriptData;
         const filename = `${item.scenario_name}_complete_script_${new Date().getTime()}.ts`;

         const blob = new Blob([content], { type: 'text/plain' });
         const url = window.URL.createObjectURL(blob);
         const a = document.createElement('a');
         a.href = url;
         a.download = filename;
         document.body.appendChild(a);
         a.click();
         window.URL.revokeObjectURL(url);
         document.body.removeChild(a);
     } catch (error) {
         console.error('Error downloading complete script:', error);
         alert('Failed to download complete script: ' + error.message);
     }
 }

function ahPreviousPage() {
    if (ahCurrentPage > 1) {
        ahCurrentPage--;
        displayAutoHealResults();
        window.scrollTo(0, 0);
    }
}

function ahNextPage() {
    const totalPages = Math.ceil(ahAllData.length / AH_ITEMS_PER_PAGE);
    if (ahCurrentPage < totalPages) {
        ahCurrentPage++;
        displayAutoHealResults();
        window.scrollTo(0, 0);
    }
}

function ahGoToPage(page) {
    ahCurrentPage = page;
    displayAutoHealResults();
    window.scrollTo(0, 0);
}

function resetAutoHealFilters() {
    // Safely reset only elements that actually exist in the DOM
    const ahModuleSelect = document.getElementById('ahModuleSelect');
    if (ahModuleSelect) {
        ahModuleSelect.value = '';
    }
    
    // These filter fields don't exist in the current HTML, skip them safely
    const ahFeatureTags = document.getElementById('ahFeatureTags');
    if (ahFeatureTags) {
        ahFeatureTags.value = '';
    }
    
    const ahScenarioTags = document.getElementById('ahScenarioTags');
    if (ahScenarioTags) {
        ahScenarioTags.value = '';
    }
    
    const ahScenarioName = document.getElementById('ahScenarioName');
    if (ahScenarioName) {
        ahScenarioName.value = '';
    }
    
    // Reset data and display
    ahAllData = [];
    ahCurrentPage = 1;
    const ahResultsContainer = document.getElementById('ahResultsContainer');
    if (ahResultsContainer) {
        ahResultsContainer.innerHTML = `
            <div style="margin-bottom: 32px; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                <div style="text-align: center; padding: 40px; color: #666; font-style: italic;">Select filters and click "Search" to view auto-heal data</div>
            </div>
        `;
    }
}

function showAutoHealLoading() {
    document.getElementById('ahResultsContainer').innerHTML = `
        <div style="margin-bottom: 32px; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
            <div style="text-align: center; padding: 40px;">
                <span class="loading"></span>
                <span>Loading auto-heal data...</span>
            </div>
        </div>
    `;
}

function showAutoHealError(message) {
    document.getElementById('ahResultsContainer').innerHTML = `
        <div style="margin-bottom: 32px; background: #f8d7da; padding: 20px; border-radius: 8px; border: 1px solid #f5c6cb;">
            <div style="color: #721c24;">⚠️ ${escapeHtml(message)}</div>
        </div>
    `;
}

function formatJson(data) {
    if (!data) return 'N/A';
    try {
        let jsonString;
        if (typeof data === 'string') {
            jsonString = JSON.stringify(JSON.parse(data), null, 2);
        } else {
            jsonString = JSON.stringify(data, null, 2);
        }
        // Escape HTML to prevent XSS when JSON is rendered in innerHTML
        return escapeHtml(jsonString);
    } catch (e) {
        return escapeHtml(String(data));
    }
}

function formatDate(dateString) {
    if (!dateString) return 'N/A';
    const date = new Date(dateString);
    return date.toLocaleString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}


// ============================================
// EXECUTION STATUS FUNCTIONS
// ============================================

// Note: API_BASE_URL is defined in app.js which loads before this file

// Global store for error details to avoid HTML escaping issues
const errorDetailsStore = new Map();
let errorDetailsListenersAttached = false;

// Function to show error details modal
function showErrorDetailsModal(errorId) {
    const modal = document.getElementById('esErrorDetailsModal');
    if (!modal) return;
    
    const errorData = errorDetailsStore.get(errorId);
    if (!errorData) {
        console.error('Error data not found for ID:', errorId);
        return;
    }
    
    // Set error message using textContent to preserve formatting
    const messageContent = document.querySelector('#esErrorDetailsModal .es-error-message-content');
    
    if (messageContent) {
        messageContent.textContent = errorData.error_message;
    }
    
    // Show modal
    modal.classList.add('visible');
}

// Function to close error details modal
function closeErrorDetailsModal() {
    const modal = document.getElementById('esErrorDetailsModal');
    if (modal) {
        modal.classList.remove('visible');
    }
}

// Attach event listeners for error details modal (only once)
function attachErrorDetailsListeners() {
    // Only attach listeners once - the modal is a persistent DOM element
    if (errorDetailsListenersAttached) {
        return;
    }
    errorDetailsListenersAttached = true;
    
    // Close modal when clicking outside or on close button
    const modal = document.getElementById('esErrorDetailsModal');
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                closeErrorDetailsModal();
            }
        });
        
        const closeBtn = modal.querySelector('.es-modal-ok-btn');
        if (closeBtn) {
            closeBtn.addEventListener('click', closeErrorDetailsModal);
        }
    }
}

// Initialize execution status section when opening
function loadExecutionStatusForToday() {
     const esDateInput = document.getElementById('esDateInput');
     if (esDateInput) {
         esDateInput.value = '';
         console.log('Date input cleared');
     }
     // Clear previous data - user must click Submit to load data
     const inProgressContainer = document.getElementById('esInProgressContainer');
     const completedContainer = document.getElementById('esCompletedContainer');
     if (inProgressContainer) {
         inProgressContainer.innerHTML = '<div class="es-no-data">Please select filters and click Submit</div>';
     }
     if (completedContainer) {
         completedContainer.innerHTML = '<div class="es-no-data">Please select filters and click Submit</div>';
     }
     populateModuleDropdown();
 }

// Populate module dropdown with available modules
async function populateModuleDropdown() {
     const esModuleInput = document.getElementById('esModuleInput');
     if (!esModuleInput) return;
     
     try {
         // Try to fetch modules from the API
         const response = await fetch(`${API_BASE_URL}/common-steps/domains`);
         if (response.ok) {
             const modules = await response.json();
             console.log('Modules fetched:', modules);
             
             // Clear existing options except the default
             esModuleInput.innerHTML = '<option value="">-- Select Module --</option>';
             
             // Add modules as options
             if (Array.isArray(modules)) {
                 modules.forEach(module => {
                     const option = document.createElement('option');
                     option.value = module;
                     option.textContent = module;
                     esModuleInput.appendChild(option);
                 });
             }
         }
     } catch (error) {
         console.warn('Failed to populate module dropdown:', error);
         // Keep default option if API fails
     }
 }

// Format date to YYYY-MM-DD for input[type="date"]
function formatDateForInput(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const formatted = `${year}-${month}-${day}`;
    console.log('Format date for input:', date, '→', formatted);
    return formatted;
}

// Format date to MMDDYYYY for API
function formatDateForAPI(date) {
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const year = date.getFullYear();
    return `${month}${day}${year}`;
}

// Load execution status by date, module, and runid
async function loadExecutionStatus(date = null, module = '', runid = '') {
     const inProgressContainer = document.getElementById('esInProgressContainer');
     const completedContainer = document.getElementById('esCompletedContainer');
     
     if (!inProgressContainer || !completedContainer) {
         console.error('Containers not found');
         return;
     }
     
     // Show loading
     inProgressContainer.innerHTML = '<div class="es-no-data">Loading...</div>';
     completedContainer.innerHTML = '<div class="es-no-data">Loading...</div>';
     
     try {
         // Build URL with query parameters
         const params = new URLSearchParams();
         
         // Only add date if it's provided
         if (date) {
             const dateStr = formatDateForAPI(date);
             params.append('date', dateStr);
             console.log('Loading execution status for date:', dateStr, 'module:', module, 'runid:', runid);
         } else {
             console.log('Loading execution status for module:', module, 'runid:', runid);
         }
         
         if (module) params.append('module', module);
         if (runid) params.append('runid', runid);
         
         const url = `${API_BASE_URL}/Web-Automation/execution-status?${params.toString()}`;
         console.log('Fetching from:', url);
         const response = await fetch(url);
         const data = await response.json();
         
         console.log('Response status:', response.status);
         console.log('Response data:', data);
         
         if (response.ok) {
             // Clear old error data before rendering new data
             errorDetailsStore.clear();
             renderExecutionTables(data);
             // Attach event listeners for error details modal
             attachErrorDetailsListeners();
         } else {
             throw new Error(data.detail || 'Failed to load execution status');
         }
     } catch (error) {
         console.error('Error loading execution status:', error);
         inProgressContainer.innerHTML = '<div class="es-no-data">Error loading data. Please try again.</div>';
         completedContainer.innerHTML = '<div class="es-no-data">Error loading data. Please try again.</div>';
     }
 }

// Group execution records by runid for row merging
function groupByRunId(executions) {
    const groups = {};
    executions.forEach(ex => {
        const runid = ex.runid || 'unknown';
        if (!groups[runid]) {
            groups[runid] = [];
        }
        groups[runid].push(ex);
    });
    return groups;
}

// Helper function to render video download cell
function renderVideoDownloadCell(videofile) {
    let executionLogCell = '';
    // Support array form: [{ name, status }, ...] (multi-recording)
    if (Array.isArray(videofile) && videofile.length > 0) {
        const activeFiles = videofile.filter(vf => vf && vf.name && vf.name.trim() && vf.status === 'Active').map(vf => vf.name);
        const expiredFiles = videofile.filter(vf => vf && vf.name && vf.name.trim() && vf.status === 'Test Log Expired').map(vf => vf.name);
        if (activeFiles.length > 1) {
            // Single link triggers sequential downloads for all active recordings
            // Escape JSON double quotes to HTML entities to avoid breaking the onclick attribute
            const jsonEncoded = JSON.stringify(activeFiles.map(name => encodeURIComponent(name))).replace(/"/g, '&quot;');
            executionLogCell = `<a href="javascript:void(0)" onclick="downloadAllRecordings(${jsonEncoded})" class="es-download-link">Download All</a>`;
        } else if (activeFiles.length === 1) {
            executionLogCell = `<a href="javascript:void(0)" onclick="downloadRecording('${escapeHtml(activeFiles[0])}')" class="es-download-link">Download</a>`;
        } else if (expiredFiles.length > 0) {
            executionLogCell = '<span class="es-expired-text">Test Log Expired</span>';
        } else {
            executionLogCell = '-';
        }
    } else if (videofile && videofile.name && videofile.name.trim()) {
        // Backward compatibility for single-object form: { name, status }
        if (videofile.status === 'Active') {
            executionLogCell = `<a href="javascript:void(0)" onclick="downloadRecording('${escapeHtml(videofile.name)}')" class="es-download-link">Download</a>`;
        } else if (videofile.status === 'Test Log Expired') {
            executionLogCell = '<span class="es-expired-text">Test Log Expired</span>';
        } else {
            executionLogCell = '-';
        }
    } else {
        executionLogCell = '-';
    }
    return executionLogCell;
}

// Render merged rows for grouped execution records with configurable columns
function renderMergedRows(groupedData, tableType, formatStatusDescription) {
    let html = '';
    
    Object.keys(groupedData).forEach(runid => {
        const scenarios = groupedData[runid];
        const rowCount = scenarios.length;
        
        scenarios.forEach((ex, index) => {
            const isFirst = index === 0;
            const statusClass = tableType === 'inprogress' 
                ? 'es-status-inprogress' 
                : (ex.status === 'PASSED' ? 'es-status-passed' : 'es-status-failed');
            
            html += '<tr>';
            
            // Run ID and Module columns - merge if multiple scenarios
            if (isFirst) {
                if (rowCount > 1) {
                    // Merged cells for Run ID and Module
                    html += `<td rowspan="${rowCount}" class="es-merged-cell"><span class="es-runid-link" onclick="showStepDetails('${escapeHtml(runid)}')">${escapeHtml(runid)}</span></td>`;
                    html += `<td rowspan="${rowCount}" class="es-merged-cell">${escapeHtml(ex.moduleName || 'N/A')}</td>`;
                } else {
                    // Single row - no merge
                    html += `<td><span class="es-runid-link" onclick="showStepDetails('${escapeHtml(runid)}')">${escapeHtml(runid)}</span></td>`;
                    html += `<td>${escapeHtml(ex.moduleName || 'N/A')}</td>`;
                }
            }
            // For non-first rows, skip Run ID and Module (they are merged)
            
            // Scenario Name - always individual per scenario
            html += `<td>${escapeHtml(ex.scenarioName || 'N/A')}</td>`;
            
            // Fourth column differs by table type
            if (tableType === 'inprogress') {
                // In-progress: Show "Successfully Executed Steps" (statusDescription)
                const formattedDesc = formatStatusDescription(ex.statusDescription);
                html += `<td class="es-status-desc">${escapeHtml(formattedDesc)}</td>`;
            } else if (tableType === 'completed') {
                // Completed: Show "Execution Log" (video download link)
                const executionLogCell = renderVideoDownloadCell(ex.videofile);
                html += `<td>${executionLogCell}</td>`;
            } else if (tableType === 'completed-with-downloads') {
                // Completed with extra download columns
                const executionLogCell = renderVideoDownloadCell(ex.videofile);
                html += `<td>${executionLogCell}</td>`;
                
                // Feature File download link - only show if feature_steps is not null
                const featureFileCell = (ex.feature_steps != null && ex.feature_steps.trim() !== '')
                    ? `<a href="javascript:void(0)" onclick="downloadDetailsFile('${escapeHtml(runid)}', 'featurefile', 'feature')" class="es-download-link">Download</a>`
                    : '-';
                html += `<td>${featureFileCell}</td>`;
                
                // Generated Script download link - only show if complt_scripts is not null
                const generatedScriptCell = (ex.complt_scripts != null && ex.complt_scripts.trim() !== '')
                    ? `<a href="javascript:void(0)" onclick="downloadDetailsFile('${escapeHtml(runid)}', 'totalscript', 'ts')" class="es-download-link">Download</a>`
                    : '-';
                html += `<td>${generatedScriptCell}</td>`;
                
            }
            
            // Status column - clickable for FAILED status to show error details
            if (ex.status === 'FAILED' && ex.error_message) {
                const errorId = `error_${Math.random().toString(36).substr(2, 9)}`;
                errorDetailsStore.set(errorId, {
                    error_message: ex.error_message
                });
                html += `<td><a href="#" class="es-status-badge ${statusClass} es-failed-status-link" data-error-id="${errorId}" onclick="event.preventDefault(); showErrorDetailsModal('${errorId}')">${escapeHtml(ex.status || 'Unknown')}</a></td>`;
            } else {
                html += `<td><span class="es-status-badge ${statusClass}">${escapeHtml(ex.status || 'Unknown')}</span></td>`;
            }
            
            html += '</tr>';
        });
    });
    
    return html;
}

// Render in-progress and completed execution tables
function renderExecutionTables(data) {
    const inProgressContainer = document.getElementById('esInProgressContainer');
    const completedContainer = document.getElementById('esCompletedContainer');
    
    // Helper function to format statusDescription
    function formatStatusDescription(statusDesc) {
        if (!statusDesc) return 'N/A';
        
        // If it's a string that looks like JSON, try to parse it
        if (typeof statusDesc === 'string') {
            try {
                const parsed = JSON.parse(statusDesc);
                
                // If it's an object with a 'status' key, extract that value
                if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && parsed.status) {
                    return String(parsed.status);
                }
                
                // If it's an array of steps, join them
                if (Array.isArray(parsed)) {
                    return parsed.join(' | ');
                }
                
                // If it's another type of object, stringify nicely
                if (typeof parsed === 'object') {
                    return JSON.stringify(parsed);
                }
                
                return statusDesc;
            } catch (e) {
                // Not JSON, return as is
                return statusDesc;
            }
        }
        
        // If it's already an object with status key, extract it
        if (statusDesc && typeof statusDesc === 'object' && !Array.isArray(statusDesc) && statusDesc.status) {
            return String(statusDesc.status);
        }
        
        // If it's already an array, join them
        if (Array.isArray(statusDesc)) {
            return statusDesc.join(' | ');
        }
        
        // If it's an object, stringify it
        if (typeof statusDesc === 'object') {
            return JSON.stringify(statusDesc);
        }
        
        return String(statusDesc);
    }
    
    // Render in-progress table
    if (data.inprogressExecution && data.inprogressExecution.length > 0) {
        // Group by runid for merging
        const groupedInProgress = groupByRunId(data.inprogressExecution);
        
        inProgressContainer.innerHTML = `
            <table class="es-table">
                <thead>
                    <tr>
                        <th>Run ID</th>
                        <th>Module</th>
                        <th>Scenario Name</th>
                        <th>Successfully Executed Steps</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    ${renderMergedRows(groupedInProgress, 'inprogress', formatStatusDescription)}
                </tbody>
            </table>
        `;
    } else {
        inProgressContainer.innerHTML = '<div class="es-no-data">No data present with the filter criteria</div>';
    }
    
    // Render completed table
    if (data.completedExecution && data.completedExecution.length > 0) {
        // Group by runid for merging
        const groupedCompleted = groupByRunId(data.completedExecution);
        
        completedContainer.innerHTML = `
            <table class="es-table">
                <thead>
                    <tr>
                        <th>Run ID</th>
                        <th>Module</th>
                        <th>Scenario Name</th>
                        <th>Execution Log (Video)</th>
                        <th>Feature File</th>
                        <th>Generated Script</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    ${renderMergedRows(groupedCompleted, 'completed-with-downloads', formatStatusDescription)}
                </tbody>
            </table>
        `;
    } else {
        completedContainer.innerHTML = '<div class="es-no-data">No data present with the filter criteria</div>';
    }
}

// Show step details modal
async function showStepDetails(runid) {
    const modal = document.getElementById('esModal');
    const stepsList = document.getElementById('esModalStepsList');
    const modalTitle = document.querySelector('#esModal .es-modal-title');
    const addSelectedBtn = document.getElementById('esModalAddSelectedBtn');
    if (addSelectedBtn) addSelectedBtn.style.display = 'none';

    // Show modal with loading state
    modal.classList.add('visible');
    stepsList.innerHTML = '<li class="es-loading-step">⏳ Loading execution details...</li>';
    
    try {
        console.log('Fetching execution details for runid:', runid);
        const response = await fetch(`${API_BASE_URL}/Web-Automation/execution-details?runid=${encodeURIComponent(runid)}`);
        console.log('Response status:', response.status);
        
        const data = await response.json();
        console.log('Response data:', data);
        
        if (response.ok) {
            // Check if this is a multi-scenario response
            if (data.description && Array.isArray(data.description) && data.description.length > 0) {
                // Check if first item is an object (multi-scenario) or string (single scenario)
                const firstItem = data.description[0];
                
                if (typeof firstItem === 'object' && firstItem !== null && 'scenario_name' in firstItem) {
                    // === MULTI-SCENARIO RESPONSE ===
                    const statusEmoji = data.status === 'PASSED' ? '✅' : 
                                       data.status === 'FAILED' ? '❌' : '📋';
                    if (modalTitle) {
                        modalTitle.textContent = `${statusEmoji} Run ID: ${data.runid} (${data.status}) - ${data.description.length} Scenarios`;
                    }
                    
                    // Render each scenario with its steps
                    let scenariosHtml = '';
                    data.description.forEach((scenario, scenarioIndex) => {
                        const scenarioStatusEmoji = scenario.status === 'PASSED' ? '✅' : 
                                                   scenario.status === 'FAILED' ? '❌' : '📋';
                        const scenarioStatusClass = scenario.status === 'PASSED' ? 'es-scenario-passed' : 
                                                   scenario.status === 'FAILED' ? 'es-scenario-failed' : '';
                        
                        scenariosHtml += `
                            <li class="es-scenario-header ${scenarioStatusClass}">
                                <strong>${scenarioStatusEmoji} Scenario ${scenarioIndex + 1}: ${escapeHtml(scenario.scenario_name || 'Unknown')}</strong>
                                <span class="es-scenario-status">[${escapeHtml(scenario.status || 'Unknown')}]</span>
                                ${scenario.module_name ? `<span class="es-scenario-module">Module: ${escapeHtml(scenario.module_name)}</span>` : ''}
                            </li>
                        `;
                        
                        if (scenario.steps && Array.isArray(scenario.steps) && scenario.steps.length > 0) {
                            scenario.steps.forEach(step => {
                                let statusClass = 'es-step-default';
                                let statusIcon = '•';
                                
                                if (step.includes('Step execution Completed')) {
                                    statusClass = 'es-step-completed';
                                    statusIcon = '✓';
                                } else if (step.includes('Step execution IN-Progress')) {
                                    statusClass = 'es-step-in-progress';
                                    statusIcon = '⏳';
                                } else if (step.includes('Step execution failed') || step.includes('Error')) {
                                    statusClass = 'es-step-failed';
                                    statusIcon = '✗';
                                }
                                
                                scenariosHtml += `
                                    <li class="${statusClass} es-step-indented">
                                        <span class="es-step-icon">${statusIcon}</span>
                                        <span class="es-step-text">${escapeHtml(step)}</span>
                                    </li>
                                `;
                            });
                        } else {
                            scenariosHtml += '<li class="es-no-steps es-step-indented">📭 No step details available</li>';
                        }
                    });
                    
                    stepsList.innerHTML = scenariosHtml;
                } else {
                    // === SINGLE SCENARIO RESPONSE (original format - strings) ===
                    // Update modal title with scenario name and status
                    if (data.scenarioName && modalTitle) {
                        const statusEmoji = data.status === 'PASSED' ? '✅' : 
                                           data.status === 'FAILED' ? '❌' : 
                                           data.status === 'IN_PROGRESS' ? '🔄' : '📋';
                        modalTitle.textContent = `${statusEmoji} ${data.scenarioName} (${data.status})`;
                    } else if (modalTitle) {
                        modalTitle.textContent = `Execution Details (${data.status || 'Unknown'})`;
                    }
                    
                    // Render step details with enhanced color coding
                    stepsList.innerHTML = data.description.map((step, index) => {
                        // Determine status and apply styling
                        let statusClass = 'es-step-default';
                        let statusIcon = '•';
                        
                        if (step.includes('Step execution Completed')) {
                            statusClass = 'es-step-completed';
                            statusIcon = '✓';
                        } else if (step.includes('Step execution IN-Progress')) {
                            statusClass = 'es-step-in-progress';
                            statusIcon = '⏳';
                        } else if (step.includes('Step execution failed') || step.includes('Error')) {
                            statusClass = 'es-step-failed';
                            statusIcon = '✗';
                        }
                        
                        return `<li class="${statusClass}">
                            <span class="es-step-icon">${statusIcon}</span>
                            <span class="es-step-text">${escapeHtml(step)}</span>
                        </li>`;
                    }).join('');
                }
            } else {
                // No description or empty - show no steps message
                if (modalTitle) {
                    modalTitle.textContent = `Execution Details (${data.status || 'Unknown'})`;
                }
                stepsList.innerHTML = '<li class="es-no-steps">📭 No step details available</li>';
            }
        } else {
            throw new Error(data.detail || 'Failed to load step details');
        }
    } catch (error) {
        console.error('Error loading step details:', error);
        stepsList.innerHTML = '<li class="es-error-step">❌ Error loading step details. Please try again.</li>';
        if (modalTitle) {
            modalTitle.textContent = 'Execution Details - Error';
        }
    }
}

// Close modal
function closeStepDetailsModal() {
    const modal = document.getElementById('esModal');
    modal.classList.remove('visible');
}

/** When showing modal with "Add Selected Steps", the iframe window to postMessage back to */
let _commonStepsModalSourceWindow = null;

/**
 * Escape HTML for safe display in the modal (used if escapeHtml is not in scope).
 */
function _escapeStepHtml(s) {
    if (typeof escapeHtml !== 'undefined') return escapeHtml(s);
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/**
 * Show the same Step Details modal with a list of steps.
 * Optionally show checkboxes and "Add Selected Steps" (e.g. for "click here" reuse flow).
 * Step text is shown without "Step execution Completed".
 * @param {string} title - Modal header text (e.g. "Step Details:")
 * @param {string[]} steps - Array of step text strings
 * @param {{ showAddSelectedSteps?: boolean }} options - If showAddSelectedSteps is true, show checkboxes and Add Selected Steps button
 */
function showCommonStepsModal(title, steps, options) {
    options = options || {};
    const showAddSelectedSteps = !!options.showAddSelectedSteps;
    const modal = document.getElementById('esModal');
    const stepsList = document.getElementById('esModalStepsList');
    const modalTitle = document.querySelector('#esModal .es-modal-title');
    const addSelectedBtn = document.getElementById('esModalAddSelectedBtn');
    if (!modal || !stepsList) return;
    if (modalTitle) {
        modalTitle.textContent = title || 'Step Details:';
    }
    addSelectedBtn.style.display = showAddSelectedSteps ? 'inline-block' : 'none';
    const stepSuffix = /\s*-\s*Step execution Completed\s*$/i;
    if (!steps || !Array.isArray(steps) || steps.length === 0) {
        stepsList.innerHTML = '<li class="es-no-steps">No steps for this key line.</li>';
        stepsList.removeAttribute('data-steps');
    } else {
        const stepsForAdd = steps.map(s => {
            const t = typeof s === 'string' ? s.trim() : String(s || '').trim();
            return t.replace(stepSuffix, '').trim();
        }).filter(Boolean);
        stepsList.dataset.steps = JSON.stringify(stepsForAdd);
        stepsList.innerHTML = stepsForAdd.map((step, index) => {
            const oneBased = index + 1;
            const line = step ? `${oneBased}. ${step}` : `${oneBased}.`;
            if (showAddSelectedSteps) {
                return `<li class="es-step-completed">
                    <input type="checkbox" class="es-step-checkbox" data-index="${index}" id="es-step-cb-${index}">
                    <label for="es-step-cb-${index}" class="es-step-label"><span class="es-step-icon">✓</span>
                    <span class="es-step-text">${_escapeStepHtml(line)}</span></label>
                </li>`;
            }
            return `<li class="es-step-completed">
                <span class="es-step-icon">✓</span>
                <span class="es-step-text">${_escapeStepHtml(line)}</span>
            </li>`;
        }).join('');
    }
    modal.classList.add('visible');
}

// Listen for postMessage from Create Steps iframe to show common-steps or script design preview
window.addEventListener('message', function (event) {
    if (event.data && event.data.type === 'SHOW_COMMON_STEPS_MODAL') {
        const title = event.data.title != null ? event.data.title : 'Step Details:';
        const steps = Array.isArray(event.data.steps) ? event.data.steps : [];
        const showAddSelectedSteps = !!event.data.showAddSelectedSteps;
        _commonStepsModalSourceWindow = showAddSelectedSteps ? event.source : null;
        showCommonStepsModal(title, steps, { showAddSelectedSteps: showAddSelectedSteps });
    } else if (event.data && event.data.type === 'SHOW_SCRIPT_PREVIEW_MODAL') {
        const steps = Array.isArray(event.data.steps) ? event.data.steps : [];
        const requestPayload = event.data.requestPayload && typeof event.data.requestPayload === 'object'
            ? event.data.requestPayload
            : null;
        if (typeof showScriptPreviewModalWithSteps === 'function') {
            showScriptPreviewModalWithSteps(steps, requestPayload);
        }
    } else if (event.data && event.data.type === 'OPEN_REUSE_STEPS_MODAL_AND_FETCH') {
        const module = (event.data.module != null && event.data.module !== '') ? String(event.data.module).trim() : '';
        if (module) {
            _commonStepsModalSourceWindow = event.source;
            openReuseStepsModalAndFetch(module);
        }
    } else if (event.data && event.data.type === 'OPEN_SCRIPT_PREVIEW_MODAL_AND_FETCH') {
        const unformattedrequest = (event.data.unformattedrequest != null) ? String(event.data.unformattedrequest) : '';
        const module = (event.data.module != null) ? String(event.data.module).trim() : '';
        if (unformattedrequest !== '' && module !== '') {
            openScriptPreviewModalAndFetch(unformattedrequest, module);
        }
    }
});

/**
 * Open the common steps modal first, then call stepbyModule and show response in the modal.
 * Used when user clicks "click here to re-use the successful workflow steps" from Create Steps.
 */
async function openReuseStepsModalAndFetch(module) {
    showCommonStepsModal('Step Details:', ['Loading...'], { showAddSelectedSteps: true });
    try {
        const url = `${API_BASE_URL}/Web-Automation/stepbyModule?module=${encodeURIComponent(module)}`;
        const response = await fetch(url);
        const data = response.ok ? await response.json().catch(() => null) : null;
        if (!response.ok || !data) {
            showCommonStepsModal('Step Details:', ['Could not load workflow steps. Please try again.'], { showAddSelectedSteps: false });
            return;
        }
        const csv = (data.commaSeparatedValues || '').trim();
        let steps = csv ? csv.split('|').map(s => s.trim()).filter(Boolean) : [];
        const stepSuffix = /\s*-\s*Step execution Completed\s*$/i;
        steps = steps.map(s => s.replace(stepSuffix, '').trim()).filter(Boolean);
        if (steps.length === 0) {
            showCommonStepsModal('Step Details:', ['No successful workflow steps found for this module.'], { showAddSelectedSteps: false });
            return;
        }
        showCommonStepsModal('Step Details:', steps, { showAddSelectedSteps: true });
    } catch (err) {
        console.error('Error loading workflow steps for reuse:', err);
        showCommonStepsModal('Step Details:', ['Error loading workflow steps. Please try again.'], { showAddSelectedSteps: false });
    }
}

/**
 * Open the Script Design Preview modal first, then call generateWorkflow and show response in the modal.
 * Used when user clicks "Show Steps Preview" from Create Steps.
 */
async function openScriptPreviewModalAndFetch(unformattedrequest, module) {
    if (typeof showScriptPreviewModalLoading === 'function') {
        showScriptPreviewModalLoading();
    }
    const commaSeparated = String(unformattedrequest || '').split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean).join(', ');
    try {
        const response = await fetch(`${API_BASE_URL}/Web-Automation/generateWorkflow`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ unformattedrequest: commaSeparated, module: module })
        });
        const data = await response.json().catch(() => ({}));
        if (data.status === 'ERROR' || !Array.isArray(data.formattedWorkflow)) {
            const errorSteps = [{ stepNo: 1, description: data.error || 'Failed to generate steps preview. Please try again.', action: '', confidence: '' }];
            if (typeof showScriptPreviewModalWithSteps === 'function') {
                showScriptPreviewModalWithSteps(errorSteps, null);
            }
            return;
        }
        const requestPayload = { unformattedrequest: commaSeparated, module: module };
        if (typeof showScriptPreviewModalWithSteps === 'function') {
            showScriptPreviewModalWithSteps(data.formattedWorkflow, requestPayload);
        }
    } catch (err) {
        console.error('Show Steps Preview error:', err);
        const errorSteps = [{ stepNo: 1, description: 'Failed to load steps preview. Please try again.', action: '', confidence: '' }];
        if (typeof showScriptPreviewModalWithSteps === 'function') {
            showScriptPreviewModalWithSteps(errorSteps, null);
        }
    }
}

/**
 * "Add Selected Steps" button: collect checked steps and postMessage to Create Steps iframe, then close modal.
 */
function onAddSelectedStepsClick() {
    const stepsList = document.getElementById('esModalStepsList');
    const stepsJson = stepsList && stepsList.dataset && stepsList.dataset.steps;
    if (!stepsJson) return;
    let steps;
    try {
        steps = JSON.parse(stepsJson);
    } catch (_) {
        return;
    }
    const checked = stepsList.querySelectorAll('.es-step-checkbox:checked');
    const indices = Array.from(checked).map(cb => parseInt(cb.getAttribute('data-index'), 10)).filter(n => !isNaN(n));
    const selected = indices.map(i => steps[i]).filter(Boolean);
    if (_commonStepsModalSourceWindow && selected.length > 0) {
        _commonStepsModalSourceWindow.postMessage({ type: 'ADD_SELECTED_STEPS', steps: selected }, '*');
    }
    closeStepDetailsModal();
    _commonStepsModalSourceWindow = null;
}
function downloadRecording(filename) {
    if (!filename) {
        alert('No recording filename available');
        return;
    }
    
    const downloadUrl = `${API_BASE_URL}/Web-Automation/recordings/download/${encodeURIComponent(filename)}`;
    console.log('Downloading recording from:', downloadUrl);
    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

async function downloadRecordingBundle(runid, filenames) {
    // Deprecated: no server bundle. Kept for backward compatibility.
}

// Trigger downloads for all filenames in the same user gesture context.
// Browsers often block subsequent downloads if delayed with async timers.
function downloadAllRecordings(encodedFilenames) {
    try {
        if (!Array.isArray(encodedFilenames) || encodedFilenames.length === 0) {
            alert('No recordings available to download');
            return;
        }
        // Fire all downloads synchronously so they are treated as part of the click action.
        for (let i = 0; i < encodedFilenames.length; i++) {
            const name = decodeURIComponent(encodedFilenames[i]);
            downloadRecording(name);
        }
    } catch (e) {
        console.error('Failed to download all recordings', e);
        alert('Failed to download some recordings');
    }
}

// Helper function to download details files (feature file or generated script)
async function downloadDetailsFile(runid, detailstype, extension) {
    if (!runid) {
        alert('No run ID available');
        return;
    }
    
    try {
        // Directly trigger GET download without HEAD preflight
        const downloadUrl = `${API_BASE_URL}/Web-Automation/execution-details/moredetails?runid=${encodeURIComponent(runid)}&detailstype=${detailstype}`;
        console.log(`Downloading ${detailstype} from:`, downloadUrl);

        const link = document.createElement('a');
        link.href = downloadUrl;
        // Suggested filename (server also provides Content-Disposition)
        const filename = `${runid}.${extension}`;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    } catch (error) {
        console.error('Error downloading file:', error);
        alert('Failed to download file. Please try again.');
    }
}

// Initialize execution status event listeners on DOM load
setTimeout(() => {
     const esSubmitBtn = document.getElementById('esSubmitBtn');
     const esDateInput = document.getElementById('esDateInput');
     const esModuleInput = document.getElementById('esModuleInput');
     const esRunIdInput = document.getElementById('esRunIdInput');
     
     if (esSubmitBtn) {
         esSubmitBtn.addEventListener('click', () => {
             const dateValue = esDateInput ? esDateInput.value : '';
             const module = esModuleInput ? esModuleInput.value : '';
             const runid = esRunIdInput ? esRunIdInput.value.trim() : '';
             
             // At least one filter must be provided
             if (!dateValue && !module && !runid) {
                 alert('Please provide at least one filter: Date, Module, or RunId');
                 return;
             }
             
             // If date is provided, validate it
             if (dateValue) {
                 const selectedDate = new Date(dateValue);
                 if (isNaN(selectedDate.getTime())) {
                     alert('Please select a valid date');
                     return;
                 }
                 loadExecutionStatus(selectedDate, module, runid);
             } else {
                 // If no date provided, pass null so API call doesn't include date parameter
                 loadExecutionStatus(null, module, runid);
             }
         });
     }
    
    // Modal close button
    const esModalOkBtn = document.getElementById('esModalOkBtn');
    if (esModalOkBtn) {
        esModalOkBtn.addEventListener('click', closeStepDetailsModal);
    }
    const esModalAddSelectedBtn = document.getElementById('esModalAddSelectedBtn');
    if (esModalAddSelectedBtn) {
        esModalAddSelectedBtn.addEventListener('click', onAddSelectedStepsClick);
    }
    
    // Close modal on overlay click
    const esModal = document.getElementById('esModal');
    if (esModal) {
        esModal.addEventListener('click', (e) => {
            if (e.target === esModal) {
                closeStepDetailsModal();
            }
        });
    }
}, 100);

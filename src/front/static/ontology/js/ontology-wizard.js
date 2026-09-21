/**
 * OntoBricks - ontology-wizard.js
 * Ontology Wizard - staged three-step Generate: Configure & Detect -> Review
 * Entities -> Complete (plan task 5 of `staged-ontology-generate`, see
 * docs/superpowers/specs/2026-09-20-three-stage-ontology-generate-design.md).
 *
 * This module owns Stage 1 (Configure & Detect) and Stage 3 (Complete)
 * orchestration plus the stepper. Stage 2 (Review Entities) rendering and
 * mutation is delegated to ontology-wizard-review.js (`window.WizardReview`),
 * which this file calls back into via `window.WizardCore`. Draft/entity/
 * checkpoint state always comes from the server (GET .../draft) — never
 * cached client-side. sessionStorage holds only the two active task ids.
 */

// =====================================================
// WIZARD STATE
// =====================================================

let wizardMetadataCache = null;
let wizardSelectedTables = new Set();
let wizardSelectedDocs = new Set();
let wizardDocsCache = [];
let wizardCurrentTaskId = null;  // Track running task (detect or complete)

// Session storage keys — task ids only. All draft/entity/checkpoint state
// is server-authoritative (GET /ontology/wizard/generate/draft); nothing
// about the draft itself is ever cached client-side.
const WIZARD_DETECT_TASK_KEY = 'ontobricks_wizard_detect_task';
const WIZARD_COMPLETE_TASK_KEY = 'ontobricks_wizard_complete_task';

const WIZARD_PROGRESS_CFG = {
    hostId: 'wizard-section',
    overlayId: 'wizardFormOverlay',
    overlayClass: 'task-progress-overlay wizard-form-overlay',
    titleId: 'wizardOverlayTitle',
    messageId: 'wizardOverlayMessage',
    progressBarId: 'wizardOverlayProgress',
    stepLogId: 'wizardStepLog',
    activityPanelId: 'wizardActivityLogPanel',
    activityLogId: 'wizardActivityLog',
    agentMountId: 'wizardAgentStepsMount',
    title: 'Detecting entities...',
    subtitle: 'Looking for candidate entities in your selected sources...',
};

// =====================================================
// WIZARD INITIALIZATION
// =====================================================

/**
 * Initialize the wizard section: resume any running detect/complete task,
 * load Stage 1 configure inputs, and resume/render whatever stage the
 * durable draft says we're in (Review or Complete) on page load/reload.
 */
async function initOntologyWizard() {
    console.log('[Wizard] Initializing...');

    await loadWizardMetadata();
    await loadWizardDocuments();
    await loadWizardTemplatesFromServer();

    const detectTaskId = sessionStorage.getItem(WIZARD_DETECT_TASK_KEY);
    const completeTaskId = sessionStorage.getItem(WIZARD_COMPLETE_TASK_KEY);

    if (detectTaskId) {
        await checkAndResumeWizardTask(detectTaskId, 'detect');
        return;
    }
    if (completeTaskId) {
        await checkAndResumeWizardTask(completeTaskId, 'complete');
        return;
    }

    // No running task — resume whatever the durable draft says (Review or
    // Complete pane), or stay on Configure when there is no draft yet.
    await resumeDraftFromServer();
}

/**
 * Check if a saved detect/complete task is still running and resume
 * monitoring it; otherwise fall back to resuming from the durable draft.
 */
async function checkAndResumeWizardTask(taskId, kind) {
    const storageKey = kind === 'complete' ? WIZARD_COMPLETE_TASK_KEY : WIZARD_DETECT_TASK_KEY;
    try {
        const response = await fetch(`/tasks/${taskId}`, { credentials: 'same-origin' });
        const data = await response.json();

        if (!data.success) {
            sessionStorage.removeItem(storageKey);
            await resumeDraftFromServer();
            return;
        }

        const task = data.task;

        if (task.status === 'running' || task.status === 'pending') {
            console.log('[Wizard] Resuming task monitoring:', taskId, kind);
            wizardCurrentTaskId = taskId;
            setWizardStage(kind === 'complete' ? 'complete' : 'configure');
            disableWizardForm(true);
            showWizardTaskProgress(task, kind);
            monitorWizardTask(taskId, kind);
        } else if (task.status === 'completed' && task.result) {
            sessionStorage.removeItem(storageKey);
            await handleWizardTaskResult(kind, task.result);
        } else if (task.status === 'failed') {
            sessionStorage.removeItem(storageKey);
            showNotification(
                (kind === 'complete' ? 'Completion' : 'Detection') +
                ' failed: ' + (task.error || 'Unknown error'),
                'error'
            );
            await resumeDraftFromServer();
        } else {
            sessionStorage.removeItem(storageKey);
            await resumeDraftFromServer();
        }
    } catch (error) {
        console.error('[Wizard] Error checking task:', error);
        sessionStorage.removeItem(storageKey);
        await resumeDraftFromServer();
    }
}

/**
 * Fetch the durable draft and render whichever stage it puts us in.
 * Called on init and whenever we return to a settled state (discard,
 * task failure, page reload).
 */
async function resumeDraftFromServer() {
    try {
        const response = await fetch('/ontology/wizard/generate/draft', {
            credentials: 'same-origin',
        });
        const data = await response.json();
        const draft = data.success ? data.draft : null;

        if (!draft) {
            setWizardStage('configure');
            if (window.WizardReview) window.WizardReview.reset();
            return;
        }

        if (draft.stage === 'reviewing') {
            setWizardStage('review');
            if (window.WizardReview) window.WizardReview.render(draft);
        } else {
            // 'completing' (partial/failed checkpoints) or 'done' (already
            // merged) both render on the Complete pane.
            setWizardStage('complete');
            renderCompleteChecklist(draft);
        }
    } catch (error) {
        console.error('[Wizard] Error resuming draft:', error);
        setWizardStage('configure');
    }
}

/**
 * Disable/enable the wizard form
 */
function disableWizardForm(disabled) {
    const form = document.getElementById('wizard-section');
    if (!form) return;

    const inputs = form.querySelectorAll('input, textarea, button, select');
    inputs.forEach(input => {
        if (disabled) {
            input.setAttribute('data-was-disabled', input.disabled);
            input.disabled = true;
        } else {
            const wasDisabled = input.getAttribute('data-was-disabled') === 'true';
            input.disabled = wasDisabled;
            input.removeAttribute('data-was-disabled');
        }
    });

    if (disabled) {
        if (typeof TaskProgressUI !== 'undefined') {
            TaskProgressUI.setOverlayVisible(WIZARD_PROGRESS_CFG, true);
        }
    } else if (typeof TaskProgressUI !== 'undefined') {
        TaskProgressUI.setOverlayVisible(WIZARD_PROGRESS_CFG, false);
    }
}

/**
 * Show task progress in the overlay for either the detect or complete task.
 */
function showWizardTaskProgress(task, kind) {
    if (typeof TaskProgressUI === 'undefined') return;
    const titleEl = document.getElementById(WIZARD_PROGRESS_CFG.titleId);
    if (titleEl) {
        titleEl.textContent = kind === 'complete'
            ? 'Completing ontology Generate...'
            : 'Detecting entities...';
    }
    TaskProgressUI.updateFromTask(WIZARD_PROGRESS_CFG, task);

    if (kind === 'complete') {
        renderCompleteChecklistFromTaskSteps(task);
    }
}

function _clearWizardProgressPanels() {
    if (typeof TaskProgressUI !== 'undefined') {
        TaskProgressUI.clearPanels(WIZARD_PROGRESS_CFG);
    }
}

/**
 * Monitor a wizard task (detect or complete) until completion.
 */
async function monitorWizardTask(taskId, kind) {
    const storageKey = kind === 'complete' ? WIZARD_COMPLETE_TASK_KEY : WIZARD_DETECT_TASK_KEY;
    const pollInterval = 1500;

    while (true) {
        try {
            await sleep(pollInterval);

            const response = await fetch(`/tasks/${taskId}`, { credentials: 'same-origin' });
            const data = await response.json();

            // In-memory tasks are lost when the dev server hot-reloads/restarts.
            if (response.status === 404 || (!data.success && data.error === 'not_found')) {
                sessionStorage.removeItem(storageKey);
                wizardCurrentTaskId = null;
                disableWizardForm(false);
                showNotification(
                    (kind === 'complete' ? 'Completion' : 'Detection') +
                    ' was interrupted (server restarted). Please try again.',
                    'warning'
                );
                await resumeDraftFromServer();
                break;
            }

            if (!data.success) {
                throw new Error(data.message || 'Task not found');
            }

            const task = data.task;
            showWizardTaskProgress(task, kind);

            if (task.status === 'completed') {
                sessionStorage.removeItem(storageKey);
                wizardCurrentTaskId = null;
                disableWizardForm(false);
                if (task.result) {
                    await handleWizardTaskResult(kind, task.result);
                }
                break;
            } else if (task.status === 'failed') {
                sessionStorage.removeItem(storageKey);
                wizardCurrentTaskId = null;
                disableWizardForm(false);
                showNotification(
                    (kind === 'complete' ? 'Completion' : 'Detection') +
                    ' failed: ' + (task.error || 'Unknown error'),
                    'error'
                );
                await resumeDraftFromServer();
                break;
            } else if (task.status === 'cancelled') {
                sessionStorage.removeItem(storageKey);
                wizardCurrentTaskId = null;
                disableWizardForm(false);
                showNotification(
                    (kind === 'complete' ? 'Completion' : 'Detection') + ' was cancelled',
                    'warning'
                );
                await resumeDraftFromServer();
                break;
            }
        } catch (error) {
            console.error('[Wizard] Monitoring error:', error);
            sessionStorage.removeItem(storageKey);
            wizardCurrentTaskId = null;
            disableWizardForm(false);
            showNotification('Error monitoring task', 'error');
            await resumeDraftFromServer();
            break;
        }
    }

    if (typeof refreshTasks === 'function') {
        refreshTasks();
    }
}

/**
 * Route a completed detect/complete task's result to the right stage.
 */
async function handleWizardTaskResult(kind, result) {
    if (kind === 'complete') {
        await handleCompletionSuccess(result);
        return;
    }
    // Detection completed — the durable draft is authoritative; fetch it
    // fresh rather than trusting the task result payload's shape.
    setWizardStage('review');
    await resumeDraftFromServer();
    showNotification(
        `Detected ${((result.draft || {}).candidate_entities || []).length} candidate entity(ies) — review them below.`,
        'success'
    );
}

/**
 * Helper function for delays
 */
function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// =====================================================
// STEPPER (Configure & Detect -> Review Entities -> Complete)
// =====================================================

const WIZARD_STAGE_ORDER = ['configure', 'review', 'complete'];
const WIZARD_STAGE_PANES = {
    configure: 'wizardConfigurePane',
    review: 'wizardReviewPane',
    complete: 'wizardCompletePane',
};

/**
 * Switch the visible stage pane and update every `.wizard-step` stepper
 * item's active/completed state + `aria-current` (accessible: screen
 * readers get the current step announced, not just a visual highlight).
 */
function setWizardStage(stage) {
    if (WIZARD_STAGE_ORDER.indexOf(stage) === -1) return;

    WIZARD_STAGE_ORDER.forEach(function (s) {
        const pane = document.getElementById(WIZARD_STAGE_PANES[s]);
        if (pane) pane.classList.toggle('ob-hidden', s !== stage);
    });

    const stageIndex = WIZARD_STAGE_ORDER.indexOf(stage);
    const stepIds = {
        configure: 'wizardStepConfigure',
        review: 'wizardStepReview',
        complete: 'wizardStepComplete',
    };
    WIZARD_STAGE_ORDER.forEach(function (s, idx) {
        const stepEl = document.getElementById(stepIds[s]);
        if (!stepEl) return;
        stepEl.classList.remove('active', 'completed');
        if (idx < stageIndex) {
            stepEl.classList.add('completed');
            stepEl.removeAttribute('aria-current');
        } else if (idx === stageIndex) {
            stepEl.classList.add('active');
            stepEl.setAttribute('aria-current', 'step');
        } else {
            stepEl.removeAttribute('aria-current');
        }
    });
}

// =====================================================
// STAGE 1: METADATA LOADING
// =====================================================

/**
 * Load metadata for the wizard
 */
async function loadWizardMetadata() {
    const statusEl = document.getElementById('wizardMetadataStatus');
    const previewEl = document.getElementById('wizardMetadataPreview');
    const noMetadataEl = document.getElementById('wizardNoMetadata');
    const tableBody = document.getElementById('wizardMetadataTableBody');

    try {
        const response = await fetch('/domain/metadata', { credentials: 'same-origin' });
        const result = await response.json();

        if (result.success && result.metadata && result.metadata.tables && result.metadata.tables.length > 0) {
            wizardMetadataCache = result.metadata;

            // Initialize selection - all tables selected by default
            wizardSelectedTables.clear();
            result.metadata.tables.forEach(table => {
                const tableName = table.full_name || table.name;
                wizardSelectedTables.add(tableName);
            });

            const tableCount = result.metadata.tables.length;
            let totalColumns = 0;
            result.metadata.tables.forEach(t => {
                totalColumns += (t.columns || []).length;
            });

            statusEl.innerHTML = `
                <div class="d-flex align-items-center">
                    <i class="bi bi-check-circle-fill text-success me-2 fs-5"></i>
                    <div>
                        <strong>${tableCount} tables</strong> available with <strong>${totalColumns} columns</strong>
                    </div>
                </div>
            `;

            tableBody.innerHTML = '';
            result.metadata.tables.forEach((table, index) => {
                const columnCount = (table.columns || []).length;
                const description = table.comment || table.description || '';
                const tableName = table.full_name || table.name;
                const displayName = tableName.split('.').pop();

                tableBody.innerHTML += `
                    <tr>
                        <td class="text-center">
                            <input type="checkbox" class="form-check-input wizard-table-checkbox" 
                                   data-table="${tableName}" id="wizardTable${index}" checked>
                        </td>
                        <td>
                            <label for="wizardTable${index}" class="mb-0 cursor-pointer">
                                <strong>${displayName}</strong>
                                <br><small class="text-muted">${tableName}</small>
                            </label>
                        </td>
                        <td class="text-center">${columnCount}</td>
                        <td class="small">${description || '<span class="text-muted">-</span>'}</td>
                    </tr>
                `;
            });

            previewEl.style.display = 'block';
            noMetadataEl.style.display = 'none';

            updateWizardSelectionCount();

            const genBtn = document.getElementById('wizardTopGenerateBtn');
            if (genBtn) genBtn.disabled = false;
        } else {
            statusEl.innerHTML = `
                <div class="d-flex align-items-center text-muted">
                    <i class="bi bi-info-circle me-2 fs-5"></i>
                    <span>No data sources loaded — you can still detect entities from documents or guidelines</span>
                </div>
            `;
            previewEl.style.display = 'none';
            noMetadataEl.style.display = 'block';
        }
    } catch (error) {
        console.error('[Wizard] Error loading metadata:', error);
        statusEl.innerHTML = `
            <div class="d-flex align-items-center text-danger">
                <i class="bi bi-x-circle-fill me-2 fs-5"></i>
                <span>Error loading data sources: ${error.message}</span>
            </div>
        `;
        noMetadataEl.style.display = 'block';
    }
}

// =====================================================
// TABLE SELECTION
// =====================================================

/**
 * Update selection when a table checkbox is toggled
 */
function updateWizardTableSelection(checkbox) {
    const tableName = checkbox.dataset.table;
    if (checkbox.checked) {
        wizardSelectedTables.add(tableName);
    } else {
        wizardSelectedTables.delete(tableName);
    }
    updateWizardSelectAllCheckbox();
    updateWizardSelectionCount();
}

/**
 * Select or deselect all tables
 */
function selectAllWizardTables(selectAll) {
    const checkboxes = document.querySelectorAll('.wizard-table-checkbox');
    checkboxes.forEach(cb => {
        cb.checked = selectAll;
        const tableName = cb.dataset.table;
        if (selectAll) {
            wizardSelectedTables.add(tableName);
        } else {
            wizardSelectedTables.delete(tableName);
        }
    });
    updateWizardSelectAllCheckbox();
    updateWizardSelectionCount();
}

/**
 * Sync the header "select all" checkbox with current row state.
 * Checked when all selected, unchecked when none, indeterminate when partial.
 */
function updateWizardSelectAllCheckbox() {
    const headerCb = document.getElementById('wizardSelectAllCheckbox');
    if (!headerCb) return;
    const total = document.querySelectorAll('.wizard-table-checkbox').length;
    const checked = wizardSelectedTables.size;
    headerCb.checked = total > 0 && checked === total;
    headerCb.indeterminate = checked > 0 && checked < total;
}

/**
 * Update the selection count display
 */
function updateWizardSelectionCount() {
    const countEl = document.getElementById('wizardSelectedCount');
    const total = wizardMetadataCache?.tables?.length || 0;
    const selected = wizardSelectedTables.size;

    if (countEl) {
        countEl.innerHTML = `<i class="bi bi-check2-square me-1"></i><strong>${selected}</strong> of ${total} tables selected for generation`;
    }
}

/**
 * Get selected metadata (only selected tables)
 */
function getSelectedMetadata() {
    if (!wizardMetadataCache) return null;

    const selectedTables = wizardMetadataCache.tables.filter(table => {
        const tableName = table.full_name || table.name;
        return wizardSelectedTables.has(tableName);
    });

    return {
        ...wizardMetadataCache,
        tables: selectedTables
    };
}

// =====================================================
// TEMPLATE LOADING  (fetched from backend global_config)
// =====================================================

let wizardTemplates = {};

/**
 * Fetch wizard quick-templates from the backend and render buttons.
 */
async function loadWizardTemplatesFromServer() {
    try {
        const response = await fetch('/ontology/wizard/templates', { credentials: 'same-origin' });
        const data = await response.json();
        if (data.success && data.templates) {
            wizardTemplates = data.templates;
            renderWizardTemplateButtons(wizardTemplates);
        }
    } catch (error) {
        console.error('[Wizard] Failed to load templates:', error);
    }
}

/**
 * Render template buttons dynamically into the Quick Templates container.
 */
function renderWizardTemplateButtons(templates) {
    const container = document.getElementById('wizardTemplateButtons');
    if (!container) return;
    container.innerHTML = '';
    for (const [key, tpl] of Object.entries(templates)) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-outline-secondary';
        btn.innerHTML = `<i class="bi bi-${tpl.icon || 'file-text'} me-1"></i>${tpl.label || key}`;
        btn.addEventListener('click', () => loadWizardTemplate(key));
        container.appendChild(btn);
    }
}

/**
 * Load a predefined template into the guidelines textarea
 */
function loadWizardTemplate(templateName) {
    const textarea = document.getElementById('wizardGuidelines');
    const tpl = wizardTemplates[templateName];
    if (tpl) {
        textarea.value = tpl.guidelines || '';
        showNotification(`Loaded ${(tpl.label || templateName).toUpperCase()} template`, 'info', 2000);
    }
}

// =====================================================
// STAGE 1: START DETECTION
// =====================================================

/**
 * Start Stage 1 candidate-entity detection (async task). If a draft is
 * already in Review/Complete, confirm before discarding it (detection
 * always starts a fresh review cycle — see `GenerateWorkflow.run_detection`).
 */
async function startGenerateDetection() {
    const reviewPane = document.getElementById('wizardReviewPane');
    const completePane = document.getElementById('wizardCompletePane');
    const hasExistingDraftInProgress =
        (reviewPane && !reviewPane.classList.contains('ob-hidden')) ||
        (completePane && !completePane.classList.contains('ob-hidden'));

    if (hasExistingDraftInProgress) {
        const confirmed = await showConfirmDialog({
            title: 'Re-run Detection',
            message:
                'Running detection again discards the current draft (any ' +
                'candidate edits, includes/excludes) and starts a new review ' +
                'cycle. Continue?',
            confirmText: 'Re-detect',
            confirmClass: 'btn-outline-danger',
            icon: 'arrow-repeat',
        });
        if (!confirmed) return;
    }

    await runGenerateDetection();
}

async function runGenerateDetection() {
    const selectedMetadata = getSelectedMetadata();
    const guidelines = document.getElementById('wizardGuidelines').value.trim();
    const selectedDocumentFiles = getSelectedDocumentFiles();
    const unavailableDocuments = selectedDocumentFiles.filter(
        file => file.parse_status !== 'ready'
    );
    const documents = selectedDocumentFiles
        .filter(file => file.parse_status === 'ready')
        .map(file => file.name);

    const hasMetadata = selectedMetadata && selectedMetadata.tables && selectedMetadata.tables.length > 0;
    const hasGuidelines = guidelines.length > 0;
    const hasDocs = documents.length > 0;

    if (unavailableDocuments.length > 0) {
        const names = unavailableDocuments.map(file => file.name).join(', ');
        showNotification(`Document parsing is not ready for: ${names}`, 'warning');
    }

    if (!hasMetadata && !hasGuidelines && !hasDocs) {
        const message = unavailableDocuments.length > 0
            ? 'Document parsing is not ready. Wait for a ready document or provide another input.'
            : 'Please provide at least data sources, documents, or guidelines';
        showNotification(message, 'warning');
        return;
    }

    const options = {
        includeDataProperties: document.getElementById('wizardIncludeDataProps').checked,
        includeRelationships: document.getElementById('wizardIncludeRelationships').checked,
        includeInheritance: document.getElementById('wizardIncludeInheritance').checked,
        useTableNames: document.getElementById('wizardUseTableNames').checked,
        useColumnComments: document.getElementById('wizardUseColumnComments').checked
    };

    try {
        const response = await fetch('/ontology/wizard/generate/detect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                metadata: hasMetadata ? selectedMetadata : {},
                guidelines: guidelines,
                options: options,
                documents: documents,
                tables: hasMetadata ? Array.from(wizardSelectedTables) : [],
            }),
            credentials: 'same-origin'
        });

        const startResult = await response.json();

        if (!startResult.success) {
            showNotification('Error: ' + startResult.message, 'error');
            return;
        }

        const taskId = startResult.task_id;
        console.log('[Wizard] Detection task started:', taskId);

        sessionStorage.setItem(WIZARD_DETECT_TASK_KEY, taskId);
        wizardCurrentTaskId = taskId;

        disableWizardForm(true);
        _clearWizardProgressPanels();

        if (typeof refreshTasks === 'function') {
            refreshTasks();
        }

        showNotification('Detecting candidate entities. You can navigate away and come back.', 'info');

        monitorWizardTask(taskId, 'detect');

    } catch (error) {
        console.error('[Wizard] Detection error:', error);
        showNotification('Error starting detection: ' + error.message, 'error');
    }
}

// =====================================================
// STAGE 3: COMPLETE (relations -> attributes -> axioms -> merge)
// =====================================================

const WIZARD_COMPLETE_SUBSTAGES = ['relations', 'attributes', 'axioms', 'merge'];

/** Map a completion checkpoint status to the checklist item's visual class + icon. */
function _completeStepVisual(status) {
    if (status === 'done') return { cls: 'wizard-complete-step-done', icon: 'bi-check-circle-fill text-success' };
    if (status === 'running') return { cls: 'wizard-complete-step-running', icon: '' }; // spinner injected separately
    if (status === 'failed') return { cls: 'wizard-complete-step-failed', icon: 'bi-x-circle-fill text-danger' };
    return { cls: '', icon: 'bi-circle text-muted' };
}

/**
 * Render the Stage 3 checklist from the durable draft's
 * `completion_checkpoints` + `merge_checkpoint` — the only source of truth
 * for "what's already done" on resume/retry.
 */
function renderCompleteChecklist(draft) {
    const checkpoints = draft.completion_checkpoints || {};
    const mergeCheckpoint = draft.merge_checkpoint || { status: 'pending' };

    WIZARD_COMPLETE_SUBSTAGES.forEach(function (substage) {
        const li = document.querySelector('[data-substage="' + substage + '"]');
        if (!li) return;
        const iconWrap = li.querySelector('.wizard-complete-step-icon');
        const status = substage === 'merge'
            ? mergeCheckpoint.status
            : (checkpoints[substage] || {}).status || 'pending';
        const visual = _completeStepVisual(status);

        li.classList.remove('wizard-complete-step-done', 'wizard-complete-step-running', 'wizard-complete-step-failed');
        if (visual.cls) li.classList.add(visual.cls);

        if (iconWrap) {
            iconWrap.innerHTML = status === 'running'
                ? '<span class="spinner-border spinner-border-sm text-primary" role="status"></span>'
                : `<i class="bi ${visual.icon}" aria-hidden="true"></i>`;
        }
    });

    const retryBtn = document.getElementById('wizardCompleteRetryBtn');
    const summaryEl = document.getElementById('wizardCompleteSummary');
    const anyFailed = WIZARD_COMPLETE_SUBSTAGES.some(function (s) {
        const status = s === 'merge' ? mergeCheckpoint.status : (checkpoints[s] || {}).status;
        return status === 'failed';
    });

    if (retryBtn) retryBtn.classList.toggle('ob-hidden', !anyFailed);

    if (draft.stage === 'done' && mergeCheckpoint.status === 'done' && summaryEl) {
        const stats = mergeCheckpoint.result || {};
        summaryEl.classList.remove('ob-hidden');
        summaryEl.innerHTML =
            '<div class="alert alert-success mb-0">' +
            '<i class="bi bi-check-circle-fill me-2"></i>' +
            `Merged ${stats.classes_added || 0} classes, ${stats.relations_added || 0} relations, ` +
            `${stats.attributes_added || 0} attributes, ${stats.axioms_added || 0} axioms into your ontology.` +
            '</div>';
    } else if (summaryEl) {
        summaryEl.classList.add('ob-hidden');
        summaryEl.innerHTML = '';
    }
}

/** Update the checklist live from an in-flight completion task's steps
 * (task.steps mirrors the substage order, so we translate its status). */
function renderCompleteChecklistFromTaskSteps(task) {
    if (!task || !Array.isArray(task.steps)) return;
    task.steps.forEach(function (step) {
        const li = document.querySelector('[data-substage="' + step.name + '"]');
        if (!li) return;
        const iconWrap = li.querySelector('.wizard-complete-step-icon');
        const visual = _completeStepVisual(
            step.status === 'completed' ? 'done' : step.status
        );
        li.classList.remove('wizard-complete-step-done', 'wizard-complete-step-running', 'wizard-complete-step-failed');
        if (visual.cls) li.classList.add(visual.cls);
        if (iconWrap) {
            iconWrap.innerHTML = step.status === 'running'
                ? '<span class="spinner-border spinner-border-sm text-primary" role="status"></span>'
                : `<i class="bi ${visual.icon}" aria-hidden="true"></i>`;
        }
    });
}

/**
 * Start (or resume) Stage 3 completion. Called from the Review pane's
 * "Continue to Complete" button (via `window.WizardCore.startCompletion`)
 * and from the Complete pane's Retry button.
 */
async function startGenerateCompletion() {
    setWizardStage('complete');
    resetCompleteChecklistToPending();

    try {
        const response = await fetch('/ontology/wizard/generate/complete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ options: {} }),
            credentials: 'same-origin',
        });
        const startResult = await response.json();

        if (!startResult.success) {
            showNotification('Error: ' + startResult.message, 'error');
            return;
        }

        const taskId = startResult.task_id;
        console.log('[Wizard] Completion task started:', taskId);

        sessionStorage.setItem(WIZARD_COMPLETE_TASK_KEY, taskId);
        wizardCurrentTaskId = taskId;

        disableWizardForm(true);
        _clearWizardProgressPanels();

        if (typeof refreshTasks === 'function') {
            refreshTasks();
        }

        showNotification('Completing ontology Generate. You can navigate away and come back.', 'info');

        monitorWizardTask(taskId, 'complete');
    } catch (error) {
        console.error('[Wizard] Completion error:', error);
        showNotification('Error starting completion: ' + error.message, 'error');
    }
}

/** Retry a partially-failed completion — resumes at the first incomplete
 * substage (the server never re-runs an already-`done` checkpoint), so
 * this is just re-issuing the same completion call. */
function retryGenerateCompletion() {
    startGenerateCompletion();
}

function resetCompleteChecklistToPending() {
    WIZARD_COMPLETE_SUBSTAGES.forEach(function (substage) {
        const li = document.querySelector('[data-substage="' + substage + '"]');
        if (!li || li.classList.contains('wizard-complete-step-done')) return;
        li.classList.remove('wizard-complete-step-failed');
        const iconWrap = li.querySelector('.wizard-complete-step-icon');
        if (iconWrap) iconWrap.innerHTML = '<i class="bi bi-circle text-muted" aria-hidden="true"></i>';
    });
    const retryBtn = document.getElementById('wizardCompleteRetryBtn');
    if (retryBtn) retryBtn.classList.add('ob-hidden');
}

/**
 * Handle a successful completion task: refresh the ontology, render the
 * final draft's checklist/summary, and navigate to the Map view — mirrors
 * today's one-shot wizard's success behavior.
 */
async function handleCompletionSuccess(result) {
    const draft = result.draft || {};
    renderCompleteChecklist(draft);

    if (typeof refreshOntologyStatus === 'function') {
        refreshOntologyStatus();
    }
    if (typeof loadOntologyFromSession === 'function') {
        await loadOntologyFromSession();
    }
    if (typeof SidebarNav !== 'undefined' && typeof SidebarNav.switchTo === 'function') {
        SidebarNav.switchTo('map');
    }

    const stats = result.merge || {};
    showNotification(
        `Ontology updated — +${stats.classes_added || 0} classes, ` +
        `+${stats.relations_added || 0} relations, +${stats.attributes_added || 0} attributes, ` +
        `+${stats.axioms_added || 0} axioms.`,
        'success'
    );
}

// =====================================================
// WIZARD CORE — cross-module bridge for ontology-wizard-review.js
// =====================================================

window.WizardCore = {
    /** Stage 2 "Continue to Complete" -> Stage 3. */
    startCompletion: startGenerateCompletion,
    /** Stale-draft banner's "Re-detect" -> re-run Stage 1 (no confirm — a
     * stale draft has nothing valid left to lose). */
    redetect: runGenerateDetection,
    /** After a successful discard, return to the Configure pane. */
    onDraftDiscarded: function () {
        setWizardStage('configure');
    },
};

// =====================================================
// DOCUMENTS SELECTION
// =====================================================

/**
 * Load documents from the domain volume.
 */
async function loadWizardDocuments() {
    const statusEl = document.getElementById('wizardDocsStatus');
    const previewEl = document.getElementById('wizardDocsPreview');
    const noDocsEl = document.getElementById('wizardNoDocs');

    try {
        const response = await fetch('/domain/documents/list', { credentials: 'same-origin' });
        const result = await response.json();

        if (result.success && result.files && result.files.length > 0) {
            wizardDocsCache = result.files;
            wizardSelectedDocs.clear();
            // Select ready documents by default; unavailable files cannot be evidence.
            wizardDocsCache
                .filter(file => file.parse_status === 'ready')
                .forEach(file => wizardSelectedDocs.add(file.name));
            const readyCount = wizardDocsCache.filter(
                file => file.parse_status === 'ready'
            ).length;

            statusEl.innerHTML = `
                <div class="d-flex align-items-center">
                    <i class="bi bi-check-circle-fill text-success me-2 fs-5"></i>
                    <div><strong>${readyCount} of ${wizardDocsCache.length} documents</strong> ready</div>
                </div>`;
            renderWizardDocsList();
            previewEl.style.display = '';
            if (noDocsEl) noDocsEl.style.display = 'none';
        } else {
            statusEl.innerHTML = '';
            if (previewEl) previewEl.style.display = 'none';
            if (noDocsEl) noDocsEl.style.display = '';
        }
    } catch (err) {
        console.warn('[Wizard] Could not load documents:', err);
        statusEl.innerHTML = '';
        if (previewEl) previewEl.style.display = 'none';
        if (noDocsEl) noDocsEl.style.display = '';
    }
}

function renderWizardDocsList() {
    const container = document.getElementById('wizardDocsList');
    if (!container) return;

    const iconMap = {
        pdf: 'bi-file-earmark-pdf text-danger',
        doc: 'bi-file-earmark-word text-primary',
        docx: 'bi-file-earmark-word text-primary',
        xls: 'bi-file-earmark-excel text-success',
        xlsx: 'bi-file-earmark-excel text-success',
        csv: 'bi-file-earmark-spreadsheet text-success',
        txt: 'bi-file-earmark-text text-secondary',
        json: 'bi-file-earmark-code text-warning',
        xml: 'bi-file-earmark-code text-warning',
    };

    let html = '<div class="list-group list-group-flush">';
    wizardDocsCache.forEach((file, idx) => {
        const ext = (file.name.split('.').pop() || '').toLowerCase();
        const iconCls = iconMap[ext] || 'bi-file-earmark text-muted';
        const checked = wizardSelectedDocs.has(file.name) ? 'checked' : '';
        const isReady = file.parse_status === 'ready';
        const disabled = isReady ? '' : 'disabled';
        const unavailableIcon = file.parse_status === 'pending' ? 'bi-hourglass-split' : 'bi-exclamation-triangle';
        const statusLabel = isReady
            ? '<span class="badge text-bg-success ms-2"><i class="bi bi-check-circle me-1"></i>Ready</span>'
            : `<span class="badge ${file.parse_status === 'pending' ? 'text-bg-warning' : 'text-bg-danger'} ms-2">
                   <i class="bi ${unavailableIcon} me-1"></i>${file.parse_status === 'pending' ? 'Parsing' : 'Parse failed'}
               </span>`;
        const size = file.size != null ? formatDocSize(file.size) : '';
        const docNameAttr = encodeURIComponent(file.name);
        html += `
            <div class="list-group-item list-group-item-action d-flex align-items-center py-2">
                <input type="checkbox" class="form-check-input me-3 wizard-doc-checkbox"
                       value="${file.name}" ${checked} ${disabled}>
                <i class="bi ${iconCls} me-2"></i>
                <span class="text-truncate flex-grow-1">${file.name}</span>
                ${size ? `<span class="small text-muted ms-2">${size}</span>` : ''}
                ${statusLabel}
                <button class="btn btn-sm btn-outline-primary py-0 px-1 ms-2" type="button"
                        data-action="wizard-doc-preview" data-doc-name="${docNameAttr}" title="Preview"
                        ${disabled}>
                    <i class="bi bi-eye"></i>
                </button>
            </div>`;
    });
    html += '</div>';
    container.innerHTML = html;
    updateWizardDocsCount();
}

function updateWizardDocSelection(checkbox) {
    if (checkbox.checked) {
        wizardSelectedDocs.add(checkbox.value);
    } else {
        wizardSelectedDocs.delete(checkbox.value);
    }
    updateWizardDocsCount();
}

function selectAllWizardDocs(selectAll) {
    wizardSelectedDocs.clear();
    if (selectAll) {
        wizardDocsCache
            .filter(file => file.parse_status === 'ready')
            .forEach(file => wizardSelectedDocs.add(file.name));
    }
    document.querySelectorAll('.wizard-doc-checkbox').forEach(cb => {
        cb.checked = selectAll && !cb.disabled;
    });
    updateWizardDocsCount();
}

function updateWizardDocsCount() {
    const el = document.getElementById('wizardDocsSelectedCount');
    if (!el) return;
    const total = wizardDocsCache.length;
    const selected = wizardSelectedDocs.size;
    el.textContent = `${selected} of ${total} document${total !== 1 ? 's' : ''} selected`;
}

function getSelectedDocumentNames() {
    return getSelectedDocumentFiles()
        .filter(file => file.parse_status === 'ready')
        .map(file => file.name);
}

function getSelectedDocumentFiles() {
    return wizardDocsCache.filter(file => wizardSelectedDocs.has(file.name));
}

function formatDocSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// =====================================================
// AGENT STEPS LOG
// =====================================================

function renderAgentStepsLog(steps) {
    if (typeof TaskProgressUI !== 'undefined') {
        return TaskProgressUI.renderAgentStepsLogHtml(steps);
    }
    return '';
}

function _truncate(str, max) { return truncate(str, max); }

// =====================================================
// EXPOSE GLOBALLY
// =====================================================

window.initOntologyWizard = initOntologyWizard;
window.loadWizardTemplate = loadWizardTemplate;
window.startGenerateDetection = startGenerateDetection;
window.retryGenerateCompletion = retryGenerateCompletion;
window.updateWizardTableSelection = updateWizardTableSelection;
window.selectAllWizardTables = selectAllWizardTables;
window.updateWizardDocSelection = updateWizardDocSelection;
window.selectAllWizardDocs = selectAllWizardDocs;

/**
 * Wizard UI: data-action click delegation and checkbox change handling (no inline handlers).
 */
(function initWizardActionDelegation() {
    function bind() {
        const root = document.getElementById('wizard-section');
        if (!root || root.dataset.wizardActionsBound === '1') return;
        root.dataset.wizardActionsBound = '1';

        root.addEventListener('click', function (e) {
            const el = e.target.closest('[data-action]');
            if (!el || !root.contains(el)) return;
            const action = el.dataset.action;
            switch (action) {
                case 'wizard-generate':
                    startGenerateDetection();
                    break;
                case 'wizard-tables-bulk':
                    selectAllWizardTables(el.dataset.selectAll === 'true');
                    break;
                case 'wizard-docs-bulk':
                    selectAllWizardDocs(el.dataset.selectAll === 'true');
                    break;
                case 'wizard-complete-retry':
                    retryGenerateCompletion();
                    break;
                case 'wizard-doc-preview': {
                    e.stopPropagation();
                    const raw = el.getAttribute('data-doc-name') || '';
                    let name = '';
                    try {
                        name = decodeURIComponent(raw);
                    } catch (err) {
                        name = raw;
                    }
                    if (name && typeof DocumentPreview !== 'undefined' && DocumentPreview.open) {
                        DocumentPreview.open(name);
                    }
                    break;
                }
                default:
                    break;
            }
        });

        root.addEventListener('change', function (e) {
            const t = e.target;
            if (t.id === 'wizardSelectAllCheckbox') {
                selectAllWizardTables(!!t.checked);
                return;
            }
            if (t.classList && t.classList.contains('wizard-table-checkbox')) {
                updateWizardTableSelection(t);
                return;
            }
            if (t.classList && t.classList.contains('wizard-doc-checkbox')) {
                updateWizardDocSelection(t);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bind);
    } else {
        bind();
    }
})();

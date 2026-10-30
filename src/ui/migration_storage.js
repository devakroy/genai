/**
 * Migration Storage Manager
 * Handles loading migration JSON files and saving to browser local storage
 * Provides utility functions to retrieve migration data and steps by module
 */

console.log('📦 loading migration_storage.js');

/**
 * Get today's date in YYYY-MM-DD format
 */
function getTodayDateString() {
    return new Date().toISOString().split('T')[0];
}

/**
 * Fetch the latest migration CSV file from server and save to browser local storage
 */
async function loadMigrationFileToLocalStorage() {
    try {
        // Construct URL to fetch the single migration CSV file
        // Path: /target/steps_migration/steps_migration.csv
        const migrationFileUrl = `/target/steps_migration/steps_migration.csv`;
        
        console.log('📦 Attempting to load migration CSV file:', migrationFileUrl);
        
        const response = await fetch(migrationFileUrl);
        
        if (!response.ok) {
            console.warn('⚠ Migration CSV file not found. Trigger export: POST /api/migration/export');
            console.log('💡 Tip: Call fetch("/api/migration/export", {method: "POST"}) in console to generate migration data');
            return;
        }
        
        const csvContent = await response.text();
        console.log(`✓ Loaded migration CSV file`);
        parseAndSaveCSV(csvContent);
        
        // Store hash for future update checks
        localStorage.setItem('_migrationFileHash', hashString(csvContent));
        
        // Start periodic check for updates (every 60 minutes)
        startPeriodicUpdateCheck(60);
    } catch (error) {
        console.error('❌ Error loading migration file to local storage:', error);
    }
}

/**
 * Parse CSV content and save to local storage
 */
function parseAndSaveCSV(csvContent) {
    try {
        const lines = csvContent.trim().split('\n');
        if (lines.length < 1) {
            console.warn('⚠ CSV file is empty');
            return;
        }
        
        // Parse header
        const headerLine = lines[0];
        const headers = parseCSVLine(headerLine);
        
        if (!headers.includes('modulenm') || !headers.includes('step')) {
            console.warn('⚠ CSV missing required columns: modulenm, step');
            return;
        }
        
        // Parse data rows
        const migrationData = {};
        let totalRecords = 0;
        
        for (let i = 1; i < lines.length; i++) {
            const line = lines[i].trim();
            if (!line) continue;
            
            const values = parseCSVLine(line);
            const record = {};
            
            headers.forEach((header, index) => {
                record[header] = values[index] || '';
            });
            
            const moduleName = record['modulenm'];
            const stepText = record['step'];
            
            if (moduleName && stepText) {
                if (!migrationData[moduleName]) {
                    migrationData[moduleName] = [];
                }
                migrationData[moduleName].push(record);
                totalRecords++;
            }
        }
        
        // Save to local storage
        saveMigrationDataToLocalStorage({
            migration_data: migrationData,
            total_records: totalRecords
        });
        
        console.log(`✓ Parsed and saved ${totalRecords} steps from CSV to local storage`);
    } catch (error) {
        console.error('❌ Error parsing CSV:', error);
    }
}

/**
 * Parse a single CSV line (handles quoted values)
 */
function parseCSVLine(line) {
    const result = [];
    let current = '';
    let insideQuotes = false;
    
    for (let i = 0; i < line.length; i++) {
        const char = line[i];
        const nextChar = line[i + 1];
        
        if (char === '"') {
            if (insideQuotes && nextChar === '"') {
                // Escaped quote
                current += '"';
                i++;
            } else {
                // Toggle quote mode
                insideQuotes = !insideQuotes;
            }
        } else if (char === ',' && !insideQuotes) {
            // Field separator
            result.push(current);
            current = '';
        } else {
            current += char;
        }
    }
    
    // Add last field
    result.push(current);
    
    return result.map(field => field.trim());
}

/**
 * Save migration data to browser local storage
 * @param {Object} migrationData - The parsed migration JSON object
 */
function saveMigrationDataToLocalStorage(migrationData) {
    try {
        // migrationData has structure: { migration_data: { moduleName: [...] }, total_records: number }
        if (!migrationData || !migrationData.migration_data) {
            console.warn('Invalid migration data structure');
            return;
        }

        // Store the migration data indexed by module (already organized from CSV parsing)
        localStorage.setItem('stepsIndexByModule', JSON.stringify(migrationData.migration_data));
        
        // Store metadata
        const metadata = {
            total_records: migrationData.total_records || 0,
            loaded_at: new Date().toISOString(),
            format: 'csv',
        };
        localStorage.setItem('migrationMetadata', JSON.stringify(metadata));
        
        console.log(`✓ Saved ${migrationData.total_records} steps to local storage`);
        console.log('Modules available:', Object.keys(migrationData.migration_data).sort());
    } catch (error) {
        console.error('Error saving migration data to local storage:', error);
        // Handle quota exceeded or other storage errors
        if (error.name === 'QuotaExceededError') {
            console.warn('Local storage quota exceeded, clearing old data');
            try {
                localStorage.removeItem('migrationData');
                localStorage.removeItem('stepsIndexByModule');
                localStorage.removeItem('migrationMetadata');
                // Retry
                localStorage.setItem('migrationData', JSON.stringify(migrationData));
            } catch (retryError) {
                console.error('Still cannot save after clearing storage:', retryError);
            }
        }
    }
}

/**
 * Retrieve migration data from local storage
 * @returns {Object|null} The migration data object or null if not found
 */
function getMigrationDataFromLocalStorage() {
    try {
        const data = localStorage.getItem('migrationData');
        return data ? JSON.parse(data) : null;
    } catch (error) {
        console.error('Error retrieving migration data from local storage:', error);
        return null;
    }
}

/**
 * Get steps for a specific module from local storage
 * @param {string} moduleName - The module/domain name
 * @returns {Array} Array of step objects for the module
 */
function getStepsForModuleFromLocalStorage(moduleName) {
    try {
        const indexData = localStorage.getItem('stepsIndexByModule');
        if (!indexData) return [];
        
        const stepsByModule = JSON.parse(indexData);
        return stepsByModule[moduleName] || [];
    } catch (error) {
        console.error('Error retrieving steps for module from local storage:', error);
        return [];
    }
}

/**
 * Get migration metadata from local storage
 * @returns {Object|null} The metadata object or null if not found
 */
function getMigrationMetadataFromLocalStorage() {
    try {
        const metadata = localStorage.getItem('migrationMetadata');
        return metadata ? JSON.parse(metadata) : null;
    } catch (error) {
        console.error('Error retrieving migration metadata from local storage:', error);
        return null;
    }
}

/**
 * Clear all migration data from local storage
 */
function clearMigrationDataFromLocalStorage() {
    try {
        localStorage.removeItem('migrationData');
        localStorage.removeItem('stepsIndexByModule');
        localStorage.removeItem('migrationMetadata');
        console.log('✓ Cleared all migration data from local storage');
    } catch (error) {
        console.error('Error clearing migration data from local storage:', error);
    }
}

/**
 * Get all available modules from local storage
 * @returns {Array} Array of module names sorted alphabetically
 */
function getAvailableModulesFromLocalStorage() {
    try {
        const indexData = localStorage.getItem('stepsIndexByModule');
        if (!indexData) return [];
        
        const stepsByModule = JSON.parse(indexData);
        return Object.keys(stepsByModule).sort();
    } catch (error) {
        console.error('Error retrieving modules from local storage:', error);
        return [];
    }
}

/**
 * Check if CSV file has been updated on the server
 * Compares with stored version using hash
 */
async function checkForMigrationUpdates() {
    try {
        const migrationFileUrl = `/target/steps_migration/steps_migration.csv`;
        console.log('🔄 Checking for migration file updates...');
        
        const response = await fetch(migrationFileUrl);
        if (!response.ok) {
            console.warn('⚠ Migration CSV file not accessible for update check');
            return false;
        }
        
        const csvContent = await response.text();
        
        // Get stored version's hash
        const currentHash = localStorage.getItem('_migrationFileHash');
        
        // Simple hash function for comparison
        const newHash = hashString(csvContent);
        
        if (currentHash === newHash) {
            console.log('✓ Migration file is up-to-date');
            return false;
        }
        
        console.log('🔄 Migration file has been updated! Reloading to localStorage...');
        
        // File has changed, parse and save to localStorage
        parseAndSaveCSV(csvContent);
        
        // Store new hash
        localStorage.setItem('_migrationFileHash', newHash);
        
        console.log('✓ LocalStorage updated with new migration data');
        return true;
    } catch (error) {
        console.error('❌ Error checking for migration updates:', error);
        return false;
    }
}

/**
 * Simple string hash function for comparing CSV files
 */
function hashString(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash = hash & hash;
    }
    return hash.toString(36);
}

/**
 * Start periodic check for migration file updates
 * Checks every X minutes without page reload
 */
function startPeriodicUpdateCheck(intervalMinutes = 60) {
    console.log(`⏰ Starting periodic migration update check (every ${intervalMinutes} minutes)`);
    
    // Check immediately on startup
    checkForMigrationUpdates();
    
    // Then check periodically
    window._migrationUpdateCheckInterval = setInterval(() => {
        checkForMigrationUpdates();
    }, intervalMinutes * 60 * 1000);
}

/**
 * Stop periodic update check
 */
function stopPeriodicUpdateCheck() {
    if (window._migrationUpdateCheckInterval) {
        clearInterval(window._migrationUpdateCheckInterval);
        console.log('⏹ Periodic migration update check stopped');
    }
}

/**
 * Get statistics about stored migration data
 * @returns {Object} Stats including total records, modules count, storage size estimate
 */
function getMigrationStorageStats() {
    try {
        const migrationData = getMigrationDataFromLocalStorage();
        const metadata = getMigrationMetadataFromLocalStorage();
        const modules = getAvailableModulesFromLocalStorage();
        
        const stats = {
            total_records: migrationData ? migrationData.steps.length : 0,
            modules_count: modules.length,
            modules: modules,
            migration_date: metadata ? metadata.migration_date : null,
            export_timestamp: metadata ? metadata.export_timestamp : null,
            loaded_at: metadata ? metadata.loaded_at : null,
        };
        
        return stats;
    } catch (error) {
        console.error('Error retrieving migration storage stats:', error);
        return null;
    }
}

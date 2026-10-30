import os.path
import json
import logging
from services.llm_layer.LLM import LLM
from services.ingestion_layer.s3_handler.s3_handler import S3FileReader
import uuid
from utils.functions import delete_files
from definitions import REVIEW_PLAYWRIGHT_SCRIPTS_DIR

# Initialize logger
logger = logging.getLogger(__name__)
script = None


def _df_is_ui_only(df_inter) -> bool:
    """True when steps are UI-only (no API/auth/mock/checkout-service patterns in DataFrame)."""
    if df_inter is None:
        return True
    try:
        n = len(df_inter)
    except TypeError:
        return True
    if n == 0:
        return True
    try:
        if "original_bdd_step" not in df_inter.columns:
            return True
        steps = df_inter["original_bdd_step"].astype(str).str.lower()
    except Exception:
        return True
    api_auth_tokens = (
        "invoke ",
        "invokes ",
        "stores response",
        "store response",
        "bearer",
        "add header",
        "modify request",
        "user mocks",
        "mock ",
        "authentication service",
        "extract cookie",
        "post.api",
        "get.api",
        "makes a get",
        "makes a post",
        "make get request",
        "make post request",
    )
    for tok in api_auth_tokens:
        try:
            if steps.str.contains(tok, regex=False, na=False).any():
                return False
        except Exception:
            continue
    try:
        if "action" in df_inter.columns:
            act = df_inter["action"].astype(str).str.lower()
            if act.str.contains(
                r"^invoke|^mock|post\.api|get\.api|store_response|add_header|modify_request",
                regex=True,
                na=False,
            ).any():
                return False
    except Exception:
        pass
    return True


def process_typescript_code(code):
    lines = code.split('\n')
    result_lines = []

    for line in lines:
        original_line = line
        # Look for lines that have test() function calls
        if 'test(' in line:
            # Find where the test text starts and ends
            start_idx = line.find("('") + 2
            end_idx = line.find("'", start_idx)

            if start_idx != -1 and end_idx != -1:
                test_text = line[start_idx:end_idx]

                # Only modify if it contains special characters
                if any(char in test_text for char in ['/']):
                    modified_text = ""
                    i = 0
                    while i < len(test_text):
                        # Skip already escaped characters
                        if test_text[i] == '\\' and i < len(test_text) - 1 and test_text[i + 1] in ['/']:
                            modified_text += test_text[i:i + 2]
                            i += 2
                        # Escape special characters with double backslashes
                        elif test_text[i] in ['/']:
                            modified_text += '\\\\' + test_text[i]
                            i += 1
                        # Keep other characters as is
                        else:
                            modified_text += test_text[i]
                            i += 1

                    # Replace only the exact test text portion
                    prefix = line[:start_idx]
                    suffix = line[end_idx:]
                    line = prefix + modified_text + suffix

        result_lines.append(line)

    return '\n'.join(result_lines)


class StepDefGenerationPlaywright:
    def __init__(self):
        self.s3_reader = S3FileReader()

    def generate_playwright_test(self, data, df, df_inter, i, flag, jira_id, current_script="", datasets=None):
        def _enforce_datasets(script_text: str, datasets_json: str) -> str:
            """
            Ensure the generated script contains the exact datasets content.
            If a datasets declaration exists, replace its array content.
            Otherwise, insert a declaration after imports.
            """
            try:
                if not datasets_json:
                    return script_text
                import re
                # Replace existing datasets array
                pattern = r"(const\s+datasets\s*=\s*)\[[\s\S]*?\](\s*;)"
                replacement = r"\1" + datasets_json + r"\2"
                if re.search(pattern, script_text):
                    return re.sub(pattern, replacement, script_text, count=1)
                # If not found, insert after imports
                insert_after = "import path from 'path';"
                insert_idx = script_text.find(insert_after)
                if insert_idx != -1:
                    insert_pos = insert_idx + len(insert_after)
                    return script_text[:insert_pos] + "\n\nconst datasets = " + datasets_json + ";\n" + script_text[insert_pos:]
                return "const datasets = " + datasets_json + ";\n" + script_text
            except Exception:
                return script_text
        

        def _enforce_login_block(script_text: str, multi_row_examples: bool) -> str:
            """
            When Examples datasets are present in non-BDD output, enforce unique per-run account creation
            by replacing the legacy 'Account service' + cookie section with a deterministic unique-email
            account creation and cookie-based session setup.
            """
            if not multi_row_examples:
                return script_text
            try:
                import re
                updated = script_text

                # 1) Ensure accountHeaders exists after NewUserAuthToken assignment
                if "const accountHeaders" not in updated:
                    token_assign_pattern = r"(storedValues\['NewUserAuthToken'\]\s*=\s*authResponse\.response_data\.access_token;\s*)"
                    headers_block = (
                        "\\1"
                        "                const accountHeaders = {\n"
                        "                  'Authorization': `Bearer ${storedValues['NewUserAuthToken']}`\n"
                        "                };\n\n"
                    )
                    updated = re.sub(token_assign_pattern, headers_block, updated)

                # 2) Prefer headers over auth_token inside accountConfig
                updated = re.sub(
                    r"auth_token:\s*storedValues\['NewUserAuthToken'\]",
                    "headers: accountHeaders",
                    updated
                )

                # 3) Inject unique email body override before account makeRequest
                if "(accountConfig as any).body" not in updated:
                    account_request_pattern = r"(const\s+accountResponse\s*=\s*await\s+apiService\.makeRequest\(\s*accountConfig\s*\)\s*;\s*)"
                    unique_email_block = (
                        "                const uniqueTimestamp = Date.now();\n"
                        "                const uniqueEmail = `playwright.test+${uniqueTimestamp}@example.com`;\n"
                        "                (accountConfig as any).body = { email: uniqueEmail, username: uniqueEmail };\n"
                        "                "
                    )
                    updated = re.sub(account_request_pattern, unique_email_block + "\\1", updated)

                # 4) Store accountResponse/createdEmail if not present
                if "storedValues['createdEmail']" not in updated:
                    updated = re.sub(
                        r"(storedValues\['accountResponse'\]\s*=\s*accountResponse\.response_data;\s*)",
                        "\\1                storedValues['createdEmail'] = uniqueEmail;\n",
                        updated
                    )

                # 5) Replace literal email fills for #username with uniqueEmail
                updated = re.sub(
                    r"(const\s+emailInput\s*=\s*page\.locator\('#username'\);\s*[\s\S]*?emailInput\.fill\()\s*(['\"][^'\"]+['\"])\s*(\)\s*;\s*)",
                    r"\1uniqueEmail\3",
                    updated
                )

                return updated
            except Exception:
                return script_text
        """
        Generates normal Playwright test format (without BDD step definitions) based on the provided input parameters and data.

        Args:
            data (str): The BDD feature file content.
            df (str): DataFrame containing the processed data.
            df_inter (pd.DataFrame): DataFrame containing intermediate data.
            i (int): Scenario index.
            flag (bool): Flag indicating if this is a duplicate scenario.
            jira_id (str): JIRA ID for the test.
        """
        global script
        paypal_code = None
        gift_card_code = None
        get_api_code = None

        valid_objects = ["Pay with PayPal", "PayPal", "PayPal Checkout", "Pay Later"]

        if df_inter['object'].isin(valid_objects).any() and \
                df_inter['action'].astype(str).str.contains("click", case=True, regex=True).any():
            paypal_code = """
              IMPORTANT: Use below code as it is for Pay with PayPal or  PayPal or PayPal Checkout or PayPal PayLater object and click action in its corresponding step definition function.
              await page.waitForLoadState('domcontentloaded');
              await page.waitForTimeout(8000);
              const buttonName = "Pay with PayPal";
              const creds = {{ email: 'ragzmong@gmail.com', password: 'welcome123' }};

              const name = buttonName.trim().toLowerCase();
              const isCheckout = /checkout/.test(name);
              const isLater = /later/.test(name);
              const isPlainPay = name === 'paypal';
              const isPayWith = !isCheckout && !isLater && !isPlainPay;

              const LABELS = isCheckout
                ? ['PayPal Checkout', 'Checkout with PayPal']
                : isLater
                ? ['Pay Later', 'PayPal Pay Later', 'Pay in 4', 'Pay monthly']
                : isPayWith
                ? ['Pay with PayPal']
                : ['PayPal'];

              await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
              await page.waitForTimeout(2000);

              const iframeSelector = 'iframe[title^="PayPal"], iframe[name*="__zoid__paypal_buttons__"]';
              await page.waitForSelector(iframeSelector, {{ state: 'visible', timeout: 30_000 }});
              const iframeLocator = page.locator(iframeSelector);

              const iframeCount = await iframeLocator.count();
              if (iframeCount === 0) throw new Error(`No PayPal iframes found for "${{buttonName}}".`);

              let clicked = false;
              for (let i = 0; i < iframeCount && !clicked; i++) {{
                const oneIframe = iframeLocator.nth(i);
                const handle = await oneIframe.elementHandle();
                if (!handle) continue;
                const frame = await handle.contentFrame();
                if (!frame) continue;

                const labelSelectors = LABELS.flatMap(l => [
                  `div[role="link"][aria-label="${{l}}"]`,
                  `div[role="button"][aria-label="${{l}}"]`,
                  `[aria-label="${{l}}"]`,
                ]);
                const candidate = frame.locator(labelSelectors.join(', ')).first();

                const safeCandidate = isLater
                  ? candidate
                  : candidate.filter({{ hasNotText: /later|installments|pay\s*in\s*\d|monthly|over\s*time/i }});

                try {{
                  await safeCandidate.waitFor({{ state: 'visible', timeout: 8000 }});
                  console.log(`Found PayPal button in iframe ${{i}}`);
                }} catch {{
                  console.log(`No PayPal button found in iframe ${{i}}`);
                  continue;
                }}

                const [popup] = await Promise.all([
                  page.waitForEvent('popup', {{ timeout: 30_000 }}),
                  safeCandidate.click({{ force: true }}),
                ]);

                await popup.waitForLoadState('domcontentloaded', {{ timeout: 15_000 }});

                const emailField = popup.locator('#email').or(popup.locator('#login_email'));
                await emailField.waitFor({{ state: 'visible', timeout: 20_000 }});
                await emailField.fill(creds.email);

                const nextBtn = popup.locator('#btnNext').or(popup.locator('button:text-is("Next")'));
                if (await nextBtn.isVisible()) await nextBtn.click();

                const usePwd = popup.locator('button:has-text("Use Password Instead")');
                if (await usePwd.isVisible()) {{
                  await usePwd.click();
                  await popup.waitForTimeout(500);
                }}

                const pwd = popup.locator('#password');
                await pwd.waitFor({{ state: 'visible', timeout: 15_000 }});
                await pwd.fill(creds.password);

                const loginBtn = popup
                  .locator('#btnLogin')
                  .or(popup.locator('button:text-is("Log In")[data-atomic-wait-viewname="password"]'));
                await loginBtn.click();

                await popup.waitForLoadState('domcontentloaded', {{ timeout: 20_000 }});

                const consent = popup
                  .locator('#consentButton')
                  .or(popup.locator('button[data-atomic-wait-task="select_agree_and_continue"]'))
                  .or(popup.getByRole('button', {{ name: /agree|continue|save/i }}))
                  .first();
                if (await consent.isVisible()) {{
                  await consent.click().catch(() => {{}});
                }}

                await popup.waitForEvent('close', {{ timeout: 20_000 }});
                clicked = true;
              }}

              if (!clicked) {{
                throw new Error(
                  `Could not find a visible "${{buttonName}}" button inside any PayPal iframe. Tried labels: ${{LABELS.join(', ')}}`
                );
              }}
            """

        # Cookie setting detection
        cookie_code = None
        if df_inter['action'].astype(str).str.contains("create.*new.*user.*account.*cookie|creates.*new.*user.*account.*sessionResponse.*cookie|logs.*in.*with.*\.json.*cookie|login.*with.*\.json", case=False, regex=True).any():
            cookie_code = """
            IMPORTANT: Use this exact code for authentication cookies (handles both sessionResponse and JSON file):
            
            // Load cookies from JSON file (merchOps.json)
            const fs = require('fs');
            const path = require('path');
            
            const jsonFileName = 'merchOps.json';
            const cookieFilePath = path.join(process.cwd(), 'target', 'json', jsonFileName);
            
            if (fs.existsSync(cookieFilePath)) {
              const cookieData = JSON.parse(fs.readFileSync(cookieFilePath, 'utf8'));
              
              // Set cookies from the JSON file
              if (cookieData.cookies && Array.isArray(cookieData.cookies)) {
                await page.context().addCookies(cookieData.cookies);
                console.log(`Loaded ${cookieData.cookies.length} cookies from ${jsonFileName}`);
              }
              
              // Set localStorage if present
              if (cookieData.origins && Array.isArray(cookieData.origins)) {
                for (const origin of cookieData.origins) {
                  if (origin.localStorage && Array.isArray(origin.localStorage)) {
                    await page.goto(origin.origin);
                    for (const item of origin.localStorage) {
                      await page.evaluate(([key, value]) => {
                        localStorage.setItem(key, value);
                      }, [item.name, item.value]);
                    }
                  }
                }
              }
              
              console.log('JSON file authentication completed successfully');
            } else {
              console.log('Cookie file not found, skipping cookie authentication');
            }
            """

        # POST API custom code generation with AccountService and ApiService integration
        post_api_code = None
        if df_inter['action'].astype(str).str.contains("POST|posts|API|request|response|verifies.*status|stores.*response|auth.*token|makes.*request|ADD-HEADER|ADD-REQUESTBODY|create.*account|session", case=False, regex=True).any():
            post_api_code = """
            IMPORTANT: For POST API steps, use these exact patterns with proper execution timing:
            
            CRITICAL EXECUTION PATTERNS:
            
            // Initialize services
            const accountService = new AccountService();
            const apiService = new ApiService();
            let storedValues: {{ [key: string]: any }} = {};
            
            // Authentication service call
            const authConfig = {{ body_from_file: path.join(process.cwd(), 'src/tests/data', 'newuser_auth_config.json') }};
            const authResponse = await apiService.makeRequest(authConfig);
            storedValues['NewUserAuthToken'] = authResponse.response_data.access_token;
            
            // Account creation using AccountService
            const accountConfig = {{
              body_from_file: path.join(process.cwd(), 'src/tests/data', 'newuser_account_config.json'),
              auth_token: storedValues['NewUserAuthToken']
            }};
            const accountResponse = await apiService.makeRequest(accountConfig);
            storedValues['accountResponse'] = accountResponse.response_data;
            
            // Account service call
            const accountConfig = {{
              body_from_file: path.join(process.cwd(), 'src/tests/data', 'newuser_account_config.json'),
              auth_token: storedValues['NewUserAuthToken']
            }};
            const accountResponse = await apiService.makeRequest(accountConfig);
            storedValues['accountResponse'] = accountResponse.response_data;
            
            // Session service call
            const sessionConfig = {{
              body_from_file: path.join(process.cwd(), 'src/tests/data', 'newuser_session_config.json'),
              auth_token: storedValues['NewUserAuthToken'],
              body: {{ loginId: storedValues['accountResponse'].loginId }}
            }};
            const sessionResponse = await apiService.makeRequest(sessionConfig);
            storedValues['sessionResponse'] = sessionResponse.response_data;
            """

        # GET API custom code generation with ApiService integration
        if df_inter['action'].astype(str).str.contains("GET.API|GET-API|VALIDATE-STATUS|VALIDATE-CONTENT|VALIDATE-TIME|VALIDATE-HEADER|VALIDATE-BODY|STORE-RESPONSE", case=False, regex=True).any():
            get_api_code = """
            IMPORTANT: Use ApiService for GET API operations:
            
            // Helper function for nested value extraction
            function getNestedValue(obj, path) {
              return path.split('.').reduce((current, key) => {
                if (key.includes('[') && key.includes(']')) {
                  const arrayKey = key.substring(0, key.indexOf('['));
                  const index = parseInt(key.substring(key.indexOf('[') + 1, key.indexOf(']')));
                  return current?.[arrayKey]?.[index];
                }
                return current?.[key];
              }, obj);
            }
            
            // GET API using ApiService
            let getApiResponse;
            
            if (url) {
              const getConfig = {
                url: url,
                method: 'GET',
                headers: customHeaders || {},
                auth_token: authToken,
                timeout: timeout || 30000
              };
              
              console.log(`Making GET request to: ${url}`);
              const startTime = Date.now();
              
              getApiResponse = await apiService.makeRequest(getConfig);
              const responseTime = Date.now() - startTime;
              
              console.log(`GET request completed - Status: ${getApiResponse.status_code}, Time: ${responseTime}ms`);
              console.log(`Response data:`, getApiResponse.response_data);
              
              // Add response time to response object
              getApiResponse.responseTime = responseTime;
            }
            
            // Status validation
            if (expectedStatus && getApiResponse) {
              expect(getApiResponse.status_code).toBe(expectedStatus);
              console.log(`Status validation passed: ${getApiResponse.status_code}`);
            }
            
            // Response time validation
            if (maxResponseTime && getApiResponse) {
              expect(getApiResponse.responseTime).toBeLessThan(maxResponseTime);
              console.log(`Response time validation passed: ${getApiResponse.responseTime}ms < ${maxResponseTime}ms`);
            }
            
            // Store entire response
            if (storedName && getApiResponse) {
              storedValues[storedName] = getApiResponse.response_data;
              console.log(`Stored entire response as ${storedName}`);
            }
            
            // Store specific field from response
            if (fieldPath && storedName && getApiResponse) {
              const fieldValue = getNestedValue(getApiResponse.response_data, fieldPath);
              storedValues[storedName] = fieldValue;
              console.log(`Stored field ${fieldPath} as ${storedName}: ${fieldValue}`);
            }
            
            // Field validation
            if (fieldToValidate && expectedValue && getApiResponse) {
              const actualValue = getNestedValue(getApiResponse.response_data, fieldToValidate);
              expect(actualValue).toBe(expectedValue);
              console.log(`Field validation passed: ${fieldToValidate} = ${actualValue}`);
            }
            """
        # Mock API custom code generation
        mock_api_code = None
        # Check for mock API steps in multiple ways
        has_mock_step = (df_inter['action'].astype(str).str.contains("MOCK-API|mock_setup|click", case=False, regex=True).any() or
                        df_inter['original_bdd_step'].astype(str).str.contains("MOCK.*button", case=False, regex=True).any())

        if has_mock_step:
            mock_api_code = """ 
            
            CRITICAL: For ANY step containing "MOCK" and "button", use this EXACT template:
            
            // User MOCKS "Ship to this address" button with mock file
            // Note: click() has built-in auto-waiting for visibility
            const mockApiService = new MockAPIService();
            if (!mockApiService.mockFileExists('mock_post_shiptothisaddress.json')) {
            throw new Error(`Mock file not found: mock_post_shiptothisaddress.json. Please ensure the file exists in src/tests/data/mock_responses/`);
            }
            await mockApiService.setupMock(page, 'mock_post_shiptothisaddress.json');
            const shipToAddressButton = page.getByTestId('submit-button').getByText('Ship to this address');
            await page.waitForLoadState('domcontentloaded');
            await page.waitForTimeout(3000);
            await shipToAddressButton.click();
            
            MANDATORY REQUIREMENTS:
            - MockAPIService import: import {{ MockAPIService }} from '../../services/mockapi.service';
            - Mock file location: src/tests/data/mock_responses/{{filename}}.json
            - Error handling: mockFileExists() check with throw Error
            - ALWAYS use setupMock() not mockResponse()
            - Service initialization: const mockApiService = new MockAPIService()
            """

        testdata = df_inter['data'].dropna().astype(str).str.cat(sep=',')

        # Prepare embedded datasets (if provided)
        datasets_literal = None
        if isinstance(datasets, list) and datasets:
            try:
                datasets_literal = json.dumps(datasets, ensure_ascii=False)
            except Exception:
                datasets_literal = None

        # Detect awssecretmanager tokens to conditionally inject env-only resolver (no AWS)
        has_awssecret = False
        try:
            if isinstance(df, str) and ('awssecretmanager.' in df):
                has_awssecret = True
            if df_inter is not None:
                if 'data' in df_inter.columns and df_inter['data'].astype(str).str.contains('awssecretmanager\\.', case=False, regex=True).any():
                    has_awssecret = True
                if 'original_bdd_step' in df_inter.columns and df_inter['original_bdd_step'].astype(str).str.contains('awssecretmanager\\.', case=False, regex=True).any():
                    has_awssecret = True
        except Exception:
            has_awssecret = False

        secret_helper_block = """
            // Env-only secret resolver for awssecretmanager.VAR (no AWS calls)
            const SECRET_ENV_MAP: Record<string, string> = {
              // Team-specific mappings: placeholder key -> Jenkins env var name
              // Example: rhapsody_password → AUTOTEST_USER_PWD
              rhapsody_password: 'AUTOTEST_USER_PWD',
            };
            function envOrFail(varName: string): string {
              const value = process.env[varName];
              if (!value) throw new Error(`Environment variable ${varName} is required but not set`);
              return value;
            }
            async function replacePlaceholders(value: any): Promise<any> {
              if (typeof value !== 'string') return value;
              const match = value.match(/\\{\\s*awssecretmanager\\.([A-Z0-9_]+)\\s*\\}/i);
              if (!match) return value;
              const rawKey = match[1];
              const mapped = SECRET_ENV_MAP[rawKey] || rawKey;
              return envOrFail(mapped);
            }
        """.strip() if has_awssecret else ""

        example = """
              Below is example for your reference

            import {{ test, expect }} from '@playwright/test';
            import {{ ApiService }} from '../../services/api.service';
            import {{ AccountService }} from '../../services/account.service';
            import {{ MockAPIService }} from '../../services/mockapi.service';
            import path from 'path';
            
            {secret_helper_block}

            {datasets_block}

            test.describe.serial('Test Suite', async () => {{

              {loop_prefix}test(`@{jira_id} [\${i + 1}] \${row.fullName}`, async ({{ page }}) => {{

                // Initialize services
                const accountService = new AccountService();
                const apiService = new ApiService();
                let storedValues: {{ [key: string]: any }} = {};

                // Authentication service
                const authConfig = {{ body_from_file: path.join(process.cwd(), 'src/tests/data', 'newuser_auth_config.json') }};
                const authResponse = await apiService.makeRequest(authConfig);
                storedValues['NewUserAuthToken'] = authResponse.response_data.access_token;

                // Account service
                const accountConfig = {{
                  body_from_file: path.join(process.cwd(), 'src/tests/data', 'newuser_account_config.json'),
                  auth_token: storedValues['NewUserAuthToken']
                }};
                const accountResponse = await apiService.makeRequest(accountConfig);
                storedValues['accountResponse'] = accountResponse.response_data;

                const sessionConfig = {{
                  body_from_file: path.join(process.cwd(), 'src/tests/data', 'newuser_session_config.json'),
                  auth_token: storedValues['NewUserAuthToken'],
                  body: {{ loginId: storedValues['accountResponse'].loginId }}
                }};
                const sessionResponse = await apiService.makeRequest(sessionConfig);
                storedValues['sessionResponse'] = sessionResponse.response_data;

                // Set authentication cookies
                await page.context().addCookies([
                  {{ name: 'ticket', value: storedValues['sessionResponse'].idToken, domain: '.www-stg.chewy.net', path: '/' }},
                  {{ name: 'asid', value: storedValues['sessionResponse'].sessionId, domain: '.www-stg.chewy.net', path: '/' }},
                  {{ name: 'at', value: storedValues['sessionResponse'].authToken, domain: '.www-stg.chewy.net', path: '/' }}
                ]);
                
                // Apply dataset-driven viewport before first navigation (if provided)
                {resolution_block}

                // Navigate to URL
                await page.goto("https://www-stg.chewy.net/purina-one-natural-smartblend-chicken/dp/42212");
                await page.waitForLoadState('domcontentloaded');
                await page.waitForTimeout(3000);

                // User clicks on "Buy once" using robust label+scroll/force pattern
                const buyOnceLabel = page.getByTestId('buy-once-selector').locator('label');
                await buyOnceLabel.scrollIntoViewIfNeeded();
                await buyOnceLabel.evaluate(el => el.scrollIntoView({ block: 'center', inline: 'center' }));
                try { await buyOnceLabel.click({{ timeout: 10000 }}); } catch { await buyOnceLabel.click({{ force: true }}); }
                await expect(page.getByRole('radio', {{ name: 'Buy once' }})).toBeChecked();

                // User clicks on "Add to Cart" with scroll/force fallback
                const addToCartButton = page.getByTestId('add-to-cart').getByText('Add to Cart');
                await addToCartButton.scrollIntoViewIfNeeded();
                await expect(addToCartButton).toBeVisible({ timeout: 30000 });
                try { await addToCartButton.click({ timeout: 10000 }); } catch { await addToCartButton.click({ force: true }); }

                // User clicks on "Proceed to Checkout" using reliable xpath
                const proceedToCheckoutButton = page.locator(`xpath=//div[@data-testid="smartshelf-actions"]/div[3]/button[2]`);
                await page.waitForLoadState('domcontentloaded');
                await page.waitForTimeout(3000);
                await proceedToCheckoutButton.click();

                // User clicks the "Buy once" Radio button (avoid sticky header interception)
                const buyOnceLabel = page.getByTestId('buy-once-selector').locator('label');
                await buyOnceLabel.waitFor({{ state: 'visible' }});
                await buyOnceLabel.scrollIntoViewIfNeeded();
                await buyOnceLabel.evaluate(el => el.scrollIntoView({{ block: 'center', inline: 'center' }}));
                try { await buyOnceLabel.click({{ timeout: 10000 }}); } catch { await buyOnceLabel.click({{ force: true }}); }
                await expect(page.getByRole('radio', {{ name: 'Buy once' }})).toBeChecked();
                
                //user enters the "First and Last Name"
                const nameInput = page.getByTestId('name-input');
                await page.waitForLoadState('domcontentloaded');
                await page.waitForTimeout(3000);
                await nameInput.fill(row.fullName);

                // User clicks on "Add to Cart" (match BDD locator) with robust scroll/click
                const addToCartButton = page.getByTestId('add-to-cart').getByText('Add to Cart');
                await addToCartButton.scrollIntoViewIfNeeded();
                await expect(addToCartButton).toBeVisible({{ timeout: 30000 }});
                try {{ await addToCartButton.click({{ timeout: 10000 }}); }} catch {{ await addToCartButton.click({{ force: true }}); }}

                // login email is handled earlier via generated unique email; no email field fill here
                
                // Mock "Ship to this address" button
                await mockApiService.setupMock(page, 'mock_post_shiptothisaddress.json');
                const shipToAddressButton = page.getByTestId('submit-button');
                await page.waitForLoadState('domcontentloaded');
                await page.waitForTimeout(3000);
                await shipToAddressButton.click();

                {paypal_code}
                {post_api_code}
                {get_api_code}
                {cookie_code}
                {mock_api_code}

              }});{loop_suffix}

            }});
            """

        wrap_guidance_line = "CRITICAL: For any string input containing awssecretmanager.*, wrap with: await replacePlaceholders(value)." if has_awssecret else ""

        template_playwright_test = """
        CRITICAL: Start your response immediately with 'import' statements. Do not include any explanatory text.
        CRITICAL: Generate a complete Playwright test file matching the exact format shown in the example.
        CRITICAL: Use the exact import structure: import {{ test }} from '@playwright/test';
        CRITICAL: Use test.describe.serial for grouping tests and individual test functions with proper tags
        CRITICAL: Generate direct Playwright actions without BDD step wrappers
        CRITICAL: Add await page.waitForLoadState('domcontentloaded') and await page.waitForTimeout(3000) before every action like navigation, click, fill, select, verification etc.
        CRITICAL: When {paypal_code} is provided, replace the PayPal click action with the EXACT code from paypal_code variable - DO NOT generate your own PayPal code.
        CRITICAL: Use the exact file name for the {jira_id} tag in the test function.
        {datasets_rules}
        CRITICAL: Use the exact code from post_api_code, get_api_code and cookie_code
        {wrap_guidance_line}
        As an expert QA Test Automation Engineer, generate a complete Playwright TypeScript test file in the exact format shown.
        
        Fields DataFrame:
        {df}

        CRITICAL PATTERNS - FOLLOW EXACTLY:

        1. IMPORTS:
            - import {{ test, expect }} from '@playwright/test';
            - import {{ ApiService }} from '../../services/api.service';
            - import {{ AccountService }} from '../../services/account.service';

        2. TEST STRUCTURE:
            - test.describe.serial with feature name
            - Individual test functions with proper tags from feature file
            - Each test should have ({{ page }}) parameter
            - Initialize services at the beginning of each test

        3. DIRECT PLAYWRIGHT ACTIONS:
            - Use page.goto() for navigation
            - Use page.setViewportSize() for setting viewport
            - Use page.waitForLoadState('domcontentloaded') after navigation
            - Use page.getByTestId(), page.getByText(), page.getByRole() for element selection
            - NOTE: Playwright actions (click, fill, etc.) have built-in auto-waiting for visibility - do NOT add explicit waitFor before these actions
            - Use await element.click(), await element.fill() for actions
            - {wrap_guidance_line}
            - Use await page.waitForTimeout() for delays when needed
            - Add descriptive comments before each action

        4. API INTEGRATION:
            - Use AccountService for account creation and authentication
            - Use ApiService for all API requests (GET, POST, etc.)
            - Store API responses in storedValues object
            - Use proper error handling and validation

        5. ELEMENT INTERACTION PATTERN:
            - Declare element as const variable
            - Wait for DOM content loaded if needed
            - Perform action (click/fill have built-in auto-waiting for visibility)
            - NOTE: Do NOT add explicit waitFor({{ state: 'visible' }}) before click/fill - it's redundant

        STRICTLY FOLLOW:
        - Use exact format from example
        - These are test datas: {testdata}
        - Use PlaywrightLocator values from DataFrame
        - Include proper test tags from feature file scenarios
        - Generate ONLY the complete test file code
        - NO code block markers or formatting
        - NO wrapper text or prefixes
        - NO BDD step definitions or wrappers
        - Generate executable TypeScript code with proper service integration

        {example}
        """

        merged_prompt = """
        CRITICAL: Start your response immediately with 'import' statements. Do not include any explanatory text.
        CRITICAL: Generate a complete Playwright test file matching the exact format shown in the example.
        CRITICAL: Use the exact import structure with AccountService and ApiService
        CRITICAL: Use test.describe.serial for grouping tests and individual test functions with proper tags
        CRITICAL: Generate direct Playwright actions without BDD step wrappers
        
        As an expert QA Test Automation Engineer, generate a complete Playwright TypeScript test file in the exact format shown.
        
        Fields DataFrame:
        {df}

        CRITICAL PATTERNS - FOLLOW EXACTLY:

        1. IMPORTS:
            - import {{ test, expect }} from '@playwright/test';
            - import {{ ApiService }} from '../../services/api.service';
            - import {{ AccountService }} from '../../services/account.service';

        2. TEST STRUCTURE:
            - test.describe.serial with feature name
            - Individual test functions with proper tags from feature file
            - Each test should have ({{ page }}) parameter
            - Initialize services at the beginning of each test

        3. DIRECT PLAYWRIGHT ACTIONS:
            - Use page.goto() for navigation
            - Use page.setViewportSize() for setting viewport
            - Use page.waitForLoadState('domcontentloaded') after navigation
            - Use page.getByTestId(), page.getByText(), page.getByRole() for element selection
            - NOTE: Playwright actions (click, fill, etc.) have built-in auto-waiting for visibility - do NOT add explicit waitFor before these actions
            - Use await element.click(), await element.fill() for actions
            - {wrap_guidance_line}
            - Use await page.waitForTimeout() for delays when needed
            - Add descriptive comments before each action

        4. API INTEGRATION:
            - Use AccountService for account creation and authentication
            - Use ApiService for all API requests (GET, POST, etc.)
            - Store API responses in storedValues object
            - Use proper error handling and validation

        5. ELEMENT INTERACTION PATTERN:
            - Declare element as const variable
            - Wait for DOM content loaded if needed
            - Perform action (click/fill have built-in auto-waiting for visibility)
            - NOTE: Do NOT add explicit waitFor({{ state: 'visible' }}) before click/fill - it's redundant

        {datasets_rules}
        {example}
        """

        model = LLM()

        logger.info("MY DF \\n")
        logger.info(df)
        logger.info(f"Checking for gift card detection...")
        logger.info(f"Objects in df_inter: {df_inter['object'].tolist()}")
        logger.info(f"Actions in df_inter: {df_inter['action'].tolist()}")
        click_actions = df_inter['action'].astype(str).str.contains("click", case=False)
        logger.info(f"Click actions found: {click_actions.any()}")

        # Build dataset-aware blocks for prompt/example
        # Detect if feature explicitly contains a resolution step
        feature_has_resolution = False
        try:
            import re as _re_local
            if isinstance(data, str):
                feature_has_resolution = bool(_re_local.search(r'the\s+browser\s+is\s+launched\s+with[\s\S]*?resolution', data, flags=_re_local.IGNORECASE))
        except Exception:
            feature_has_resolution = False

        if datasets_literal:
            datasets_block = f"const datasets = {datasets_literal};"
            loop_prefix = "for (const [i, row] of datasets.entries()) { "
            loop_suffix = " }"
            if _df_is_ui_only(df_inter):
                datasets_rules = """
        CRITICAL: Examples/datasets present (UI-only). You MUST:
        - Declare `const datasets = [...]` exactly matching the injected JSON.
        - Use `for (const [i, row] of datasets.entries()) { ... }` when the JSON has one or more rows.
        - Name each test: `test(\`@example_table [${i + 1}] row-${i + 1}\`, ...)` — never use row.fullName, row.password, row.address, etc. unless that property exists on every dataset object.
        - Use only `row.<key>` for keys that exist on dataset objects (e.g. baseUrl, firstProductUrl, secondProductUrl). Do not invent keys.
        - Implement only what the Fields DataFrame describes (navigate → page.goto with row.* or literals from DataFrame; click/fill → use PlaywrightLocator / role / testid from the DataFrame).
        - FORBIDDEN unless in DataFrame: AccountService, ApiService, login/auth, checkout address forms, smartshelf-actions XPath, PayPal/API helper strings, page.evaluate for fake code blocks.
        - After `page.goto`, call `await page.waitForLoadState('domcontentloaded')` once; avoid chaining `waitForTimeout(3000)` before every click.
            """.strip()
            else:
                datasets_rules = """
        CRITICAL: Dataset present. You MUST:
        - Declare `const datasets = [...]` exactly as injected.
        - Iterate with: `for (const [i, row] of datasets.entries()) { ... }`.
        - Name each test: `@example_table [${i + 1}] ${row.fullName}`.
        - After authentication, ALWAYS create a unique account per row and log in with that generated email.
          Do NOT use any email/username from the dataset for login. Use row.password for the password.
        - Fill checkout inputs strictly from dataset: row.fullName, row.address, row.city, row.state (selectOption), row.zip, row.phone.
        - Use robust selectors as in the example (scrollIntoViewIfNeeded, force-click fallback) and proceed via
          `xpath=//div[@data-testid='smartshelf-actions']/div[3]/button[2]`.
        - Add `await page.waitForLoadState('domcontentloaded')` and small waits (`await page.waitForTimeout(3000)`) where needed.
            """.strip()
            resolution_block = """
                const parseResolution = (value: unknown) => {
                  if (!value || typeof value !== 'string') return undefined;
                  const m = value.match(/(\\d+)\\s*[xX\\*,\\s]\\s*(\\d+)/);
                  if (!m) return undefined;
                  const width = parseInt(m[1], 10);
                  const height = parseInt(m[2], 10);
                  if (width >= 300 && width <= 5000 && height >= 300 && height <= 5000) return { width, height };
                  return undefined;
                };
                const res = parseResolution((row as any).resolution);
                if (res) {
                  await page.setViewportSize(res);
                  console.log(`Applied dataset viewport: ${res.width}x${res.height}`);
                }
            """.strip()
            # Only include viewport logic when the feature actually has the resolution step
            if not feature_has_resolution:
                resolution_block = "// Feature has no explicit resolution step; skipping viewport set"
        else:
            datasets_block = "// No datasets provided"
            loop_prefix = ""
            loop_suffix = ""
            datasets_rules = "// No datasets: generate a single test without any loop"
            resolution_block = "// No datasets: skipping viewport set"

        # Inject blocks via literal replacement to avoid Python .format interpreting TS braces
        example = (example
                   .replace("{datasets_block}", datasets_block)
                   .replace("{resolution_block}", resolution_block)
                   .replace("{loop_prefix}", loop_prefix)
                   .replace("{loop_suffix}", loop_suffix)
                   .replace("{jira_id}", str(jira_id))
                   .replace("{secret_helper_block}", secret_helper_block)
                   .replace("{paypal_code}", paypal_code or "")
                   .replace("{post_api_code}", post_api_code or "")
                   .replace("{get_api_code}", get_api_code or "")
                   .replace("{cookie_code}", cookie_code or "")
                   .replace("{mock_api_code}", mock_api_code or "")
                   )

        if datasets_literal and _df_is_ui_only(df_inter):
            example = """
Below is a minimal reference for UI-only scenarios with Examples (no API/auth/checkout boilerplate).

import {{ test, expect }} from '@playwright/test';

{secret_helper_block}

{datasets_block}

test.describe.serial('Feature', () => {{
  {loop_prefix}test(`@example_table [\${i + 1}] row-\${i + 1}`, async ({{ page }}) => {{
    {resolution_block}
    await page.goto(String((row as any).baseUrl));
    await page.waitForLoadState('domcontentloaded');
    await page.goto(String((row as any).firstProductUrl));
    await page.getByRole('button', {{ name: 'Set Up Autoship' }}).click();
    await page.goto(String((row as any).secondProductUrl));
    await page.getByRole('button', {{ name: 'Set Up Autoship' }}).click();
  }});{loop_suffix}
}});
"""
            example = (
                example.replace("{datasets_block}", datasets_block)
                .replace("{resolution_block}", resolution_block)
                .replace("{loop_prefix}", loop_prefix)
                .replace("{loop_suffix}", loop_suffix)
                .replace("{secret_helper_block}", secret_helper_block)
            )

        if flag:
            output_qa_prompt_test = model.send_request(merged_prompt,
                                                      ["df", "example", "testdata", "paypal_code",
                                                       "post_api_code", "get_api_code", "cookie_code", "mock_api_code",
                                                       "datasets_block", "loop_prefix", "loop_suffix", "datasets_rules", "jira_id", "wrap_guidance_line"],
                                                      {'df': df, "example": example,
                                                       "testdata": testdata,
                                                       "paypal_code": paypal_code or "",
                                                       "post_api_code": post_api_code or "",
                                                       "get_api_code": get_api_code or "",
                                                       "cookie_code": cookie_code or "",
                                                       "mock_api_code": mock_api_code or "",
                                                       "datasets_block": datasets_block,
                                                       "loop_prefix": loop_prefix,
                                                       "loop_suffix": loop_suffix,
                                                       "datasets_rules": datasets_rules,
                                                       "jira_id": jira_id,
                                                       "wrap_guidance_line": wrap_guidance_line})

            if output_qa_prompt_test.startswith("```typescript"
                                               "") and output_qa_prompt_test.endswith("```"):
                output_qa_prompt_test = output_qa_prompt_test[7:-3]
            script += "\n\n"
            script_chunk = process_typescript_code(output_qa_prompt_test)
            # Enforce exact datasets embedding
            if datasets_literal:
                script_chunk = _enforce_datasets(script_chunk, datasets_literal)
            if datasets_literal and not _df_is_ui_only(df_inter):
                script_chunk = _enforce_login_block(script_chunk, True)
            script += script_chunk

        else:
            logger.info(f"🔍 PayPal code status: {'ACTIVE' if paypal_code else 'INACTIVE'}")
            output_qa_prompt_test = model.send_request(template_playwright_test,
                                                      ["df", "example", "testdata", "paypal_code",
                                                       "post_api_code", "get_api_code", "cookie_code", "mock_api_code",
                                                       "datasets_block", "loop_prefix", "loop_suffix", "datasets_rules", "jira_id", "wrap_guidance_line"],
                                                      {'df': df, "example": example,
                                                       "testdata": testdata,
                                                       "paypal_code": paypal_code or "",
                                                       "post_api_code": post_api_code or "",
                                                       "get_api_code": get_api_code or "",
                                                       "cookie_code": cookie_code or "",
                                                       "mock_api_code": mock_api_code or "",
                                                       "datasets_block": datasets_block,
                                                       "loop_prefix": loop_prefix,
                                                       "loop_suffix": loop_suffix,
                                                       "datasets_rules": datasets_rules,
                                                       "jira_id": jira_id,
                                                       "wrap_guidance_line": wrap_guidance_line})

            if output_qa_prompt_test.startswith("```typescript"
                                               "") and output_qa_prompt_test.endswith("```"):
                output_qa_prompt_test = output_qa_prompt_test[7:-3]
            script = process_typescript_code(output_qa_prompt_test)
            # Enforce exact datasets embedding
            if datasets_literal:
                script = _enforce_datasets(script, datasets_literal)
            if datasets_literal and not _df_is_ui_only(df_inter):
                script = _enforce_login_block(script, True)
            logger.info(script)

        logger.info('Playwright test file generated')
        return script
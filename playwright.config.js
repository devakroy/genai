// Playwright configuration for optimized browser downloads
module.exports = {
  // Use specific browsers to avoid downloading unnecessary ones
  projects: [
    {
      name: 'chromium',
      use: { ...require('@playwright/test').devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...require('@playwright/test').devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...require('@playwright/test').devices['Desktop Safari'] },
    },
  ],
  
  // Optimize download settings
  use: {
    // Reduce timeout for faster failures
    actionTimeout: 10000,
    navigationTimeout: 30000,
    
    // Disable unnecessary features for faster startup
    headless: true,
    ignoreHTTPSErrors: true,
    
    // Optimize browser launch
    launchOptions: {
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-accelerated-2d-canvas',
        '--no-first-run',
        '--no-zygote',
        '--disable-gpu'
      ]
    }
  },
  
  // Global test settings
  timeout: 30000,
  retries: 2,
  
  // Reporter configuration
  reporter: 'html',
  
  // Global setup and teardown
  globalSetup: undefined,
  globalTeardown: undefined,
};

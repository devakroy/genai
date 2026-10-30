# Platform-wide configuration constant
# All values migrated from .env

PLATFORM_CONFIG = {
    "AUTH_CLIENT_SECRET": "AjW1BSLoCvCgTI4iSR8BYJSOeo8Hpcg7",
    "AUTH_SCOPE": "account-orchestrator-service:/v1/accounts:create account-orchestrator-service:/v1/accounts/password:create account-orchestrator-service:/v1/accounts/sessions:create",
    "ACCOUNTAPI_URL": "https://stg-use2-apt-shared-main.plat.stg.aws.chewy.cloud/eks/app/account-orchestrator/v1/test/accounts",
    "ACCOUNTAPI_U2S_CLIENT_ID": "storefront-web-u2s-client",
    "SESSIONAPI_URL": "https://stg-use2-apt-shared-main.plat.stg.aws.chewy.cloud/eks/app/account-orchestrator/v1/accounts/sessions",
    "PLAYWRIGHT_DEFAULT_PASSWORD": "chewyplaywright123#",
    "PLAYWRIGHT_EMAIL_DOMAIN": "paymentsuiplaywright.com",
    "PLAYWRIGHT_EMAIL_PREFIX": "playwright-",
    "BEDROCK_MODEL_ID": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "GIFT_CARD_API_URL": "https://giftcard.opta.stg.aws.chewy.cloud/giftcard-service/v1/giftcards",
    "GIFT_CARD_S2S_CLIENT_ID": "gift-cards-test-automation-s2s-client",
    "GIFT_CARD_S2S_CLIENT_SECRET": "v23OwrAyUVv8Acf3gvuocQ0ozPyiLBrx",
    # Database Configuration
    "PGHOST": "chewy-dev-us-east-1-test-genie.cbpioeretmip.us-east-1.rds.amazonaws.com",
    "PGPORT": 5432,
    "PGDATABASE": "postgres",
    "PGUSER": "postgres",
    "PGPASSWORD": "testgenie-db",
    "PGSSLMODE": "require",
    "PGSCHEMA": "public",
    "APP_ID": "chewy_web_storefront",
    "PLAYWRIGHT_STORE_DIR": "target/playwright_temp_files",
    "JSON_FILE_PATH": "target/json",
    "CHROMADB_EMBEDDING_PATH": "target/embedding_model",
    "CHROMADB_STORAGE_PATH": "target/SavedContext",
    "RECORDING_DIR":"target/recording",
    "RECORDING_CLEAN_INTERVAL_MINUTES": 1,   # how often the sweeper runs
    "RECORDING_TTL_MINUTES": 15,
    "DEPLOY_ENV":"STAGE",
    # S3 recordings only (corp_ai_workloads_stg bucket; use S3_RECORDINGS_AWS_* in .env for credentials)
    "S3_RECORDINGS_BUCKET": "genai-chewy-automation",
    "S3_RECORDINGS_PREFIX": "Recordings",
    # Presigned URL validity (seconds). Default: 2 hours.
    "S3_PRESIGNED_EXPIRES_SECONDS": 7200,
    # SMTP/Notifications (defaults; override via environment if needed)
    "SMTP_HOST": "smtp-nonprod.shd.aws.chewy.cloud",
    "SMTP_PORT": 25,
    "SMTP_STARTTLS": False,
    "SMTP_SENDER": "sa-testgenie-sh@chewy.com",
    
    # TTL Cache Configuration for Locator Reuse
    "CACHE_TTL_SECONDS": 1800,  # 30 minutes
    "CACHE_MAX_SIZE": 1000,     # Maximum cache entries
    "CACHE_CLEANUP_THRESHOLD": 1200,  # Trigger cleanup when exceeded

    "soft_validation":False,
}

# JIRA_IO = {
#     "input": "../target/jira_input"
# }
# CONFLUENCE_IO = {
#     "input": "../target/confluence_input"
# }
# BDD_IO = {
#     "input": "../target/bdd_input"
# }

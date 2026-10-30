import os

# Base directory paths
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, '..', 'data')
REVIEW_DIR = os.path.join(DATA_DIR, 'review')

# Specific directory paths
REVIEW_PLAYWRIGHT_SCRIPTS_DIR = os.path.join(REVIEW_DIR, 'playwright_scripts')
BDD_READY_FOR_REVIEW_DIR = os.path.join(REVIEW_DIR, 'bdd_ready_for_review')
BDD_REVIEWED_DIR = os.path.join(REVIEW_DIR, 'bdd_reviewed')

# Ensure directories exist
os.makedirs(REVIEW_PLAYWRIGHT_SCRIPTS_DIR, exist_ok=True)
os.makedirs(BDD_READY_FOR_REVIEW_DIR, exist_ok=True)
os.makedirs(BDD_REVIEWED_DIR, exist_ok=True)
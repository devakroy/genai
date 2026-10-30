import subprocess
import os
import sys
import shutil
import platform
import time

# ANSI color codes for terminal output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    RESET = '\033[0m'
    
    @staticmethod
    def ok(text):
        return f"{Colors.GREEN}{text}{Colors.RESET}"
    
    @staticmethod
    def error(text):
        return f"{Colors.RED}{text}{Colors.RESET}"
    
    @staticmethod
    def warning(text):
        return f"{Colors.YELLOW}{text}{Colors.RESET}"

def get_platform_info():
    """Get information about the operating system"""
    return {
        'system': platform.system(),  # 'Windows', 'Darwin' (Mac), 'Linux'
        'is_windows': sys.platform == 'win32',
        'is_mac': sys.platform == 'darwin',
        'is_linux': sys.platform.startswith('linux')
    }

def run_command(command, shell=False, check=True):
    """Wrapper for running commands with proper error handling"""
    try:
        print(f"Running command: {' '.join(command if isinstance(command, list) else command)}")
        result = subprocess.run(
            command,
            shell=shell,
            check=check,
            capture_output=True,
            text=True
        )
        print(f"Command output: {result.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Command failed with error: {e}")
        print(f"Error output: {e.stderr}")
        return False
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return False

def check_python_version():
    """Check if Python 3.12 is being used"""
    version_info = sys.version_info
    current_version = f"{version_info.major}.{version_info.minor}.{version_info.micro}"
    
    print(f"Current Python version: {current_version}")
    
    if version_info.major == 3 and version_info.minor == 12:
        print(f"{Colors.ok('[OK]')} Python 3.12.x detected")
        if version_info.micro != 8:
            print(f"  Note: For optimal MCP functionality, Python 3.12.8 is recommended (you have 3.12.{version_info.micro})")
        return True
    else:
        print(f"{Colors.error('[ERROR]')} Python {current_version} detected")
        print("\nWARNING: For proper MCP functionality, Python 3.12.8 is recommended!")
        print("Download Python 3.12.8 from: https://www.python.org/downloads/")
        print("\nYou can continue, but may experience issues with MCP features.\n")
        return False

def check_nodejs():
    """Check if Node.js is installed"""
    try:
        result = subprocess.run(
            ['node', '--version'],
            capture_output=True,
            text=True,
            check=True
        )
        version = result.stdout.strip()
        print(f"{Colors.ok('[OK]')} Node.js is installed: {version}")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"{Colors.error('[ERROR]')} Node.js is NOT installed")
        print("\nWARNING: Node.js is required for Playwright MCP server!")
        print("Please install Node.js from: https://nodejs.org/")
        print("\nYou can continue with Python setup, but you'll need Node.js")
        print("to use Playwright MCP server features.\n")
        return False

def create_and_install():
    platform_info = get_platform_info()
    print(f"\nCHEWY GENAI AUTOMATION - Installation Setup")
    print(f"Platform: {platform_info['system']}\n")
    
    # Check dependencies
    print("Pre-Installation Dependency Check")
    print("-" * 50)
    
    print("\n> Python Version:")
    python_ok = check_python_version()
    
    print("\n> Node.js:")
    nodejs_installed = check_nodejs()
    print()
    
    if not python_ok:
        response = input(f"{Colors.warning('[WARNING]')} Python 3.12.8 is recommended for MCP. Continue anyway? (y/n): ").lower()
        if response != 'y':
            print(f"\n{Colors.error('[ERROR]')} Installation cancelled. Please install Python 3.12.8 and try again.")
            return False
    
    print("\nVirtual Environment Setup")
    print("-" * 50 + "\n")

    # Delete the existing environment
    print("[Step 1] Checking for existing virtual environment...")
    venv_path = '.venv'
    if os.path.exists(venv_path):
        print("   Found existing .venv - removing it...")
        try:
            shutil.rmtree(venv_path)
            print(f"   {Colors.ok('[OK]')} Removed existing virtual environment")
        except Exception as e:
            print(f"   {Colors.error('[ERROR]')} Error removing virtual environment: {str(e)}")
            return False
    else:
        print(f"   {Colors.ok('[OK]')} No existing virtual environment found")

    # Create the virtual environment
    print("\n[Step 2] Creating new virtual environment...")
    venv_command = [sys.executable, '-m', 'venv', venv_path]
    if not run_command(venv_command):
        return False
    print(f"   {Colors.ok('[OK]')} Virtual environment created")

    # Determine the path to pip inside the venv
    print("\n[Step 3] Configuring virtual environment paths...")
    if platform_info['is_windows']:
        pip_path = os.path.join(venv_path, 'Scripts', 'pip.exe')
        python_path = os.path.join(venv_path, 'Scripts', 'python.exe')
    else:
        pip_path = os.path.join(venv_path, 'bin', 'pip')
        python_path = os.path.join(venv_path, 'bin', 'python')

    print(f"   {Colors.ok('[OK]')} Pip: {pip_path}")
    print(f"   {Colors.ok('[OK]')} Python: {python_path}")

    print("\nPackage Installation")
    print("-" * 50 + "\n")

    # Upgrade pip
    print("[Step 4] Upgrading pip...")
    upgrade_pip_command = [python_path, '-m', 'pip', 'install', '--upgrade', 'pip']
    if not run_command(upgrade_pip_command):
        return False
    print(f"   {Colors.ok('[OK]')} Pip upgraded")

    # Install tqdm first for progress tracking
    print("\n[Step 5] Installing tqdm...")
    tqdm_command = [pip_path, 'install', 'tqdm']
    if not run_command(tqdm_command):
        return False
    print(f"   {Colors.ok('[OK]')} tqdm installed")
    
    # Install packages using the venv's pip with progress bar
    print("\n[Step 6] Installing project dependencies...")
    requirements_path = os.path.join('requirements.txt')
    if os.path.exists(requirements_path):
        # Read requirements.txt to count packages
        with open(requirements_path, 'r') as f:
            lines = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
        
        total_packages = len(lines)
        print(f"   Found {total_packages} packages to install\n")
        
        # Import tqdm in a subprocess and install packages with progress
        install_script = f'''
import subprocess
import sys
try:
    from tqdm import tqdm
    
    packages = {lines}
    installed_packages = []
    failed_packages = []
    
    with tqdm(total=len(packages), desc="Installing", unit="pkg", ncols=80, dynamic_ncols=False, leave=True) as pbar:
        for package in packages:
            pkg_name = package.split('==')[0] if '==' in package else package
            # Update description without causing line wraps
            pbar.set_description(f"{{pkg_name[:15]:15s}}")
            result = subprocess.run(
                ["{pip_path}", "install", package],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                pbar.write(f"Warning: Failed to install {{package}}")
                failed_packages.append(package)
            else:
                installed_packages.append(package)
            pbar.update(1)
        # Clear the line after completion
        pbar.set_description("Complete")
    
    print(f"\\n{Colors.ok('[OK]')} Successfully installed {{len(installed_packages)}}/{{len(packages)}} packages")
    if failed_packages:
        print(f"{Colors.error('[ERROR]')} Failed packages: {{', '.join(failed_packages)}}")
except ImportError:
    print("Error: tqdm not found. Installing packages without progress bar...")
    subprocess.run(["{pip_path}", "install", "-r", "{requirements_path}"])
'''
        
        # Run the installation script with the venv's python
        try:
            result = subprocess.run(
                [python_path, '-c', install_script],
                check=False,
                text=True
            )
            if result.returncode != 0:
                print("Installation completed with some warnings")
        except Exception as e:
            print(f"Error during installation: {str(e)}")
            return False
    else:
        print(f"Error: requirements.txt not found at {requirements_path}")
        return False

    print("\nPlaywright Browser Setup")
    print("-" * 50 + "\n")
    
    # Install Playwright browsers
    print("[Step 7] Installing Playwright browsers...")
    print("   Downloading Chromium, Firefox, and WebKit (this may take a few minutes)\n")
    playwright_install_command = [python_path, '-m', 'playwright', 'install']
    if not run_command(playwright_install_command):
        print(f"   {Colors.warning('[WARNING]')} Playwright browser installation failed.")
        print(f"   You may need to run manually: {python_path} -m playwright install")
    else:
        print(f"   {Colors.ok('[OK]')} Browsers installed")
    
    # Install Playwright system dependencies (for Linux)
    if platform_info['is_linux']:
        print("\n[Step 8] Installing Playwright system dependencies (Linux)...")
        playwright_deps_command = [python_path, '-m', 'playwright', 'install-deps']
        if not run_command(playwright_deps_command):
            print(f"   {Colors.warning('[WARNING]')} Some system dependencies may be missing.")
            print(f"   Run manually: sudo {python_path} -m playwright install-deps")
        else:
            print(f"   {Colors.ok('[OK]')} Dependencies installed")

    print("\nEnvironment Configuration")
    print("-" * 50 + "\n")
    
    # Create .env file
    step_num = '9' if platform_info['is_linux'] else '8'
    print(f"[Step {step_num}] Creating .env configuration file...")
    src_dir = "src"
    
    try:
        os.makedirs(src_dir, exist_ok=True)
        print(f"   {Colors.ok('[OK]')} Directory: {src_dir}")
        
        env_file_path = os.path.join(src_dir, '.env')
        
        with open(env_file_path, 'w') as f:
            f.write("""# Environment variables
        # External Embedding Model Configuration
# External Embedding Model Configuration
EMBEDDING_MODEL_NAME=thenlper/gte-small
EXTERNAL_MODEL_THRESHOLD=0.156
DEFAULT_MODEL_THRESHOLD=0.50

# TEMPERATURE is the randomness factor for LLM responses (0-1), higher values make output more diverse.
TEMPERATURE=0.25



# Provide the number of contexts to be retrieved from Chroma DB
CONTEXTS_RETRIEVED_COUNT=3
# Provide the type of BDD Scenario to be generated ( Positive/ Negative/ Both )
BDD_SCENARIO_TYPE=Both

# AWS BEDROCK Configuration
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0
BEDROCK_TEMPERATURE=0.50

JIRA_URL=https://chewyinc.atlassian.net
JIRA_API_TOKEN=""
JIRA_EMAIL=""

# Confluence Credentials
CONFLUENCE_URL=https://chewyinc.atlassian.net
CONFLUENCE_API_TOKEN=""
CONFLUENCE_EMAIL=""

# ChromaDB Configuration
# Embedding Models Configuration
EMBEDDING_MODEL_NAME=thenlper/gte-small
EMBEDDING_MODEL_PATH=../data/External_embedding_model
EXTERNAL_MODEL_THRESHOLD=0.156
DEFAULT_MODEL_THRESHOLD=0.50


# AWS KENDRA INDEX Configuration
CONFLUENCE_INDEX_ID="902d780e-5f8e-4d17-b9f7-29c5a8faeed8"
BDD_SCENARIO_INDEX_ID="c35219ba-d3f0-4d6f-bf68-37c68164dcd9"

# AWS S3 Configuration (recordings bucket / prefix; overridden by env if set)
S3_RECORDINGS_BUCKET=genai-chewy-automation
S3_RECORDINGS_PREFIX=Recordings

LOCAL_MCP = true

# AWS Credentials (Secrets Manager, Bedrock, etc. — unchanged)
AWS_REGION=us-east-1

AWS_ACCESS_KEY_ID="YOUR_AWS_ACCESS_KEY_ID"
AWS_SECRET_ACCESS_KEY="YOUR_AWS_SECRET_ACCESS_KEY"
AWS_SESSION_TOKEN="YOUR_AWS_SESSION_TOKEN"

# S3 recordings only — corp_ai_workloads_stg (Option B: separate from AWS_* above)
# When set, uploads, presigned URLs, and /recordings/download streaming use this bucket + these keys.
S3_RECORDINGS_AWS_REGION=us-east-1
S3_RECORDINGS_AWS_ACCESS_KEY_ID="YOUR_S3_RECORDINGS_AWS_ACCESS_KEY_ID"
S3_RECORDINGS_AWS_SECRET_ACCESS_KEY="YOUR_S3_RECORDINGS_AWS_SECRET_ACCESS_KEY"
S3_RECORDINGS_AWS_SESSION_TOKEN="YOUR_S3_RECORDINGS_AWS_SESSION_TOKEN"

""")
        print(f"   {Colors.ok('[OK]')} .env file created at: {env_file_path}")
        print(f"   {Colors.warning('[WARNING]')} Remember to update credentials in this file!")
    except Exception as e:
        print(f"   {Colors.error('[ERROR]')} Error creating .env file: {str(e)}")
        return False

    return True

def main():
    success = create_and_install()
    
    print("\n" + "-" * 50)
    if not success:
        print(f"{Colors.error('[FAILED]')} Installation failed - check errors above\n")
        sys.exit(1)
    else:
        print(f"{Colors.ok('[SUCCESS]')} Installation completed!\n")
        print("Next Steps:")
        print("  1. Activate virtual environment:")
        print("     Windows:   .venv\\Scripts\\activate")
        print("     Mac/Linux: source .venv/bin/activate")
        print("  2. Update credentials in: src/.env")
        print("  3. Install Node.js if using Playwright MCP server")
        print("\nSetup complete!\n")

if __name__ == '__main__':
    main()
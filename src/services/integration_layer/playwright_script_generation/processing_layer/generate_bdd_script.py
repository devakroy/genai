import asyncio
import json
import os
from services.integration_layer.playwright_script_generation.processing_layer.playwright_main import crawl
from services.integration_layer.playwright_script_generation.processing_layer.request_bodies_manager import set_request_bodies
import re
import logging
import pandas as pd
from utils.db_utils import push_steps_df
from services.integration_layer.github_integration.github_integration_jenkins_trigger import main
from utils.excepations import UserInputError, SystemError, GitHubIntegrationError
from pathlib import Path
from utils.analytics_tracker import track_execution

import traceback
from typing import Optional
logger = logging.getLogger(__name__)


def _extract_recording_zip_from_exc(exc: Exception) -> Optional[str]:
    """Extract recording zip path/URL from an exception.
    Prefers attribute 'recording_zip'; falls back to parsing message tag 'RECORDING_ZIP: ...'.
    """
    try:
        if hasattr(exc, "recording_zip"):
            val = getattr(exc, "recording_zip", None)
            if isinstance(val, str) and val.strip():
                return val
        if exc.args:
            for arg in exc.args:
                if isinstance(arg, str) and "RECORDING_ZIP:" in arg:
                    return arg.split("RECORDING_ZIP:", 1)[1].strip()
    except Exception:
        pass
    return None


class WindowStateManager:
    """Manages window detection state for a single scenario execution"""

    def __init__(self, scenario_number=None):
        self.is_detected = False
        self.page_count = 0
        self.scenario_number = scenario_number
        self.execution_id = None
        self.window_type = None  # 'popup', 'new_tab', or None
        self.popup_detected = False
        self.new_tab_detected = False

    def update_state(self, pages_count: int, window_type: str = None):
        """Update window detection based on page count and window type"""
        self.page_count = pages_count
        previous_state = self.is_detected
        self.is_detected = pages_count > 1

        # Track specific window types
        if window_type:
            self.window_type = window_type
            if window_type == 'popup':
                self.popup_detected = True
            elif window_type == 'new_tab':
                self.new_tab_detected = True

        if previous_state != self.is_detected:
            logger.info(
                f"🔄 Scenario {self.scenario_number}: Window state changed {previous_state} → {self.is_detected}, type: {self.window_type}")

        return self.is_detected

    def mark_popup_detected(self):
        """Mark that a popup window was detected"""
        self.popup_detected = True
        self.window_type = 'popup'
        self.is_detected = True
        logger.info(f"🪟 Scenario {self.scenario_number}: Popup window detected")

    def mark_new_tab_detected(self):
        """Mark that a new tab was detected"""
        self.new_tab_detected = True
        self.window_type = 'new_tab'
        self.is_detected = True
        logger.info(f"🗂️ Scenario {self.scenario_number}: New tab detected")

    def reset_detection(self):
        """Reset detection flags for new action"""
        self.window_type = None
        # Don't reset popup_detected/new_tab_detected as they track overall scenario state

    def __str__(self):
        return f"WindowState(detected={self.is_detected}, pages={self.page_count}, type={self.window_type}, popup={self.popup_detected}, new_tab={self.new_tab_detected})"


def extract_and_validate_first_tag(feature_content):
    """
    Extract the first tag from feature file content.

    Args:
        feature_content (str): The feature file content

    Returns:
        dict: Results with the first tag if found

    Raises:
        ValueError: If no tag is found
    """
    try:
        # Find all tags using regex
        tag_pattern = r'@([^\s@]+)'
        tags = re.findall(tag_pattern, feature_content)

        if not tags:
            error_msg = "No tags found in feature file"
            logger.error(error_msg)
            raise ValueError(error_msg)

        first_tag = tags[0]  # Get the first tag

        logger.info(f"First tag found: @{first_tag}")
        parts = first_tag.split('-', 1)
        first_part = parts[0]

        # Return the first tag without complex validations
        success_msg = f"✓ Tag extraction successful: @{first_tag}"
        logger.info(success_msg)

        return {
            'first_part': first_part,
            'tag': first_tag
        }

    except ValueError:
        raise
    except Exception as e:
        error_msg = f"Error parsing feature file: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg)



def extract_scenario_tags(content: str) -> dict:
     scenario_tags = {}
     lines = content.split('\n')
     current_tags = []  # Store all tags as a list
     scenario_num = 0
     feature_found = False
     
     for line in lines:
         stripped = line.strip()
         
         # Track when we've passed the feature line
         if stripped.lower().startswith('feature:'):
             feature_found = True
             continue
         
         # Only look for tags AFTER feature line but BEFORE any scenario content
         if feature_found:
             # Check if line is ONLY tags (starts with @ and contains no other text like Given/When/Then)
             if stripped.startswith('@') and not any(keyword in stripped.lower() for keyword in ['given', 'when', 'then', 'and', 'but']):
                 # This is a tag line - extract ALL tags on this line
                 # Tags are space-separated, each starting with @
                 tags_on_line = [tag for tag in stripped.split() if tag.startswith('@')]
                 current_tags.extend(tags_on_line)
             # Check if this is a scenario line
             elif stripped.lower().startswith('scenario outline:') or stripped.lower().startswith('scenario:'):
                 scenario_num += 1
                 if current_tags:
                     # Use all tags found before this scenario, comma-separated (consistent with feature_tags)
                     scenario_tags[scenario_num] = ','.join(current_tags)
                 else:
                     # No tags found, generate default
                     scenario_tags[scenario_num] = f"@scenario{scenario_num}"
                 current_tags = []  # Reset for next scenario
             # If we hit a step keyword (Given/When/Then), we're past the tag section
             # Reset current_tags to avoid picking up tags from within steps
             elif any(stripped.lower().startswith(keyword) for keyword in ['given ', 'when ', 'then ', 'and ', 'but ']):
                 # We're in the steps section now, ignore any @ that appears in step text
                 current_tags = []
     
     return scenario_tags


def split_scenarios(input_text):
    scenarios = []
    lines = input_text.split('\n')
    current_scenario = None

    for line in lines:
        stripped_line = line.strip()
        if stripped_line.lower().startswith("scenario outline:") or stripped_line.lower().startswith(
            "scenario:"
        ):
            if current_scenario:
                scenarios.append(current_scenario)
            current_scenario = {
                "Scenario": re.sub(r"(?i)^scenario(?:\s+outline)?:\s*", "", stripped_line).strip(),
                "steps": []
            }
        elif current_scenario is not None and stripped_line:
            # Skip lines that are just tags (start with @ and don't contain step keywords)
            if stripped_line.startswith('@') and not any(keyword in stripped_line.lower() for keyword in ['given ', 'when ', 'then ', 'and ', 'but ']):
                # This is a tag line, not a step - skip it
                continue
            current_scenario["steps"].append(stripped_line)

    if current_scenario:
        scenarios.append(current_scenario)

    return scenarios


def _parse_table_row(line: str) -> [str]:
    """
    Parse a Gherkin-style table row like:
      | First and Last Name  | Playwright Test |
    Returns a list of trimmed cell values. Empty cells are returned as empty strings.
    """
    try:
        parts = [p.strip() for p in line.strip().split('|')]
        # split gives leading/trailing empty entries for lines that start/end with |
        # filter only by position: keep internal cells
        cells = [c for c in parts[1:-1]]
        return cells
    except Exception:
        return []


# Bulk form: intro step ends with ':' and is immediately followed by a | field | value | table (validated below).
# Matches any wording (shipping, payment, following …, etc.); optional "the" before "user".
# Excludes lines that already look like a single quoted field step: "…" as "…"
_BULK_FORM_INTRO_RE = re.compile(
    r"(?i)^(?P<kw>given|when|then|and)\s+(?:the\s+)?user\s+(?:enters?|fills?|types?|inputs?)\s+.+:\s*$"
)
_SINGLE_FIELD_ENTER_QUOTED_AS_RE = re.compile(r'(?i)"[^"]*"\s+as\s+"')


def _match_bulk_form_intro(line: str):
    line = (line or "").strip()
    if _SINGLE_FIELD_ENTER_QUOTED_AS_RE.search(line):
        return None
    return _BULK_FORM_INTRO_RE.match(line)


def _normalize_bulk_form_steps(steps: list) -> list:
    """
    Expands a bulk | field | value | table (Gherkin) into per-field steps:

      <Kw> user enters following details:
        | field | value |
        | Name  | John  |
    ->  <Kw> user enters "Name" as "John"

    <Kw> is preserved (Given/When/Then/And). Intro matches user/fill/type/input … ending with ':',
    excluding steps that already look like quoted single-field enters ("Field" as "value").
    The next line must be a pipe table with field and value headers.

    Select vs enter:
      - If field is in SELECT_FIELDS -> 'user select "<Field>" as "<Value>"'
      - Otherwise -> 'user enters "<Field>" as "<Value>"'

    Multiple bulk blocks per scenario are supported. Row order is preserved.
    """
    if not isinstance(steps, list) or not steps:
        return steps

    SELECT_FIELDS = {"State", "Country"}

    out = []
    i = 0
    while i < len(steps):
        line = (steps[i] or "").strip()
        m_bulk = _match_bulk_form_intro(line)
        if m_bulk:
            _kw = m_bulk.group("kw").lower()
            _step_kw = {"given": "Given", "when": "When", "then": "Then", "and": "And"}.get(_kw, "Then")
            # Expect a header row next starting with '|'
            header_idx = i + 1
            if header_idx >= len(steps) or not steps[header_idx].strip().startswith("|"):
                # Malformed table; keep original line and continue
                out.append(line)
                i += 1
                continue

            header_cells = _parse_table_row(steps[header_idx])
            # Map headers to indices; enforce 'field' and 'value'
            header_lc = [h.lower() for h in header_cells]
            try:
                field_col = header_lc.index("field")
                value_col = header_lc.index("value")
            except ValueError:
                # Headers not as expected; emit original line and continue
                out.append(line)
                i += 1
                continue

            # Collect data rows
            data_idx = header_idx + 1
            while data_idx < len(steps) and steps[data_idx].strip().startswith("|"):
                row_cells = _parse_table_row(steps[data_idx])
                # Guard for short/empty rows
                if row_cells and max(field_col, value_col) < len(row_cells):
                    field_name = (row_cells[field_col] or "").strip()
                    field_value = (row_cells[value_col] or "").strip()
                    if field_name:
                        if field_name in SELECT_FIELDS:
                            out.append(f'{_step_kw} user select "{field_name}" as "{field_value}"')
                        else:
                            out.append(f'{_step_kw} user enters "{field_name}" as "{field_value}"')
                data_idx += 1

            # Skip original bulk line + table block
            i = data_idx
            continue

        # Default: pass-through
        out.append(line)
        i += 1

    return out


def prepare_scenario_transcript_steps(raw_steps: list):
    """
    Single pipeline for executable Gherkin steps: strip Examples, substitute first Examples row,
    expand bulk field/value tables, expand bulk "verifies following details" tables.
    Use this anywhere scenario steps are materialized (generation, API metadata, previews)
    so behavior stays consistent.
    Returns (transcript_steps, datasets) where datasets is the list of Example row dicts (may be empty).
    """
    steps_wo_examples, datasets = _extract_examples_from_steps(raw_steps or [])
    transcript_steps = steps_wo_examples
    if datasets:
        try:
            transcript_steps = _substitute_placeholders_in_steps(steps_wo_examples, datasets[0])
        except Exception as sub_err:
            logger.warning(f"Placeholder substitution failed, proceeding with original steps: {sub_err}")
            transcript_steps = steps_wo_examples
    try:
        transcript_steps = _normalize_bulk_form_steps(transcript_steps or [])
    except Exception as norm_err:
        logger.warning(f"Bulk-step normalization failed, proceeding without changes: {norm_err}")
    try:
        transcript_steps = _normalize_bulk_verify_steps(transcript_steps or [])
    except Exception as verify_norm_err:
        logger.warning(
            f"Bulk verify-step normalization failed, proceeding without changes: {verify_norm_err}"
        )
    return transcript_steps or [], datasets

def _bdd_escape_quoted_fragment(text: str) -> str:
    """Escape backslashes and double quotes for embedding inside a Gherkin double-quoted fragment."""
    if not isinstance(text, str):
        text = str(text or "")
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_bulk_verify_steps(steps: list) -> list:
    """
    Expands bulk verify tables after:
      Then user verifies following details:

    (1) Two columns | field | value | — Rule A:
        | Name  |       |  -> verify "Name" (empty value -> use field)
        | Msg   | Hello |  -> verify "Hello" (non-empty value -> use value)

    (2) Single column | value | — each data row is the text to verify:
        | Shipped    |
        | Processing |

    Multiple bulk blocks per scenario are supported. Row order is preserved.
    """
    if not isinstance(steps, list) or not steps:
        return steps

    out = []
    i = 0
    while i < len(steps):
        line = (steps[i] or "").strip()
        if re.match(
            r"(?i)^(?:given|when|then|and)\s+user\s+verifies\s+following\s+details:?\s*$",
            line,
        ):
            header_idx = i + 1
            if header_idx >= len(steps) or not steps[header_idx].strip().startswith("|"):
                out.append(line)
                i += 1
                continue

            header_cells = _parse_table_row(steps[header_idx])
            header_lc = [(h or "").strip().lower() for h in header_cells]
            # Drop trailing empty header cells from padding
            while len(header_lc) > 1 and header_lc[-1] == "":
                header_lc.pop()

            has_field = "field" in header_lc
            has_value = "value" in header_lc
            single_value_only = (
                len(header_lc) == 1
                and header_lc[0] == "value"
            )

            if not single_value_only and has_field and has_value:
                try:
                    field_col = header_lc.index("field")
                    value_col = header_lc.index("value")
                except ValueError:
                    out.append(line)
                    i += 1
                    continue

                data_idx = header_idx + 1
                while data_idx < len(steps) and steps[data_idx].strip().startswith("|"):
                    row_cells = _parse_table_row(steps[data_idx])
                    if row_cells and max(field_col, value_col) < len(row_cells):
                        field_name = (row_cells[field_col] or "").strip()
                        field_value = (row_cells[value_col] or "").strip()
                        if field_value:
                            display_text = field_value
                        elif field_name:
                            display_text = field_name
                        else:
                            data_idx += 1
                            continue
                        escaped = _bdd_escape_quoted_fragment(display_text)
                        out.append(f'Then user verifies that "{escaped}" is displayed')
                    data_idx += 1

                i = data_idx
                continue

            if single_value_only:
                value_col = 0
                data_idx = header_idx + 1
                while data_idx < len(steps) and steps[data_idx].strip().startswith("|"):
                    row_cells = _parse_table_row(steps[data_idx])
                    if row_cells and value_col < len(row_cells):
                        cell = (row_cells[value_col] or "").strip()
                        if cell:
                            escaped = _bdd_escape_quoted_fragment(cell)
                            out.append(f'Then user verifies that "{escaped}" is displayed')
                    data_idx += 1

                i = data_idx
                continue

            out.append(line)
            i += 1
            continue

        out.append(line)
        i += 1

    return out



def _extract_examples_from_steps(steps: list) -> (list, list):
    """
    Extract an Examples: block from steps (if present).
    Returns (steps_without_examples, datasets_list_of_dicts).
    """
    if not isinstance(steps, list) or not steps:
        return steps, []

    steps_out = []
    datasets = []
    i = 0
    while i < len(steps):
        line = (steps[i] or "").strip()
        if re.match(r"(?i)^examples:\s*$", line):
            header = None
            i += 1
            while i < len(steps) and steps[i].strip().startswith("|"):
                row_cells = _parse_table_row(steps[i])
                if row_cells:
                    if header is None:
                        header = row_cells
                    else:
                        row_map = {}
                        for idx, key in enumerate(header):
                            row_map[str(key)] = str(row_cells[idx]) if idx < len(row_cells) else ""
                        datasets.append(row_map)
                i += 1
            # Skip Examples block entirely
            continue
        else:
            steps_out.append(steps[i])
            i += 1
    return steps_out, datasets


def _substitute_placeholders_in_line(line: str, mapping: dict) -> str:
    if not isinstance(line, str) or not mapping:
        return line

    def repl(m):
        key = m.group(1)
        return str(mapping.get(key, m.group(0)))

    return re.sub(r"<([^>]+)>", repl, line)


def _substitute_placeholders_in_steps(steps: list, mapping: dict) -> list:
    if not isinstance(steps, list) or not steps or not mapping:
        return steps
    return [_substitute_placeholders_in_line(s, mapping) for s in steps]

def _filename_from_url(maybe_url: str) -> str:
    try:
        from urllib.parse import urlparse
        import os as _os
        if isinstance(maybe_url, str) and maybe_url:
            if maybe_url.startswith(("http://", "https://", "s3://", "file:")):
                parsed = urlparse(maybe_url)
                return _os.path.basename(parsed.path) or "recording.zip"
            if maybe_url.startswith("/recordings/") or maybe_url.startswith("recordings/"):
                return maybe_url.rsplit('/', 1)[-1]
            return maybe_url
    except Exception:
        pass
    return "recording.zip"


def _sanitize_processing_summary(ps: dict) -> dict:
    """Return a copy of processing summary with URLs removed from recordings (filenames only)."""
    try:
        if not isinstance(ps, dict):
            return ps
        ps_copy = dict(ps)
        # Sanitize scenario_details: strip URLs and RECORDING_ZIP from 'error' and 'message'
        def _clean_text(txt: str) -> str:
            import re as _re
            t = str(txt or "")
            t = _re.sub(r"RECORDING_ZIP:\s*\S+", "", t)
            t = _re.sub(r"(https?://\S+|s3://\S+|file://\S+)", "", t)
            t = _re.sub(r"\s{2,}", " ", t).strip()
            return t
        clean_details = []
        for s in ps.get("scenario_details") or []:
            if not isinstance(s, dict):
                continue
            cleaned = dict(s)
            if "error" in cleaned:
                cleaned["error"] = _clean_text(cleaned.get("error"))
            if "message" in cleaned:
                cleaned["message"] = _clean_text(cleaned.get("message"))
            clean_details.append(cleaned)
        if clean_details:
            ps_copy["scenario_details"] = clean_details
        def _map_list(arr):
            out = []
            for r in (arr or []):
                scen = r.get("scenario_number")
                url = r.get("zip_url")
                out.append({"scenario_number": scen, "zip_filename": _filename_from_url(url)})
            return out
        ps_copy["success_recordings"] = _map_list(ps.get("success_recordings"))
        ps_copy["failed_recordings"] = _map_list(ps.get("failed_recordings"))
        return ps_copy
    except Exception:
        return ps


def _friendly_error_message(raw: str) -> str:
    """
    Convert a technical error string into a short, user-friendly reason.
    Keeps it high-level and avoids stack traces.
    """
    try:
        import re
        text = str(raw or "").strip()
        # Remove recording tokens/URLs first so they don't leak into the user message
        text = re.sub(r"RECORDING_ZIP:\s*\S+", "", text)
        text = re.sub(r"(https?://\S+|s3://\S+|file://\S+)", "", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        # Keep only the first line/sentence to avoid stack traces
        first_line = text.splitlines()[0] if "\n" in text else text
        lowered = first_line.lower()

        if "timeout" in lowered:
            return "Step timed out while waiting for the page or action to complete."
        if "locator" in lowered or "element not found" in lowered or "no such element" in lowered:
            return "Couldn’t find the expected element on the page."
        if "unauthorized" in lowered or "forbidden" in lowered or "401" in lowered or "403" in lowered:
            return "Authentication or permission issue during the step."
        if "network" in lowered or "econn" in lowered or "dns" in lowered or "connection" in lowered:
            return "Network issue occurred during the step."
        if "assert" in lowered or "expect" in lowered or "mismatch" in lowered:
            return "The expected result did not match the actual result."

        # Fallback: concise version of the first line
        return (first_line[:180] + "…") if len(first_line) > 180 else first_line
    except Exception:
        return "Step failed due to an unexpected error."


def _sanitize_error(raw: str) -> str:
    """
    Remove recording links or tags (e.g., 'RECORDING_ZIP: ...') from technical error text
    to avoid leaking internal URLs in notifications.
    """
    try:
        import re
        text = str(raw or "")
        # Remove 'RECORDING_ZIP: <anything-nonspace>' tokens
        text = re.sub(r"RECORDING_ZIP:\s*\S+", "", text)
        # Remove generic URLs (http, https, s3, file)
        text = re.sub(r"(https?://\S+|s3://\S+|file://\S+)", "", text)
        # Collapse double spaces introduced by removal
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text
    except Exception:
        return str(raw or "")


async def process_feature_files(content, jira_id, s3_path, file_version, push_to_github, selected_domain, target_github_repo, jenkins_project_name, github_access_token, jenkins_username, jenkins_api_token, record_video: bool = False, generate_step_definitions=True, dry_run_strategy: bool = False, custom_branch_base: Optional[str] = None, repo_base_path: str = "src/tests", base_branch_override: str = "main", run_id: Optional[str] = None, feature_tags: Optional[str] = None, feature_nm: Optional[str] = None, scn_tags: Optional[str] = None, scn_nm: Optional[str] = None, api_steps: Optional[str] = None):
    """
    Processes feature files by reading configuration properties, merging them,
    and generating scripts based on the BDD feature files.
    
    Args:
        run_id: Optional run ID for status tracking in tran_status table

    Returns:
        Dict: Processing result with success status, step definition file, and GitHub details
    """
    # Note: Analytics will be tracked per-scenario, not per-feature
    
    # Import status tracking functions only when needed
    if run_id:
        try:
            from utils.run_id_manager import (
                init_tran_status, append_step_to_tran_status, 
                mark_dry_run_complete, mark_script_generation_complete, 
                delete_tran_status, reset_tran_status_steps,
                store_generated_response, update_complete_scripts_for_runid
            )
            status_tracking_available = True
        except ImportError:
            logger.warning("⚠️  Status tracking unavailable - continuing without tran_status updates")
            status_tracking_available = False
    else:
        status_tracking_available = False
    
    # ✅ FIX: Clear all global state at test run level (not per-scenario)
    # This clears AWS cache to reset for fresh test run
    from services.integration_layer.playwright_script_generation.processing_layer.playwright_main import _cleanup_global_state
    try:
        _cleanup_global_state(preserve_aws_cache=False)
        logger.info("🧹 Test run state initialized: All global state cleared including AWS secret cache")
    except Exception as e:
        logger.warning(f"⚠️ Could not initialize test run state: {e}")
    
    base = os.path.dirname(os.path.abspath(__file__))
    parent_base = os.path.dirname(os.path.dirname(base))
    root = os.path.dirname(os.path.dirname(parent_base))
    project_root = os.path.dirname(root)
    json_dir = os.path.join(project_root, "target/json")
    json_dir = Path(json_dir)
    logger.info(f"json_dir: {json_dir}")

    version = None
    try:
        validated_dict = extract_and_validate_first_tag(content)
        # Extract feature tag and description for analytics
        feature_tag = validated_dict.get('tag')  # e.g., "webui-test5"
        feature_description_line = extract_first_feature(content)  # e.g., "Feature: Place Guest Order..."
        feature_description = None
        if feature_description_line and feature_description_line.startswith("Feature:"):
            # Remove "Feature:" prefix and clean up
            feature_description = feature_description_line.replace("Feature:", "").strip()
        
        logger.info(f"📊 Feature metadata - Tag: {feature_tag}, Description: {feature_description}")
        
    except ValueError as tag_error:
        logger.error(f"❌ Tag validation failed: {str(tag_error)}")
        raise UserInputError(
            message="Feature file tag format is invalid",
            error_code="INVALID_TAG_FORMAT",
            context={"jira_id": jira_id, "tag_error": str(tag_error)}
        )
    except Exception as e:
        logger.error(f"❌ Unexpected error during tag validation: {str(e)}")
        raise SystemError(
            message=f"Unexpected error during tag validation: {str(e)}",
            error_code="FEATURE_PROCESSING_FAILED",
            context={"jira_id": jira_id, "stage": "tag_validation"}
        )
    # Load request bodies files from json_dir
    request_bodies = {}
    try:
        if json_dir.exists():
            # Look for all JSON files except request_bodies.json (output file)
            matching_files = list(json_dir.glob("*.json"))

            for json_file in matching_files:
                # Skip request_bodies.json as it's the output file, not an input config
                if json_file.name == "request_bodies.json":
                    logger.info(f"Skipping output file: {json_file.name}")
                    continue
                    
                # Use filename without extension as config name
                config_name = json_file.stem  # Remove .json extension
                
                try:
                    with open(json_file, 'r', encoding='utf-8') as f:
                        file_content = json.load(f)
                        request_bodies[config_name] = file_content
                        logger.info(f"Loaded {config_name} from: {json_file}")
                except json.JSONDecodeError as e:
                    logger.error(f"JSON decode error in {json_file}: {e}")
                    continue
                except Exception as e:
                    logger.error(f"Error loading {json_file}: {e}")
                    continue

            if not request_bodies:
                logger.info(f"No JSON config files found in {json_dir}")
        else:
            logger.info(f"JSON directory not found: {json_dir}")
    except Exception as e:
        logger.info(f"Warning: Could not load request bodies files: {str(e)}")
    # Set the request bodies globally for access during processing
    set_request_bodies(request_bodies if request_bodies else None)
    # Split the scenarios
    feature = extract_first_feature(content)
    scenarios = split_scenarios(content)
    step_definition_file = ""

    scenario_results = []
    success_recordings = []
    failed_recordings = []
    successful_scenarios = 0
    failed_scenarios = 0
    total_scenarios = len(scenarios)
    scenarios_stored_in_db = 0  # Track how many scenarios were actually stored in t_gen_resp
    
    # === BUILD FEATURE_STEPS JSON for database storage ===
    feature_steps_json_text = None
    try:
        import json as _json_steps
        out_steps = []
        for sc in (scenarios or []):
            name = sc.get("Scenario", "")
            raw_steps = sc.get("steps", []) or []
            expanded, _datasets_unused = prepare_scenario_transcript_steps(raw_steps)

            def _is_step_line(s: str) -> bool:
                s0 = (s or "").strip().lower()
                return s0.startswith(("given ", "when ", "then ", "and ", "but "))

            filtered = [s for s in (expanded or []) if _is_step_line(s)]
            out_steps.append({"scenario": name, "steps": filtered})
        feature_steps_json_text = _json_steps.dumps(out_steps, ensure_ascii=False)
        logger.info(f"📝 Built feature_steps JSON ({len(feature_steps_json_text)} chars) for {len(out_steps)} scenarios")
    except Exception as fs_err:
        logger.warning(f"⚠️  Failed to build feature_steps JSON: {fs_err}")
        feature_steps_json_text = None
    
    # Extract scenario tags before processing
    scenario_tags_map = extract_scenario_tags(content)
    
    # Initialize per-scenario tracking array
    scenarios_data = []
    
    i=1
    scenario_dict ={}

    for scenario in scenarios:
        # Extract scenario metadata
        scenario_description = scenario.get("Scenario", f"Scenario {i}")
        scenario_tag = scenario_tags_map.get(i, f"@scenario{i}")
        
        logger.info(f"Processing scenario {i}/{total_scenarios}: {scenario_tag} - {scenario_description}")
        
        
        # Create per-scenario analytics tracker
        scenario_analytics = track_execution(
            jira_id=jira_id,
            selected_domain=selected_domain,
            total_scenarios=total_scenarios,
            scenario_tag=scenario_tag,
            scenario_description=scenario_description
        )
        
        # Set execution configuration
        scenario_analytics.set_execution_mode(
            push_to_github=push_to_github,
            generate_step_definitions=generate_step_definitions,
            record_video=record_video
        )
        scenario_analytics.set_crawler_type(dry_run_strategy=dry_run_strategy)
        scenario_analytics.set_feature_metadata(
            feature_tag=feature_tag,
            feature_description=feature_description
        )
        
        raw_steps = scenario.get("steps", []) or []
        transcript_steps, datasets = prepare_scenario_transcript_steps(raw_steps)
        try:
            logger.info(f"📊 Examples detected: {len(datasets)} rows" if datasets else "📊 No Examples detected")
            if datasets:
                logger.debug(f"📊 Examples rows preview: {datasets[:3]}")
        except Exception:
            pass
        # Datasets will be embedded inline for non-BDD test generation; no sidecar file is written

        # Use transcript_steps for the LLM pipeline and execution alignment
        test_lst = transcript_steps

        # Build a scenario dict for the transcript run (no Examples block; substituted if dataset present)
        scenario_for_transcript = {
            "Scenario": scenario.get("Scenario", ""),
            "steps": transcript_steps
        }

        scenario_part = extract_scenario_steps(scenario_for_transcript)
        feature_text = feature if feature is not None else ""
        scenario_text = scenario_part if scenario_part is not None else ""
        combine_feature_scenario_part = feature_text + "\n" + scenario_text
        logger.info(combine_feature_scenario_part)
        window_state_manager = WindowStateManager(scenario_number=i)


        if request_bodies:
            request_bodies_file_path = os.path.join(json_dir, "request_bodies.json")
            with open(request_bodies_file_path, "w") as file:
                json.dump(request_bodies, file, indent=2)
            logger.info(f"Saved request bodies file: {request_bodies_file_path}")


        scenario_str = str(scenario_for_transcript).replace("{", "{{").replace("}", "}}")
        logger.info(scenario_str)
        
        # Initialize tran_status for MCP flow (only for first scenario)
        if i == 1 and dry_run_strategy and status_tracking_available and run_id:
            try:
                init_tran_status(run_id, selected_domain, scenario_description)
                logger.info(f"🎬 Status tracking initialized for run_id: {run_id}")
            except Exception as e:
                logger.warning(f"⚠️  Failed to initialize status tracking: {e}")
        elif i > 1 and dry_run_strategy and status_tracking_available and run_id:
            # Reset tran_status for subsequent scenarios to clear previous scenario steps
            try:
                reset_tran_status_steps(run_id, scenario_description)
                logger.info(f"🔄 Status tracking reset for scenario {i} ('{scenario_description}'), run_id: {run_id}")
            except Exception as e:
                logger.warning(f"⚠️  Failed to reset status tracking: {e}")
        
        try:
            # Choose crawler based on dry_run_strategy parameter
            if dry_run_strategy:
                # Use MCP crawler with locator reuse
                logger.info(f"🎯 Using Enhanced MCP crawler with locator reuse for scenario {i} | Domain: {selected_domain}")
                from services.integration_layer.mcp_integration.mcp_enhanced_utilization import UiAutomationServiceWithLocatorReuse
                mcp_service = UiAutomationServiceWithLocatorReuse(domain_selection=selected_domain)
                result = await mcp_service.chat(
                    step_lst=test_lst,
                    record_video=record_video,
                    jira_id=jira_id,
                    generate_step_definitions=generate_step_definitions,
                    current_script=step_definition_file,
                    scenario_number=i,
                    scenario_dict=scenario_dict,
                    datasets=datasets,
                    run_id=run_id  # NEW: Pass run_id for status tracking
                )
                
                # Log locator reuse performance statistics
                try:
                    stats = mcp_service.get_locator_reuse_stats()
                    if stats['total_attempts'] > 0:
                        logger.info(f"📊 LOCATOR REUSE STATS: {stats['reuse_success']}/{stats['total_attempts']} success "
                                  f"({stats.get('success_rate', 0):.1f}%) | Time saved: {stats.get('performance_improvement', 0):.1f}s")
                except Exception as e:
                    logger.debug(f"Could not retrieve locator reuse stats: {e}")
            else:
                # Use Javascript crawler (default)
                logger.info(f"🎯 Using Javascript crawler for scenario {i}")
                result = await crawl(
                    scenario_str, i,
                    jira_id=jira_id,
                    test_lst=test_lst,
                    scenario_dict=scenario_dict,
                    new_window_detected=window_state_manager,
                    current_script=step_definition_file,
                    record_video=record_video,
                    generate_step_definitions=generate_step_definitions,
                    datasets=datasets
                )

            # Initialize recording_zip to None before conditional assignment
            recording_zip = None
            if isinstance(result, tuple):
                step_definition_file, recording_zip = result
                scenario_results.append({
                    "scenario_number": i,
                    "status": "success",
                    "message": "Scenario processed successfully",
                    "recording_zip": recording_zip
                })
                success_recordings.append({"scenario_number": i, "zip_url": recording_zip})
            else:
                step_definition_file = result
                scenario_results.append({
                    "scenario_number": i,
                    "status": "success",
                    "message": "Scenario processed successfully"
                })

            # Mark success in analytics
            scenario_analytics.script_gen_success = True
            scenario_analytics.reexecution_success = True
            
            # Save analytics immediately after scenario completes
            scenario_analytics.save_to_database()
            
            # Extract successful steps from tran_status for modal display
            successful_steps_list = []
            if dry_run_strategy and status_tracking_available and run_id:
                try:
                    from utils.run_id_manager import get_tran_status_steps
                    successful_steps_list = get_tran_status_steps(run_id)
                    logger.info(f"📊 SUCCESS: Extracted {len(successful_steps_list)} steps for scenario {i}")
                except Exception as get_steps_err:
                    logger.warning(f"⚠️  Could not extract steps for success scenario: {get_steps_err}")
            
            # Store per-scenario success data for execution-details modal
            scenario_data = {
                "scenario_number": i,
                "scenario_name": scenario_description,
                "status": "passed",
                "successful_steps": successful_steps_list
            }
            scenarios_data.append(scenario_data)
            
            # === STORE PER-SCENARIO SUCCESS IN t_gen_resp ===
            if dry_run_strategy and status_tracking_available and run_id:
                try:
                    scenario_response = {
                        "run_id": run_id,
                        "scenario_number": i,
                        "scenario_name": scenario_description,
                        "status": "PASSED",
                        "successful_steps": successful_steps_list,
                        "jira_id": jira_id
                    }
                    # Extract video filename from recording_zip URL if available
                    scenario_video_filename = None
                    if recording_zip:
                        scenario_video_filename = _filename_from_url(recording_zip)
                        logger.info(f"📹 Video filename for scenario {i}: {scenario_video_filename}")
                    
                    # Build step-locator map from scenario DataFrame
                    # Pass raw data to PostgreSQL and let it handle JSON conversion
                    stp_loc_map_json = None
                    try:
                        scenario_key = f"Scenario_{i}"
                        if scenario_key in scenario_dict and scenario_dict[scenario_key] is not None:
                            df = scenario_dict[scenario_key]
                            if not df.empty and 'original_bdd_step' in df.columns and 'playwrightLocator' in df.columns:
                                # Extract raw data from DataFrame without modification
                                steps_list = []
                                for _, row in df.iterrows():
                                    step_name = str(row.get('original_bdd_step', '')).strip()
                                    locator = row.get('playwrightLocator', '')
                                    # Keep original values - let PostgreSQL handle None/null
                                    if step_name:
                                        steps_list.append({
                                            'stepname': step_name,
                                            'locator': locator  # Pass as-is (None, string, etc.)
                                        })
                                if steps_list:
                                    # Let PostgreSQL handle JSON conversion and null handling
                                    stp_loc_map_json = json.dumps({'steps': steps_list}, ensure_ascii=False)
                                    logger.info(f"📍 Built step-locator map with {len(steps_list)} steps for scenario {i}")
                    except Exception as stp_err:
                        logger.warning(f"⚠️  Failed to build step-locator map for scenario {i}: {stp_err}")
                    
                    store_success = store_generated_response(
                        run_id=run_id,
                        response_data=scenario_response,
                        jira_id=jira_id,
                        module_name=selected_domain,  # Use user-selected domain/module
                        status="PASSED",
                        scenario_name=scenario_description,
                        video_filename=scenario_video_filename,
                        complete_scripts=step_definition_file if step_definition_file else None,
                        feature_steps=feature_steps_json_text,
                        feature_tags=feature_tags,  # Use passed-in metadata
                        feature_nm=feature_nm,  # Use passed-in metadata
                        scn_tags=scenario_tag,  # Use per-scenario tag extracted from feature file
                        scn_nm=scenario_description,  # Use actual scenario name from this scenario
                        stp_loc_map=stp_loc_map_json,  # Step-locator map from MCP results
                        api_steps=api_steps  # Use passed-in metadata
                    )
                    if store_success:
                        scenarios_stored_in_db += 1
                        logger.info(f"💾 Scenario {i} '{scenario_description}' stored in t_gen_resp (run_id: {run_id}, module: {selected_domain}, video: {scenario_video_filename})")
                    else:
                        logger.warning(f"⚠️  Failed to store scenario {i} in t_gen_resp")
                except Exception as store_err:
                    logger.warning(f"⚠️  Error storing scenario {i} in t_gen_resp: {store_err}")
            
            # Mark script generation complete for MCP (only on last scenario)
            if i == total_scenarios and dry_run_strategy and status_tracking_available and run_id:
                try:
                    mark_script_generation_complete(run_id)
                    logger.info(f"✨ Script generation marked complete for run_id: {run_id}")
                except Exception as e:
                    logger.warning(f"⚠️  Failed to mark script generation complete: {e}")
            
            successful_scenarios += 1
            logger.info(f"✅ Scenario {i} completed successfully")
        except Exception as e:
            logger.error(f"❌ Error processing scenario {i}: {str(e)}\n{traceback.format_exc()}")
            failed_scenarios += 1
            error_details = str(e)
            
            # === FIRST: Extract successful steps from tran_status BEFORE any deletion ===
            successful_steps_before_failure = []
            if dry_run_strategy and status_tracking_available and run_id:
                try:
                    from utils.run_id_manager import get_tran_status_steps
                    successful_steps_before_failure = get_tran_status_steps(run_id)
                    logger.info(f"📊 Extracted {len(successful_steps_before_failure)} steps before failure for scenario {i}")
                except Exception as get_steps_err:
                    logger.warning(f"⚠️  Could not extract steps before failure: {get_steps_err}")

            # Preserve partial script from MCP failure context for downstream artifacts.
            try:
                context = getattr(e, "context", None)
                partial_script = context.get("step_definition_file") if isinstance(context, dict) else None
                if isinstance(partial_script, str) and partial_script.strip():
                    step_definition_file = partial_script
                    logger.info(f"🧩 Updated step_definition_file from failure context (len={len(partial_script)})")
            except Exception as context_err:
                logger.warning(f"⚠️  Failed to read step_definition_file from failure context: {context_err}")
            
            # Mark failure in analytics
            scenario_analytics.script_gen_success = False
            scenario_analytics.reexecution_success = False
            scenario_analytics.set_error(error_details)
            
            # Save failed scenario analytics
            scenario_analytics.save_to_database()
            
            # Try to capture failed scenario recording zip (if any)
            try:
                rec = _extract_recording_zip_from_exc(e)
                if rec:
                    failed_recordings.append({"scenario_number": i, "zip_url": rec})
            except Exception:
                pass
            
            # If scenario has 0 steps, it means it never started (likely due to previous failure)
            # Use a clearer error message in this case
            if len(successful_steps_before_failure) == 0 and i > 1:
                scenario_error_message = f"Scenario {i} did not execute - previous scenario failure may have affected the test environment"
            else:
                scenario_error_message = error_details[:500]
            
            scenario_results.append({
                "scenario_number": i,
                "status": "failed",
                "error": error_details,
                "message": f"Scenario processing failed: {error_details[:100]}...",
                "user_reason": _friendly_error_message(error_details),
                "technical_error": _sanitize_error(error_details)
            })
            
            # Store per-scenario failure data for execution-details modal
            scenario_data = {
                "scenario_number": i,
                "scenario_name": scenario_description,
                "status": "failed",
                "successful_steps": successful_steps_before_failure,
                "error_message": scenario_error_message[:200],
                "error_stage": "execution"
            }
            scenarios_data.append(scenario_data)
            
            # === STORE PER-SCENARIO FAILURE IN t_gen_resp ===
            if dry_run_strategy and status_tracking_available and run_id:
                try:
                    scenario_response = {
                        "run_id": run_id,
                        "scenario_number": i,
                        "scenario_name": scenario_description,
                        "status": "FAILED",
                        "successful_steps": successful_steps_before_failure,
                        "error_message": scenario_error_message,
                        "error_stage": "execution",
                        "jira_id": jira_id
                    }
                    # Extract video filename from failed_recordings if available
                    failure_video_filename = None
                    if failed_recordings:
                        # Find recording for this scenario
                        for rec in failed_recordings:
                            if rec.get("scenario_number") == i and rec.get("zip_url"):
                                failure_video_filename = _filename_from_url(rec.get("zip_url"))
                                logger.info(f"📹 Video filename for failed scenario {i}: {failure_video_filename}")
                                break
                    
                    # Build step-locator map from scenario DataFrame (even for failed scenarios)
                    # Pass raw data to PostgreSQL and let it handle JSON conversion
                    stp_loc_map_json = None
                    try:
                        scenario_key = f"Scenario_{i}"
                        if scenario_key in scenario_dict and scenario_dict[scenario_key] is not None:
                            df = scenario_dict[scenario_key]
                            if not df.empty and 'original_bdd_step' in df.columns and 'playwrightLocator' in df.columns:
                                # Extract raw data from DataFrame without modification
                                steps_list = []
                                for _, row in df.iterrows():
                                    step_name = str(row.get('original_bdd_step', '')).strip()
                                    locator = row.get('playwrightLocator', '')
                                    # Keep original values - let PostgreSQL handle None/null
                                    if step_name:
                                        steps_list.append({
                                            'stepname': step_name,
                                            'locator': locator  # Pass as-is (None, string, etc.)
                                        })
                                if steps_list:
                                    # Let PostgreSQL handle JSON conversion and null handling
                                    stp_loc_map_json = json.dumps({'steps': steps_list}, ensure_ascii=False)
                                    logger.info(f"📍 Built step-locator map with {len(steps_list)} steps for failed scenario {i}")
                    except Exception as stp_err:
                        logger.warning(f"⚠️  Failed to build step-locator map for failed scenario {i}: {stp_err}")
                    
                    store_success = store_generated_response(
                        run_id=run_id,
                        response_data=scenario_response,
                        jira_id=jira_id,
                        module_name=selected_domain,  # Use user-selected domain/module
                        status="FAILED",
                        scenario_name=scenario_description,
                        video_filename=failure_video_filename,
                        complete_scripts=(
                            step_definition_file if step_definition_file else (
                                (e.context.get("step_definition_file") if isinstance(getattr(e, "context", None), dict) else None)
                            )
                        ),
                        feature_steps=feature_steps_json_text,
                        feature_tags=feature_tags,  # Use passed-in metadata
                        feature_nm=feature_nm,  # Use passed-in metadata
                        scn_tags=scenario_tag,  # Use per-scenario tag extracted from feature file
                        scn_nm=scenario_description,  # Use actual scenario name from this scenario
                        stp_loc_map=stp_loc_map_json,  # Step-locator map from MCP results
                        api_steps=api_steps  # Use passed-in metadata
                    )
                    if store_success:
                        scenarios_stored_in_db += 1
                        logger.info(f"💾 Scenario {i} '{scenario_description}' failure stored in t_gen_resp (run_id: {run_id}, module: {selected_domain}, video: {failure_video_filename})")
                    else:
                        logger.warning(f"⚠️  Failed to store scenario {i} failure in t_gen_resp")
                except Exception as store_err:
                    logger.warning(f"⚠️  Error storing scenario {i} failure in t_gen_resp: {store_err}")
                
                # Reset steps for next scenario (don't delete - subsequent scenarios need the entry)
                # The entry will be deleted at the end by playwright_api.py
                try:
                    reset_tran_status_steps(run_id)
                    logger.info(f"🔄 Reset tran_status steps for next scenario after failure (run_id: {run_id})")
                except Exception as reset_err:
                    logger.warning(f"⚠️  Failed to reset tran_status steps: {reset_err}")
            
            last_exception = e
            i+=1
            continue
        i+=1

    processing_summary = {
        "total_scenarios": total_scenarios,
        "successful_scenarios": successful_scenarios,
        "failed_scenarios": failed_scenarios,
        "scenarios_stored_in_db": scenarios_stored_in_db,  # Count of scenarios actually stored in t_gen_resp
        "success_rate": f"{(successful_scenarios / total_scenarios) * 100:.1f}%" if total_scenarios > 0 else "0%",
        "scenario_details": scenario_results,
        "scenarios": scenarios_data,
        "failed_reasons": [
            {"scenario_number": s.get("scenario_number"), "reason": s.get("user_reason")}
            for s in scenario_results if s.get("status") == "failed"
        ],
        "failed_details": [
            {
                "scenario_number": s.get("scenario_number"),
                "reason": s.get("user_reason"),
                "technical": s.get("technical_error")
            }
            for s in scenario_results if s.get("status") == "failed"
        ],
        "success_recordings": success_recordings,  # list of {"scenario_number": int, "zip_url": str}
        "failed_recordings": failed_recordings     # list of {"scenario_number": int, "zip_url": str}
    }

    # If multiple scenarios were stored, update all rows with the final merged script.
    if run_id and status_tracking_available and scenarios_stored_in_db > 0:
        if isinstance(step_definition_file, str) and step_definition_file.strip():
            try:
                updated = update_complete_scripts_for_runid(run_id, step_definition_file)
                if updated:
                    logger.info(f"🧩 Updated complt_scripts for run_id={run_id} with final merged script")
                else:
                    logger.warning(f"⚠️  Failed to update complt_scripts for run_id={run_id}")
            except Exception as update_err:
                logger.warning(f"⚠️  Error updating complt_scripts for run_id={run_id}: {update_err}")
    
    if step_definition_file:
        logger.info(f"Writing step definition file: {step_definition_file}")
        logger.info(f"Processing completed: {successful_scenarios}/{total_scenarios} scenarios successful")
        base_branch = base_branch_override or "main"
        new_branch_name = f"feature/{validated_dict['tag']}"
        target_module= validated_dict["first_part"]
        try:
            frames = [df for df in scenario_dict.values() if df is not None and not df.empty]
            combined_df = pd.concat(frames, ignore_index=True) if frames else None

            if combined_df is not None and not combined_df.empty:
                # Validate and fix any remaining current_frame. locators in the DataFrame
                if 'playwrightLocator' in combined_df.columns:
                    current_frame_mask = combined_df['playwrightLocator'].astype(str).str.contains('current_frame.', na=False)
                    current_frame_count = current_frame_mask.sum()
                    
                    if current_frame_count > 0:
                        logger.warning(f"Found {current_frame_count} current_frame. locators in data - converting to page. locators")
                        # Convert current_frame. to page. prefix
                        combined_df.loc[current_frame_mask, 'playwrightLocator'] = (
                            combined_df.loc[current_frame_mask, 'playwrightLocator']
                            .astype(str)
                            .str.replace('current_frame.', 'page.', regex=False)
                        )
                        logger.info(f"Converted {current_frame_count} locators from current_frame. to page. prefix")
                
                # Clean up legacy current_frame. locators before pushing new data
                try:
                    from utils.db_utils import cleanup_legacy_current_frame_locators
                    cleaned_count = cleanup_legacy_current_frame_locators()
                    if cleaned_count > 0:
                        logger.info(f"Cleaned up {cleaned_count} legacy current_frame. locators before database update")
                except Exception as cleanup_err:
                    logger.warning(f"Failed to cleanup legacy locators: {cleanup_err}")
                
                pushed = push_steps_df(
                    df=combined_df,
                    table="t_locators",
                    key_col="unique_id",
                )
                logger.info(f"DB UPSERT complete. Rows attempted: {pushed}")
            else:
                logger.info("No scenario rows to push to DB.")
        except Exception as e:
            logger.error(f"Error processing scenario: {str(e)}")
            # Log DataFrame details for debugging
            if 'combined_df' in locals() and combined_df is not None:
                logger.error(f"Combined DataFrame shape: {combined_df.shape}")
                if 'playwrightLocator' in combined_df.columns:
                    current_frame_count = combined_df['playwrightLocator'].astype(str).str.contains('current_frame.', na=False).sum()
                    logger.error(f"Found {current_frame_count} current_frame. locators in the data")
                    if current_frame_count > 0:
                        logger.error("Sample current_frame. locators:")
                        sample_locators = combined_df[combined_df['playwrightLocator'].astype(str).str.contains('current_frame.', na=False)]['playwrightLocator'].head(3).tolist()
                        for loc in sample_locators:
                            logger.error(f"  - {loc}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Don't re-raise the exception, continue with the rest of the process
        if push_to_github:
            logger.info(f"Writing step definition file")
            base_branch = base_branch_override or "main"
            # If a custom branch base was provided, use it; otherwise fall back to tag-based naming
            new_branch_name = (custom_branch_base.strip() if isinstance(custom_branch_base, str) and custom_branch_base.strip() else f"feature/{validated_dict['tag']}")
            target_module = validated_dict["first_part"]

            try:
                github_result = main(
                    jira_id, github_access_token, target_github_repo, target_module,
                    new_branch_name, base_branch, jenkins_project_name, jenkins_username,
                    jenkins_api_token, content, step_definition_file, validated_dict["tag"], repo_base_path, is_non_bdd=(not generate_step_definitions)                )

                if github_result["success"]:
                    logger.info("🎉 GitHub workflow completed successfully!")
                    
                    # Check Jenkins pipeline status and include in response
                    jenkins_status = github_result.get("jenkins_trigger", {})
                    response_data = {
                        "success": True,
                        "message": "Feature file processing and GitHub integration completed successfully",
                        "jira_id": jira_id,
                        "github_result": github_result,
                        "step_definition_file": step_definition_file,
                        "processing_summary": processing_summary
                    }
                    
                    # Always include Jenkins status in response
                    if jenkins_status:
                        if jenkins_status.get("success"):
                            response_data["jenkins_pipeline_status"] = "Success"
                            response_data["jenkins_details"] = jenkins_status.get("details", {})
                            response_data["jenkins_queue_location"] = jenkins_status.get("details", {}).get("queue_location", "N/A")
                            response_data["jenkins_job_name"] = jenkins_status.get("details", {}).get("job_name", jenkins_project_name)
                        else:
                            response_data["jenkins_pipeline_status"] = "Failed"
                            response_data["jenkins_error"] = jenkins_status.get("error", "Unknown Jenkins error")
                            response_data["jenkins_details"] = jenkins_status.get("details", {})
                            logger.warning(f"⚠️ Jenkins pipeline failed: {jenkins_status.get('error')}")
                    else:
                        response_data["jenkins_pipeline_status"] = "Not Triggered"
                        response_data["jenkins_error"] = "Jenkins trigger information not found in GitHub result"
                    
                    # Add Jenkins status fields directly to response for backward compatibility
                    response_data["jenkins_status"] = jenkins_status

                    # Attach preview steps from combined DataFrame if available
                    try:
                        frames = [df for df in scenario_dict.values() if df is not None and not df.empty]
                        combined_df = pd.concat(frames, ignore_index=True) if frames else None
                        preview_steps = []
                        if combined_df is not None and not combined_df.empty:
                            seen_steps = set()
                            for _, row in combined_df.iterrows():
                                full_step = str(row.get('original_bdd_step', '')).strip()
                                if not full_step or full_step in seen_steps:
                                    continue
                                seen_steps.add(full_step)
                                action = str(row.get('action', '')).strip().lower()
                                locator = str(row.get('playwrightLocator', '')).strip()
                                preview_steps.append({
                                    "stepNo": len(preview_steps) + 1,
                                    "fullStep": full_step,
                                    "action": action,
                                    "locator": locator,
                                    "confidence": 100.0
                                })
                        response_data["preview_steps"] = preview_steps
                    except Exception:
                        response_data["preview_steps"] = []
                    
                    return response_data
                else:
                    error_stage = github_result.get("github_workflow", {}).get("stage", "unknown")
                    error_message = github_result.get("github_workflow", {}).get("error", "Unknown error")
                    overall_error = github_result.get("overall_error", "GitHub workflow failed")

                    logger.error(f"❌ GitHub workflow failed at stage: {error_stage}")
                    
                    raise GitHubIntegrationError(
                        message=f"GitHub workflow failed at {error_stage}: {overall_error}",
                        error_code="GITHUB_WORKFLOW_FAILED",
                        context={
                            "jira_id": jira_id,
                            "stage": error_stage,
                            "error_details": error_message,
                            "processing_summary": _sanitize_processing_summary(processing_summary),
                            "github_result": github_result,
                            "step_definition_file": step_definition_file,
                            "generated_ts_filename": f"{jira_id}.ts"
                        }
                    )


            except GitHubIntegrationError:
                # Preserve rich context from earlier raise
                # Analytics already saved before raising the error
                raise
            except Exception as e:
                logger.error(f"❌ Unexpected error during GitHub integration: {str(e)}")
                
                raise GitHubIntegrationError(
                    message=f"Unexpected error during GitHub integration: {str(e)}",
                    error_code="GITHUB_WORKFLOW_FAILED",
                    context={
                        "jira_id": jira_id,
                        "stage": "github_integration_exception",
                        "processing_summary": _sanitize_processing_summary(processing_summary),
                        "original_error": str(e)
                    }
                )
        else:
            logger.info("📋 GitHub push disabled - feature file processing completed locally only")

            # Build preview steps from combined DataFrame if available
            preview_steps = []
            try:
                frames = [df for df in scenario_dict.values() if df is not None and not df.empty]
                combined_df = pd.concat(frames, ignore_index=True) if frames else None
                if combined_df is not None and not combined_df.empty:
                    seen_steps = set()
                    for _, row in combined_df.iterrows():
                        full_step = str(row.get('original_bdd_step', '')).strip()
                        if not full_step or full_step in seen_steps:
                            continue
                        seen_steps.add(full_step)
                        action = str(row.get('action', '')).strip().lower()
                        locator = str(row.get('playwrightLocator', '')).strip()
                        preview_steps.append({
                            "stepNo": len(preview_steps) + 1,
                            "fullStep": full_step,
                            "action": action,
                            "locator": locator,
                            "confidence": 100.0
                        })
            except Exception as _:
                # Non-fatal: preview is best-effort and should not block
                preview_steps = []
            
            # Check if there were any failures - re-raise if so
            if 'last_exception' in locals() and last_exception:
                logger.error(f"Re-raising last exception for MCP local flow: {str(last_exception)}")
                
                # Attach processing_summary to exception so outer handler can access scenarios data
                try:
                    last_exception.processing_summary = processing_summary
                    logger.info(f"📊 Attached processing_summary with {len(processing_summary.get('scenarios', []))} scenarios to exception")
                except Exception as attach_err:
                    logger.warning(f"⚠️  Could not attach processing_summary to exception: {attach_err}")
                
                # Mark exception as already stored if scenarios were stored individually
                # This prevents duplicate storage in outer exception handlers
                if scenarios_stored_in_db > 0:
                    try:
                        last_exception._db_stored = True
                        logger.info(f"📊 Marked exception as _db_stored=True ({scenarios_stored_in_db} scenarios stored)")
                    except (AttributeError, TypeError) as attr_err:
                        logger.warning(f"⚠️  Could not set _db_stored on exception: {attr_err}")
                
                raise last_exception
            
            return {
                "success": True,
                "message": "Feature file processing completed successfully (GitHub push disabled)",
                "jira_id": jira_id,
                "step_definition_file": step_definition_file,
                "processing_summary": processing_summary,
                "github_push_enabled": False,
                "preview_steps": preview_steps
            }


    else:
        if 'last_exception' in locals() and last_exception:
            logger.error(f"Re-raising last exception: {str(last_exception)}")
            
            # Attach processing_summary to exception so outer handler can access scenarios data
            try:
                last_exception.processing_summary = processing_summary
                logger.info(f"📊 Attached processing_summary with {len(processing_summary.get('scenarios', []))} scenarios to exception")
            except Exception as attach_err:
                logger.warning(f"⚠️ Could not attach processing_summary to exception: {attach_err}")
            
            # Mark exception as already stored if scenarios were stored individually
            # This prevents duplicate storage in outer exception handlers
            if scenarios_stored_in_db > 0:
                try:
                    last_exception._db_stored = True
                    logger.info(f"📊 Marked exception as _db_stored=True ({scenarios_stored_in_db} scenarios stored)")
                except (AttributeError, TypeError) as attr_err:
                    logger.warning(f"⚠️  Could not set _db_stored on exception: {attr_err}")
            
            raise last_exception
        logger.warning(f"⚠️ No scenario definition generated for JIRA ID: {jira_id}")
        raise SystemError(
            message="Failed to generate step definitions",
            error_code="CODE_GENERATION_FAILED",
            context={"jira_id": jira_id,"processing_summary": processing_summary, }
        )



def extract_first_feature(text):
    """
    Extract feature description from Feature: line until:
    1. First scenario tag (line starting with @), OR
    2. First Scenario: line
    
    This ensures feature description doesn't include scenario tags.
    """
    # Find text from Feature: onwards
    match = re.search(
        r"Feature:(.*?)(?=\s*\n\s*@|\s*\n\s*Scenario(?:\s+Outline)?:|\$)", text, re.DOTALL
    )
    if match:
        # Clean up the captured text - remove trailing whitespace and empty lines
        feature_text = match.group(1).strip()
        return "Feature:" + feature_text
    return None


def extract_scenario_steps(data):
    scenario = data.get('Scenario', '')
    steps = data.get('steps', [])

    # Format the scenario and steps according to the requested format
    output = f"Scenario: {scenario}"
    for step in steps:
        output += f"\n   {step.strip()}"

    return output




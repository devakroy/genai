"""
Run ID Management Utilities

This module provides utilities for validating, generating, and managing run IDs
across the Chewy GenAI Automation platform. Every execution run should have
a unique run ID that tracks the execution through the entire lifecycle.

The run ID system supports:
- User-provided run IDs with format validation (internal_<alphanumeric>)
- Automatic generation of run IDs using timestamp format (YYMMDDHHMMSS)
- Database storage of generated responses against run ID in t_gen_resp table
- Integration with Slack and email notifications

Database Structure:
- Table: t_gen_resp (id: bigint, runid: text, resp: json)
- Run IDs are stored directly as text in the database
- Response data is stored as JSON in the resp column
"""

import re
import uuid
import logging
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime
from sqlalchemy import text, engine
from utils.db_utils import PGSCHEMA, _get_engine, BASE_DATABASE_URL
from utils.constants import PLATFORM_CONFIG

# Initialize logger
logger = logging.getLogger(__name__)

# Run ID format patterns
USER_RUN_ID_PATTERN = re.compile(r'^internal_[a-zA-Z0-9]+$')  # User-provided format
GENERATED_RUN_ID_PATTERN = re.compile(r'^\d{12}$')  # Generated format: YYMMDDHHMMSS
RUN_ID_PREFIX = "internal_"


class RunIdManager:
    """
    Manages run IDs for the automation platform.
    
    Provides validation, generation, and database operations for run IDs.
    """
    
    def __init__(self):
        self.logger = logger
    
    def validate_run_id(self, run_id: Optional[str]) -> Tuple[bool, str]:
        """
        Validate the format of a user-provided run ID.
        
        Args:
            run_id: The run ID to validate (can be None)
            
        Returns:
            Tuple of (is_valid, message)
            
        Format requirement: internal_<alphanumeric>
        Example valid: internal_abc123, internal_test01, internal_xyz, internal_djbdcbd787
        Example invalid: internal_, test_123, internal_@#$, 251230155523
        """
        if not run_id:
            return False, "Run ID is None or empty"
        
        if not isinstance(run_id, str):
            return False, "Run ID must be a string"
        
        # Check format: internal_<alphanumeric>
        if not USER_RUN_ID_PATTERN.match(run_id):
            return False, (
                f"Invalid run ID format. Must match pattern: internal_<alphanumeric>. "
                f"Got: {run_id}"
            )
        
        # Check minimum length (internal_ + at least 1 character)
        if len(run_id) <= len(RUN_ID_PREFIX):
            return False, f"Run ID must have content after '{RUN_ID_PREFIX}' prefix"
        
        return True, "Valid run ID format"
    
    def generate_run_id(self, prefix: str = "auto") -> str:
        """
        Generate a new run ID with timestamp format (no internal_ prefix).
        
        Args:
            prefix: Optional prefix (ignored for generated IDs)
            
        Returns:
            Generated run ID in format: YYMMDDHHMMSS
            Example: 251230155030
        """
        from datetime import datetime
        
        # Generate timestamp (YYMMDDHHMMSS)
        now = datetime.now()
        run_id = now.strftime("%y%m%d%H%M%S")
        
        self.logger.info(f"🆔 Generated run ID: {run_id}")
        return run_id
    
    def resolve_run_id(self, user_run_id: Optional[str] = None, 
                      generation_prefix: str = "auto") -> Tuple[str, bool]:
        """
        Resolve the run ID to use for an execution.
        
        If user provides a valid run ID, use it.
        If user provides an invalid run ID or None, generate a new one using millisecond timestamp.
        
        Args:
            user_run_id: Optional run ID provided by user
            generation_prefix: Prefix to use when generating new run ID
            
        Returns:
            Tuple of (resolved_run_id, was_user_provided)
        """
        if user_run_id:
            is_valid, message = self.validate_run_id(user_run_id)
            if is_valid:
                self.logger.info(f"✅ Using user-provided run ID: {user_run_id}")
                return user_run_id, True
            else:
                self.logger.warning(f"❌ Invalid user run ID: {message}")
                self.logger.info("🔄 Generating new run ID with timestamp format...")
        
        # Generate new run ID (timestamp only)
        generated_id = self.generate_run_id(generation_prefix)
        return generated_id, False
    
    def store_generated_response(self, run_id: str, response_data: Dict[str, Any],
                               jira_id: Optional[str] = None, module_name: Optional[str] = None,
                               status: Optional[str] = None, scenario_name: Optional[str] = None,
                               video_filename: Optional[str] = None, complete_scripts: Optional[str] = None,
                               feature_steps: Optional[str] = None, feature_tags: Optional[str] = None,
                               feature_nm: Optional[str] = None, scn_tags: Optional[str] = None,
                               scn_nm: Optional[str] = None, stp_loc_map: Optional[str] = None,
                               api_steps: Optional[str] = None) -> bool:
        """
        Store generated response data in the t_gen_resp table.
        
        Table structure: id, runid, resp (json), execution_dt, status, modulenm, scenarioname, video_filenm, complt_scripts,
                        feature_steps, feature_tags, feature_nm, scn_tags, scn_nm, stp_loc_map, api_steps
        
        Args:
            run_id: The run ID to associate with this response
            response_data: The response data to store (will be JSON serialized)
            jira_id: Optional JIRA ID for additional context (stored within response_data)
            module_name: Optional module/domain name to populate modulenm column
            status: Optional status to populate status column ("pass" or "fail")
            scenario_name: Optional scenario name to populate scenarioname column
            video_filename: Optional video filename to populate video_filenm column
            complete_scripts: Optional complete scripts content to populate complt_scripts column
            feature_steps: Optional feature steps JSON to populate feature_steps column
            feature_tags: Optional feature tags (JSON) to populate feature_tags column
            feature_nm: Optional feature name to populate feature_nm column
            scn_tags: Optional scenario tags (JSON) to populate scn_tags column
            scn_nm: Optional scenario name to populate scn_nm column
            stp_loc_map: Optional step vs locator map (JSON) to populate stp_loc_map column
            api_steps: Optional API steps (JSON) to populate api_steps column
            
        Returns:
            True if storage was successful, False otherwise
        """
        try:
            import json
            
            # Add jira_id to response data if provided
            enhanced_data = response_data.copy()
            if jira_id:
                enhanced_data["jira_id"] = jira_id
            enhanced_data["run_id"] = run_id
            enhanced_data["stored_at"] = datetime.now().isoformat()
            
            # Serialize response data to JSON
            response_json = json.dumps(enhanced_data, default=str, ensure_ascii=False)
            
            # Ensure api_steps is properly JSON serialized (not a string with newlines)
            if api_steps:
                if isinstance(api_steps, str):
                    try:
                        # Try to parse as JSON to validate format
                        json.loads(api_steps)
                        # If valid, use as-is
                    except (json.JSONDecodeError, ValueError):
                        # If not valid JSON, try to convert from list/dict
                        try:
                            api_steps = json.dumps(json.loads(api_steps), ensure_ascii=False)
                        except Exception:
                            # If all else fails, wrap in array
                            api_steps = json.dumps([api_steps], ensure_ascii=False)
                elif isinstance(api_steps, (list, dict)):
                    # Convert list/dict to JSON string
                    api_steps = json.dumps(api_steps, ensure_ascii=False)
            
            # Store run_id directly as text (no hashing needed)
            runid_text = run_id
            
            # Get current timestamp for execution_dt (now uses TIMESTAMP WITHOUT TIME ZONE)
            current_timestamp = datetime.now()
            
            # Map status values: "success" -> "PASSED", "failure" -> "FAILED"
            db_status = None
            if status:
                if status.lower() in ["success", "pass", "passed"]:
                    db_status = "PASSED"
                elif status.lower() in ["failure", "fail", "failed"]:
                    db_status = "FAILED"
                else:
                    db_status = status
            
            self.logger.info(f"🔍 Attempting to store run_id: {runid_text} (type: {type(runid_text).__name__})")
            self.logger.info(f"🔍 Schema: {PGSCHEMA}, Database URL configured: {bool(BASE_DATABASE_URL)}")
            self.logger.info(f"🔍 Module: {module_name}, Status: {db_status}, Timestamp: {current_timestamp}")
            self.logger.info(f"🔍 Scenario Name: {scenario_name} (type: {type(scenario_name).__name__ if scenario_name else 'None'})")
            if video_filename:
                self.logger.info(f"🎬 Video Filename: {video_filename}")
            if complete_scripts:
                self.logger.info(f"📝 Complete Scripts: {len(complete_scripts)} characters")
            if feature_steps:
                try:
                    self.logger.info(f"🧩 Feature Steps length: {len(feature_steps)} characters")
                except Exception:
                    self.logger.info("🧩 Feature Steps provided")
            
            eng = _get_engine(BASE_DATABASE_URL)
            
            # Try UPSERT first (requires composite unique constraint on runid, scenarioname)
            # Falls back to simple INSERT if constraint doesn't exist
            upsert_sql = text(f"""
                INSERT INTO {PGSCHEMA}.t_gen_resp
                (runid, resp, execution_dt, status, modulenm, scenarioname, video_filenm, complt_scripts, feature_steps,
                 feature_tags, feature_nm, scn_tags, scn_nm, stp_loc_map, api_steps)
                VALUES
                (:runid, :resp, :execution_dt, :status, :modulenm, :scenarioname, :video_filenm, :complt_scripts, :feature_steps,
                 :feature_tags, :feature_nm, :scn_tags, :scn_nm, CAST(:stp_loc_map AS json), CAST(:api_steps AS json))
                ON CONFLICT (runid, scenarioname)
                DO UPDATE SET
                    resp = EXCLUDED.resp,
                    execution_dt = EXCLUDED.execution_dt,
                    status = EXCLUDED.status,
                    modulenm = EXCLUDED.modulenm,
                    video_filenm = EXCLUDED.video_filenm,
                    complt_scripts = EXCLUDED.complt_scripts,
                    feature_steps = EXCLUDED.feature_steps,
                    feature_tags = EXCLUDED.feature_tags,
                    feature_nm = EXCLUDED.feature_nm,
                    scn_tags = EXCLUDED.scn_tags,
                    scn_nm = EXCLUDED.scn_nm,
                    stp_loc_map = CAST(EXCLUDED.stp_loc_map AS json),
                    api_steps = CAST(EXCLUDED.api_steps AS json)
            """)
            
            simple_insert_sql = text(f"""
                INSERT INTO {PGSCHEMA}.t_gen_resp
                (runid, resp, execution_dt, status, modulenm, scenarioname, video_filenm, complt_scripts, feature_steps,
                 feature_tags, feature_nm, scn_tags, scn_nm, stp_loc_map, api_steps)
                VALUES
                (:runid, :resp, :execution_dt, :status, :modulenm, :scenarioname, :video_filenm, :complt_scripts, :feature_steps,
                 :feature_tags, :feature_nm, :scn_tags, :scn_nm, CAST(:stp_loc_map AS json), CAST(:api_steps AS json))
            """)
            
            params = {
                "runid": runid_text,
                "resp": response_json,
                "execution_dt": current_timestamp,
                "status": db_status,
                "modulenm": module_name,
                "scenarioname": scenario_name,
                "video_filenm": video_filename,
                "complt_scripts": complete_scripts,
                "feature_steps": feature_steps,
                "feature_tags": feature_tags,
                "feature_nm": feature_nm,
                "scn_tags": scn_tags,
                "scn_nm": scn_nm,
                "stp_loc_map": stp_loc_map,
                "api_steps": api_steps
            }
            
            # Execute insert - try UPSERT first, fallback to simple INSERT
            # Use separate transactions to avoid "transaction aborted" errors
            upsert_succeeded = False
            
            try:
                # Try UPSERT first (works if composite constraint exists)
                with eng.begin() as conn:
                    result = conn.execute(upsert_sql, params)
                    self.logger.info(f"🔍 UPSERT executed, rowcount: {result.rowcount if hasattr(result, 'rowcount') else 'N/A'}")
                    upsert_succeeded = True
            except Exception as upsert_error:
                if "no unique or exclusion constraint" in str(upsert_error).lower():
                    # Constraint doesn't exist - fall back to simple INSERT in NEW transaction
                    self.logger.warning(f"⚠️  UPSERT failed (constraint missing), falling back to simple INSERT")
                    try:
                        with eng.begin() as conn2:
                            result = conn2.execute(simple_insert_sql, params)
                            self.logger.info(f"🔍 Simple INSERT executed, rowcount: {result.rowcount if hasattr(result, 'rowcount') else 'N/A'}")
                            upsert_succeeded = True
                    except Exception as insert_error:
                        if "duplicate key" in str(insert_error).lower() or "unique constraint" in str(insert_error).lower():
                            # Entry already exists - try UPDATE instead to avoid data loss
                            self.logger.warning(f"⚠️  Duplicate key on INSERT, attempting UPDATE for run_id={runid_text}, scenario={scenario_name}")
                            try:
                                update_sql = text(f"""
                                    UPDATE {PGSCHEMA}.t_gen_resp
                                    SET resp = :resp, execution_dt = :execution_dt, status = :status, modulenm = :modulenm,
                                        video_filenm = :video_filenm, complt_scripts = :complt_scripts, feature_steps = :feature_steps,
                                        feature_tags = :feature_tags, feature_nm = :feature_nm, scn_tags = :scn_tags,
                                        scn_nm = :scn_nm, stp_loc_map = CAST(:stp_loc_map AS json), api_steps = CAST(:api_steps AS json)
                                    WHERE runid = :runid AND (scenarioname = :scenarioname OR (scenarioname IS NULL AND :scenarioname IS NULL))
                                """)
                                with eng.begin() as conn3:
                                    result = conn3.execute(update_sql, params)
                                    rows_updated = result.rowcount if hasattr(result, 'rowcount') else 0
                                    if rows_updated > 0:
                                        self.logger.info(f"✅ Updated existing entry for run_id={runid_text}, scenario={scenario_name}")
                                        upsert_succeeded = True
                                    else:
                                        self.logger.warning(f"⚠️  UPDATE matched 0 rows for run_id={runid_text}, scenario={scenario_name}")
                            except Exception as update_error:
                                self.logger.error(f"❌ UPDATE also failed: {update_error}")
                                raise insert_error
                        else:
                            raise insert_error
                else:
                    raise upsert_error
            
            if not upsert_succeeded:
                self.logger.warning(f"⚠️  Could not store response for run_id={runid_text}, scenario={scenario_name}")
                return False
                
            self.logger.info(f"💾 Stored response for run ID: {run_id}")
            if jira_id:
                self.logger.info(f"   📋 Associated JIRA ID: {jira_id}")
            if module_name:
                self.logger.info(f"   📦 Module: {module_name}")
            if scenario_name:
                self.logger.info(f"   🎬 Scenario: {scenario_name}")
            if db_status:
                self.logger.info(f"   ✅ Status: {db_status}")
            
            return True
            
        except Exception as e:
            import traceback
            self.logger.error(f"❌ Failed to store response for run ID {run_id}: {str(e)}")
            self.logger.error(f"❌ Full traceback: {traceback.format_exc()}")
            return False

    def update_complete_scripts_for_runid(self, run_id: str, complete_scripts: str) -> bool:
        """
        Update complt_scripts for all rows with the given run_id.

        Args:
            run_id: The run ID to update
            complete_scripts: Final merged script content

        Returns:
            True if update was successful, False otherwise
        """
        try:
            if not run_id or not isinstance(complete_scripts, str) or not complete_scripts.strip():
                self.logger.warning("⚠️  update_complete_scripts_for_runid called with empty data")
                return False

            eng = _get_engine(BASE_DATABASE_URL)
            update_sql = text(f"""
                UPDATE {PGSCHEMA}.t_gen_resp
                SET complt_scripts = :complt_scripts
                WHERE runid = :runid
            """)
            params = {
                "runid": run_id,
                "complt_scripts": complete_scripts
            }

            with eng.begin() as conn:
                result = conn.execute(update_sql, params)
                self.logger.info(
                    f"🔄 Updated complt_scripts for run_id={run_id}, rows={result.rowcount if hasattr(result, 'rowcount') else 'N/A'}"
                )
            return True
        except Exception as e:
            self.logger.error(f"❌ Failed to update complt_scripts for run_id={run_id}: {e}")
            return False
    
    def get_stored_response(self, run_id: str, scenario_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Retrieve stored response data for a run ID.
        
        Args:
            run_id: The run ID to look up
            scenario_name: Optional scenario name to filter by (for multi-scenario run IDs)
            
        Returns:
            Stored response data as dict, or None if not found
        """
        try:
            # Use run_id directly as text (no hashing needed)
            runid_text = run_id
            
            self.logger.info(f"🔍 Attempting to retrieve run_id: {runid_text} (type: {type(runid_text).__name__})")
            if scenario_name:
                self.logger.info(f"🔍 Filtering by scenario: {scenario_name}")
            
            # Build query based on whether scenario_name is provided
            if scenario_name:
                select_sql = text(f"""
                    SELECT resp
                    FROM {PGSCHEMA}.t_gen_resp 
                    WHERE runid = :runid AND scenarioname = :scenarioname
                """)
                params = {"runid": runid_text, "scenarioname": scenario_name}
            else:
                select_sql = text(f"""
                    SELECT resp
                    FROM {PGSCHEMA}.t_gen_resp 
                    WHERE runid = :runid
                """)
                params = {"runid": runid_text}
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                result = conn.execute(select_sql, params)
                row = result.fetchone()
                self.logger.info(f"🔍 Query executed, row found: {row is not None}")
                
            if row:
                # Handle both JSON string and dict cases
                data = row[0]
                if isinstance(data, str):
                    import json
                    return json.loads(data)
                else:
                    # Already a dict/object
                    return data
            else:
                self.logger.info(f"ℹ️  No stored response found for run ID: {run_id}" + 
                               (f", scenario: {scenario_name}" if scenario_name else ""))
                return None
            
        except Exception as e:
            self.logger.error(f"❌ Failed to retrieve response for run ID {run_id}: {str(e)}")
            return None
    
    def get_all_scenarios_by_runid(self, run_id: str) -> List[Dict[str, Any]]:
        """
        Retrieve ALL scenarios stored for a given run ID.
        
        This is useful for multi-scenario feature files where multiple scenarios
        share the same run_id but have different scenario names.
        
        Args:
            run_id: The run ID to look up
            
        Returns:
            List of scenario data dicts with keys: scenarioname, status, modulenm, resp, execution_dt
            Returns empty list if no scenarios found
        """
        try:
            runid_text = run_id
            
            self.logger.info(f"🔍 Retrieving ALL scenarios for run_id: {runid_text}")
            
            select_sql = text(f"""
                SELECT scenarioname, status, modulenm, resp, execution_dt
                FROM {PGSCHEMA}.t_gen_resp 
                WHERE runid = :runid
                ORDER BY id ASC
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            scenarios = []
            
            with eng.begin() as conn:
                result = conn.execute(select_sql, {"runid": runid_text})
                rows = result.fetchall()
                self.logger.info(f"🔍 Query executed, found {len(rows)} scenario(s)")
                
                for row in rows:
                    scenario_name, status, module_name, resp_data, execution_dt = row
                    
                    # Parse resp JSON
                    if isinstance(resp_data, str):
                        import json
                        resp_dict = json.loads(resp_data)
                    else:
                        resp_dict = resp_data if resp_data else {}
                    
                    scenarios.append({
                        "scenario_name": scenario_name,
                        "status": status,
                        "module_name": module_name,
                        "response": resp_dict,
                        "execution_dt": str(execution_dt) if execution_dt else None
                    })
            
            self.logger.info(f"📋 Retrieved {len(scenarios)} scenario(s) for run_id: {run_id}")
            return scenarios
            
        except Exception as e:
            self.logger.error(f"❌ Failed to retrieve scenarios for run ID {run_id}: {str(e)}")
            return []
    
    # ============================================================================
    # TRAN_STATUS TABLE OPERATIONS
    # ============================================================================
    
    def init_tran_status(self, run_id: str, module_name: Optional[str] = None, 
                        scenario_name: Optional[str] = None) -> bool:
        """
        Initialize a new entry in tran_status table at the start of execution.
        
        Table structure: id, runid, modulename, scenarioname, succ_steps (json), execution_dt, status
        
        Args:
            run_id: The run ID for this execution
            module_name: Optional module/domain name
            scenario_name: Optional scenario name
            
        Returns:
            True if initialization was successful, False otherwise
        """
        try:
            import json
            
            # Initial status message
            initial_status = "Test Execution -Started, Dry run will start soon"
            
            # Store as JSON object with status key
            succ_steps_json = json.dumps({"status": initial_status}, ensure_ascii=False)
            
            current_timestamp = datetime.now()
            
            insert_sql = text(f"""
                INSERT INTO {PGSCHEMA}.tran_status
                (runid, modulename, scenarioname, succ_steps, execution_dt, status)
                VALUES
                (:runid, :modulename, :scenarioname, :succ_steps, :execution_dt, :status)
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                conn.execute(insert_sql, {
                    "runid": run_id,
                    "modulename": module_name,
                    "scenarioname": scenario_name,
                    "succ_steps": succ_steps_json,
                    "execution_dt": current_timestamp,
                    "status": "IN_PROGRESS"  # Initial execution phase
                })
            
            self.logger.info(f"🎬 Initialized tran_status for run ID: {run_id}")
            self.logger.info(f"   📝 Initial status: {initial_status}")
            self.logger.info(f"   🔄 Execution phase: IN_PROGRESS")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize tran_status for run ID {run_id}: {str(e)}")
            return False
    
    def set_current_executing_step(self, run_id: str, step_sentence: str) -> bool:
        """
        Set the current step being executed in tran_status table.
        This is called BEFORE executing a step to track progress.
        
        Args:
            run_id: The run ID for this execution
            step_sentence: The step sentence currently being executed
            
        Returns:
            True if update was successful, False otherwise
        """
        try:
            import json
            
            # Get current status
            select_sql = text(f"""
                SELECT succ_steps
                FROM {PGSCHEMA}.tran_status 
                WHERE runid = :runid
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                result = conn.execute(select_sql, {"runid": run_id})
                row = result.fetchone()
                
                if not row:
                    self.logger.warning(f"⚠️  No tran_status entry found for run ID: {run_id}")
                    return False
                
                # Parse existing status
                current_steps_data = row[0]
                if current_steps_data is None:
                    # Initialize empty object if succ_steps is NULL
                    current_steps_obj = {"status": "", "current_step": None}
                elif isinstance(current_steps_data, str):
                    current_steps_obj = json.loads(current_steps_data)
                else:
                    current_steps_obj = current_steps_data
                
                # Add current_step field to track the step being executed
                current_steps_obj["current_step"] = step_sentence
                
                # Update database
                update_sql = text(f"""
                    UPDATE {PGSCHEMA}.tran_status 
                    SET succ_steps = :succ_steps
                    WHERE runid = :runid
                """)
                
                updated_steps_json = json.dumps(current_steps_obj, ensure_ascii=False)
                conn.execute(update_sql, {
                    "runid": run_id,
                    "succ_steps": updated_steps_json
                })
            
            self.logger.info(f"▶️  Set current executing step for run ID: {run_id}")
            self.logger.info(f"   📝 Step: {step_sentence}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to set current step for run ID {run_id}: {str(e)}")
            return False
    
    def append_step_to_tran_status(self, run_id: str, step_sentence: str) -> bool:
        """
        Append a successfully executed step to the tran_status table.
        This is called AFTER successfully executing a step.
        
        Args:
            run_id: The run ID for this execution
            step_sentence: The step sentence that was executed successfully
            
        Returns:
            True if append was successful, False otherwise
        """
        try:
            import json
            
            # Get current status
            select_sql = text(f"""
                SELECT succ_steps
                FROM {PGSCHEMA}.tran_status 
                WHERE runid = :runid
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                result = conn.execute(select_sql, {"runid": run_id})
                row = result.fetchone()
                
                if not row:
                    self.logger.warning(f"⚠️  No tran_status entry found for run ID: {run_id}")
                    return False
                
                # Parse existing status
                current_steps_data = row[0]
                if current_steps_data is None:
                    # Initialize empty object if succ_steps is NULL
                    current_steps_obj = {"status": "", "current_step": None}
                elif isinstance(current_steps_data, str):
                    current_steps_obj = json.loads(current_steps_data)
                else:
                    current_steps_obj = current_steps_data
                
                current_status = current_steps_obj.get("status", "")
                
                # Append new step with pipe separator
                new_status = f"{current_status} | Step '{step_sentence}' has been executed successfully"
                
                # Update database and clear current_step since it's now completed
                update_sql = text(f"""
                    UPDATE {PGSCHEMA}.tran_status 
                    SET succ_steps = :succ_steps
                    WHERE runid = :runid
                """)
                
                # Clear current_step field after successful execution
                updated_steps_json = json.dumps({"status": new_status, "current_step": None}, ensure_ascii=False)
                conn.execute(update_sql, {
                    "runid": run_id,
                    "succ_steps": updated_steps_json
                })
            
            self.logger.info(f"✅ Appended step to tran_status for run ID: {run_id}")
            self.logger.info(f"   📝 Step: {step_sentence}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to append step to tran_status for run ID {run_id}: {str(e)}")
            return False
    
    def mark_dry_run_complete(self, run_id: str) -> bool:
        """
        Mark dry run as complete in tran_status table.
        
        Args:
            run_id: The run ID for this execution
            
        Returns:
            True if update was successful, False otherwise
        """
        try:
            import json
            
            # Get current status
            select_sql = text(f"""
                SELECT succ_steps
                FROM {PGSCHEMA}.tran_status 
                WHERE runid = :runid
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                result = conn.execute(select_sql, {"runid": run_id})
                row = result.fetchone()
                
                if not row:
                    self.logger.warning(f"⚠️  No tran_status entry found for run ID: {run_id}")
                    return False
                
                # Parse existing status
                current_steps_data = row[0]
                if isinstance(current_steps_data, str):
                    current_steps_obj = json.loads(current_steps_data)
                else:
                    current_steps_obj = current_steps_data
                
                current_status = current_steps_obj.get("status", "")
                
                # Append dry run completion message
                new_status = f"{current_status} | Dry run completed, Script generation has been started"
                
                # Update database - keep status as in_progress
                update_sql = text(f"""
                    UPDATE {PGSCHEMA}.tran_status 
                    SET succ_steps = :succ_steps
                    WHERE runid = :runid
                """)
                
                updated_steps_json = json.dumps({"status": new_status}, ensure_ascii=False)
                conn.execute(update_sql, {
                    "runid": run_id,
                    "succ_steps": updated_steps_json
                })
            
            self.logger.info(f"🏁 Marked dry run complete in tran_status for run ID: {run_id}")
            self.logger.info(f"   📝 Status message updated (phase remains: in_progress)")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to mark dry run complete for run ID {run_id}: {str(e)}")
            return False
    
    def mark_script_generation_complete(self, run_id: str) -> bool:
        """
        Mark script generation as complete in tran_status table.
        
        Args:
            run_id: The run ID for this execution
            
        Returns:
            True if update was successful, False otherwise
        """
        try:
            import json
            
            # Get current status
            select_sql = text(f"""
                SELECT succ_steps
                FROM {PGSCHEMA}.tran_status 
                WHERE runid = :runid
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                result = conn.execute(select_sql, {"runid": run_id})
                row = result.fetchone()
                
                if not row:
                    self.logger.warning(f"⚠️  No tran_status entry found for run ID: {run_id}")
                    return False
                
                # Parse existing status
                current_steps_data = row[0]
                if isinstance(current_steps_data, str):
                    current_steps_obj = json.loads(current_steps_data)
                else:
                    current_steps_obj = current_steps_data
                
                current_status = current_steps_obj.get("status", "")
                
                # Append script generation completion message
                new_status = f"{current_status} | Script generation has been completed successfully"
                
                # Update database
                update_sql = text(f"""
                    UPDATE {PGSCHEMA}.tran_status 
                    SET succ_steps = :succ_steps
                    WHERE runid = :runid
                """)
                
                updated_steps_json = json.dumps({"status": new_status}, ensure_ascii=False)
                conn.execute(update_sql, {
                    "runid": run_id,
                    "succ_steps": updated_steps_json
                })
            
            self.logger.info(f"✨ Marked script generation complete in tran_status for run ID: {run_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to mark script generation complete for run ID {run_id}: {str(e)}")
            return False
    
    def delete_tran_status(self, run_id: str, allow_inner_deletion: bool = False) -> bool:
        """
        Delete the tran_status entry for a run ID (called on completion or failure).
        
        Args:
            run_id: The run ID to delete
            allow_inner_deletion: If False (default), prevents deletion from inner processing layers
                                 to ensure tran_status remains available for outer exception handlers.
                                 Set to True only when calling from top-level exception handlers.
            
        Returns:
            True if deletion was successful, False otherwise
        """
        try:
            # 🛑 GUARD: Prevent premature deletion from inner processing layers
            # Only allow deletion from outer exception handlers to ensure tran_status
            # remains available for database storage
            if not allow_inner_deletion:
                self.logger.info(f"⏭️  Skipping inner deletion for run_id: {run_id} (requires allow_inner_deletion=True)")
                return False  # Return False to indicate deletion was blocked
            
            delete_sql = text(f"""
                DELETE FROM {PGSCHEMA}.tran_status 
                WHERE runid = :runid
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                result = conn.execute(delete_sql, {"runid": run_id})
                
            self.logger.info(f"🗑️  Deleted tran_status entry for run ID: {run_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to delete tran_status for run ID {run_id}: {str(e)}")
            return False
    
    def get_tran_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the current status from tran_status table.
        
        Args:
            run_id: The run ID to retrieve status for
            
        Returns:
            Dict with 'steps', 'phase', and 'scenario_name' or None if not found
        """
        try:
            import json
            
            select_sql = text(f"""
                SELECT succ_steps, status, scenarioname
                FROM {PGSCHEMA}.tran_status 
                WHERE runid = :runid
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                result = conn.execute(select_sql, {"runid": run_id})
                row = result.fetchone()
                
                if not row:
                    return None
                
                # Parse status
                steps_data = row[0]
                phase = row[1] or "IN_PROGRESS"
                scenario_name = row[2]
                
                if isinstance(steps_data, str):
                    steps_obj = json.loads(steps_data)
                else:
                    steps_obj = steps_data
                
                return {
                    "steps": steps_obj.get("status", ""),
                    "phase": phase,
                    "scenario_name": scenario_name,
                    "current_step": steps_obj.get("current_step")  # Add current executing step
                }
            
        except Exception as e:
            self.logger.error(f"❌ Failed to get tran_status for run ID {run_id}: {str(e)}")
            return None
    
    def update_tran_status_phase(self, run_id: str, phase: str) -> bool:
        """
        Update the execution phase in tran_status table.
        
        Args:
            run_id: The run ID to update
            phase: New phase (currently only 'IN_PROGRESS' is used)
            
        Returns:
            True if update was successful, False otherwise
        """
        try:
            update_sql = text(f"""
                UPDATE {PGSCHEMA}.tran_status 
                SET status = :status
                WHERE runid = :runid
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                conn.execute(update_sql, {
                    "runid": run_id,
                    "status": phase
                })
            
            self.logger.info(f"🔄 Updated tran_status phase for run ID {run_id}: {phase}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to update tran_status phase for run ID {run_id}: {str(e)}")
            return False
    
    def reset_tran_status_steps(self, run_id: str, scenario_name: str = None) -> bool:
        """
        Reset the steps string for a new scenario and update scenario name.
        Called at the start of each new scenario (scenario 2, 3, etc.) to clear
        accumulated steps from previous scenarios.
        
        Args:
            run_id: The run ID to reset steps for
            scenario_name: The name of the new scenario (optional, but recommended)
            
        Returns:
            True if reset was successful, False otherwise
        """
        try:
            import json
            
            # Initial status for new scenario
            initial_status = "New scenario started - waiting for first step"
            succ_steps_json = json.dumps({
                "status": initial_status, 
                "current_step": None
            }, ensure_ascii=False)
            
            # Update both succ_steps and scenarioname if provided
            if scenario_name:
                update_sql = text(f"""
                    UPDATE {PGSCHEMA}.tran_status 
                    SET succ_steps = :succ_steps,
                        scenarioname = :scenario_name
                    WHERE runid = :runid
                """)
            else:
                update_sql = text(f"""
                    UPDATE {PGSCHEMA}.tran_status 
                    SET succ_steps = :succ_steps
                    WHERE runid = :runid
                """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                if scenario_name:
                    conn.execute(update_sql, {
                        "runid": run_id,
                        "succ_steps": succ_steps_json,
                        "scenario_name": scenario_name
                    })
                    self.logger.info(f"🔄 Reset steps and updated scenario name to '{scenario_name}' for run ID: {run_id}")
                else:
                    conn.execute(update_sql, {
                        "runid": run_id,
                        "succ_steps": succ_steps_json
                    })
                    self.logger.info(f"🔄 Reset steps for new scenario in run ID: {run_id}")
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Error resetting steps for run ID {run_id}: {str(e)}")
            return False
    
    def get_tran_status_steps(self, run_id: str) -> list:
        """
        Extract the successful steps from tran_status for a given run_id.
        Used when a scenario fails to get the steps that completed before failure.
        
        Args:
            run_id: The run ID to get steps for
            
        Returns:
            List of step strings that were completed before failure
        """
        try:
            import json
            
            query_sql = text(f"""
                SELECT succ_steps
                FROM {PGSCHEMA}.tran_status 
                WHERE runid = :runid
            """)
            
            eng = _get_engine(BASE_DATABASE_URL)
            with eng.begin() as conn:
                result = conn.execute(query_sql, {"runid": run_id})
                row = result.fetchone()
                
                if not row or not row.succ_steps:
                    return []
                
                succ_steps = row.succ_steps
                steps_list = []
                
                # Parse JSON structure
                if isinstance(succ_steps, dict):
                    # Handle {"status": "step1|step2|step3", "current_step": "..."}
                    if 'status' in succ_steps and isinstance(succ_steps['status'], str):
                        # Split by pipe separator and filter internal messages
                        internal_keywords = ["Test Execution", "Dry run", "Script generation", "New scenario started"]
                        raw_steps = succ_steps['status'].split('|')
                        for step in raw_steps:
                            step = step.strip()
                            if step and not any(keyword in step for keyword in internal_keywords):
                                # Extract step text from "Step 'xxx' has been executed successfully" format
                                if "Step '" in step and "' has been executed successfully" in step:
                                    start_idx = step.find("Step '") + 6
                                    end_idx = step.find("' has been executed successfully")
                                    step_text = step[start_idx:end_idx] if start_idx < end_idx else step
                                    steps_list.append(step_text)
                                else:
                                    steps_list.append(step)
                elif isinstance(succ_steps, str):
                    # Try to parse as JSON
                    try:
                        succ_steps_dict = json.loads(succ_steps)
                        if isinstance(succ_steps_dict, dict) and 'status' in succ_steps_dict:
                            internal_keywords = ["Test Execution", "Dry run", "Script generation", "New scenario started"]
                            raw_steps = succ_steps_dict['status'].split('|')
                            for step in raw_steps:
                                step = step.strip()
                                if step and not any(keyword in step for keyword in internal_keywords):
                                    # Extract step text from "Step 'xxx' has been executed successfully" format
                                    if "Step '" in step and "' has been executed successfully" in step:
                                        start_idx = step.find("Step '") + 6
                                        end_idx = step.find("' has been executed successfully")
                                        step_text = step[start_idx:end_idx] if start_idx < end_idx else step
                                        steps_list.append(step_text)
                                    else:
                                        steps_list.append(step)
                    except json.JSONDecodeError:
                        pass
                
                return steps_list
            
        except Exception as e:
            self.logger.error(f"❌ Error getting steps for run ID {run_id}: {str(e)}")
            return []
    
    # ============================================================================
    # END OF TRAN_STATUS TABLE OPERATIONS
    # ============================================================================
    
    def update_response_status(self, run_id: str, status: str, 
                             error_message: Optional[str] = None) -> bool:
        """
        Update the status of a stored response.
        
        Args:
            run_id: The run ID to update
            status: New status (e.g., "success", "failure", "processing")
            error_message: Optional error message for failure cases
            
        Returns:
            True if update was successful, False otherwise
        """
        try:
            # First, get existing data
            existing_data = self.get_stored_response(run_id)
            if not existing_data:
                self.logger.warning(f"⚠️  No existing data found for run ID: {run_id}")
                return False
            
            # Update status in the JSON data
            existing_data["status"] = status
            existing_data["updated_at"] = datetime.now().isoformat()
            if error_message:
                existing_data["error_message"] = error_message
            
            # Store the updated data
            jira_id = existing_data.get("jira_id")
            result = self.store_generated_response(run_id, existing_data, jira_id)
            
            if result:
                self.logger.info(f"✅ Updated status for run ID {run_id}: {status}")
                return True
            else:
                self.logger.warning(f"⚠️  Failed to update status for run ID: {run_id}")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Failed to update status for run ID {run_id}: {str(e)}")
            return False
    
    def update_run_id_if_auto_generated(self, current_run_id: str, session_id: Optional[str] = None) -> Tuple[str, bool]:
        """
        This method is kept for backward compatibility but no longer used.
        Run IDs are now generated with millisecond timestamps and not updated.
        
        Args:
            current_run_id: The current run ID being used
            session_id: Optional session ID (ignored)
            
        Returns:
            Tuple of (current_run_id, False) - no updates performed
        """
        self.logger.info(f"🔒 Run ID format is now timestamp-based, not updating: {current_run_id}")
        return current_run_id, False

    def create_response_summary(self, run_id: str, response_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a response summary that includes the run ID for notifications.
        
        Args:
            run_id: The run ID to include
            response_data: The original response data
            
        Returns:
            Enhanced response data with run ID included
        """
        # Create a copy to avoid modifying original
        summary = response_data.copy()
        
        # Add run ID to the summary
        summary["run_id"] = run_id
        
        # Ensure run ID appears in key locations for notifications
        if "status" in summary:
            status_msg = summary.get("message", "")
            if run_id not in status_msg:
                summary["message"] = f"{status_msg} [Run ID: {run_id}]".strip()
        
        return summary


# Global instance for easy access
run_id_manager = RunIdManager()


def validate_run_id(run_id: Optional[str]) -> Tuple[bool, str]:
    """Convenience function to validate run ID format."""
    return run_id_manager.validate_run_id(run_id)


def generate_run_id(prefix: str = "auto") -> str:
    """Convenience function to generate a new run ID."""
    return run_id_manager.generate_run_id(prefix)


def resolve_run_id(user_run_id: Optional[str] = None, 
                  generation_prefix: str = "auto") -> Tuple[str, bool]:
    """Convenience function to resolve run ID."""
    return run_id_manager.resolve_run_id(user_run_id, generation_prefix)


def update_run_id_if_auto_generated(current_run_id: str, session_id: Optional[str] = None) -> Tuple[str, bool]:
    """Convenience function - kept for backward compatibility but no longer updates run IDs."""
    return run_id_manager.update_run_id_if_auto_generated(current_run_id, session_id)


def store_generated_response(run_id: str, response_data: Dict[str, Any],
                            jira_id: Optional[str] = None, module_name: Optional[str] = None,
                            status: Optional[str] = None, scenario_name: Optional[str] = None,
                            video_filename: Optional[str] = None, complete_scripts: Optional[str] = None,
                            feature_steps: Optional[str] = None, feature_tags: Optional[str] = None,
                            feature_nm: Optional[str] = None, scn_tags: Optional[str] = None,
                            scn_nm: Optional[str] = None, stp_loc_map: Optional[str] = None,
                            api_steps: Optional[str] = None) -> bool:
     """Convenience function to store response data with enhanced parameters."""
     return run_id_manager.store_generated_response(run_id, response_data, jira_id, module_name, status, scenario_name, video_filename, complete_scripts, feature_steps, feature_tags, feature_nm, scn_tags, scn_nm, stp_loc_map, api_steps)


def update_complete_scripts_for_runid(run_id: str, complete_scripts: str) -> bool:
    """Convenience function to update complt_scripts for all rows of a run_id."""
    return run_id_manager.update_complete_scripts_for_runid(run_id, complete_scripts)


def get_stored_response(run_id: str, scenario_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Convenience function to retrieve response data. Optionally filter by scenario name."""
    return run_id_manager.get_stored_response(run_id, scenario_name)


def get_all_scenarios_by_runid(run_id: str) -> List[Dict[str, Any]]:
    """
    Convenience function to retrieve all scenarios for a given run ID.
    
    Returns list of scenario dicts with: scenario_name, status, module_name, response, execution_dt
    """
    return run_id_manager.get_all_scenarios_by_runid(run_id)


def update_response_status(run_id: str, status: str, 
                         error_message: Optional[str] = None) -> bool:
    """Convenience function to update response status."""
    return run_id_manager.update_response_status(run_id, status, error_message)


def create_response_summary_with_run_id(run_id: str, response_data: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience function to create response summary with run ID."""
    return run_id_manager.create_response_summary(run_id, response_data)


# ============================================================================
# TRAN_STATUS CONVENIENCE FUNCTIONS
# ============================================================================

def init_tran_status(run_id: str, module_name: Optional[str] = None, 
                    scenario_name: Optional[str] = None) -> bool:
    """Convenience function to initialize tran_status entry."""
    return run_id_manager.init_tran_status(run_id, module_name, scenario_name)


def set_current_executing_step(run_id: str, step_sentence: str) -> bool:
    """Convenience function to set current executing step."""
    return run_id_manager.set_current_executing_step(run_id, step_sentence)


def append_step_to_tran_status(run_id: str, step_sentence: str) -> bool:
    """Convenience function to append step to tran_status."""
    return run_id_manager.append_step_to_tran_status(run_id, step_sentence)


def reset_tran_status_steps(run_id: str, scenario_name: str = None) -> bool:
    """Convenience function to reset steps for new scenario."""
    return run_id_manager.reset_tran_status_steps(run_id, scenario_name)


def get_tran_status_steps(run_id: str) -> list:
    """Convenience function to get successful steps from tran_status."""
    return run_id_manager.get_tran_status_steps(run_id)


def mark_dry_run_complete(run_id: str) -> bool:
    """Convenience function to mark dry run complete in tran_status."""
    return run_id_manager.mark_dry_run_complete(run_id)


def mark_script_generation_complete(run_id: str) -> bool:
    """Convenience function to mark script generation complete in tran_status."""
    return run_id_manager.mark_script_generation_complete(run_id)


def delete_tran_status(run_id: str, allow_inner_deletion: bool = False) -> bool:
    """Convenience function to delete tran_status entry."""
    return run_id_manager.delete_tran_status(run_id, allow_inner_deletion)


def get_tran_status(run_id: str) -> Optional[Dict[str, Any]]:
    """Convenience function to get current tran_status with phase."""
    return run_id_manager.get_tran_status(run_id)


def update_tran_status_phase(run_id: str, phase: str) -> bool:
    """Convenience function to update tran_status execution phase."""
    return run_id_manager.update_tran_status_phase(run_id, phase)

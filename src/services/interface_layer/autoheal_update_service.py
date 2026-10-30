"""
Auto-Heal Update Service

Handles updating the t_autoHealScn table when upload-feature-file API completes
with a run_id containing the phrase "autoheal".

This service:
1. Detects if a run_id contains "autoheal"
2. Extracts the original run_id (without "autoheal")
3. Compares the newly generated stp_loc_map with the stored one
4. Updates t_autoHealScn with appropriate columns based on differences
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from sqlalchemy import text

from utils.db_utils import PGSCHEMA, engine

logger = logging.getLogger(__name__)


class AutoHealUpdateService:
    """Service for updating t_autoHealScn table during upload-feature-file completion"""
    
    @staticmethod
    def _extract_original_runid(run_id: str) -> str:
        """
        Extract original run_id by removing 'autoheal' phrase.
        
        Example: "internal_444af28463404096a273a5906b9fbe20autoheal" 
                 → "internal_444af28463404096a273a5906b9fbe20"
        
        Args:
            run_id: The run_id to process
            
        Returns:
            Original run_id without 'autoheal' phrase
        """
        if not run_id:
            return run_id
        
        # Remove 'autoheal' case-insensitively from the end
        run_id_lower = run_id.lower()
        if run_id_lower.endswith("autoheal"):
            # Remove the last 8 characters ('autoheal')
            return run_id[:-8]
        
        return run_id
    
    @staticmethod
    def _compare_stp_loc_maps(old_map: Optional[Any], new_map: Optional[Any]) -> Tuple[bool, str]:
        """
        Compare two stp_loc_map objects to detect differences in locators.
        
        Handles nested array format: {"steps": [{"stepname": "...", "locator": "..."}]}
        as well as legacy flat format: {step_name: locator}
        
        Args:
            old_map: Previously stored stp_loc_map (JSON string or dict)
            new_map: Newly generated stp_loc_map (JSON string or dict)
            
        Returns:
            Tuple of (has_differences: bool, diff_summary: str)
        """
        try:
            # Parse JSON if strings
            old_data = old_map
            new_data = new_map
            
            if isinstance(old_map, str):
                try:
                    old_data = json.loads(old_map) if old_map.strip() else {}
                except (json.JSONDecodeError, AttributeError):
                    old_data = {}
            
            if isinstance(new_map, str):
                try:
                    new_data = json.loads(new_map) if new_map.strip() else {}
                except (json.JSONDecodeError, AttributeError):
                    new_data = {}
            
            # Convert to dicts if None
            old_data = old_data or {}
            new_data = new_data or {}
            
            # If both are empty, no differences
            if not old_data and not new_data:
                return False, "Both maps are empty"
            
            # Extract steps based on format
            # Format 1: Nested array format {"steps": [{"stepname": "...", "locator": "..."}]}
            # Format 2: Legacy flat format {step_name: locator}
            old_steps = AutoHealUpdateService._extract_steps_dict(old_data)
            new_steps = AutoHealUpdateService._extract_steps_dict(new_data)
            
            # If both are empty after extraction
            if not old_steps and not new_steps:
                return False, "Both maps have no steps"
            
            # Check for step count change
            if len(old_steps) != len(new_steps):
                return True, f"Step count changed: {len(old_steps)} → {len(new_steps)}"
            
            # Compare locators for each step by step name
            differences = []
            all_step_names = set(old_steps.keys()) | set(new_steps.keys())
            
            for step_name in sorted(all_step_names):
                old_locator = old_steps.get(step_name, "")
                new_locator = new_steps.get(step_name, "")
                
                # Normalize locators for comparison (remove extra whitespace)
                old_normalized = str(old_locator or "").strip()
                new_normalized = str(new_locator or "").strip()
                
                if old_normalized != new_normalized:
                    # Only log first 80 chars of locators for readability
                    old_display = (old_normalized[:80] + "...") if len(old_normalized) > 80 else old_normalized
                    new_display = (new_normalized[:80] + "...") if len(new_normalized) > 80 else new_normalized
                    differences.append(f"Step '{step_name}': '{old_display}' → '{new_display}'")
            
            if differences:
                return True, f"Locator differences found: {len(differences)} step(s) changed"
            
            return False, "No locator differences found"
            
        except Exception as e:
            logger.error(f"Error comparing stp_loc_maps: {e}")
            # If comparison fails, assume there are differences to be safe
            return True, f"Comparison error: {str(e)}"
    
    @staticmethod
    def _extract_steps_dict(data: Dict[str, Any]) -> Dict[str, str]:
        """
        Extract steps as a flat dictionary {stepname: locator}.
        
        Handles nested array format: {"steps": [{"stepname": "...", "locator": "..."}]}
        as well as legacy flat format where the dict itself is {step_name: locator}
        
        Args:
            data: The stp_loc_map data structure
            
        Returns:
            Dict mapping step names to locators
        """
        if not data:
            return {}
        
        # Check if this is the nested array format
        if "steps" in data and isinstance(data["steps"], list):
            steps_dict = {}
            for step_obj in data["steps"]:
                if isinstance(step_obj, dict):
                    step_name = step_obj.get("stepname") or step_obj.get("step_name")
                    locator = step_obj.get("locator")
                    if step_name:
                        steps_dict[step_name] = locator or ""
            return steps_dict
        
        # Otherwise treat it as a flat format, but exclude known non-step keys
        # that might exist at the root level
        non_step_keys = {"steps", "metadata", "description", "created_at", "updated_at"}
        steps_dict = {
            k: v for k, v in data.items() 
            if k not in non_step_keys and not k.startswith("_")
        }
        return steps_dict
    
    @staticmethod
    def _fetch_from_gen_resp(run_id: str) -> Tuple[Optional[Any], Optional[str]]:
        """
        Fetch stp_loc_map and complete_scripts from t_gen_resp table.
        
        Args:
            run_id: The run_id to query
            
        Returns:
            Tuple of (stp_loc_map, complete_scripts)
        """
        try:
            query = text(f"""
                SELECT "stp_loc_map", "complt_scripts"
                FROM {PGSCHEMA}.t_gen_resp
                WHERE "runid" = :runid
                LIMIT 1
            """)
            
            with engine.begin() as conn:
                result = conn.execute(query, {"runid": run_id})
                row = result.fetchone()
            
            if row:
                return row[0], row[1]
            
            return None, None
        
        except Exception as e:
            logger.warning(f"Failed to fetch data from t_gen_resp: {e}")
            return None, None
    
    @staticmethod
    def update_autoheal_on_completion(
        run_id: str,
        new_stp_loc_map: Optional[Any] = None,
        new_complete_scripts: Optional[str] = None,
        module_name: Optional[str] = None,
        feature_tags: Optional[str] = None,
        feature_name: Optional[str] = None,
        scenario_tags: Optional[str] = None,
        scenario_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Update t_autoHealScn table if run_id contains "autoheal".
        
        Processing logic:
        1. Check if run_id contains phrase "autoheal"
        2. Extract original run_id (without "autoheal")
        3. Query t_autoHealScn with the original run_id
        4. Fetch newly generated data from t_gen_resp if not provided
        5. Compare stp_loc_map data
        6. Update table based on differences
        
        Args:
            run_id: The run_id from upload-feature-file
            new_stp_loc_map: Newly generated stp_loc_map (optional, will fetch from t_gen_resp if not provided)
            new_complete_scripts: Newly generated TypeScript scripts (optional, will fetch from t_gen_resp if not provided)
            module_name: Optional module name for additional filtering
            feature_tags: Optional feature tags for additional filtering
            feature_name: Optional feature name for additional filtering
            scenario_tags: Optional scenario tags for additional filtering
            scenario_name: Optional scenario name for additional filtering
            
        Returns:
            Dict with update status and details
        """
        try:
            # Step 1: Check if run_id contains "autoheal" as a suffix
            # Must match extraction logic in _extract_original_runid which uses endswith()
            if not run_id or not run_id.lower().endswith("autoheal"):
                logger.info(f"Auto-heal: Run ID does not end with 'autoheal' phrase - no update needed: {run_id}")
                return {
                    "status": "skipped",
                    "reason": "Run ID does not contain 'autoheal' suffix",
                    "run_id": run_id
                }
            
            logger.info(f"Auto-heal: Detected 'autoheal' suffix in run_id: {run_id}")
            
            # Step 2: Extract original run_id
            original_run_id = AutoHealUpdateService._extract_original_runid(run_id)
            logger.info(f"Auto-heal: Extracted original run_id: {original_run_id}")
            
            # If stp_loc_map or scripts not provided, fetch from t_gen_resp
            # IMPORTANT: Use full run_id (with "autoheal"), not original_run_id, because
            # store_generated_response() stores data under resolved_run_id which includes "autoheal"
            if new_stp_loc_map is None or new_complete_scripts is None:
                logger.info(f"Auto-heal: Fetching data from t_gen_resp for run_id: {run_id}")
                fetched_map, fetched_scripts = AutoHealUpdateService._fetch_from_gen_resp(run_id)
                
                if new_stp_loc_map is None:
                    new_stp_loc_map = fetched_map
                    if fetched_map:
                        logger.info("Auto-heal: Retrieved stp_loc_map from t_gen_resp")
                    else:
                        logger.warning("Auto-heal: No stp_loc_map found in t_gen_resp for this run_id")
                
                if new_complete_scripts is None:
                    new_complete_scripts = fetched_scripts
                    if fetched_scripts:
                        logger.info("Auto-heal: Retrieved complete_scripts from t_gen_resp")
                    else:
                        logger.warning("Auto-heal: No complete_scripts found in t_gen_resp for this run_id")
                
                if new_stp_loc_map is None or new_complete_scripts is None:
                    logger.warning(f"Auto-heal: Failed to fetch required data from t_gen_resp - aborting auto-heal update (map={new_stp_loc_map is not None}, scripts={new_complete_scripts is not None})")
                    return {
                        "status": "fetch_failed",
                        "reason": "Could not retrieve required data from t_gen_resp (stp_loc_map or complete_scripts missing)",
                        "run_id": run_id
                    }
            
            # Step 3: Query t_autoHealScn with the original run_id
            query_filters = ["\"runid\" = :runid"]
            query_params = {"runid": original_run_id}
            
            # Add optional filters for more precise matching
            if module_name:
                query_filters.append("\"modulenm\" = :modulenm")
                query_params["modulenm"] = module_name
            
            if feature_tags:
                query_filters.append("REPLACE(\"feature_tags\", '@', '') = REPLACE(:feature_tags, '@', '')")
                query_params["feature_tags"] = feature_tags
            
            if scenario_tags:
                query_filters.append("REPLACE(\"scn_tags\", '@', '') = REPLACE(:scn_tags, '@', '')")
                query_params["scn_tags"] = scenario_tags
            
            if scenario_name:
                query_filters.append("\"scenarioname\" = :scenarioname")
                query_params["scenarioname"] = scenario_name
            
            if feature_name:
                query_filters.append("\"feature_nm\" = :feature_nm")
                query_params["feature_nm"] = feature_name
            
            where_clause = " AND ".join(query_filters)
            
            select_query = text(f"""
                SELECT "id", "stp_loc_map", "runid", "modulenm", "feature_tags", 
                       "scn_tags", "scenarioname"
                FROM {PGSCHEMA}."t_autoHealScn"
                WHERE {where_clause}
                LIMIT 1
            """)
            
            with engine.begin() as conn:
                result = conn.execute(select_query, query_params)
                row = result.fetchone()
            
            if not row:
                logger.warning(f"Auto-heal: No matching row found in t_autoHealScn for: {original_run_id}")
                return {
                    "status": "no_match",
                    "reason": "No data row identified",
                    "original_run_id": original_run_id
                }
            
            logger.info(f"Auto-heal: Found matching row in t_autoHealScn (id={row[0]})")
            
            # Extract stored stp_loc_map
            stored_stp_loc_map = row[1]
            
            # Step 4: Compare stp_loc_map data
            has_differences, diff_summary = AutoHealUpdateService._compare_stp_loc_maps(
                stored_stp_loc_map, 
                new_stp_loc_map
            )
            
            logger.info(f"Auto-heal: Comparison result - Differences: {has_differences}, Summary: {diff_summary}")
            
            # Step 5: Update table based on differences
            current_dt = datetime.utcnow()
            
            if has_differences:
                # Update with auto-healing complete - Scripts required
                logger.info(f"Auto-heal: Updating with script changes for row id={row[0]}")
                
                # Serialize new_stp_loc_map to JSON string if needed
                # Safety check: Don't store None as JSON "null" - this prevents database corruption
                if new_stp_loc_map is None:
                    logger.error(f"Auto-heal: Unexpected None value for new_stp_loc_map at update stage - this should have been caught earlier")
                    new_stp_loc_map_str = json.dumps({})
                else:
                    new_stp_loc_map_str = new_stp_loc_map if isinstance(new_stp_loc_map, str) else json.dumps(new_stp_loc_map)
                
                update_query = text(f"""
                    UPDATE {PGSCHEMA}."t_autoHealScn"
                    SET "updated_stp_loc_map" = :updated_stp_loc_map,
                        "updated_script" = :updated_script,
                        "autoheal_dt" = :autoheal_dt,
                        "stat_msg" = :stat_msg
                    WHERE "id" = :id
                """)
                
                with engine.begin() as conn:
                    conn.execute(update_query, {
                        "id": row[0],
                        "updated_stp_loc_map": new_stp_loc_map_str,
                        "updated_script": new_complete_scripts,
                        "autoheal_dt": current_dt,
                        "stat_msg": "Auto-healing complete, Scripts are required to be updated"
                    })
                
                logger.info(f"Auto-heal: Successfully updated row id={row[0]} with script changes")
                
                return {
                    "status": "updated_with_changes",
                    "message": "Auto-healing complete, Scripts are required to be updated",
                    "original_run_id": original_run_id,
                    "autoheal_runid": run_id,
                    "updated_row_id": row[0],
                    "differences_found": True,
                    "diff_summary": diff_summary,
                    "updated_at": current_dt.isoformat()
                }
            else:
                # Update with auto-healing complete - No changes needed
                logger.info(f"Auto-heal: Updating with no script changes for row id={row[0]}")
                
                update_query = text(f"""
                    UPDATE {PGSCHEMA}."t_autoHealScn"
                    SET "update_dt" = :update_dt,
                        "autoheal_dt" = :autoheal_dt,
                        "stat_msg" = :stat_msg
                    WHERE "id" = :id
                """)
                
                with engine.begin() as conn:
                    conn.execute(update_query, {
                        "id": row[0],
                        "update_dt": current_dt,
                        "autoheal_dt": current_dt,
                        "stat_msg": "Auto-healing complete, No Script update is required"
                    })
                
                logger.info(f"Auto-heal: Successfully updated row id={row[0]} with no script changes")
                
                return {
                    "status": "updated_no_changes",
                    "message": "Auto-healing complete, No Script update is required",
                    "original_run_id": original_run_id,
                    "autoheal_runid": run_id,
                    "updated_row_id": row[0],
                    "differences_found": False,
                    "diff_summary": diff_summary,
                    "updated_at": current_dt.isoformat()
                }
        
        except Exception as e:
            logger.error(f"Auto-heal: Error updating t_autoHealScn table: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "run_id": run_id
            }

import logging
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from enum import Enum
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from sqlalchemy import text
from utils.db_utils import engine, PGSCHEMA
from services.integration_layer.notifications.email_notifier import send_run_summary_email
import httpx
from zoneinfo import ZoneInfo

# Initialize logger
logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/Web-Automation", tags=["Auto-Heal"])

# Load module names from configuration file
def _load_module_names() -> List[str]:
    """Load module names from onboardingConfiguration.json"""
    try:
        config_path = Path(__file__).parent.parent.parent / "onboardingConfiguration.json"
        with open(config_path, 'r') as f:
            config = json.load(f)
        # Extract domain values from the configuration
        module_names = [item.get("domain") for item in config if item.get("domain")]
        logger.info(f"Loaded {len(module_names)} module names from configuration: {module_names}")
        return module_names
    except Exception as e:
        logger.error(f"Error loading module names from configuration: {str(e)}")
        return []

# Load full repo modules map (domain -> config) for notifications
def load_repo_modules() -> Dict[str, Dict[str, Any]]:
    """Load repository modules from onboardingConfiguration.json and return formatted options"""
    config_path = Path(__file__).parent.parent.parent / "onboardingConfiguration.json"
    with open(config_path, "r", encoding="utf-8") as f:
        modules_data = json.load(f) or []
    formatted_options: Dict[str, Dict[str, Any]] = {}
    for item in modules_data:
        if isinstance(item, dict) and "domain" in item:
            domain_name = item["domain"]
            formatted_options[domain_name] = item
    return formatted_options

# Helper to build absolute URL
def _abs_url(request: Request, url: str) -> str:
    base = str(request.base_url).rstrip("/")
    return url if str(url).startswith("http") else f"{base}{url if str(url).startswith('/') else '/' + str(url)}"

# Load module names at startup
AVAILABLE_MODULE_NAMES = _load_module_names()

# Dynamically create ModuleName Enum from configuration
if AVAILABLE_MODULE_NAMES:
    ModuleName = Enum('ModuleName', {name: name for name in AVAILABLE_MODULE_NAMES})
else:
    # Fallback if no modules are loaded
    ModuleName = Enum('ModuleName', {'Payment': 'Payment'})

# Define response types as Enum for Swagger dropdown
class ResponseType(str, Enum):
    """Response type options for the Auto-Heal API"""
    all = "all"
    feature = "feature"
    completeScripts = "completeScripts"
    updatedscrips = "updatedscrips"
    previousmap = "previousmap"
    updatedmap = "updatedmap"

# Valid response types
VALID_RESPONSE_TYPES = [rt.value for rt in ResponseType]


def _generate_response_id() -> str:
    """Generate a unique response ID."""
    from datetime import datetime
    now = datetime.utcnow()
    return f"RESP-{now.strftime('%Y-%m-%d')}-{str(now.timestamp()).split('.')[0][-3:]}"


def _build_autoheal_response_object(row: Dict[str, Any]) -> Dict[str, Any]:

    return {
        "runid": row.get("runid"),
        "module_name": row.get("modulenm"),
        "feature_tag": row.get("feature_tags"),
        "feature_name": row.get("feature_nm"),
        "scenario_tag": row.get("scn_tags"),
        "scenario_name": row.get("scenarioname"),
        "feature_steps": bool(row.get("feature_steps")),
        "old_step_loc_map": bool(row.get("stp_loc_map")),
        "updated_step_loc_map": bool(row.get("updated_stp_loc_map")),
        "complete_script": bool(row.get("complt_scripts")),
        "updated_complete_script": bool(row.get("updated_script")),
        "api_step": bool(row.get("api_steps")),
        "creation_date": row.get("update_dt"),
        "autoheal_date": row.get("autoheal_dt"),
        "status": row.get("stat_msg"),
    }


def _build_feature_response_object(row: Dict[str, Any]) -> Dict[str, Any]:
    """Build response object for feature response type."""
    return {
        "feature_steps": row.get("feature_steps")
    }


def _build_complete_scripts_response_object(row: Dict[str, Any]) -> Dict[str, Any]:
    """Build response object for completeScripts response type."""
    return {
        "complete_script": row.get("complt_scripts")
    }


def _build_updated_scripts_response_object(row: Dict[str, Any]) -> Dict[str, Any]:
    """Build response object for updatedscrips response type."""
    return {
        "updated_complete_script": row.get("updated_script")
    }


def _build_previous_map_response_object(row: Dict[str, Any]) -> Dict[str, Any]:
    """Build response object for previousmap response type."""
    return {
        "old_step_loc_map": row.get("stp_loc_map")
    }


def _build_updated_map_response_object(row: Dict[str, Any]) -> Dict[str, Any]:
    """Build response object for updatedmap response type."""
    return {
        "updated_step_loc_map": row.get("updated_stp_loc_map")
    }


def _build_query_for_response_type(
    response_type: str,
    modulename: str,
    featuretags: Optional[str] = None,
    scenariotags: Optional[str] = None,
    scenarioname: Optional[str] = None,
) -> tuple:
    
    # Build WHERE clause
    where_conditions = []
    params = {}
    
    if modulename:
        where_conditions.append("modulenm = :modulename")
        params["modulename"] = modulename
    
    if featuretags:
        where_conditions.append("feature_tags = :featuretags")
        params["featuretags"] = featuretags
    
    if scenariotags:
        where_conditions.append("scn_tags = :scenariotags")
        params["scenariotags"] = scenariotags
    
    if scenarioname:
        where_conditions.append("scenarioname = :scenarioname")
        params["scenarioname"] = scenarioname
    
    where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"
    
    # Build SELECT clause based on response type
    if response_type == "all":
        select_cols = "*"
    elif response_type == "feature":
        select_cols = "feature_steps"
    elif response_type == "completeScripts":
        select_cols = "complt_scripts"
    elif response_type == "updatedscrips":
        select_cols = "updated_script"
    elif response_type == "previousmap":
        select_cols = "stp_loc_map"
    elif response_type == "updatedmap":
        select_cols = "updated_stp_loc_map"
    else:
        select_cols = "*"
    
    # Build the full query - use double quotes for case-sensitive table name
    sql = f"""
        SELECT {select_cols}
        FROM {PGSCHEMA}."t_autoHealScn"
        WHERE {where_clause}
        ORDER BY update_dt DESC
    """
    
    return sql, params


@router.get("/getAutoHealData")
async def get_autoheal_data(
    modulename: ModuleName = Query(..., description="Module name (mandatory - select from dropdown)"),
    responsetype: ResponseType = Query(..., description="Response type (mandatory - select from dropdown)"),
    featuretags: Optional[str] = Query(None, description="Feature tags (optional for 'all', mandatory for others)"),
    scenariotags: Optional[str] = Query(None, description="Scenario tags (optional for 'all', mandatory for others)"),
    scenarioname: Optional[str] = Query(None, description="Scenario name (optional for 'all', mandatory for others)"),
) -> Dict[str, Any]:
    
    try:
        # Convert Enum values to strings
        modulename_str = str(modulename.value) if modulename else None
        responsetype_str = responsetype.value if isinstance(responsetype, ResponseType) else str(responsetype)
        
        # Validate modulename is provided
        if not modulename_str:
            raise HTTPException(
                status_code=400,
                detail="modulename is mandatory. Please select a module from the dropdown."
            )
        
        # Validate response type
        if responsetype_str not in VALID_RESPONSE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid responsetype. Must be one of: {', '.join(VALID_RESPONSE_TYPES)}"
            )
        
        # Validate mandatory parameters for non-'all' response types
        if responsetype_str != "all":
            missing_params = []
            if not featuretags:
                missing_params.append("featuretags")
            if not scenariotags:
                missing_params.append("scenariotags")
            if not scenarioname:
                missing_params.append("scenarioname")
            
            if missing_params:
                raise HTTPException(
                    status_code=400,
                    detail=f"When responsetype is '{responsetype}', the following parameters are mandatory: {', '.join(missing_params)}"
                )
        
        # Build and execute query
        sql, params = _build_query_for_response_type(
            responsetype_str,
            modulename_str,
            featuretags,
            scenariotags,
            scenarioname
        )
        
        logger.info(f"Executing auto-heal query for module: {modulename_str}, responsetype: {responsetype_str}")
        
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            rows = result.mappings().all()
        
        # Build response based on response type
        autoheal_response = []
        
        if responsetype_str == "all":
            for row in rows:
                autoheal_response.append(_build_autoheal_response_object(dict(row)))
        elif responsetype_str == "feature":
            for row in rows:
                autoheal_response.append(_build_feature_response_object(dict(row)))
        elif responsetype_str == "completeScripts":
            for row in rows:
                autoheal_response.append(_build_complete_scripts_response_object(dict(row)))
        elif responsetype_str == "updatedscrips":
            for row in rows:
                autoheal_response.append(_build_updated_scripts_response_object(dict(row)))
        elif responsetype_str == "previousmap":
            for row in rows:
                autoheal_response.append(_build_previous_map_response_object(dict(row)))
        elif responsetype_str == "updatedmap":
            for row in rows:
                autoheal_response.append(_build_updated_map_response_object(dict(row)))
        
        # Build final response
        response = {
            "responseId": _generate_response_id(),
            "status": "SUCCESS",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "autohealResponse": autoheal_response
        }
        
        logger.info(f"Successfully retrieved {len(autoheal_response)} auto-heal records for module: {modulename_str}")
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving auto-heal data: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )


@router.post("/pushAutohealingData")
async def push_autohealing_data(
    modulename: str = Query(..., description="Module name (free text)"),
    featuretags: str = Query(..., description="Feature tags (free text)"),
    scenariotags: str = Query(..., description="Scenario tags (free text)"),
    scenarioname: str = Query(..., description="Scenario name (free text)"),
    action: str = Query(..., description="Action to perform: 'accept' or 'reject'"),
) -> Dict[str, Any]:
    try:
        # Validate action parameter
        action_lower = (action or "").strip().lower()
        if action_lower not in ["accept", "reject"]:
            raise HTTPException(
                status_code=400,
                detail="Invalid action parameter. Must be either 'accept' or 'reject'"
            )
        
        # Validate mandatory parameters
        if not modulename or not modulename.strip():
            raise HTTPException(status_code=400, detail="modulename is mandatory and cannot be empty")
        if not featuretags or not featuretags.strip():
            raise HTTPException(status_code=400, detail="featuretags is mandatory and cannot be empty")
        if not scenariotags or not scenariotags.strip():
            raise HTTPException(status_code=400, detail="scenariotags is mandatory and cannot be empty")
        if not scenarioname or not scenarioname.strip():
            raise HTTPException(status_code=400, detail="scenarioname is mandatory and cannot be empty")
        
        logger.info(f"pushAutohealingData: Processing {action_lower} action for scenario '{scenarioname}' in module '{modulename}'")
        
        current_dt = datetime.utcnow()
        with engine.begin() as conn:
            select_query = text(f"""
                SELECT "id", "updated_script", "complt_scripts", "updated_stp_loc_map", "stp_loc_map"
                FROM {PGSCHEMA}."t_autoHealScn"
                WHERE "modulenm" = :modulenm
                  AND "feature_tags" = :featuretags
                  AND "scn_tags" = :scenariotags
                  AND "scenarioname" = :scenarioname
                ORDER BY "id" DESC
                LIMIT 1
            """)
            
            result = conn.execute(select_query, {
                "modulenm": modulename,
                "featuretags": featuretags,
                "scenariotags": scenariotags,
                "scenarioname": scenarioname
            })
            row = result.fetchone()
        
        if not row:
            logger.warning(f"pushAutohealingData: No matching row found for modulename='{modulename}', featuretags='{featuretags}', scenariotags='{scenariotags}', scenarioname='{scenarioname}'")
            return {
                "status": "error",
                "message": "No matching scenario found in database",
                "details": {
                    "modulename": modulename,
                    "featuretags": featuretags,
                    "scenariotags": scenariotags,
                    "scenarioname": scenarioname
                }
            }
        
        row_id = row[0]
        updated_script = row[1]
        complt_scripts = row[2]
        updated_stp_loc_map = row[3]
        stp_loc_map = row[4]
        
        # Process based on action
        if action_lower == "accept":
            # In case of accept:
            # 1. Take the script from updated_script column and move to complt_scripts
            # 2. Take the locator map from updated_stp_loc_map and move to stp_loc_map
            # 3. Clear both updated columns by setting them to null
            
            if not updated_script:
                logger.warning(f"pushAutohealingData: accept action - No updated_script found for row id={row_id}")
                return {
                    "status": "error",
                    "message": "Cannot accept: no updated script available for this scenario",
                    "action": "accept"
                }
            
            logger.info(f"pushAutohealingData: accept action - Updating row id={row_id}: updated_script → complt_scripts, updated_stp_loc_map → stp_loc_map, then clearing updated columns")
            
            import json as _json
            updated_stp_loc_map_value = updated_stp_loc_map
            if isinstance(updated_stp_loc_map, dict):
                updated_stp_loc_map_value = _json.dumps(updated_stp_loc_map)
            
            update_query = text(f"""
                UPDATE {PGSCHEMA}."t_autoHealScn"
                SET "complt_scripts" = :updated_script,
                    "updated_script" = NULL,
                    "stp_loc_map" = :updated_stp_loc_map,
                    "updated_stp_loc_map" = NULL,
                    "update_dt" = :update_dt,
                    "stat_msg" = :stat_msg
                WHERE "id" = :id
            """)
            
            with engine.begin() as conn:
                conn.execute(update_query, {
                    "updated_script": updated_script,
                    "updated_stp_loc_map": updated_stp_loc_map_value,
                    "update_dt": current_dt,
                    "stat_msg": "Auto-healing changes accepted and applied",
                    "id": row_id
                })
            
            logger.info(f"pushAutohealingData: accept action - Successfully updated row id={row_id} (script and locator map)")
            
            return {
                "status": "success",
                "message": "Auto-healing changes accepted and applied (script and locator map)",
                "action": "accept",
                "details": {
                    "row_id": row_id,
                    "modulename": modulename,
                    "featuretags": featuretags,
                    "scenariotags": scenariotags,
                    "scenarioname": scenarioname,
                    "script_applied": True,
                    "locator_map_applied": True
                },
                "timestamp": current_dt.isoformat() + "Z"
            }
        
        else:  # action_lower == "reject"
            # In case of reject:
            # Remove both the updated_script and updated_stp_loc_map columns and make them null
            # This discards all auto-healing changes (both script and locator map)
            
            logger.info(f"pushAutohealingData: reject action - Clearing updated_script and updated_stp_loc_map for row id={row_id}")
            
            # Update the database - clear both healed script and healed locator map
            update_query = text(f"""
                UPDATE {PGSCHEMA}."t_autoHealScn"
                SET "updated_script" = NULL,
                    "updated_stp_loc_map" = NULL,
                    "update_dt" = :update_dt,
                    "stat_msg" = :stat_msg
                WHERE "id" = :id
            """)
            
            with engine.begin() as conn:
                conn.execute(update_query, {
                    "update_dt": current_dt,
                    "stat_msg": "Auto-healing changes rejected",
                    "id": row_id
                })
            
            logger.info(f"pushAutohealingData: reject action - Successfully updated row id={row_id} (cleared script and locator map)")
            
            return {
                "status": "success",
                "message": "Auto-healing changes rejected and discarded (script and locator map)",
                "action": "reject",
                "details": {
                    "row_id": row_id,
                    "modulename": modulename,
                    "featuretags": featuretags,
                    "scenariotags": scenariotags,
                    "scenarioname": scenarioname
                },
                "timestamp": current_dt.isoformat() + "Z"
            }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"pushAutohealingData: Error processing request: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )


@router.get("/autoheal")
async def autoheal_trigger(
    request: Request,
    modulename: ModuleName = Query(..., description="Module name (select from dropdown)"),
    featuretags: str = Query(..., description="Feature-level tags (comma or space separated)"),
    scenariotags: str = Query(..., description="Scenario-level tags (comma or space separated)"),
    scenarioname: str = Query(..., description="Scenario name to auto-heal"),
):
    modulename = modulename.value if isinstance(modulename, ModuleName) else str(modulename)
    if not (modulename and modulename.strip()):
        raise HTTPException(status_code=400, detail="Invalid modulename")
    if not (featuretags and featuretags.strip()):
        raise HTTPException(status_code=400, detail="Invalid featuretags")
    if not (scenariotags and scenariotags.strip()):
        raise HTTPException(status_code=400, detail="Invalid scenariotags")
    if not (scenarioname and scenarioname.strip()):
        raise HTTPException(status_code=400, detail="Invalid scenarioname")

    now_utc = datetime.now(timezone.utc)
    submitted_at_display = now_utc.astimezone(ZoneInfo("America/New_York")).strftime("%B %d, %Y, %H:%M:%S %Z")
    submitted_at_utc_display = now_utc.strftime("%B %d, %Y, %H:%M:%S UTC")

    def _normalize_tags(raw: str) -> str:
        parts = []
        for tok in (raw or "").replace(",", " ").split():
            t = tok.strip()
            if t:
                parts.append(t if t.startswith("@") else f"@{t}")
        return " ".join(parts)

    feature_tags_line = _normalize_tags(featuretags)
    scenario_tags_line = _normalize_tags(scenariotags)

    row = None
    try:
        with engine.begin() as conn:
            sql = text(f"""
                SELECT "runid", "feature_steps", "api_steps", "complt_scripts", "feature_nm"
                FROM {PGSCHEMA}."t_autoHealScn"
                WHERE "modulenm" = :m
                  AND REPLACE("feature_tags", '@', '') = REPLACE(:ft, '@', '')
                  AND REPLACE("scn_tags", '@', '') = REPLACE(:st, '@', '')
                  AND "scenarioname" = :sn
                ORDER BY "id" DESC NULLS LAST
                LIMIT 1
            """)
            res = conn.execute(sql, {
                "m": modulename,
                "ft": featuretags,
                "st": scenariotags,
                "sn": scenarioname
            })
            row = res.fetchone()
    except Exception:
        return JSONResponse(status_code=202, content={
            "Status": "Failed",
            "message": "Failed initiating autoheal process: database lookup error",
            "submittedAt": submitted_at_display,
            "submittedAtUtc": submitted_at_utc_display
        })

    if not row:
        return JSONResponse(status_code=202, content={
            "Status": "Failed",
            "message": "Failed initiating autoheal process: no matching row found for provided parameters",
            "submittedAt": submitted_at_display,
            "submittedAtUtc": submitted_at_utc_display
        })

    existing_runid = (row[0] or "").strip()
    feature_steps = row[1]
    api_steps_raw = row[2]
    complt_scripts_raw = row[3]
    feature_nm = (row[4] or "").strip()

    if not existing_runid or not feature_steps:
        return JSONResponse(status_code=202, content={
            "Status": "Failed",
            "message": "Failed initiating autoheal process: missing runid or feature_steps in selected row",
            "submittedAt": submitted_at_display,
            "submittedAtUtc": submitted_at_utc_display
        })

    lines = []
    if feature_tags_line:
        lines.append(feature_tags_line)
    lines.append(f"Feature: {feature_nm}" if feature_nm else f"Feature: {scenarioname}")
    lines.append("")
    if scenario_tags_line:
        lines.append(scenario_tags_line)
    lines.append(f"Scenario: {scenarioname}")

    steps_appended = False
    try:
        import json as _json
        parsed = feature_steps
        if isinstance(parsed, str):
            parsed = _json.loads(parsed)
        if isinstance(parsed, list) and parsed:
            chosen = None
            for item in parsed:
                try:
                    nm = (item or {}).get("scenario")
                    if isinstance(nm, str) and nm.strip().lower() == (scenarioname or "").strip().lower():
                        chosen = item
                        break
                except Exception:
                    continue
            if not chosen:
                chosen = parsed[0]
            steps_list = (chosen or {}).get("steps") or []
            for s in steps_list:
                s_line = str(s or "").rstrip("\r\n")
                if s_line:
                    lines.append(s_line)
                    steps_appended = True
    except Exception:
        steps_appended = False

    if not steps_appended:
        for step_line in str(feature_steps).splitlines():
            sl = step_line.rstrip("\r\n")
            if sl:
                lines.append(sl)

    feature_content = "\n".join(lines) + "\n"

    files_payload = [
        ("feature_file", ("autoheal.feature", feature_content.encode("utf-8"), "text/plain"))
    ]

    api_json_bytes = None
    if api_steps_raw is not None and str(api_steps_raw).strip():
        try:
            import json as _json
            if isinstance(api_steps_raw, (dict, list)):
                api_json_bytes = _json.dumps(api_steps_raw, ensure_ascii=False).encode("utf-8")
            else:
                s = str(api_steps_raw).strip()
                try:
                    parsed = _json.loads(s)
                except Exception:
                    parsed = {"api_config": [s]}
                api_json_bytes = _json.dumps(parsed, ensure_ascii=False).encode("utf-8")
        except Exception:
            api_json_bytes = None

    if api_json_bytes:
        files_payload.append(("json_files", ("api_config.json", api_json_bytes, "application/json")))

    def _is_bdd_script(ts_text: str) -> bool:
        try:
            s = (ts_text or "").lower()
            if not s.strip():
                return True
            bdd_indicators = [
                "@cucumber/cucumber",
                "playwright-bdd",
                "cucumber-tsflow",
                "definestep",
                "setdefaulttimeout",
                "before(",
                "after(",
                "given(",
                "when(",
                "then(",
            ]
            if any(tok in s for tok in bdd_indicators):
                return True
            non_bdd_indicators = [
                "@playwright/test",
                "test.describe(",
                "test(",
            ]
            if any(tok in s for tok in non_bdd_indicators):
                return False
            return True
        except Exception:
            return True

    gen_bdd = _is_bdd_script(complt_scripts_raw if isinstance(complt_scripts_raw, str) else str(complt_scripts_raw or ""))

    # Ensure autoheal suffix is added exactly once (don't double-append if already present)
    autoheal_runid = existing_runid
    if not autoheal_runid.lower().endswith("autoheal"):
        autoheal_runid = f"{existing_runid}autoheal"

    data_fields = {
        "push_to_github": "false",
        "generate_step_definitions": "true" if gen_bdd else "false",
        "dry_run_strategy": "MCP",
        "run_id": autoheal_runid,
        "domain_selection": modulename,
    }

    async def _trigger_sync_upload():
        url = _abs_url(request, "/Web-Automation/upload-feature-file")
        try:
            timeout = httpx.Timeout(3600.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, data=data_fields, files=files_payload)
                if resp.status_code >= 400:
                    logger.error(f"Autoheal trigger failed: {resp.status_code} {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Autoheal trigger error: {e}")

    try:
        asyncio.create_task(_trigger_sync_upload())
    except Exception:
        return JSONResponse(status_code=202, content={
            "Status": "Failed",
            "message": "Failed initiating autoheal process: internal scheduling error",
            "submittedAt": submitted_at_display,
            "submittedAtUtc": submitted_at_utc_display
        })

    try:
        with engine.begin() as conn:
            upd = text(f"""
                UPDATE {PGSCHEMA}."t_autoHealScn"
                SET "stat_msg" = :msg
                WHERE "modulenm" = :m
                  AND REPLACE("feature_tags", '@', '') = REPLACE(:ft, '@', '')
                  AND REPLACE("scn_tags", '@', '') = REPLACE(:st, '@', '')
                  AND "scenarioname" = :sn
            """)
            conn.execute(upd, {
                "msg": "Auto -healing is in-progress",
                "m": modulename,
                "ft": featuretags,
                "st": scenariotags,
                "sn": scenarioname
            })
    except Exception as e:
        logger.error(f"Failed to update auto-heal status message: {e}")

    async def _send_autoheal_initiation_notification():
        try:
            repo_modules = {}
            try:
                repo_modules = load_repo_modules() or {}
            except Exception:
                repo_modules = {}
            selected = repo_modules.get(modulename) or {}

            def _normalize_list(v):
                if isinstance(v, list):
                    return [x.strip() for x in v if isinstance(x, str) and x.strip()]
                if isinstance(v, str) and v.strip():
                    return [s.strip() for s in v.split(",") if s.strip()]
                return []

            slack_list = _normalize_list(selected.get("slack_channel_emails") or [])
            email_list = _normalize_list(selected.get("email_recipients") or [])
            recipients = list(dict.fromkeys(slack_list + email_list))
            if not recipients:
                logger.info("Autoheal notify: no recipients resolved; skipping notification")
                return

            initiation_payload = {
                "status": "initiated",
                "message": "Auto-heal request accepted and processing has been initiated.",
                "run_id": autoheal_runid,
                "selected_domain": modulename,
                "push_to_github": False,
                "submitted_at_eastern": submitted_at_display,
                "submitted_at_utc": submitted_at_utc_display,
            }
            try:
                await asyncio.to_thread(send_run_summary_email, initiation_payload, recipients, 5)
                logger.info(f"Autoheal notify: initiation summary sent to {len(recipients)} recipients")
            except Exception as notify_exc:
                logger.error(f"Autoheal notify failed: {type(notify_exc).__name__}: {notify_exc}")
        except Exception:
            pass

    try:
        asyncio.create_task(_send_autoheal_initiation_notification())
    except Exception as e:
        logger.error(f"Failed to schedule autoheal initiation notification: {e}")

    return JSONResponse(
        status_code=202,
        content={
            "Status": "Pass",
            "message": "Autoheal Process Started",
            "runid": autoheal_runid,
            "submittedAt": submitted_at_display,
            "submittedAtUtc": submitted_at_utc_display
        }
    )



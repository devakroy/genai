import os
import json
import logging
import re
from typing import List, Optional, Dict
from enum import Enum

from fastapi import APIRouter, Form, Request, UploadFile, File, Query, HTTPException

logger = logging.getLogger(__name__)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from utils.db_utils import engine
import hashlib
from pydantic import BaseModel

# ----------------------- Shared helpers (importable) -----------------------
def _norm_keyline(s: str) -> str:
    if not isinstance(s, str):
        return ""
    t = re.sub(r"\s+", " ", s).strip().lower()
    return t

def get_step_macros_for_domain(module: str) -> Dict[str, Dict]:
    """
    Load all step macros for a given module (domain) from DB.
    Returns dict: normalized_keyLine -> { 'keyStep', 'cmnSteps', 'defaultParam', 'hasAPIStep', 'apiRequests' }
    """
    if not module:
        return {}
    with engine.begin() as conn:
        rows = conn.execute(
            text('SELECT "keyStep", "cmnSteps", "defaultParam", "hasAPIStep", "apiRequests" FROM public.cmn_stps WHERE module = :m'),
            {"m": module},
        ).mappings().all()
    out: Dict[str, Dict] = {}
    for r in rows:
        k = _norm_keyline(r.get("keyStep", ""))
        out[k] = {
            "keyStep": r.get("keyStep"),
            "cmnSteps": r.get("cmnSteps") or [],
            "defaultParam": r.get("defaultParam") or {},
            "hasAPIStep": bool(r.get("hasAPIStep")),
            "apiRequests": r.get("apiRequests") or [],
        }
    return out

_PLACEHOLDER_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_\-\.]+)\}")
_STEP_KEYWORD_RE = re.compile(r"^\s*(Given|When|Then|And|But)\s+", re.IGNORECASE)

def _materialize_line_with_defaults(line: str, defaults: Dict[str, object]) -> str:
    if not isinstance(line, str) or not defaults:
        return line
    def repl(m):
        key = m.group(1)
        if key in defaults:
            val = defaults[key]
            # Only materialize if a concrete default value is provided
            if val is not None and val != "":
                if isinstance(val, (dict, list)):
                    rep = json.dumps(val, ensure_ascii=False)
                else:
                    rep = str(val)
                # Quote the replacement to preserve common BDD step patterns
                return f"\"{rep}\""
            # If default is None/empty, leave the placeholder as-is
        return m.group(0)
    return _PLACEHOLDER_TOKEN_RE.sub(repl, line)

def expand_feature_text_with_macros(
    feature_text: str,
    module: str,
    *,
    materialize_defaults_for_quoted: bool = False,
    materialize_from_examples: bool = False,
) -> Dict[str, object]:
    """
    Replace key lines with stored cmnSteps for the given module.
    - Exact, case-insensitive match on the step content (without the leading keyword).
    - Paste cmnSteps verbatim. No keyword injection.
    - If materialize_defaults_for_quoted: replace {var} with defaults when available (quoted placeholders).
    Returns: { 'content': str, 'expanded': [ { 'key': k, 'at_line': i, 'lines_added': n } ], 'warnings': [] }
    """
    macros = get_step_macros_for_domain(module)
    if not macros or not feature_text:
        return {"content": feature_text, "expanded": [], "warnings": []}

    lines = feature_text.splitlines(keepends=False)
    out_lines: List[str] = []
    expanded_events: List[Dict[str, object]] = []
    warnings: List[str] = []

    # Determine if directive style is present anywhere in the file.
    # One-line directive format:  # step-macro: Then user sign-in using common steps
    DIRECTIVE_RE = re.compile(r"^\s*#\s*step-macro\s*:\s*(.+)$", re.IGNORECASE)
    directive_mode = any(DIRECTIVE_RE.match(ln or "") for ln in lines)

    i = 0
    while i < len(lines):
        line = lines[i]
        # Skip within macro markers (prevent re-expansion)
        if line.strip().startswith("# BEGIN_MACRO:"):
            out_lines.append(line)
            i += 1
            while i < len(lines):
                out_lines.append(lines[i])
                if lines[i].strip().startswith("# END_MACRO:"):
                    i += 1
                    break
                i += 1
            continue

        # If in directive mode, only expand directives
        if directive_mode:
            dm = DIRECTIVE_RE.match(line or "")
            if not dm:
                out_lines.append(line)
                i += 1
                continue
            keyline_text = dm.group(1).strip()
            key_norm = _norm_keyline(keyline_text)
            macro = macros.get(key_norm)
            if not macro:
                # No macro found, keep comment as-is and warn
                out_lines.append(line)
                warnings.append(f"No macro found for directive: {keyline_text}")
                i += 1
                continue

            # Preserve the indentation of the comment line for inserted block
            leading = line[: line.find("#")] if "#" in line else ""
            inserted_count = 0
            for raw in (macro["cmnSteps"] or []):
                step_line = str(raw)
                if materialize_defaults_for_quoted:
                    step_line = _materialize_line_with_defaults(step_line, macro.get("defaultParam") or {})
                out_lines.append(f"{leading}{step_line}")
                inserted_count += 1
            expanded_events.append({"key": macro["keyStep"], "at_line": i + 1, "lines_added": inserted_count})
            i += 1
            continue

        # Non-directive mode: exact step-line match path
        m = _STEP_KEYWORD_RE.match(line or "")
        if not m:
            out_lines.append(line)
            i += 1
            continue

        leading = line[:m.start()]  # indentation
        keyword = m.group(1)        # Given/When/Then/And/But
        rest = line[m.end():]       # content after keyword
        key_norm = _norm_keyline(rest)

        macro = macros.get(key_norm)
        if not macro:
            out_lines.append(line)
            i += 1
            continue

        # Build inserted block (no markers)
        inserted_count = 0
        for raw in (macro["cmnSteps"] or []):
            step_line = str(raw)
            if materialize_defaults_for_quoted:
                step_line = _materialize_line_with_defaults(step_line, macro.get("defaultParam") or {})
            # Preserve stored line verbatim; retain original indentation
            out_lines.append(f"{leading}{step_line}")
            inserted_count += 1

        expanded_events.append({"key": macro["keyStep"], "at_line": i + 1, "lines_added": inserted_count})
        i += 1

    # Optional: materialize placeholders using Examples (highest priority) then macro defaults (fallback)
    def _parse_examples_map(all_lines: List[str]) -> Dict[str, str]:
        """
        Very simple parser: finds 'Examples:' and reads the next two '|' rows as header and first data row.
        """
        ex_map: Dict[str, str] = {}
        idx = 0
        while idx < len(all_lines):
            if all_lines[idx].strip().lower().startswith("examples:"):
                # find header row
                h = None
                d = None
                j = idx + 1
                while j < len(all_lines) and (not all_lines[j].strip()):
                    j += 1
                if j < len(all_lines) and "|" in all_lines[j]:
                    h = all_lines[j]
                    j += 1
                while j < len(all_lines) and (not all_lines[j].strip()):
                    j += 1
                if j < len(all_lines) and "|" in all_lines[j]:
                    d = all_lines[j]
                if h and d:
                    header_cols = [c.strip() for c in h.strip().strip("|").split("|")]
                    data_cols = [c.strip() for c in d.strip().strip("|").split("|")]
                    for k, v in zip(header_cols, data_cols):
                        if k:
                            ex_map[k] = v
                    break
            idx += 1
        return ex_map

    def _replace_placeholders(line: str, vars_map: Dict[str, str], defaults_map: Dict[str, object]) -> str:
        if not isinstance(line, str):
            return line
        def repl(m):
            key = m.group(1)
            # Skip special runtime placeholders
            if key.startswith("awssecretmanager.") or "." in key:
                return m.group(0)
            if key in vars_map and vars_map[key] not in (None, ""):
                val = vars_map[key]
                return f"\"{val}\""
            if key in defaults_map:
                val = defaults_map[key]
                if val not in (None, ""):
                    if isinstance(val, (dict, list)):
                        rep = json.dumps(val, ensure_ascii=False)
                    else:
                        rep = str(val)
                    return f"\"{rep}\""
            return m.group(0)
        return _PLACEHOLDER_TOKEN_RE.sub(repl, line)

    content_text = "\n".join(out_lines) + ("\n" if feature_text.endswith("\n") else "")

    if materialize_from_examples:
        ex_map = _parse_examples_map(out_lines)  # may be empty for plain Scenarios
        # Build defaults from macros used
        defaults_agg: Dict[str, object] = {}
        if expanded_events:
            macros_map = get_step_macros_for_domain(module)
            for ev in expanded_events:
                mk = _norm_keyline(ev.get("key", ""))
                mobj = macros_map.get(mk) or {}
                for dk, dv in (mobj.get("defaultParam") or {}).items():
                    defaults_agg.setdefault(dk, dv)
        # Apply replacement to all lines (Examples first, then defaults fallback)
        new_lines: List[str] = []
        for ln in out_lines:
            new_lines.append(_replace_placeholders(ln, ex_map, defaults_agg))
        # Strip any Examples: block from the final output (simple removal)
        stripped_lines: List[str] = []
        skipping = False
        for ln in new_lines:
            s = (ln or "").strip()
            if not skipping and s.lower().startswith("examples:"):
                skipping = True
                continue
            if skipping:
                # Skip table rows and immediate blank/comment lines part of the examples block
                if s.startswith("|") or s == "" or s.startswith("#"):
                    continue
                # First non-table, non-blank/comment line → stop skipping
                skipping = False
            stripped_lines.append(ln)
        content_text = "\n".join(stripped_lines) + ("\n" if feature_text.endswith("\n") else "")

    return {"content": content_text, "expanded": expanded_events, "warnings": warnings}

router = APIRouter(prefix="/common-steps", tags=["Step Macros"])


def _project_root_dir() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(base))
    return os.path.dirname(root)


def _repo_modules_path() -> str:
    # src/onboardingConfiguration.json under project root
    return os.path.join(_project_root_dir(), "src", "onboardingConfiguration.json")


def load_domains() -> List[str]:
    """Load domain names from onboarding JSON (onboardingConfiguration.json)."""
    path = _repo_modules_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        domains: List[str] = []
        for item in data:
            if isinstance(item, dict) and item.get("domain"):
                domains.append(str(item["domain"]))
        return sorted(set(domains))
    except Exception:
        return []

@router.get("/domains", response_model=List[str])
async def get_domains():
    """
    Return available domain names pulled from src/onboardingConfiguration.json.
    Used by TestGenie UI to populate the Domain dropdowns dynamically.
    """
    return load_domains()

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_\-\.]+)\}")


def _extract_placeholders(steps: List[str]) -> List[str]:
    found: List[str] = []
    for s in steps:
        for m in _PLACEHOLDER_RE.findall(s or ""):
            if m not in found:
                found.append(m)
    return found


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


def _normalize_var_refs(value: str) -> str:
    """Turn header values like {Var} into {{Var}} for templating."""
    return re.sub(r"\{([A-Za-z0-9_\-\.]+)\}", r"{{\\1}}", value or "")


def _build_api_requests_from_steps(steps: List[str], uploaded_names: List[str]) -> (List[Dict], List[str]):
    actions: List[Dict] = []
    errors: List[str] = []

    uploaded_set = {n.strip() for n in (uploaded_names or []) if n}

    # Patterns
    invoke_re = re.compile(r"(?i)(?:^|\b)(?:then|when|and)\s+user\s+invokes\s+([\w\- ]+?)\s+service\s+with\s*\{([^}]+)\}")
    extract_re = re.compile(r"(?i)stores\s+\"([^\"]+)\"\s+from\s+response\s+as\s+\"([^\"]+)\"")
    header_re = re.compile(r"(?i)add\s+header\s+\"([^\"]+)\".*")
    store_resp_re = re.compile(r"(?i)stores\s+response\s+as\s+\"([^\"]+)\"")

    for raw in steps:
        line = (raw or "").strip()
        if not line:
            continue

        m = invoke_re.search(line)
        if m:
            service_name = m.group(1).strip()
            cfg = m.group(2).strip()
            actions.append({"action": "invoke", "name": service_name, "configFile": cfg})
            if uploaded_set and cfg not in uploaded_set:
                errors.append(f"Missing uploaded file for '{cfg}'")
            continue

        m = extract_re.search(line)
        if m:
            actions.append({"action": "extract", "from": m.group(1).strip(), "as": m.group(2).strip()})
            continue

        m = header_re.search(line)
        if m:
            # Expect full header in quotes: "Key: Value {Var}"
            try:
                quoted = re.search(r'"([^"]+)"', line).group(1)
                parts = quoted.split(":", 1)
                key = parts[0].strip()
                val = _normalize_var_refs(parts[1].strip()) if len(parts) > 1 else ""
                actions.append({"action": "setHeader", "headers": {key: val}})
            except Exception:
                errors.append("Unrecognized header format; expected \"Key: Value\"")
            continue

        m = store_resp_re.search(line)
        if m:
            actions.append({"action": "storeResponse", "as": m.group(1).strip()})
            continue

    return actions, errors

def _create_domain_enum() -> Enum:
    domains = load_domains()
    if not domains:
        return Enum("DomainOptions", {"_No_Domains_Configured": ""})
    enum_values: Dict[str, str] = {}
    for d in domains:
        key = d.replace("-", "_").replace(" ", "_")
        if key and key[0].isdigit():
            key = f"_{key}"
        base_key, suffix = key, 1
        while key in enum_values:
            suffix += 1
            key = f"{base_key}_{suffix}"
        enum_values[key] = d
    return Enum("DomainOptions", enum_values)


DomainOptions = _create_domain_enum()


@router.get("/ui/preview", response_class=HTMLResponse, include_in_schema=False)
async def form_ui(request: Request):
    domains = load_domains()
    domain_options = "".join(
        f'<option value="{_html_escape(d)}">{_html_escape(d)}</option>' for d in domains
    ) or '<option value="">(no domains found)</option>'

    html = f"""
<!DOCTYPE html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>Step Macros - Add</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 24px; }}
      textarea {{ width: 100%; font-family: Consolas, monospace; }}
      input[type=text] {{ width: 100%; font-size: 16px; padding: 10px; }}
      .keyline {{ height: 64px; }}
      .steps {{ height: 300px; }}
      .row {{ margin-bottom: 12px; }}
      .hint {{ color: #666; font-size: 12px; }}
      .json {{ white-space: pre-wrap; font-family: Consolas, monospace; background: #fafafa; padding: 8px; height: 160px; }}
    </style>
  </head>
  <body>
    <h2>Add Step Macro (Preview Only)</h2>
    <form method=\"post\" action=\"/common-steps/ui/preview\" enctype=\"multipart/form-data\"> 
      <div class=\"row\">
        <label>Key line</label><br/>
        <textarea name=\"keyStep\" class=\"keyline\" placeholder=\"user navigate to checkout page\" required></textarea>
      </div>
      <div class=\"row\">
        <label>Common steps (one per line)</label><br/>
        <textarea name=\"cmnSteps\" class=\"steps\" placeholder=\"Then user clicks on \"Buy once\" Radio button\nThen user clicks on \"Add to Cart\"\nThen user clicks on \"Proceed to Checkout\"\" required></textarea>
      </div>
      <div class=\"row\">
        <label>Domain</label><br/>
        <select name=\"module\" required>{domain_options}</select>
      </div>
      <div class=\"row\">
        <label><input type=\"checkbox\" name=\"hasAPIStep\" value=\"1\"/> Has API Steps</label>
      </div>
      <div class=\"row\">
        <label>Default parameters (JSON, optional)</label>
        <textarea name=\"defaultParam\" class=\"json\" placeholder=\"{{\n  \"email\": \"ss@ss.com\"\n}}\"></textarea>
        <div class=\"hint\">Leave empty to auto-detect placeholders like {{var}} from steps.</div>
      </div>
      <div class=\"row\">
        <label>API requests (JSON array, optional)</label>
        <textarea name=\"apiRequests\" class=\"json\" placeholder=\"[\n  {{ \"action\": \"invoke\", \"configFile\": \"newuser_auth_config.json\" }}\n]\"></textarea>
      </div>
      <div class=\"row\">
        <label>Or upload API step JSON file(s)</label><br/>
        <input type=\"file\" name=\"apiFiles\" accept=\".json\" multiple />
        <div class=\"hint\">Each file may be a JSON array or {{\"steps\": [ ... ]}} object.</div>
      </div>
      <button type=\"submit\">Preview Row (no DB write)</button>
    </form>
  </body>
  </html>
"""
    return HTMLResponse(content=html)


@router.get("/ui/delete", response_class=HTMLResponse, include_in_schema=False)
async def ui_delete_get(
    request: Request,
    module: Optional[str] = Query(None),
    keyStep: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    domains = load_domains()
    selected = module or ""
    domain_options = "".join(
        f'<option value="{_html_escape(d)}" {"selected" if d == selected else ""}>{_html_escape(d)}</option>'
        for d in domains
    ) or '<option value="">(no domains found)</option>'

    results_html = ""
    if module:
        params = {"module": module}
        if keyStep and keyStep.strip():
            raw = keyStep.strip()
            tokens = [t for t in re.split(r"\s+", re.sub(r"[\"']", " ", raw)) if t][:40]
            conds = []
            for idx, tok in enumerate(tokens):
                p = f"kw{idx}"
                params[p] = f"%{tok.lower()}%"
                conds.append(f"lower(\"keyStep\") LIKE :{p}")
            where_extra = (" AND " + " AND ".join(conds)) if conds else ""
            sql = text(
                f"""
                SELECT id, module, "keyStep", "cmnSteps", "hasAPIStep", "defaultParam", "apiRequests"
                FROM public.cmn_stps
                WHERE module = :module{where_extra}
                ORDER BY id DESC
                LIMIT :limit
                """
            )
            params["limit"] = limit
        else:
            params["limit"] = limit
            sql = text(
                """
                SELECT id, module, "keyStep", "cmnSteps", "hasAPIStep", "defaultParam", "apiRequests"
                FROM public.cmn_stps
                WHERE module = :module
                ORDER BY id DESC
                LIMIT :limit
                """
            )

        with engine.begin() as conn:
            rows = list(conn.execute(sql, params).mappings().all())

        if rows:
            body_rows = []
            for r in rows:
                steps_list = "".join(f"<li>{_html_escape(str(s))}</li>" for s in (r.get("cmnSteps") or []))
                default_json = _html_escape(json.dumps(r.get("defaultParam"), indent=2)) if r.get("defaultParam") is not None else "—"
                api_json = _html_escape(json.dumps(r.get("apiRequests"), indent=2)) if r.get("apiRequests") is not None else "—"
                body_rows.append(
                    f"<tr>"
                    f"<td><input type=\"checkbox\" name=\"ids\" value=\"{r.get('id')}\"/></td>"
                    f"<td>{r.get('id')}</td>"
                    f"<td>{_html_escape(str(r.get('module')))}</td>"
                    f"<td>{_html_escape(str(r.get('keyStep')))}</td>"
                    f"<td><ul>{steps_list}</ul></td>"
                    f"<td>{'Yes' if r.get('hasAPIStep') else 'No'}</td>"
                    f"<td><pre class=\"json\">{default_json}</pre></td>"
                    f"<td><pre class=\"json\">{api_json}</pre></td>"
                    f"</tr>"
                )
            results_html = (
                "<form method=\"post\" action=\"/common-steps/ui/delete\" onsubmit=\"return confirm('Delete selected step macros?');\">"
                "<table><thead><tr>"
                "<th></th><th>ID</th><th>Domain</th><th>Key line</th><th>Common steps</th>"
                "<th>Has API</th><th>Default params</th><th>API requests</th>"
                "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"
                "<div class=\"row\" style=\"margin-top:12px;\"><button type=\"submit\">Delete selected</button></div>"
                "</form>"
            )
        else:
            if keyStep and keyStep.strip():
                results_html = "<div class=\"empty\">Sorry! No data found for provided key line.</div>"
            else:
                results_html = "<div class=\"empty\">No results found for the selected domain.</div>"

    html = f"""
<!DOCTYPE html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>Delete Step Macros</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 24px; }}
      textarea {{ width: 100%; font-family: Consolas, monospace; }}
      .row {{ margin-bottom: 12px; }}
      table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
      th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; vertical-align: top; }}
      th {{ background: #f7f7f7; }}
      .json {{ white-space: pre-wrap; font-family: Consolas, monospace; background: #fafafa; padding: 8px; }}
      .empty {{ color: #666; margin-top: 12px; }}
    </style>
  </head>
  <body>
    <h2>Delete Step Macros</h2>
    <form method=\"get\" action=\"/common-steps/ui/delete\"> 
      <div class=\"row\">
        <label>Domain</label><br/>
        <select name=\"module\" required>{domain_options}</select>
      </div>
      <div class=\"row\">
        <label>Key line contains (optional)</label><br/>
        <input type=\"text\" name=\"keyStep\" value=\"{_html_escape(keyStep or '')}\" style=\"width:100%; padding:8px;\" />
      </div>
      <div class=\"row\">
        <label>Limit</label><br/>
        <input type=\"number\" name=\"limit\" value=\"{limit}\" min=\"1\" max=\"200\" />
      </div>
      <button type=\"submit\">Search</button>
      <a href=\"/common-steps/ui/query\" style=\"margin-left:12px;\">Back to View</a>
    </form>
    {results_html}
  </body>
  </html>
"""
    return HTMLResponse(content=html)

@router.get(
    "/delete",
    summary="Delete step macros",
    description="Delete saved step macros by Domain and optional Key line. For a selectable table view with checkboxes, use [UI delete](/common-steps/ui/delete).",
    response_class=HTMLResponse,
)
async def delete_link():
    # Simple helper to mirror the Query API card with a link out to the UI
    html = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Delete Step Macros</title>
    <style>
      body { font-family: Arial, sans-serif; margin: 24px; }
      a.button { display:inline-block; padding:10px 14px; background:#2563eb; color:#fff; text-decoration:none; border-radius:6px; }
      p { color:#444; }
    </style>
  </head>
  <body>
    <h2>Delete Step Macros</h2>
    <p>Use the dedicated UI to search and remove Step Macros:</p>
    <p><a class="button" href="/common-steps/ui/delete">Open Delete UI</a></p>
  </body>
</html>
"""
    return HTMLResponse(content=html)


@router.post("/ui/delete", response_class=HTMLResponse, include_in_schema=False)
async def ui_delete_post(
    request: Request,
    ids: List[str] = Form([]),
    module: Optional[str] = Form(None),
    keyStep: Optional[str] = Form(None),
    limit: int = Form(50),
):
    # Convert to ints safely
    id_ints: List[int] = []
    for v in ids or []:
        try:
            id_ints.append(int(v))
        except Exception:
            continue

    deleted = 0
    if id_ints:
        with engine.begin() as conn:
            for one in id_ints:
                conn.execute(text('DELETE FROM public.cmn_stps WHERE id = :id'), {"id": one})
                deleted += 1

    # Redirect back to GET with a success banner
    params = []
    if module:
        params.append(("module", module))
    if keyStep:
        params.append(("keyStep", keyStep))
    params.append(("limit", str(limit)))
    success = f"Deleted {deleted} record(s)." if id_ints else "No records selected."
    # Render a small confirmation page with back link
    html = f"""
<!DOCTYPE html>
<html>
  <head><meta charset=\"utf-8\" /><title>Delete Step Macros</title></head>
  <body>
    <div>✅ { _html_escape(success) }</div>
    <div style=\"margin-top:12px;\">
      <a href=\"/common-steps/ui/delete?{'&'.join(f'{k}={_html_escape(v)}' for k,v in params)}\">Back</a>
    </div>
  </body>
</html>
"""
    return HTMLResponse(content=html)


class DeleteStepMacrosRequest(BaseModel):
    ids: List[int]


class DeleteStepMacrosResponse(BaseModel):
    deleted: int
    requested: int


@router.delete(
    "",
    summary="Delete Step Macros",
    response_description="Delete by IDs",
    response_model=DeleteStepMacrosResponse,
    include_in_schema=False,
)
async def delete_step_macros(payload: DeleteStepMacrosRequest):
    """
    Delete one or more step macro rows by id.
    Body JSON: { "ids": [1,2,3] }
    """
    ids = payload.ids if payload and isinstance(payload.ids, list) else []
    id_ints: List[int] = [int(v) for v in ids if v is not None]
    if not id_ints:
        raise HTTPException(status_code=400, detail="No valid ids provided")

    deleted = 0
    with engine.begin() as conn:
        for one in id_ints:
            conn.execute(text('DELETE FROM public.cmn_stps WHERE id = :id'), {"id": one})
            deleted += 1
    return DeleteStepMacrosResponse(deleted=deleted, requested=len(ids))

@router.post("/ui/preview", response_class=HTMLResponse, include_in_schema=False)
async def preview_post(
    keyStep: str = Form(...),
    cmnSteps: str = Form(...),
    module: DomainOptions = Form(...),
    hasAPIStep: bool = Form(False),
    defaultParam: Optional[str] = Form(None),
    apiRequests: Optional[str] = Form(None),
    apiFiles: Optional[List[UploadFile]] = File(None),
):
    key = " ".join((keyStep or "").strip().split())
    steps = [s.strip() for s in (cmnSteps or "").splitlines() if s.strip()]
    has_api = hasAPIStep

    def _parse_json(text: Optional[str]):
        if not text or not text.strip():
            return None
        try:
            return json.loads(text)
        except Exception:
            return {"_error": "Invalid JSON"}

    default_param_obj = _parse_json(defaultParam)
    api_requests_obj = _parse_json(apiRequests)

    # If has API and no apiRequests text provided, build from steps and uploaded file names
    file_errors: List[str] = []
    if has_api and (api_requests_obj is None or isinstance(api_requests_obj, dict) and api_requests_obj.get("_error")):
        uploaded_names = []
        if apiFiles:
            for f in apiFiles:
                uploaded_names.append(f.filename)
                # read/validate JSON syntax if desired
                try:
                    raw = (await f.read()).decode("utf-8", errors="replace")
                    json.loads(raw)  # just validate
                except Exception as e:
                    file_errors.append(f"{f.filename}: JSON parse error: {str(e)[:120]}")

        inferred_actions, infer_errors = _build_api_requests_from_steps(steps, uploaded_names)
        api_requests_obj = inferred_actions if inferred_actions else None
        file_errors.extend(infer_errors)

    # Build defaults only from explicit {placeholders}; do not convert quoted literals
    if not isinstance(default_param_obj, dict):
        placeholders = _extract_placeholders(steps)
        default_param_obj = {p: None for p in placeholders} if placeholders else None

    # ---- Soft duplicate detection by steps signature (within same module) ----
    def _normalize_step_for_sig(s: str) -> str:
        if s is None:
            return ""
        # Normalize quotes and whitespace, lowercase
        t = s.replace("\u201C", '"').replace("\u201D", '"').replace("\u2018", "'").replace("\u2019", "'")
        t = re.sub(r"\s+", " ", t).strip().lower()
        return t

    def _steps_signature(steps_list: List[str]) -> str:
        norm = [_normalize_step_for_sig(x) for x in steps_list or []]
        payload = json.dumps(norm, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    module_value = getattr(module, "value", module)
    preview_sig = _steps_signature(steps)
    duplicate_rows: List[Dict] = []
    try:
        with engine.begin() as conn:
            mod_rows = conn.execute(
                text('SELECT id, "keyStep", "cmnSteps" FROM public.cmn_stps WHERE module = :module'),
                {"module": module_value},
            ).mappings().all()
            for r in mod_rows:
                if _steps_signature(r.get("cmnSteps")) == preview_sig:
                    duplicate_rows.append({"id": r["id"], "keyStep": r["keyStep"]})
    except Exception:
        # Non-fatal for preview
        pass

    preview_row = {
        "keyStep": key,
        "cmnSteps": steps,
        "module": getattr(module, "value", module),
        "defaultParam": default_param_obj,
        "hasAPIStep": has_api,
        "apiRequests": api_requests_obj if has_api else None,
    }

    # Check if a record with same keyStep+module already exists
    exists_id = None
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT id FROM public.cmn_stps WHERE lower(\"keyStep\") = lower(:key) AND module = :module"),
                {"key": key, "module": getattr(module, "value", module)},
            ).fetchone()
            if row:
                exists_id = row[0]
    except Exception:
        pass

    def _to_html_cell(value) -> str:
        if isinstance(value, list):
            items = "".join(f"<li>{_html_escape(str(v))}</li>" for v in value)
            return f"<ul>{items}</ul>"
        if isinstance(value, dict):
            pretty = _html_escape(json.dumps(value, indent=2))
            return f"<pre class=\"json\">{pretty}</pre>"
        return _html_escape("" if value is None else str(value))

    # Append file errors to preview if any
    rows = "".join(
        f"<tr><th>{k}</th><td>{_to_html_cell(v)}</td></tr>"
        for k, v in preview_row.items()
    )

    if file_errors:
        err_items = "".join(f"<li>{_html_escape(e)}</li>" for e in file_errors)
        rows += f"<tr><th>apiFileErrors</th><td><ul>{err_items}</ul></td></tr>"

    # Compute conditional UI pieces for duplicate steps and overwrite hint
    # Remove the exact same keyline row from the duplicate-steps list (to reduce confusion)
    duplicate_filtered = [d for d in duplicate_rows if d.get("id") != exists_id]

    dup_notice = ""
    if duplicate_filtered:
        items = "".join(
            f"<li>ID {d['id']}: { _html_escape(str(d['keyStep'])) }</li>" for d in duplicate_filtered
        )
        dup_notice = (
            f"<div style=\"color:#a60;margin:8px 0;\">Identical steps already exist in this domain under other key lines:</div><ul>{items}</ul>"
        )

    # Build a single, consolidated guidance block
    guidance_lines = []
    if exists_id and duplicate_filtered:
        guidance_lines.append(
            f"<div style=\"color:#a60;\">This key line already exists (ID: {exists_id}) and the steps match entries above.</div>"
        )
        guidance_lines.append(
            "<div>• Click <b>Overwrite existing</b> to update the current row.</div>"
        )
        guidance_lines.append(
            "<div>• Or change the <b>Key line</b> to add a new alias for the same steps.</div>"
        )
    elif exists_id:
        guidance_lines.append(
            f"<div style=\"color:#a60;\">This key line already exists (ID: {exists_id}). Submitting will overwrite it.</div>"
        )
        guidance_lines.append(
            "<div>• To create a new entry, change the <b>Key line</b>.</div>"
        )
    elif duplicate_filtered:
        guidance_lines.append(
            "<div style=\"color:#a60;\">These steps already exist under the listed key lines. Saving will add a new key line for the same steps.</div>"
        )

    exist_msg_html = dup_notice + ("".join(guidance_lines) if guidance_lines else "")
    overwrite_hidden_html = '<input type="hidden" name="overwrite" value="1"/>' if exists_id else ""
    button_label = "Overwrite existing" if exists_id else "Save to database"

    html = f"""
<!DOCTYPE html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>Preview: Step Macro</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 24px; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; vertical-align: top; }}
      th {{ background: #f7f7f7; width: 220px; }}
      .json {{ white-space: pre-wrap; font-family: Consolas, monospace; background: #fafafa; padding: 8px; }}
      .actions {{ margin-top: 16px; display: flex; gap: 12px; align-items: center; }}
    </style>
  </head>
  <body>
    <h2>Preview: Step Macro (not saved)</h2>
    {dup_notice}
    {exist_msg_html}
    <table>
      <tbody>
        {rows}
      </tbody>
    </table>
    <div class=\"actions\"> 
      <a href=\"/common-steps/ui/preview\">&#8592; Back to form</a>
      <form method=\"post\" action=\"/common-steps/ui/save\"> 
        <textarea name=\"record_json\" style=\"display:none\">{_html_escape(json.dumps(preview_row))}</textarea>
        {overwrite_hidden_html}
        <button type=\"submit\">{button_label}</button>
      </form>
    </div>
  </body>
  </html>
"""
    return HTMLResponse(content=html)


@router.post(
    "",
    summary="Step Macros UI",
    description=(
        "Use this form to create or update common step macros that map a key line to a sequence of steps.\n\n"
        "What you can enter:\n"
        "- Basic steps (UI actions)\n"
        "- Steps with placeholders like {email}, {city} (defaults optional)\n"
        "- Steps that orchestrate API calls (toggle Has API Steps) and optionally upload related JSON config files\n\n"
        "How to use:\n"
        "1) Click [Take me there](/common-steps/ui/preview).\n"
        "2) Fill Key line, Common steps (one per line), select Domain.\n"
        "3) Optionally provide Default parameters (JSON) and/or upload API JSON files.\n"
        "4) Submit to preview. If a matching key line exists, you'll be offered Overwrite; otherwise Save."
    ),
)
async def get_ui_link(request: Request):
    base = str(request.base_url).rstrip("/")
    ui = f"{base}/common-steps/ui/preview"
    return {
        "ui_url": ui,
        "instructions": [
            "Open the form using the link provided.",
            "Enter Key line and Common steps (one per line).",
            "Select a Domain from the dropdown.",
            "Optionally paste defaultParam JSON and/or upload API JSON files.",
            "Preview first; then Save or Overwrite based on duplicate detection.",
        ],
        "supported_types": [
            "basic_steps",
            "steps_with_placeholders",
            "api_steps",
        ],
    }


@router.post("/ui/save", response_class=HTMLResponse, include_in_schema=False)
async def save_post(record_json: str = Form(...)):
    try:
        rec = json.loads(record_json)
        key = (rec.get("keyStep") or "").strip()
        steps = rec.get("cmnSteps") or []
        module = rec.get("module") or ""
        default_param = rec.get("defaultParam")
        has_api = bool(rec.get("hasAPIStep", False))
        api_requests = rec.get("apiRequests")

        if not key or not steps or not module:
            raise ValueError("keyStep, cmnSteps, and module are required")

        # Prepare parameters, casting JSON via SQL to ensure correct types
        api_text_array = None
        if isinstance(api_requests, list):
            try:
                api_text_array = [json.dumps(x) for x in api_requests]
            except Exception:
                api_text_array = None

        def_json_text = None
        if default_param is not None:
            def_json_text = json.dumps(default_param)

        params = {
            "key": key,
            "cmn": steps,
            "module": module,
            "def_json_text": def_json_text,            # cast to ::jsonb
            "has": has_api,
            "api_json_text_array": api_text_array,      # cast to ::text[]::jsonb[]
        }

        status = "created"
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT id FROM public.cmn_stps WHERE lower(\"keyStep\") = lower(:key) AND module = :module"),
                {"key": key, "module": module},
            ).fetchone()

            if row:
                conn.execute(
                    text(
                        "UPDATE public.cmn_stps SET \"cmnSteps\" = :cmn, \"defaultParam\" = CAST(:def_json_text AS jsonb), \"hasAPIStep\" = :has, \"apiRequests\" = CAST(:api_json_text_array AS text[])::jsonb[] WHERE id = :id"
                    ),
                    {**params, "id": row[0]},
                )
                status = "updated"
            else:
                conn.execute(
                    text(
                        "INSERT INTO public.cmn_stps (\"keyStep\", \"cmnSteps\", module, \"defaultParam\", \"hasAPIStep\", \"apiRequests\") VALUES (:key, :cmn, :module, CAST(:def_json_text AS jsonb), :has, CAST(:api_json_text_array AS text[])::jsonb[])"
                    ),
                    params,
                )

        html = f"""
<!DOCTYPE html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>Saved cmn_stps</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 24px; }}
      .ok {{ color: #0a0; margin-bottom: 12px; }}
    </style>
  </head>
  <body>
    <div class=\"ok\">Row {status} successfully.</div>
    <a href=\"/common-steps/ui/preview\">Add another</a>
  </body>
  </html>
"""
        return HTMLResponse(content=html)

    except Exception as e:
        err = _html_escape(str(e))
        html = f"""
<!DOCTYPE html>
<html>
  <head><meta charset=\"utf-8\" /><title>Error</title></head>
  <body>
    <div style=\"color:#a00\">Save failed: {err}</div>
    <a href=\"/common-steps/ui/preview\">Back</a>
  </body>
  </html>
"""
        return HTMLResponse(content=html, status_code=400)


@router.get(
    "/query",
    summary="Query step macros",
    description=(
        "Query saved step macros by Domain and optional Key line. "
        "For a readable table view, use [UI view](/common-steps/ui/query)."
    ),
)
async def query_common_steps(
    module: DomainOptions = Query(..., description="Domain/module"),
    keyStep: Optional[str] = Query(None, description="Optional key line (case-insensitive partial match; all keywords)"),
    limit: int = Query(50, ge=1, le=200, description="Max rows when listing by domain"),
):
    module_val = getattr(module, "value", module)
    logger.info("common-steps/query request: module=%s keyStep=%s limit=%s", module_val, keyStep, limit)
    params = {"module": module_val}
    sql = None

    if keyStep and keyStep.strip():
        # Build partial-match conditions: all tokens must appear (AND)
        raw = keyStep.strip()
        tokens = [t for t in re.split(r"\s+", re.sub(r"[\"']", " ", raw)) if t]
        tokens = tokens[:40]
        conds = []
        for idx, tok in enumerate(tokens):
            p = f"kw{idx}"
            params[p] = f"%{tok.lower()}%"
            conds.append(f"lower(\"keyStep\") LIKE :{p}")
        where_extra = (" AND " + " AND ".join(conds)) if conds else ""
        sql = text(
            f"""
            SELECT id, module, "keyStep", "cmnSteps", "hasAPIStep", "defaultParam", "apiRequests"
            FROM public.cmn_stps
            WHERE module = :module{where_extra}
            ORDER BY id DESC
            LIMIT :limit
            """
        )
        params["limit"] = limit
    else:
        params.update({"limit": limit})
        sql = text(
            """
            SELECT id, module, "keyStep", "cmnSteps", "hasAPIStep", "defaultParam", "apiRequests"
            FROM public.cmn_stps
            WHERE module = :module
            ORDER BY id DESC
            LIMIT :limit
            """
        )

    with engine.begin() as conn:
        rows = list(conn.execute(sql, params).mappings().all())

    # Convert rows to list of dicts for logging (Row/mappings may not serialize directly)
    rows_serializable = [dict(r) for r in rows]
    logger.info("common-steps/query response:\n%s", json.dumps(rows_serializable, indent=2, default=str))
    return rows


@router.get("/ui/query", response_class=HTMLResponse, include_in_schema=False)
async def ui_query(
    request: Request,
    module: Optional[str] = Query(None),
    keyStep: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    domains = load_domains()
    selected = module or ""
    domain_options = "".join(
        f'<option value="{_html_escape(d)}" {"selected" if d == selected else ""}>{_html_escape(d)}</option>'
        for d in domains
    ) or '<option value="">(no domains found)</option>'

    results_html = ""
    if module:
        params = {"module": module}
        if keyStep and keyStep.strip():
            raw = keyStep.strip()
            tokens = [t for t in re.split(r"\s+", re.sub(r"[\"']", " ", raw)) if t]
            tokens = tokens[:40]
            conds = []
            for idx, tok in enumerate(tokens):
                p = f"kw{idx}"
                params[p] = f"%{tok.lower()}%"
                conds.append(f"lower(\"keyStep\") LIKE :{p}")
            where_extra = (" AND " + " AND ".join(conds)) if conds else ""
            sql = text(
                f"""
                SELECT id, module, "keyStep", "cmnSteps", "hasAPIStep", "defaultParam", "apiRequests"
                FROM public.cmn_stps
                WHERE module = :module{where_extra}
                ORDER BY id DESC
                LIMIT :limit
                """
            )
            params["limit"] = limit
        else:
            params["limit"] = limit
            sql = text(
                """
                SELECT id, module, "keyStep", "cmnSteps", "hasAPIStep", "defaultParam", "apiRequests"
                FROM public.cmn_stps
                WHERE module = :module
                ORDER BY id DESC
                LIMIT :limit
                """
            )

        with engine.begin() as conn:
            rows = list(conn.execute(sql, params).mappings().all())

        if rows:
            body_rows = []
            for r in rows:
                steps_list = "".join(f"<li>{_html_escape(str(s))}</li>" for s in (r.get("cmnSteps") or []))
                default_json = _html_escape(json.dumps(r.get("defaultParam"), indent=2)) if r.get("defaultParam") is not None else "—"
                api_json = _html_escape(json.dumps(r.get("apiRequests"), indent=2)) if r.get("apiRequests") is not None else "—"
                body_rows.append(
                    f"<tr>"
                    f"<td>{r.get('id')}</td>"
                    f"<td>{_html_escape(str(r.get('module')))}</td>"
                    f"<td>{_html_escape(str(r.get('keyStep')))}</td>"
                    f"<td><ul>{steps_list}</ul></td>"
                    f"<td>{'Yes' if r.get('hasAPIStep') else 'No'}</td>"
                    f"<td><pre class=\"json\">{default_json}</pre></td>"
                    f"<td><pre class=\"json\">{api_json}</pre></td>"
                    f"</tr>"
                )
            results_html = (
                "<table><thead><tr>"
                "<th>ID</th><th>Domain</th><th>Key line</th><th>Common steps</th>"
                "<th>Has API</th><th>Default params</th><th>API requests</th>"
                "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"
            )
        else:
            if keyStep and keyStep.strip():
                results_html = "<div class=\"empty\">Sorry! No data found for provided key line.</div>"
            else:
                results_html = "<div class=\"empty\">No results found for the selected domain.</div>"

    html = f"""
<!DOCTYPE html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>Query Step Macros</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 24px; }}
      textarea {{ width: 100%; font-family: Consolas, monospace; }}
      .row {{ margin-bottom: 12px; }}
      table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
      th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; vertical-align: top; }}
      th {{ background: #f7f7f7; }}
      .json {{ white-space: pre-wrap; font-family: Consolas, monospace; background: #fafafa; padding: 8px; }}
      .empty {{ color: #666; margin-top: 12px; }}
    </style>
  </head>
  <body>
    <h2>Find Step Macros</h2>
    <form method=\"get\" action=\"/common-steps/ui/query\"> 
      <div class=\"row\">
        <label>Domain</label><br/>
        <select name=\"module\" required>{domain_options}</select>
      </div>
      <div class=\"row\">
        <label>Key line (optional)</label><br/>
        <input type=\"text\" name=\"keyStep\" value=\"{_html_escape(keyStep or '')}\" style=\"width:100%; padding:8px;\" />
      </div>
      <div class=\"row\">
        <label>Limit</label><br/>
        <input type=\"number\" name=\"limit\" value=\"{limit}\" min=\"1\" max=\"200\" />
      </div>
      <button type=\"submit\">Get step macros</button>
      <a href=\"/common-steps/ui/preview\" style=\"margin-left:12px;\">Add new step macro</a>
      <a href=\"/common-steps/ui/delete\" style=\"margin-left:12px;\">Delete step macros</a>
    </form>
    {results_html}
  </body>
  </html>
"""
    return HTMLResponse(content=html)
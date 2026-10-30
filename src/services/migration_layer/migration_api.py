import asyncio
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from services.migration_layer.steps_migration import migrate_steps_for_date_range
from services.migration_layer.autoheal_migration import migrate_autoheal_scenarios_for_date
from services.migration_layer.tran_status_cleanup import clear_tran_status_table_for_date_range
from utils.db_utils import _get_engine, BASE_DATABASE_URL

router = APIRouter(prefix="/admin", tags=["migration"])


def _parse_iso_date(value: Optional[str], field_name: str) -> Optional[date]:
	if value is None:
		return None
	try:
		return datetime.strptime(value, "%Y-%m-%d").date()
	except ValueError:
		raise HTTPException(status_code=400, detail=f"Invalid {field_name}. Expected format YYYY-MM-DD")


@router.post("/migrate-steps")
async def migrate_steps(
	date_str: Optional[str] = Query(None, alias="date", description="Single date YYYY-MM-DD"),
	start_date_str: Optional[str] = Query(None, alias="start_date", description="Start date YYYY-MM-DD"),
	end_date_str: Optional[str] = Query(None, alias="end_date", description="End date YYYY-MM-DD (inclusive)"),
):
	"""
	Manual trigger to migrate steps from t_gen_resp to t_steps and auto-heal scenarios to t_autoHealScn.
	Also clears tran_status table for the specified date range.
	Provide either:
	- date=YYYY-MM-DD
	or
	- start_date=YYYY-MM-DD & end_date=YYYY-MM-DD (inclusive)
	
	Steps, auto-heal migrations, and tran_status cleanup run for the specified date range.
	"""
	single_date = _parse_iso_date(date_str, "date") if date_str else None
	start_dt = _parse_iso_date(start_date_str, "start_date") if start_date_str else None
	end_dt = _parse_iso_date(end_date_str, "end_date") if end_date_str else None

	if single_date and (start_dt or end_dt):
		raise HTTPException(status_code=400, detail="Provide either date or (start_date and end_date), not both")

	if single_date:
		start_dt = end_dt = single_date
	else:
		if not (start_dt and end_dt):
			raise HTTPException(status_code=400, detail="Provide both start_date and end_date, or provide date")
		if start_dt > end_dt:
			raise HTTPException(status_code=400, detail="start_date must be on or before end_date")

	# Offload blocking DB work to a thread to keep event loop responsive
	# Create a shared engine for consistent connection pooling (matching scheduler pattern)
	eng = _get_engine(BASE_DATABASE_URL)
	steps_summary = await asyncio.to_thread(migrate_steps_for_date_range, start_date=start_dt, end_date=end_dt, engine=eng)
	
	# Migrate auto-heal scenarios for each day in the range
	autoheal_results = []
	current_date = start_dt
	while current_date <= end_dt:
		try:
			result = await asyncio.to_thread(migrate_autoheal_scenarios_for_date, target_date=current_date, engine=eng)
			autoheal_results.append(result)
		except Exception as e:
			autoheal_results.append({
				"target_date": current_date.isoformat(),
				"status": "failed",
				"error": str(e)
			})
		
		current_date = current_date + timedelta(days=1)
	
	# Clear tran_status table for the date range
	tran_status_result = {}
	try:
		tran_status_result = await asyncio.to_thread(
			clear_tran_status_table_for_date_range,
			start_date=start_dt,
			end_date=end_dt,
			engine=eng
		)
	except Exception as e:
		tran_status_result = {
			"status": "failed",
			"rows_deleted": 0,
			"start_date": start_dt.isoformat(),
			"end_date": end_dt.isoformat(),
			"message": f"Failed to clear tran_status table: {str(e)}"
		}
	
	return {
		"ok": True,
		"start_date": start_dt.isoformat(),
		"end_date": end_dt.isoformat(),
		"summary": steps_summary,  # BACKWARD COMPAT: kept for existing clients
		"steps_migration": steps_summary,  # NEW: clearer naming
		"autoheal_migration": autoheal_results,
		"tran_status_cleanup": tran_status_result,  # NEW: tran_status cleanup results
	}



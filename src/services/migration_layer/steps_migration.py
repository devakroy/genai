import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine
from psycopg2.extras import execute_values

from utils.db_utils import PGSCHEMA, _get_engine, BASE_DATABASE_URL

logger = logging.getLogger(__name__)


# -----------------------------
# Normalization helpers
# -----------------------------
def _normalize_whitespace(value: str) -> str:
	"""
	Collapse all internal whitespace to a single space after trimming.
	"""
	if value is None:
		return ""
	# Replace any whitespace (including tabs/newlines) runs with a single space
	return " ".join(str(value).strip().split())


def normalize_step_for_compare(step_text: str) -> str:
	"""
	Normalization used for de-duplication comparisons:
	- Trim ends
	- Collapse internal whitespace
	- Lowercase for case-insensitive comparisons
	"""
	return _normalize_whitespace(step_text).lower()


# -----------------------------
# Feature steps extraction
# -----------------------------
def extract_steps_from_feature_json_text(feature_steps_text: Optional[str]) -> List[str]:
	"""
	Parse the JSON text in feature_steps and extract a flat list of step strings.
	Expected shape in DB:
	[{"scenario": "Some name", "steps": ["When ...", "Then ...", ...]}]

	Returns an empty list on parse errors or if no steps are found.
	"""
	if not feature_steps_text:
		return []
	try:
		payload = json.loads(feature_steps_text)
	except Exception:
		logger.warning("Failed to parse feature_steps JSON; skipping this record")
		return []

	steps: List[str] = []
	try:
		# handle list of objects; each object may contain "steps": [ ... ]
		if isinstance(payload, list):
			for item in payload:
				if isinstance(item, dict) and "steps" in item and isinstance(item["steps"], list):
					for s in item["steps"]:
						if isinstance(s, str) and s.strip():
							steps.append(s.strip())
		elif isinstance(payload, dict) and "steps" in payload and isinstance(payload["steps"], list):
			for s in payload["steps"]:
				if isinstance(s, str) and s.strip():
					steps.append(s.strip())
	except Exception:
		logger.exception("Unexpected structure in feature_steps; skipping problematic parts")
		return steps

	return steps


# -----------------------------
# DB utilities
# -----------------------------
def ensure_steps_unique_index(engine: Optional[Engine] = None) -> None:
	"""
	Create a unique expression index that prevents duplicates per module, case-insensitively,
	with whitespace collapsed in steps.
	"""
	eng = engine or _get_engine(BASE_DATABASE_URL)
	ddl = text(f"""
		CREATE UNIQUE INDEX IF NOT EXISTS ux_t_steps_mod_step_norm
		ON {PGSCHEMA}.t_steps (
			lower(modulenm),
			lower(regexp_replace(btrim(step), '\\s+', ' ', 'g'))
		)
	""")
	with eng.begin() as conn:
		conn.execute(ddl)
	logger.info("Ensured unique expression index ux_t_steps_mod_step_norm on t_steps")


def _fetch_passed_rows_for_range(
	start_date: date,
	end_date: date,
	engine: Optional[Engine] = None,
) -> Iterable[Tuple[str, Optional[str]]]:
	"""
	Fetch rows from t_gen_resp with status='PASSED' and execution_dt within [start_date, end_date] inclusive.
	Implementation uses an exclusive upper bound (< end_date + 1 day) to work for DATE or TIMESTAMP columns
	while keeping index-friendly predicates.
	Returns tuples of (modulenm, feature_steps_text).
	"""
	eng = engine or _get_engine(BASE_DATABASE_URL)
	sql = text(f"""
		SELECT modulenm,
		       feature_steps
		FROM {PGSCHEMA}.t_gen_resp
		WHERE status = 'PASSED'
		  AND execution_dt >= :start_dt
		  AND execution_dt < :end_exclusive
	""")
	params = {
		"start_dt": start_date.strftime("%Y-%m-%d"),
		"end_exclusive": (end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
	}
	with eng.begin() as conn:
		for row in conn.execute(sql, params):
			yield (row[0], row[1])


def migrate_steps_for_date_range(
	start_date: date,
	end_date: date,
	engine: Optional[Engine] = None,
) -> Dict[str, int]:
	"""
	Main migration routine:
	- Read PASSED rows within date range from t_gen_resp
	- Parse feature_steps to extract steps
	- Group by module (case-insensitive for dedupe)
	- Deduplicate steps per module using normalized compare
	- Insert into t_steps (step=original text, modulenm=as stored)
	- Skip duplicates via ON CONFLICT DO NOTHING (backed by expression unique index)

	Returns a summary dict with counts.
	"""
	ensure_steps_unique_index(engine=engine)

	eng = engine or _get_engine(BASE_DATABASE_URL)

	# Track dedupe per module (case-insensitive); store first-seen original for each normalized step
	module_to_seen_norm: Dict[str, Set[str]] = defaultdict(set)
	# Diagnostics: per-module counters
	per_module_extracted: Dict[str, int] = defaultdict(int)
	per_module_unique: Dict[str, int] = defaultdict(int)
	# Collect rows to insert: (step, modulenm)
	inserts: List[Tuple[str, str]] = []

	total_source_rows = 0
	total_steps_extracted = 0

	for modulenm, feature_steps_text in _fetch_passed_rows_for_range(start_date, end_date, engine=eng):
		total_source_rows += 1
		if not modulenm:
			continue
		mod_key = (modulenm or "").strip().lower()
		if not mod_key:
			continue

		steps = extract_steps_from_feature_json_text(feature_steps_text)
		if not steps:
			continue

		for s in steps:
			total_steps_extracted += 1
			per_module_extracted[mod_key] += 1
			norm = normalize_step_for_compare(s)
			if not norm:
				continue
			if norm in module_to_seen_norm[mod_key]:
				continue
			# First time we see this normalized step for the module -> use original text
			module_to_seen_norm[mod_key].add(norm)
			per_module_unique[mod_key] += 1
			inserts.append((s, modulenm))

	inserted = 0
	if inserts:
		with eng.begin() as conn:
			# Use ON CONFLICT DO NOTHING so the unique expression index suppresses duplicates
			sql = f'''
				INSERT INTO {PGSCHEMA}.t_steps (step, modulenm)
				VALUES %s
				ON CONFLICT DO NOTHING
			'''
			cur = conn.connection.cursor()
			# Manually chunk to ensure cur.rowcount reflects each batch (psycopg2 execute_values only reports last page)
			total_inserted = 0
			batch_size = 1000
			for i in range(0, len(inserts), batch_size):
				batch = inserts[i:i + batch_size]
				# Ensure single page per execute to make rowcount accurate for this batch
				execute_values(cur, sql, batch, page_size=len(batch))
				rc = getattr(cur, "rowcount", 0) or 0
				if rc < 0:
					rc = 0
				total_inserted += rc
			inserted = total_inserted

	logger.info(
		"Steps migration summary | rows=%s steps_extracted=%s unique_pairs=%s inserted=%s",
		total_source_rows, total_steps_extracted, len(inserts), inserted
	)
	# Emit per-module diagnostics
	for mod_key in sorted(set(list(per_module_extracted.keys()) + list(per_module_unique.keys()))):
		logger.info(
			"Module '%s' | extracted=%s unique_after_norm=%s",
			mod_key, per_module_extracted.get(mod_key, 0), per_module_unique.get(mod_key, 0)
		)
	
	# Export migrated steps to CSV file after migration
	logger.info("Exporting migrated steps to CSV file")
	export_result = export_steps_data(engine=eng)
	logger.info("Migration and export completed | file: %s | records: %d", export_result["file_path"], export_result["total_records"])
	
	return {
		"source_rows": total_source_rows,
		"steps_extracted": total_steps_extracted,
		"unique_module_step_pairs": len(inserts),
		"inserted": inserted,
		"per_module": {
			m: {
				"extracted": per_module_extracted.get(m, 0),
				"unique_after_normalization": per_module_unique.get(m, 0),
			}
			for m in set(list(per_module_extracted.keys()) + list(per_module_unique.keys()))
		},
		"csv_export": {
			"file_path": export_result["file_path"],
			"export_timestamp": export_result["export_timestamp"],
			"total_records": export_result["total_records"],
		},
	}


# -----------------------------
# File export functionality
# -----------------------------
def _get_export_directory() -> Path:
	"""
	Get or create the export directory for migrated steps files.
	Location: target/steps_migration/
	"""
	export_dir = Path(__file__).parent.parent.parent.parent / "target" / "steps_migration"
	export_dir.mkdir(parents=True, exist_ok=True)
	return export_dir


def _fetch_all_steps_from_table(engine: Optional[Engine] = None) -> List[Dict]:
	"""
	Fetch all steps from t_steps table ordered by module and step.
	"""
	eng = engine or _get_engine(BASE_DATABASE_URL)
	sql = text(f"""
		SELECT id, step, modulenm
		FROM {PGSCHEMA}.t_steps
		ORDER BY modulenm, step
	""")
	
	steps_list = []
	with eng.begin() as conn:
		for row in conn.execute(sql):
			steps_list.append({
				"id": row[0],
				"step": row[1],
				"modulenm": row[2],
			})
	return steps_list


def export_steps_to_csv_single(engine: Optional[Engine] = None) -> str:
	"""
	Export all steps from t_steps table to a single CSV file.
	Filename: steps_migration.csv (fixed name, no date suffix)
	Columns: id, step, modulenm
	File is overwritten on each call.
	
	Returns the path to the created file.
	"""
	import csv
	
	steps_data = _fetch_all_steps_from_table(engine=engine)
	
	export_dir = _get_export_directory()
	filename = "steps_migration.csv"
	filepath = export_dir / filename
	
	# Write CSV file with headers: id, step, modulenm (overwrites existing file)
	with open(filepath, "w", newline='', encoding="utf-8") as f:
		writer = csv.DictWriter(f, fieldnames=["id", "step", "modulenm"])
		writer.writeheader()
		writer.writerows(steps_data)
	
	logger.info("Exported %d steps to CSV file: %s", len(steps_data), filepath)
	return str(filepath)


def export_steps_data(engine: Optional[Engine] = None) -> Dict[str, str]:
	"""
	Export steps to CSV format for browser consumption.
	Filename: steps_migration.csv (fixed name, overwrites existing file)
	
	Returns a dict with export metadata including file path and record count.
	"""
	csv_path = export_steps_to_csv_single(engine=engine)
	
	# Get record count from the CSV
	steps_data = _fetch_all_steps_from_table(engine=engine)
	
	return {
		"file_path": csv_path,
		"export_timestamp": datetime.now().isoformat(),
		"format": "csv",
		"total_records": len(steps_data),
	}


# -----------------------------
# Background scheduler
# -----------------------------
async def _sleep_until(target_dt: datetime) -> None:
	now = datetime.now()
	if target_dt > now:
		await asyncio.sleep((target_dt - now).total_seconds())


def _next_run_at_local(hour: int, minute: int) -> datetime:
	now = datetime.now()
	run_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
	if run_dt <= now:
		run_dt = run_dt + timedelta(days=1)
	return run_dt


def _yesterday_local() -> date:
	return (datetime.now().date() - timedelta(days=1))


async def daily_steps_migration_forever(hour: int = 0, minute: int = 10) -> None:
	"""
	Background task: every day at HH:MM local time, migrate steps for D-1.
	Also migrates auto-heal scenarios to t_autoHealScn table.
	Also clears the tran_status table daily.
	"""
	logger.info("Daily steps migration scheduler started (local %02d:%02d)", hour, minute)
	eng = _get_engine(BASE_DATABASE_URL)
	ensure_steps_unique_index(engine=eng)

	while True:
		try:
			run_at = _next_run_at_local(hour, minute)
			logger.info("Next steps migration scheduled at %s", run_at.isoformat())
			await _sleep_until(run_at)
			target = _yesterday_local()
			logger.info("Running daily steps migration for %s", target.isoformat())
			# Offload blocking DB work to a thread to avoid blocking the event loop
			await asyncio.to_thread(migrate_steps_for_date_range, start_date=target, end_date=target, engine=eng)
			
			# ===== NEW: Migrate auto-heal scenarios for the same date =====
			# This runs after steps migration. If it fails, it won't block the main scheduler.
			try:
				from services.migration_layer.autoheal_migration import migrate_autoheal_scenarios_for_date
				logger.info("Starting auto-heal scenario migration for %s", target.isoformat())
				autoheal_result = await asyncio.to_thread(migrate_autoheal_scenarios_for_date, target_date=target, engine=eng)
				logger.info("Auto-heal migration result: %s", autoheal_result)
			except ImportError:
				logger.warning("Auto-heal migration module not available; skipping t_autoHealScn update")
			except Exception as autoheal_err:
				logger.error("Auto-heal migration failed for %s: %s", target, autoheal_err, exc_info=True)
				# Continue even if auto-heal fails - don't block the main scheduler
			# ===== END: Auto-heal migration =====
			
			# ===== NEW: Clear tran_status table daily =====
			# This runs after auto-heal migration. If it fails, it won't block the main scheduler.
			try:
				from services.migration_layer.tran_status_cleanup import clear_tran_status_table_daily
				logger.info("Starting tran_status table cleanup")
				cleanup_result = await asyncio.to_thread(clear_tran_status_table_daily, engine=eng)
				logger.info("Tran_status cleanup result: %s", cleanup_result)
			except ImportError:
				logger.warning("Tran_status cleanup module not available; skipping tran_status cleanup")
			except Exception as cleanup_err:
				logger.error("Tran_status cleanup failed: %s", cleanup_err, exc_info=True)
				# Continue even if cleanup fails - don't block the main scheduler
			# ===== END: Tran_status cleanup =====
			
		except Exception:
			logger.exception("Daily steps migration run failed")
		# Ensure at least small delay before computing next run
		await asyncio.sleep(1.0)



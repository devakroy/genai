"""
Auto-Heal Scenario Migration

Handles daily migration of successful scenario execution data from t_gen_resp to t_autoHealScn table.
Implements deduplication logic based on unique combinations of scenario metadata.
Integrated with existing daily steps migration scheduler.
"""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from utils.db_utils import PGSCHEMA, _get_engine, BASE_DATABASE_URL

logger = logging.getLogger(__name__)


class AutoHealMigrationService:
	"""Service for migrating successful scenario executions to t_autoHealScn table"""
	
	def __init__(self, engine: Optional[Engine] = None):
		"""Initialize the service with database engine"""
		self.engine = engine or _get_engine(BASE_DATABASE_URL)
	
	@staticmethod
	def _normalize_value(value: Optional[str]) -> str:
		"""Normalize values for comparison (handle None, empty strings, non-string types)"""
		if value is None:
			return ""
		# Always apply .strip().lower() regardless of original type
		# Ensures Python dedup matches SQL DELETE logic: LOWER(BTRIM(COALESCE(..., '')))
		return str(value).strip().lower()
	
	@staticmethod
	def _create_unique_key(scn_tags: str, modulenm: str, feature_tags: str, 
						  feature_nm: str, scenarioname: str) -> tuple:
		"""
		Create a unique key from scenario metadata for deduplication.
		Returns a tuple of normalized values (tuples are unambiguous, unlike pipe-delimited strings).
		"""
		norm_scn_tags = AutoHealMigrationService._normalize_value(scn_tags)
		norm_modulenm = AutoHealMigrationService._normalize_value(modulenm)
		norm_feature_tags = AutoHealMigrationService._normalize_value(feature_tags)
		norm_feature_nm = AutoHealMigrationService._normalize_value(feature_nm)
		norm_scenarioname = AutoHealMigrationService._normalize_value(scenarioname)
		
		# Use tuple as key: unambiguous and hashable
		# Avoids pipe-delimiter collision problem (e.g., "a|b|c" vs "a|b|c" from different field splits)
		return (norm_scn_tags, norm_modulenm, norm_feature_tags, norm_feature_nm, norm_scenarioname)
	
	def _fetch_passed_executions_for_date(self, target_date: date) -> List[Dict]:
		"""
		Fetch all PASSED executions from t_gen_resp for a specific date.
		Uses range comparison to allow index usage on execution_dt (not function-wrapped).
		
		Args:
			target_date: The date to fetch executions for
			
		Returns:
			List of execution dictionaries with required metadata
		"""
		try:
			# Use range comparison for index-friendly query: >= start AND < (start+1 day)
			# Avoids DATE(execution_dt) = :target_date which prevents index usage
			end_exclusive = target_date + timedelta(days=1)
			
			query = text(f"""
				SELECT 
					id,
					runid,
					scn_tags,
					modulenm,
					feature_tags,
					feature_nm,
					scenarioname,
					stp_loc_map,
					complt_scripts,
					api_steps,
					feature_steps,
					execution_dt,
					status
				FROM {PGSCHEMA}.t_gen_resp
				WHERE status = 'PASSED'
				  AND execution_dt >= :start_dt
				  AND execution_dt < :end_exclusive
				ORDER BY execution_dt DESC
			""")
			
			params = {
				"start_dt": target_date.strftime("%Y-%m-%d"),
				"end_exclusive": end_exclusive.strftime("%Y-%m-%d")
			}
			
			with self.engine.begin() as conn:
				result = conn.execute(query, params).fetchall()
				
				executions = []
				for row in result:
					executions.append({
						'id': row[0],
						'runid': row[1],
						'scn_tags': row[2],
						'modulenm': row[3],
						'feature_tags': row[4],
						'feature_nm': row[5],
						'scenarioname': row[6],
						'stp_loc_map': row[7],
						'complt_scripts': row[8],
						'api_steps': row[9],
						'feature_steps': row[10],
						'execution_dt': row[11],
						'status': row[12]
					})
				
				logger.info(f"Auto-heal: Fetched {len(executions)} PASSED executions for {target_date}")
				return executions
				
		except Exception as e:
			logger.error(f"Auto-heal: Error fetching passed executions for {target_date}: {e}", exc_info=True)
			raise
	
	def _deduplicate_executions(self, executions: List[Dict]) -> Dict[tuple, Dict]:
		"""
		Deduplicate executions by unique combination of metadata.
		For duplicate combinations, keep the latest execution (by datetime).
		
		Logic:
		- Group by unique combo: (scn_tags, modulenm, feature_tags, feature_nm, scenarioname)
		- Keep only the execution with latest execution_dt (most recent)
		
		Args:
			executions: List of execution records from t_gen_resp
			
		Returns:
			Dict mapping unique_key (tuple) -> latest execution record
		"""
		unique_executions = {}
		
		for execution in executions:
			unique_key = self._create_unique_key(
				scn_tags=execution.get('scn_tags'),
				modulenm=execution.get('modulenm'),
				feature_tags=execution.get('feature_tags'),
				feature_nm=execution.get('feature_nm'),
				scenarioname=execution.get('scenarioname')
			)
			
			if unique_key not in unique_executions:
				unique_executions[unique_key] = execution
				logger.debug(f"First occurrence of unique combo: {unique_key}")
			else:
				existing_dt = unique_executions[unique_key].get('execution_dt')
				current_dt = execution.get('execution_dt')
				
				if current_dt and existing_dt and current_dt > existing_dt:
					logger.debug(f"Replacing with newer execution for unique combo: {unique_key}")
					unique_executions[unique_key] = execution
		
		logger.info(f"Auto-heal: Deduplicated {len(executions)} executions to {len(unique_executions)} unique combinations")
		return unique_executions
	
	@staticmethod
	def _to_json_str(value) -> Optional[str]:
		"""Helper to serialize dict/None to JSON string"""
		if value is None:
			return None
		if isinstance(value, dict):
			return json.dumps(value)
		if isinstance(value, str):
			# Already a string, could be JSON or plain text
			return value
		# Fallback for other types
		return json.dumps(value)
	
	def _upsert_into_autoheal_table(self, executions: Dict[tuple, Dict], 
									migration_dt: datetime) -> int:
		"""
		Insert or update t_autoHealScn table with deduplicated executions.
		
		Logic:
		- If unique combo (scn_tags, modulenm, feature_tags, feature_nm, scenarioname) exists, UPDATE
		- Otherwise, INSERT new row
		- Always update update_dt with migration timestamp
		
		Args:
			executions: Dict of unique_key (tuple) -> execution record
			migration_dt: Datetime of the migration run
			
		Returns:
			Number of rows inserted/updated
		"""
		if not executions:
			logger.info("Auto-heal: No executions to migrate")
			return 0
		
		try:
			# This upsert strategy: DELETE existing record (if any) then INSERT
			# This avoids needing a UNIQUE constraint on the table
			# Using IS NOT DISTINCT FROM handles NULL comparisons correctly (NULL-safe equality)
			# Using LOWER(regexp_replace(...)) matches Python's _normalize_value logic:
			#   - COALESCE(..., '') converts NULL to empty string (matching Python's None → "")
			#   - regexp_replace with '^\s+|\s+$' removes all leading/trailing whitespace (matching Python's .strip())
			#   - LOWER() lowercases (matching Python's .lower())
			delete_sql = text(f"""
				DELETE FROM {PGSCHEMA}."t_autoHealScn"
				WHERE LOWER(regexp_replace(COALESCE(scn_tags, ''), '^\s+|\s+$', '', 'g')) IS NOT DISTINCT FROM LOWER(regexp_replace(COALESCE(:scn_tags, ''), '^\s+|\s+$', '', 'g'))
				  AND LOWER(regexp_replace(COALESCE(modulenm, ''), '^\s+|\s+$', '', 'g')) IS NOT DISTINCT FROM LOWER(regexp_replace(COALESCE(:modulenm, ''), '^\s+|\s+$', '', 'g'))
				  AND LOWER(regexp_replace(COALESCE(feature_tags, ''), '^\s+|\s+$', '', 'g')) IS NOT DISTINCT FROM LOWER(regexp_replace(COALESCE(:feature_tags, ''), '^\s+|\s+$', '', 'g'))
				  AND LOWER(regexp_replace(COALESCE(feature_nm, ''), '^\s+|\s+$', '', 'g')) IS NOT DISTINCT FROM LOWER(regexp_replace(COALESCE(:feature_nm, ''), '^\s+|\s+$', '', 'g'))
				  AND LOWER(regexp_replace(COALESCE(scenarioname, ''), '^\s+|\s+$', '', 'g')) IS NOT DISTINCT FROM LOWER(regexp_replace(COALESCE(:scenarioname, ''), '^\s+|\s+$', '', 'g'))
			""")
			
			insert_sql = text(f"""
				INSERT INTO {PGSCHEMA}."t_autoHealScn"
				(runid, scn_tags, modulenm, feature_tags, feature_nm, scenarioname, 
				 stp_loc_map, complt_scripts, api_steps, feature_steps, update_dt)
				VALUES
				(:runid, :scn_tags, :modulenm, :feature_tags, :feature_nm, :scenarioname,
				 :stp_loc_map, :complt_scripts, :api_steps, :feature_steps, :update_dt)
			""")
			
			rows_processed = 0
			
			for unique_key, execution in executions.items():
				try:
					params = {
						'runid': execution.get('runid'),
						'scn_tags': execution.get('scn_tags'),
						'modulenm': execution.get('modulenm'),
						'feature_tags': execution.get('feature_tags'),
						'feature_nm': execution.get('feature_nm'),
						'scenarioname': execution.get('scenarioname'),
						'stp_loc_map': self._to_json_str(execution.get('stp_loc_map')),
						'complt_scripts': execution.get('complt_scripts'),
						'api_steps': self._to_json_str(execution.get('api_steps')),
						'feature_steps': self._to_json_str(execution.get('feature_steps')),
						'update_dt': migration_dt
					}
					
					# Each upsert (delete + insert) gets its own transaction
					with self.engine.begin() as conn:
						conn.execute(delete_sql, params)
						conn.execute(insert_sql, params)
					rows_processed += 1
					
				except Exception as row_err:
					logger.error(f"Auto-heal: Error upserting scenario (key={unique_key}): {row_err}")
					# Continue with next row even if this one fails
					continue
			
			logger.info(f"Auto-heal: Successfully processed {rows_processed} scenarios")
			return rows_processed
			
		except Exception as e:
			logger.error(f"Auto-heal: Error during upsert to t_autoHealScn: {e}", exc_info=True)
			raise
	
	def migrate_passed_scenarios_for_date(self, target_date: date) -> Dict[str, Any]:
		"""
		Execute the full migration workflow for a specific date:
		1. Fetch PASSED executions from t_gen_resp (only status='PASSED')
		2. Deduplicate by unique scenario metadata combination (keep latest by execution_dt)
		3. Upsert into t_autoHealScn (insert new or update existing)
		
		Args:
			target_date: The date to migrate data for
			
		Returns:
			Summary dict with migration statistics
		"""
		try:
			logger.info(f"Auto-heal: Starting migration for {target_date}")
			
			# Step 1: Fetch all PASSED executions for the date
			executions = self._fetch_passed_executions_for_date(target_date)
			
			if not executions:
				logger.info(f"Auto-heal: No PASSED executions for {target_date}")
				return {
					"target_date": str(target_date),
					"source_executions": 0,
					"unique_combinations": 0,
					"migrated_scenarios": 0,
					"status": "completed_no_data"
				}
			
			# Step 2: Deduplicate executions (keep latest for each unique combo)
			unique_executions = self._deduplicate_executions(executions)
			
			# Step 3: Upsert into t_autoHealScn
			migration_dt = datetime.now()
			migrated_count = self._upsert_into_autoheal_table(unique_executions, migration_dt)
			
			summary = {
				"target_date": str(target_date),
				"source_executions": len(executions),
				"unique_combinations": len(unique_executions),
				"migrated_scenarios": migrated_count,
				"migration_timestamp": migration_dt.isoformat(),
				"status": "completed_success"
			}
			
			logger.info(f"Auto-heal: Migration complete - {summary}")
			return summary
			
		except Exception as e:
			logger.error(f"Auto-heal: Migration failed for {target_date}: {e}", exc_info=True)
			return {
				"target_date": str(target_date),
				"status": "failed",
				"error": str(e)
			}


def migrate_autoheal_scenarios_for_date(target_date: date, engine: Optional[Engine] = None) -> Dict[str, Any]:
	"""
	Synchronous wrapper for daily auto-heal migration.
	Called by the existing daily scheduler.
	
	Args:
		target_date: The date to migrate (typically yesterday)
		engine: Optional database engine
		
	Returns:
		Migration summary dict
	"""
	service = AutoHealMigrationService(engine=engine)
	return service.migrate_passed_scenarios_for_date(target_date)

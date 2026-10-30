import logging
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from utils.db_utils import PGSCHEMA, _get_engine, BASE_DATABASE_URL

logger = logging.getLogger(__name__)


def clear_tran_status_table_for_date_range(
    start_date: date,
    end_date: date,
    engine: Optional[Engine] = None
) -> Dict[str, any]:
    eng = engine or _get_engine(BASE_DATABASE_URL)
    
    try:
        with eng.begin() as conn:
            # Execute DELETE query to remove data from tran_status table for the date range
            # Uses execution_dt column to filter by date
            sql = text(f"""
                DELETE FROM {PGSCHEMA}."tran_status"
                WHERE DATE(execution_dt) >= :start_date
                  AND DATE(execution_dt) <= :end_date
            """)
            result = conn.execute(sql, {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat()
            })
            rows_deleted = result.rowcount if result.rowcount else 0
            
            logger.info(f"✅ TRAN_STATUS CLEANUP: SUCCESS | Deleted {rows_deleted} rows from {start_date} to {end_date}")
            
            return {
                "status": "success",
                "rows_deleted": rows_deleted,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "message": f"Successfully deleted {rows_deleted} rows from tran_status table for date range {start_date} to {end_date}"
            }
    
    except Exception as e:
        logger.error(f"❌ TRAN_STATUS CLEANUP: FAILED | Error: {str(e)}", exc_info=True)
        return {
            "status": "failed",
            "rows_deleted": 0,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "message": f"Failed to clean tran_status table: {str(e)}"
        }


def clear_tran_status_table(engine: Optional[Engine] = None) -> Dict[str, any]:
    eng = engine or _get_engine(BASE_DATABASE_URL)
    
    try:
        with eng.begin() as conn:
            # Execute DELETE query to remove all data from tran_status table
            sql = text(f"""
                DELETE FROM {PGSCHEMA}."tran_status"
            """)
            result = conn.execute(sql)
            rows_deleted = result.rowcount if result.rowcount else 0
            
            logger.info(f"✅ TRAN_STATUS CLEANUP: SUCCESS | Deleted {rows_deleted} rows from tran_status table")
            
            return {
                "status": "success",
                "rows_deleted": rows_deleted,
                "message": f"Successfully deleted {rows_deleted} rows from tran_status table"
            }
    
    except Exception as e:
        logger.error(f"❌ TRAN_STATUS CLEANUP: FAILED | Error: {str(e)}", exc_info=True)
        return {
            "status": "failed",
            "rows_deleted": 0,
            "message": f"Failed to clean tran_status table: {str(e)}"
        }


def clear_tran_status_table_daily(engine: Optional[Engine] = None) -> Dict[str, any]:
    logger.info("🔄 TRAN_STATUS CLEANUP: Starting daily cleanup task")
    
    # Get yesterday's date for cleanup
    yesterday = (datetime.now().date() - timedelta(days=1))
    
    result = clear_tran_status_table_for_date_range(
        start_date=yesterday,
        end_date=yesterday,
        engine=engine
    )
    
    if result["status"] == "success":
        logger.info(f"✅ TRAN_STATUS CLEANUP: Daily task completed successfully | {result['message']}")
    else:
        logger.error(f"❌ TRAN_STATUS CLEANUP: Daily task failed | {result['message']}")
    
    return result

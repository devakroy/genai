"""
Analytics Tracker for Test Genie
Collects and stores analytical data to t_gen_usage table for each execution
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy import text
from utils.db_utils import engine
from utils.constants import PLATFORM_CONFIG

logger = logging.getLogger(__name__)

class AnalyticsTracker:
    """
    Tracks and stores analytics data for each test execution.
    Collects metrics from various stages and saves to t_gen_usage table.
    """
    
    def __init__(self, jira_id: str, selected_domain: Optional[str] = None,
                 total_scenarios: Optional[int] = None,
                 scenario_tag: Optional[str] = None,
                 scenario_description: Optional[str] = None):
        """
        Initialize analytics tracker for a specific scenario execution.
        
        Args:
            jira_id: The JIRA ticket ID for this execution
            selected_domain: The domain/module being tested (optional)
            total_scenarios: Total number of scenarios in the feature file
            scenario_tag: The scenario tag (e.g., "@scenario1")
            scenario_description: The scenario description (e.g., "Test CSRB1")
        """
        self.jira_id = jira_id
        self.selected_domain = selected_domain
        self.start_time = datetime.now()
        self.end_time = None
        
        # Metrics to collect
        self.module = selected_domain or "unknown"
        self.scn_count = total_scenarios or 0  # Total scenarios in feature file
        self.feature_nm = jira_id
        self.time_taken = 0
        self.execution_dt = datetime.now()
        self.script_gen_success = False
        self.reexecution_success = False
        self.crl_type = "Unknown"
        
        # Additional context
        self.push_to_github = False
        self.generate_step_definitions = False
        self.record_video = False
        self.error_message = None
        
        # Feature metadata
        self.feature_tag = None  # e.g., "@webui-test5"
        self.feature_description = None  # e.g., "Place Guest Order with PayPal"
        
        # Scenario metadata (per-scenario tracking)
        self.scn_tag = scenario_tag  # e.g., "@scenario1"
        self.scn_description = scenario_description  # e.g., "Test CSRB1"
        
        logger.info(f"📊 Analytics tracker initialized - {jira_id} - Scenario: {scenario_tag}")
    
    def set_crawler_type(self, dry_run_strategy: bool):
        """
        Set the crawler type used for this execution.
        
        Args:
            dry_run_strategy: True if MCP crawler, False if Javascript crawler
        """
        self.crl_type = "MCP" if dry_run_strategy else "Javascript"
        logger.debug(f"📊 Crawler type set to: {self.crl_type}")
    
    
    def set_execution_mode(self, push_to_github: bool = False, 
                          generate_step_definitions: bool = False,
                          record_video: bool = False):
        """
        Set execution mode flags.
        
        Args:
            push_to_github: Whether pushing to GitHub
            generate_step_definitions: Whether generating step definitions
            record_video: Whether recording video
        """
        self.push_to_github = push_to_github
        self.generate_step_definitions = generate_step_definitions
        self.record_video = record_video
        logger.debug(f"📊 Execution mode - GitHub: {push_to_github}, "
                    f"StepDefs: {generate_step_definitions}, Video: {record_video}")
    
    
    def set_feature_metadata(self, feature_tag: Optional[str] = None,
                            feature_description: Optional[str] = None):
        """
        Set feature file metadata.
        
        Args:
            feature_tag: First tag from feature file (e.g., "@webui-test5")
            feature_description: Feature description (e.g., "Place Guest Order with PayPal")
        """
        if feature_tag:
            # Keep @ prefix if present, otherwise add it
            self.feature_tag = f"@{feature_tag}" if not feature_tag.startswith('@') else feature_tag
        if feature_description:
            self.feature_description = feature_description
        logger.debug(f"📊 Feature metadata - Tag: {self.feature_tag}, Desc: {self.feature_description}")
    
    def set_error(self, error_message: str):
        """
        Set error message if execution failed.
        
        Args:
            error_message: The error message to record
        """
        self.error_message = error_message
        self.script_gen_success = False
        self.reexecution_success = False
        logger.debug(f"📊 Error recorded: {error_message[:100]}")
    
    def finalize(self) -> Dict[str, Any]:
        """
        Finalize analytics collection by calculating final metrics.
        
        Returns:
            Dictionary containing all collected analytics data
        """
        self.end_time = datetime.now()
        self.time_taken = int((self.end_time - self.start_time).total_seconds())
        
        analytics_data = {
            "module": self.module,
            "scn_count": self.scn_count,
            "feature_nm": self.feature_nm,
            "time_taken": self.time_taken,
            "execution_dt": self.execution_dt,
            "script_gen_success": self.script_gen_success,
            "reexecution_success": self.reexecution_success,
            "crl_type": self.crl_type,
            "push_to_github": self.push_to_github,
            "feature_tag": self.feature_tag,
            "feature_description": self.feature_description,
            "scn_tag": self.scn_tag,
            "scn_description": self.scn_description,
            # Additional metadata (not in DB table but useful for logging)
            "_metadata": {
                "push_to_github": self.push_to_github,
                "generate_step_definitions": self.generate_step_definitions,
                "record_video": self.record_video,
                "error_message": self.error_message,
                "start_time": self.start_time.isoformat(),
                "end_time": self.end_time.isoformat() if self.end_time else None
            }
        }
        
        logger.info(f"📊 Analytics finalized - Time: {self.time_taken}s, "
                   f"Success: {self.script_gen_success}")
        
        return analytics_data
    
    def save_to_database(self) -> bool:
        """
        Save analytics data to t_gen_usage table in the database.
        
        Returns:
            bool: True if save was successful, False otherwise
        """
        try:
            # Finalize data collection
            analytics_data = self.finalize()
            
            # Import here to get the same constants used by working db code
            from utils.db_utils import PGSCHEMA
            
            # Insert data into t_gen_usage table
            insert_sql = text(f"""
                INSERT INTO {PGSCHEMA}.t_gen_usage
                (module, scn_count, feature_nm, time_taken, execution_dt,
                 scrpt_gen_success, reexecution_success, crl_type, push_to_github,
                 feature_tag, feature_description, scn_tag, scn_description)
                VALUES
                (:module, :scn_count, :feature_nm, :time_taken, :execution_dt,
                 :scrpt_gen_success, :reexecution_success, :crl_type, :push_to_github,
                 :feature_tag, :feature_description, :scn_tag, :scn_description)
            """)
            
            with engine.begin() as conn:
                result = conn.execute(insert_sql, {
                    "module": analytics_data["module"],
                    "scn_count": analytics_data["scn_count"],
                    "feature_nm": analytics_data["feature_nm"],
                    "time_taken": analytics_data["time_taken"],
                    "execution_dt": analytics_data["execution_dt"],
                    "scrpt_gen_success": analytics_data["script_gen_success"],
                    "reexecution_success": analytics_data["reexecution_success"],
                    "crl_type": analytics_data["crl_type"],
                    "push_to_github": analytics_data["push_to_github"],
                    "feature_tag": analytics_data["feature_tag"],
                    "feature_description": analytics_data["feature_description"],
                    "scn_tag": analytics_data["scn_tag"],
                    "scn_description": analytics_data["scn_description"]
                })
            
            logger.info(f"✅ Analytics saved - {self.scn_tag}: {self.scn_description}, "
                       f"Time: {self.time_taken}s, Success: {self.script_gen_success}")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Analytics save failed - {type(e).__name__}: {str(e)}")
            logger.error(f"   JIRA ID: {self.jira_id}, Module: {self.module}", exc_info=True)
            # Don't raise exception - analytics should not break the main flow
            return False
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - automatically save on exit"""
        if exc_type is not None:
            # An exception occurred during processing
            error_msg = f"{exc_type.__name__}: {str(exc_val)}"
            self.set_error(error_msg)
        
        # Always save analytics, even if there was an error
        self.save_to_database()
        
        # Don't suppress the exception
        return False


def track_execution(jira_id: str, selected_domain: Optional[str] = None,
                   total_scenarios: Optional[int] = None,
                   scenario_tag: Optional[str] = None,
                   scenario_description: Optional[str] = None) -> AnalyticsTracker:
    """
    Factory function to create an analytics tracker for per-scenario tracking.
    
    Usage:
        tracker = track_execution(
            jira_id="TEST-123",
            selected_domain="Payment",
            total_scenarios=3,
            scenario_tag="@scenario1",
            scenario_description="Test CSRB1"
        )
        tracker.set_crawler_type(dry_run_strategy=False)
        # ... process scenario ...
        tracker.save_to_database()
    
    Or use as context manager:
        with track_execution(jira_id="TEST-123", scenario_tag="@scenario1") as tracker:
            tracker.set_crawler_type(False)
            # ... process scenario ...
            # Analytics saved automatically on exit
    
    Args:
        jira_id: The JIRA ticket ID
        selected_domain: The domain/module being tested (optional)
        total_scenarios: Total number of scenarios in feature file
        scenario_tag: Scenario tag (e.g., "@scenario1")
        scenario_description: Scenario description (e.g., "Test CSRB1")
    
    Returns:
        AnalyticsTracker instance
    """
    return AnalyticsTracker(jira_id, selected_domain, total_scenarios,
                           scenario_tag, scenario_description)
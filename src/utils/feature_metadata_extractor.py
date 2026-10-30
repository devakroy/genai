"""
Feature Metadata Extractor

This module provides utilities to extract metadata from Gherkin feature files including:
- Feature tags and feature name
- Scenario tags and scenario names
- Step vs locator mappings
- API steps from uploaded JSON files
"""

import json
import re
import logging
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


class FeatureMetadataExtractor:
    """Extract metadata from Gherkin feature files."""
    
    @staticmethod
    def extract_feature_tags(feature_content: str) -> List[str]:
        """
        Extract feature-level tags from feature file content.
        
        Tags appear before the Feature: line and start with @
        Example: @payment-checkout @critical
        
        Args:
            feature_content: The raw feature file content
            
        Returns:
            List of feature tags (with @ prefix)
        """
        try:
            tags = []
            lines = feature_content.split('\n')
            
            for i, line in enumerate(lines):
                stripped = line.strip()
                
                # Stop when we reach Feature: line
                if stripped.startswith('Feature:'):
                    break
                
                # Extract tags (lines starting with @)
                if stripped.startswith('@'):
                    # Split multiple tags on same line - keep @ prefix
                    tag_matches = re.findall(r'(@\w+(?:-\w+)*)', stripped)
                    tags.extend(tag_matches)
            
            logger.info(f"✅ Extracted {len(tags)} feature tags: {tags}")
            return tags
        except Exception as e:
            logger.error(f"❌ Error extracting feature tags: {e}")
            return []
    
    @staticmethod
    def extract_feature_name(feature_content: str) -> Optional[str]:
        """
        Extract feature name from feature file content.
        
        Feature name appears after "Feature:" keyword
        Example: Feature: User Login Functionality
        
        Args:
            feature_content: The raw feature file content
            
        Returns:
            Feature name or None if not found
        """
        try:
            lines = feature_content.split('\n')
            
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('Feature:'):
                    # Extract text after "Feature:"
                    feature_name = stripped.replace('Feature:', '').strip()
                    logger.info(f"✅ Extracted feature name: {feature_name}")
                    return feature_name
            
            logger.warning("⚠️  No feature name found in content")
            return None
        except Exception as e:
            logger.error(f"❌ Error extracting feature name: {e}")
            return None
    
    @staticmethod
    def extract_scenarios_metadata(feature_content: str) -> List[Dict[str, Any]]:
        """
        Extract scenario-level metadata (tags and names) from feature file.
        
        Args:
            feature_content: The raw feature file content
            
        Returns:
            List of dicts with 'tags' and 'name' for each scenario
        """
        try:
            scenarios = []
            lines = feature_content.split('\n')
            current_tags = []
            found_feature = False
            
            for i, line in enumerate(lines):
                stripped = line.strip()
                
                # Mark when we find the Feature: line
                if stripped.startswith('Feature:'):
                    found_feature = True
                    current_tags = []  # Reset tags after feature line
                    continue
                
                # Only collect tags that appear AFTER the Feature: line (scenario tags)
                if found_feature:
                    if stripped.startswith('@'):
                        # Extract tags with @ prefix
                        tag_matches = re.findall(r'(@\w+(?:-\w+)*)', stripped)
                        current_tags.extend(tag_matches)
                    
                    # Process scenario
                    elif stripped.startswith('Scenario:') or stripped.startswith('Scenario Outline:'):
                        # Extract scenario name
                        scenario_name = re.sub(r'^Scenario(?:\s+Outline)?:\s*', '', stripped)
                        
                        scenarios.append({
                            'name': scenario_name,
                            'tags': current_tags.copy()
                        })
                        
                        # Reset tags for next scenario
                        current_tags = []
            
            logger.info(f"✅ Extracted {len(scenarios)} scenarios with metadata")
            for idx, sc in enumerate(scenarios, 1):
                logger.info(f"   Scenario {idx}: {sc['name']} | Tags: {sc['tags']}")
            
            return scenarios
        except Exception as e:
            logger.error(f"❌ Error extracting scenarios metadata: {e}")
            return []
    
    @staticmethod
    def build_step_locator_map(steps_with_locators: List[Dict[str, str]]) -> str:
        """
        Build a step vs locator map from processed steps.
        
        Args:
            steps_with_locators: List of dicts with 'step' and 'locator' keys
            
        Returns:
            JSON string representing the step-locator mapping
        """
        try:
            if not steps_with_locators:
                logger.warning("⚠️  No steps provided for locator map")
                return json.dumps({"steps": []}, ensure_ascii=False)
            
            # Transform to expected format
            steps_list = []
            for item in steps_with_locators:
                step_name = item.get('step') or item.get('stepname') or ''
                locator = item.get('locator', 'NA')
                
                if step_name:  # Only add if step name exists
                    steps_list.append({
                        'stepname': step_name,
                        'locator': locator
                    })
            
            result = {
                'steps': steps_list
            }
            
            json_str = json.dumps(result, ensure_ascii=False, indent=2)
            logger.info(f"✅ Built step-locator map with {len(steps_list)} steps")
            return json_str
        except Exception as e:
            logger.error(f"❌ Error building step-locator map: {e}")
            return json.dumps({"steps": []}, ensure_ascii=False)
    
    @staticmethod
    def extract_api_steps_from_json_files(json_files_content: Dict[str, str]) -> str:
        """
        Extract and combine API steps from uploaded JSON files.
        
        Args:
            json_files_content: Dict mapping filename -> file content (as string)
            
        Returns:
            JSON string with all API steps combined in api_config wrapper
        """
        try:
            if not json_files_content:
                logger.warning("⚠️  No JSON files provided for API steps")
                return json.dumps({"api_config": []}, ensure_ascii=False)
            
            api_steps_list = []
            
            for filename, content in json_files_content.items():
                try:
                    # Parse JSON content
                    if isinstance(content, str):
                        json_data = json.loads(content)
                    else:
                        json_data = content
                    
                    # Extract filename without extension as the name
                    name = filename.rsplit('.', 1)[0] if '.' in filename else filename
                    
                    # Handle both single object and array formats
                    if isinstance(json_data, list):
                        # If it's already a list, add each item with name
                        for item in json_data:
                            if isinstance(item, dict):
                                # Create new dict with name first, then other fields
                                item_copy = {"name": name}
                                item_copy.update(item)
                                api_steps_list.append(item_copy)
                    elif isinstance(json_data, dict):
                        # Single object - create new dict with name first
                        json_data_copy = {"name": name}
                        json_data_copy.update(json_data)
                        api_steps_list.append(json_data_copy)
                    
                    logger.info(f"✅ Processed API file: {filename}")
                except json.JSONDecodeError as je:
                    logger.warning(f"⚠️  Failed to parse JSON file {filename}: {je}")
                except Exception as e:
                    logger.warning(f"⚠️  Error processing file {filename}: {e}")
            
            # Wrap API steps in api_config object
            result = {"api_config": api_steps_list}
            result_json = json.dumps(result, ensure_ascii=False, indent=2)
            logger.info(f"✅ Extracted API steps from {len(json_files_content)} files")
            return result_json
        except Exception as e:
            logger.error(f"❌ Error extracting API steps: {e}")
            return json.dumps({"api_config": []}, ensure_ascii=False)


# Convenience functions
def extract_feature_tags(feature_content: str) -> List[str]:
    """Extract feature tags from feature content."""
    return FeatureMetadataExtractor.extract_feature_tags(feature_content)


def extract_feature_name(feature_content: str) -> Optional[str]:
    """Extract feature name from feature content."""
    return FeatureMetadataExtractor.extract_feature_name(feature_content)


def extract_scenarios_metadata(feature_content: str) -> List[Dict[str, Any]]:
    """Extract scenario metadata from feature content."""
    return FeatureMetadataExtractor.extract_scenarios_metadata(feature_content)


def build_step_locator_map(steps_with_locators: List[Dict[str, str]]) -> str:
    """Build step-locator map JSON."""
    return FeatureMetadataExtractor.build_step_locator_map(steps_with_locators)


def extract_api_steps_from_json_files(json_files_content: Dict[str, str]) -> str:
    """Extract API steps from JSON files."""
    return FeatureMetadataExtractor.extract_api_steps_from_json_files(json_files_content)

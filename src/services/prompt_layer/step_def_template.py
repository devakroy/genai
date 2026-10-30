import logging
import re
import os
import pandas as pd
from services.llm_layer.LLM import LLM

# Initialize logger
logger = logging.getLogger(__name__)


def process_typescript_code(code):
    lines = code.splitlines()
    result_lines = []

    for line in lines:
        original_line = line
        # Look for lines that have Given, When, or Then
        if any(step_type in line for step_type in ["Given('", "When('", "Then('"]):
            # Find where the step text starts and ends
            start_idx = line.find("('") + 2
            end_idx = line.find("'", start_idx)

            if start_idx != -1 and end_idx != -1:
                step_text = line[start_idx:end_idx]

                # Only modify if it contains special characters
                if any(char in step_text for char in ['/']):
                    modified_text = ""
                    i = 0
                    while i < len(step_text):
                        # Skip already escaped characters
                        if step_text[i] == '\\' and i < len(step_text) - 1 and step_text[i + 1] in ['/']:
                            modified_text += step_text[i:i + 2]
                            i += 2
                        # Escape special characters with double backslashes
                        elif step_text[i] in ['/']:
                            modified_text += '\\\\' + step_text[i]
                            i += 1
                        # Keep other characters as is
                        else:
                            modified_text += step_text[i]
                            i += 1

                    # Replace only the exact step text portion
                    prefix = line[:start_idx]
                    suffix = line[end_idx:]
                    line = prefix + modified_text + suffix

        result_lines.append(line)

    return '\n'.join(result_lines) 


class StepDefTemplate:
    def __init__(self):
        # Configuration for chunking strategy - aggressive settings for large feature files
        self.max_input_tokens = int(
            os.getenv('STEPDEF_MAX_INPUT_TOKENS', 2500))  # Aggressive limit to prevent LLM token issues
        self.max_steps_per_chunk = int(os.getenv('STEPDEF_MAX_STEPS_PER_CHUNK', 10))  # Small chunks for large files
        self.enable_chunking = os.getenv('STEPDEF_ENABLE_CHUNKING', 'true').lower() == 'true'

        logger.info(f"StepDefTemplate initialized with aggressive chunking for large files: "
                    f"max_tokens={self.max_input_tokens}, "
                    f"max_steps={self.max_steps_per_chunk}, "
                    f"chunking_enabled={self.enable_chunking}")
        pass

    def _estimate_token_count(self, text):
        """Estimate token count with conservative multiplier for complex operations"""
        base_tokens = len(str(text)) // 3  # More conservative than //4

        # Add extra tokens for complex operations that generate more code
        text_str = str(text)
        complexity_bonus = 0

        # Count complex operations that typically generate longer code
        complex_patterns = ['switch_to_iframe', 'frameLocator', 'xpath=', 'getByTestId', 'contentFrame']
        for pattern in complex_patterns:
            complexity_bonus += text_str.count(pattern) * 10  # 10 extra tokens per complex operation

        return base_tokens + complexity_bonus

    def _validate_response_completeness(self, response, df_chunk):
        """Validate that LLM response is complete, especially for iframe operations"""
        try:
            # Basic checks for response completeness
            if not response or len(response.strip()) < 10:
                logger.warning("Response too short - likely truncated")
                return False

            # Enhanced iframe operation detection and validation
            iframe_indicators = ['switch_to_iframe', 'frameLocator', 'contentFrame', 'iframe[name', 'enactment-iframe']
            iframe_count = sum(str(df_chunk).count(indicator) for indicator in iframe_indicators)
            if iframe_count > 0:
                # For iframe operations, ensure the response has proper structure
                response_iframe_count = sum(response.count(indicator) for indicator in ['frameLocator', 'contentFrame'])
                if iframe_count > 0 and response_iframe_count == 0:
                    logger.warning(f"Missing iframe handling in response - expected {iframe_count} iframe operations")
                    return False

                # Check for CSRB specific patterns
                csrb_steps = str(df_chunk).count('CSRB')
                if csrb_steps > 0:
                    # CSRB steps should have nested iframe handling
                    if csrb_steps > response.count('frameLocator'):
                        logger.warning(f"CSRB steps ({csrb_steps}) vs frameLocator count mismatch")
                        return False

            # Enhanced truncation detection
            response_lines = response.strip().split('\n')

            # Check for incomplete TypeScript syntax
            open_braces = response.count('{')
            close_braces = response.count('}')
            if abs(open_braces - close_braces) > 1:  # Allow minor mismatch
                logger.warning(f"Unbalanced braces: {open_braces} open, {close_braces} close")
                return False

            # Check for abrupt ending (common in truncation)
            last_lines = response_lines[-5:] if len(response_lines) >= 5 else response_lines
            incomplete_patterns = ['const', 'function', 'async', 'await', 'if', 'for', 'while']

            for line in last_lines:
                stripped_line = line.strip()
                if stripped_line:
                    # Check for incomplete statements
                    if any(stripped_line.startswith(pattern) for pattern in incomplete_patterns):
                        if not stripped_line.endswith((';', '}', ')', '`', ',')):
                            logger.warning(f"Potentially incomplete statement: {stripped_line[-100:]}")
                            return False

                    # Check for incomplete variable declarations
                    if stripped_line.startswith('const ') and '=' not in stripped_line:
                        logger.warning(f"Incomplete const declaration: {stripped_line}")
                        return False

            # Validate that all expected steps have corresponding code
            chunk_str = str(df_chunk)
            step_count = chunk_str.count('Given(') + chunk_str.count('When(') + chunk_str.count('Then(')
            response_step_count = response.count('Given(') + response.count('When(') + response.count('Then(')

            if response_step_count < step_count:
                logger.warning(f"Missing steps in response: expected {step_count}, got {response_step_count}")
                return False

            return True

        except Exception as e:
            logger.warning(f"Error validating response completeness: {e}")
            return True  # If validation fails, assume response is OK to avoid false positives

    def _chunk_dataframe_by_steps(self, df, df_inter, max_steps=None):
        """Split DataFrame into chunks based on BDD steps to manage token limits while preserving order"""
        if max_steps is None:
            max_steps = self.max_steps_per_chunk

        # Handle string inputs (fallback when emergency chunking with non-DataFrame data)
        # df can be a string (field_details) but df_inter should be a DataFrame for proper chunking
        if not hasattr(df_inter, 'columns'):
            logger.warning("Received string inputs instead of DataFrames - using emergency text chunking")
            return self._chunk_text_data(df, df_inter, max_steps)

        # Preserve original order of steps from the feature file
        # Use df_inter since df might be a string (field_details)
        if hasattr(df_inter, 'columns') and 'original_bdd_step' in df_inter.columns:
            # Get unique steps while preserving order
            unique_steps = []
            seen_steps = set()
            for step in df_inter['original_bdd_step']:
                if step not in seen_steps:
                    unique_steps.append(step)
                    seen_steps.add(step)
        else:
            unique_steps = list(range(len(df_inter)))

        chunks = []
        current_chunk_df = []
        current_chunk_df_inter = []
        current_step_count = 0

        for step in unique_steps:
            if hasattr(df_inter, 'columns') and 'original_bdd_step' in df_inter.columns:
                # Use df_inter as the primary source for chunking since df might be a string
                step_df_inter = df_inter[df_inter['original_bdd_step'] == step]
                # For df (field_details string), we'll create a simple representation but preserve original content
                step_df = pd.DataFrame({'step_data': [str(step)]})  # Simple representation for compatibility
            else:
                # Fallback: use positional indexing
                step_df_inter = df_inter.iloc[current_step_count:current_step_count + 1] if current_step_count < len(
                    df_inter) else pd.DataFrame()
                step_df = pd.DataFrame({'step_data': [f'step_{current_step_count}']})

            # Simple chunking decision based on step count and token limits
            projected_content = pd.concat(current_chunk_df + [step_df]) if current_chunk_df else step_df
            projected_tokens = self._estimate_token_count(projected_content.to_string())

            # Check if adding this step would exceed limits
            if (current_step_count >= max_steps or
                    projected_tokens > self.max_input_tokens):

                # Save current chunk
                if current_chunk_df:
                    chunks.append({
                        'df': pd.concat(current_chunk_df, ignore_index=True),
                        'df_inter': pd.concat(current_chunk_df_inter,
                                              ignore_index=True) if current_chunk_df_inter else pd.DataFrame(),
                        'step_count': current_step_count,
                        'original_df': df  # Preserve original df content (string or DataFrame)
                    })

                # Start new chunk
                current_chunk_df = [step_df] if not step_df.empty else []
                current_chunk_df_inter = [step_df_inter] if not step_df_inter.empty else []
                current_step_count = 1
            else:
                # Add to current chunk
                if not step_df.empty:
                    current_chunk_df.append(step_df)
                if not step_df_inter.empty:
                    current_chunk_df_inter.append(step_df_inter)
                current_step_count += 1

        # Add final chunk
        if current_chunk_df:
            chunks.append({
                'df': pd.concat(current_chunk_df, ignore_index=True),
                'df_inter': pd.concat(current_chunk_df_inter,
                                      ignore_index=True) if current_chunk_df_inter else pd.DataFrame(),
                'step_count': current_step_count,
                'original_df': df  # Preserve original df content (string or DataFrame)
            })

        return chunks

    def _chunk_text_data(self, data_text, data_inter_text="", max_steps=10):
        """Emergency chunking for text data when DataFrames are not available"""
        try:
            # Extract steps from text data
            lines = str(data_text).split('\n')
            steps = []

            for line in lines:
                line = line.strip()
                if line and any(line.startswith(keyword) for keyword in ['Given', 'When', 'Then', 'And', 'But']):
                    steps.append(line)

            if not steps:
                # Fallback: treat as single chunk
                logger.warning("No BDD steps found in text data - treating as single chunk")
                return [{'text_data': str(data_text), 'text_inter': str(data_inter_text), 'step_count': 1}]

            # Create chunks based on step count
            chunks = []
            for i in range(0, len(steps), max_steps):
                chunk_steps = steps[i:i + max_steps]
                chunk_data = '\n'.join(chunk_steps)
                chunks.append({
                    'text_data': chunk_data,
                    'text_inter': str(data_inter_text),  # Keep original inter data
                    'step_count': len(chunk_steps),
                    'steps': chunk_steps
                })

            logger.info(f"Created {len(chunks)} text chunks from {len(steps)} steps")
            return chunks

        except Exception as e:
            logger.error(f"Error in emergency text chunking: {str(e)}")
            # Ultimate fallback: single chunk
            return [{'text_data': str(data_text), 'text_inter': str(data_inter_text), 'step_count': 1}]

    def _should_use_chunking(self, data, df, df_inter):
        """Determine if chunking is needed for large feature files"""
        # First check if chunking is enabled
        if not self.enable_chunking:
            logger.info("Chunking disabled via configuration")
            return False

        # For compatibility: df_inter should be a DataFrame, df can be string or DataFrame
        if not hasattr(df_inter, 'columns'):
            logger.warning("Chunking disabled: df_inter is not a DataFrame")
            return False

        # Calculate total content size
        df_text = str(df.to_string()) if hasattr(df, 'to_string') else str(df)
        df_inter_text = str(df_inter.to_string()) if hasattr(df_inter, 'to_string') else str(df_inter)
        total_text = str(data) + df_text + df_inter_text

        # Enhanced token estimation
        estimated_tokens = self._estimate_token_count(total_text)

        # Count steps - prefer df_inter (DataFrame) over df (could be string)
        if hasattr(df_inter, 'columns') and 'original_bdd_step' in df_inter.columns:
            step_count = len(df_inter['original_bdd_step'].unique())
        elif hasattr(df, 'columns') and 'original_bdd_step' in df.columns:
            step_count = len(df['original_bdd_step'].unique())
        elif hasattr(df_inter, '__len__'):
            step_count = len(df_inter)
        elif hasattr(df, '__len__'):
            step_count = len(df)
        else:
            # Fallback: count lines in string representation
            step_count = len(str(df).split('\n'))

        # Simple chunking triggers based on size
        token_trigger = estimated_tokens > self.max_input_tokens
        step_trigger = step_count > self.max_steps_per_chunk
        large_file_trigger = len(total_text) > 8000  # Large feature file content

        should_chunk = token_trigger or step_trigger or large_file_trigger

        # Logging for debugging
        logger.info(f"Chunking analysis: tokens={estimated_tokens}/{self.max_input_tokens}, "
                    f"steps={step_count}/{self.max_steps_per_chunk}, "
                    f"content_size={len(total_text)}")

        if should_chunk:
            triggers = []
            if token_trigger: triggers.append(f"tokens({estimated_tokens}>{self.max_input_tokens})")
            if step_trigger: triggers.append(f"steps({step_count}>{self.max_steps_per_chunk})")
            if large_file_trigger: triggers.append(f"large_file({len(total_text)}>8000)")

            logger.info(f"🔄 CHUNKING TRIGGERED by: {', '.join(triggers)}")
        else:
            logger.info("No chunking needed - feature file within limits")

        return should_chunk

    def _extract_scenario_steps_from_data(self, data):
        """Extract individual BDD steps from feature data for targeted processing"""
        lines = data.splitlines()
        steps = []
        current_step = ""

        for line in lines:
            line = line.strip()
            if re.match(r'^\s*(Given|When|Then|And|But)\s+', line, re.IGNORECASE):
                if current_step:
                    steps.append(current_step)
                current_step = line
            elif line and current_step:
                current_step += " " + line

        if current_step:
            steps.append(current_step)

        return steps

    def _extract_imports_and_setup(self, code):
        """Extract import statements and setup code from generated code"""
        lines = code.splitlines()
        imports = []
        setup_lines = []
        code_lines = []

        in_imports = True

        for line in lines:
            line_stripped = line.strip()

            # Collect import statements
            if line_stripped.startswith('import ') or line_stripped.startswith('from '):
                imports.append(line)
            # Collect setup statements
            elif line_stripped.startswith('setDefaultTimeout') or 'setDefaultTimeout' in line:
                setup_lines.append(line)
            # Empty lines in import section
            elif in_imports and not line_stripped:
                continue
            # Start of actual code
            else:
                in_imports = False
                if line_stripped:  # Skip empty lines at start of code
                    code_lines.append(line)
                elif code_lines:  # Keep empty lines within code
                    code_lines.append(line)

        return imports, setup_lines, code_lines

    def _deduplicate_imports(self, all_imports):
        """Remove duplicate import statements while preserving order"""
        seen = set()
        unique_imports = []

        for imp in all_imports:
            imp_clean = imp.strip()
            if imp_clean and imp_clean not in seen:
                seen.add(imp_clean)
                unique_imports.append(imp)

        return unique_imports

    def _extract_step_signatures(self, code):
        """Extract step definition signatures from code"""
        import re
        signatures = set()
        lines = code.split('\n')
        
        for line in lines:
            # Match step definitions: Given('...'), When('...'), Then('...')
            step_match = re.match(r'\s*(Given|When|Then|And|But)\(\s*[\'"](.*?)[\'"]\s*,', line)
            if step_match:
                step_type = step_match.group(1)
                step_text = step_match.group(2)
                # Normalize step text for comparison
                normalized_text = step_text.strip()
                signature = f"{step_type}('{normalized_text}'"
                signatures.add(signature)
        
        return signatures

    def _deduplicate_step_definitions(self, code_sections):
        """Remove duplicate step definitions based on step signature while preserving order"""
        import re
        seen_steps = set()
        unique_sections = []
        
        for section in code_sections:
            if not section or not section.strip():
                continue
                
            lines = section.split('\n')
            filtered_lines = []
            skip_until_next_step = False
            current_step_signature = None
            
            for line in lines:
                # Check if line is a step definition
                step_match = re.match(r'\s*(Given|When|Then|And|But)\(\s*[\'"](.*?)[\'"]\s*,', line)
                if step_match:
                    step_type = step_match.group(1)
                    step_text = step_match.group(2)
                    
                    # Normalize step text to detect pattern equivalence
                    normalized_text = self._normalize_step_pattern(step_text.strip())
                    current_step_signature = f"{step_type}('{normalized_text}'"
                    
                    if current_step_signature in seen_steps:
                        skip_until_next_step = True
                        logger.info(f"🔄 DEDUP: Skipping duplicate step: {current_step_signature}")
                        logger.info(f"🔄 DEDUP: Original text: '{step_text}' -> Normalized: '{normalized_text}'")
                        continue
                    else:
                        seen_steps.add(current_step_signature)
                        skip_until_next_step = False
                        logger.info(f"✅ DEDUP: Added unique step: {current_step_signature}")
                        logger.info(f"✅ DEDUP: Original text: '{step_text}' -> Normalized: '{normalized_text}'")
                elif line.strip() and (line.strip().startswith('Given(') or 
                                     line.strip().startswith('When(') or 
                                     line.strip().startswith('Then(') or
                                     line.strip().startswith('And(') or
                                     line.strip().startswith('But(')):
                    # New step found, stop skipping previous duplicate
                    skip_until_next_step = False
                    # Check if this new step is also a duplicate
                    step_match = re.match(r'\s*(Given|When|Then|And|But)\(\s*[\'"](.*?)[\'"]\s*,', line)
                    if step_match:
                        step_type = step_match.group(1)
                        step_text = step_match.group(2)
                        normalized_text = step_text.strip()
                        current_step_signature = f"{step_type}('{normalized_text}'"
                        
                        if current_step_signature in seen_steps:
                            skip_until_next_step = True
                            logger.info(f"🔄 DEDUP: Skipping duplicate step: {current_step_signature}")
                            continue
                        else:
                            seen_steps.add(current_step_signature)
                            logger.debug(f"✅ DEDUP: Added unique step: {current_step_signature}")
                
                if not skip_until_next_step:
                    filtered_lines.append(line)
                    
            if filtered_lines:
                cleaned_section = '\n'.join(filtered_lines).strip()
                if cleaned_section:
                    unique_sections.append(cleaned_section)
        
        logger.info(f"🔄 DEDUP: Processed {len(code_sections)} sections, kept {len(unique_sections)} unique sections")
        return unique_sections

    def _normalize_step_pattern(self, step_text):
        """Normalize step patterns to detect equivalent steps (parameterized vs hardcoded)"""
        import re
        
        # For navigation steps, be aggressive about URL normalization
        if 'navigates to' in step_text.lower() or 'navigate to' in step_text.lower() or 'goto' in step_text.lower():
            # For navigation steps, replace any quoted URL with {string}
            normalizations = [
                # Full URLs in quotes - handle both escaped and unescaped URLs
                (r'"https?:\\?/\\?/[^"]*"', '{string}'),  # Matches "https:\\/\\/" or "https://" and replaces with {string}
                (r'"https?://[^"]*"', '{string}'),       # Standard URLs -> {string}
                # Any other quoted content that looks like URLs
                (r'"[^"]*\.(?:com|net|org)[^"]*"', '{string}'),
            ]
        elif 'clicks on' in step_text.lower() or 'click' in step_text.lower():
            # For click steps, preserve button names - only normalize URLs and emails
            normalizations = [
                (r'https?://[^\s"\']+', '{string}'),
                (r'[\w\.-]+@[\w\.-]+\.\w+', '{string}'),
            ]
        else:
            # For other steps, use conservative approach
            normalizations = [
                # URLs - replace any URL with {string} pattern
                (r'https?://[^\s"\']+', '{string}'),
                # Email patterns  
                (r'[\w\.-]+@[\w\.-]+\.\w+', '{string}'),
                # Numbers (but be careful not to normalize version numbers in button names)
                (r'\b\d{3,}\b', '{number}'),  # Only normalize longer numbers
                # Common test data patterns like "John Doe" but not field names
                # Skip this pattern entirely for "enters" steps to preserve all field names
                # Only apply to non-enters steps
            ]
            
            # Only add name normalization pattern for non-enters steps
            if 'enters' not in step_text.lower():
                normalizations.append((r'\b([A-Z][a-z]+ [A-Z][a-z]+)\b(?=.*(?:as|into|with))', '{string}'))
        
        normalized = step_text
        for pattern, replacement in normalizations:
            normalized = re.sub(pattern, replacement, normalized)
        
        # Clean up multiple consecutive placeholders
        normalized = re.sub(r'\{string\}\s+\{string\}', '{string}', normalized)
        
        return normalized.strip()

    def _filter_duplicate_steps_from_new_code(self, new_code, existing_signatures):
        """Filter out step definitions from new code that already exist
        Special handling for iframe-based steps to prevent over-filtering."""
        import re
        if not new_code or not new_code.strip():
            return new_code
        
        # Iframe-specific steps that should be preserved even if similar exists
        iframe_keywords = ['frameLocator', 'contentFrame', 'enactment-iframe', 
                          'Hi, Playwright! Account', 'Autoship', 'Manage Autoship',
                          'Change beside Payment Method', 'Select" button']
            
        lines = new_code.split('\n')
        filtered_lines = []
        skip_until_next_step = False
        
        for line in lines:
            # Check if line is a step definition
            step_match = re.match(r'\s*(Given|When|Then|And|But)\(\s*[\'"](.*?)[\'"]\s*,', line)
            if step_match:
                step_type = step_match.group(1)
                step_text = step_match.group(2)
                normalized_text = step_text.strip()
                signature = f"{step_type}('{normalized_text}'"
                
                if signature in existing_signatures:
                    skip_until_next_step = True
                    logger.info(f"🔄 FILTER: Skipping existing step: {signature}")
                    continue
                else:
                    skip_until_next_step = False
                    logger.debug(f"✅ FILTER: Keeping new step: {signature}")
            elif line.strip() and (line.strip().startswith('Given(') or 
                                 line.strip().startswith('When(') or 
                                 line.strip().startswith('Then(') or
                                 line.strip().startswith('And(') or
                                 line.strip().startswith('But(')):
                # New step found
                skip_until_next_step = False
                step_match = re.match(r'\s*(Given|When|Then|And|But)\(\s*[\'"](.*?)[\'"]\s*,', line)
                if step_match:
                    step_type = step_match.group(1)
                    step_text = step_match.group(2)
                    normalized_text = step_text.strip()
                    signature = f"{step_type}('{normalized_text}'"
                    
                    # Check if this is an iframe-related step that should be preserved
                    is_iframe_step = any(keyword in normalized_text for keyword in iframe_keywords)
                    
                    if signature in existing_signatures and not is_iframe_step:
                        skip_until_next_step = True
                        logger.info(f"🔄 FILTER: Skipping existing step: {signature}")
                        continue
                    elif is_iframe_step:
                        logger.info(f"🎯 IFRAME: Preserved iframe-specific step: {signature}")
                    else:
                        logger.debug(f"✅ FILTER: Keeping new step: {signature}")
            
            if not skip_until_next_step:
                filtered_lines.append(line)
        
        return '\n'.join(filtered_lines)

    def _convert_dynamic_placeholders(self, step_text):
        """Convert dynamic placeholders like {accountResponse.email} to {string} parameters"""
        import re

        # Pattern to match dynamic placeholders like {accountResponse.email}, {variable.field}, etc.
        # Use negative lookbehind/lookahead to avoid matching placeholders already inside double braces
        # Also exclude {string} which is already the target format
        placeholder_pattern = r'(?<!\{)\{(?!string\})[a-zA-Z][a-zA-Z0-9_.]*\}(?!\})'

        # Find all dynamic placeholders
        placeholders = re.findall(placeholder_pattern, step_text)

        # Convert step text by replacing dynamic placeholders with {string}
        converted_step = step_text
        for placeholder in placeholders:
            # Replace the first occurrence of this specific placeholder with {string}
            converted_step = converted_step.replace(placeholder, '{string}', 1)

        return converted_step, placeholders

    def _extract_field_name_from_step(self, step_text):
        """Extract field name from steps like 'user enters "Field Name" as {string}'"""
        import re
        # Pattern to match quoted field names
        field_pattern = r'"([^"]+)"'
        match = re.search(field_pattern, step_text)
        return match.group(1) if match else None

    def _clean_chunk_for_append(self, chunk_code, is_first_chunk):
        """Clean chunk code to avoid duplicate imports and declarations when appending"""
        if is_first_chunk:
            return chunk_code  # Keep everything for first chunk

        lines = chunk_code.splitlines()
        cleaned_lines = []

        # Skip import statements and setDefaultTimeout for non-first chunks
        skip_patterns = [
            'import ',
            'setDefaultTimeout',
            '// filepath:',
            '// Initialize'
        ]

        for line in lines:
            line_stripped = line.strip()

            # Skip lines that match patterns (except for first chunk)
            if any(line_stripped.startswith(pattern) for pattern in skip_patterns):
                continue

            # Skip empty lines at the beginning
            if not cleaned_lines and not line_stripped:
                continue

            cleaned_lines.append(line)

        return '\n'.join(cleaned_lines)

    def _process_with_chunking(self, data, df, df_inter, i, flag, jira_id, current_script, model,
                               merged_prompt, template_stepdef, example, testdata,
                               paypal_code, post_api_code, get_api_code, cookie_code, mock_api_code, exclude_paypal_code=False):
        """Process large feature files using chunking strategy"""
        logger.info("Starting chunked processing for large feature file")

        # Check if we need emergency text chunking (when df is string or very large)
        emergency_text_chunking = False
        total_content_size = len(str(data)) + len(str(df)) + len(str(df_inter))

        # Only use emergency text chunking when DataFrames are not available (df is string)
        # Avoid emergency chunking for large scenarios with valid DataFrames to preserve iframe locators
        if not hasattr(df_inter, 'columns'):
            emergency_text_chunking = True
            logger.warning(f"🚨 Using emergency text chunking for non-DataFrame input (size: {total_content_size})")
        elif total_content_size > 25000:
            emergency_text_chunking = True
            logger.warning(f"🚨 Using emergency text chunking for very large content (size: {total_content_size})")

            # Try DataFrame chunking first if df_inter is a DataFrame to preserve iframe locators
            if hasattr(df_inter, 'columns') and 'playwrightLocator' in df_inter.columns:
                logger.info("Using DataFrame chunking despite large size to preserve iframe locators")
                chunks = self._chunk_dataframe_by_steps(df, df_inter)
                logger.info(f"DataFrame chunking: Split into {len(chunks)} chunks for processing")
            else:
                # Convert everything to text for emergency chunking only when DataFrame not available
                text_data = str(data) + "\n" + str(df) + "\n" + str(df_inter)
                chunks = self._chunk_text_data(text_data)
                logger.info(f"Emergency text chunking: Split into {len(chunks)} text chunks for processing")
        else:
            # Normal DataFrame chunking
            chunks = self._chunk_dataframe_by_steps(df, df_inter)
            logger.info(f"DataFrame chunking: Split into {len(chunks)} chunks for processing")

        # Collect all imports and step definitions separately
        all_imports = []
        all_setup_lines = []
        all_code_sections = []

        successful_chunks = 0
        failed_chunks = 0
        failed_chunk_data = []  # Store failed chunks for retry with different strategy

        for chunk_idx, chunk in enumerate(chunks):
            if emergency_text_chunking:
                # Handle text chunks
                logger.info(f"Processing text chunk {chunk_idx + 1}/{len(chunks)} with {chunk['step_count']} steps")

                # For text chunks, create a simple DataFrame representation for LLM processing
                chunk_text = chunk['text_data']
                chunk_testdata = ""  # No specific test data for text chunks
                df_to_send_str = chunk_text
                chunk_df = None  # No DataFrame for text chunks
                chunk_df_inter = None
            else:
                # Handle DataFrame chunks
                logger.info(
                    f"Processing DataFrame chunk {chunk_idx + 1}/{len(chunks)} with {chunk['step_count']} steps")

                chunk_df = chunk['df']
                chunk_df_inter = chunk['df_inter']
                
                # Use original df content if available (preserves field_details string)
                original_df = chunk.get('original_df')
                if original_df is not None and isinstance(original_df, str):
                    # Use original field_details string but ensure iframe locators are preserved
                    df_to_send_str = original_df
                    
                    # Check if df_inter has iframe locators that might not be in field_details
                    if (hasattr(chunk_df_inter, 'columns') and 
                        'playwrightLocator' in chunk_df_inter.columns):
                        # Check for iframe-related data in df_inter that might be missing from field_details
                        iframe_data = chunk_df_inter[chunk_df_inter['playwrightLocator'].str.contains(
                            'iframe|frameLocator|switch_to_iframe', case=False, na=False)]
                        
                        if not iframe_data.empty:
                            # Append iframe-specific locator information to preserve it
                            df_to_send_str += "\n\nIframe Locator Information:\n"
                            df_to_send_str += self._dataframe_to_string_preserve_iframe(iframe_data)
                else:
                    # Use DataFrame representation with iframe preservation
                    df_to_send_str = self._dataframe_to_string_preserve_iframe(chunk_df)

                # Extract testdata for this chunk
                chunk_testdata = chunk_df_inter['data'].dropna().astype(str).str.cat(
                    sep=',') if not chunk_df_inter.empty else ""

                # PER-CHUNK PayPal detection to ensure iframe locators get proper PayPal template
                chunk_paypal_code = ""
                valid_paypal_objects = ["Pay with PayPal", "PayPal", "PayPal Checkout", "PayPal PayLater", "PayPal Credit"]
                
                # Debug: Log chunk data for special template detection
                logger.info(f"🔍 CHUNK DEBUG {chunk_idx + 1}: chunk_df_inter shape: {chunk_df_inter.shape if not chunk_df_inter.empty else 'EMPTY'}")
                if not chunk_df_inter.empty and 'object' in chunk_df_inter.columns:
                    chunk_objects = chunk_df_inter['object'].tolist()
                    logger.info(f"🔍 CHUNK DEBUG {chunk_idx + 1}: Objects in chunk: {chunk_objects}")
                    
                    paypal_matches = chunk_df_inter['object'].isin(valid_paypal_objects)
                    logger.info(f"🔍 CHUNK DEBUG {chunk_idx + 1}: PayPal matches: {paypal_matches.sum()}/{len(chunk_df_inter)}")
                    
                    # Check for iframe locators in this chunk
                    if 'playwrightLocator' in chunk_df_inter.columns:
                        iframe_matches = chunk_df_inter['playwrightLocator'].str.contains(
                            r'iframe|frameLocator|contentFrame|switch_to_iframe|\.frame\(', 
                            case=False, na=False, regex=True
                        )
                        logger.info(f"🔍 CHUNK DEBUG {chunk_idx + 1}: Iframe matches: {iframe_matches.sum()}/{len(chunk_df_inter)}")
                    
                    if 'action' in chunk_df_inter.columns:
                        click_matches = chunk_df_inter['action'].astype(str).str.contains("click", case=True, regex=True)
                        logger.info(f"🔍 CHUNK DEBUG {chunk_idx + 1}: Click matches: {click_matches.sum()}/{len(chunk_df_inter)}")
                
                # PayPal-specific template detection - respect exclude_paypal_code flag
                if exclude_paypal_code:
                    chunk_paypal_code = ""
                    logger.info(f"🚫 CHUNK PAYPAL: PayPal code excluded for MCP flow in chunk {chunk_idx + 1}")
                elif (not chunk_df_inter.empty and 
                    'object' in chunk_df_inter.columns and
                    chunk_df_inter['object'].isin(valid_paypal_objects).any() and 
                    'action' in chunk_df_inter.columns and
                    chunk_df_inter['action'].astype(str).str.contains("click", case=True, regex=True).any()):
                    
                    logger.info(f"🎯 CHUNK PAYPAL: PayPal step detected in chunk {chunk_idx + 1}, applying iframe template")
                    chunk_paypal_code = paypal_code or ""
                else:
                    logger.info(f"🔍 CHUNK DEBUG {chunk_idx + 1}: PayPal conditions not met - will check for iframe content in final string")

            # Determine which prompt to use
            if flag:
                prompt_to_use = merged_prompt
            else:
                prompt_to_use = template_stepdef

            chunk_processed = False

            try:
                logger.info(f"Sending chunk {chunk_idx + 1} to LLM for processing")
                
                # Log which steps are in this chunk for debugging
                if not chunk_df_inter.empty and 'original_bdd_step' in chunk_df_inter.columns:
                    chunk_steps = chunk_df_inter['original_bdd_step'].dropna().tolist()
                    logger.info(f"📝 CHUNK {chunk_idx + 1} STEPS: {[step[:50]+'...' if len(step) > 50 else step for step in chunk_steps]}")
                else:
                    logger.info(f"📝 CHUNK {chunk_idx + 1}: No original_bdd_step column found")

                # Retry logic for LLM requests with different strategies
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        # Prepare data for LLM request
                        if emergency_text_chunking:
                            # For text chunks, use text directly
                            current_df_str = df_to_send_str
                            if attempt > 0:
                                # For text retries, try with half the content
                                lines = current_df_str.split('\n')
                                mid_point = len(lines) // 2
                                current_df_str = '\n'.join(lines[:mid_point])
                                logger.info(f"Retry {attempt}: Reducing text chunk size by half")
                        else:
                            # For DataFrame chunks, handle retries with DataFrame operations
                            if chunk_df is not None:
                                df_to_send = chunk_df
                                if attempt > 0 and len(chunk_df) > 1:
                                    # Split chunk in half for retry
                                    mid_point = len(chunk_df) // 2
                                    df_to_send = chunk_df.head(mid_point)
                                    logger.info(f"Retry {attempt}: Reducing chunk size to {len(df_to_send)} steps")
                                
                                # CRITICAL: Use iframe-preserved string if available, otherwise preserve iframe locators
                                if 'df_to_send_str' in locals() and df_to_send_str and 'iframe' in df_to_send_str.lower():
                                    logger.info("🎯 IFRAME PRESERVATION: Using pre-preserved iframe string")
                                    current_df_str = df_to_send_str
                                else:
                                    logger.info("🔧 IFRAME PRESERVATION: Creating new iframe-preserved string")
                                    current_df_str = self._dataframe_to_string_preserve_iframe(df_to_send)
                                
                                # Check if any iframe locators are present and need special template handling
                                if 'iframe' in current_df_str.lower():
                                    logger.info(f"🎯 IFRAME DEBUG: Found iframe content in df_str (length: {len(current_df_str)})")
                                    iframe_lines = [line for line in current_df_str.split('\n') if 'iframe' in line.lower()]
                                    for line in iframe_lines[:3]:  # Show first 3 iframe lines
                                        logger.info(f"🎯 IFRAME LINE: {line[:200]}...")
                                    
                                    # Check if this is PayPal iframe that needs special template
                                    if any(paypal_term in current_df_str for paypal_term in ['PayPal', 'Pay with PayPal']):
                                        logger.info(f"🎯 IFRAME DEBUG: PayPal iframe detected - applying PayPal template")
                                        chunk_paypal_code = paypal_code or ""  # Apply PayPal template
                                    else:
                                        logger.info(f"🎯 IFRAME DEBUG: Non-PayPal iframe detected - ensuring iframe locators are prominent")
                                        # For non-PayPal iframes, add emphasis in the prompt to use iframe locators
                                        current_df_str += "\n\n⚠️ IMPORTANT: This chunk contains IFRAME locators. Use the complete iframe locator syntax as provided above, including .contentFrame() or frameLocator() methods. Do NOT simplify to basic page.getByRole() selectors."
                                else:
                                    logger.warning("⚠️ IFRAME DEBUG: No iframe content found in current_df_str")
                            else:
                                # Fallback if no DataFrame available
                                current_df_str = df_to_send_str

                        output_qa_prompt_stepDef = model.send_request(prompt_to_use,
                                                                      ["df", "example", "testdata", "paypal_code",
                                                                       "post_api_code", "get_api_code", "cookie_code",
                                                                       "mock_api_code"],
                                                                      {'df': current_df_str,
                                                                       "example": example,
                                                                       "testdata": chunk_testdata,
                                                                       "paypal_code": chunk_paypal_code or "",
                                                                       "post_api_code": post_api_code or "",
                                                                       "get_api_code": get_api_code or "",
                                                                       "cookie_code": cookie_code or "",
                                                                       "mock_api_code": mock_api_code or ""
                                                                       })

                        # Validate response completeness before proceeding
                        if emergency_text_chunking:
                            validation_data = current_df_str
                        else:
                            validation_data = df_to_send if 'df_to_send' in locals() else chunk_df
                        
                        if not self._validate_response_completeness(output_qa_prompt_stepDef, validation_data):
                            logger.warning(
                                f"⚠️ LLM response appears truncated for chunk {chunk_idx + 1}, attempt {attempt + 1}")
                            if attempt < max_retries - 1:
                                logger.info("🔄 Retrying with smaller chunk size due to truncation")
                                continue  # Retry with smaller chunk
                            else:
                                logger.error("❌ Final attempt - response still truncated")
                                raise Exception("LLM response truncated after all retries")

                        # Handle remaining data for partial chunk processing
                        if attempt > 0:
                            if emergency_text_chunking:
                                # For text chunks, we don't handle remaining data the same way
                                # The retry already processed with smaller content
                                pass
                            else:
                                # For DataFrame chunks, store remaining for later processing only if this chunk partially succeeded
                                # Don't double-process if we're already handling remaining data
                                if len(chunk_df) > len(df_to_send) and not any(
                                    fc.get('original_chunk_idx') == chunk_idx and 'remaining_processed' in fc 
                                    for fc in failed_chunk_data
                                ):
                                    remaining_df = chunk_df.tail(len(chunk_df) - len(df_to_send))
                                    remaining_df_inter = chunk_df_inter.tail(
                                        len(chunk_df_inter) - len(df_to_send)) if len(chunk_df_inter) > len(
                                        df_to_send) else pd.DataFrame()
                                    failed_chunk_data.append({
                                        'df': remaining_df,
                                        'df_inter': remaining_df_inter,
                                        'step_count': len(remaining_df),
                                        'original_chunk_idx': chunk_idx,
                                        'remaining_processed': True
                                    })
                                    logger.info(f"🔄 RETRY: Stored remaining {len(remaining_df)} steps from chunk {chunk_idx + 1} for later processing")

                        chunk_processed = True
                        break  # Success, exit retry loop

                    except Exception as retry_e:
                        logger.warning(f"Attempt {attempt + 1} failed for chunk {chunk_idx + 1}: {str(retry_e)}")
                        if attempt == max_retries - 1:  # Last attempt
                            raise retry_e
                        import time
                        time.sleep(2 ** attempt)  # Exponential backoff

                # Clean up response
                if output_qa_prompt_stepDef.startswith("```typescript") and output_qa_prompt_stepDef.endswith("```"):
                    output_qa_prompt_stepDef = output_qa_prompt_stepDef[13:-3]  # Remove ```typescript and ```
                elif output_qa_prompt_stepDef.startswith("```") and output_qa_prompt_stepDef.endswith("```"):
                    output_qa_prompt_stepDef = output_qa_prompt_stepDef[3:-3]  # Remove ``` and ```

                processed_code = process_typescript_code(output_qa_prompt_stepDef)

                # Extract imports, setup, and code sections
                imports, setup_lines, code_lines = self._extract_imports_and_setup(processed_code)

                # Collect imports and setup from all chunks
                all_imports.extend(imports)
                all_setup_lines.extend(setup_lines)

                # Add code section (step definitions) maintaining order
                if code_lines:
                    all_code_sections.append('\n'.join(code_lines))

                successful_chunks += 1
                logger.info(f"✅ Chunk {chunk_idx + 1} processed successfully")
                
                # Validate that all expected steps were generated
                if not chunk_df_inter.empty and 'original_bdd_step' in chunk_df_inter.columns:
                    expected_steps = chunk_df_inter['original_bdd_step'].dropna().tolist()
                    generated_step_count = output_qa_prompt_stepDef.count("Then(") + output_qa_prompt_stepDef.count("When(") + output_qa_prompt_stepDef.count("Given(")
                    logger.info(f"📊 STEP COUNT: Expected {len(expected_steps)} steps, Generated {generated_step_count} step definitions")
                    
                    if generated_step_count < len(expected_steps):
                        logger.warning(f"⚠️ MISSING STEPS: Only {generated_step_count}/{len(expected_steps)} steps generated")
                        for i, step in enumerate(expected_steps):
                            logger.info(f"   Expected Step {i+1}: {step[:60]}...")

            except Exception as e:
                failed_chunks += 1
                logger.error(f"❌ Error processing chunk {chunk_idx + 1}: {str(e)}")

                # Store failed chunk for fallback processing
                failed_chunk_data.append({
                    'df': chunk_df,
                    'df_inter': chunk_df_inter,
                    'step_count': chunk['step_count'],
                    'original_chunk_idx': chunk_idx,
                    'error': str(e)
                })

                # Generate basic fallback step definitions to ensure no steps are lost
                logger.warning(f"🔧 Generating fallback step definitions for chunk {chunk_idx + 1}")
                fallback_code = self._generate_fallback_step_definitions(chunk_df, chunk_df_inter)
                if fallback_code:
                    all_code_sections.append(fallback_code)
                    logger.info(f"✅ Fallback step definitions generated for chunk {chunk_idx + 1}")

                continue

        # Process any failed chunks with simplified fallback strategy
        if failed_chunk_data:
            logger.info(f"🔄 Attempting fallback processing for {len(failed_chunk_data)} failed chunks")

            for failed_chunk in failed_chunk_data:
                if 'error' in failed_chunk:  # This was a completely failed chunk
                    continue  # Already handled with fallback above

                # Skip if this is marked as remaining data that was already processed
                if failed_chunk.get('remaining_processed'):
                    logger.info(f"📝 Skipping already processed remaining data from chunk {failed_chunk['original_chunk_idx']}")
                    continue

                # This is a remaining portion from partial processing
                try:
                    logger.info(f"Processing remaining portion of chunk {failed_chunk['original_chunk_idx']}")

                    # Use simpler prompt or basic template for remaining portions
                    simple_output = self._generate_simple_step_definitions(failed_chunk['df'], failed_chunk['df_inter'])
                    if simple_output:
                        all_code_sections.append(simple_output)
                        logger.info(f"✅ Remaining portion processed successfully")

                except Exception as fallback_e:
                    logger.error(f"❌ Fallback processing failed: {str(fallback_e)}")
                    # Generate basic step definitions as last resort
                    basic_fallback = self._generate_fallback_step_definitions(failed_chunk['df'],
                                                                              failed_chunk['df_inter'])
                    if basic_fallback:
                        all_code_sections.append(basic_fallback)

        # Assemble final script with proper organization
        final_script_parts = []

        # Add existing script if provided
        if current_script and current_script.strip():
            # Extract imports from existing script too
            existing_imports, existing_setup, existing_code = self._extract_imports_and_setup(current_script)
            all_imports.extend(existing_imports)
            all_setup_lines.extend(existing_setup)
            if existing_code:
                all_code_sections.insert(0, '\n'.join(existing_code))

        # Deduplicate and add imports at the top
        unique_imports = self._deduplicate_imports(all_imports)
        if unique_imports:
            final_script_parts.extend(unique_imports)
            final_script_parts.append('')  # Empty line after imports

        # Add setup lines (like setDefaultTimeout)
        unique_setup = self._deduplicate_imports(all_setup_lines)  # Reuse dedup logic
        if unique_setup:
            final_script_parts.extend(unique_setup)
            final_script_parts.append('')  # Empty line after setup

        # Apply deduplication to code sections before assembly
        unique_code_sections = self._deduplicate_step_definitions(all_code_sections)
        logger.info(f"🔄 DEDUP: Reduced {len(all_code_sections)} sections to {len(unique_code_sections)} unique sections")
        
        # Add all unique code sections in the order they were processed (maintains feature file step order)
        for i, code_section in enumerate(unique_code_sections):
            if code_section.strip():
                if i > 0:
                    final_script_parts.append('')  # Empty line between sections
                final_script_parts.append(code_section)

        final_script = '\n'.join(final_script_parts)

        # Log final statistics
        logger.info(f"Chunked processing completed. "
                    f"Successful chunks: {successful_chunks}/{len(chunks)}, "
                    f"Failed chunks: {failed_chunks}/{len(chunks)}, "
                    f"Imports collected: {len(unique_imports)}, "
                    f"Generated {len(final_script.splitlines())} lines of code")

        if failed_chunks > 0:
            logger.warning(f"{failed_chunks} chunks failed during processing. "
                           f"Some step definitions may be missing from the final script.")

        return final_script

    def _generate_fallback_step_definitions(self, chunk_df, chunk_df_inter):
        """Generate basic step definitions as fallback when LLM fails"""
        fallback_steps = []

        if 'original_bdd_step' in chunk_df.columns:
            for step in chunk_df['original_bdd_step'].unique():
                # Generate basic step definition
                step_clean = step.strip()
                if step_clean.lower().startswith(('given', 'when', 'then', 'and', 'but')):
                    # Extract step type and content
                    parts = step_clean.split(' ', 1)
                    if len(parts) >= 2:
                        step_type = parts[0].capitalize()
                        step_content = parts[1]

                        # Create proper step definition with basic interaction
                        fallback_step = f'''{step_type}('{step_content}', async function(this: CustomWorld) {{
  const page = this.page!;

  // Basic interaction based on step content
  if ('{step_content}'.toLowerCase().includes('click')) {{
    const element = page.getByRole('button').first();
    await element.waitFor({{ state: 'visible', timeout: 10000 }});
    await element.click();
  }} else if ('{step_content}'.toLowerCase().includes('enter') || '{step_content}'.toLowerCase().includes('type')) {{
    const input = page.locator('input').first();
    await input.waitFor({{ state: 'visible', timeout: 10000 }});
    await input.fill('test_value');
  }} else if ('{step_content}'.toLowerCase().includes('navigate') || '{step_content}'.toLowerCase().includes('goto')) {{
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
  }} else if ('{step_content}'.toLowerCase().includes('verify') || '{step_content}'.toLowerCase().includes('see') || '{step_content}'.toLowerCase().includes('displayed')) {{
    const element = page.getByText('expected_text').first();
    await expect(element).toBeVisible();
  }} else {{
    // Default action
    console.log('Step executed: {step_content}');
  }}
}});'''
                        fallback_steps.append(fallback_step)

        fallback_code = '\n\n'.join(fallback_steps) if fallback_steps else ''
        logger.info(f"🔧 FALLBACK: Generated {len(fallback_steps)} basic step definitions")
        return fallback_code

    def _generate_simple_step_definitions(self, chunk_df, chunk_df_inter):
        """Generate simplified step definitions using basic patterns"""
        try:
            simple_steps = []

            if 'original_bdd_step' in chunk_df.columns:
                for step in chunk_df['original_bdd_step'].unique():
                    step_clean = step.strip()
                    if step_clean.lower().startswith(('given', 'when', 'then', 'and', 'but')):
                        parts = step_clean.split(' ', 1)
                        if len(parts) >= 2:
                            step_type = parts[0].capitalize()
                            step_content = parts[1]

                            # Use simple template patterns
                            simple_step = f'''{step_type}('{step_content}', async function(this: CustomWorld) {{
  const page = this.page!;

  // TODO: Implement specific logic for: {step_content}
  // This is a simplified step definition - please review and enhance

  try {{
    if ('{step_content}'.toLowerCase().includes('click')) {{
      const clickElement = page.locator('text={step_content.split()[-1]}').first();
      await clickElement.waitFor({{ state: 'visible', timeout: 10000 }});
      await clickElement.click();
    }} else if ('{step_content}'.toLowerCase().includes('enter')) {{
      const inputElement = page.locator('input[type="text"], input[type="email"], input[type="password"]').first();
      await inputElement.waitFor({{ state: 'visible', timeout: 10000 }});
      await inputElement.fill('sample_input');
    }} else {{
      console.log('Executing step: {step_content}');
    }}
  }} catch (error) {{
    console.error('Step execution failed:', error);
    throw error;
  }}
}});'''
                            simple_steps.append(simple_step)

            simple_code = '\n\n'.join(simple_steps) if simple_steps else ''
            logger.info(f"📝 SIMPLE: Generated {len(simple_steps)} simplified step definitions")
            return simple_code

        except Exception as e:
            logger.error(f"Simple step generation failed: {str(e)}")
            return self._generate_fallback_step_definitions(chunk_df, chunk_df_inter)

    def generate_playwright(self, data, df, df_inter, i, flag, jira_id, current_script="",
                            generate_step_definitions=True, exclude_paypal_code=False):
        """
        Generates Playwright TypeScript step definitions based on the provided input parameters and data.

        This method now supports automatic chunking for large feature files to handle token limits.

        Environment variables for configuration:
        - STEPDEF_MAX_INPUT_TOKENS: Maximum tokens per chunk (default: 6000)
        - STEPDEF_MAX_STEPS_PER_CHUNK: Maximum steps per chunk (default: 10)
        - STEPDEF_ENABLE_CHUNKING: Enable/disable chunking (default: true)

        Args:
            data (str): The BDD feature file content.
            df (DataFrame): DataFrame containing the processed data.
            df_inter (DataFrame): DataFrame containing intermediate data.
            i (int): Iteration counter.
            flag (bool): Flag for processing type.
            jira_id (str): JIRA ID for the feature.
            current_script (str): Existing script content to append to.
            generate_step_definitions (bool): Whether to generate step definitions.
            exclude_paypal_code (bool): Whether to exclude custom PayPal code (for MCP flows).

        Returns:
            str: Generated TypeScript step definition code.
        """

        paypal_code = None
        gift_card_code = None
        get_api_code = None

        valid_objects = ["Pay with PayPal", "PayPal", "PayPal Checkout", "PayPal PayLater", "PayPal Credit"]

        if not exclude_paypal_code and df_inter['object'].isin(valid_objects).any() and \
                df_inter['action'].astype(str).str.contains("click", case=True, regex=True).any():
            paypal_code = """
                    IMPORTANT: Use below code as it is for Pay with PayPal or  PayPal or PayPal Checkout or PayPal PayLater object and click action in its corresponding step definition function.

                    (When | Then )('user clicks on ("Pay with PayPal"|"PayPal"|"PayPal Checkout"|"PayPal PayLater"|"PayPal Credit")',
                      async function (this: CustomWorld, buttonName: string) {
                        const page = this.page!;
                        const creds = { email: 'ragzmong@gmail.com', password: 'welcome123' };

                        // Normalize requested variant from the step param
                        const name = (buttonName ?? '').trim().toLowerCase();
                        const isCheckout = /checkout/.test(name);
                        const isLater = /later/.test(name);
                        const isPlainPay = name === 'paypal';
                        const isPayWith = !isCheckout && !isLater && !isPlainPay; // "Pay with PayPal"

                        // Exact aria-labels to search for (inside the iframe)
                        const LABELS =
                          isCheckout
                            ? ['PayPal Checkout', 'Checkout with PayPal']                 // checkout-only
                            : isLater
                            ? ['Pay Later', 'PayPal Pay Later', 'Pay in 4', 'Pay monthly']
                            : isPayWith
                            ? ['Pay with PayPal']
                            : ['PayPal']; // plain

                        // Encourage PayPal to mount buttons
                        await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
                        await page.waitForTimeout(300);

                        // Find PayPal iframes (don’t use :has() here — it can’t pierce frames)
                        const iframeSelector = 'iframe[title^="PayPal"], iframe[name*="__zoid__paypal_buttons__"]';
                        await page.waitForSelector(iframeSelector, { state: 'attached', timeout: 30_000 });
                        const iframeLocator = page.locator(iframeSelector);

                        const iframeCount = await iframeLocator.count();
                        if (iframeCount === 0) throw new Error(`No PayPal iframes found for "${buttonName}".`);

                        // Try each iframe until the requested button is found
                        let clicked = false;
                        for (let i = 0; i < iframeCount && !clicked; i++) {
                          const oneIframe = iframeLocator.nth(i);
                          const handle = await oneIframe.elementHandle();
                          if (!handle) continue;
                          const frame = await handle.contentFrame();
                          if (!frame) continue;

                          // Build a union of exact-label selectors; support both role=link and role=button
                          const labelSelectors = LABELS.flatMap(l => [
                            `div[role="link"][aria-label="${l}"]`,
                            `div[role="button"][aria-label="${l}"]`,
                            `[aria-label="${l}"]`,
                          ]);
                          const candidate = frame.locator(labelSelectors.join(', ')).first();

                          // For non–Pay Later buttons, exclude any “later/installments/payinX/monthly/over time” variants
                          const safeCandidate = isLater
                            ? candidate
                            : candidate.filter({ hasNotText: /later|installments|pay\s*in\s*\d|monthly|over\s*time/i });

                          // Give the button a moment to render in this frame; if not visible, move on
                          try {
                            await safeCandidate.waitFor({ state: 'visible', timeout: 4000 });
                          } catch {
                            continue;
                          }

                          // Click and wait for popup
                          const [popup] = await Promise.all([
                            page.waitForEvent('popup', { timeout: 30_000 }),
                            safeCandidate.click({ force: true }),
                          ]);

                          // ===== Popup / login flow =====
                          await popup.waitForLoadState('domcontentloaded', { timeout: 15_000 });

                          const emailField = popup.locator('#email').or(popup.locator('#login_email'));
                          await emailField.waitFor({ state: 'visible', timeout: 20_000 });
                          await emailField.fill(creds.email);

                          const nextBtn = popup.locator('#btnNext').or(popup.locator('button:text-is("Next")'));
                          if (await nextBtn.isVisible()) await nextBtn.click();

                          const usePwd = popup.locator('button:has-text("Use Password Instead")');
                          if (await usePwd.isVisible()) {
                            await usePwd.click();
                            await popup.waitForTimeout(500);
                          }

                          const pwd = popup.locator('#password');
                          await pwd.waitFor({ state: 'visible', timeout: 15_000 });
                          await pwd.fill(creds.password);

                          const loginBtn = popup
                            .locator('#btnLogin')
                            .or(popup.locator('button:text-is("Log In")[data-atomic-wait-viewname="password"]'));
                          await loginBtn.click();

                          await popup.waitForLoadState('domcontentloaded', { timeout: 20_000 });

                          const consent = popup
                            .locator('#consentButton')
                            .or(popup.locator('button[data-atomic-wait-task="select_agree_and_continue"]'))
                            .or(popup.getByRole('button', { name: /agree|continue|save/i }))
                            .first();
                          if (await consent.isVisible()) {
                            await consent.click().catch(() => {});
                          }

                          await popup.waitForEvent('close', { timeout: 20_000 });
                          clicked = true;
                        }

                        if (!clicked) {
                          throw new Error(
                            `Could not find a visible "${buttonName}" button inside any PayPal iframe. Tried labels: ${LABELS.join(', ')}`
                          );
                        }
                      }
                    );
            """

        # Cookie setting detection
        cookie_code = None
        if df_inter['action'].astype(str).str.contains(
                "create.*new.*user.*account.*cookie|creates.*new.*user.*account.*sessionResponse.*cookie", case=False,
                regex=True).any():
            cookie_code = """
            IMPORTANT: Use this exact code for setting authentication cookies from sessionResponse:
            Then('user creates new user account with sessionResponse as cookie', async function(this: CustomWorld) {
              const page = this.page!;
              if (!this.storedValues) { this.storedValues = {}; }
              const sessionData = this.storedValues['sessionResponse'];

              if (!sessionData || !sessionData.idToken || !sessionData.sessionId || !sessionData.authToken) {
                throw new Error('Session response data is missing or incomplete');
              }

              await page.goto('https://www-stg.chewy.net');

              await page.context().addCookies([
                { name: 'ticket', value: sessionData.idToken, domain: '.www-stg.chewy.net', path: '/' },
                { name: 'asid', value: sessionData.sessionId, domain: '.www-stg.chewy.net', path: '/' },
                { name: 'at', value: sessionData.authToken, domain: '.www-stg.chewy.net', path: '/' },
                { name: 'lbGroup', value: 'use2', domain: '.www-stg.chewy.net', path: '/' }
              ]);

              console.log('Authentication cookies set successfully');
            });
            """

        # POST API custom code generation
        post_api_code = None
        if df_inter['action'].astype(str).str.contains(
                "POST|posts|API|request|response|verifies.*status|stores.*response|auth.*token|makes.*request|ADD-HEADER|ADD-REQUESTBODY",
                case=False, regex=True).any():
            post_api_code = """
            IMPORTANT: For POST API steps, use these exact patterns with proper execution timing:

            CRITICAL EXECUTION PATTERNS:
            1. For "stores response" steps - Execute API call IMMEDIATELY when storing:
               Given('user stores {string} from response as {string}', async function(this: CustomWorld, responseField: string, storedName: string) {
                 if (!this.storedValues) { this.storedValues = {}; }
                 if (!this.pendingApiConfig) {
                   throw new Error('No pending API config found.');
                 }
                 const apiService = new ApiService();
                 this.apiResponse = await apiService.makeRequest(this.pendingApiConfig);
                 this.pendingApiConfig = undefined;
                 this.storedValues[storedName] = this.apiResponse.response_data[responseField];
                 console.log(`Stored ${responseField} as ${storedName}: ${this.storedValues[storedName]}`);
               });

            2. For "add header" steps - Smart execution based on service type:
               When('user add Header {string} in {string} service', async function(this: CustomWorld, authToken: string, serviceName: string) {
                 if (!this.pendingApiConfig) {
                   throw new Error('No pending API config found.');
                 }
                 const headerValue = this.replacePlaceholders(authToken);
                 const apiService = new ApiService();
                 apiService.addHeaders(headerValue, this.pendingApiConfig);
                 // Execute immediately for account/giftcard, defer for session
                 if (serviceName.toLowerCase() !== 'session') {
                   this.apiResponse = await apiService.makeRequest(this.pendingApiConfig);
                   this.pendingApiConfig = undefined;
                   console.log(`Added header and executed ${serviceName} service API call`);
                 } else {
                   console.log(`Added header to ${serviceName} service config`);
                 }
               });

            3. For service invocation - Store config in pendingApiConfig:
               When('user INVOKES {string} service with {string}', async function(this: CustomWorld, serviceName: string, configFile: string) {
                 const configPath = path.join(process.cwd(), 'src/tests/data', configFile);
                 this.pendingApiConfig = { body_from_file: configPath };
                 console.log(`Prepared ${serviceName} service config: ${configFile}`);
               });

            4. For body modification - Execute API after adding body:
               When('user add {string} as {string} to {string} body', async function(this: CustomWorld, field: string, value: string, serviceName: string) {
                 if (!this.pendingApiConfig) {
                   throw new Error('No pending API config found.');
                 }
                 const resolvedValue = this.replacePlaceholders(value);
                 if (!this.pendingApiConfig.body) {
                   this.pendingApiConfig.body = {};
                 }
                 this.pendingApiConfig.body[field] = resolvedValue;
                 const apiService = new ApiService();
                 this.apiResponse = await apiService.makeRequest(this.pendingApiConfig);
                 this.pendingApiConfig = undefined;
                 console.log(`Added ${field} to ${serviceName} body and executed API call`);
               });

              When('user INVOKES {{string}} service with {{string}}', async function(this: CustomWorld, serviceName: string, configFile: string) {{
                const configPath = path.join(process.cwd(), 'src/tests/data', configFile);
                this.pendingApiConfig = {{ body_from_file: configPath }};
                console.log(`Prepared ${{serviceName}} service config: ${{configFile}}`);
              }});

              When('user add Header {{string}} in {{string}} service', async function(this: CustomWorld, authToken: string, serviceName: string) {{
                if (!this.pendingApiConfig) {{
                  throw new Error('No pending API config found.');
                }}
                const headerValue = this.replacePlaceholders(authToken);
                const apiService = new ApiService();
                apiService.addHeaders(headerValue, this.pendingApiConfig);
                this.apiResponse = await apiService.makeRequest(this.pendingApiConfig);
                this.pendingApiConfig = undefined;
                console.log(`Added header and executed ${{serviceName}} service API call`);
              }});

            CRITICAL REQUIREMENTS:
            - Always use this.replacePlaceholders() for dynamic token values
            - Always initialize: if (!this.storedValues) { this.storedValues = {}; }
            - Use apiService.addHeaders(headerValue, this.pendingApiConfig) for header handling
            - Execute API calls at the right timing (when storing data or adding final parameters)
            - Clear this.pendingApiConfig after execution
            - Include comprehensive console.log statements
            """

        # GET API custom code generation
        if df_inter['action'].astype(str).str.contains(
                "GET.API|GET-API|VALIDATE-STATUS|VALIDATE-CONTENT|VALIDATE-TIME|VALIDATE-HEADER|VALIDATE-BODY|STORE-RESPONSE",
                case=False, regex=True).any():
            get_api_code = """
            IMPORTANT: Generate comprehensive GET API step definitions with these requirements:

            CRITICAL PATTERNS TO IMPLEMENT:
            - 'When I send a GET request to {string}' - Basic GET request with response storage
            - 'When user makes GET request to {string}' - Alternative GET request syntax
            - 'When I make a GET request to {string} with headers:' - GET with custom headers from data table
            - 'When user gets data from {string}' - GET with dynamic URL resolution
            - 'Then the response status should be {int}' - Status code validation
            - 'Then user verifies response status is {int}' - Alternative status validation
            - 'Then user stores response as {string}' - Store entire response object
            - 'Then user stores {string} from response as {string}' - Store specific field from response
            - 'Then the response should contain {string}' - Validate field exists
            - 'Then the response body should have field {string} with value {string}' - Field value validation
            - 'Then the response time should be less than {int} milliseconds' - Performance validation

            IMPLEMENTATION REQUIREMENTS:
            - CRITICAL: Always initialize 'if (!this.storedValues) { this.storedValues = {}; }' before storing
            - Use 'this.apiResponse' for storing response objects
            - Use 'this.storedValues[name]' for storing response data
            - Use 'this.replacePlaceholders(value)' for dynamic value resolution
            - Include comprehensive try-catch blocks with detailed console.log statements
            - Use 'page.request.get(url)' for making requests
            - Parse response with 'await response.json()'
            - Support nested field access like 'user.profile.name' and array access like 'data[0].title'
            - Add helper method 'getNestedValue(obj, path)' for complex field extraction
            - Log all requests and responses with status codes and full JSON data
            - Handle headers from data tables properly
            - Support dynamic URL resolution with stored variables

            Generate complete TypeScript step definitions following these patterns exactly.
            """
        # Mock API custom code generation
        mock_api_code = None
        # Check for mock API steps in multiple ways
        has_mock_step = (df_inter['action'].astype(str).str.contains("MOCK-API|mock_setup|custom_mock_api_tool|click", case=False,
                                                                     regex=True).any() or
                         df_inter['original_bdd_step'].astype(str).str.contains("MOCK", case=False,
                                                                                regex=True).any())

        if has_mock_step:
            mock_api_code = """

            CRITICAL: For ANY step containing "MOCK", generate step definitions that load and setup mock routes:

            1. For steps like 'user MOCKS with "mock_file.json"':
            Then('user MOCKS with {{string}}', async function(this: CustomWorld, mockFile: string) {{
              const page = this.page!;
              const path = require('path');
              const fs = require('fs');
              
              // Load mock configuration file
              const mockFilePath = path.join(process.cwd(), 'target/json', mockFile);
              if (!fs.existsSync(mockFilePath)) {{
                throw new Error(`Mock file not found at: ${{mockFilePath}}`);
              }}
              
              const mockConfig = JSON.parse(fs.readFileSync(mockFilePath, 'utf-8'));
              const routes = Array.isArray(mockConfig) ? mockConfig : [mockConfig];
              
              console.log(`📋 Loading mock configuration from: ${{mockFile}}`);
              console.log(`🎯 Setting up ${{routes.length}} mock route(s)`);
              
              // Setup route interception in browser
              for (const route of routes) {{
                const pattern = route.route || route.path;
                const mockResponse = route.response || route.mockResponse;
                const status = route.status || 200;
                const method = (route.method || 'GET').toUpperCase();
                
                await page.route(pattern, async (routeHandler) => {{
                  if (routeHandler.request().method() === method) {{
                    console.log(`✅ MOCKED: ${{method}} ${{routeHandler.request().url()}}`);
                    await routeHandler.fulfill({{
                      status: status,
                      contentType: 'application/json',
                      body: JSON.stringify(mockResponse)
                    }});
                  }} else {{
                    await routeHandler.continue();
                  }}
                }});
              }}
              
              console.log(`✅ Mock setup complete for ${{mockFile}}`);
            }});

            2. For steps with "button" like 'user MOCKS "button" with "mock_file.json"':
            When('user MOCKS {{string}} with {{string}}', async function(this: CustomWorld, buttonName: string, mockFile: string) {{
              const page = this.page!;
              const path = require('path');
              const fs = require('fs');
              
              // Load mock configuration
              const mockFilePath = path.join(process.cwd(), 'target/json', mockFile);
              if (!fs.existsSync(mockFilePath)) {{
                throw new Error(`Mock file not found at: ${{mockFilePath}}`);
              }}
              
              const mockConfig = JSON.parse(fs.readFileSync(mockFilePath, 'utf-8'));
              const routes = Array.isArray(mockConfig) ? mockConfig : [mockConfig];
              
              // Setup mocks
              for (const route of routes) {{
                await page.route(route.route || route.path, async (routeHandler) => {{
                  await routeHandler.fulfill({{
                    status: route.status || 200,
                    contentType: 'application/json',
                    body: JSON.stringify(route.response || route.mockResponse)
                  }});
                }});
              }}
              
              // Click the button after mock is set up
              const button = page.getByRole('button', {{ name: buttonName }});
              await button.click();
              console.log(`Clicked button "${{buttonName}}" with mock from ${{mockFile}}`);
            }});

            MANDATORY REQUIREMENTS:
            - ALWAYS load mock JSON file from target/json directory
            - ALWAYS support both array and single object mock configurations
            - ALWAYS setup route interception for each route in the config
            - ALWAYS check file existence and throw error if missing
            - ALWAYS include proper logging for debugging
            - ALWAYS handle method matching (GET, POST, etc)
            - ALWAYS use page.route() for Playwright interception
            - Include comprehensive error handling and status codes
            """
        example = """
              Below is example for your reference

            import {{ Given, When, Then, Before, After, setDefaultTimeout }} from '@cucumber/cucumber';
            import {{ chromium, Page, Browser, expect }} from '@playwright/test';
            import {{ CustomWorld }} from "../../support/world";
            import {{ AccountService }} from '../../services/account.service';
            import {{ PageContextService }} from '../../services/pageContext.service';
            import {{ ApiRequestConfig, ApiResponse, ApiService }} from '../../services/api.service';
            import {{ MockAPIService }} from '../../services/mockapi.service';

            setDefaultTimeout(60 * 1000);

            // Explicit viewport step for Scenario Outline resolution values
            Given('the browser is launched with {{string}} resolution', async function(this: CustomWorld, value: string) {{
              const page = this.page!;
              const raw = this.replacePlaceholders(value);
              const m = typeof raw === 'string' ? raw.match(/(\\d+)\\s*[xX\\*,\\s]\\s*(\\d+)/) : null;
              const width = m ? parseInt(m[1], 10) : undefined;
              const height = m ? parseInt(m[2], 10) : undefined;
              if (width && height && width >= 300 && width <= 5000 && height >= 300 && height <= 5000) {{
                await page.setViewportSize({{ width, height }});
                console.log(`Applied explicit viewport: ${{width}}x${{height}}`);
              }} else {{
                console.log(`Invalid or missing resolution '${{raw}}', keeping default viewport`);
              }}
            }});


            Given('user INVOKES authentication service with {{string}}', async function(this: CustomWorld, configFile: string) {{
              const configPath = path.join(process.cwd(), 'src/tests/data', configFile);
              this.pendingApiConfig = {{ body_from_file: configPath }};
              console.log(`Prepared authentication service config: ${{configFile}}`);
            }});

            Then('user creates new user account with sessionResponse as cookie', async function(this: CustomWorld) {{
              const page = this.page!;
              const pageContextService = new PageContextService();
              const sessionResponse = this.storedValues?.['sessionResponse'];
              if (!sessionResponse) {{
                throw new Error('Session response not found in stored values');
              }}

              // Define cookie mapping for Chewy
              const cookieMapping = {{
                authToken: 'at',
                idToken: 'ticket',
                sessionId: 'asid'
              }};

              await pageContextService.addCookiesFromResponse(page, sessionResponse, cookieMapping, '.chewy.net');
              console.log('Created new user account with session response as cookie');
            }});

            Given('the user Navigate to {{string}}', async function(this: CustomWorld, url: string) {{
              const page = this.page!;
              await page.goto('https://www.example.com/');
              await page.waitForLoadState('domcontentloaded');
            }});

            When('user Click on the "login" button', async function(this: CustomWorld) {{
              const page = this.page!;
              const loginButton = page.getByRoleName('loginButton');
              await loginButton.click();
            }});

            When('user enters "First Name" as {{string}}', async function(this: CustomWorld, inputValue: string) {{
              const page = this.page!;
              const resolvedValue = this.replacePlaceholders(inputValue);
              const fieldLocator = page.locator('input[placeholder*="email"]');
              await fieldLocator.fill(resolvedValue);
              console.log(`Entered "${{resolvedValue}}" in "Email" field`);
            }});

            Then('user enters "Field Name" as {{string}}', async function(this: CustomWorld, inputValue: string) {{
              const page = this.page!;
              const resolvedValue = this.replacePlaceholders(inputValue);
              const fieldLocator = page.getByTestId('field-locator');
              await fieldLocator.fill(resolvedValue);
              console.log(`Entered "${{resolvedValue}}" in "Field Name" field`);
            }});

            // CRITICAL: Example for dynamic placeholders - use this.replacePlaceholders() for dynamic values
            Then('user enters "Email Address" as {{string}}', async function(this: CustomWorld, inputValue: string) {{
              const page = this.page!;
              const emailField = page.locator('input[placeholder*="email"]');
              await emailField.fill(this.replacePlaceholders(inputValue));
              console.log(`Entered email: ${{this.replacePlaceholders(inputValue)}}`);
            }});

            // CRITICAL: Example for dynamic click targets - use this.replacePlaceholders() for dynamic selectors
            Then('user clicks on {{string}}', async function(this: CustomWorld, userId: string) {{
              const page = this.page!;
              const userIdLink = page.getByRole('link', {{name: this.replacePlaceholders(userId)}});
              await userIdLink.click();
              console.log(`Clicked on user ID: ${{this.replacePlaceholders(userId)}}`);
            }});

            // Env-only secret input example (no AWS calls). Use process.env[VAR] for awssecretmanager.VAR
            Then('user enters "Password" as {{string}}', async function(this: CustomWorld, inputValue: string) {{
              const page = this.page!;
              const m = typeof inputValue === 'string' ? inputValue.match(/\\{{\\s*awssecretmanager\\.([A-Z0-9_]+)\\s*\\}}/i) : null;
              const value = ((): string => {{
                if (!m) return this.replacePlaceholders(inputValue);
                const rawKey = m[1];
                const SECRET_ENV_MAP: Record<string, string> = {{ rhapsody_password: 'AUTOTEST_USER_PWD' }};
                const mapped = SECRET_ENV_MAP[rawKey] || rawKey;
                const envVal = process.env[mapped];
                if (!envVal) throw new Error(`Environment variable ${{mapped}} is required but not set`);
                return envVal;
              }})();
              const passwordField = page.locator('#password');
              await passwordField.fill(value);
            }});

            When('user stores response as {{string}}', async function(this: CustomWorld, storedName: string) {{
              if (!this.storedValues) {{ this.storedValues = {{}}; }}
              if (!this.apiResponse) {{
                throw new Error('No API response found to store.');
              }}
              this.storedValues[storedName] = this.apiResponse.response_data;
              console.log(`Stored response as ${{storedName}}`);
            }});

            Then('user extracts cookies as {{string}}', async function(this: CustomWorld, cookieName: string) {{
              const page = this.page!;
              const cookies = await page.context().cookies();
              if (!this.storedValues) {{ this.storedValues = {{}}; }}
              const cookieMap = cookies.reduce((acc, cookie) => {{
                acc[cookie.name] = cookie.value;
                return acc;
              }}, {{}} as Record<string, string>);
              this.storedValues[cookieName] = cookieMap;
              console.log(`Extracted cookies as ${{cookieName}}:`, JSON.stringify(cookieMap, null, 2));
            }});

            {paypal_code}
            {post_api_code}
            {get_api_code}
            {cookie_code}
            {mock_api_code}


            """.format(paypal_code=paypal_code or "", post_api_code=post_api_code or "",
                       get_api_code=get_api_code or "", cookie_code=cookie_code or "",
                       mock_api_code=mock_api_code or "")
        template_stepdef = """
        CRITICAL: Start your response immediately with 'import' statements. Do not include any explanatory text like "Based on the provided scenario" or "Here's the step definition".
        CRITICAL: Only replace TEST DATA VALUES with {{string}} placeholders, NOT field names, web element identifiers, or UI element names like "Radio button", "Submit button", "Login button", etc.
        CRITICAL: UI element names like "Radio button", "Submit button", "Login button" should remain as literal text in quotes, NOT converted to {{string}}.
        CRITICAL: For dynamic placeholders like {{accountResponse.email}} or {{variable.field}}, convert them to {{string}} parameters and use this.replacePlaceholders() in the implementation.
        CRITICAL: For cookie extraction steps like "user extracts cookies as abcd_cookie", ALWAYS generate cookies as key-value pairs using reduce() method:
          const cookieMap = cookies.reduce((acc, cookie) => {{ acc[cookie.name] = cookie.value; return acc; }}, {{}} as Record<string, string>);
          this.storedValues[cookieName] = cookieMap;
          Do NOT store cookies as an array. Always convert to Record<string, string> format.
        As an expert QA Test Automation Engineer, generate a Playwright TypeScript step definition.Generate each step function function for each scenario step.
Fields DataFrame:
{df}

CRITICAL EXECUTION PATTERNS - FOLLOW EXACTLY:

1. API EXECUTION TIMING:
    - For "stores response" steps: Execute API call IMMEDIATELY when storing data
    - For "add header" steps: Check if body modification follows - if YES, only add header; if NO, execute API
    - For service invocation: Store config in this.pendingApiConfig for later execution
    - For body modification: Execute API after adding body parameters
    - CRITICAL: Session service headers should NOT execute API if body modification follows

2. HEADER HANDLING:
    - Use apiService.addHeaders(headerValue, this.pendingApiConfig) for all header operations
    - Support both full headers ("Authorization: Bearer token") and token-only formats
    - Always use this.replacePlaceholders() for dynamic values

3. COOKIE MANAGEMENT:
    - Use PageContextService.addCookiesFromResponse() with configurable cookie mapping
    - Define cookieMapping object for service-specific cookie names
    - Include proper error handling for missing session data

4. LOCATOR STRATEGY:
    - Include proper element visibility waits
    - Use this.replacePlaceholders() for dynamic field values

STRICTLY FOLLOW BELOW INSTRUCTIONS:
-Each step in the scenario should have its own unique function with the exact step text as the function name, including all quotation marks and with any parameters.
-These are test datas: {testdata}

-For steps like 'user enters "Field Name" as "Test Value"', only "Test Value" should become {{string}}, "Field Name" stays as literal text.
-EXAMPLE: 'Then user enters "Any Field Name" as "Test Value"' becomes 'Then('user enters "Any Field Name" as {{string}}', async function(this: CustomWorld, inputValue: string)'
-The field name stays literal, only the test value becomes {{string}} parameter.
-CRITICAL: For dynamic placeholders like {{accountResponse.email}} or {{userId}}, convert them to {{string}} and use this.replacePlaceholders() in implementation:
  * 'user enters "Email Address" as "{{accountResponse.email}}"' becomes 'user enters "Email Address" as {{string}}'
  * 'user clicks on "{{accountResponse.userId}}"' becomes 'user clicks on {{string}}'
  * In implementation: await field.fill(this.replacePlaceholders(inputValue)); or page.getByRole('link', {{name: this.replacePlaceholders(userId)}});
-CRITICAL LOCATOR RULE: The Fields dataframe has columns in order: BDDStep -- ActionType -- WebElementName -- TestDataValue -- PlaywrightLocator -- Condition -- WindowDetection.
  The 5th column ('PlaywrightLocator') is the EXACT Playwright locator to use in the generated TypeScript. The 4th column ('TestDataValue') is the INPUT VALUE — it is NOT a locator.
  EXAMPLE: For the row  "Then user enters \"Email address\" as \"user@example.com\" -- type -- Email address -- user@example.com -- page.getByRole('textbox', {{ name: 'Email address' }}) -- -- False"
    → the locator is:  page.getByRole('textbox', {{ name: 'Email address' }})   (column 5)
    → NOT page.getByPlaceholder('Email address') or any other derived form.
  Copy the PlaywrightLocator value verbatim — do NOT generate, infer, or substitute a different locator.
  If a PlaywrightLocator value is provided for a step, it MUST appear unchanged in the generated code (e.g., const element = page.getByRole('textbox', {{ name: 'Email address' }});).
-CRITICAL: For input field steps, only the input VALUE should use {{string}} placeholder. Field names should be literal text in quotes like "Email", "First Name", "Gift Card Number", etc.
-DO NOT use {{string}} placeholders for web element names/identifiers. Keep web element names exactly as they appear in the scenario.
-If action is "switch to window"  include window switching code BEFORE performing the primary action
for 'switch to iframe' you have to use "frameLocator" function
- WINDOW SWITCHING: When "Window Detection" column shows "True" in the dataframe, include window switching code before performing the action:
          * Use this pattern: const pages = this.page!.context().pages(); const currentPage = pages[pages.length - 1];
          * Perform all subsequent actions on currentPage instead of this.page
          * Always add await currentPage.waitForLoadState(); after getting the new page
- Use Playwright's expect library for all assertions.Implement assertions based on the "condition" column in the DataFrame
- NOTE: Playwright's action methods (click, fill, type, check, etc.) have built-in auto-waiting for visibility. Do NOT add explicit waitFor({{ state: 'visible' }}) before these actions as it is redundant.
- Use waitFor({{ state: 'visible' }}) ONLY when you need to verify visibility without performing an action, or for non-actionable elements.
- STRICTLY add DefaultTimeout and import the necessary Playwright and Cucumber libraries.
- STRICTLY Return only playwright step definition code without adding any extra comments like `script` and any others.
- CRITICAL: Always initialize 'if (!this.storedValues) {{ this.storedValues = {{}}; }}' before storing
- CRITICAL: Use the exact patterns from the example for API calls, cookie handling, and field interactions
- AWS SECRETS MANAGER INPUTS: If a step value uses `{{awssecretmanager.<VAR>}}`, resolve at runtime from environment variables only: read `process.env[<VAR>]` and throw a clear error if missing. Do NOT call AWS from the generated step definitions.
- DYNAMIC PLACEHOLDER HANDLING: For any dynamic placeholders like {{accountResponse.email}}, {{variable.field}}, convert to {{string}} parameter and ALWAYS use this.replacePlaceholders() for value resolution in the implementation.
- Generate ONLY: Given(), When(), Then() function definitions.
- Don't include any code block markers such as ```typescript``` or any other formatting. Return only executable playwright step functions.
 -- CRITICAL: Do NOT add any wrapper text, keywords, or identifiers such as 'script', 'code', 'typescript', or any other prefixes.
{example}
"""
        merged_prompt = """
        CRITICAL: Start your response immediately with 'import' statements. Do not include any explanatory text like "Based on the provided scenario" or "Here's the step definition".
        CRITICAL: Only replace TEST DATA VALUES with {{string}} placeholders, NOT field names, web element identifiers, or UI element names like "Radio button", "Submit button", "Login button", etc.
        CRITICAL: UI element names like "Radio button", "Submit button", "Login button" should remain as literal text in quotes, NOT converted to {{string}}.
        CRITICAL: For cookie extraction steps like "user extracts cookies as abcd_cookie", ALWAYS generate cookies as key-value pairs using reduce() method:
          const cookieMap = cookies.reduce((acc, cookie) => {{ acc[cookie.name] = cookie.value; return acc; }}, {{}} as Record<string, string>);
          this.storedValues[cookieName] = cookieMap;
          Do NOT store cookies as an array. Always convert to Record<string, string> format.
                                        As an expert QA Test Automation Engineer, generate a Playwright TypeScript step definition.Generate each step function function for each scenario step.
                        Fields DataFrame:
                        {df}
                        CRITICAL EXECUTION PATTERNS - FOLLOW EXACTLY:

                        1. API EXECUTION TIMING:
                            - For "stores response" steps: Execute API call IMMEDIATELY when storing data
                            - For "add header" steps: Check if body modification follows - if YES, only add header; if NO, execute API
                            - For service invocation: Store config in this.pendingApiConfig for later execution
                            - For body modification: Execute API after adding body parameters
                            - CRITICAL: Session service headers should NOT execute API if body modification follows

                        2. HEADER HANDLING:
                            - Use apiService.addHeaders(headerValue, this.pendingApiConfig) for all header operations
                            - Support both full headers ("Authorization: Bearer token") and token-only formats
                            - Always use this.replacePlaceholders() for dynamic values

                        3. COOKIE MANAGEMENT:
                            - Use PageContextService.addCookiesFromResponse() with configurable cookie mapping
                            - Define cookieMapping object for service-specific cookie names
                            - Include proper error handling for missing session data

                        4. LOCATOR STRATEGY:
                            - Include proper element visibility waits
                            - Use this.replacePlaceholders() for dynamic field values

                        STRICTLY FOLLOW BELOW INSTRUCTIONS:
-Each step in the scenario should have its own unique function with the exact step text as the function name, including all quotation marks and with any parameters.
-These are test datas: {testdata}

-For steps like 'user enters "Field Name" as "Test Value"', only "Test Value" should become {{string}}, "Field Name" stays as literal text.
-EXAMPLE: 'Then user enters "Any Field Name" as "Test Value"' becomes 'Then('user enters "Any Field Name" as {{string}}', async function(this: CustomWorld, inputValue: string)'
-The field name stays literal, only the test value becomes {{string}} parameter.
-CRITICAL: For dynamic placeholders like {{accountResponse.email}} or {{userId}}, convert them to {{string}} and use this.replacePlaceholders() in implementation:
  * 'user enters "Email Address" as "{{accountResponse.email}}"' becomes 'user enters "Email Address" as {{string}}'
  * 'user clicks on "{{accountResponse.userId}}"' becomes 'user clicks on {{string}}'
  * In implementation: await field.fill(this.replacePlaceholders(inputValue)); or page.getByRole('link', {{name: this.replacePlaceholders(userId)}});
-CRITICAL LOCATOR RULE: The Fields dataframe has columns in order: BDDStep -- ActionType -- WebElementName -- TestDataValue -- PlaywrightLocator -- Condition -- WindowDetection.
  The 5th column ('PlaywrightLocator') is the EXACT Playwright locator to use in the generated TypeScript. The 4th column ('TestDataValue') is the INPUT VALUE — it is NOT a locator.
  EXAMPLE: For the row  "Then user enters \"Email address\" as \"user@example.com\" -- type -- Email address -- user@example.com -- page.getByRole('textbox', {{ name: 'Email address' }}) -- -- False"
    → the locator is:  page.getByRole('textbox', {{ name: 'Email address' }})   (column 5)
    → NOT page.getByPlaceholder('Email address') or any other derived form.
  Copy the PlaywrightLocator value verbatim — do NOT generate, infer, or substitute a different locator.
  If a PlaywrightLocator value is provided for a step, it MUST appear unchanged in the generated code.
-CRITICAL: For input field steps, only the input VALUE should use {{string}} placeholder. Field names should be literal text in quotes like "Email", "First Name", "Gift Card Number", etc.
-DO NOT use {{string}} placeholders for web element names/identifiers. Keep web element names exactly as they appear in the scenario.
-If action is "switch to window"  include window switching code BEFORE performing the primary action
for 'switch to iframe' you have to use "frameLocator" function
- WINDOW SWITCHING: When "Window Detection" column shows "True" in the dataframe, include window switching code before performing the action:
  * Use this pattern: const pages = this.page!.context().pages(); const currentPage = pages[pages.length - 1];
  * Perform all subsequent actions on currentPage instead of this.page
  * Always add await currentPage.waitForLoadState(); after getting the new page
- Use Playwright's expect library for all assertions.Implement assertions based on the "condition" column in the DataFrame
- NOTE: Playwright's action methods (click, fill, type, check, etc.) have built-in auto-waiting for visibility. Do NOT add explicit waitFor({{ state: 'visible' }}) before these actions as it is redundant.
- Use waitFor({{ state: 'visible' }}) ONLY when you need to verify visibility without performing an action, or for non-actionable elements.
- STRICTLY Return only playwright step definition code without adding any extra comments like `script` and any others.
- Generate each step function for each scenario step.
- CRITICAL: Always initialize 'if (!this.storedValues) {{ this.storedValues = {{}}; }}' before storing
- CRITICAL: Use the exact patterns from the example for API calls, cookie handling, and field interactions
- AWS SECRETS MANAGER INPUTS: If a step value uses `{{awssecretmanager.<VAR>}}`, resolve at runtime from environment variables only: read `process.env[<VAR>]` and throw a clear error if missing. Do NOT call AWS from the generated step definitions.
- DYNAMIC PLACEHOLDER HANDLING: For any dynamic placeholders like {{accountResponse.email}}, {{variable.field}}, convert to {{string}} parameter and ALWAYS use this.replacePlaceholders() for value resolution in the implementation.
- CRITICAL: Generate ONLY the step definition functions. NO imports, NO extra code, NO comments.
- Do NOT include: import statements, const declarations, or any setup code.
- Generate ONLY: Given(), When(), Then() function definitions.
- Don't include any code block markers such as ```typescript``` or any other formatting. Return only executable playwright step functions.
-- CRITICAL: Do NOT add any wrapper text, keywords, or identifiers such as 'script', 'code', 'typescript', or any other prefixes.

{example}
"""
        testdata = df_inter['data'].dropna().astype(str).str.cat(sep=',')

        model = LLM()

        # Emergency chunking check for very large feature files
        emergency_chunking = False
        if not self._should_use_chunking(data, df, df_inter):
            # Check if content is actually too large despite detection logic
            total_content_size = len(str(data)) + len(str(df)) + len(str(df_inter))
            if total_content_size > 25000:  # Very large feature file - raised threshold to preserve iframe locators
                emergency_chunking = True
                logger.warning(f"🚨 EMERGENCY CHUNKING: Very large feature file detected (size: {total_content_size})!")

        # Use chunking for large feature files
        if self._should_use_chunking(data, df, df_inter) or emergency_chunking:
            logger.info(f"Large feature file detected. Using chunking strategy for processing.")
            final_script = self._process_with_chunking(data, df, df_inter, i, flag, jira_id, current_script, model,
                                               merged_prompt, template_stepdef, example, testdata,
                                               paypal_code, post_api_code, get_api_code, cookie_code, mock_api_code, exclude_paypal_code)
            # Defer return to allow conditional injection below
        else:
            final_script = None

        logger.info("MY DF \n")
        logger.info(df)
        logger.info(f"Checking for gift card detection...")
        logger.info(f"Objects in df_inter: {df_inter['object'].tolist()}")
        logger.info(f"Actions in df_inter: {df_inter['action'].tolist()}")
        click_actions = df_inter['action'].astype(str).str.contains("click", case=False)
        logger.info(f"Click actions found: {click_actions.any()}")

        if flag and final_script is None:
            output_qa_prompt_stepDef = model.send_request(merged_prompt,
                                                          ["df", "example", "testdata", "paypal_code",
                                                           "post_api_code", "get_api_code", "cookie_code",
                                                           "mock_api_code"],
                                                          {'df': df, "example": example,
                                                           "testdata": testdata,
                                                           "paypal_code": paypal_code or "",
                                                           "post_api_code": post_api_code or "",
                                                           "get_api_code": get_api_code or "",
                                                           "cookie_code": cookie_code or "",
                                                           "mock_api_code": mock_api_code or ""
                                                           })

            if output_qa_prompt_stepDef.startswith("```typescript"
                                                   "") and output_qa_prompt_stepDef.endswith("```"):
                output_qa_prompt_stepDef = output_qa_prompt_stepDef[7:-3]
            processed_code = process_typescript_code(output_qa_prompt_stepDef)

            # Apply the same import organization for consistency with deduplication
            if current_script:
                # Extract components from both current and new code
                current_imports, current_setup, current_code = self._extract_imports_and_setup(current_script)
                new_imports, new_setup, new_code = self._extract_imports_and_setup(processed_code)

                # Check for duplicate step definitions before combining
                existing_step_signatures = self._extract_step_signatures(current_script)
                filtered_new_code_text = self._filter_duplicate_steps_from_new_code('\n'.join(new_code), existing_step_signatures)
                filtered_new_code = filtered_new_code_text.split('\n') if filtered_new_code_text.strip() else []
                
                logger.info(f"🔄 DEDUP: Filtered new code from {len(new_code)} to {len(filtered_new_code)} lines (removed duplicates)")

                # Combine and deduplicate imports/setup
                all_imports = self._deduplicate_imports(current_imports + new_imports)
                all_setup = self._deduplicate_imports(current_setup + new_setup)

                # Assemble final script
                final_parts = []
                if all_imports:
                    final_parts.extend(all_imports)
                    final_parts.append('')
                if all_setup:
                    final_parts.extend(all_setup)
                    final_parts.append('')
                if current_code:
                    final_parts.append('\n'.join(current_code))
                if filtered_new_code and any(line.strip() for line in filtered_new_code):
                    if current_code:
                        final_parts.append('')
                    final_parts.append('\n'.join(filtered_new_code))

                final_script = '\n'.join(final_parts)
            else:
                final_script = processed_code

        elif final_script is None:
            output_qa_prompt_stepDef = model.send_request(template_stepdef,
                                                          ["df", "example", "testdata", "paypal_code",
                                                           "post_api_code", "get_api_code", "cookie_code",
                                                           "mock_api_code"],
                                                          {'df': df, "example": example,
                                                           "testdata": testdata,
                                                           "paypal_code": paypal_code or "",
                                                           "post_api_code": post_api_code or "",
                                                           "get_api_code": get_api_code or "",
                                                           "cookie_code": cookie_code or "",
                                                           "mock_api_code": mock_api_code or ""
                                                           })

            if output_qa_prompt_stepDef.startswith("```typescript"
                                                   "") and output_qa_prompt_stepDef.endswith("```"):
                output_qa_prompt_stepDef = output_qa_prompt_stepDef[7:-3]
            processed_code = process_typescript_code(output_qa_prompt_stepDef)

            # Apply the same import organization for consistency with deduplication
            if current_script:
                # Extract components from both current and new code
                current_imports, current_setup, current_code = self._extract_imports_and_setup(current_script)
                new_imports, new_setup, new_code = self._extract_imports_and_setup(processed_code)

                # Check for duplicate step definitions before combining
                existing_step_signatures = self._extract_step_signatures(current_script)
                filtered_new_code_text = self._filter_duplicate_steps_from_new_code('\n'.join(new_code), existing_step_signatures)
                filtered_new_code = filtered_new_code_text.split('\n') if filtered_new_code_text.strip() else []
                
                logger.info(f"🔄 DEDUP: Filtered new code from {len(new_code)} to {len(filtered_new_code)} lines (removed duplicates)")

                # Combine and deduplicate imports/setup
                all_imports = self._deduplicate_imports(current_imports + new_imports)
                all_setup = self._deduplicate_imports(current_setup + new_setup)

                # Assemble final script
                final_parts = []
                if all_imports:
                    final_parts.extend(all_imports)
                    final_parts.append('')
                if all_setup:
                    final_parts.extend(all_setup)
                    final_parts.append('')
                if current_code:
                    final_parts.append('\n'.join(current_code))
                if filtered_new_code and any(line.strip() for line in filtered_new_code):
                    if current_code:
                        final_parts.append('')
                    final_parts.append('\n'.join(filtered_new_code))

                final_script = '\n'.join(final_parts)
            else:
                final_script = processed_code

            logger.info(final_script)

        # Conditional injection: add/normalize resolution steps based on feature content
        try:
            feature_has_resolution = False
            # Look for a step phrase containing both parts to avoid false positives
            import re as _re_local
            if isinstance(data, str):
                feature_has_resolution = bool(_re_local.search(r'the\s+browser\s+is\s+launched\s+with[\s\S]*?resolution', data, flags=_re_local.IGNORECASE))
            # Avoid duplicate if LLM already generated it
            already_present = "the browser is launched with" in (final_script or "")
            if feature_has_resolution and not already_present:
                resolution_step_code = """
            Given('the browser is launched with {string} resolution', async function(this: CustomWorld, value: string) {
              const page = this.page!;
              const raw = this.replacePlaceholders(value);
              const m = typeof raw === 'string' ? raw.match(/(\\d+)\\s*[xX\\*,\\s]\\s*(\\d+)/) : null;
              const width = m ? parseInt(m[1], 10) : undefined;
              const height = m ? parseInt(m[2], 10) : undefined;
              if (width && height && width >= 300 && width <= 5000 && height >= 300 && height <= 5000) {
                await page.setViewportSize({ width, height });
                console.log(`Applied explicit viewport: ${width}x${height}`);
              } else {
                console.log(`Invalid or missing resolution '${raw}', keeping default viewport`);
              }
            });
                """.rstrip()
                # Ensure final_script ends with a newline
                if not final_script.endswith("\n"):
                    final_script += "\n"
                final_script += resolution_step_code + "\n"
                logger.info("✅ Injected resolution step binding based on feature content")

            # Normalize or inject 'user resizes browser to {string}' pattern for alternative phrasing
            try:
                if isinstance(final_script, str):
                    import re as _re_norm
                    # Replace any hardcoded literal variant with a parameterized one
                    literal_resize_pattern = r"Then\('user resizes browser to\s+[^']+'\s*,\s*async function\(this: CustomWorld\)[\s\S]*?\}\);"
                    if _re_norm.search(literal_resize_pattern, final_script):
                        param_resize_code = """
            Then('user resizes browser to {string}', async function(this: CustomWorld, value: string) {
              const page = this.page!;
              const raw = this.replacePlaceholders(value);
              const m = typeof raw === 'string' ? raw.match(/(\\d+)\\s*[xX\\*,\\s]\\s*(\\d+)/) : null;
              const width = m ? parseInt(m[1], 10) : undefined;
              const height = m ? parseInt(m[2], 10) : undefined;
              if (width && height && width >= 300 && width <= 5000 && height >= 300 && height <= 5000) {
                await page.setViewportSize({ width, height });
                console.log(`Applied explicit viewport: ${width}x${height}`);
              } else {
                console.log(`Invalid or missing resolution '${raw}', keeping default viewport`);
              }
            });
                        """.rstrip()
                        final_script = _re_norm.sub(param_resize_code, final_script, count=1)
                        logger.info("✅ Normalized literal 'user resizes browser to' step to parameterized form")
                    else:
                        # If feature uses the 'resizes browser to' phrasing, ensure parameterized binding exists
                        feature_has_resize = isinstance(data, str) and _re_norm.search(r"resizes\s+browser\s+to", data, flags=_re_norm.IGNORECASE)
                        param_signature_present = "user resizes browser to {string}" in (final_script or "")
                        if feature_has_resize and not param_signature_present:
                            param_resize_code = """
            Then('user resizes browser to {string}', async function(this: CustomWorld, value: string) {
              const page = this.page!;
              const raw = this.replacePlaceholders(value);
              const m = typeof raw === 'string' ? raw.match(/(\\d+)\\s*[xX\\*,\\s]\\s*(\\d+)/) : null;
              const width = m ? parseInt(m[1], 10) : undefined;
              const height = m ? parseInt(m[2], 10) : undefined;
              if (width && height && width >= 300 && width <= 5000 && height >= 300 && height <= 5000) {
                await page.setViewportSize({ width, height });
                console.log(`Applied explicit viewport: ${width}x${height}`);
              } else {
                console.log(`Invalid or missing resolution '${raw}', keeping default viewport`);
              }
            });
                            """.rstrip()
                            if not final_script.endswith("\n"):
                                final_script += "\n"
                            final_script += param_resize_code + "\n"
                            logger.info("✅ Injected parameterized 'user resizes browser to {string}' step based on feature content")
            except Exception as _ne:
                logger.debug(f"Resolution step normalization skipped: {_ne}")

            # Normalize incorrect ZIP Code step binding where the value and field placeholders are flipped
            try:
                if isinstance(final_script, str):
                    import re as _re_zip
                    # Example incorrect form:
                    # Then('user enters "ZIP Code" in the {string} field', async function(this: CustomWorld, value: string) { ... })
                    zip_incorrect_pattern = _re_zip.compile(
                        r"(?m)^(Then|When)\('user enters\s+\"([^\"]+)\"\s+in the\s+\{string\}\s+field'\s*,",
                        flags=_re_zip.IGNORECASE
                    )
                    def _zip_swap(match):
                        kw = match.group(1)
                        label = match.group(2)
                        return f"{kw}('user enters {{string}} in the \"{label}\" field',"
                    new_script, subs = zip_incorrect_pattern.subn(_zip_swap, final_script)
                    if subs > 0:
                        final_script = new_script
                        logger.info(f"✅ Normalized {subs} ZIP/field value step binding(s) to correct parameter order")
            except Exception as _ze:
                logger.debug(f"ZIP step normalization skipped: {_ze}")
        except Exception as _e:
            logger.warning(f"⚠️ Failed to conditionally inject resolution step: {_e}")
            
        
        logger.info('Step definition file generated')
        return final_script

    def _dataframe_to_string_preserve_iframe(self, df):
        """Convert DataFrame to string while preserving full iframe locators"""
        try:
            logger.info(f"🔧 IFRAME PRESERVATION: Processing DataFrame with {len(df)} rows")
            
            # Use unlimited width and max_colwidth to prevent truncation of iframe locators
            pd.set_option('display.max_colwidth', None)
            pd.set_option('display.width', None)
            pd.set_option('display.max_columns', None)
            
            # Convert to string with full content preserved
            df_str = df.to_string(max_cols=None, max_colwidth=None)
            
            # Reset pandas display options to defaults
            pd.reset_option('display.max_colwidth')
            pd.reset_option('display.width') 
            pd.reset_option('display.max_columns')
            
            # Additional check: If iframe locators are still truncated, handle them specifically
            if 'playwrightLocator' in df.columns:
                # Enhanced iframe detection with more comprehensive patterns
                iframe_patterns = r'iframe|frameLocator|contentFrame|switch_to_iframe|frameLocator|\.frame\('
                iframe_rows = df[df['playwrightLocator'].str.contains(iframe_patterns, case=False, na=False, regex=True)]
                
                if not iframe_rows.empty:
                    logger.info(f"🔍 Found {len(iframe_rows)} iframe locator rows - ensuring full preservation")
                    
                    # Log each iframe locator for debugging
                    for idx, row in iframe_rows.iterrows():
                        locator_preview = str(row.get('playwrightLocator', 'N/A'))[:100]
                        logger.info(f"🎯 Iframe Row {idx}: {locator_preview}...")
                    
                    # Append full iframe locator details at the end
                    iframe_details = "\n\n=== IFRAME LOCATOR DETAILS (FULL) ===\n"
                    for idx, row in iframe_rows.iterrows():
                        iframe_details += f"Step: {row.get('original_bdd_step', 'N/A')}\n"
                        iframe_details += f"Full Locator: {row.get('playwrightLocator', 'N/A')}\n"
                        iframe_details += f"Action: {row.get('action', 'N/A')}\n"
                        iframe_details += f"Object: {row.get('object', 'N/A')}\n\n"
                    
                    df_str += iframe_details
                    logger.info("✅ Added iframe locator preservation section to DataFrame string")
            
            return df_str
            
        except Exception as e:
            logger.error(f"❌ Error preserving iframe locators in DataFrame string: {e}")
            # Fallback to standard to_string() if something goes wrong
            return df.to_string()
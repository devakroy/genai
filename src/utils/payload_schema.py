from pydantic import BaseModel, conlist
from typing import List, Dict

class GPTProcessRequest(BaseModel):
    jira_ids: List[str]
    confluence_ids: List[str]


class ProcessResponse(BaseModel):
    jira_details: dict
    confluence_details: dict
    test_steps: str
    test_scenarios: str

class GetConfluenceData(BaseModel):
    confluence_id: str

class GetJiraData(BaseModel):
    jira_id: str

# class ProcessRequest(BaseModel):
#     data: List[List[str]]
#
#     def validate_pairs(self) -> bool:
#         """Validate that each pair has exactly 2 elements."""
#         return all(len(pair) == 2 for pair in self.data)
#
#     def get_ids(self) -> tuple[List[str], List[str]]:
#         """Extract Jira and Confluence IDs from pairs."""
#         if not self.data or not self.validate_pairs():
#             return [], []
#         return list(zip(*self.data))

class ProcessRequest(BaseModel):
    data: List[str]

    def validate_ids(self) -> bool:
        """Validate that each ID follows the expected format."""
        return all(isinstance(id_, str) and id_.strip() for id_ in self.data)

    def get_ids(self) -> List[str]:
        """Extract and return the list of Jira IDs."""
        if not self.data or not self.validate_ids():
            return []
        return [id_.strip() for id_ in self.data]
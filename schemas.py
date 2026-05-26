from pydantic import BaseModel, field_validator
from typing import Optional

# User schema
class UserCreate(BaseModel):
    user_id: str
    user_name: str
    email: Optional[str] = None
    role: Optional[str] = None

# Token schema
class UserTokenCreate(BaseModel):
    token_id: str
    user_id: str
    token: str


# Example Pydantic model
class AdminLogin(BaseModel):
    username: str
    password: str


# creat a pydantic model for UserToken details
class UserTokenDetails(BaseModel):
    token_id: str
    user_id: str
    token: str
    updated_at: str

# store data in  transript table
class TranscriptCreate(BaseModel):
    file_link: str
    owner_id: str
    transcription: Optional[str] = None
    client_name: Optional[str] = None
    client_number: Optional[str] = None
    policy_type: Optional[str] = None
    reason_for_call: Optional[str] = None
    key_points: Optional[str] = None
    customer_sentiment: Optional[str] = None
    follow_up_needed: Optional[bool] = None
    follow_up_task: Optional[str] = None
    crm_note: Optional[str] = None
    recordingID: Optional[str] = None
    caller_number: Optional[str] = None
    from_name: Optional[str] = None
    usage_type: Optional[str] = None
    usage_sec: Optional[int] = None
    start_time: Optional[str] = None
    call_type: Optional[str] = None
    direction: Optional[str] = None
    to_phoneNumber: Optional[str] = None
    to_name: Optional[str] = None
    insured_intent: Optional[str] = None
    material_risk_facts: Optional[str] = None
    coverage_discussed: Optional[str] = None
    monetary_values: Optional[str] = None
    options_presented: Optional[str] = None
    client_selection: Optional[str] = None
    agent_recommendation: Optional[str] = None
    eo_red_flags: Optional[str] = None
    agent_statements_liability: Optional[str] = None
    missing_information: Optional[str] = None
    confidence_score: Optional[int] = None

    @field_validator(
        "transcription",
        "client_name",
        "client_number",
        "policy_type",
        "reason_for_call",
        "key_points",
        "customer_sentiment",
        "follow_up_task",
        "crm_note",
        "recordingID",
        "caller_number",
        "from_name",
        "usage_type",
        "start_time",
        "call_type",
        "direction",
        "to_phoneNumber",
        "to_name",
        "insured_intent",
        "material_risk_facts",
        "coverage_discussed",
        "monetary_values",
        "options_presented",
        "client_selection",
        "agent_recommendation",
        "eo_red_flags",
        "agent_statements_liability",
        "missing_information",
        mode="before",
    )
    @classmethod
    def empty_string_to_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("follow_up_needed", mode="before")
    @classmethod
    def normalize_follow_up_needed(cls, value):
        if value in (None, ""):
            return None
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                return None
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
        return value


# Update transcript request model
class UpdateTranscriptRequest(BaseModel):
    transcription: Optional[str] = None
    reason_for_call: Optional[str] = None
    key_points: Optional[str] = None
    follow_up_task: Optional[str] = None
    crm_note: Optional[str] = None


class FollowUpTaskUpdate(BaseModel):
    action: str
    task: Optional[str] = None
    new_task: Optional[str] = None

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.flow import FlowSourceType, MatchKeyStrategy, ParserType


# ---------- FlowSourceAccount ----------
class FlowSourceAccountBase(BaseModel):
    account_number: str
    label: Optional[str] = None


class FlowSourceAccountCreate(FlowSourceAccountBase):
    pass


class FlowSourceAccountResponse(FlowSourceAccountBase):
    id: int
    flow_source_id: int
    model_config = ConfigDict(from_attributes=True)


# ---------- FlowSource ----------
class FlowSourceBase(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool = True
    source_type: FlowSourceType
    parser_type: ParserType
    match_key_strategy: MatchKeyStrategy = MatchKeyStrategy.RECO_ID_AMOUNT
    inbox_subfolder: Optional[str] = None
    file_pattern: Optional[str] = None
    parser_config: Optional[Dict[str, Any]] = None
    accounts: List[FlowSourceAccountCreate] = Field(default_factory=list)


class FlowSourceCreate(FlowSourceBase):
    pass


class FlowSourceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    source_type: Optional[FlowSourceType] = None
    parser_type: Optional[ParserType] = None
    match_key_strategy: Optional[MatchKeyStrategy] = None
    inbox_subfolder: Optional[str] = None
    file_pattern: Optional[str] = None
    parser_config: Optional[Dict[str, Any]] = None
    accounts: Optional[List[FlowSourceAccountCreate]] = None


class FlowSourceResponse(FlowSourceBase):
    id: int
    flow_id: int
    accounts: List[FlowSourceAccountResponse] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ---------- Flow ----------
class FlowBase(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool = True
    default_currency: Optional[str] = None


class FlowCreate(FlowBase):
    sources: List[FlowSourceCreate] = Field(default_factory=list)


class FlowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    default_currency: Optional[str] = None
    sources: Optional[List[FlowSourceCreate]] = None


class FlowResponse(FlowBase):
    id: int
    sources: List[FlowSourceResponse] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)

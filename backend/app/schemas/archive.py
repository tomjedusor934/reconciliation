from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.flow import FlowSourceType, MatchKeyStrategy, ParserType


class ArchiveEntryResponse(BaseModel):
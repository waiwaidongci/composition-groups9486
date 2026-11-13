from pydantic import BaseModel, ConfigDict, Field


class CorrectionItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: int = Field(strict=True)
    group: str = Field(min_length=1, max_length=40)
    values: dict = Field(min_length=1, max_length=8)


class CorrectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    study_id: int = Field(strict=True)
    reason: str = Field(min_length=8, max_length=1500)
    items: list[CorrectionItemInput] = Field(min_length=1, max_length=300)


class ChangeView(BaseModel):
    field: str
    label: str
    before: object
    after: object


class CorrectionItemView(BaseModel):
    sample_id: int
    code: str
    group: str
    values: dict
    included: bool
    changed: bool
    reject_reason: str
    changes: list[ChangeView]
    before_group: str
    before_values: dict


class CorrectionView(BaseModel):
    id: int
    study_id: int
    reason: str
    status: str
    items: list[CorrectionItemView]
    conflict: dict
    created_by: str
    confirmed_by: str
    created_at: str
    confirmed_at: str
    included_count: int
    excluded_count: int
    changed_count: int

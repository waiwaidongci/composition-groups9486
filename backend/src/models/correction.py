from sqlalchemy import String, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from ..config.database import Base
from ..utils.clock import now


class SampleCorrection(Base):
    """化验复测后的批量更正批次。

    status:
      preview    预览已生成，尚未确认，未写入任何样本
      confirmed  已整批确认，included 项已写入 composition_samples
      conflicted 确认时发现并发冲突或已被先确认批次覆盖，整批拒绝
    items 以 JSON 留存每个样本的前后值、是否写入及不写入原因；
    conflict 留存整批拒绝时的冲突样本与说明。
    """

    __tablename__ = "sample_corrections"
    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("composition_studies.id"))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="preview")
    items: Mapped[list] = mapped_column(JSON)
    conflict: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(160))
    confirmed_by: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[str] = mapped_column(String(64), default=now)
    confirmed_at: Mapped[str] = mapped_column(String(64), default="")

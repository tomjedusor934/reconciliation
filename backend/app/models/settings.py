from sqlalchemy import Column, Integer, String, Text, UniqueConstraint

from app.db.base import Base


class Settings(Base):
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, nullable=False, index=True)
    value = Column(Text, nullable=True)  # JSON string pour les valeurs complexes
    description = Column(String, nullable=True)

    # Contrainte : la clé doit être unique
    __table_args__ = (
        UniqueConstraint('key', name='uq_setting_key'),
    )

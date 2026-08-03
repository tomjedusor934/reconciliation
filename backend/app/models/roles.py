import enum

from sqlalchemy import Column, Enum, ForeignKey, Integer, String, Table
from sqlalchemy.orm import relationship

from app.db.base import Base


class AccessLevelEnum(str, enum.Enum):
    ALL = "ALL"
    EDIT = "EDIT"
    NONE = "NONE"


#table d'association pour les relations many-to-many entre utilisateurs et rôles
user_roles = Table(
    'user_roles',
    Base.metadata,
    Column('user_id', Integer, ForeignKey('user.id'), primary_key=True),
    Column('role_id', Integer, ForeignKey('role.id'), primary_key=True)
)

class AccessiblePage(Base):
    __tablename__ = 'accessible_page'
    id = Column(Integer, primary_key=True, index=True)
    path = Column(String, nullable=False)
    role_id = Column(Integer, ForeignKey('role.id'))
    access_level = Column(Enum(AccessLevelEnum), default=AccessLevelEnum.ALL, nullable=False)

    role = relationship("Role", back_populates="accessible_pages")

class Role(Base):
    __tablename__ = 'role'
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)

    #relation many-to-many avec les utilisateurs
    users = relationship("User", secondary=user_roles, back_populates="roles")
    accessible_pages = relationship("AccessiblePage", back_populates="role", cascade="all, delete-orphan")

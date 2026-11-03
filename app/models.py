"""This project's database tables.

Define tables on the shared Base, then create a migration:
    python scripts/manage.py makemigration "add petitions"

Example:
    from sqlalchemy import String, Text
    from sqlalchemy.orm import Mapped, mapped_column
    from foundation.db import Base

    class Petition(Base):
        __tablename__ = "petitions"
        id: Mapped[int] = mapped_column(primary_key=True)
        title: Mapped[str] = mapped_column(String(300))
        body: Mapped[str] = mapped_column(Text, default="")
"""

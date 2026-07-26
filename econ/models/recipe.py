import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import Boolean, Integer, String, Numeric, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class Recipe(Base):
    """A declared transformation: consume inputs now, credit outputs after
    duration_ticks. The manufacturing tree is data — it emerges from which
    recipes exist, not from code."""

    __tablename__ = "recipes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # uppercase, e.g. BAKE_BREAD
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    duration_ticks: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = completes at start
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    inputs: Mapped[list["RecipeInput"]] = relationship(
        "RecipeInput", back_populates="recipe", cascade="all, delete-orphan", order_by="RecipeInput.symbol"
    )
    outputs: Mapped[list["RecipeOutput"]] = relationship(
        "RecipeOutput", back_populates="recipe", cascade="all, delete-orphan", order_by="RecipeOutput.symbol"
    )
    requirements: Mapped[list["RecipeRequirement"]] = relationship(
        "RecipeRequirement", back_populates="recipe", cascade="all, delete-orphan"
    )
    unlocks: Mapped[list["RecipeUnlock"]] = relationship(
        "RecipeUnlock", back_populates="recipe", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Recipe {self.code} duration={self.duration_ticks}>"


class RecipeInput(Base):
    __tablename__ = "recipe_inputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="inputs")


class RecipeOutput(Base):
    __tablename__ = "recipe_outputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="outputs")


class RecipeRequirement(Base):
    """The recipe may only be started by an entity whose unlock set (own +
    world) contains this technology — the whole of recipe gating."""

    __tablename__ = "recipe_requirements"
    __table_args__ = (UniqueConstraint("recipe_id", "technology_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    technology_id: Mapped[str] = mapped_column(String(36), ForeignKey("technologies.id"), nullable=False)

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="requirements")
    technology: Mapped["Technology"] = relationship("Technology")


class RecipeUnlock(Base):
    """Completing the recipe grants this technology (research: the output is
    an unlock rather than goods)."""

    __tablename__ = "recipe_unlocks"
    __table_args__ = (UniqueConstraint("recipe_id", "technology_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    technology_id: Mapped[str] = mapped_column(String(36), ForeignKey("technologies.id"), nullable=False)

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="unlocks")
    technology: Mapped["Technology"] = relationship("Technology")

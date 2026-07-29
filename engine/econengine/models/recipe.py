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
    # parcel-bound production: the process must be bound to a controlled parcel
    # carrying a facility of this type ("smelt at a forge"); NULL = unlocated
    requires_facility: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # construction: completion erects a facility of this type on the bound
    # parcel — the output is a facility rather than goods
    builds_facility: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
    branches: Mapped[list["RecipeBranch"]] = relationship(
        "RecipeBranch", back_populates="recipe", cascade="all, delete-orphan", order_by="RecipeBranch.position"
    )
    requirements: Mapped[list["RecipeRequirement"]] = relationship(
        "RecipeRequirement", back_populates="recipe", cascade="all, delete-orphan"
    )
    unlocks: Mapped[list["RecipeUnlock"]] = relationship(
        "RecipeUnlock", back_populates="recipe", cascade="all, delete-orphan"
    )
    good_requirements: Mapped[list["RecipeGoodRequirement"]] = relationship(
        "RecipeGoodRequirement", back_populates="recipe", cascade="all, delete-orphan",
        order_by="RecipeGoodRequirement.symbol",
    )
    deposit_inputs: Mapped[list["RecipeDepositInput"]] = relationship(
        "RecipeDepositInput", back_populates="recipe", cascade="all, delete-orphan",
        order_by="RecipeDepositInput.symbol",
    )
    # Inputs consumed once per tick, every tick the process is RUNNING (not
    # just at start). A duration-N recipe pays these N times. See
    # production.consume_per_tick_inputs. Lets labour paid out of a flow
    # income fund a multi-tick process (research, construction) that the
    # one-shot `inputs` model could only demand as a lump sum -- which, with
    # 0.5/tick decay on the labour goods, was unreachable.
    per_tick_inputs: Mapped[list["RecipePerTickInput"]] = relationship(
        "RecipePerTickInput", back_populates="recipe", cascade="all, delete-orphan",
        order_by="RecipePerTickInput.symbol",
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


class RecipePerTickInput(Base):
    """Consumed once per tick, every tick the process is RUNNING, for a
    duration-N recipe that pays N times (production.consume_per_tick_inputs).
    The recurring-cost counterpart to RecipeInput's lump-sum: a multi-tick
    process fed from a flow rather than a stock. Drawn from the unreserved
    balance at face value; a tick the entity cannot meet it abandons the
    process (FAILED, inputs forfeit) -- the engine never partially draws."""

    __tablename__ = "recipe_per_tick_inputs"
    __table_args__ = (UniqueConstraint("recipe_id", "symbol"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="per_tick_inputs")


class RecipeOutput(Base):
    __tablename__ = "recipe_outputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="outputs")


class RecipeBranch(Base):
    """One row of a stochastic recipe's outcome table: an alternative output
    set with a fixed weight, sampled once at completion. A recipe declares
    either plain outputs or branches, never both. Odds are constant within a
    recipe — a player reads the table and knows them exactly."""

    __tablename__ = "recipe_branches"
    __table_args__ = (UniqueConstraint("recipe_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # table order; selection walks positions
    weight: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")  # e.g. "ruined the blank"

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="branches")
    outputs: Mapped[list["RecipeBranchOutput"]] = relationship(
        "RecipeBranchOutput", back_populates="branch", cascade="all, delete-orphan",
        order_by="RecipeBranchOutput.symbol",
    )


class RecipeBranchOutput(Base):
    __tablename__ = "recipe_branch_outputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    branch_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipe_branches.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)

    branch: Mapped["RecipeBranch"] = relationship("RecipeBranch", back_populates="outputs")


class RecipeGoodRequirement(Base):
    """Present but not consumed: 'hold ≥ quantity of symbol while this runs'
    (machinery, tools). Checked at start, never consumed — and *reserved*: a
    symbol's requirements summed across an entity's running processes may not
    exceed its holding, and reserved quantities are unavailable to market
    settlement (you cannot sell the oven mid-bake)."""

    __tablename__ = "recipe_good_requirements"
    __table_args__ = (UniqueConstraint("recipe_id", "symbol"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="good_requirements")


class RecipeDepositInput(Base):
    """Extraction: quantity drawn from the bound parcel's matching deposit at
    start (MINE_IRON, FELL_TIMBER). Deposits deplete only through these."""

    __tablename__ = "recipe_deposit_inputs"
    __table_args__ = (UniqueConstraint("recipe_id", "symbol"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="deposit_inputs")


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

import logging
import os
import shutil
import tempfile
import traceback
from typing import List, Optional, Any
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select, func

# Constants and Configuration
from ..config import config_manager
from ..database import get_db
from ..models import (
    Sleep,
    Activity,
    Readiness,
    Resilience,
    SleepSession,
    Workout,
    Meditation,
    RingBattery,
    HeartRate,
    Temperature,
    RingConfiguration,
    Tag,
    CardiovascularAge,
)
from .schemas import DayDataResponse
from ..ingestion import OuraParser
from ..llm import DataAnalyst

# Logging
logger = logging.getLogger("API")

# Router Initialization
router = APIRouter()

# -----------------------------------------------------------------------------
# Data Models and request/response schemas
# -----------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    history: List[dict] = []


class Dashboard(BaseModel):
    id: str
    name: str
    widgets: List[Any]
    layout: List[Any]


class DashboardConfigRequest(BaseModel):
    dashboards: Optional[List[Dashboard]] = None
    activeDashboardId: Optional[str] = None
    layout: Optional[List[Any]] = None
    widgets: Optional[List[Any]] = None


class IngestRequest(BaseModel):
    file_path: str


# -----------------------------------------------------------------------------
# Chat / Advisor Endpoints
# -----------------------------------------------------------------------------


@router.post("/api/advisor/chat")
async def chat(request: ChatRequest):
    """
    Interacts with the AI Advisor (Claude tool-use analyst over the local DB).
    """
    try:
        logger.info("Incoming Chat Request.")
        advisor = DataAnalyst()

        # Append latest user message to history references
        full_history = request.history + [{"role": "user", "content": request.message}]

        response = advisor.chat(full_history)
        return response
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Dashboard Configuration Endpoints
# -----------------------------------------------------------------------------


@router.get("/api/dashboard")
async def get_dashboard_config():
    """Retrieves the saved dashboard layout and widgets."""
    try:
        config = config_manager.get_config()
        return config.get("dashboard", {"dashboards": [], "activeDashboardId": None})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/dashboard")
async def save_dashboard_config(request: DashboardConfigRequest):
    """Saves the dashboard configuration."""
    try:
        update_data = {}
        if request.dashboards is not None:
            update_data["dashboards"] = [d.dict() for d in request.dashboards]
        if request.activeDashboardId is not None:
            update_data["activeDashboardId"] = request.activeDashboardId

        # Legacy fallback
        if request.layout is not None:
            update_data["layout"] = request.layout
        if request.widgets is not None:
            update_data["widgets"] = request.widgets

        config_manager.update_config(dashboard=update_data)
        return {"message": "Dashboard saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Data Access Endpoints
# -----------------------------------------------------------------------------


@router.get("/api/days/{date_str}", response_model=DayDataResponse)
async def get_day_data(
    date_str: str, include_details: bool = False, db: Session = Depends(get_db)
):
    """
    Retrieves comprehensive data for a specific day (YYYY-MM-DD).
    Includes summary metrics and optional time-series details.
    """
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        # Fetch daily summaries
        sleep = db.query(Sleep).filter(Sleep.day == target_date).first()
        activity = db.query(Activity).filter(Activity.day == target_date).first()
        readiness = db.query(Readiness).filter(Readiness.day == target_date).first()
        resilience = db.query(Resilience).filter(Resilience.day == target_date).first()
        cv_age = (
            db.query(CardiovascularAge)
            .filter(CardiovascularAge.day == target_date)
            .first()
        )

        # Fetch detailed components
        sleep_sessions = (
            db.query(SleepSession).filter(SleepSession.day == target_date).all()
        )
        workouts = db.query(Workout).filter(Workout.day == target_date).all()
        sessions = db.query(Meditation).filter(Meditation.day == target_date).all()

        # Fetch Ring Battery
        start_of_day = datetime.combine(target_date, datetime.min.time())
        end_of_day = datetime.combine(target_date, datetime.max.time())
        battery = (
            db.query(RingBattery)
            .filter(
                RingBattery.timestamp >= start_of_day,
                RingBattery.timestamp <= end_of_day,
            )
            .order_by(RingBattery.timestamp)
            .all()
        )

        response_data = {
            "date": target_date,
            "sleep": sleep,
            "activity": activity,
            "readiness": readiness,
            "resilience": resilience,
            "cardiovascular_age": cv_age,
            "ring_battery": battery,
            "sleep_sessions": sleep_sessions,
            "workouts": workouts,
            "meditation": sessions,
        }

        if include_details:

            def fetch_timeseries(model):
                return db.scalars(
                    select(model)
                    .where(model.timestamp >= start_of_day)
                    .where(model.timestamp <= end_of_day)
                    .order_by(model.timestamp)
                ).all()

            response_data["heart_rate"] = fetch_timeseries(HeartRate)
            response_data["temperature"] = fetch_timeseries(Temperature)

        # Pydantic will validate and serialize
        return response_data

    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        )
    except Exception as e:
        logger.error(f"Error fetching day data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/query")
def query_data(
    path: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """
    Dynamic query endpoint for fetching specific metric trends over time.

    Path format:
    - 'domain.field' (e.g., 'sleep.score')
    - 'domain.json_col.key' (e.g., 'sleep.contributors.deep_training')

    Returns: List of {date: ..., value: ...}
    """
    try:
        parts = path.split(".")
        if len(parts) < 2:
            raise HTTPException(
                status_code=400,
                detail="Invalid path format. Use 'domain.field' or 'domain.field.key'",
            )

        domain = parts[0].lower()
        field = parts[1].lower()
        json_key = ".".join(parts[2:]) if len(parts) > 2 else None

        # Map domain name to SQLAlchemy Model
        model_map = {
            "sleep": Sleep,
            "activity": Activity,
            "readiness": Readiness,
            "resilience": Resilience,
            "cardiovascular_age": CardiovascularAge,
            "sleep_session": SleepSession,
            "workout": Workout,
            "meditation": Meditation,
            "ring_battery": RingBattery,
            "heart_rate": HeartRate,
            "temperature": Temperature,
            "ring_configuration": RingConfiguration,
            "tag": Tag,
        }

        model = model_map.get(domain)
        if not model:
            raise HTTPException(status_code=400, detail=f"Unknown domain: {domain}")

        # Validate against actual table columns (hasattr alone would also
        # match relationships/class attrs like 'metadata' or 'registry').
        if field not in model.__table__.columns:
            raise HTTPException(
                status_code=400, detail=f"Unknown field: {field} in {domain}"
            )

        column = getattr(model, field)

        # Construct Value Expression
        if json_key:
            # Extract value from JSON column
            value_expr = func.json_extract(column, f"$.{json_key}")
        else:
            value_expr = column

        # Determine Date Column (Day vs Timestamp)
        if domain in ["heart_rate", "temperature", "ring_battery"]:
            date_col = model.timestamp
        else:
            date_col = model.day if hasattr(model, "day") else model.timestamp

        query = select(date_col, value_expr).order_by(date_col)

        # Special filtering for Sleep Sessions
        if domain == "sleep_session":
            query = query.where(SleepSession.type.in_(["long_sleep", "sleep"]))
            query = query.order_by(date_col, SleepSession.type.desc())

        # Apply Date Filters
        if start_date:
            if (
                hasattr(date_col.type, "python_type")
                and date_col.type.python_type == datetime
            ):
                query = query.where(
                    date_col >= datetime.combine(start_date, datetime.min.time())
                )
            else:
                query = query.where(date_col >= start_date)

        if end_date:
            if (
                hasattr(date_col.type, "python_type")
                and date_col.type.python_type == datetime
            ):
                query = query.where(
                    date_col <= datetime.combine(end_date, datetime.max.time())
                )
            else:
                query = query.where(date_col <= end_date)

        results = db.execute(query).all()

        # Format Results
        data = []
        for row in results:
            day_val = row[0]
            val = row[1]

            if isinstance(day_val, datetime):
                day_val = day_val.isoformat()

            data.append({"date": day_val, "value": val})

        return data

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Query Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/schema")
def get_schema():
    """
    Introspects the database models to return a schema definition.
    Useful for the frontend to build dynamic selectors.
    """

    model_map = {
        "sleep": Sleep,
        "activity": Activity,
        "readiness": Readiness,
        "resilience": Resilience,
        "cardiovascular_age": CardiovascularAge,
        "sleep_session": SleepSession,
        "workout": Workout,
        "meditation": Meditation,
        "ring_battery": RingBattery,
        "heart_rate": HeartRate,
        "temperature": Temperature,
        "ring_configuration": RingConfiguration,
        "tag": Tag,
    }

    schema = {}

    try:
        for name, model in model_map.items():
            fields = []
            try:
                for col in model.__table__.columns:
                    if col.name == "id":
                        continue

                    # Naive check for JSON columns
                    is_json = False
                    try:
                        type_str = str(col.type).upper()
                        is_json = "JSON" in type_str
                    except Exception:
                        pass

                    fields.append(
                        {
                            "name": col.name,
                            "type": "json" if is_json else str(col.type),
                            "is_json": is_json,
                        }
                    )
            except Exception as e:
                logger.error(f"Error inspecting model {name}: {e}")
                continue  # Skip model if error

            schema[name] = fields

        return schema
    except Exception as e:
        logger.error(f"Schema Critical Error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Data Ingestion Endpoints (Uploads)
# -----------------------------------------------------------------------------


@router.post("/api/ingest/zip")
async def ingest_zip(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Endpoint for uploading and ingesting an Oura export ZIP file manually.
    """
    parser = OuraParser(db)
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp_file:
            shutil.copyfileobj(file.file, tmp_file)
            tmp_path = tmp_file.name

        logger.info(f"Received ZIP file, saved to {tmp_path}")

        parser.parse_zip(tmp_path)
        os.remove(tmp_path)

        return {"message": "Ingestion successful"}
    except Exception as e:
        logger.error(f"Ingestion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

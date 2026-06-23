from __future__ import annotations

import json
import os
import smtplib
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from email.message import EmailMessage
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .agent import CheapFlightsAgent
from .locations import get_database_url
from .models import MultiCitySegment, TripRequest
from .providers import provider_from_env


@dataclass(frozen=True)
class FareAlert:
    id: str
    request: TripRequest
    target_price_usd: int
    email: str | None
    active: bool
    current_price_usd: int | None
    lowest_price_usd: int | None
    airline: str | None
    booking_url: str | None
    status: str
    last_error: str | None
    created_at: datetime
    last_checked_at: datetime | None
    triggered_at: datetime | None


class FareAlertRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = get_database_url(database_url)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fare_alerts (
                    id UUID PRIMARY KEY,
                    request_json JSONB NOT NULL,
                    target_price_usd INTEGER NOT NULL CHECK (target_price_usd > 0),
                    email TEXT,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    current_price_usd INTEGER,
                    lowest_price_usd INTEGER,
                    airline TEXT,
                    booking_url TEXT,
                    status TEXT NOT NULL DEFAULT 'watching',
                    last_error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_checked_at TIMESTAMPTZ,
                    triggered_at TIMESTAMPTZ,
                    last_notified_price_usd INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fare_alerts_active "
                "ON fare_alerts(active, last_checked_at)"
            )

    def create(
        self,
        request: TripRequest,
        target_price_usd: int,
        email: str | None = None,
    ) -> FareAlert:
        alert_id = str(uuid.uuid4())
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO fare_alerts (
                    id, request_json, target_price_usd, email
                ) VALUES (%s, %s::jsonb, %s, %s)
                RETURNING *
                """,
                (
                    alert_id,
                    json.dumps(trip_request_to_payload(request)),
                    target_price_usd,
                    email or None,
                ),
            ).fetchone()
        return _alert_from_row(row)

    def list(self) -> list[FareAlert]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fare_alerts ORDER BY created_at DESC"
            ).fetchall()
        return [_alert_from_row(row) for row in rows]

    def get(self, alert_id: str) -> FareAlert | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM fare_alerts WHERE id = %s",
                (alert_id,),
            ).fetchone()
        return _alert_from_row(row) if row else None

    def list_active(self) -> list[FareAlert]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fare_alerts WHERE active = TRUE ORDER BY created_at"
            ).fetchall()
        return [_alert_from_row(row) for row in rows]

    def delete(self, alert_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM fare_alerts WHERE id = %s",
                (alert_id,),
            )
        return result.rowcount > 0

    def update_check(
        self,
        alert_id: str,
        *,
        current_price_usd: int | None,
        airline: str | None,
        booking_url: str | None,
        status: str,
        error: str | None = None,
        notified_price_usd: int | None = None,
    ) -> FareAlert:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE fare_alerts
                SET current_price_usd = %s,
                    lowest_price_usd = CASE
                        WHEN %s IS NULL THEN lowest_price_usd
                        WHEN lowest_price_usd IS NULL THEN %s
                        ELSE LEAST(lowest_price_usd, %s)
                    END,
                    airline = %s,
                    booking_url = %s,
                    status = %s,
                    last_error = %s,
                    last_checked_at = NOW(),
                    triggered_at = CASE
                        WHEN %s = 'triggered' THEN COALESCE(triggered_at, NOW())
                        ELSE triggered_at
                    END,
                    last_notified_price_usd = COALESCE(%s, last_notified_price_usd)
                WHERE id = %s
                RETURNING *
                """,
                (
                    current_price_usd,
                    current_price_usd,
                    current_price_usd,
                    current_price_usd,
                    airline,
                    booking_url,
                    status,
                    error,
                    status,
                    notified_price_usd,
                    alert_id,
                ),
            ).fetchone()
        if not row:
            raise ValueError("Fare alert was not found.")
        return _alert_from_row(row)

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self.database_url, row_factory=dict_row)
        except psycopg.Error as exc:
            raise RuntimeError(
                "Fare alerts could not connect to PostgreSQL. Check DATABASE_URL "
                "and confirm the database is running."
            ) from exc


class FareAlertChecker:
    def __init__(
        self,
        repository: FareAlertRepository | None = None,
        agent: CheapFlightsAgent | None = None,
    ) -> None:
        self.repository = repository or FareAlertRepository()
        self.agent = agent or CheapFlightsAgent(provider_from_env(require_live=True))

    def check(self, alert: FareAlert) -> FareAlert:
        try:
            result = self.agent.search(replace(alert.request, budget_usd=None))
            if not result.ranked_flights:
                return self.repository.update_check(
                    alert.id,
                    current_price_usd=None,
                    airline=None,
                    booking_url=None,
                    status="no_results",
                )

            offer = min(
                (ranked.offer for ranked in result.ranked_flights),
                key=lambda item: item.price_usd,
            )
            triggered = offer.price_usd <= alert.target_price_usd
            status = "triggered" if triggered else "watching"
            should_notify = (
                triggered
                and bool(alert.email)
                and (
                    alert.current_price_usd is None
                    or offer.price_usd < alert.current_price_usd
                    or alert.status != "triggered"
                )
            )
            notified_price = None
            if should_notify:
                send_alert_email(alert, offer.price_usd, offer.airline, offer.booking_url)
                notified_price = offer.price_usd
            return self.repository.update_check(
                alert.id,
                current_price_usd=offer.price_usd,
                airline=offer.airline,
                booking_url=offer.booking_url,
                status=status,
                notified_price_usd=notified_price,
            )
        except Exception as exc:
            return self.repository.update_check(
                alert.id,
                current_price_usd=None,
                airline=None,
                booking_url=None,
                status="error",
                error=str(exc)[:500],
            )

    def check_all(self) -> list[FareAlert]:
        return [self.check(alert) for alert in self.repository.list_active()]


def send_alert_email(
    alert: FareAlert,
    price_usd: int,
    airline: str,
    booking_url: str,
) -> None:
    host = os.getenv("SMTP_HOST")
    if not host or not alert.email:
        return
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM") or username
    if not sender:
        raise RuntimeError("SMTP_FROM or SMTP_USERNAME is required for alert email.")

    message = EmailMessage()
    message["Subject"] = (
        f"Fare alert: {alert.request.origin} to {alert.request.destination} is ${price_usd}"
    )
    message["From"] = sender
    message["To"] = alert.email
    message.set_content(
        f"{airline} is now ${price_usd}, at or below your "
        f"${alert.target_price_usd} target.\n\n{booking_url}"
    )

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if os.getenv("SMTP_STARTTLS", "true").lower() == "true":
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)


def trip_request_to_payload(request: TripRequest) -> dict[str, Any]:
    payload = asdict(request)
    payload["depart_date"] = request.depart_date.isoformat() if request.depart_date else None
    payload["return_date"] = request.return_date.isoformat() if request.return_date else None
    payload["multi_city_segments"] = [
        {
            "origin": segment.origin,
            "destination": segment.destination,
            "depart_date": segment.depart_date.isoformat(),
        }
        for segment in request.multi_city_segments
    ]
    return payload


def trip_request_from_payload(payload: dict[str, Any]) -> TripRequest:
    return TripRequest(
        origin=str(payload["origin"]),
        destination=str(payload["destination"]),
        depart_date=_parse_date(payload.get("depart_date")),
        return_date=_parse_date(payload.get("return_date")),
        passengers=int(payload.get("passengers") or 1),
        budget_usd=int(payload["budget_usd"]) if payload.get("budget_usd") is not None else None,
        max_stops=int(payload["max_stops"]) if payload.get("max_stops") is not None else None,
        include_bags=bool(payload.get("include_bags")),
        cabin_class=str(payload.get("cabin_class") or "economy"),
        trip_type=str(payload.get("trip_type") or "round_trip"),
        multi_city_segments=tuple(
            MultiCitySegment(
                origin=str(segment["origin"]),
                destination=str(segment["destination"]),
                depart_date=date.fromisoformat(str(segment["depart_date"])),
            )
            for segment in payload.get("multi_city_segments") or []
        ),
        flexible_dates=bool(payload.get("flexible_dates")),
        trip_duration_days=int(payload["trip_duration_days"])
        if payload.get("trip_duration_days") is not None
        else None,
        return_stopover=str(payload["return_stopover"])
        if payload.get("return_stopover")
        else None,
        stopover_days=int(payload["stopover_days"])
        if payload.get("stopover_days") is not None
        else None,
    )


def alert_to_payload(alert: FareAlert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "request": trip_request_to_payload(alert.request),
        "targetPriceUsd": alert.target_price_usd,
        "email": alert.email,
        "active": alert.active,
        "currentPriceUsd": alert.current_price_usd,
        "lowestPriceUsd": alert.lowest_price_usd,
        "airline": alert.airline,
        "bookingUrl": alert.booking_url,
        "status": alert.status,
        "lastError": alert.last_error,
        "createdAt": alert.created_at.isoformat(),
        "lastCheckedAt": alert.last_checked_at.isoformat() if alert.last_checked_at else None,
        "triggeredAt": alert.triggered_at.isoformat() if alert.triggered_at else None,
    }


def run_worker() -> None:
    interval = max(5, int(os.getenv("ALERT_CHECK_INTERVAL_MINUTES", "360")))
    checker = FareAlertChecker()
    while True:
        checked_at = datetime.now(timezone.utc).isoformat()
        alerts = checker.check_all()
        print(f"{checked_at}: checked {len(alerts)} fare alerts", flush=True)
        time.sleep(interval * 60)


def _parse_date(value: Any) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def _alert_from_row(row: dict[str, Any]) -> FareAlert:
    request_payload = row["request_json"]
    if isinstance(request_payload, str):
        request_payload = json.loads(request_payload)
    return FareAlert(
        id=str(row["id"]),
        request=trip_request_from_payload(request_payload),
        target_price_usd=int(row["target_price_usd"]),
        email=row["email"],
        active=bool(row["active"]),
        current_price_usd=row["current_price_usd"],
        lowest_price_usd=row["lowest_price_usd"],
        airline=row["airline"],
        booking_url=row["booking_url"],
        status=row["status"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        last_checked_at=row["last_checked_at"],
        triggered_at=row["triggered_at"],
    )


if __name__ == "__main__":
    run_worker()

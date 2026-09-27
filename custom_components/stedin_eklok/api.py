"""API client voor Stedin Eklok."""
from __future__ import annotations

import asyncio
import logging
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_URL = "https://eklok.nl/api/pricedetail"
DEFAULT_TIMEZONE = "Europe/Amsterdam"


class StedinEklokError(Exception):
    """Algemene exception voor Stedin Eklok API."""


class StedinEklokConnectionError(StedinEklokError):
    """Exception voor verbindingsfouten met Stedin Eklok API."""


class StedinEklokDataError(StedinEklokError):
    """Exception voor ongeldige data van Stedin Eklok API."""


class StedinEklokAPI:
    """API client voor Stedin Eklok.
    
    De Eklok API retourneert data met:
    - range: -100 (zeer goed/groen) tot +100 (zeer slecht/rood)
    - Negatieve waarden = goed moment om energie te gebruiken
    - Positieve waarden = slecht moment (piek)
    - Data in 5-minuut intervallen
    - Tijden in UTC
    """

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        time_zone: str | ZoneInfo | None = None,
        cache_ttl: timedelta = timedelta(hours=1),
    ) -> None:
        """Initialiseer de API client."""
        self._session = session
        self._cache_ttl = cache_ttl
        self._cached_items: list[dict] | None = None
        self._last_fetch: datetime | None = None

        if isinstance(time_zone, str):
            try:
                self._tz = ZoneInfo(time_zone)
            except ZoneInfoNotFoundError:
                _LOGGER.warning("Onbekende tijdzone '%s', terugvallen op %s", time_zone, DEFAULT_TIMEZONE)
                self._tz = ZoneInfo(DEFAULT_TIMEZONE)
        elif isinstance(time_zone, (timezone, ZoneInfo)):
            self._tz = time_zone
        else:
            self._tz = ZoneInfo(DEFAULT_TIMEZONE)

    async def get_data(self, force_refresh: bool = False) -> dict[str, Any]:
        """Haal alle data op van de API."""
        today = datetime.now(self._tz)
        tomorrow = today + timedelta(days=1)
        
        all_items = await self._fetch_all(force_refresh=force_refresh)
        today_data = self._filter_day(all_items, today)
        tomorrow_data = self._filter_day(all_items, tomorrow)
        
        _LOGGER.debug("Today data: %s items", len(today_data) if today_data else 0)
        _LOGGER.debug("Tomorrow data: %s items", len(tomorrow_data) if tomorrow_data else 0)
        
        # Analyseer de data
        today_analysis = self._analyze_day(today_data) if today_data else {}
        tomorrow_analysis = self._analyze_day(tomorrow_data) if tomorrow_data else {}
        
        # Bepaal huidige status
        current_status = self._get_current_status(today_data)
        
        return {
            "today": today_data,
            "tomorrow": tomorrow_data,
            "today_analysis": today_analysis,
            "tomorrow_analysis": tomorrow_analysis,
            "current_status": current_status,
            "last_update": datetime.now(self._tz).isoformat(),
        }

    async def _fetch_all(self, force_refresh: bool = False) -> list[dict]:
        """Haal alle ruwe data op van de API in één request."""
        now_utc = datetime.now(timezone.utc)
        if not force_refresh and self._cached_items is not None and self._last_fetch is not None:
            if now_utc - self._last_fetch < self._cache_ttl:
                _LOGGER.debug("Hergebruik gecachete Eklok data (leeftijd: %s)", now_utc - self._last_fetch)
                return self._cached_items

        if self._session is None:
            raise StedinEklokConnectionError("Geen aiohttp ClientSession geconfigureerd")

        try:
            _LOGGER.debug("Eklok API benaderd: %s", API_URL)
            async with self._session.get(
                API_URL, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
                
                # API retourneert {"data": [...]} structuur
                if isinstance(data, dict) and "data" in data:
                    raw_items = data["data"]
                elif isinstance(data, list):
                    raw_items = data
                else:
                    raise StedinEklokDataError(
                        f"Onverwacht dataformaat ontvangen van Eklok API: {type(data)}"
                    )

                self._cached_items = raw_items
                self._last_fetch = now_utc
                return self._cached_items
                
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
            _LOGGER.error("Fout bij ophalen van Eklok data: %s", err)
            raise StedinEklokConnectionError(
                f"Fout bij ophalen van Eklok data: {err}"
            ) from err
        except ValueError as err:
            _LOGGER.error("Fout bij parsen van JSON response: %s", err)
            raise StedinEklokDataError(
                f"Ongeldige JSON ontvangen van Eklok API: {err}"
            ) from err

    def _filter_day(self, items: list[dict] | None, date: datetime | date_type) -> list[dict] | None:
        """Filter data voor een specifieke dag."""
        if items is None:
            return None

        if isinstance(date, datetime):
            target_date = date.astimezone(self._tz).date() if date.tzinfo else date.date()
        elif isinstance(date, date_type):
            target_date = date
        else:
            return None

        matching_items = []

        for item in items:
            try:
                item_datetime = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
                local_datetime = item_datetime.astimezone(self._tz)

                # Compare local calendar date in the target timezone
                if local_datetime.date() == target_date:
                    matching_items.append(item)

            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Ongeldige datum in API-data: %r", item)

        return matching_items

    def _analyze_day(self, data: list[dict]) -> dict[str, Any]:
        """Analyseer de data van een dag.
        
        Range interpretatie:
        - range <= -30: Groen (goed moment)
        - range -30 tot +30: Oranje (neutraal)  
        - range >= +30: Rood (slecht moment)
        """
        if not data:
            return {}
        
        ranges = []
        green_moments = []
        orange_moments = []
        red_moments = []
        
        for item in data:
            range_val = item.get("range", 100)
            ranges.append(range_val)
            
            moment_info = {
                "date": item.get("date"),
                "range": range_val,
                "color": item.get("color", self._get_color(range_val)),
            }
            
            # Negatief = goed, Positief = slecht
            if range_val <= -30:
                green_moments.append(moment_info)
            elif range_val <= 30:
                orange_moments.append(moment_info)
            else:
                red_moments.append(moment_info)
        
        # Sorteer beste momenten (laagste/meest negatieve range eerst)
        all_moments = sorted(
            [{"date": d.get("date"), "range": d.get("range", 100)} for d in data],
            key=lambda x: x["range"]
        )
        
        # Groepeer per uur voor hourly_data
        hourly_data = self._aggregate_hourly(data)
        
        # Tel groene uren (uren waar gemiddelde <= -30)
        green_hours = sum(1 for h in hourly_data if h.get("range") is not None and h["range"] <= -30)
        
        return {
            "average_range": round(sum(ranges) / len(ranges), 1) if ranges else 100,
            "min_range": min(ranges) if ranges else 100,
            "max_range": max(ranges) if ranges else 100,
            "green_count": green_hours,  # Aantal groene uren
            "orange_count": sum(1 for h in hourly_data if h.get("range") is not None and -30 < h["range"] <= 30),
            "red_count": sum(1 for h in hourly_data if h.get("range") is not None and h["range"] > 30),
            "best_moments": all_moments[:5],  # Top 5 beste momenten
            "green_moments": green_moments[:10],  # Top 10 groene momenten
            "hourly_data": hourly_data,
            "raw_data_count": len(data),
        }

    def _aggregate_hourly(self, data: list[dict]) -> list[dict]:
        """Aggregeer 5-minuut data naar uur-data."""
        hourly = {}
        
        for item in data:
            try:
                dt_str = item.get("date", "")
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                local_dt = dt.astimezone(self._tz)
                hour = local_dt.hour
                
                if hour not in hourly:
                    hourly[hour] = {"ranges": [], "colors": []}
                
                hourly[hour]["ranges"].append(item.get("range", 100))
                hourly[hour]["colors"].append(item.get("color", "#ff0000"))
            except (ValueError, TypeError):
                continue
        
        result = []
        for hour in range(24):
            if hour in hourly and hourly[hour]["ranges"]:
                avg_range = sum(hourly[hour]["ranges"]) / len(hourly[hour]["ranges"])
                result.append({
                    "hour": hour,
                    "range": round(avg_range, 1),
                    "color": self._get_color(avg_range),
                })
            else:
                result.append({
                    "hour": hour,
                    "range": None,
                    "color": "gray",
                })
        
        return result

    def _get_current_status(self, today_data: list[dict] | None) -> dict[str, Any]:
        """Bepaal de huidige status op basis van het dichtstbijzijnde datapunt."""
        if not today_data:
            return {"status": "unknown", "range": 100, "color": "gray", "is_good_moment": False}
        
        now = datetime.now(self._tz)
        closest_item = None
        min_diff = timedelta(days=1)
        
        for item in today_data:
            try:
                dt_str = item.get("date", "")
                item_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(self._tz)
                diff = abs(now - item_dt)
                
                if diff < min_diff:
                    min_diff = diff
                    closest_item = item
            except (ValueError, TypeError):
                continue
        
        if closest_item:
            range_val = closest_item.get("range", 100)
            return {
                "status": "good" if range_val <= -30 else "moderate" if range_val <= 30 else "bad",
                "range": range_val,
                "color": closest_item.get("color", self._get_color(range_val)),
                "is_good_moment": range_val <= -30,
                "time": closest_item.get("date"),
            }
        
        return {"status": "unknown", "range": 100, "color": "gray", "is_good_moment": False}

    @staticmethod
    def _get_color(range_val: float) -> str:
        """Bepaal de kleur op basis van de range waarde.
        
        Eklok kleuren:
        - Groen (#00ff00): range <= -30 (goed moment)
        - Oranje: range -30 tot +30 (neutraal)
        - Rood (#ff0000): range >= +30 (slecht moment)
        """
        if range_val <= -30:
            return "green"
        elif range_val <= 30:
            return "orange"
        return "red"

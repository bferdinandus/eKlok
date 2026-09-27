"""Binary sensor platform voor Stedin Eklok integratie."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Stedin Eklok binary sensors vanuit een config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    binary_sensors = [
        StedinEklokGoodMomentBinarySensor(coordinator, entry),
    ]
    
    async_add_entities(binary_sensors)


class StedinEklokBinarySensorBase(CoordinatorEntity, BinarySensorEntity):
    """Basis binary sensor voor Stedin Eklok."""
    
    _attr_has_entity_name = True
    
    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialiseer de binary sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Stedin Eklok",
            manufacturer="Stedin",
            model="Eklok",
            configuration_url="https://eklok.nl",
        )


class StedinEklokGoodMomentBinarySensor(StedinEklokBinarySensorBase):
    """Binary sensor voor goed moment (Aan/Uit).
    
    Aan (True) = range <= -30 (goed moment, groen)
    Uit (False) = range > -30 (neutraal of slecht moment)
    """
    
    _attr_translation_key = "good_moment"
    
    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialiseer de sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_good_moment"
        self._attr_icon = "mdi:lightning-bolt"
    
    @property
    def is_on(self) -> bool:
        """Return of het nu een goed moment is."""
        if self.coordinator.data:
            current = self.coordinator.data.get("current_status", {})
            return bool(current.get("is_good_moment", False))
        return False
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributen."""
        if self.coordinator.data:
            current = self.coordinator.data.get("current_status", {})
            range_val = current.get("range", 100)
            return {
                "range": range_val,
                "color": current.get("color", "gray"),
                "status": current.get("status", "unknown"),
                "uitleg": "Negatieve range = goed moment, Positieve range = slecht moment",
            }
        return {}

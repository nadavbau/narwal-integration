"""Sensor entities for Narwal robot vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import NarwalState
from .narwal_client.error_codes import error_message
from .narwal_client.proto import enum_labels

CONSUMABLE_NAMES = {
    "CONSUMABLE_MAINTAIN_ITEM_DUST_BOX": "Clean the dust bin",
    "CONSUMABLE_MAINTAIN_ITEM_DUST_FILTER": "Clean the dust bin filter",
    "CONSUMABLE_MAINTAIN_ITEM_DIRTY_WATER": "Empty the dirty water tank",
    "CONSUMABLE_MAINTAIN_ITEM_WASH_RIBS": "Clean the base station tray",
    "CONSUMABLE_MAINTAIN_ITEM_UNIVERSAL_WHEEL": "Clean the caster wheel",
    "CONSUMABLE_MAINTAIN_ITEM_CLIFF_SENSOR": "Clean the sensors",
    "CONSUMABLE_MAINTAIN_ITEM_SIDE_DISTANCE_SENSOR": "Clean the sensors",
    "CONSUMABLE_MAINTAIN_ITEM_ANTI_WINDING_BRUSH": "Clean the roller brush",
    "CONSUMABLE_MAINTAIN_ITEM_WATER_TANK_SPONGE": "Clean the water tank filter sponge",
    "CONSUMABLE_MAINTAIN_ITEM_SMART_MODULE_SPONGE": "Clean the filter sponge",
    "CONSUMABLE_MAINTAIN_ITEM_DUST_CONTAINER": "Empty the dust bin / dust bag",
    "CONSUMABLE_REPLACE_ITEM_DUST_FILTER": "Replace the dust bin filter",
    "CONSUMABLE_REPLACE_ITEM_MOP": "Replace the mops",
    "CONSUMABLE_REPLACE_ITEM_SIDE_BRUSH": "Replace the side brush",
    "CONSUMABLE_REPLACE_ITEM_ROLLER_BRUSH": "Replace the roller brush",
    "CONSUMABLE_REPLACE_ITEM_DETERGENT": "Refill the detergent",
    "CONSUMABLE_REPLACE_ITEM_DUST_BAG": "Replace the dust bag",
}


def _slug(label: Any, prefix: str) -> str | None:
    if not isinstance(label, str):
        return None
    if label.startswith(prefix):
        label = label[len(prefix):]
    return label.lower()


def _enum_options(enum: str, prefix: str) -> list[str]:
    return [s for v, label in sorted(enum_labels(enum).items()) if v and (s := _slug(label, prefix))]


@dataclass(frozen=True, kw_only=True)
class NarwalSensorDescription(SensorEntityDescription):
    value_fn: Callable[[NarwalState], Any]
    attrs_fn: Callable[[NarwalState], dict[str, Any]] | None = None
    exists_fn: Callable[[NarwalState], bool] = lambda _: True


def _base_enum(key: str, field_name: str, enum: str, prefix: str, icon: str) -> NarwalSensorDescription:
    return NarwalSensorDescription(
        key=key,
        translation_key=key,
        icon=icon,
        device_class=SensorDeviceClass.ENUM,
        options=_enum_options(enum, prefix),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s, f=field_name, p=prefix: _slug(s.base.get(f), p),
    )


def _error_attrs(state: NarwalState) -> dict[str, Any]:
    return {
        "codes": [f"0x{c:08X}" for c in state.error_codes],
        "messages": [error_message(c) for c in state.error_codes],
        "reasons": state.error_reasons,
        "last_task_result": _slug(state.base.get("terminateReason"), "TASK_RESULT_"),
    }


def _consumable_attrs(state: NarwalState) -> dict[str, Any]:
    due = state.consumables_due
    items = due.get("maintain", []) + due.get("replace", [])
    return {"items": sorted({CONSUMABLE_NAMES.get(i, i) for i in items}), "raw": items}


SENSORS: tuple[NarwalSensorDescription, ...] = (
    NarwalSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.battery_level,
    ),
    NarwalSensorDescription(
        key="status",
        translation_key="status",
        icon="mdi:robot-vacuum",
        value_fn=lambda s: s.status_text,
        attrs_fn=lambda s: {"task": s.task, "sub_status": s.sub_status, "activity": s.activity},
    ),
    NarwalSensorDescription(
        key="error",
        translation_key="error",
        icon="mdi:alert-circle-outline",
        value_fn=lambda s: error_message(s.error_codes[0]) if s.error_codes else "No error",
        attrs_fn=_error_attrs,
    ),
    NarwalSensorDescription(
        key="elapsed_time",
        translation_key="elapsed_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda s: s.elapsed_time,
    ),
    NarwalSensorDescription(
        key="remaining_time",
        translation_key="remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda s: s.remaining_time if s.is_cleaning else None,
    ),
    NarwalSensorDescription(
        key="cleaned_area",
        translation_key="cleaned_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        value_fn=lambda s: s.cleaned_area,
    ),
    NarwalSensorDescription(
        key="progress",
        translation_key="progress",
        icon="mdi:progress-check",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.progress if s.is_cleaning else None,
    ),
    NarwalSensorDescription(
        key="current_room",
        translation_key="current_room",
        icon="mdi:floor-plan",
        value_fn=lambda s: next(
            (r.display_name for r in s.rooms if r.room_id == s.current_room_id), None
        ) if s.is_cleaning and s.current_room_id else None,
    ),
    NarwalSensorDescription(
        key="consumables_due",
        translation_key="consumables_due",
        icon="mdi:wrench-clock",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: len(_consumable_attrs(s)["items"]),
        attrs_fn=_consumable_attrs,
    ),
    _base_enum("clean_water_tank", "cleanWaterTankState", "RobotBaseStatus_CleanWaterTankState",
               "CLEAN_WATER_TANK_STATE_", "mdi:water"),
    _base_enum("dirty_water_tank", "sewageTankState", "RobotBaseStatus_SewageTankState",
               "SEWAGE_TANK_STATE_", "mdi:water-opacity"),
    _base_enum("dust_bin", "dustBoxState", "RobotBaseStatus_DustBoxState",
               "DUST_BOX_STATE_", "mdi:delete-variant"),
    _base_enum("detergent", "detergentState", "RobotBaseStatus_DetergentState",
               "DETERGENT_STATE_", "mdi:bottle-tonic-outline"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(NarwalSensor(coordinator, d) for d in SENSORS)


class NarwalSensor(NarwalEntity, SensorEntity):
    """A sensor computed from the vacuum state."""

    entity_description: NarwalSensorDescription

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.state_data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.state_data) if fn else None

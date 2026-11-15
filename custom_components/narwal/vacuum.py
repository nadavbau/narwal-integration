"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import CLEAN_MODE_MAP, FAN_SPEED_LIST, FAN_SPEED_MAP, FAN_SPEED_REVERSE, MOP_HUMIDITY_MAP
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity, entity_supported
from .narwal_client.error_codes import error_message
from .narwal_client import CleanMode, FanLevel, MopHumidity
from .narwal_client.models import (
    ACTIVITY_CLEANING,
    ACTIVITY_DOCKED,
    ACTIVITY_ERROR,
    ACTIVITY_IDLE,
    ACTIVITY_PAUSED,
    ACTIVITY_RETURNING,
)

try:  # Home Assistant 2026.3+: native "clean area" (rooms mapped to HA areas)
    from homeassistant.components.vacuum import Segment

    _CLEAN_AREA = VacuumEntityFeature.CLEAN_AREA
except ImportError:  # pragma: no cover - older Home Assistant
    Segment = None  # type: ignore[assignment,misc]
    _CLEAN_AREA = VacuumEntityFeature(0)

_LOGGER = logging.getLogger(__name__)

_ACTIVITY = {
    ACTIVITY_CLEANING: VacuumActivity.CLEANING,
    ACTIVITY_DOCKED: VacuumActivity.DOCKED,
    ACTIVITY_IDLE: VacuumActivity.IDLE,
    ACTIVITY_PAUSED: VacuumActivity.PAUSED,
    ACTIVITY_RETURNING: VacuumActivity.RETURNING,
    ACTIVITY_ERROR: VacuumActivity.ERROR,
}

# vacuum.send_command commands that map straight onto a client method.
_SIMPLE_COMMANDS = {
    "wash_mop": "wash_mop",
    "dry_mop": "dry_mop",
    "wash_and_dry_mop": "wash_and_dry_mop",
    "empty_dustbin": "empty_dustbin",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([NarwalVacuum(entry.runtime_data)])


class NarwalVacuum(NarwalEntity, StateVacuumEntity):
    """Representation of a Narwal robot vacuum."""

    _attr_translation_key = "narwal"
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.SEND_COMMAND
        | VacuumEntityFeature.MAP
        | _CLEAN_AREA
    )
    _attr_fan_speed_list = FAN_SPEED_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "vacuum")
        self._segments_issue_raised = False

    @property
    def activity(self) -> VacuumActivity:
        return _ACTIVITY.get(self.state_data.activity, VacuumActivity.IDLE)

    @property
    def fan_speed(self) -> str | None:
        level = self.state_data.fan_level
        if level and level == self.coordinator.client.max_fan_level:
            return "Max"
        return FAN_SPEED_REVERSE.get(level) if level else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.state_data
        attrs: dict[str, Any] = {
            "status": state.status_text,
            "rooms": {r.room_id: r.display_name for r in state.rooms},
            # Rooms marked no-go in the Narwal app; cleaning them is refused.
            "no_go_rooms": sorted(state.no_go_room_ids),
        }
        if state.current_room_id and state.is_cleaning:
            room = next((r for r in state.rooms if r.room_id == state.current_room_id), None)
            attrs["current_room"] = room.display_name if room else state.current_room_id
        if state.mop_humidity:
            attrs["mop_humidity"] = state.mop_humidity.name.lower()
        if state.error_codes:
            # Also here (not only on the error sensor) so automations
            # triggered by the vacuum turning to "error" see the cause:
            # the sensor's state is written after the vacuum's.
            code = state.error_codes[0]
            attrs["error_code"] = f"0x{code:08X}"
            reasons = state.error_reasons
            attrs["error_reason"] = reasons[0] if reasons else error_message(code)
        return attrs

    # ---- basic control -------------------------------------------------

    async def async_start(self) -> None:
        client = self.coordinator.client
        if self.state_data.is_paused:
            await self._run(client.resume())
        else:
            await self._run(client.start_clean(
                mode=self.coordinator.selected_clean_mode,
                passes=self.coordinator.clean_passes,
            ))
        await self.coordinator.async_request_refresh()

    async def async_stop(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.stop())
        await self.coordinator.async_request_refresh()

    async def async_pause(self) -> None:
        await self._run(self.coordinator.client.pause())
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.return_to_base())
        await self.coordinator.async_request_refresh()

    async def async_locate(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.locate())

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is None:
            raise ServiceValidationError(f"Unknown fan speed {fan_speed!r}; use one of {FAN_SPEED_LIST}")
        await self._run(self.coordinator.client.set_fan_speed(level))
        self.async_write_ha_state()

    # ---- rooms ---------------------------------------------------------

    async def async_get_segments(self) -> list[Any]:
        """Rooms for Home Assistant's area mapping (vacuum.clean_area)."""
        state = self.state_data
        if not state.rooms:
            await self.coordinator.client.fetch_rooms()
        return [
            Segment(id=str(r.room_id), name=r.display_name)
            for r in self.state_data.rooms
        ]

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        room_ids = [int(s) for s in segment_ids]
        await self._clean_rooms(room_ids)

    async def _clean_rooms(
        self,
        room_ids: list[int],
        mode: CleanMode | None = None,
        fan: FanLevel | None = None,
        mop: MopHumidity | None = None,
        passes: int | None = None,
    ) -> None:
        known = {r.room_id for r in self.state_data.rooms}
        unknown = [r for r in room_ids if known and r not in known]
        if unknown:
            raise ServiceValidationError(f"Unknown room id(s) {unknown}; known rooms: {sorted(known)}")
        await self._run(self.coordinator.client.start_clean(
            room_ids=room_ids,
            mode=mode or self.coordinator.selected_clean_mode,
            fan_level=fan,
            mop_humidity=mop,
            passes=passes or self.coordinator.clean_passes,
        ))
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._check_segments_changed()
        super()._handle_coordinator_update()

    @callback
    def _check_segments_changed(self) -> None:
        """Raise HA's repair issue when the robot's rooms change after mapping."""
        if Segment is None or self.registry_entry is None or self._segments_issue_raised:
            return
        last_seen = self.last_seen_segments
        rooms = self.state_data.rooms
        if last_seen is None or not rooms:
            return
        current = {(str(r.room_id), r.display_name) for r in rooms}
        if current != {(s.id, s.name) for s in last_seen}:
            self.async_create_segments_issue()
            self._segments_issue_raised = True

    # ---- send_command --------------------------------------------------

    async def async_send_command(
        self, command: str, params: dict[str, Any] | list[Any] | None = None, **kwargs: Any,
    ) -> None:
        """Extra commands.

        clean_rooms  params: rooms: [ids], mode: "Vacuum & Mop", fan_speed:
                     "Strong", mop_humidity: "wet", passes: 2 (all optional
                     except rooms)
        wash_mop | dry_mop | wash_and_dry_mop | empty_dustbin
        """
        p = params if isinstance(params, dict) else {}
        if command == "clean_rooms":
            rooms = p.get("rooms") or []
            if not rooms:
                raise ServiceValidationError("clean_rooms needs params.rooms (a list of room ids)")
            mode_name = p.get("mode")
            if mode_name is not None and mode_name not in CLEAN_MODE_MAP:
                raise ServiceValidationError(f"Unknown mode {mode_name!r}; use one of {list(CLEAN_MODE_MAP)}")
            fan_name = p.get("fan_speed")
            if fan_name is not None and fan_name not in FAN_SPEED_MAP:
                raise ServiceValidationError(f"Unknown fan_speed {fan_name!r}; use one of {FAN_SPEED_LIST}")
            mop_name = p.get("mop_humidity")
            if mop_name is not None and mop_name not in MOP_HUMIDITY_MAP:
                raise ServiceValidationError(f"Unknown mop_humidity {mop_name!r}; use dry, normal or wet")
            await self._clean_rooms(
                [int(r) for r in rooms],
                mode=CLEAN_MODE_MAP.get(mode_name) if mode_name else None,
                fan=FAN_SPEED_MAP.get(fan_name) if fan_name else None,
                mop=MOP_HUMIDITY_MAP.get(mop_name) if mop_name else None,
                passes=int(p["passes"]) if p.get("passes") else None,
            )
        elif command in _SIMPLE_COMMANDS:
            if not entity_supported(self.coordinator, command):
                raise ServiceValidationError(f"{command} is not supported by this robot's base station")
            await self._run(getattr(self.coordinator.client, _SIMPLE_COMMANDS[command])())
        else:
            raise ServiceValidationError(
                f"Unknown command {command!r}; supported: clean_rooms, {', '.join(_SIMPLE_COMMANDS)}"
            )

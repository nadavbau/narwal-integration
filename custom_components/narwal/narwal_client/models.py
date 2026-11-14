"""Data models for Narwal vacuum state."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .error_codes import ERROR_MESSAGES
from .const import ROOM_SUB_TYPE_NAMES, SUCCESS_RESULTS, FanLevel, MopHumidity
from .proto import ProtoError, decode

_LOGGER = logging.getLogger(__name__)

# Activity values (mirror Home Assistant's VacuumActivity strings).
ACTIVITY_CLEANING = "cleaning"
ACTIVITY_DOCKED = "docked"
ACTIVITY_IDLE = "idle"
ACTIVITY_PAUSED = "paused"
ACTIVITY_RETURNING = "returning"
ACTIVITY_ERROR = "error"

# Top-level task types that mean "the robot is out cleaning".
_CLEANING_TASKS = frozenset({
    "TASK_TYPE_SWEEP",
    "TASK_TYPE_MOP",
    "TASK_TYPE_SWEEP_THEN_MOP",
    "TASK_TYPE_SWEEP_AND_MOP",
    "TASK_TYPE_SMART_CLEAN",
    "TASK_TYPE_COMPOSITE_CLEAN",
    "TASK_TYPE_MAINTIAN_FLOOR",
    "TASK_TYPE_BUILD_MAP",
    "TASK_TYPE_UPDATE_MAP",
    "TASK_TYPE_DEPARTURE",
    "TASK_TYPE_RELOCALIZATION",
})
# Station-side tasks: the robot is on the dock doing maintenance.
_DOCK_TASKS = frozenset({
    "TASK_TYPE_WASH_AND_DRY_MOP",
    "TASK_TYPE_DUST_GATHERING",
    "TASK_TYPE_DRY_STATION_BAG",
    "TASK_TYPE_PUMP_TROUBLESHOOTING",
    "TASK_TYPE_ROBOT_SEWAGE_BOX_PUMP_TROUBLESHOOTING",
})
_RETURN_TASKS = frozenset({"TASK_TYPE_RECALL", "TASK_TYPE_PARKING"})

_FRIENDLY_IDLE = {
    "IDLE_STATUS_IN_STATION": "Docked",
    "IDLE_STATUS_LOW_POWER_IN_STATION": "Sleeping on dock",
    "IDLE_STATUS_OUTSIDE_STATION": "Idle",
    "IDLE_STATUS_LOW_POWER_OUTSIDE_STATION": "Sleeping off dock",
    "IDLE_STATUS_UNINSTALLED_CLEAN_MODULE": "Cleaning module missing",
    "IDLE_STATUS_UNINSTALLED_SWEEP_MODULE": "Vacuum module missing",
    "IDLE_STATUS_UNINSTALLED_MOP_MODULE": "Mop module missing",
}
_TASK_NAMES = {
    "TASK_TYPE_SWEEP": "Vacuuming",
    "TASK_TYPE_MOP": "Mopping",
    "TASK_TYPE_SWEEP_THEN_MOP": "Vacuum then mop",
    "TASK_TYPE_SWEEP_AND_MOP": "Vacuum and mop",
    "TASK_TYPE_SMART_CLEAN": "Smart clean",
    "TASK_TYPE_RECALL": "Returning to base",
    "TASK_TYPE_WASH_AND_DRY_MOP": "Mop care",
    "TASK_TYPE_DUST_GATHERING": "Emptying dust bin",
    "TASK_TYPE_DRY_STATION_BAG": "Drying dust bag",
    "TASK_TYPE_BUILD_MAP": "Mapping",
    "TASK_TYPE_UPDATE_MAP": "Updating map",
    "TASK_TYPE_UPGRADE": "Updating firmware",
    "TASK_TYPE_RELOCALIZATION": "Relocating",
}
_IN_STATION_IDLE = frozenset({"IDLE_STATUS_IN_STATION", "IDLE_STATUS_LOW_POWER_IN_STATION"})
_CONTACTED = frozenset({"STATION_CONTACT_TYPE_CONTACTED", "STATION_CONTACT_TYPE_CHARGING"})


def _pretty(label: Any, prefix: str = "") -> str:
    """'SWEEP_AND_MOP_STATUS_ROOM_CLEAN' -> 'Room clean' (prefix stripped)."""
    if not isinstance(label, str):
        return str(label) if label is not None else ""
    if prefix and label.startswith(prefix):
        label = label[len(prefix):]
    return label.replace("_", " ").capitalize()


def strip_narwal_frame(raw: bytes) -> bytes:
    """Drop the 0x01 + varint(len) + auth-protobuf header; return the message."""
    if len(raw) < 2 or raw[0] != 0x01:
        return raw
    length = shift = 0
    pos = 1
    while pos < len(raw):
        byte = raw[pos]
        pos += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return raw[pos + length:]


@dataclass
class CommandResponse:
    """Parsed command response."""

    command: str
    raw: bytes = b""
    message: dict[str, Any] = field(default_factory=dict)
    result: str | int | None = None

    @property
    def success(self) -> bool:
        # Read-only commands (get_feature_list, get_device_info) carry no
        # result field at all; treat a missing result as success.
        return self.result is None or self.result in SUCCESS_RESULTS or self.result == 1

    @classmethod
    def parse(cls, command: str, raw: bytes, message_type: str | None) -> CommandResponse:
        body = strip_narwal_frame(raw)
        resp = cls(command=command, raw=body)
        if message_type:
            try:
                resp.message = decode(message_type, body)
            except ProtoError:
                _LOGGER.debug("Could not decode %s as %s: %s", command, message_type, body.hex())
        resp.result = resp.message.get("result")
        return resp


@dataclass
class RoomInfo:
    """A room from the vacuum's map (``StaticMapPayload.roomInfos``)."""

    room_id: int
    room_sub_type: int = 0
    name: str = ""
    category: int = 0
    instance_index: int = 0

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        base = ROOM_SUB_TYPE_NAMES.get(self.room_sub_type, f"Room {self.room_id}")
        if self.instance_index > 1:
            return f"{base} {self.instance_index}"
        return base


@dataclass
class Pose:
    """Position in map grid-cell units and heading in radians."""

    x: float
    y: float
    theta: float = 0.0

    @classmethod
    def from_msg(cls, msg: dict[str, Any] | None) -> Pose | None:
        if not isinstance(msg, dict):
            return None
        loc = msg.get("location") or {}
        if "x" not in loc and "y" not in loc:
            return None
        return cls(float(loc.get("x", 0.0)), float(loc.get("y", 0.0)), float(msg.get("theta", 0.0)))


@dataclass
class MapSnapshot:
    """The static map plus enough geometry to place the robot on it."""

    map_id: int
    width: int
    height: int
    resolution_mm: int
    left: int
    bottom: int
    grid: bytes
    rotate_angle: int = 0
    station: Pose | None = None
    robot: Pose | None = None
    fetched_at: float = field(default_factory=time.time)

    def to_pixel(self, pose: Pose) -> tuple[float, float]:
        """Map coords -> grid pixel (origin bottom-left, before any flip).

        Poses are in grid-cell units already (verified: the docked robot lands
        ~4 cells from a wall); only the border offset is applied.
        """
        return pose.x - self.left, pose.y - self.bottom


@dataclass
class NarwalState:
    """Current vacuum state assembled from pushes and command responses."""

    device_reachable: bool = False
    base: dict[str, Any] = field(default_factory=dict)  # RobotBaseStatus
    working: dict[str, Any] = field(default_factory=dict)  # WorkingStatus
    config: dict[str, Any] = field(default_factory=dict)  # GetConfig_Response.config
    features: dict[str, Any] = field(default_factory=dict)
    consumables_due: dict[str, list[str]] = field(default_factory=dict)
    firmware_version: str = ""
    rooms: list[RoomInfo] = field(default_factory=list)
    map: MapSnapshot | None = None
    robot_pose: Pose | None = None
    current_room_id: int | None = None
    no_go_room_ids: set[int] = field(default_factory=set)
    preferred_fan_level: FanLevel | None = None
    preferred_mop_humidity: MopHumidity | None = None
    last_base_update: float = 0.0

    # ---- update paths -------------------------------------------------

    def update_base_status(self, base: dict[str, Any]) -> None:
        prev = self.activity
        self.base = base
        self.last_base_update = time.time()
        if self.activity != prev:
            _LOGGER.info(
                "Narwal activity %s -> %s (task=%s, sub=%s)",
                prev, self.activity, self.task, self.sub_status,
            )

    def update_working_status(self, working: dict[str, Any]) -> None:
        self.working = working
        zone = working.get("cleaningZoneId")
        if isinstance(zone, int) and zone:
            self.current_room_id = zone

    def update_display_map(self, display: dict[str, Any]) -> None:
        pose = Pose.from_msg(display.get("robot"))
        if pose:
            self.robot_pose = pose
        zone = display.get("currentCleanZoneId")
        if isinstance(zone, int):
            self.current_room_id = zone or None

    def update_rooms_from_map(self, static_map: dict[str, Any]) -> None:
        rooms: list[RoomInfo] = []
        for info in static_map.get("roomInfos", []):
            if not isinstance(info, dict) or "roomId" not in info:
                continue
            rooms.append(RoomInfo(
                room_id=int(info["roomId"]),
                room_sub_type=int(info.get("type", 0) or 0),
                name=str(info.get("customName", "") or ""),
                instance_index=int(info.get("roomTypeId", 0) or 0),
            ))
        if rooms:
            self.rooms = sorted(rooms, key=lambda r: r.room_id)
        # Whole rooms marked no-go for both vacuum and mop; the robot refuses
        # any task that includes them, so a full-house clean must skip them.
        self.no_go_room_ids = {
            int(f["roomId"])
            for f in static_map.get("forbiddens", [])
            if isinstance(f, dict)
            and f.get("mode") == "FORBIDDEN_MODE_ROOM"
            and f.get("type") in ("FORBIDDEN_TYPE_SWEEP_MOP", "FORBIDDEN_TYPE_UNSPECIFIED", None)
            and f.get("roomId")
        }

    # ---- derived properties -------------------------------------------

    @property
    def task_status(self) -> dict[str, Any]:
        status = self.base.get("robotTaskStatus")
        return status if isinstance(status, dict) else {}

    @property
    def task(self) -> str:
        task = self.task_status.get("task")
        return task if isinstance(task, str) else "TASK_TYPE_UNSPECIFIED"

    @property
    def sub_status(self) -> str:
        """The task-specific sub-status label (e.g. SWEEP_STATUS_ROOM_CLEAN)."""
        for key, value in self.task_status.items():
            if key != "task" and isinstance(value, str):
                return value
        return ""

    @property
    def battery_level(self) -> float | None:
        value = self.base.get("batteryPercentage")
        if isinstance(value, (int, float)):
            return max(0.0, min(100.0, float(value)))
        return None

    @property
    def reported_error_codes(self) -> list[int]:
        """Every code the robot reports, including ones the app never shows."""
        codes = []
        for err in self.base.get("errorCode", []) or []:
            if isinstance(err, dict) and err.get("identityCode"):
                codes.append(int(err["identityCode"]))
        return codes

    @property
    def error_codes(self) -> list[int]:
        """Errors the Narwal app would show: codes in its table for this model.

        The robot also reports internal notices (seen live: 0x01010072, level
        1, mid-clean with everything normal) that the app has no message for
        and doesn't display; those must not raise errors in HA.
        """
        return [c for c in self.reported_error_codes if c in ERROR_MESSAGES]

    @property
    def hidden_error_codes(self) -> list[int]:
        return [c for c in self.reported_error_codes if c not in ERROR_MESSAGES]

    @property
    def on_station(self) -> bool:
        return (
            self.base.get("stationContactType") in _CONTACTED
            or self.sub_status in _IN_STATION_IDLE
        )

    @property
    def is_charging(self) -> bool:
        if self.base.get("stationContactType") == "STATION_CONTACT_TYPE_CHARGING":
            return True
        return self.on_station and self.base.get("chargingStatus") == "CHG_STATUS_CHARGING"

    @property
    def activity(self) -> str:
        if not self.base:
            return ACTIVITY_IDLE
        if self.error_codes and self.task in ("TASK_TYPE_IDLE", "TASK_TYPE_UNSPECIFIED"):
            return ACTIVITY_ERROR
        task, sub = self.task, self.sub_status
        if (
            self.task_status.get("pauseStatus")
            or sub.endswith("_WAITING_COMMAND")
            or task == "TASK_TYPE_WAITING_COMMAND"
        ):
            return ACTIVITY_PAUSED
        if task in _RETURN_TASKS:
            return ACTIVITY_RETURNING
        if task in _CLEANING_TASKS:
            if "RECALL" in sub or "BACK_TO_STATION" in sub or sub.endswith("_PARKING"):
                return ACTIVITY_RETURNING
            return ACTIVITY_CLEANING
        if task in _DOCK_TASKS:
            return ACTIVITY_DOCKED
        if task == "TASK_TYPE_IDLE":
            return ACTIVITY_DOCKED if self.on_station else ACTIVITY_IDLE
        return ACTIVITY_DOCKED if self.on_station else ACTIVITY_IDLE

    @property
    def is_paused(self) -> bool:
        return self.activity == ACTIVITY_PAUSED

    @property
    def is_cleaning(self) -> bool:
        return self.task in _CLEANING_TASKS

    @property
    def status_text(self) -> str:
        """Readable status similar to the app, e.g. 'Sweep and mop: room clean'."""
        if not self.device_reachable:
            return "Offline"
        if not self.base:
            return "Unknown"
        sub = self.sub_status
        if self.task == "TASK_TYPE_IDLE":
            if self.is_charging:
                return "Charging"
            if sub in _FRIENDLY_IDLE:
                return _FRIENDLY_IDLE[sub]
        task = _TASK_NAMES.get(self.task) or _pretty(self.task, "TASK_TYPE_")
        if sub:
            prefix = sub.rsplit("_STATUS_", 1)[0] + "_STATUS_" if "_STATUS_" in sub else ""
            return f"{task}: {_pretty(sub, prefix).lower()}"
        return task

    @property
    def fan_level(self) -> FanLevel | None:
        """Suction: the robot's live value while cleaning, else the level the
        next clean will use (a choice made in HA while docked wins)."""
        reported = _enum_from_label(FanLevel, self.base.get("fanLevel"), "FAN_LEVEL_", {"MUTE": "QUIET", "SUPER": "MAX"})
        if self.is_cleaning:
            return reported
        return self.preferred_fan_level or reported

    @property
    def mop_humidity(self) -> MopHumidity | None:
        reported = _enum_from_label(MopHumidity, self.base.get("mopHumidity"), "MOP_HUMIDITY_")
        if self.is_cleaning:
            return reported
        return self.preferred_mop_humidity or reported

    @property
    def cleaned_area(self) -> float | None:
        """Area covered in the current/last task, m²."""
        value = self.working.get("coveredArea")
        return round(float(value), 1) if isinstance(value, (int, float)) else None

    @property
    def elapsed_time(self) -> int | None:
        value = self.working.get("timeConsuming")
        return int(value) if isinstance(value, int) else None

    @property
    def remaining_time(self) -> int | None:
        value = self.working.get("remainedTime")
        return int(value) if isinstance(value, int) else None

    @property
    def progress(self) -> float | None:
        value = self.working.get("workingProgress")
        if not isinstance(value, (int, float)):
            return None
        # The robot reports 0..1 on some firmware and 0..100 on others.
        return round(value * 100 if value <= 1.0 else value, 1)


def _enum_from_label(enum_cls, label: Any, prefix: str, aliases: dict[str, str] | None = None):
    if not isinstance(label, str) or not label.startswith(prefix):
        return None
    name = label[len(prefix):]
    name = (aliases or {}).get(name, name)
    try:
        member = enum_cls[name]
    except KeyError:
        return None
    return None if member.value == 0 else member

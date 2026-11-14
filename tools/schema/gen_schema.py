import json,sys
SCH=json.load(open(sys.argv[1])); EN=json.load(open(sys.argv[2])); out=sys.argv[3]
ROOTS=["GetDeviceBaseStatus_Response","RobotBaseStatus","WorkingStatus","GetConfig_Response","GetConsumableInfo_Response",
 "GetFeatureList_Response","StartClean_Request","StartClean_Response","SetConfig_Request","SetConfig_Response",
 "SetFanLevel_Request","SetFanLevel_Response","SetMopHumidity_Request","SetMopHumidity_Response","ResetConsumableInfo_Request",
 "PauseTask_Request","ResumeTask_Request","ForceEndTask_Request","GetMap_Request","GetMap_Response","DisplayMap",
 "GetCleanSchedules_Response","GetFirmwareVersion_Response","OTAUpgradeStatus","ActiveRobotPublish_Request",
 "ActiveRobotPublish_Response","NotifyAppEvent_Request","NotifyAppEvent_Response"]
STOP={"VisionData","RobotTrajectory","RobotTrajectoryInfo","RobotTrajectoryWithInfo","RobotTrajectoryArc","IncrementalMapData",
 "EntireMapData","VideoCruisePointData","PetHeatArea","ThresholdData","Window","RoomContour","ReducedMap","Carpet","UpdateMapRegion",
 "MapCenterAlign","ZoneInfo","Door","SmartCleanParam","MapConfiguration","WakeUpWord","AIInfo","DebugOption",
 "MaintainFloorItem","RobotTaskStatusV2","RobotTaskStatusExternalV2","ValleyCharging","VideoCruiseStatus","SearchPetStatus","WaitingUserStatus"}
msgs={}; enums=set(); todo=list(ROOTS)
while todo:
    m=todo.pop()
    if m in msgs or m not in SCH: continue
    fl=SCH[m]["fields"]; msgs[m]=fl
    for f in fl:
        t=f["type"].replace("repeated ",""); r=f["ref"]
        if t=="enum" and r: enums.add(r)
        if t=="message" and r and r not in STOP: todo.append(r)
L=['"""Narwal MQTT protobuf schema (subset).','',
'Field numbers, types and enum values recovered from the official Narwal',
'Android app (com.narwal.pita_global 2.11.90). Generated - edit the generator,',
'not this file. Messages not listed here decode as raw bytes.','"""','','from __future__ import annotations','',
'# message -> {field_number: (name, type, ref, repeated)}','MESSAGES: dict[str, dict[int, tuple[str, str, str | None, bool]]] = {']
for m in sorted(msgs):
    L.append(f"    {m!r}: {{")
    for f in msgs[m]:
        if f["n"] is None or f["type"]=="?": continue
        t=f["type"].replace("repeated ",""); rep=f["type"].startswith("repeated")
        ref=f["ref"] if t in("enum","message") else None
        if t=="message" and ref in STOP: t,ref="bytes",None
        L.append(f"        {f['n']}: ({f['name']!r}, {t!r}, {ref!r}, {rep}),")
    L.append("    },")
L+=["}","","# enum -> {value: label}","ENUMS: dict[str, dict[int, str]] = {"]
for e in sorted(enums):
    if e not in EN: continue
    L.append(f"    {e!r}: {{"+", ".join(f"{int(k)}: {v!r}" for k,v in EN[e].items())+"},")
L+=["}",""]
open(out,"w").write("\n".join(L)); print(len(msgs),"messages",len(enums),"enums")

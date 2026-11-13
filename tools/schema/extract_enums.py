"""Extract protobuf enum values from blutter's object dump (objs.txt).

Usage: python extract_enums.py <blutter_out>/objs.txt enums.json [schema.proto]

Each ProtobufEnum constant appears in objs.txt as
    Obj!FanLevel@... : { Super!ProtobufEnum : { off_8: int(0x3), off_10: "FAN_LEVEL_STRONG" } }
If schema.proto is given, the enums are appended to it in .proto syntax.
"""

import json
import re
import sys
from collections import defaultdict

PATTERN = re.compile(
    r'Obj!(\w+)@[0-9a-f]+ : \{\n  Super!ProtobufEnum : \{\n'
    r'    off_8: int\((0x[0-9a-f]+|-?\d+)\),\n    off_10: "([^"]+)"'
)


def main() -> None:
    text = open(sys.argv[1], errors="replace").read()
    enums: dict[str, dict[int, str]] = defaultdict(dict)
    for name, value, label in PATTERN.findall(text):
        enums[name][int(value, 0)] = label
    ordered = {k: dict(sorted(v.items())) for k, v in sorted(enums.items())}
    with open(sys.argv[2], "w") as out:
        json.dump(ordered, out, indent=1)
    if len(sys.argv) > 3:
        with open(sys.argv[3], "a") as proto:
            for name, values in ordered.items():
                body = "".join(f"  {label} = {num};\n" for num, label in values.items())
                proto.write(f"enum {name} {{\n{body}}}\n\n")
    print(f"{len(ordered)} enums")


if __name__ == "__main__":
    main()

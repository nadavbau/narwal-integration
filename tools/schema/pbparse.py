"""Rebuild protobuf message schemas from blutter asm of Dart *.pb.dart files."""
import re, sys, os, json
BASE=0
T={0x10:"bool",0x20:"bytes",0x40:"string",0x80:"double",0x100:"float",0x200:"enum",0x400:"group",0x800:"int32",0x1000:"int64",
   0x2000:"sint32",0x4000:"sint64",0x8000:"uint32",0x10000:"uint64",0x20000:"fixed32",0x40000:"fixed64",0x80000:"sfixed32",0x100000:"sfixed64",0x200000:"message",0x400000:"map"}
SHORT={"aOS":(0x40,False),"aOB":(0x10,False),"aInt64":(0x1000,False),"aOM":(0x200000,False),"pPS":(0x40,True),"pc":(None,True),"aE":(0x200,False),"e":(None,False),"a":(None,False),"m":(0x400000,True),"aI":(0x800,False),"aQM":(0x200000,False)}
def tname(code):
    if code is None: return "?"
    rep = "repeated " if code & 2 else ""
    base = code & ~0x7
    return rep + T.get(base, hex(code))
re_cls=re.compile(r"^class (\w+) extends GeneratedMessage")
re_int=re.compile(r"//\s+0x[0-9a-f]+: r\d+ = (-?\d+)$")
re_str=re.compile(r'//\s+0x[0-9a-f]+: r\d+ = "([^"]*)"$')
re_ta=re.compile(r"r16 = <(\w+)>$")
re_call=re.compile(r"r0 = (\w+)\(\)$")
re_bi=re.compile(r"static BuilderInfo _i\(\)")
def parse_file(path):
    out={}; cls=None; inbi=False; pending=[]; ta=None
    for line in open(path, errors="replace"):
        line=line.rstrip()
        m=re_cls.match(line)
        if m: cls=m.group(1); inbi=False; continue
        if re_bi.search(line): inbi=True; fields=out.setdefault(cls,[]); pending=[]; ta=None; continue
        if not inbi: continue
        if line.startswith("  }") and not line.startswith("  }}"): inbi=False; continue
        m=re_ta.search(line)
        if m: ta=m.group(1); continue
        m=re_int.search(line)
        if m: pending.append(("int",int(m.group(1)))); continue
        m=re_str.search(line)
        if m: pending.append(("str",m.group(1))); continue
        m=re_call.search(line)
        if m:
            fn=m.group(1)
            if fn in ("BuilderInfo","InitLateStaticField"): pending=[]; ta=None; continue
            # pattern: int(fieldnum), str(name), [int(type)]
            num=name=code=None
            for i,(k,v) in enumerate(pending):
                if k=="str" and i>0 and pending[i-1][0]=="int": num=pending[i-1][1]; name=v; rest=pending[i+1:]; 
            if name is not None:
                ints=[v for k,v in rest if k=="int"]
                if fn in SHORT and SHORT[fn][0] is not None: code=SHORT[fn][0] | (2 if SHORT[fn][1] else 0)
                elif ints: code=ints[0]
                if fn=="pc" and ints: code=ints[0]
                fields.append({"n":num,"name":name,"type":tname(code),"ref":ta if (code and (code&~7) in (0x200,0x200000,0x400000)) or fn in("aOM","pc","aE","e","m") else None,"fn":fn})
            pending=[]; ta=None
    return out
root=sys.argv[1]; found=[]
for d,_,fs in os.walk(root):
    for f in fs:
        if f.endswith(".pb.dart"):
            rel=os.path.relpath(os.path.join(d,f),root)
            for c,fl in parse_file(os.path.join(d,f)).items(): found.append((c,rel,fl))
# Several files define a message with the same name (e.g. CleanParam in
# CleanTask.pb.dart and ReportBaseTypes.pb.dart). Qualify duplicates with
# their file stem and resolve references to the same-file definition.
from collections import Counter
dup={c for c,n in Counter(c for c,_,_ in found).items() if n>1}
stem=lambda rel: os.path.basename(rel).split(".")[0]
names={(c,rel): (f"{c}__{stem(rel)}" if c in dup else c) for c,rel,_ in found}
allm={}
for c,rel,fl in found:
    for fd in fl:
        r=fd.get("ref")
        if r in dup:
            same=[names[(x,rr)] for x,rr,_ in found if x==r and rr==rel]
            fd["ref"]=same[0] if same else next(names[(x,rr)] for x,rr,_ in found if x==r)
    allm[names[(c,rel)]]={"file":rel,"fields":fl}
json.dump(allm,open(sys.argv[2],"w"),indent=1)
with open(sys.argv[3],"w") as o:
    for c in sorted(allm):
        o.write(f"message {c}  // {allm[c]['file']}\n")
        for fd in allm[c]["fields"]:
            o.write(f"  {fd['type']}{' '+fd['ref'] if fd['ref'] else ''} {fd['name']} = {fd['n']};\n")
        o.write("\n")
print(len(allm),"messages,",sum(len(v['fields']) for v in allm.values()),"fields")

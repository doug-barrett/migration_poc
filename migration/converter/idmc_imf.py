"""
idmc_imf.py  --  Low-level readers for Informatica IDMC/IICS export assets.

An IDMC export package contains one directory per asset:
  *.DTEMPLATE/   -- a mapping *template*  (the transformation logic / DAG)
      mappingTemplate.json   -- wrapper metadata
      metadata.meta
      bin/@N.bin             -- the IMFOBJECT: the real mapping (JSON)
  *.MTT.zip      -- a mapping *task*       (binds template params to concrete
                                            connections/objects at runtime)
      mtTask.json

The IMFOBJECT (bin/@N.bin) is JSON using Informatica's internal object-identity
graph:  every object may carry an integer "$$ID"; references to it appear as
{"##ID": n} (object ref) or {"##SID": "smd:..."} (seed/type ref).  This module
loads that graph and resolves the references.

This file has NO Coalesce knowledge -- it only decodes Informatica assets into
plain Python dicts.  idmc_model.py turns that into a migration IR, and
idmc_emit.py writes Coalesce YAML.
"""
from __future__ import annotations
import json
import os
import zipfile


# ---- IMF class ids (from metadata.$$classInfo) -----------------------------
CLS_TEMPLATE   = 1
CLS_LINK       = 4
CLS_GROUP      = 5
CLS_EXPRESSION = 6
CLS_TARGET     = 7
CLS_SOURCE     = 8
CLS_PARAM      = 9
CLS_LOOKUP     = 10
CLS_EXPR_FIELD = 15
CLS_TXFIELD    = 19
CLS_LOOKUP_COND = 21

# $$class of a transformation -> short role name.  Not every export uses the
# same numeric ids for the rarer transforms, so idmc_model resolves unknown
# transforms by the class *name* in $$classInfo as a fallback.
TX_ROLE = {
    CLS_EXPRESSION: "Expression",
    CLS_TARGET:     "Target",
    CLS_SOURCE:     "Source",
    CLS_LOOKUP:     "Lookup",
}


class Imf:
    """A loaded IMFOBJECT with a resolver over its $$ID/##ID graph."""

    def __init__(self, content: dict, classinfo: dict):
        self.content = content
        self.classinfo = classinfo          # {"<id>": "com.informatica...."}
        self.index: dict[int, dict] = {}
        self._build_index(content)

    def _build_index(self, o):
        if isinstance(o, dict):
            i = o.get("$$ID")
            if isinstance(i, int):
                self.index[i] = o
            for v in o.values():
                self._build_index(v)
        elif isinstance(o, list):
            for v in o:
                self._build_index(v)

    def deref(self, o):
        """One-level dereference of a {'##ID': n} ref; returns o unchanged
        when it is not a ref."""
        if isinstance(o, dict) and "##ID" in o and len(o) <= 2:
            return self.index.get(o["##ID"], o)
        return o

    def class_name(self, cls_id) -> str:
        return self.classinfo.get(str(cls_id), "")

    @property
    def name(self) -> str:
        return self.content.get("name", "")

    @property
    def transformations(self) -> list[dict]:
        return self.content.get("transformations", []) or []

    @property
    def links(self) -> list[dict]:
        return self.content.get("links", []) or []


def _imf_from_bytes(imf_bytes: bytes) -> Imf:
    j = json.loads(imf_bytes)
    content = j["content"]
    classinfo = {}
    for cid, info in j.get("metadata", {}).get("$$classInfo", {}).items():
        classinfo[cid] = info.get("name") if isinstance(info, dict) else info
    return Imf(content, classinfo)


def _imfobject_id(file_records: list) -> str | None:
    for rec in file_records:
        if rec.get("type") == "IMFOBJECT":
            return rec["id"]
    return None


def load_dtemplate(path: str) -> Imf:
    """Load a *.DTEMPLATE, given either the extracted directory or the .zip.

    The real mapping is the bin/ file whose fileRecord type is IMFOBJECT
    (NOT the preview jpeg, which is the other .bin)."""
    if path.endswith(".zip") or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            fr = json.loads(z.read("fileRecord.json"))
            oid = _imfobject_id(fr)
            binname = f"bin/{oid}.bin" if oid else None
            if binname not in names:
                # fall back to the largest .bin
                bins = [n for n in names if n.startswith("bin/") and n.endswith(".bin")]
                binname = max(bins, key=lambda n: z.getinfo(n).file_size)
            return _imf_from_bytes(z.read(binname))

    # extracted directory
    bindir = os.path.join(path, "bin")
    fr_path = os.path.join(path, "fileRecord.json")
    imf_path = None
    if os.path.exists(fr_path):
        oid = _imfobject_id(json.load(open(fr_path)))
        if oid:
            cand = os.path.join(bindir, oid + ".bin")
            if os.path.exists(cand):
                imf_path = cand
    if imf_path is None:
        bins = [os.path.join(bindir, f) for f in os.listdir(bindir)
                if f.endswith(".bin")]
        imf_path = max(bins, key=os.path.getsize)
    return _imf_from_bytes(open(imf_path, "rb").read())


def load_mtt(mtt_zip: str) -> dict:
    """Load a *.MTT.zip and return its mtTask dict."""
    with zipfile.ZipFile(mtt_zip) as z:
        with z.open("mtTask.json") as f:
            return json.load(f)[0]


# ---- platform type -> Snowflake -------------------------------------------
# Lookup/expression fields carry a platformType seed ref such as
#   "smd:com.informatica.metadata.seed.platform.Platform.typesystem/decimal"
# We map the trailing token to a Snowflake type.
def platform_type_to_snowflake(sid: str, precision, scale) -> str:
    tok = (sid or "").rsplit("/", 1)[-1].lower()
    try:
        p = int(precision) if precision not in (None, "") else None
    except (TypeError, ValueError):
        p = None
    try:
        s = int(scale) if scale not in (None, "") else 0
    except (TypeError, ValueError):
        s = 0
    if tok in ("decimal", "numeric", "number", "bigint", "integer", "int",
               "smallint", "tinyint"):
        if tok == "bigint":
            return "NUMBER(19,0)"
        if tok in ("integer", "int"):
            return "NUMBER(10,0)"
        if tok in ("smallint", "tinyint"):
            return "NUMBER(5,0)"
        if p:
            p = min(p, 38)              # Snowflake max precision
            s = min(s, p)
            return f"NUMBER({p},{s})"
        return "NUMBER(38,0)"
    if tok in ("double", "float", "real"):
        return "FLOAT"
    if tok in ("date", "date/time", "datetime", "timestamp"):
        return "TIMESTAMP" if tok != "date" else "DATE"
    if tok in ("string", "varchar", "varchar2", "nvarchar", "char", "text"):
        return f"VARCHAR({p or 256})"
    if tok in ("binary", "varbinary"):
        return "BINARY"
    if tok in ("boolean", "bit"):
        return "BOOLEAN"
    # Unknown -> best-effort VARCHAR
    return f"VARCHAR({p or 256})"

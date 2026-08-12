#!/usr/bin/env python3
"""
foampatch - OpenFOAM case otomasyonu
=====================================
Tek bir config dosyasindan yola cikarak birden fazla OpenFOAM case'inde:
  1) patch isimlerini toplu degistirir (rename)
  2) 0/ altindaki tum alanlarin boundaryField bloklarini yeniden yazar
  3) system/blockMeshDict icindeki boundary listesini gunceller
  4) system/snappyHexMeshDict icindeki geometry / refinementSurfaces /
     features bloklarini gunceller

Kullanim:
    python3 foampatch.py config.yaml --dry-run     # sadece raporla
    python3 foampatch.py config.yaml               # uygula (.bak yedegi alir)
    python3 foampatch.py config.yaml --no-backup
    python3 foampatch.py config.yaml --only 0,blockmesh
"""

import argparse
import copy
import json
import os
import re
import shutil
import sys

try:
    import yaml
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# BC PRESET TABLOSU
# ---------------------------------------------------------------------------
# role -> field -> {type: ..., ekstra anahtarlar}
# $VAR ifadeleri config'teki "vars" bolumunden doldurulur.
# Bir alan burada yoksa FALLBACK kullanilir.

PRESETS = {
    "wall": {
        "U":            {"type": "noSlip"},
        "p":            {"type": "zeroGradient"},
        "p_rgh":        {"type": "fixedFluxPressure", "value": "uniform 0"},
        "k":            {"type": "kqRWallFunction", "value": "$internalField"},
        "epsilon":      {"type": "epsilonWallFunction", "value": "$internalField"},
        "omega":        {"type": "omegaWallFunction", "value": "$internalField"},
        "nut":          {"type": "nutkWallFunction", "value": "uniform 0"},
        "nuTilda":      {"type": "fixedValue", "value": "uniform 0"},
        "alphat":       {"type": "compressible::alphatWallFunction", "Prt": "0.85", "value": "uniform 0"},
        "T":            {"type": "zeroGradient"},
        "alpha.water":  {"type": "zeroGradient"},
    },
    "inlet": {
        "U":            {"type": "fixedValue", "value": "$U_inlet"},
        "p":            {"type": "zeroGradient"},
        "p_rgh":        {"type": "fixedFluxPressure", "value": "uniform 0"},
        "k":            {"type": "fixedValue", "value": "$k_inlet"},
        "epsilon":      {"type": "fixedValue", "value": "$epsilon_inlet"},
        "omega":        {"type": "fixedValue", "value": "$omega_inlet"},
        "nut":          {"type": "calculated", "value": "uniform 0"},
        "nuTilda":      {"type": "fixedValue", "value": "uniform 0"},
        "alphat":       {"type": "calculated", "value": "uniform 0"},
        "T":            {"type": "fixedValue", "value": "$T_inlet"},
        "alpha.water":  {"type": "fixedValue", "value": "uniform 1"},
    },
    "outlet": {
        "U":            {"type": "inletOutlet", "inletValue": "uniform (0 0 0)", "value": "$internalField"},
        "p":            {"type": "fixedValue", "value": "$p_outlet"},
        "p_rgh":        {"type": "fixedValue", "value": "uniform 0"},
        "k":            {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "epsilon":      {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "omega":        {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "nut":          {"type": "calculated", "value": "uniform 0"},
        "nuTilda":      {"type": "zeroGradient"},
        "alphat":       {"type": "calculated", "value": "uniform 0"},
        "T":            {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "alpha.water":  {"type": "inletOutlet", "inletValue": "uniform 0", "value": "$internalField"},
    },
    "pressureInlet": {
        "U":            {"type": "pressureInletOutletVelocity", "value": "$internalField"},
        "p":            {"type": "totalPressure", "p0": "$p_inlet", "value": "$internalField"},
        "p_rgh":        {"type": "totalPressure", "p0": "uniform 0", "value": "$internalField"},
        "k":            {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "epsilon":      {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "omega":        {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "nut":          {"type": "calculated", "value": "uniform 0"},
        "T":            {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "alpha.water":  {"type": "inletOutlet", "inletValue": "uniform 0", "value": "$internalField"},
    },
    "atmosphere": {
        "U":            {"type": "pressureInletOutletVelocity", "value": "$internalField"},
        "p_rgh":        {"type": "totalPressure", "p0": "uniform 0", "value": "$internalField"},
        "p":            {"type": "calculated", "value": "$internalField"},
        "k":            {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "epsilon":      {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "omega":        {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"},
        "nut":          {"type": "calculated", "value": "uniform 0"},
        "alpha.water":  {"type": "inletOutlet", "inletValue": "uniform 0", "value": "$internalField"},
    },
    "symmetry":      {"*": {"type": "symmetry"}},
    "symmetryPlane": {"*": {"type": "symmetryPlane"}},
    "empty":         {"*": {"type": "empty"}},
    "slip":          {"*": {"type": "slip"}},
    "cyclic":        {"*": {"type": "cyclic"}},
    "wedge":         {"*": {"type": "wedge"}},
    "zeroGradient":  {"*": {"type": "zeroGradient"}},
}

# Preset'te tanimsiz alan icin guvenli varsayilan
FALLBACK = {
    "wall":          {"type": "zeroGradient"},
    "inlet":         {"type": "zeroGradient"},
    "outlet":        {"type": "zeroGradient"},
    "pressureInlet": {"type": "zeroGradient"},
    "atmosphere":    {"type": "zeroGradient"},
}

# blockMesh / boundary dosyasi icin geometrik tip esleme
GEO_TYPE = {
    "wall": "wall", "inlet": "patch", "outlet": "patch",
    "pressureInlet": "patch", "atmosphere": "patch",
    "symmetry": "symmetry", "symmetryPlane": "symmetryPlane",
    "empty": "empty", "slip": "patch", "cyclic": "cyclic",
    "wedge": "wedge", "zeroGradient": "patch",
}


# ---------------------------------------------------------------------------
# YARDIMCI: dengeli blok bulma
# ---------------------------------------------------------------------------

def is_junk(fn):
    """OpenFOAM alan dosyasi olmayan cop dosyalari eler."""
    return (fn.startswith(".")
            or ":" in fn
            or "Zone.Identifier" in fn
            or fn.endswith((".bak", ".gz", ".orig~", "~", ".swp", ".stl", ".obj")))


def find_block(text, keyword, opener="{", closer="}"):
    """
    'keyword' anahtar kelimesinden sonraki ilk opener'i bulur ve
    dengeli kapanisina kadar olan araligi dondurur.
    Donus: (start, end, inner_text, indent) veya None
    Yorum satirlari ve string'ler kabaca atlanir.
    """
    m = re.search(r"(?m)^[ \t]*" + re.escape(keyword) + r"[ \t]*(?://.*)?$", text)
    if not m:
        m = re.search(r"(?m)^[ \t]*" + re.escape(keyword) + r"[ \t]*" + re.escape(opener), text)
        if not m:
            return None
    i = text.find(opener, m.start())
    if i == -1:
        return None
    depth = 0
    j = i
    n = len(text)
    while j < n:
        c = text[j]
        if c == "/" and j + 1 < n and text[j + 1] == "/":
            j = text.find("\n", j)
            if j == -1:
                return None
            continue
        if c == "/" and j + 1 < n and text[j + 1] == "*":
            j = text.find("*/", j)
            if j == -1:
                return None
            j += 2
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                line_start = text.rfind("\n", 0, m.start()) + 1
                indent = re.match(r"[ \t]*", text[line_start:]).group(0)
                return (m.start(), j + 1, text[i + 1:j], indent)
        j += 1
    return None


def reindent(lines, indent):
    """Uretilen satirlara blogun kendi girintisini uygular."""
    return "\n".join((indent + l) if l.strip() else "" for l in lines)


def close_paren(text, end, chunk):
    """Paren blogu sonrasi ';' zaten varsa tekrar ekleme."""
    if end < len(text) and text[end] == ";":
        return chunk, end          # orijinal ';' yerinde kalir
    return chunk + ";", end


def substitute_vars(value, variables):
    """$VAR ifadelerini config vars ile degistirir. $internalField korunur."""
    if not isinstance(value, str):
        return str(value)
    def repl(m):
        name = m.group(1)
        if name == "internalField":
            return "$internalField"
        if name in variables:
            return str(variables[name])
        return m.group(0)
    return re.sub(r"\$(\w+)", repl, value)


# ---------------------------------------------------------------------------
# BC URETIMI
# ---------------------------------------------------------------------------

def resolve_bc(patch_name, patch_cfg, field, variables):
    """Bir patch + field icin BC sozlugunu cozer (override > preset > fallback)."""
    role = patch_cfg.get("role", "wall")
    preset = PRESETS.get(role, {})

    if "*" in preset:
        bc = dict(preset["*"])
    elif field in preset:
        bc = dict(preset[field])
    else:
        bc = dict(FALLBACK.get(role, {"type": "zeroGradient"}))

    # per-patch override:  overrides: {U: {type: fixedValue, value: "uniform (2 0 0)"}}
    ov = (patch_cfg.get("overrides") or {}).get(field)
    if ov:
        if isinstance(ov, str):
            bc = {"type": ov}
        else:
            bc = dict(ov)

    return {k: substitute_vars(v, variables) for k, v in bc.items()}


def render_boundary_field(patches, field, variables, indent="    "):
    """0/<field> icin boundaryField govdesini uretir."""
    lines = []
    for name, cfg in patches.items():
        if field in (cfg.get("skip_fields") or []):
            continue
        bc = resolve_bc(name, cfg, field, variables)
        lines.append(f"{indent}{name}")
        lines.append(f"{indent}{{")
        keyw = max((len(k) for k in bc), default=4)
        for k, v in bc.items():
            lines.append(f"{indent}    {k.ljust(keyw)}    {v};")
        lines.append(f"{indent}}}")
        lines.append("")
    if not lines:
        lines = [f"{indent}\"(.*)\"", f"{indent}{{", f"{indent}    type    zeroGradient;", f"{indent}}}", ""]
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# OTOMATIK KESIF: STL isimlerinden patch + rol cikarimi
# ---------------------------------------------------------------------------
# Sirali kontrol edilir, ilk eslesen kazanir. config.yaml icinde
# "role_rules:" ile ezilebilir.

DEFAULT_ROLE_RULES = [
    (r"inlet|giris|intake|feed",            "inlet"),
    (r"outlet|out\b|cikis|exit|exhaust",    "outlet"),
    (r"atmosphere|atm|sky|farfield|freestream", "atmosphere"),
    (r"symm",                               "symmetry"),
    (r"empty|frontandback|front_back",      "empty"),
    (r"cyclic|periodic",                    "cyclic"),
    (r"wedge",                              "wedge"),
    (r"wall|body|duct|pipe|solid|surface|govde|duvar", "wall"),
]

# Rol basina varsayilan snappy ayarlari
ROLE_DEFAULTS = {
    "wall":       {"level": [3, 4], "feature_level": 4, "layers": 3},
    "inlet":      {"level": [2, 2], "feature_level": 3},
    "outlet":     {"level": [2, 2], "feature_level": 3},
    "atmosphere": {"level": [1, 1], "feature_level": 2},
}


def infer_role(name, rules=None):
    """Dosya/patch isminden rol tahmin eder. Bulamazsa 'wall' doner."""
    low = name.lower()
    for pattern, role in (rules or DEFAULT_ROLE_RULES):
        if re.search(pattern, low):
            return role
    return "wall"


def discover_stls(case, stl_dir="constant/triSurface", rules=None):
    """
    STL klasorunu tarar, her dosya icin patch tanimi uretir.
    Patch adi = STL dosya adi (uzantisiz).
    """
    search_dirs = [stl_dir, "constant/triSurface", "constant/geometry", "constant"]
    d = None
    for cand in search_dirs:
        p = os.path.join(case, cand)
        if os.path.isdir(p) and any(f.lower().endswith((".stl", ".obj", ".stlb"))
                                    for f in os.listdir(p)):
            d = p
            break
    if d is None:
        return {}
    if os.path.basename(d) not in ("triSurface", "geometry"):
        print(f"  ! UYARI: STL'ler {os.path.relpath(d, case)} icinde bulundu.")
        print("    snappyHexMesh bunlari constant/triSurface altinda bekler:")
        print("    mkdir -p constant/triSurface && mv *.stl constant/triSurface/")
    found = {}
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith((".stl", ".obj", ".stlb")):
            continue
        base = os.path.splitext(fn)[0]
        role = infer_role(base, rules)
        cfg = {"role": role, "stl": fn, "in_blockmesh": False}
        cfg.update(copy.deepcopy(ROLE_DEFAULTS.get(role, {})))
        found[base] = cfg
    return found


def write_surface_feature_dict(case, patches, runner):
    """
    Feature extraction dict'ini uretir. Hangi surumdesin ona gore:
      * system/surfaceFeaturesDict        -> OpenFOAM.org (surfaceFeatures)
      * system/surfaceFeatureExtractDict  -> OpenFOAM.com/ESI (surfaceFeatureExtract)
    Mevcut olan(lar) guncellenir; hicbiri yoksa ikisi de yazilir ve
    kullanici hangisini silecegini secer.
    """
    stls = [c["stl"] for c in patches.values() if c.get("stl")]
    if not stls:
        return

    def hdr(obj):
        return ("FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
                "    class       dictionary;\n    object      " + obj + ";\n}\n"
                "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n")

    # --- OpenFOAM.org bicimi ---
    org = [hdr("surfaceFeaturesDict"), "surfaces", "("]
    org += [f'    "{stl}"' for stl in stls]
    org += [");", "", "includedAngle    150;", "",
            "subsetFeatures", "{", "    nonManifoldEdges    no;",
            "    openEdges           yes;", "}", "", "writeObj    yes;", ""]
    org_txt = "\n".join(org)

    # --- OpenFOAM.com / ESI bicimi ---
    esi = [hdr("surfaceFeatureExtractDict")]
    for stl in stls:
        esi += [stl, "{", "    extractionMethod    extractFromSurface;",
                "    includedAngle       150;", "    subsetFeatures", "{",
                "        nonManifoldEdges    no;", "        openEdges           yes;",
                "    }", "    writeObj            yes;", "}", ""]
    esi_txt = "\n".join(esi)

    p_org = os.path.join(case, "system", "surfaceFeaturesDict")
    p_esi = os.path.join(case, "system", "surfaceFeatureExtractDict")
    have_org, have_esi = os.path.exists(p_org), os.path.exists(p_esi)

    def emit(path, text, existed):
        if runner.dry_run:
            runner.log(f"    ~ [DRY] {'guncellenecek' if existed else 'olusturulacak'}: "
                       f"{os.path.relpath(path)}")
            runner.changed.append(path)
            return
        if existed:
            runner.write(path, text)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            runner.changed.append(path)
            runner.log(f"    + olusturuldu : {os.path.relpath(path)}")

    if have_org:
        emit(p_org, org_txt, True)
    if have_esi:
        emit(p_esi, esi_txt, True)
    if not have_org and not have_esi:
        emit(p_org, org_txt, False)
        emit(p_esi, esi_txt, False)
        runner.log("    i Iki surum icin de yazildi. Kullanmadigini silebilirsin:")
        runner.log("      OpenFOAM.org -> surfaceFeatures       (surfaceFeaturesDict)")
        runner.log("      OpenFOAM.com -> surfaceFeatureExtract (surfaceFeatureExtractDict)")



# ---------------------------------------------------------------------------
# ISLEMLER
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, dry_run=False, backup=True, verbose=True):
        self.dry_run = dry_run
        self.backup = backup
        self.verbose = verbose
        self.changed = []
        self.skipped = []

    def log(self, msg):
        if self.verbose:
            print(msg)

    def write(self, path, new_text):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            old = f.read()
        if old == new_text:
            self.skipped.append(path)
            self.log(f"    = degisiklik yok : {os.path.relpath(path)}")
            return False
        self.changed.append(path)
        if self.dry_run:
            self.log(f"    ~ [DRY] guncellenecek: {os.path.relpath(path)}")
            return True
        if self.backup and not os.path.exists(path + ".bak"):
            shutil.copy2(path, path + ".bak")
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
        self.log(f"    + guncellendi : {os.path.relpath(path)}")
        return True

    # --- 1. rename ---------------------------------------------------------
    def do_rename(self, case, mapping):
        if not mapping:
            return
        self.log("  [rename] patch isimleri")
        targets = []
        for sub in ("0", "0.orig", "constant", "system"):
            d = os.path.join(case, sub)
            if not os.path.isdir(d):
                continue
            for root, _, files in os.walk(d):
                for fn in files:
                    if is_junk(fn):
                        continue
                    targets.append(os.path.join(root, fn))
        for path in targets:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
            except Exception:
                continue
            new = txt
            for old, new_name in mapping.items():
                new = re.sub(r"(?<![\w.])" + re.escape(old) + r"(?![\w.])", new_name, new)
            if new != txt:
                self.write(path, new)

    # --- 2. 0/ alanlari ----------------------------------------------------
    def do_zero(self, case, patches, variables, zero_dirs=("0", "0.orig")):
        for zd in zero_dirs:
            d = os.path.join(case, zd)
            if not os.path.isdir(d):
                continue
            self.log(f"  [{zd}/] boundaryField bloklari")
            for fn in sorted(os.listdir(d)):
                path = os.path.join(d, fn)
                if not os.path.isfile(path) or is_junk(fn):
                    continue
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
                blk = find_block(txt, "boundaryField")
                if not blk:
                    self.log(f"    ! boundaryField bulunamadi: {fn}")
                    continue
                start, end, _, ind = blk
                body = render_boundary_field(patches, fn, variables)
                new_block = "boundaryField\n{\n" + body + "}"
                self.write(path, txt[:start] + new_block + txt[end:])

    # --- 3. blockMeshDict --------------------------------------------------
    def do_blockmesh(self, case, patches):
        path = os.path.join(case, "system", "blockMeshDict")
        if not os.path.exists(path):
            path = os.path.join(case, "constant", "polyMesh", "blockMeshDict")
        if not os.path.exists(path):
            self.log("  [blockMesh] dosya yok, atlandi")
            return
        self.log("  [blockMeshDict] boundary listesi")
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        blk = find_block(txt, "boundary", opener="(", closer=")")
        if not blk:
            self.log("    ! boundary blogu bulunamadi")
            return
        start, end, inner, ind = blk

        # mevcut girdileri sirasiyla, ham metin olarak topla
        existing = []          # [(isim, ham_metin, faces_metni)]
        pos = 0
        while True:
            m = re.compile(r'([\w\"\(\)\|\.\*]+)\s*\{').search(inner, pos)
            if not m:
                break
            depth, j = 0, m.end() - 1
            while j < len(inner):
                if inner[j] == "{":
                    depth += 1
                elif inner[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            raw = inner[m.start():j + 1]
            fm = re.search(r"faces\s*\((.*?)\);", raw, re.S)
            existing.append((m.group(1), raw, fm.group(1).strip() if fm else ""))
            pos = j + 1

        old_faces = {n: f for n, f, _ in [(a, c, b) for a, b, c in existing]}
        managed = {n for n, c in patches.items() if c.get("in_blockmesh") is not False}

        out = ["boundary", "("]

        def render(name, cfg, faces):
            gtype = cfg.get("geometric_type") or GEO_TYPE.get(cfg.get("role", "wall"), "patch")
            r = [f"    {name}", "    {", f"        type    {gtype};"]
            if cfg.get("neighbourPatch"):
                r.append(f"        neighbourPatch    {cfg['neighbourPatch']};")
            if faces:
                r += ["        faces", "        ("]
                r += [f"            {l.strip()}" for l in faces.splitlines() if l.strip()]
                r.append("        );")
            else:
                r.append("        faces ();   // <<< doldurulacak")
            r += ["    }", ""]
            return r

        seen = set()
        for name, raw, faces in existing:
            if name in managed:                      # config yonetiyor -> yeniden uret
                out += render(name, patches[name], patches[name].get("faces") or faces)
            else:                                    # config disi -> dokunma
                rl = raw.splitlines()
                pad = min((len(l) - len(l.lstrip()) for l in rl[1:] if l.strip()), default=0)
                out += ["    " + rl[0].strip()] + ["    " + l[pad:] for l in rl[1:]] + [""]
            seen.add(name)

        for name in managed:                         # config'te olup dosyada olmayanlar
            if name not in seen:
                out += render(name, patches[name], patches[name].get("faces"))

        chunk, end = close_paren(txt, end, reindent(out + [")"], ind))
        self.write(path, txt[:start] + chunk + txt[end:])

    # --- 4. snappyHexMeshDict ---------------------------------------------
    def do_snappy(self, case, patches, snappy_cfg):
        path = os.path.join(case, "system", "snappyHexMeshDict")
        if not os.path.exists(path):
            self.log("  [snappy] dosya yok, atlandi")
            return
        self.log("  [snappyHexMeshDict] geometry / refinementSurfaces / features")
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()

        stl_patches = {n: c for n, c in patches.items() if c.get("stl")}
        if not stl_patches:
            self.log("    ! stl tanimli patch yok, atlandi")
            return

        # geometry
        blk = find_block(txt, "geometry")
        if blk:
            start, end, _, ind = blk
            g = ["geometry", "{"]
            for name, cfg in stl_patches.items():
                stl = cfg["stl"]
                g.append(f"    {stl}")
                g.append("    {")
                g.append("        type    triSurfaceMesh;")
                g.append(f"        name    {name};")
                g.append("    }")
                g.append("")
            for extra in (snappy_cfg.get("extra_geometry") or []):
                g.append("    " + extra.strip().replace("\n", "\n    "))
                g.append("")
            g.append("}")
            txt = txt[:start] + reindent(g, ind) + txt[end:]

        # refinementSurfaces
        blk = find_block(txt, "refinementSurfaces")
        if blk:
            start, end, _, ind = blk
            r = ["refinementSurfaces", "{"]
            for name, cfg in stl_patches.items():
                lvl = cfg.get("level", snappy_cfg.get("default_level", [1, 1]))
                r.append(f"    {name}")
                r.append("    {")
                r.append(f"        level    ({lvl[0]} {lvl[1]});")
                gtype = cfg.get("geometric_type") or GEO_TYPE.get(cfg.get("role", "wall"), "patch")
                r.append("        patchInfo")
                r.append("        {")
                r.append(f"            type    {gtype};")
                r.append("        }")
                if cfg.get("layers") is not None:
                    r.append(f"        // nSurfaceLayers: addLayers bolumunde")
                r.append("    }")
                r.append("")
            r.append("}")
            txt = txt[:start] + reindent(r, ind) + txt[end:]

        # features
        blk = find_block(txt, "features", opener="(", closer=")")
        if blk:
            start, end, _, ind = blk
            f_ = ["features", "("]
            for name, cfg in stl_patches.items():
                base = os.path.splitext(cfg["stl"])[0]
                lvl = cfg.get("feature_level", snappy_cfg.get("default_feature_level", 1))
                f_.append("    {")
                f_.append(f'        file    "{base}.eMesh";')
                f_.append(f"        level    {lvl};")
                f_.append("    }")
            chunk, end = close_paren(txt, end, reindent(f_ + [")"], ind))
            txt = txt[:start] + chunk + txt[end:]

        # layers
        blk = find_block(txt, "layers")
        if blk:
            start, end, _, ind = blk
            l = ["layers", "{"]
            for name, cfg in patches.items():
                if cfg.get("layers"):
                    l.append(f"    {name}")
                    l.append("    {")
                    l.append(f"        nSurfaceLayers    {cfg['layers']};")
                    l.append("    }")
            l.append("}")
            txt = txt[:start] + reindent(l, ind) + txt[end:]

        self.write(path, txt)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        if yaml is None:
            sys.exit("HATA: PyYAML kurulu degil -> pip install pyyaml")
        return yaml.safe_load(text)
    return json.loads(text)


def main():
    ap = argparse.ArgumentParser(description="OpenFOAM toplu case guncelleyici")
    ap.add_argument("config", help="config.yaml veya config.json")
    ap.add_argument("--dry-run", action="store_true", help="hicbir dosyayi yazma, sadece raporla")
    ap.add_argument("--no-backup", action="store_true", help=".bak yedegi alma")
    ap.add_argument("--only", default="", help="rename,0,blockmesh,snappy,sfe (virgullu)")
    ap.add_argument("--auto", action="store_true",
                    help="constant/triSurface icindeki STL'lerden patch'leri otomatik cikar")
    ap.add_argument("--write-config", metavar="DOSYA",
                    help="--auto ile bulunanlari config olarak yaz ve cik")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cases = cfg.get("cases") or []
    patches = cfg.get("patches") or {}
    variables = cfg.get("vars") or {}
    rename = cfg.get("rename") or {}
    snappy_cfg = cfg.get("snappy") or {}
    zero_dirs = cfg.get("zero_dirs") or ["0", "0.orig"]

    role_rules = [(k, v) for d in (cfg.get("role_rules") or [])
                  for k, v in d.items()] or None
    stl_dir = cfg.get("stl_dir", "constant/triSurface")

    # --- otomatik kesif ---
    if args.auto or not patches:
        for case in cases:
            case_p = os.path.expanduser(case)
            found = discover_stls(case_p, stl_dir, role_rules)
            if not found:
                continue
            print(f"-- {case}: {len(found)} STL bulundu")
            for n, c in found.items():
                print(f"   {c['stl']:<24} -> patch '{n}'  rol: {c['role']}")
            # config'te elle yazilmis patch varsa o kazanir
            merged = dict(found)
            for n, c in patches.items():
                merged[n] = {**found.get(n, {}), **c}
            patches = merged
            break
        print()

    if not patches:
        sys.exit(
            "\nHATA: Hicbir patch tanimlanmadi - hicbir dosyaya dokunulmadi.\n"
            "  * --auto kullandiysan: constant/triSurface icinde STL bulunamadi.\n"
            "    Kontrol et ->  find . -iname '*.stl'\n"
            "  * Ya da config.yaml icindeki 'patches:' bolumunu elle doldur.\n"
            "Not: bos patch listesiyle devam etseydi tum BC'ler silinirdi.\n")

    if args.write_config:
        if yaml is None:
            sys.exit("PyYAML gerekli")
        out = dict(cfg)
        out["patches"] = patches
        with open(args.write_config, "w", encoding="utf-8") as f:
            yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        print(f">>> Yazildi: {args.write_config}  (gozden gecirip duzenleyebilirsin)")
        return

    steps = [s.strip() for s in args.only.split(",") if s.strip()] or ["rename", "0", "blockmesh", "snappy", "sfe"]

    runner = Runner(dry_run=args.dry_run, backup=not args.no_backup)

    if args.dry_run:
        print(">>> DRY RUN - hicbir dosya yazilmayacak\n")

    for case in cases:
        case = os.path.expanduser(case)
        if not os.path.isdir(case):
            print(f"!! case bulunamadi: {case}")
            continue
        print(f"== CASE: {case}")
        if "rename" in steps:
            runner.do_rename(case, rename)
        if "0" in steps:
            runner.do_zero(case, patches, variables, zero_dirs)
        if "blockmesh" in steps:
            runner.do_blockmesh(case, patches)
        if "snappy" in steps:
            runner.do_snappy(case, patches, snappy_cfg)
        if "sfe" in steps:
            runner.log("  [surfaceFeatureExtractDict]")
            write_surface_feature_dict(case, patches, runner)
        print()

    print(f">>> Ozet: {len(runner.changed)} dosya degisti, {len(runner.skipped)} dosya ayni kaldi.")
    if not args.dry_run and not args.no_backup and runner.changed:
        print(">>> Yedekler *.bak olarak duruyor. Geri almak icin:")
        print("    for f in $(find . -name '*.bak'); do mv \"$f\" \"${f%.bak}\"; done")


if __name__ == "__main__":
    main()

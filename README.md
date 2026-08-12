# foampatch

Bulk boundary-condition setup for OpenFOAM cases.

Setting up a new OpenFOAM case usually means copying an existing one and editing
it by hand. With 6 STL surfaces and 7 fields in `0/`, that is 42 boundary
condition blocks to write — and the same patch names have to match in
`blockMeshDict`, `snappyHexMeshDict` and the feature-extraction dict. Miss one
and the mesh comes out wrong without an obvious error.

`foampatch` derives patch roles from STL filenames and writes all of it for you.

```bash
python3 foampatch.py config.yaml --auto
```

```
inlet.stl    → role: inlet    → fixedValue in U, zeroGradient in p, ...
outlet1.stl  → role: outlet   → inletOutlet in U, fixedValue in p, ...
walls.stl    → role: wall     → noSlip in U, wall functions in k/epsilon/nut, ...
```

---

## What it does

| Target | Action |
|---|---|
| `0/` and `0.orig/` | Rewrites `boundaryField` in every field file |
| `system/blockMeshDict` | Updates the `boundary` list, preserving existing `faces` |
| `system/snappyHexMeshDict` | Regenerates `geometry`, `refinementSurfaces`, `features`, `layers` |
| `system/surfaceFeaturesDict`<br>`system/surfaceFeatureExtractDict` | Generates whichever your OpenFOAM version uses |
| All of the above | Bulk patch renaming |

Runs across multiple cases in one command. Every modified file gets a `.bak`
backup.

---

## Install

Requires Python 3.8+ and PyYAML.

```bash
# Debian/Ubuntu
sudo apt install python3-yaml

# or
pip install pyyaml
```

Optional, for the foamlib-based variant:

```bash
pip install foamlib
```

Drop `foampatch.py` (and optionally `foampatch_fl.py`) into your case directory,
or anywhere on your `PATH`.

---

## Quick start

Your case should look like this:

```
myCase/
├── 0/                       U, p, k, epsilon, nut, T ...
├── constant/
│   └── triSurface/          inlet.stl, outlet1.stl, walls.stl ...
├── system/
│   ├── blockMeshDict
│   └── snappyHexMeshDict
├── foampatch.py
└── config.yaml
```

Note the capital **S** in `triSurface` — OpenFOAM is case-sensitive here.

Minimal `config.yaml`:

```yaml
cases:
  - .

vars:
  U_inlet:       "uniform (10 0 0)"
  p_outlet:      "uniform 0"
  k_inlet:       "uniform 0.375"
  epsilon_inlet: "uniform 0.0143"
  omega_inlet:   "uniform 2.6"
  T_inlet:       "uniform 300"

patches: {}
```

Preview, then apply:

```bash
python3 foampatch.py config.yaml --auto --dry-run
python3 foampatch.py config.yaml --auto
```

Then mesh as usual:

```bash
surfaceFeatures        # or surfaceFeatureExtract on OpenFOAM.com
blockMesh
snappyHexMesh -overwrite
checkMesh
```

---

## Two ways to define patches

### Automatic (`--auto`)

Scans `constant/triSurface` and infers a role from each filename. Patterns are
checked in order; first match wins:

| Pattern | Role |
|---|---|
| `inlet`, `giris`, `intake`, `feed` | inlet |
| `outlet`, `out`, `cikis`, `exit`, `exhaust` | outlet |
| `atmosphere`, `atm`, `sky`, `farfield`, `freestream` | atmosphere |
| `symm` | symmetry |
| `empty`, `frontandback` | empty |
| `cyclic`, `periodic` | cyclic |
| `wedge` | wedge |
| `wall`, `body`, `duct`, `pipe`, `solid`, `surface` | wall |

No match falls back to `wall`. Add your own rules in `config.yaml`:

```yaml
role_rules:
  - "nozzle": inlet
  - "plenum": outlet
```

### Explicit

For anything the filename can't express — different inlet temperatures, custom
refinement levels — generate a full config, edit it, and drop `--auto`:

```bash
python3 foampatch.py config.yaml --auto --write-config config_full.yaml
$EDITOR config_full.yaml
python3 foampatch.py config_full.yaml
```

---

## Config reference

```yaml
cases:                    # directories to process
  - .
  - ../case_B

zero_dirs: ["0", "0.orig"]

stl_dir: constant/triSurface

vars:                     # referenced as $name in presets
  U_inlet: "uniform (10 0 0)"

rename:                   # old_name: new_name, applied across 0/ constant/ system/
  INLET_1: inlet_main

patches:
  inlet:
    role: inlet           # see role table below
    stl: inlet.stl        # enables snappyHexMesh entries
    level: [2, 3]         # refinementSurfaces level
    feature_level: 3      # features level
    layers: 5             # nSurfaceLayers in addLayersControls
    in_blockmesh: false   # STL patches don't belong in blockMeshDict
    geometric_type: patch # override the inferred type
    skip_fields: ["T"]    # leave these field files untouched
    overrides:            # per-patch, per-field override of the preset
      p:
        type: fixedValue
        value: "uniform -50"

  frontAndBack:           # a blockMesh patch, defined by vertices
    role: empty
    faces: |
      (0 3 2 1)
      (4 5 6 7)

snappy:
  default_level: [1, 1]
  default_feature_level: 1
```

### Roles

`wall`, `inlet`, `outlet`, `pressureInlet`, `atmosphere`, `symmetry`,
`symmetryPlane`, `empty`, `slip`, `cyclic`, `wedge`, `zeroGradient`.

Each role maps to a boundary condition per field. Fields covered: `U`, `p`,
`p_rgh`, `k`, `epsilon`, `omega`, `nut`, `nuTilda`, `alphat`, `T`,
`alpha.water`. Anything else falls back to `zeroGradient`, or whatever you set
in `overrides`.

The preset table lives at the top of `foampatch.py` — edit it if your house
style differs.

---

## CLI

```
python3 foampatch.py CONFIG [options]

--auto                  infer patches from STL filenames
--write-config FILE     write the inferred config and exit
--dry-run               report what would change, write nothing
--no-backup             skip .bak files
--only STEPS            rename,0,blockmesh,snappy,sfe
```

The foamlib variant adds:

```
--list-patches          read patch names from constant/polyMesh/boundary
```

### Undo

```bash
find . -name '*.bak' -exec sh -c 'mv "$1" "${1%.bak}"' _ {} \;
```

---

## Two implementations

**`foampatch.py`** — brace-matched text editing, no dependencies beyond PyYAML.
Keeps the rest of each file byte-identical, so diffs stay small. Can be
confused by `#include` directives or regex patch names in unusual positions.

**`foampatch_fl.py`** — uses [foamlib](https://github.com/gerlero/foamlib) for
real parsing. Handles `#include`, macros and regex patch keys such as
`"(wall|outlet).*"` correctly, and validates values as it writes them. It
reformats the files it touches, so expect noisy diffs. Requires `foampatch.py`
alongside it for the preset table.

Start with the first; switch to the second if your dicts are complex.

---

## Prior art

- [PyFoam](https://openfoamwiki.net/index.php/Contrib/PyFoam) — the long-standing Python toolkit; `pyFoamPrepareCase.py` handles template-driven setup
- [foamlib](https://github.com/gerlero/foamlib) — modern parser and case API
- [InsightCAE Case Builder](https://github.com/hkroeger/insightcae) — GUI that parses `constant/polyMesh/boundary` and assigns BCs interactively
- `foamDictionary` — ships with OpenFOAM, edits single entries from shell scripts

What's different here: roles inferred from filenames, one preset expanding to
every field, and several cases updated in a single command.

---

## Caveats

Prototype. Read the `--dry-run` output before applying, and keep the `.bak`
files until you've run `checkMesh`.

`foampatch` doesn't validate that your boundary conditions are physically
sensible, and it doesn't know which solver you're running — it writes entries
for every field it finds in `0/`.

STL patches are marked `in_blockmesh: false` automatically. Background-mesh
patches still need their `faces` written by hand; new ones are flagged with
`// <<< doldurulacak` so they don't slip through.

---

## License

MIT

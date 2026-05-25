# Scenify

Extracts unique 3D assets from a scene file (FBX, OBJ, GLB, etc.), exports them as GLB meshes, and generates Lua scripts that reconstruct the full scene in Roblox Studio.

---

## Requirements

- **Python 3.11** (Homebrew) — `brew install python@3.11 python-tk@3.11`
- **Blender 4.x** — installed at `/Applications/Blender.app` (macOS default)
- **Pillow** — `pip3.11 install Pillow`

---

## Running the App

```bash
cd scene-optimizer
/opt/homebrew/bin/python3.11 main.py
```

---

## Workflow

### 1. Open a Scene File
Click **Open Scene** and select your FBX (or OBJ, GLB, DAE, etc.).

Blender runs in the background to analyze the scene — this extracts the asset list, instance positions/rotations, and generates a preview. Takes 1–3 minutes for large scenes.

### 2. Export Assets
Click **Export Assets** after analysis completes.

Blender exports each unique mesh as a GLB file into `output/unique_assets/`. Two export modes are available:

| Mode | Description |
|------|-------------|
| **Single combined GLB** | All assets in one file — faster to import into Roblox |
| **One GLB per asset** | Separate file per asset — useful for modular uploads |

### 3. Import into Roblox Studio
- For **single combined GLB**: import `output/assets_combined.glb` via **File → Import → Import 3D** in Roblox Studio
- For **individual GLBs**: import each file from `output/unique_assets/`

Place the imported model/meshes inside `ReplicatedStorage` (or wherever your Lua script expects them).

### 4. Generate Lua
Click **Generate Lua** after export completes.

Two scripts are written to `output/`:

| File | Purpose |
|------|---------|
| `*_reconstruct.lua` | Places all instances using CFrame — run once to build the scene |
| `*_runner.lua` | Entry point / loader script |

Paste `*_reconstruct.lua` into a `Script` in Roblox Studio and run it to reconstruct the full scene.

---

## Output Files

```
output/
├── assets_combined.glb          # All meshes in one GLB (single mode)
├── unique_assets/               # Per-asset GLBs (individual mode)
├── scene_manifest.json          # Raw scene data from Blender analysis
├── _extraction_results.json     # Export results with per-instance positions
├── DemoScene_reconstruct.lua    # Scene reconstruction script
└── DemoScene_runner.lua         # Loader script
```

---

## Project Structure

```
scene-optimizer/
├── main.py                      # Desktop GUI (tkinter)
├── blender_backend.py           # Headless Blender subprocess orchestration
├── scene_analyzer.py            # Reads manifest, feeds GUI stats
├── lua_generator.py             # Generates Roblox Lua from extraction results
├── _regen_lua.py                # CLI tool to regenerate Lua without re-extracting
└── blender_scripts/
    ├── analyze_scene.py         # Blender: parse scene → scene_manifest.json
    ├── extract_assets.py        # Blender: export unique meshes as GLB
    ├── render_preview.py        # Blender: render scene thumbnail
    └── compute_bbox_centers.py  # Blender: bounding box utilities
```

---

## Regenerating Lua Only

If you've already extracted assets and only want to regenerate the Lua scripts (e.g. after tweaking `lua_generator.py`):

```bash
/opt/homebrew/bin/python3.11 _regen_lua.py
```

This reads the existing `output/_extraction_results.json` and rewrites the Lua files without re-running Blender.

---

## Troubleshooting

**Blender not found**
Ensure Blender is installed at `/Applications/Blender.app`. On other platforms, set the `BLENDER_PATH` environment variable or update `BLENDER_PATHS` in `blender_backend.py`.

**Tkinter crashes on launch**
Run with Homebrew Python 3.11 specifically — system Python and Python 3.13 have Tk compatibility issues on macOS.

**Assets rotated incorrectly**
Assets imported from Unreal Engine FBX files may have an axis-correction rotation baked into their local transform by Blender's FBX importer. The extractor detects and compensates for this automatically. If an asset still appears wrong, check whether it has a non-Z rotation in its `matrix_local` in Blender.

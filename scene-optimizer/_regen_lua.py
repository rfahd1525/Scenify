#!/usr/bin/env python3
"""
Standalone script to regenerate the split Lua outputs (runner + rbxmx + monolithic)
from the saved scene_manifest.json and _extraction_results.json without launching
the full GUI.

Usage:
    python3 _regen_lua.py
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from lua_generator import generate_lua_script, generate_split_output
from scene_analyzer import SceneAnalysis, UniqueAsset, AssetInstance

DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_DIR = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.environ.get("SCENE_OPTIMIZER_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
)
MANIFEST_PATH = os.path.join(OUTPUT_DIR, "scene_manifest.json")
EXTRACTION_CANDIDATES = [
    os.path.join(OUTPUT_DIR, "extracted_assets", "_extraction_results.json"),
    os.path.join(OUTPUT_DIR, "_extraction_results.json"),
]
EXTRACTION_PATH = next((p for p in EXTRACTION_CANDIDATES if os.path.exists(p)),
                       EXTRACTION_CANDIDATES[0])

print(f"Loading manifest: {MANIFEST_PATH}")
with open(MANIFEST_PATH) as f:
    manifest = json.load(f)

print(f"Loading extraction results: {EXTRACTION_PATH}")
with open(EXTRACTION_PATH) as f:
    last_extraction = json.load(f)

# Build SceneAnalysis (mirrors App._generate_lua)
analysis = SceneAnalysis()
for asset_data in manifest.get("assets", []):
    base_name = asset_data["baseName"]
    unique_asset = UniqueAsset(base_name=base_name)
    unique_asset.category = asset_data.get("category", "Uncategorized")
    bbox = asset_data.get("localBboxCenter")
    if bbox and len(bbox) == 3:
        unique_asset.local_bbox_center = (bbox[0], bbox[1], bbox[2])
    for inst in asset_data.get("instances", []):
        unique_asset.instances.append(AssetInstance(
            instance_name=inst.get("name", base_name),
            world_position=tuple(inst.get("position", [0, 0, 0])),
            world_rotation=tuple(inst.get("rotation", [0, 0, 0])),
            world_scale=tuple(inst.get("scale", [1, 1, 1])),
        ))
    analysis.assets[base_name] = unique_asset

analysis.total_instances = manifest.get("totalInstances", 0)
analysis.unique_assets = manifest.get("uniqueAssets", 0)
analysis.optimization_ratio = manifest.get("optimizationRatio", 0)

scene_name = os.environ.get("SCENE_OPTIMIZER_SCENE_NAME", "").strip()
if not scene_name:
    input_file = manifest.get("inputFile") or manifest.get("source") or ""
    if input_file:
        scene_name = os.path.splitext(os.path.basename(input_file))[0]
if not scene_name:
    scene_name = "ReconstructedScene"
scene_name = re.sub(r"[^0-9A-Za-z_]+", "_", scene_name).strip("_") or "ReconstructedScene"

print(f"Scene: {scene_name}, assets: {len(analysis.assets)}")

# Split output (runner + rbxmx)
print("Generating split output...")
runner_lua, data_rbxmx = generate_split_output(
    analysis,
    scene_name=scene_name,
    export_results=last_extraction,
)
runner_path = os.path.join(OUTPUT_DIR, f"{scene_name}_runner.lua")
rbxmx_path  = os.path.join(OUTPUT_DIR, "scene_data.rbxmx")

with open(runner_path, "w") as f:
    f.write(runner_lua)
print(f"  runner  → {runner_path}  ({len(runner_lua):,} bytes)")

with open(rbxmx_path, "w") as f:
    f.write(data_rbxmx)
print(f"  rbxmx   → {rbxmx_path}  ({len(data_rbxmx):,} bytes)")

# Monolithic Lua (legacy)
print("Generating monolithic Lua...")
script = generate_lua_script(
    analysis,
    scene_name=scene_name,
    export_results=last_extraction,
)
legacy_path = os.path.join(OUTPUT_DIR, f"{scene_name}_reconstruct.lua")
with open(legacy_path, "w") as f:
    f.write(script)
print(f"  legacy  → {legacy_path}  ({len(script):,} bytes)")

print("Done.")

"""
Scene Analyzer
Groups scene instances by base asset name, identifies unique assets,
matches them to individual asset files, and computes world-space transforms.

Supports two input sources:
1. Blender JSON manifest (from blender_scripts/analyze_scene.py) — preferred
2. Legacy parser output (fbx_parser, gltf_parser, obj_parser)

Naming Convention Detection:
- Blender style: "AssetName.001", "AssetName.002"
- Unreal style: "AssetName_C_01", "AssetName2"
- Generic: strips trailing numeric suffixes after common separators
"""

import re
import os
import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Set
from pathlib import Path


@dataclass
class AssetInstance:
    """A single placed instance of an asset in the scene."""
    instance_name: str
    world_position: Tuple[float, float, float]
    world_rotation: Tuple[float, float, float]
    world_scale: Tuple[float, float, float]


@dataclass
class UniqueAsset:
    """A unique asset type with all its instances."""
    base_name: str
    instances: List[AssetInstance] = field(default_factory=list)
    matched_file: Optional[str] = None
    category: str = "Uncategorized"
    # Bbox center of the template mesh in its local (pre-transform) space,
    # in Blender coordinates. extract_assets.py centers the mesh on this
    # point, so the correct world placement for a centered MeshPart is
    # `matrix_world @ local_bbox_center`, not `matrix_world.translation`.
    local_bbox_center: Tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class SceneAnalysis:
    """Complete analysis results."""
    total_instances: int = 0
    unique_assets: int = 0
    optimization_ratio: float = 0.0
    assets: Dict[str, UniqueAsset] = field(default_factory=dict)
    categories: Dict[str, int] = field(default_factory=dict)
    unmatched_assets: List[str] = field(default_factory=list)
    matched_assets: List[str] = field(default_factory=list)


# Patterns for stripping instance suffixes to find the base asset name
INSTANCE_SUFFIX_PATTERNS = [
    re.compile(r"\.\d{3,}$"),          # Blender: .001, .002, .0001
    re.compile(r"_instance\d*$", re.I), # _instance, _instance1
    re.compile(r"_copy\d*$", re.I),     # _copy, _copy1
    re.compile(r"\s*\(\d+\)$"),         # (1), (2)
    re.compile(r"_\d+$"),              # Trailing _01, _02 (careful)
]

# More aggressive pattern - only used as fallback
AGGRESSIVE_SUFFIX = re.compile(r"_\d+$")


def strip_instance_suffix(name: str) -> str:
    """Strip instance/copy suffixes to get the base asset name."""
    result = name.strip()
    for pattern in INSTANCE_SUFFIX_PATTERNS[:-1]:  # Skip aggressive pattern
        result = pattern.sub("", result)
    return result


def strip_instance_suffix_aggressive(name: str) -> str:
    """Aggressively strip suffixes including trailing _## numbers."""
    result = strip_instance_suffix(name)
    return AGGRESSIVE_SUFFIX.sub("", result)


def _safe_ratio(unique: int, total: int) -> float:
    """Compute optimization ratio safely, never returning NaN or negative values."""
    if total <= 0 or unique <= 0:
        return 0.0
    if unique >= total:
        return 0.0
    ratio = (1 - unique / total) * 100
    # Guard against NaN/Inf
    if math.isnan(ratio) or math.isinf(ratio):
        return 0.0
    return max(0.0, min(100.0, round(ratio, 1)))


def analyze_from_blender_manifest(manifest: Dict) -> SceneAnalysis:
    """
    Create a SceneAnalysis from a Blender JSON manifest
    (output of blender_scripts/analyze_scene.py).
    """
    analysis = SceneAnalysis()

    for asset_data in manifest.get("assets", []):
        base_name = asset_data.get("baseName", "Unknown")
        unique_asset = UniqueAsset(base_name=base_name)
        unique_asset.category = asset_data.get("category", "Uncategorized")
        bbox = asset_data.get("localBboxCenter")
        if bbox and len(bbox) == 3:
            unique_asset.local_bbox_center = (bbox[0], bbox[1], bbox[2])

        for inst in asset_data.get("instances", []):
            pos = inst.get("position", [0, 0, 0])
            rot = inst.get("rotation", [0, 0, 0])
            scl = inst.get("scale", [1, 1, 1])

            unique_asset.instances.append(AssetInstance(
                instance_name=inst.get("name", base_name),
                world_position=tuple(pos),
                world_rotation=tuple(rot),
                world_scale=tuple(scl),
            ))

        analysis.assets[base_name] = unique_asset

    analysis.total_instances = manifest.get("totalInstances",
                                             sum(len(a.instances) for a in analysis.assets.values()))
    analysis.unique_assets = manifest.get("uniqueAssets", len(analysis.assets))
    analysis.optimization_ratio = _safe_ratio(analysis.unique_assets, analysis.total_instances)

    # Category counts
    for asset in analysis.assets.values():
        cat = asset.category
        analysis.categories[cat] = analysis.categories.get(cat, 0) + len(asset.instances)

    return analysis


def analyze_scene(
    scene_data: Dict,
    asset_library_path: Optional[str] = None,
    model_type_filter: Optional[Set[str]] = None,
) -> SceneAnalysis:
    """
    Analyze a parsed scene to find unique assets and their instances.

    Args:
        scene_data: Output from any parser (fbx_parser, gltf_parser, obj_parser)
        asset_library_path: Optional path to individual asset files for matching
        model_type_filter: Optional set of model types to include (e.g., {"Mesh"})

    Returns:
        SceneAnalysis with deduplication results
    """
    # Check if this is a Blender manifest
    if scene_data.get("source") == "blender_analyzer":
        return analyze_from_blender_manifest(scene_data)

    # Legacy path for non-Blender parsers
    from fbx_parser import SceneModel

    models = scene_data["models"]
    analysis = SceneAnalysis()

    # Build asset library index if path provided
    asset_library = {}
    if asset_library_path:
        asset_library = _build_asset_library(asset_library_path)

    # Group models by base name
    asset_groups: Dict[str, List[Tuple[str, 'SceneModel']]] = {}

    for model_id, model in models.items():
        # Filter by type if specified
        if model_type_filter and model.model_type not in model_type_filter:
            continue

        # Skip empty/null nodes unless they have meaningful names
        if model.model_type == "Null" and not _looks_like_asset_name(model.name):
            continue

        base_name = strip_instance_suffix(model.name)

        if base_name not in asset_groups:
            asset_groups[base_name] = []

        asset_groups[base_name].append((model.name, model))

    # If we have an asset library, try aggressive matching for unmatched assets
    if asset_library:
        refined_groups: Dict[str, List[Tuple[str, 'SceneModel']]] = {}
        for base_name, instances in asset_groups.items():
            matched = _find_asset_match(base_name, asset_library)
            if matched:
                key = matched
            else:
                aggressive_name = strip_instance_suffix_aggressive(base_name)
                matched = _find_asset_match(aggressive_name, asset_library)
                key = matched if matched else base_name

            if key not in refined_groups:
                refined_groups[key] = []
            refined_groups[key].extend(instances)

        asset_groups = refined_groups

    # Build analysis results
    for base_name, instances in asset_groups.items():
        unique_asset = UniqueAsset(base_name=base_name)

        for instance_name, model in instances:
            world_pos, world_rot, world_scale = _compute_world_transform(model, models)

            unique_asset.instances.append(
                AssetInstance(
                    instance_name=instance_name,
                    world_position=world_pos,
                    world_rotation=world_rot,
                    world_scale=world_scale,
                )
            )

        unique_asset.category = _categorize_asset(base_name)

        if asset_library:
            matched_file = _find_asset_match(base_name, asset_library)
            if matched_file:
                unique_asset.matched_file = asset_library[matched_file]
                path_cat = _categorize_asset(matched_file, asset_library[matched_file])
                if path_cat != "Uncategorized":
                    unique_asset.category = path_cat
                analysis.matched_assets.append(base_name)
            else:
                analysis.unmatched_assets.append(base_name)
        else:
            analysis.unmatched_assets.append(base_name)

        analysis.assets[base_name] = unique_asset

    # Compute statistics with safe ratio
    analysis.total_instances = sum(len(a.instances) for a in analysis.assets.values())
    analysis.unique_assets = len(analysis.assets)
    analysis.optimization_ratio = _safe_ratio(analysis.unique_assets, analysis.total_instances)

    # Category counts
    for asset in analysis.assets.values():
        cat = asset.category
        analysis.categories[cat] = analysis.categories.get(cat, 0) + len(asset.instances)

    return analysis


def _compute_world_transform(model, all_models):
    """Compute world-space position, rotation, scale by walking up the parent chain."""
    pos = list(model.translation)
    rot = list(model.rotation)
    scale = list(model.scaling)

    visited = set()
    current = model
    while current.parent_id is not None and current.parent_id in all_models:
        if current.parent_id in visited:
            break
        visited.add(current.parent_id)
        parent = all_models[current.parent_id]

        parent_scale = parent.scaling
        pos[0] = pos[0] * parent_scale[0] + parent.translation[0]
        pos[1] = pos[1] * parent_scale[1] + parent.translation[1]
        pos[2] = pos[2] * parent_scale[2] + parent.translation[2]

        rot[0] += parent.rotation[0]
        rot[1] += parent.rotation[1]
        rot[2] += parent.rotation[2]

        scale[0] *= parent_scale[0]
        scale[1] *= parent_scale[1]
        scale[2] *= parent_scale[2]

        current = parent

    return tuple(pos), tuple(rot), tuple(scale)


def _build_asset_library(root_path: str) -> Dict[str, str]:
    """Scan an asset folder and build a name -> filepath index."""
    library = {}
    supported_ext = {".fbx", ".obj", ".gltf", ".glb", ".dae"}

    for dirpath, dirnames, filenames in os.walk(root_path):
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext in supported_ext:
                asset_name = os.path.splitext(filename)[0]
                full_path = os.path.join(dirpath, filename)
                library[asset_name.lower()] = full_path

    return library


def _find_asset_match(name: str, library: Dict[str, str]) -> Optional[str]:
    """Find an asset in the library by name (case-insensitive)."""
    lower_name = name.lower()

    if lower_name in library:
        return lower_name

    for prefix in ["sm_", "sk_", "s_", "m_"]:
        if lower_name.startswith(prefix):
            stripped = lower_name[len(prefix):]
            if stripped in library:
                return stripped

    for prefix in ["sm_", "sk_"]:
        prefixed = prefix + lower_name
        if prefixed in library:
            return prefixed

    parts = lower_name.split("_")
    for i in range(len(parts) - 1, 2, -1):
        candidate = "_".join(parts[:i])
        if candidate in library:
            return candidate

    return None


def _looks_like_asset_name(name: str) -> bool:
    """Heuristic: does this name look like a placed asset vs a grouping node?"""
    if not name:
        return False
    prefixes = ["SM_", "SK_", "S_", "BP_", "Mesh", "Prop", "Bldg", "Env"]
    return any(name.startswith(p) for p in prefixes) or "_" in name


def _categorize_asset(library_key: str, file_path: str = "") -> str:
    """Derive category from asset name or file path."""
    if file_path:
        path_lower = file_path.lower().replace("\\", "/")
        path_categories = {
            "/buildings/": "Buildings",
            "/props/": "Props",
            "/environment/": "Environment",
            "/vehicle/": "Vehicles",
            "/characters/": "Characters",
            "/interiors/": "Interiors",
            "/train": "Train System",
            "/collider/": "Colliders",
        }
        for path_part, cat in path_categories.items():
            if path_part in path_lower:
                return cat

    lower = library_key.lower()
    parts = lower.split("_")

    type_map = {
        "bldg": "Buildings", "prop": "Props", "props": "Props",
        "env": "Environment", "envr": "Environment",
        "veh": "Vehicles", "vehicle": "Vehicles",
        "char": "Characters", "character": "Characters",
        "int": "Interiors", "interior": "Interiors",
        "train": "Train System", "collider": "Colliders",
        "road": "Environment", "nature": "Environment",
        "sidewalk": "Environment", "fence": "Environment",
        "terrain": "Environment", "water": "Environment",
    }

    for part in parts:
        if part in type_map:
            return type_map[part]

    substring_map = {
        "vehicle": "Vehicles", "bldg": "Buildings", "building": "Buildings",
        "prop": "Props", "interior": "Interiors", "envr": "Environment",
        "train": "Train System", "collider": "Colliders",
    }
    for substr, cat in substring_map.items():
        if substr in lower:
            return cat

    return "Uncategorized"


def analysis_to_dict(analysis: SceneAnalysis) -> dict:
    """Convert SceneAnalysis to a JSON-serializable dictionary."""
    assets_list = []
    for base_name, asset in analysis.assets.items():
        instances_list = []
        for inst in asset.instances:
            instances_list.append({
                "name": inst.instance_name,
                "position": {"x": round(inst.world_position[0], 4),
                             "y": round(inst.world_position[1], 4),
                             "z": round(inst.world_position[2], 4)},
                "rotation": {"x": round(inst.world_rotation[0], 4),
                             "y": round(inst.world_rotation[1], 4),
                             "z": round(inst.world_rotation[2], 4)},
                "scale":    {"x": round(inst.world_scale[0], 4),
                             "y": round(inst.world_scale[1], 4),
                             "z": round(inst.world_scale[2], 4)},
            })

        assets_list.append({
            "baseName": base_name,
            "instanceCount": len(asset.instances),
            "matchedFile": asset.matched_file,
            "category": asset.category,
            "instances": instances_list,
        })

    # Sort by instance count descending
    assets_list.sort(key=lambda a: a["instanceCount"], reverse=True)

    return {
        "totalInstances": analysis.total_instances,
        "uniqueAssets": analysis.unique_assets,
        "optimizationRatio": _safe_ratio(analysis.unique_assets, analysis.total_instances),
        "categories": analysis.categories,
        "matchedCount": len(analysis.matched_assets),
        "unmatchedCount": len(analysis.unmatched_assets),
        "hasLibrary": len(analysis.matched_assets) > 0 or len(analysis.unmatched_assets) < analysis.unique_assets,
        "assets": assets_list,
    }

"""
Lua Reconstruction Script Generator for Roblox Studio.

Generates a Lua script that:
1. Resolves MeshPart or Model assets by name from ServerStorage / ReplicatedStorage
2. Iterates per-instance placement data (position, rotation, scale)
3. Clones the template asset for every instance and CFrames its BaseParts into place

Pairs with blender_scripts/extract_assets.py. When extraction centers
pivots, instance positions already refer to the geometric center, so the
Lua places the asset at the stored position — no offset math.
"""

import json
import math
from typing import Dict, List, Optional, Tuple

from scene_analyzer import SceneAnalysis


# ---------------------------------------------------------------------------
# Blender (Z-up, right-handed) -> Roblox (Y-up, right-handed) transform.
#
# Blender's GLB exporter runs with export_yup=True, so the mesh geometry is
# already Y-up when Studio imports the template. But the instance placement
# data stored in the manifest is taken straight from Blender's matrix_world —
# still Z-up. Writing those values into Vector3.new(...) without conversion
# is what makes the scene come up "vertical" with weird rotations.
#
# Axis mapping (keeps the scene visually identical):
#     blender.x -> roblox.x
#     blender.z -> roblox.y   (up matches up)
#     blender.y -> roblox.-z  (Blender forward -> Roblox forward)
#
# Empirically, GLB MeshParts imported by Studio face opposite the local Z
# direction used by the converted Blender matrices. Apply that as a local
# 180-degree Y correction in the generated matrix data so asymmetric props
# keep the same front/back direction they have in Blender.
# ---------------------------------------------------------------------------

_AXIS_SWAP = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
_AXIS_SWAP_T = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
_ROBLOX_MESHPART_LOCAL_FIX = [[-1, 0, 0], [0, 1, 0], [0, 0, -1]]


def _mat_mul_3x3(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    return [
        [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]


def _scale_sign(value: float) -> float:
    return -1.0 if value < 0 else 1.0


def _euler_xyz_to_matrix(rx: float, ry: float, rz: float) -> List[List[float]]:
    """Blender 'XYZ' Euler (radians) -> rotation matrix.

    Blender's XYZ Euler is extrinsic — verified directly against
    Euler((rx,ry,rz),'XYZ').to_matrix() — so M = Rz @ Ry @ Rx as a
    matrix product applied to column vectors. The angles stored in
    extraction results come from `quat.to_euler('XYZ')` and must be
    reconstructed under the same convention here.
    """
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    rx_mat = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    ry_mat = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    rz_mat = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]
    return _mat_mul_3x3(_mat_mul_3x3(rz_mat, ry_mat), rx_mat)


def _matrix_to_euler_xyz(m: List[List[float]]) -> Tuple[float, float, float]:
    """Matrix -> Roblox CFrame.Angles(rx,ry,rz) compatible Euler (radians).

    Roblox's CFrame.Angles uses intrinsic XYZ (M = Rx @ Ry @ Rz), which is
    different from Blender's extrinsic XYZ. This inverse is intentionally
    intrinsic because its only caller feeds the result into CFrame.Angles.
    """
    sy = max(-1.0, min(1.0, m[0][2]))
    ry = math.asin(sy)
    if abs(sy) < 0.9999999:
        rx = math.atan2(-m[1][2], m[2][2])
        rz = math.atan2(-m[0][1], m[0][0])
    else:
        rx = math.atan2(m[1][0] if sy > 0 else -m[1][0], m[1][1])
        rz = 0.0
    return rx, ry, rz


def _blender_to_roblox_transform(
    pos: Tuple[float, float, float],
    rot_deg: Tuple[float, float, float],
    scale: Tuple[float, float, float],
) -> Tuple[Tuple[float, float, float],
           Tuple[float, float, float],
           Tuple[float, float, float]]:
    """Legacy: returns (roblox_pos, roblox_rot_euler_deg, roblox_scale). Used by monolithic path."""
    px, py, pz = pos
    roblox_pos = (px, pz, -py)

    rx, ry, rz = (math.radians(a) for a in rot_deg)
    m_b = _euler_xyz_to_matrix(rx, ry, rz)
    m_r = _mat_mul_3x3(_mat_mul_3x3(_AXIS_SWAP, m_b), _AXIS_SWAP_T)
    m_r = _mat_mul_3x3(m_r, _ROBLOX_MESHPART_LOCAL_FIX)
    rxr, ryr, rzr = _matrix_to_euler_xyz(m_r)
    roblox_rot = (math.degrees(rxr), math.degrees(ryr), math.degrees(rzr))

    sx, sy, sz = scale
    roblox_scale = (sx, sz, sy)

    return roblox_pos, roblox_rot, roblox_scale


def _blender_to_roblox_matrix(
    pos: Tuple[float, float, float],
    rot_deg: Tuple[float, float, float],
    scale: Tuple[float, float, float],
) -> Tuple[List[float], List[float], List[float], List[float], List[float]]:
    """Convert Blender transform to Roblox-space position + CFrame column vectors + scale.

    Returns (pos3, vX, vY, vZ, scl3) where vX/vY/vZ are the three columns
    of the rotation matrix, ready for CFrame.fromMatrix in Lua.
    Scale is returned as-is (signed). Size uses abs(scale) in Lua, while
    the signs are folded into the CFrame basis vectors here so Blender
    negative-scale/reflected instances keep their visual orientation.
    """
    px, py, pz = pos
    roblox_pos = [px, pz, -py]  # Blender Z-up -> Roblox Y-up

    rx, ry, rz = (math.radians(a) for a in rot_deg)
    m_b = _euler_xyz_to_matrix(rx, ry, rz)
    m_r = _mat_mul_3x3(_mat_mul_3x3(_AXIS_SWAP, m_b), _AXIS_SWAP_T)
    m_r = _mat_mul_3x3(m_r, _ROBLOX_MESHPART_LOCAL_FIX)

    # Column vectors of the Roblox rotation matrix
    vX = [m_r[0][0], m_r[1][0], m_r[2][0]]
    vY = [m_r[0][1], m_r[1][1], m_r[2][1]]
    vZ = [m_r[0][2], m_r[1][2], m_r[2][2]]

    sx, sy, sz = scale
    roblox_scale = [sx, sz, sy]  # Blender Y/Z -> Roblox Z/Y
    for vector, sign in ((vX, _scale_sign(roblox_scale[0])),
                         (vY, _scale_sign(roblox_scale[1])),
                         (vZ, _scale_sign(roblox_scale[2]))):
        if sign < 0:
            vector[0] = -vector[0]
            vector[1] = -vector[1]
            vector[2] = -vector[2]

    return roblox_pos, vX, vY, vZ, roblox_scale


def generate_lua_script(
    analysis: SceneAnalysis,
    scene_name: str = "ReconstructedScene",
    use_mesh_parts: bool = True,
    scale_factor: float = 1.0,
    export_results: Optional[Dict] = None,
) -> str:
    """
    Generate a Roblox Lua reconstruction script.

    Args:
        analysis: Scene analysis (used for the expected-asset list and
            per-instance placement data).
        scene_name: Name of the root Folder created in workspace.
        use_mesh_parts: If True, clone MeshPart/Model templates; if False, use InsertService.
        scale_factor: Global scale multiplier (useful for unit conversion).
        export_results: Optional extraction results. When present, its
            adjustedInstances override the analysis positions so the Lua
            uses pivot-centered coordinates.
    """
    adjusted = _index_adjusted_instances(export_results) if export_results else {}
    mode = (export_results or {}).get("mode", "") if export_results else ""
    centered = bool((export_results or {}).get("centerPivots")) if export_results else False

    lines: List[str] = []
    lines.append(_generate_header(analysis, scene_name, mode, centered))
    lines.append(_generate_asset_id_table(analysis, export_results))
    lines.append(_generate_instance_data(analysis, scale_factor, adjusted))

    if use_mesh_parts:
        lines.append(_generate_meshpart_reconstruction(scene_name))
    else:
        lines.append(_generate_insert_reconstruction(scene_name))

    lines.append(_generate_footer(analysis))

    return "\n".join(lines)


def _index_adjusted_instances(export_results: Dict) -> Dict[str, Dict[str, Dict]]:
    """Build baseName -> {instanceName -> adjusted instance data} from extraction results.

    Keying by name instead of index prevents placement drift when the Blender
    extraction produces extra instances (names absent from the manifest) that
    are inserted in the middle of the adjustedInstances list, which would shift
    all subsequent index-based lookups and place thousands of objects at wrong
    positions.
    """
    index: Dict[str, Dict[str, Dict]] = {}
    for asset in export_results.get("assets", []):
        if not asset.get("success"):
            continue
        base_name = asset.get("baseName")
        insts = asset.get("adjustedInstances")
        if base_name and insts:
            # Build instance-name -> data mapping for O(1) name-based lookup.
            index[base_name] = {inst["name"]: inst for inst in insts if "name" in inst}
    return index


def _generate_header(analysis: SceneAnalysis, scene_name: str,
                     mode: str, centered: bool) -> str:
    if mode == "single":
        import_note = (
            "Recommended: import `assets_combined.glb` into Roblox Studio ONCE\n"
            "       (Game Explorer → + → 3D Import → select the combined GLB).\n"
            "       Studio will create a Model whose children are MeshParts named\n"
            "       after each unique asset. Move that Model into ServerStorage or\n"
            "       ReplicatedStorage (or rename it to \"SceneAssets\")."
        )
    else:
        import_note = (
            "Import every .glb in the extracted_assets folder into Roblox Studio.\n"
            "       Put the resulting MeshParts inside a Folder named \"SceneAssets\"\n"
            "       in ServerStorage or ReplicatedStorage."
        )

    pivot_note = (
        "Pivots have been centered on each asset's bounding box, so the\n"
        "       stored positions are the geometric centers — placement is direct."
        if centered else
        "Pivots follow the original object origins from the source scene."
    )

    return f"""--[[
    Scene Reconstruction Script
    Generated by Scenify

    Scene: {scene_name}
    Total Instances: {analysis.total_instances}
    Unique Assets: {analysis.unique_assets}
    Optimization: {analysis.optimization_ratio:.1f}% fewer uploads needed

    SETUP:
    1. {import_note}
    2. Run this script in Roblox Studio's Command Bar or as a Script.
       Asset IDs resolve automatically by matching MeshPart or Model names.

    NOTE: {pivot_note}
    Adjust SCALE_FACTOR if your scene appears too large / too small.
--]]

local SCALE_FACTOR = 1.0         -- unit conversion (try 0.01 for cm -> studs)
local ASSET_LIBRARY_NAME = "SceneAssets"  -- folder/model containing uploaded templates
"""


def _expected_asset_names(analysis: SceneAnalysis,
                          export_results: Optional[Dict] = None) -> List[str]:
    names = set(analysis.assets.keys())
    if export_results:
        for asset in export_results.get("assets", []):
            if not asset.get("success"):
                continue
            export_name = asset.get("exportName")
            if export_name:
                names.add(export_name)
            for variant in asset.get("variants", []):
                asset_name = variant.get("assetName")
                if asset_name:
                    names.add(asset_name)
            for inst in asset.get("adjustedInstances", []):
                asset_name = inst.get("assetName")
                if asset_name:
                    names.add(asset_name)
    return sorted(names)


def _generate_asset_id_table(analysis: SceneAnalysis,
                             export_results: Optional[Dict] = None) -> str:
    lines = [
        "-- ============================================",
        "-- STEP 1: Auto-discover MeshPart/Model templates by name",
        "-- ============================================",
        "local expectedAssets = {",
    ]

    for asset_name in _expected_asset_names(analysis, export_results):
        safe = asset_name.replace('"', '\\"')
        lines.append(f'    "{safe}",')

    lines.append("}")
    lines.append("")
    lines.append(
        """-- Walks ServerStorage + ReplicatedStorage looking for MeshParts or Models
-- whose names match the expected asset list. Works whether you imported one
-- combined GLB or many individual GLBs.
local function hasBaseParts(inst)
    if inst:IsA("BasePart") then return true end
    for _, d in ipairs(inst:GetDescendants()) do
        if d:IsA("BasePart") then return true end
    end
    return false
end

local function buildTemplateMap()
    local templates = {}
    local searchRoots = {
        game:GetService("ServerStorage"),
        game:GetService("ReplicatedStorage"),
    }

    for _, root in ipairs(searchRoots) do
        local library = root:FindFirstChild(ASSET_LIBRARY_NAME, true) or root
        -- Prefer Models over same-named child MeshParts so multi-part assets
        -- reconstruct as a complete unit.
        for _, desc in ipairs(library:GetDescendants()) do
            if desc:IsA("Model") and hasBaseParts(desc) and not templates[desc.Name] then
                templates[desc.Name] = desc
            end
        end
        for _, desc in ipairs(library:GetDescendants()) do
            if desc:IsA("MeshPart") and desc.MeshId ~= "" and not templates[desc.Name] then
                templates[desc.Name] = desc
            end
        end
    end

    local found, missing = 0, {}
    for _, name in ipairs(expectedAssets) do
        if templates[name] then
            found = found + 1
        else
            table.insert(missing, name)
        end
    end
    print(string.format("[Scene Optimizer] Resolved %d/%d asset templates",
        found, #expectedAssets))
    if #missing > 0 then
        warn(string.format("[Scene Optimizer] %d assets not found (will be skipped):",
            #missing))
        for _, name in ipairs(missing) do warn("  missing: " .. name) end
    end

    return templates
end

local templates = buildTemplateMap()

-- Lookup with two fallbacks:
-- 1. Roblox GLB importer compresses names >50 chars to first23 + "..." + last24.
-- 2. Sub-component objects (doors, wheels) fall back to their parent mesh.
local function getTemplate(name)
    if templates[name] then return templates[name] end
    if #name > 50 then
        local t = templates[name:sub(1, 23) .. "..." .. name:sub(-24)]
        if t then return t end
    end
    local candidate = name
    for _ = 1, 3 do
        local parent = candidate:match("^(.+)_[^_]+$")
        if not parent then break end
        candidate = parent
        if templates[candidate] then return templates[candidate] end
        if #candidate > 50 then
            local t = templates[candidate:sub(1, 23) .. "..." .. candidate:sub(-24)]
            if t then return t end
        end
    end
    return nil
end
"""
    )
    return "\n".join(lines)


def _generate_instance_data(analysis: SceneAnalysis, scale_factor: float,
                             adjusted: Dict[str, Dict[str, Dict]]) -> str:
    lines = [
        "-- ============================================",
        "-- Instance placement data (auto-generated)",
        "-- ============================================",
        "local instances = {",
    ]

    for base_name, asset in analysis.assets.items():
        adj_by_name = adjusted.get(base_name)  # {instanceName -> data} or None
        bbox_center = asset.local_bbox_center

        for inst in asset.instances:
            # Look up by instance name to avoid index drift from extra entries
            # that the Blender extraction may have inserted mid-list.
            src = adj_by_name.get(inst.instance_name) if adj_by_name is not None else None
            if src is not None:
                px, py, pz = src.get("position", [0, 0, 0])
                rx, ry, rz = src.get("rotation", [0, 0, 0])
                sx, sy, sz = src.get("scale", [1, 1, 1])
                row_name = src.get("assetName", base_name)
            else:
                px, py, pz = _apply_bbox_center(inst, bbox_center)
                rx, ry, rz = inst.world_rotation
                sx, sy, sz = inst.world_scale
                row_name = base_name

            safe = row_name.replace('"', '\\"')
            lines.append(_format_instance_line(safe, (px, py, pz),
                                                (rx, ry, rz),
                                                (sx, sy, sz), scale_factor))

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _apply_bbox_center(inst, bbox_center: Tuple[float, float, float]
                        ) -> Tuple[float, float, float]:
    """Shift the instance's world origin to the world-space bbox center.

    extract_assets.py centers the mesh vertices on the local bbox center,
    so a centered MeshPart placed at (matrix_world.translation) would sit
    off by `rotation @ (scale * local_bbox_center)`. We undo that here."""
    if bbox_center == (0.0, 0.0, 0.0):
        return inst.world_position

    rx, ry, rz = (math.radians(a) for a in inst.world_rotation)
    m = _euler_xyz_to_matrix(rx, ry, rz)
    sx, sy, sz = inst.world_scale
    bx, by, bz = bbox_center
    scaled = (bx * sx, by * sy, bz * sz)
    offset = (
        m[0][0] * scaled[0] + m[0][1] * scaled[1] + m[0][2] * scaled[2],
        m[1][0] * scaled[0] + m[1][1] * scaled[1] + m[1][2] * scaled[2],
        m[2][0] * scaled[0] + m[2][1] * scaled[1] + m[2][2] * scaled[2],
    )
    px, py, pz = inst.world_position
    return (px + offset[0], py + offset[1], pz + offset[2])


def _format_instance_line(safe_name: str,
                           pos: Tuple[float, float, float],
                           rot: Tuple[float, float, float],
                           scl: Tuple[float, float, float],
                           scale_factor: float) -> str:
    pos, vX, vY, vZ, scl = _blender_to_roblox_matrix(pos, rot, scl)
    px, py, pz = pos
    px *= scale_factor
    py *= scale_factor
    pz *= scale_factor
    sx, sy, sz = scl
    return (
        f'    {{asset = "{safe_name}", '
        f"pos = Vector3.new({px:.4f}, {py:.4f}, {pz:.4f}), "
        f"vX = Vector3.new({vX[0]:.4f}, {vX[1]:.4f}, {vX[2]:.4f}), "
        f"vY = Vector3.new({vY[0]:.4f}, {vY[1]:.4f}, {vY[2]:.4f}), "
        f"vZ = Vector3.new({vZ[0]:.4f}, {vZ[1]:.4f}, {vZ[2]:.4f}), "
        f"scl = Vector3.new({sx:.4f}, {sy:.4f}, {sz:.4f})}},"
    )


def _generate_meshpart_reconstruction(scene_name: str) -> str:
    return f"""-- ============================================
-- STEP 2: Run this script to reconstruct the scene
-- ============================================
local function absVec3(v)
    return Vector3.new(math.abs(v.X), math.abs(v.Y), math.abs(v.Z))
end

local ROOT_PART_KEY = "__ROOT__"

local function templatePivot(template)
    if template:IsA("BasePart") then
        return template.CFrame
    end
    local bboxCFrame = template:GetBoundingBox()
    return CFrame.new(bboxCFrame.Position)
end

local function partKey(root, child)
    if child == root then return ROOT_PART_KEY end
    local parts = {{}}
    local current = child
    while current and current ~= root do
        local index = 1
        if current.Parent then
            for _, sibling in ipairs(current.Parent:GetChildren()) do
                if sibling == current then break end
                if sibling.Name == current.Name and sibling.ClassName == current.ClassName then
                    index += 1
                end
            end
        end
        table.insert(parts, 1, current.ClassName .. ":" .. current.Name .. "#" .. index)
        current = current.Parent
    end
    return table.concat(parts, "/")
end

local function collectBaseParts(root)
    local parts = {{}}
    if root:IsA("BasePart") then
        parts[ROOT_PART_KEY] = root
    end
    for _, d in ipairs(root:GetDescendants()) do
        if d:IsA("BasePart") then
            parts[partKey(root, d)] = d
        end
    end
    return parts
end

local function rotationOnly(cf)
    return CFrame.fromMatrix(Vector3.zero, cf.XVector, cf.YVector, cf.ZVector)
end

local function reconstruct()
    local sceneRoot = workspace:FindFirstChild("{scene_name}")
    if sceneRoot then sceneRoot:Destroy() end
    sceneRoot = Instance.new("Folder")
    sceneRoot.Name = "{scene_name}"
    sceneRoot.Parent = workspace

    local placed, skipped, reflected = 0, 0, 0

    -- Pre-compute each BasePart's offset relative to its template pivot.
    -- MeshPart templates have one root part; Model templates may have many.
    local templateOffsets = {{}}
    for _, template in pairs(templates) do
        local offsets = {{}}
        local pivot = templatePivot(template)
        for key, d in pairs(collectBaseParts(template)) do
            offsets[key] = {{
                size = d.Size,
                offsetCFrame = pivot:ToObjectSpace(d.CFrame),
            }}
        end
        if next(offsets) ~= nil then templateOffsets[template] = offsets end
    end

    for _, inst in ipairs(instances) do
        local template = getTemplate(inst.asset)
        if template then
            local clone = template:Clone()
            clone.Name = inst.asset
            local sclAbs = absVec3(inst.scl)
            if inst.scl.X * inst.scl.Y * inst.scl.Z < 0 then
                reflected += 1
            end

            local targetCFrame = CFrame.fromMatrix(
                inst.pos * SCALE_FACTOR,
                inst.vX,
                inst.vY,
                inst.vZ)

            local offsets = templateOffsets[template]
            if offsets then
                for key, d in pairs(collectBaseParts(clone)) do
                    local info = offsets[key]
                    if info then
                        d.Anchored = true
                        d.Size = info.size * sclAbs * SCALE_FACTOR
                        local p = info.offsetCFrame.Position
                        local scaledPos = Vector3.new(
                            p.X * sclAbs.X,
                            p.Y * sclAbs.Y,
                            p.Z * sclAbs.Z) * SCALE_FACTOR
                        d.CFrame = targetCFrame * CFrame.new(scaledPos) * rotationOnly(info.offsetCFrame)
                    end
                end
            end


            clone.Parent = sceneRoot
            placed += 1
        else
            skipped += 1
        end
    end

    print(string.format(
        "[Scene Optimizer] Reconstruction complete: %d placed, %d skipped (template missing)",
        placed, skipped
    ))
    if reflected > 0 then
        print(string.format("[Scene Optimizer] %d reflected instance(s) placed with signed CFrame basis",
            reflected))
    end
end

reconstruct()
"""


def _generate_insert_reconstruction(scene_name: str) -> str:
    return f"""-- ============================================
-- STEP 2: Run this script to reconstruct
-- (Using InsertService - for uploaded Models)
-- ============================================
local InsertService = game:GetService("InsertService")

local function absVec3(v)
    return Vector3.new(math.abs(v.X), math.abs(v.Y), math.abs(v.Z))
end

local ROOT_PART_KEY = "__ROOT__"

local function templatePivot(template)
    if template:IsA("BasePart") then
        return template.CFrame
    end
    local bboxCFrame = template:GetBoundingBox()
    return CFrame.new(bboxCFrame.Position)
end

local function partKey(root, child)
    if child == root then return ROOT_PART_KEY end
    local parts = {{}}
    local current = child
    while current and current ~= root do
        local index = 1
        if current.Parent then
            for _, sibling in ipairs(current.Parent:GetChildren()) do
                if sibling == current then break end
                if sibling.Name == current.Name and sibling.ClassName == current.ClassName then
                    index += 1
                end
            end
        end
        table.insert(parts, 1, current.ClassName .. ":" .. current.Name .. "#" .. index)
        current = current.Parent
    end
    return table.concat(parts, "/")
end

local function collectBaseParts(root)
    local parts = {{}}
    if root:IsA("BasePart") then
        parts[ROOT_PART_KEY] = root
    end
    for _, d in ipairs(root:GetDescendants()) do
        if d:IsA("BasePart") then
            parts[partKey(root, d)] = d
        end
    end
    return parts
end

local function rotationOnly(cf)
    return CFrame.fromMatrix(Vector3.zero, cf.XVector, cf.YVector, cf.ZVector)
end

local templateOffsets = {{}}
for _, template in pairs(templates) do
    local offsets = {{}}
    local pivot = templatePivot(template)
    for key, d in pairs(collectBaseParts(template)) do
        offsets[key] = {{
            size = d.Size,
            offsetCFrame = pivot:ToObjectSpace(d.CFrame),
        }}
    end
    if next(offsets) ~= nil then templateOffsets[template] = offsets end
end

local function reconstruct()
    local sceneRoot = Instance.new("Folder")
    sceneRoot.Name = "{scene_name}"
    sceneRoot.Parent = workspace

    local placed, skipped, reflected = 0, 0, 0

    for _, inst in ipairs(instances) do
        local template = getTemplate(inst.asset)
        if template then
            local clone = template:Clone()
            clone.Name = inst.asset
            local sclAbs = absVec3(inst.scl)
            if inst.scl.X * inst.scl.Y * inst.scl.Z < 0 then
                reflected += 1
            end

            local targetCFrame = CFrame.fromMatrix(
                inst.pos * SCALE_FACTOR,
                inst.vX,
                inst.vY,
                inst.vZ)

            local offsets = templateOffsets[template]
            if offsets then
                for key, d in pairs(collectBaseParts(clone)) do
                    local info = offsets[key]
                    if info then
                        d.Anchored = true
                        d.Size = info.size * sclAbs * SCALE_FACTOR
                        local p = info.offsetCFrame.Position
                        local scaledPos = Vector3.new(
                            p.X * sclAbs.X,
                            p.Y * sclAbs.Y,
                            p.Z * sclAbs.Z) * SCALE_FACTOR
                        d.CFrame = targetCFrame * CFrame.new(scaledPos) * rotationOnly(info.offsetCFrame)
                    end
                end
            end

            clone.Parent = sceneRoot
            placed += 1
        else
            skipped += 1
        end
    end

    print(string.format(
        "[Scene Optimizer] Reconstruction complete: %d placed, %d skipped",
        placed, skipped
    ))
    if reflected > 0 then
        print(string.format("[Scene Optimizer] %d reflected instance(s) placed with signed CFrame basis",
            reflected))
    end
end

reconstruct()
"""


def _generate_footer(analysis: SceneAnalysis) -> str:
    return f"""
--[[
    Asset Summary:
    - Unique assets to upload: {analysis.unique_assets}
    - Total instances placed: {analysis.total_instances}
    - Optimization: {analysis.optimization_ratio:.1f}% reduction in uploads
]]
"""


# ---------------------------------------------------------------------------
# Split-output mode: tiny command-bar runner + rbxmx data module.
#
# Pasting a single ~10MB Lua into Studio's command bar freezes the UI for
# minutes because the widget has to render every character and the Lua
# parser has to build 50k+ AST nodes. Splitting the output into:
#   1. reconstruct_runner.lua  (a few KB — trivial paste)
#   2. scene_data.rbxmx         (drag-drop into ReplicatedStorage once)
# gives an instant command-bar paste and a fast rbxmx import, and since
# the data module holds a single long-string JSON literal, require-time
# Lua parsing is O(string length) instead of O(instance count).
# ---------------------------------------------------------------------------


def generate_split_output(
    analysis: SceneAnalysis,
    scene_name: str = "ReconstructedScene",
    scale_factor: float = 1.0,
    export_results: Optional[Dict] = None,
) -> Tuple[str, str]:
    """Return (runner_lua, data_rbxmx). See module docstring for usage."""
    json_blob = _build_instance_json(analysis, scene_name, scale_factor, export_results)
    runner = _build_runner_lua(scene_name)
    data_rbxmx = _wrap_json_as_rbxmx(json_blob, module_name="SceneData")
    return runner, data_rbxmx


def _build_instance_json(
    analysis: SceneAnalysis,
    scene_name: str,
    scale_factor: float,
    export_results: Optional[Dict],
) -> str:
    """Build compact JSON payload for the rbxmx data module.

    Source-of-truth priority:
      1. adjustedInstances from export_results  — complete, pivot-corrected.
         Iterate ALL entries directly; manifest may be missing some objects.
      2. Manifest instances (fallback when no extraction results exist).
    """
    expected_assets = _expected_asset_names(analysis, export_results)
    instances: List[List] = []

    if export_results:
        # Use adjustedInstances as the authoritative list — it contains every
        # instance Blender knows about, including ones the manifest missed.
        for asset in export_results.get("assets", []):
            if not asset.get("success"):
                continue
            base_name = asset.get("baseName", "")
            if not base_name:
                continue
            manifest_asset = analysis.assets.get(base_name)
            bbox_center = manifest_asset.local_bbox_center if manifest_asset else (0.0, 0.0, 0.0)
            for src in asset.get("adjustedInstances", []):
                raw_pos = tuple(src.get("position", [0, 0, 0]))
                raw_rot = tuple(src.get("rotation", [0, 0, 0]))
                raw_scl = tuple(src.get("scale", [1, 1, 1]))
                asset_name = src.get("assetName", base_name)
                instances.append(_compact_row(asset_name, raw_pos, raw_rot,
                                              raw_scl, scale_factor))
    else:
        # No extraction results — fall back to manifest positions.
        for base_name, asset in analysis.assets.items():
            bbox_center = asset.local_bbox_center
            for inst in asset.instances:
                raw_pos = _apply_bbox_center(inst, bbox_center)
                instances.append(_compact_row(base_name, raw_pos,
                                              inst.world_rotation,
                                              inst.world_scale, scale_factor))

    payload = {
        "sceneName": scene_name,
        "expectedAssets": expected_assets,
        "instances": instances,
    }
    return json.dumps(payload, separators=(",", ":"))


def _compact_row(
    name: str,
    pos: Tuple[float, float, float],
    rot: Tuple[float, float, float],
    scl: Tuple[float, float, float],
    scale_factor: float,
) -> List:
    """Emit a 6-field row: [name, pos, vX, vY, vZ, scl].

    vX/vY/vZ are the column vectors of the Roblox rotation matrix, ready for
    CFrame.fromMatrix. This avoids Euler-angle gimbal lock and the FLIP_Y hack.
    Scale stays signed. Size uses abs(scale), while the generated CFrame
    basis vectors already include the signs for reflected Blender instances.
    """
    rp, vX, vY, vZ, rs = _blender_to_roblox_matrix(pos, rot, scl)
    R = lambda v: [round(x, 4) for x in v]
    return [
        name,
        [round(rp[0] * scale_factor, 4),
         round(rp[1] * scale_factor, 4),
         round(rp[2] * scale_factor, 4)],
        R(vX), R(vY), R(vZ),
        R(rs),
    ]


def _build_runner_lua(scene_name: str) -> str:
    return f"""--[[
    Scene Reconstruction Runner (split mode)

    Before running this, import scene_data.rbxmx into ReplicatedStorage:
        1. Right-click ReplicatedStorage in the Explorer.
        2. "Insert From File..." → select scene_data.rbxmx.
        3. A ModuleScript named "SceneData" will appear.

    Then paste this script into the command bar. It's tiny so the paste
    is instant; the heavy instance data lives in the pre-imported module.
--]]

local HttpService = game:GetService("HttpService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local SCALE_FACTOR = 1.0
local ASSET_LIBRARY_NAME = "SceneAssets"
local DATA_MODULE_NAME = "SceneData"
local SCENE_NAME = "{scene_name}"

local dataModule = ReplicatedStorage:FindFirstChild(DATA_MODULE_NAME, true)
assert(dataModule and dataModule:IsA("ModuleScript"),
    "SceneData ModuleScript not found in ReplicatedStorage — import scene_data.rbxmx first.")

local sceneData = HttpService:JSONDecode(require(dataModule))
local expectedAssets = sceneData.expectedAssets
local instanceRows = sceneData.instances

local function buildTemplateMap()
    local templates = {{}}
    local searchRoots = {{
        game:GetService("ServerStorage"),
        ReplicatedStorage,
    }}

    local function hasBaseParts(inst)
        if inst:IsA("BasePart") then return true end
        for _, d in ipairs(inst:GetDescendants()) do
            if d:IsA("BasePart") then return true end
        end
        return false
    end

    for _, root in ipairs(searchRoots) do
        local library = root:FindFirstChild(ASSET_LIBRARY_NAME, true) or root
        -- Prefer Models over same-named child MeshParts so multi-part assets
        -- reconstruct as a complete unit.
        for _, desc in ipairs(library:GetDescendants()) do
            if desc:IsA("Model") and hasBaseParts(desc) and not templates[desc.Name] then
                templates[desc.Name] = desc
            end
        end
        for _, desc in ipairs(library:GetDescendants()) do
            if desc:IsA("MeshPart") and desc.MeshId ~= "" and not templates[desc.Name] then
                templates[desc.Name] = desc
            end
        end
    end
    return templates
end

local templates = buildTemplateMap()

-- Lookup with two fallbacks:
-- 1. Roblox GLB importer compresses names >50 chars to first23 + "..." + last24.
-- 2. Sub-component objects fall back to their parent mesh name.
local function getTemplate(name)
    if templates[name] then return templates[name] end
    if #name > 50 then
        local t = templates[name:sub(1, 23) .. "..." .. name:sub(-24)]
        if t then return t end
    end
    local candidate = name
    for _ = 1, 3 do
        local parent = candidate:match("^(.+)_[^_]+$")
        if not parent then break end
        candidate = parent
        if templates[candidate] then return templates[candidate] end
        if #candidate > 50 then
            local t = templates[candidate:sub(1, 23) .. "..." .. candidate:sub(-24)]
            if t then return t end
        end
    end
    return nil
end

-- Report resolution stats
local found, missing = 0, {{}}
for _, name in ipairs(expectedAssets) do
    if getTemplate(name) then found += 1 else table.insert(missing, name) end
end
print(string.format("[Scene Optimizer] Resolved %d/%d asset templates",
    found, #expectedAssets))
if #missing > 0 then
    warn(string.format("[Scene Optimizer] %d assets not found (will be skipped):", #missing))
    for _, name in ipairs(missing) do warn("  missing: " .. name) end
end

-- MeshPart Size cannot be negative, so dimensions use abs(scale).
-- Negative-scale/reflected instances are represented in vX/vY/vZ instead.
local function absVec3(x, y, z)
    return Vector3.new(math.abs(x), math.abs(y), math.abs(z))
end

local ROOT_PART_KEY = "__ROOT__"

local function templatePivot(template)
    if template:IsA("BasePart") then
        return template.CFrame
    end
    local bboxCFrame = template:GetBoundingBox()
    return CFrame.new(bboxCFrame.Position)
end

local function partKey(root, child)
    if child == root then return ROOT_PART_KEY end
    local parts = {{}}
    local current = child
    while current and current ~= root do
        local index = 1
        if current.Parent then
            for _, sibling in ipairs(current.Parent:GetChildren()) do
                if sibling == current then break end
                if sibling.Name == current.Name and sibling.ClassName == current.ClassName then
                    index += 1
                end
            end
        end
        table.insert(parts, 1, current.ClassName .. ":" .. current.Name .. "#" .. index)
        current = current.Parent
    end
    return table.concat(parts, "/")
end

local function collectBaseParts(root)
    local parts = {{}}
    if root:IsA("BasePart") then
        parts[ROOT_PART_KEY] = root
    end
    for _, d in ipairs(root:GetDescendants()) do
        if d:IsA("BasePart") then
            parts[partKey(root, d)] = d
        end
    end
    return parts
end

local function rotationOnly(cf)
    return CFrame.fromMatrix(Vector3.zero, cf.XVector, cf.YVector, cf.ZVector)
end

-- Cache BasePart offsets once per template. MeshPart templates have one root
-- part; Model templates may have many descendant parts.
local templateOffsets = {{}}
for _, template in pairs(templates) do
    local offsets = {{}}
    local pivot = templatePivot(template)
    for key, d in pairs(collectBaseParts(template)) do
        offsets[key] = {{
            size = d.Size,
            offsetCFrame = pivot:ToObjectSpace(d.CFrame),
        }}
    end
    if next(offsets) ~= nil then templateOffsets[template] = offsets end
end

local function reconstruct()
    local sceneRoot = workspace:FindFirstChild(SCENE_NAME)
    if sceneRoot then sceneRoot:Destroy() end
    sceneRoot = Instance.new("Folder")
    sceneRoot.Name = SCENE_NAME
    sceneRoot.Parent = workspace

    local placed, skipped, reflected = 0, 0, 0

    for _, row in ipairs(instanceRows) do
        -- Row shape: {{assetName, pos, vX, vY, vZ, scl}}
        -- vX/vY/vZ are the columns of the rotation matrix for CFrame.fromMatrix.
        local assetName = row[1]
        local pos = row[2]
        local vX  = row[3]
        local vY  = row[4]
        local vZ  = row[5]
        local scl = row[6]
        local template = getTemplate(assetName)
        if template then
            local clone = template:Clone()
            clone.Name = assetName
            local sclAbs = absVec3(scl[1], scl[2], scl[3])
            if scl[1] * scl[2] * scl[3] < 0 then
                reflected += 1
            end

            local targetCFrame = CFrame.fromMatrix(
                Vector3.new(pos[1] * SCALE_FACTOR,
                            pos[2] * SCALE_FACTOR,
                            pos[3] * SCALE_FACTOR),
                Vector3.new(vX[1], vX[2], vX[3]),
                Vector3.new(vY[1], vY[2], vY[3]),
                Vector3.new(vZ[1], vZ[2], vZ[3]))

            local offsets = templateOffsets[template]
            if offsets then
                for key, d in pairs(collectBaseParts(clone)) do
                    local info = offsets[key]
                    if info then
                        d.Anchored = true
                        d.Size = info.size * sclAbs * SCALE_FACTOR
                        local p = info.offsetCFrame.Position
                        local scaledPos = Vector3.new(
                            p.X * sclAbs.X,
                            p.Y * sclAbs.Y,
                            p.Z * sclAbs.Z) * SCALE_FACTOR
                        d.CFrame = targetCFrame * CFrame.new(scaledPos) * rotationOnly(info.offsetCFrame)
                    end
                end
            end

            clone.Parent = sceneRoot
            placed += 1
        else
            skipped += 1
        end
    end

    print(string.format("[Scene Optimizer] Reconstruction complete: %d placed, %d skipped",
        placed, skipped))
    if reflected > 0 then
        print(string.format("[Scene Optimizer] %d reflected instance(s) placed with signed CFrame basis",
            reflected))
    end
end

reconstruct()
"""


def _wrap_json_as_rbxmx(json_blob: str, module_name: str = "SceneData") -> str:
    """Package a JSON string inside a ModuleScript rbxmx file.

    The Source is `return [===[<json>]===]` so Lua parses a single long-string
    literal at require-time (fast), and the runtime JSONDecode handles the
    actual structure in native C code.
    """
    # JSON never emits `[===[` or `]===]`, so the level-3 long bracket is safe.
    lua_source = f"return [===[{json_blob}]===]"
    # CDATA closes on ']]>', which can't appear in our Lua source (no '>').
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" '
        'version="4">\n'
        '  <External>null</External>\n'
        '  <External>nil</External>\n'
        '  <Item class="ModuleScript" referent="RBX_SCENE_DATA_1">\n'
        '    <Properties>\n'
        f'      <string name="Name">{module_name}</string>\n'
        f'      <ProtectedString name="Source"><![CDATA[{lua_source}]]></ProtectedString>\n'
        '    </Properties>\n'
        '  </Item>\n'
        '</roblox>\n'
    )

"""
Blender Scene Analyzer
Run headlessly: blender --background --python analyze_scene.py -- input_file output_json

Imports any supported 3D file, enumerates mesh objects,
deduplicates by mesh geometry fingerprint, and outputs JSON.
"""
import bpy
import json
import sys
import os
import math
import hashlib
import struct
import re


def get_args():
    argv = sys.argv
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return []


def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()
    for block in bpy.data.meshes:
        if block.users == 0:
            bpy.data.meshes.remove(block)
    for block in bpy.data.materials:
        if block.users == 0:
            bpy.data.materials.remove(block)
    for block in bpy.data.images:
        if block.users == 0:
            bpy.data.images.remove(block)


def import_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.blend':
        bpy.ops.wm.open_mainfile(filepath=filepath)
    elif ext == '.fbx':
        bpy.ops.import_scene.fbx(filepath=filepath)
    elif ext == '.obj':
        # Try new importer first, fall back to old
        try:
            bpy.ops.wm.obj_import(filepath=filepath)
        except AttributeError:
            bpy.ops.import_scene.obj(filepath=filepath)
    elif ext in ('.gltf', '.glb'):
        bpy.ops.import_scene.gltf(filepath=filepath)
    elif ext == '.dae':
        bpy.ops.wm.collada_import(filepath=filepath)
    elif ext == '.3ds':
        try:
            bpy.ops.import_scene.autodesk_3ds(filepath=filepath)
        except AttributeError:
            bpy.ops.wm.autodesk_3ds_import(filepath=filepath)
    elif ext == '.stl':
        try:
            bpy.ops.wm.stl_import(filepath=filepath)
        except AttributeError:
            bpy.ops.import_mesh.stl(filepath=filepath)
    elif ext == '.ply':
        try:
            bpy.ops.wm.ply_import(filepath=filepath)
        except AttributeError:
            bpy.ops.import_mesh.ply(filepath=filepath)
    elif ext in ('.abc',):
        bpy.ops.wm.alembic_import(filepath=filepath)
    elif ext in ('.usd', '.usda', '.usdc', '.usdz'):
        bpy.ops.wm.usd_import(filepath=filepath)
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def strip_instance_suffix(name):
    """Strip .001, .002, _instance1, (1) etc. from names."""
    result = name.strip()
    result = re.sub(r'\.\d{3,}$', '', result)
    result = re.sub(r'_instance\d*$', '', result, flags=re.IGNORECASE)
    result = re.sub(r'_copy\d*$', '', result, flags=re.IGNORECASE)
    result = re.sub(r'\s*\(\d+\)$', '', result)
    return result


def compute_local_bbox_center(mesh):
    """Bbox center of the mesh in LOCAL space (before any world transform).
    Stored per-asset so downstream tools can place the centered mesh at the
    correct world position without re-importing the scene."""
    if not mesh.vertices:
        return (0.0, 0.0, 0.0)
    min_x = min_y = min_z = float('inf')
    max_x = max_y = max_z = float('-inf')
    for v in mesh.vertices:
        co = v.co
        if co.x < min_x: min_x = co.x
        if co.x > max_x: max_x = co.x
        if co.y < min_y: min_y = co.y
        if co.y > max_y: max_y = co.y
        if co.z < min_z: min_z = co.z
        if co.z > max_z: max_z = co.z
    return ((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, (min_z + max_z) * 0.5)


def mesh_fingerprint(mesh):
    """
    Create a geometry fingerprint from mesh data.
    Objects sharing the same mesh data (linked duplicates) get the same hash.
    For non-linked meshes, we hash vertex count + polygon count + bounds.
    """
    # If multiple objects share the same mesh datablock, they're the same asset
    # Use the mesh datablock name as primary key
    h = hashlib.md5()
    h.update(struct.pack('<I', len(mesh.vertices)))
    h.update(struct.pack('<I', len(mesh.polygons)))
    h.update(struct.pack('<I', len(mesh.edges)))

    # Add bounding box for additional discrimination
    if mesh.vertices:
        coords = [v.co for v in mesh.vertices]
        min_x = min(c.x for c in coords)
        max_x = max(c.x for c in coords)
        min_y = min(c.y for c in coords)
        max_y = max(c.y for c in coords)
        min_z = min(c.z for c in coords)
        max_z = max(c.z for c in coords)
        # Round to avoid floating point noise
        h.update(struct.pack('<6f',
            round(min_x, 3), round(max_x, 3),
            round(min_y, 3), round(max_y, 3),
            round(min_z, 3), round(max_z, 3)))

        # Sample some vertex positions for extra discrimination
        step = max(1, len(mesh.vertices) // 20)
        for i in range(0, len(mesh.vertices), step):
            v = mesh.vertices[i].co
            h.update(struct.pack('<3f', round(v.x, 3), round(v.y, 3), round(v.z, 3)))

    return h.hexdigest()


def get_material_info(obj):
    """Extract material info from an object."""
    materials = []
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue

        mat_info = {
            "name": mat.name,
            "diffuse_color": [1.0, 1.0, 1.0],
            "has_texture": False,
            "texture_name": "",
        }

        if mat.use_nodes and mat.node_tree:
            # Find Principled BSDF
            for node in mat.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    base_color = node.inputs.get('Base Color')
                    if base_color:
                        if hasattr(base_color, 'default_value'):
                            c = base_color.default_value
                            mat_info["diffuse_color"] = [c[0], c[1], c[2]]
                        # Check for connected texture
                        if base_color.links:
                            linked = base_color.links[0].from_node
                            if linked.type == 'TEX_IMAGE' and linked.image:
                                mat_info["has_texture"] = True
                                mat_info["texture_name"] = linked.image.name
                    break
        else:
            # Non-node material
            mat_info["diffuse_color"] = [mat.diffuse_color[0], mat.diffuse_color[1], mat.diffuse_color[2]]

        materials.append(mat_info)
    return materials


def categorize_asset(name):
    """Categorize asset from its name."""
    lower = name.lower()
    parts = lower.replace("-", "_").split("_")

    type_map = {
        "bldg": "Buildings", "building": "Buildings",
        "prop": "Props", "props": "Props",
        "env": "Environment", "envr": "Environment",
        "veh": "Vehicles", "vehicle": "Vehicles",
        "char": "Characters", "character": "Characters",
        "int": "Interiors", "interior": "Interiors",
        "train": "Train System",
        "collider": "Colliders",
        "road": "Environment", "nature": "Environment",
        "sidewalk": "Environment", "fence": "Environment",
        "terrain": "Environment", "water": "Environment",
        "tree": "Environment", "rock": "Environment",
        "light": "Lighting", "lamp": "Lighting",
        "sign": "Props", "bench": "Props",
        "door": "Buildings", "window": "Buildings",
        "wall": "Buildings", "roof": "Buildings",
    }

    for part in parts:
        if part in type_map:
            return type_map[part]

    # Substring check
    for substr in ["vehicle", "bldg", "building", "prop", "interior", "envr",
                    "train", "collider", "terrain"]:
        if substr in lower:
            return type_map.get(substr, "Uncategorized")

    return "Uncategorized"


def analyze_scene():
    """Analyze all mesh objects in the current Blender scene."""
    # Collect all mesh objects
    objects = [obj for obj in bpy.data.objects if obj.type == 'MESH']

    # Group by mesh fingerprint + base name for deduplication
    # Two objects are "the same asset" if they share the same mesh datablock
    # OR if they have the same base name and same geometry fingerprint
    mesh_groups = {}  # key -> {base_name, fingerprint, instances, materials, ...}

    for obj in objects:
        if not obj.data or len(obj.data.vertices) == 0:
            continue

        base_name = strip_instance_suffix(obj.name)
        mesh = obj.data

        # Use mesh datablock name as primary grouping (catches linked duplicates)
        mesh_datablock_name = mesh.name
        fp = mesh_fingerprint(mesh)

        # Group key: prefer mesh datablock for linked duplicates,
        # otherwise use base_name + fingerprint
        group_key = mesh_datablock_name

        # Get world transform
        loc = obj.matrix_world.translation
        rot = obj.matrix_world.to_euler('XYZ')
        scl = obj.matrix_world.to_scale()

        instance_data = {
            "name": obj.name,
            "position": [round(loc.x, 4), round(loc.y, 4), round(loc.z, 4)],
            "rotation": [round(math.degrees(rot.x), 4),
                         round(math.degrees(rot.y), 4),
                         round(math.degrees(rot.z), 4)],
            "scale": [round(scl.x, 4), round(scl.y, 4), round(scl.z, 4)],
        }

        if group_key not in mesh_groups:
            materials = get_material_info(obj)
            bbox_center = compute_local_bbox_center(mesh)
            mesh_groups[group_key] = {
                "base_name": base_name,
                "display_name": base_name,
                "fingerprint": fp,
                "vertex_count": len(mesh.vertices),
                "polygon_count": len(mesh.polygons),
                "edge_count": len(mesh.edges),
                "materials": materials,
                "category": categorize_asset(base_name),
                "instances": [],
                "blender_object_name": obj.name,  # First object (used for extraction)
                "local_bbox_center": [round(bbox_center[0], 6),
                                       round(bbox_center[1], 6),
                                       round(bbox_center[2], 6)],
            }

        mesh_groups[group_key]["instances"].append(instance_data)

    # Now merge groups that have the same base_name AND same fingerprint
    # (different mesh datablocks but identical geometry)
    merged = {}
    fp_to_key = {}  # (base_name, fingerprint) -> merged key

    for group_key, group_data in mesh_groups.items():
        merge_key = (group_data["base_name"].lower(), group_data["fingerprint"])
        if merge_key in fp_to_key:
            existing_key = fp_to_key[merge_key]
            merged[existing_key]["instances"].extend(group_data["instances"])
        else:
            fp_to_key[merge_key] = group_key
            merged[group_key] = group_data

    return merged


def build_output(mesh_groups, input_file):
    """Build the final JSON output."""
    total_instances = sum(len(g["instances"]) for g in mesh_groups.values())
    unique_assets = len(mesh_groups)

    if total_instances > 0 and unique_assets > 0:
        optimization_ratio = round((1 - unique_assets / total_instances) * 100, 1)
    else:
        optimization_ratio = 0.0

    # Ensure ratio is valid
    if optimization_ratio != optimization_ratio:  # NaN check
        optimization_ratio = 0.0
    optimization_ratio = max(0.0, optimization_ratio)

    # Build category counts
    categories = {}
    for group in mesh_groups.values():
        cat = group["category"]
        categories[cat] = categories.get(cat, 0) + len(group["instances"])

    # Sort by instance count descending
    assets_list = []
    for group_key, group in sorted(mesh_groups.items(),
                                    key=lambda x: len(x[1]["instances"]),
                                    reverse=True):
        assets_list.append({
            "baseName": group["display_name"],
            "instanceCount": len(group["instances"]),
            "category": group["category"],
            "vertexCount": group["vertex_count"],
            "polygonCount": group["polygon_count"],
            "materials": group["materials"],
            "blenderObjectName": group["blender_object_name"],
            "localBboxCenter": group["local_bbox_center"],
            "instances": group["instances"],
        })

    return {
        "source": "blender_analyzer",
        "blenderVersion": bpy.app.version_string,
        "inputFile": os.path.basename(input_file),
        "totalInstances": total_instances,
        "uniqueAssets": unique_assets,
        "optimizationRatio": optimization_ratio,
        "categories": categories,
        "assets": assets_list,
    }


def main():
    args = get_args()
    if len(args) < 2:
        print("Usage: blender --background --python analyze_scene.py -- input_file output_json")
        sys.exit(1)

    input_file = args[0]
    output_json = args[1]

    print(f"PROGRESS:Importing scene...")

    clear_scene()
    import_file(input_file)

    print(f"PROGRESS:Analyzing {len(bpy.data.objects)} objects...")

    mesh_groups = analyze_scene()

    print(f"PROGRESS:Building manifest...")

    output = build_output(mesh_groups, input_file)

    os.makedirs(os.path.dirname(output_json) or '.', exist_ok=True)
    with open(output_json, 'w') as f:
        json.dump(output, f, indent=2)

    total = output["totalInstances"]
    unique = output["uniqueAssets"]
    ratio = output["optimizationRatio"]
    print(f"PROGRESS:Analysis complete: {unique} unique assets, {total} instances, {ratio}% optimization")
    print(f"RESULT:SUCCESS")


if __name__ == "__main__":
    main()

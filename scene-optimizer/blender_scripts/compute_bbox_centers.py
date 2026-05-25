"""
Lightweight re-extraction — computes adjusted instance positions
(world-space bbox centers) from an existing manifest without re-exporting
any GLBs. Writes an _extraction_results.json that lua_generator.py can
consume via the export_results argument.

Usage:
    blender --background --python compute_bbox_centers.py -- \
        input_file output_dir manifest_json
"""
import bpy
import json
import os
import re
import sys
from mathutils import Vector


def get_args():
    argv = sys.argv
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return []


def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()


def import_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.blend':
        bpy.ops.wm.open_mainfile(filepath=filepath)
    elif ext == '.fbx':
        bpy.ops.import_scene.fbx(filepath=filepath)
    elif ext in ('.gltf', '.glb'):
        bpy.ops.import_scene.gltf(filepath=filepath)
    elif ext == '.obj':
        try:
            bpy.ops.wm.obj_import(filepath=filepath)
        except AttributeError:
            bpy.ops.import_scene.obj(filepath=filepath)
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def strip_instance_suffix(name):
    result = name.strip()
    result = re.sub(r'\.\d{3,}$', '', result)
    result = re.sub(r'_instance\d*$', '', result, flags=re.IGNORECASE)
    result = re.sub(r'_copy\d*$', '', result, flags=re.IGNORECASE)
    result = re.sub(r'\s*\(\d+\)$', '', result)
    return result


def compute_local_bbox_center(mesh):
    if not mesh.vertices:
        return Vector((0.0, 0.0, 0.0))
    min_co = Vector((float('inf'),) * 3)
    max_co = Vector((float('-inf'),) * 3)
    for v in mesh.vertices:
        co = v.co
        if co.x < min_co.x: min_co.x = co.x
        if co.y < min_co.y: min_co.y = co.y
        if co.z < min_co.z: min_co.z = co.z
        if co.x > max_co.x: max_co.x = co.x
        if co.y > max_co.y: max_co.y = co.y
        if co.z > max_co.z: max_co.z = co.z
    return (min_co + max_co) * 0.5


def main():
    positional = get_args()
    if len(positional) < 3:
        print("Usage: ... -- input_file output_dir manifest_json")
        sys.exit(1)

    input_file, output_dir, manifest_path = positional[:3]
    os.makedirs(output_dir, exist_ok=True)

    with open(manifest_path, 'r') as f:
        manifest = json.load(f)

    print("PROGRESS:Importing scene...")
    clear_scene()
    import_file(input_file)

    mesh_objs = [o for o in bpy.data.objects if o.type == 'MESH']
    obj_by_name = {o.name: o for o in mesh_objs}
    obj_by_base = {}
    for o in mesh_objs:
        obj_by_base.setdefault(strip_instance_suffix(o.name), o)

    print(f"PROGRESS:Computing bbox centers for {len(manifest.get('assets', []))} assets...")

    results_assets = []
    for asset in manifest.get("assets", []):
        base_name = asset["baseName"]
        template = (
            obj_by_name.get(asset.get("blenderObjectName")) or
            obj_by_base.get(base_name)
        )
        if template is None or template.data is None:
            results_assets.append({
                "baseName": base_name,
                "success": False,
                "error": "Template not found in scene",
            })
            continue

        mesh = template.data
        bbox_center = compute_local_bbox_center(mesh)

        adjusted = []
        for inst_meta in asset.get("instances", []):
            inst_obj = obj_by_name.get(inst_meta.get("name")) or template
            world_pivot = inst_obj.matrix_world @ bbox_center

            adjusted.append({
                "name": inst_meta.get("name", base_name),
                "position": [round(world_pivot.x, 4),
                             round(world_pivot.y, 4),
                             round(world_pivot.z, 4)],
                "rotation": inst_meta.get("rotation", [0, 0, 0]),
                "scale": inst_meta.get("scale", [1, 1, 1]),
            })

        results_assets.append({
            "baseName": base_name,
            "success": True,
            "pivotOffset": [round(bbox_center.x, 6),
                            round(bbox_center.y, 6),
                            round(bbox_center.z, 6)],
            "adjustedInstances": adjusted,
        })

    out = {
        "totalAssets": len(results_assets),
        "exported": sum(1 for r in results_assets if r.get("success")),
        "failed": sum(1 for r in results_assets if not r.get("success")),
        "outputDir": output_dir,
        "mode": "bbox_only",
        "centerPivots": True,
        "combinedFile": None,
        "assets": results_assets,
    }

    results_path = os.path.join(output_dir, "_extraction_results.json")
    with open(results_path, 'w') as f:
        json.dump(out, f, indent=2)

    print(f"PROGRESS:Wrote {results_path} ({out['exported']}/{out['totalAssets']} assets resolved)")
    print("RESULT:SUCCESS")


if __name__ == "__main__":
    main()

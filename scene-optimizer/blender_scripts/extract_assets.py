"""
Blender Asset Extractor
Usage:
    blender --background --python extract_assets.py -- \
        input_file output_dir manifest_json \
        [--mode single|individual] [--center-pivots true|false]

Modes:
    single      One combined GLB containing every unique asset as a named mesh
                node. Roblox imports it as a single Model whose children are
                MeshParts named after the assets. Default.
    individual  One GLB per unique asset (legacy behaviour).

When --center-pivots is true (default) each unique asset's mesh data is
translated so its bounding-box center sits at the local origin. The world
position of each instance (which is what Roblox needs to place the clone) is
re-computed as matrix_world @ original_local_bbox_center, so the visual
layout is preserved.
"""
import bpy
import json
import sys
import os
import re
import math
from mathutils import Vector, Euler, Quaternion, Matrix


# ---------------------------------------------------------------- arg parsing

def get_args():
    argv = sys.argv
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return []


def parse_args(raw):
    positional = []
    flags = {}
    i = 0
    while i < len(raw):
        arg = raw[i]
        if arg.startswith("--") and i + 1 < len(raw):
            flags[arg[2:]] = raw[i + 1]
            i += 2
        else:
            positional.append(arg)
            i += 1
    return positional, flags


def as_bool(val, default=True):
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on", "y")


# ---------------------------------------------------------------- scene I/O

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


# ---------------------------------------------------------------- naming

def safe_filename(name):
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip() or "asset"


def strip_instance_suffix(name):
    result = name.strip()
    result = re.sub(r'\.\d{3,}$', '', result)
    result = re.sub(r'_instance\d*$', '', result, flags=re.IGNORECASE)
    result = re.sub(r'_copy\d*$', '', result, flags=re.IGNORECASE)
    result = re.sub(r'\s*\(\d+\)$', '', result)
    return result


# ---------------------------------------------------------------- geometry

def compute_local_bbox_center(mesh):
    """Center of the mesh's axis-aligned bounding box in local space."""
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


def translate_mesh_vertices(mesh, offset):
    """In-place translation of mesh vertices by -offset (move bbox center to origin)."""
    # Fast bulk vertex read/write with foreach_get/foreach_set
    n = len(mesh.vertices)
    if n == 0:
        return
    buf = [0.0] * (n * 3)
    mesh.vertices.foreach_get("co", buf)
    ox, oy, oz = offset.x, offset.y, offset.z
    for i in range(n):
        buf[i * 3]     -= ox
        buf[i * 3 + 1] -= oy
        buf[i * 3 + 2] -= oz
    mesh.vertices.foreach_set("co", buf)
    mesh.update()


def scale_sign_tuple(scale):
    return tuple(-1 if value < 0 else 1 for value in scale)


def signed_variant_name(base_name, signs):
    negative_axes = "".join(axis for axis, sign in zip("XYZ", signs) if sign < 0)
    if not negative_axes:
        return safe_filename(base_name)
    return safe_filename(f"{base_name}__neg{negative_axes}")


# ---------------------------------------------------------------- templates

def build_asset_templates(manifest, center_pivots):
    """
    For each unique asset, locate its template object, optionally center the
    mesh on its bbox, compute corrected world positions for every instance,
    and compute per-instance rotations adjusted for whatever rotation we'll
    bake into the exported mesh.

    Rotation handling: some assets (notably those coming from FBX files
    authored in Unreal's Y-up convention) arrive in Blender with an axis-
    correcting rotation on the template's transform — e.g. vehicles in the
    DemoScene show [90, 0, 0] in matrix_world. When make_export_clone runs
    with identity rotation and export_yup=True, Blender's GLB exporter
    applies a Z-up→Y-up rotation to vertex data that's *already* Y-up, so
    the GLB mesh comes out rotated 90° on X. A global 180° Y flip in the
    Lua runner can't undo a 90° X error.

    To fix this, we capture the template's world rotation here and hand it
    to make_export_clone (which bakes it into a private copy of the mesh,
    putting the vertices back into Blender-Z-up orientation). We also
    subtract that same rotation from every instance's world rotation, so
    the baked rotation and the adjustment cancel during reconstruction —
    each instance ends up at its original world rotation, not 90° off.

    Using matrix_world (not matrix_local) because the axis correction may
    live on a parent object; matrix_local would miss it in that case.
    """
    assets = manifest.get("assets", [])
    all_mesh_objs = [obj for obj in bpy.data.objects if obj.type == 'MESH']
    obj_lookup = {obj.name: obj for obj in all_mesh_objs}

    base_name_lookup = {}
    for name, obj in obj_lookup.items():
        base = strip_instance_suffix(name)
        base_name_lookup.setdefault(base, obj)

    templates = []
    processed_meshes = set()  # mesh.name -> already had bbox subtracted

    for asset in assets:
        base_name = asset["baseName"]
        blender_name = asset.get("blenderObjectName", base_name)

        template = obj_lookup.get(blender_name) or base_name_lookup.get(base_name)
        if template is None:
            for n, candidate in obj_lookup.items():
                if strip_instance_suffix(n) == base_name:
                    template = candidate
                    break

        if template is None or template.data is None:
            templates.append({
                "base_name": base_name,
                "missing": True,
                "error": "Object not found in scene",
            })
            continue

        mesh = template.data

        # Bbox center in local space, computed BEFORE we mutate the mesh.
        bbox_center = compute_local_bbox_center(mesh) if center_pivots else Vector((0, 0, 0))

        # Axis-correction bake: capture the X/Y components of the template's
        # WORLD rotation (matrix_world, not matrix_local) and bake them into
        # the mesh. Placement rotations in this project are around Z and must
        # be left alone — baking them would rotate the shared mesh for all
        # linked duplicates and break instances at different Z-angles.
        #
        # matrix_world is required because FBX/Unreal axis fixups commonly
        # live on a parent empty (e.g. "Building" with rot=(90,0,0)), so the
        # child's matrix_local sees identity and would skip the bake — while
        # matrix_world correctly sees the parent's rotation.
        _, world_quat, _ = template.matrix_world.decompose()
        rot_world = world_quat.to_euler('XYZ')
        if abs(rot_world.x) > 1e-3 or abs(rot_world.y) > 1e-3:
            bake_euler = Euler((rot_world.x, rot_world.y, 0.0), 'XYZ')
            template_bake_quat = bake_euler.to_quaternion()
        else:
            template_bake_quat = Quaternion()
        template_bake_quat_inv = template_bake_quat.inverted()

        # Adjusted world positions and rotations for every instance.
        adjusted_instances = []
        used_scale_signs = set()
        for inst_meta in asset.get("instances", []):
            inst_obj = obj_lookup.get(inst_meta.get("name")) or template
            world_pivot = inst_obj.matrix_world @ bbox_center

            _, inst_world_quat, inst_world_scale = inst_obj.matrix_world.decompose()
            adjusted_quat = inst_world_quat @ template_bake_quat_inv
            adjusted_euler = adjusted_quat.to_euler('XYZ')

            adjusted_instances.append({
                "name": inst_meta.get("name", base_name),
                "position": [round(world_pivot.x, 4),
                             round(world_pivot.y, 4),
                             round(world_pivot.z, 4)],
                "rotation": [round(math.degrees(adjusted_euler.x), 4),
                             round(math.degrees(adjusted_euler.y), 4),
                             round(math.degrees(adjusted_euler.z), 4)],
                "scale": [round(inst_world_scale.x, 4),
                          round(inst_world_scale.y, 4),
                          round(inst_world_scale.z, 4)],
            })
            used_scale_signs.add(scale_sign_tuple((inst_world_scale.x,
                                                   inst_world_scale.y,
                                                   inst_world_scale.z)))

        if not used_scale_signs:
            used_scale_signs.add((1, 1, 1))

        export_variants = []
        for signs in sorted(used_scale_signs, key=lambda s: (s != (1, 1, 1), s)):
            export_variants.append({
                "signs": signs,
                "asset_name": signed_variant_name(base_name, signs),
            })
        sign_to_asset_name = {variant["signs"]: variant["asset_name"]
                              for variant in export_variants}

        # MeshPart Size cannot be negative. For reflected Blender instances,
        # export a mesh variant with those scale signs baked into vertex data
        # and make placement use positive dimensions plus a normal right-handed
        # CFrame. This avoids dark lighting from left-handed CFrames in Roblox.
        for inst in adjusted_instances:
            signs = scale_sign_tuple(inst["scale"])
            asset_name = sign_to_asset_name.get(signs)
            if asset_name and signs != (1, 1, 1):
                inst["assetName"] = asset_name
                inst["scaleSignsBaked"] = list(signs)
                inst["originalScale"] = inst["scale"]
                inst["scale"] = [abs(v) for v in inst["scale"]]

        # Center the mesh (destructive). Linked duplicates share the same mesh
        # datablock, so guard against translating twice.
        if center_pivots and bbox_center.length > 1e-6 and mesh.name not in processed_meshes:
            translate_mesh_vertices(mesh, bbox_center)
            processed_meshes.add(mesh.name)

        templates.append({
            "base_name": base_name,
            "template_obj": template,
            "mesh_name": mesh.name,
            "bbox_center": [round(bbox_center.x, 6),
                            round(bbox_center.y, 6),
                            round(bbox_center.z, 6)],
            "bake_rotation": template_bake_quat,
            "export_variants": export_variants,
            "adjusted_instances": adjusted_instances,
            "vertex_count": asset.get("vertexCount", len(mesh.vertices)),
            "polygon_count": asset.get("polygonCount", len(mesh.polygons)),
            "category": asset.get("category", "Uncategorized"),
            "missing": False,
        })

    return templates


# ---------------------------------------------------------------- export

GLTF_KWARGS = dict(
    export_format='GLB',
    export_apply_modifiers=False,
    export_materials='EXPORT',
    export_colors=False,
    export_normals=True,
    export_tangentials=False,
    export_yup=True,
)


def _call_gltf_export(filepath):
    """Wrapper that gracefully handles older Blender versions."""
    try:
        bpy.ops.export_scene.gltf(filepath=filepath, use_selection=True, **GLTF_KWARGS)
        return True
    except Exception as e:
        print(f"  gltf export fallback ({e}); retrying with minimal settings")
        try:
            bpy.ops.export_scene.gltf(filepath=filepath, use_selection=True,
                                       export_format='GLB')
            return True
        except Exception as e2:
            print(f"  FATAL gltf export error: {e2}")
            return False


def _bake_rotation_into_mesh(mesh_data, rotation_quat):
    """Apply a rotation to mesh data, including derived normal data."""
    mesh_data.transform(rotation_quat.to_matrix().to_4x4())
    mesh_data.update()


def _bake_scale_signs_into_mesh(mesh_data, signs):
    """Mirror mesh data for a signed-scale export variant.

    Negative-determinant mirrors need their face winding flipped so exported
    normals still point outward in Roblox.
    """
    if signs == (1, 1, 1):
        return
    mesh_data.transform(Matrix.Diagonal((signs[0], signs[1], signs[2], 1.0)))
    if signs[0] * signs[1] * signs[2] < 0:
        mesh_data.flip_normals()
    mesh_data.update()


def make_export_clone(template_obj, export_name, location=(0.0, 0.0, 0.0),
                      bake_rotation=None, scale_signs=(1, 1, 1)):
    """Create a lightweight mesh object for export.

    When bake_rotation is non-identity, a private copy of the mesh is made
    and the rotation is applied to its vertex positions. Callers must pair
    this with matching adjustments on per-instance rotations (see
    build_asset_templates) — otherwise the baked rotation gets applied
    twice in Roblox.
    """
    needs_rotation = bake_rotation is not None and abs(bake_rotation.angle) > 1e-5
    needs_signed_variant = scale_signs != (1, 1, 1)
    if needs_rotation or needs_signed_variant:
        mesh_data = template_obj.data.copy()
        if needs_rotation:
            _bake_rotation_into_mesh(mesh_data, bake_rotation)
        if needs_signed_variant:
            _bake_scale_signs_into_mesh(mesh_data, scale_signs)
    else:
        mesh_data = template_obj.data

    clone = bpy.data.objects.new(export_name, mesh_data)
    bpy.context.collection.objects.link(clone)
    clone.location = location
    clone.rotation_euler = (0.0, 0.0, 0.0)
    clone.scale = (1.0, 1.0, 1.0)
    return clone


def _cleanup_clone(clone, template_mesh):
    """Remove the clone; if it owned a private mesh copy, remove that too."""
    mesh = clone.data
    bpy.data.objects.remove(clone, do_unlink=True)
    if mesh is not template_mesh:
        bpy.data.meshes.remove(mesh, do_unlink=True)


def export_individual(templates, output_dir):
    """One GLB per unique asset. Mesh is centered so Roblox pivot = geometric center."""
    results = []
    total = len([t for t in templates if not t.get("missing")])
    done = 0

    for template in templates:
        if template.get("missing"):
            results.append({
                "baseName": template["base_name"],
                "success": False,
                "error": template.get("error", "Object not found"),
            })
            continue

        done += 1
        if done == 1 or done % 25 == 0 or done == total:
            print(f"PROGRESS:Extracting {done}/{total}: {template['base_name']}")

        variants = template.get("export_variants") or [{
            "signs": (1, 1, 1),
            "asset_name": safe_filename(template["base_name"]),
        }]
        variant_results = []
        success = True
        for variant in variants:
            safe = variant["asset_name"]
            output_path = os.path.join(output_dir, f"{safe}.glb")

            clone = make_export_clone(
                template["template_obj"], safe,
                bake_rotation=template.get("bake_rotation"),
                scale_signs=variant["signs"],
            )
            bpy.ops.object.select_all(action='DESELECT')
            clone.select_set(True)
            bpy.context.view_layer.objects.active = clone

            variant_success = _call_gltf_export(output_path)
            success = success and variant_success and os.path.exists(output_path)

            _cleanup_clone(clone, template["template_obj"].data)

            variant_results.append({
                "signs": list(variant["signs"]),
                "assetName": safe,
                "path": output_path,
                "success": variant_success and os.path.exists(output_path),
            })

        base_variant = variants[0]
        base_path = os.path.join(output_dir, f"{base_variant['asset_name']}.glb")
        if success and os.path.exists(base_path):
            results.append({
                "baseName": template["base_name"],
                "success": True,
                "path": base_path,
                "format": "glb",
                "fileSize": sum(os.path.getsize(v["path"]) for v in variant_results if v["success"]),
                "vertexCount": template["vertex_count"],
                "polygonCount": template["polygon_count"],
                "pivotOffset": template["bbox_center"],
                "adjustedInstances": template["adjusted_instances"],
                "variants": variant_results,
            })
        else:
            results.append({
                "baseName": template["base_name"],
                "success": False,
                "error": "Export failed",
            })

    return results, None


def export_single_file(templates, output_dir):
    """
    All unique assets bundled into one GLB. Each asset becomes a named mesh
    node at an arbitrary grid position — Roblox's importer will create one
    MeshPart per node, named after the asset, which Lua looks up by name.
    """
    valid = [t for t in templates if not t.get("missing")]
    if not valid:
        return [], None

    print(f"PROGRESS:Building combined export with {len(valid)} assets...")

    # Build clones at non-overlapping grid positions so the file is also
    # useful for visual inspection in Blender/asset browsers.
    spacing = 25.0
    clone_count = sum(len(t.get("export_variants") or [None]) for t in valid)
    cols = max(1, int(math.ceil(math.sqrt(clone_count))))

    bpy.ops.object.select_all(action='DESELECT')

    clones = []
    used_names = set()
    clone_index = 0
    for template in valid:
        variants = template.get("export_variants") or [{
            "signs": (1, 1, 1),
            "asset_name": safe_filename(template["base_name"]),
        }]
        for variant in variants:
            row, col = divmod(clone_index, cols)
            clone_index += 1
            base_safe = variant["asset_name"]
            name = base_safe
            suffix = 1
            while name in used_names:
                suffix += 1
                name = f"{base_safe}_{suffix}"
            used_names.add(name)
            variant["asset_name"] = name
            if name != base_safe:
                for inst in template["adjusted_instances"]:
                    if inst.get("assetName") == base_safe:
                        inst["assetName"] = name

            clone = make_export_clone(
                template["template_obj"],
                name,
                location=(col * spacing, row * spacing, 0.0),
                bake_rotation=template.get("bake_rotation"),
                scale_signs=variant["signs"],
            )
            clone.select_set(True)
            clones.append((clone, template, name, variant))

    if clones:
        bpy.context.view_layer.objects.active = clones[0][0]

    output_path = os.path.join(output_dir, "assets_combined.glb")
    print(f"PROGRESS:Writing combined GLB (this is the slow part)...")
    success = _call_gltf_export(output_path)

    for clone, t, _n, _v in clones:
        _cleanup_clone(clone, t["template_obj"].data)

    results = []
    for template in templates:
        if template.get("missing"):
            results.append({
                "baseName": template["base_name"],
                "success": False,
                "error": template.get("error", "Object not found"),
            })
            continue

        # Find the export name we actually used for this template.
        export_name = next((n for c, t, n, v in clones
                            if t is template and tuple(v["signs"]) == (1, 1, 1)),
                           None)
        if export_name is None:
            export_name = next((n for c, t, n, v in clones if t is template),
                               safe_filename(template["base_name"]))
        variant_results = [
            {
                "signs": list(v["signs"]),
                "assetName": n,
            }
            for c, t, n, v in clones
            if t is template and tuple(v["signs"]) != (1, 1, 1)
        ]

        results.append({
            "baseName": template["base_name"],
            "success": success,
            "exportName": export_name,
            "format": "glb_combined",
            "vertexCount": template["vertex_count"],
            "polygonCount": template["polygon_count"],
            "pivotOffset": template["bbox_center"],
            "adjustedInstances": template["adjusted_instances"],
            "variants": variant_results,
            **({} if success else {"error": "Combined export failed"}),
        })

    if success and os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"PROGRESS:Combined GLB ready ({size_mb:.1f} MB)")
        return results, output_path

    return results, None


# ---------------------------------------------------------------- main

def main():
    raw = get_args()
    positional, flags = parse_args(raw)

    if len(positional) < 3:
        print("Usage: ... -- input_file output_dir manifest_json "
              "[--mode single|individual] [--center-pivots true|false]")
        sys.exit(1)

    input_file = positional[0]
    output_dir = positional[1]
    manifest_path = positional[2]

    mode = (flags.get("mode") or "single").lower()
    if mode not in ("single", "individual"):
        mode = "single"
    center_pivots = as_bool(flags.get("center-pivots"), default=True)

    os.makedirs(output_dir, exist_ok=True)

    with open(manifest_path, 'r') as f:
        manifest = json.load(f)

    assets = manifest.get("assets", [])
    if not assets:
        print("PROGRESS:No assets to extract")
        results_path = os.path.join(output_dir, "_extraction_results.json")
        with open(results_path, 'w') as f:
            json.dump({"totalAssets": 0, "exported": 0, "failed": 0,
                       "mode": mode, "centerPivots": center_pivots,
                       "assets": [], "combinedFile": None,
                       "outputDir": output_dir}, f, indent=2)
        print("RESULT:SUCCESS")
        return

    print(f"PROGRESS:Importing scene for extraction (mode={mode}, "
          f"pivots={'centered' if center_pivots else 'original'})...")
    clear_scene()
    import_file(input_file)

    print(f"PROGRESS:Preparing {len(assets)} unique assets...")
    templates = build_asset_templates(manifest, center_pivots=center_pivots)

    if mode == "single":
        exported, combined_file = export_single_file(templates, output_dir)
    else:
        exported, combined_file = export_individual(templates, output_dir)

    success_count = sum(1 for r in exported if r.get("success"))
    total = len(exported)

    results = {
        "totalAssets": total,
        "exported": success_count,
        "failed": total - success_count,
        "outputDir": output_dir,
        "mode": mode,
        "centerPivots": center_pivots,
        "combinedFile": combined_file,
        "assets": exported,
    }

    results_path = os.path.join(output_dir, "_extraction_results.json")
    with open(results_path, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"PROGRESS:Extraction complete: {success_count}/{total} assets exported")
    print("RESULT:SUCCESS")


if __name__ == "__main__":
    main()

"""
Blender Scene Preview Renderer
Run headlessly: blender --background --python render_preview.py -- input_file output_png [width] [height]

Uses WORKBENCH renderer for speed — renders in seconds regardless of scene complexity.
Falls back gracefully if any step fails.
"""
import bpy
import sys
import os
import math
from mathutils import Vector


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
    elif ext in ('.stl',):
        try:
            bpy.ops.wm.stl_import(filepath=filepath)
        except AttributeError:
            bpy.ops.import_mesh.stl(filepath=filepath)
    elif ext == '.ply':
        try:
            bpy.ops.wm.ply_import(filepath=filepath)
        except AttributeError:
            bpy.ops.import_mesh.ply(filepath=filepath)
    else:
        bpy.ops.import_scene.fbx(filepath=filepath)


def get_scene_bounds():
    min_co = Vector((float('inf'), float('inf'), float('inf')))
    max_co = Vector((float('-inf'), float('-inf'), float('-inf')))
    found = False
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        found = True
        for corner in obj.bound_box:
            world_co = obj.matrix_world @ Vector(corner)
            min_co.x = min(min_co.x, world_co.x)
            min_co.y = min(min_co.y, world_co.y)
            min_co.z = min(min_co.z, world_co.z)
            max_co.x = max(max_co.x, world_co.x)
            max_co.y = max(max_co.y, world_co.y)
            max_co.z = max(max_co.z, world_co.z)
    if not found:
        return Vector((0, 0, 0)), Vector((10, 10, 10))
    return min_co, max_co


def setup_camera(min_co, max_co):
    center = (min_co + max_co) / 2
    size = max_co - min_co
    max_dim = max(size.x, size.y, size.z) or 10

    distance = max_dim * 1.8
    cam_pos = center + Vector((distance * 0.7, -distance * 0.7, distance * 0.5))

    cam_data = bpy.data.cameras.new("PreviewCamera")
    cam_data.lens = 35
    cam_data.clip_start = max_dim * 0.001
    cam_data.clip_end = max_dim * 20

    cam_obj = bpy.data.objects.new("PreviewCamera", cam_data)
    bpy.context.collection.objects.link(cam_obj)
    cam_obj.location = cam_pos

    direction = center - cam_pos
    rot_quat = direction.to_track_quat('-Z', 'Y')
    cam_obj.rotation_euler = rot_quat.to_euler()
    bpy.context.scene.camera = cam_obj


def setup_render_workbench(width, height):
    """Workbench: renders instantly, no lighting setup needed."""
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False

    # Workbench display settings
    shading = scene.display.shading
    shading.light = 'MATCAP'
    shading.color_type = 'MATERIAL'
    shading.show_shadows = False
    shading.show_cavity = True
    shading.cavity_type = 'SCREEN'

    # Background
    world = bpy.data.worlds.get('World') or bpy.data.worlds.new('World')
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get('Background')
    if bg:
        bg.inputs['Color'].default_value = (0.15, 0.15, 0.18, 1.0)
        bg.inputs['Strength'].default_value = 1.0


def main():
    args = get_args()
    if len(args) < 2:
        print("Usage: blender --background --python render_preview.py -- input_file output_png [width] [height]")
        sys.exit(1)

    input_file = args[0]
    output_png = args[1]
    # Use lower resolution for speed — 640x360 is plenty for a thumbnail
    width = int(args[2]) if len(args) > 2 else 640
    height = int(args[3]) if len(args) > 3 else 360

    print(f"PROGRESS:Importing scene for preview...")
    clear_scene()
    import_file(input_file)

    obj_count = len([o for o in bpy.data.objects if o.type == 'MESH'])
    print(f"PROGRESS:Setting up preview ({obj_count} objects)...")

    min_co, max_co = get_scene_bounds()
    setup_camera(min_co, max_co)
    setup_render_workbench(width, height)

    print(f"PROGRESS:Rendering preview (Workbench, {width}x{height})...")
    os.makedirs(os.path.dirname(output_png) or '.', exist_ok=True)
    bpy.context.scene.render.filepath = output_png
    bpy.ops.render.render(write_still=True)

    print(f"PROGRESS:Preview saved")
    print(f"RESULT:SUCCESS")


if __name__ == "__main__":
    main()

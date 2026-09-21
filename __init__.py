bl_info = {
	"name": "CAD Drafting Tools + OpenSCAD",
	"author": "Grok / xAI Community Contribution",
	"version": (1, 0, 0),
	"blender": (4, 2, 0),
	"location": "View3D > Sidebar > CAD",
	"description": "CAD-style 2D drafting, dimensioning tools and OpenSCAD import/export",
	"warning": "Dimension objects are approximate; full constraint solving is limited",
	"category": "3D View",
}

import bpy
import bmesh
import math
import os
import tempfile
import subprocess
import re
from mathutils import Vector, Matrix, Euler
from bpy.props import (
	StringProperty, FloatProperty, IntProperty, BoolProperty,
	EnumProperty, PointerProperty, FloatVectorProperty
)
from bpy.types import (
	Operator, Panel, PropertyGroup, AddonPreferences, WorkSpaceTool
)
from bpy_extras.view3d_utils import (
	region_2d_to_location_3d, region_2d_to_vector_3d
)
from bpy_extras import object_utils

# ----------------------------------------------------------------------
# Preferences
# ----------------------------------------------------------------------
class CADPreferences(AddonPreferences):
	bl_idname = __name__

	openscad_path: StringProperty(
		name="OpenSCAD Executable",
		subtype='FILE_PATH',
		default="",
		description="Full path to openscad executable"
	)

	default_precision: IntProperty(name="Default Precision", default=2, min=0, max=6)
	default_text_size: FloatProperty(name="Default Text Size", default=0.05, min=0.001)
	arrow_size: FloatProperty(name="Arrow Size", default=0.03, min=0.001)

	def draw(self, context):
		layout = self.layout
		layout.prop(self, "openscad_path")
		layout.prop(self, "default_precision")
		layout.prop(self, "default_text_size")
		layout.prop(self, "arrow_size")

# ----------------------------------------------------------------------
# Scene Properties
# ----------------------------------------------------------------------
class CADSceneProps(PropertyGroup):
	unit_system: EnumProperty(
		name="Units",
		items=[
			('METRIC', "Metric", ""),
			('IMPERIAL', "Imperial", ""),
		],
		default='METRIC'
	)
	precision: IntProperty(name="Precision", default=2, min=0, max=6)
	text_size: FloatProperty(name="Text Size", default=0.05, min=0.001)
	show_arrows: BoolProperty(name="Show Arrows", default=True)
	dim_color: FloatVectorProperty(
		name="Dimension Color", subtype='COLOR',
		default=(0.1, 0.1, 0.1), min=0, max=1, size=3
	)

# ----------------------------------------------------------------------
# Utility Functions
# ----------------------------------------------------------------------
def get_prefs():
	return bpy.context.preferences.addons[__name__].preferences

def format_length(value, precision=2, unit='METRIC'):
	if unit == 'IMPERIAL':
		inches = value * 39.3701
		feet = int(inches // 12)
		inch_rem = inches % 12
		if feet > 0:
			return f"{feet}'-{inch_rem:.{precision}f}\""
		return f"{inch_rem:.{precision}f}\""
	return f"{value:.{precision}f}"

def create_dimension_objects(context, start, end, dim_type='LINEAR', offset=0.1, value=None):
	"""Create a dimension as a curve + text object"""
	scene = context.scene
	props = scene.cad_props
	prefs = get_prefs()

	mid = (start + end) * 0.5
	direction = (end - start).normalized()
	length = (end - start).length
	if value is None:
		value = length

	# Offset direction (perpendicular in XY for simplicity)
	perp = Vector((-direction.y, direction.x, 0)).normalized()
	if perp.length < 0.001:
		perp = Vector((0, 0, 1))

	offset_vec = perp * offset
	p1 = start + offset_vec
	p2 = end + offset_vec
	text_loc = mid + offset_vec

	# Create curve for dimension line + arrows
	curve_data = bpy.data.curves.new(name="DimCurve", type='CURVE')
	curve_data.dimensions = '3D'
	curve_data.resolution_u = 2
	spline = curve_data.splines.new('POLY')
	spline.points.add(3)

	# Simple line with small arrow stubs
	arrow = prefs.arrow_size
	spline.points[0].co = (*start, 1)
	spline.points[1].co = (*p1, 1)
	spline.points[2].co = (*p2, 1)
	spline.points[3].co = (*end, 1)

	curve_obj = bpy.data.objects.new("Dimension", curve_data)
	context.collection.objects.link(curve_obj)
	curve_obj.color = (*props.dim_color, 1)

	# Text object
	font_curve = bpy.data.curves.new(name="DimText", type='FONT')
	font_curve.body = format_length(value, props.precision, props.unit_system)
	font_curve.size = props.text_size
	font_curve.align_x = 'CENTER'
	font_curve.align_y = 'CENTER'

	text_obj = bpy.data.objects.new("DimLabel", font_curve)
	context.collection.objects.link(text_obj)
	text_obj.location = text_loc
	text_obj.rotation_euler = (0, 0, math.atan2(direction.y, direction.x))

	# Parent text to curve for convenience
	text_obj.parent = curve_obj

	# Store metadata
	curve_obj["cad_dim_type"] = dim_type
	curve_obj["cad_start"] = start[:]
	curve_obj["cad_end"] = end[:]
	curve_obj["cad_value"] = value

	return curve_obj, text_obj

def get_snap_point(context, event, objects=None):
	"""Basic snapping to vertices / midpoints"""
	region = context.region
	rv3d = context.region_data
	coord = event.mouse_region_x, event.mouse_region_y

	# Raycast
	view_vector = region_2d_to_vector_3d(region, rv3d, coord)
	ray_origin = region_2d_to_location_3d(region, rv3d, coord, view_vector)

	# Simple closest vertex search (improve with BVH in production)
	closest = None
	min_dist = 0.05  # threshold in 3D units approx

	for obj in (objects or context.visible_objects):
		if obj.type != 'MESH':
			continue
		mw = obj.matrix_world
		for v in obj.data.vertices:
			world_co = mw @ v.co
			# Project to 2D for screen distance would be better
			dist = (world_co - ray_origin).length
			if dist < min_dist:
				min_dist = dist
				closest = world_co

	if closest:
		return closest
	return region_2d_to_location_3d(region, rv3d, coord, view_vector)

# ----------------------------------------------------------------------
# Dimension Operators
# ----------------------------------------------------------------------
class CAD_OT_add_linear_dimension(Operator):
	bl_idname = "cad.add_linear_dimension"
	bl_label = "Add Linear Dimension"
	bl_options = {'REGISTER', 'UNDO'}

	def modal(self, context, event):
		context.area.tag_redraw()

		if event.type == 'MOUSEMOVE':
			self.current = get_snap_point(context, event)
			return {'RUNNING_MODAL'}

		elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
			if self.stage == 0:
				self.start = self.current.copy()
				self.stage = 1
			elif self.stage == 1:
				self.end = self.current.copy()
				create_dimension_objects(context, self.start, self.end)
				return {'FINISHED'}

		elif event.type in {'RIGHTMOUSE', 'ESC'}:
			return {'CANCELLED'}

		return {'RUNNING_MODAL'}

	def invoke(self, context, event):
		self.stage = 0
		self.start = Vector()
		self.end = Vector()
		self.current = Vector()
		context.window_manager.modal_handler_add(self)
		self.report({'INFO'}, "Click first point, then second point")
		return {'RUNNING_MODAL'}

class CAD_OT_add_angular_dimension(Operator):
	bl_idname = "cad.add_angular_dimension"
	bl_label = "Add Angular Dimension"
	bl_options = {'REGISTER', 'UNDO'}

	def execute(self, context):
		# Simplified: requires 3 selected vertices
		obj = context.active_object
		if not obj or obj.type != 'MESH' or obj.mode != 'EDIT':
			self.report({'ERROR'}, "Select 3 vertices in Edit Mode")
			return {'CANCELLED'}

		bm = bmesh.from_edit_mesh(obj.data)
		verts = [v for v in bm.verts if v.select]
		if len(verts) != 3:
			self.report({'ERROR'}, "Select exactly 3 vertices")
			return {'CANCELLED'}

		# Center is middle vertex
		v0, v1, v2 = [obj.matrix_world @ v.co for v in verts]
		# Approximate angle dimension placement
		create_dimension_objects(context, v0, v2, dim_type='ANGULAR', offset=0.15)
		return {'FINISHED'}

# ----------------------------------------------------------------------
# Drafting Tools
# ----------------------------------------------------------------------
class CAD_OT_draw_line(Operator):
	bl_idname = "cad.draw_line"
	bl_label = "Draw CAD Line"
	bl_options = {'REGISTER', 'UNDO'}

	def modal(self, context, event):
		if event.type == 'MOUSEMOVE':
			self.current = get_snap_point(context, event)
			# Force horizontal/vertical with Shift
			if event.shift and self.start:
				delta = self.current - self.start
				if abs(delta.x) > abs(delta.y):
					self.current.y = self.start.y
				else:
					self.current.x = self.start.x
			return {'RUNNING_MODAL'}

		elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
			if self.stage == 0:
				self.start = self.current.copy()
				self.stage = 1
			else:
				# Create edge
				mesh = bpy.data.meshes.new("CAD_Line")
				bm = bmesh.new()
				v1 = bm.verts.new(self.start)
				v2 = bm.verts.new(self.current)
				bm.edges.new([v1, v2])
				bm.to_mesh(mesh)
				bm.free()
				obj = bpy.data.objects.new("CAD_Line", mesh)
				context.collection.objects.link(obj)
				return {'FINISHED'}

		elif event.type in {'RIGHTMOUSE', 'ESC'}:
			return {'CANCELLED'}

		return {'RUNNING_MODAL'}

	def invoke(self, context, event):
		self.stage = 0
		self.start = None
		self.current = Vector()
		context.window_manager.modal_handler_add(self)
		return {'RUNNING_MODAL'}

# ----------------------------------------------------------------------
# OpenSCAD Conversion
# ----------------------------------------------------------------------
class CAD_OT_import_openscad(Operator):
	bl_idname = "cad.import_openscad"
	bl_label = "Import OpenSCAD"
	bl_options = {'REGISTER', 'UNDO'}

	filepath: StringProperty(subtype='FILE_PATH')
	filter_glob: StringProperty(default="*.scad", options={'HIDDEN'})

	def execute(self, context):
		prefs = get_prefs()
		scad_path = prefs.openscad_path or "openscad"

		if not os.path.exists(self.filepath):
			self.report({'ERROR'}, "File not found")
			return {'CANCELLED'}

		# Prefer CLI render to STL
		with tempfile.TemporaryDirectory() as tmpdir:
			stl_path = os.path.join(tmpdir, "export.stl")
			try:
				cmd = [scad_path, "-o", stl_path, self.filepath]
				result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
				if result.returncode != 0 or not os.path.exists(stl_path):
					self.report({'WARNING'}, "OpenSCAD CLI failed, trying basic parser")
					return self._basic_parse(context)

				# Import STL
				bpy.ops.wm.stl_import(filepath=stl_path)
				self.report({'INFO'}, "Imported via OpenSCAD → STL")
				return {'FINISHED'}
			except Exception as e:
				self.report({'WARNING'}, f"CLI error: {e}. Using basic parser.")
				return self._basic_parse(context)

	def _basic_parse(self, context):
		"""Very limited fallback parser for simple primitives"""
		with open(self.filepath, 'r') as f:
			code = f.read()

		# Extremely simplified – only handles single cube/sphere/cylinder for demo
		if "cube(" in code:
			match = re.search(r'cube\s*\(\s*\[?\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)', code)
			if match:
				size = [float(x) for x in match.groups()]
				bpy.ops.mesh.primitive_cube_add(size=1)
				obj = context.active_object
				obj.scale = size
				bpy.ops.object.transform_apply(scale=True)
				self.report({'INFO'}, "Basic cube imported")
				return {'FINISHED'}

		self.report({'ERROR'}, "Could not parse. Install OpenSCAD and set path.")
		return {'CANCELLED'}

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

class CAD_OT_export_openscad(Operator):
	bl_idname = "cad.export_openscad"
	bl_label = "Export to OpenSCAD"
	bl_options = {'REGISTER'}

	filepath: StringProperty(subtype='FILE_PATH')

	def execute(self, context):
		objects = context.selected_objects
		if not objects:
			self.report({'ERROR'}, "No objects selected")
			return {'CANCELLED'}

		lines = ["// Generated by CAD Drafting Tools\n"]
		for obj in objects:
			if obj.type != 'MESH':
				continue
			mesh = obj.data
			mw = obj.matrix_world

			# Export as polyhedron (simple version)
			verts = [mw @ v.co for v in mesh.vertices]
			faces = [p.vertices[:] for p in mesh.polygons]

			lines.append(f"// Object: {obj.name}")
			lines.append("polyhedron(")
			lines.append("  points=[")
			for v in verts:
				lines.append(f"    [{v.x:.6f}, {v.y:.6f}, {v.z:.6f}],")
			lines.append("  ],")
			lines.append("  faces=[")
			for f in faces:
				lines.append(f"    {list(f)},")
			lines.append("  ]")
			lines.append(");\n")

		with open(self.filepath, 'w') as f:
			f.write("\n".join(lines))

		self.report({'INFO'}, f"Exported to {self.filepath}")
		return {'FINISHED'}

	def invoke(self, context, event):
		self.filepath = "export.scad"
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

# ----------------------------------------------------------------------
# UI Panels
# ----------------------------------------------------------------------
class CAD_PT_main_panel(Panel):
	bl_label = "CAD Drafting"
	bl_idname = "CAD_PT_main"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = "CAD"

	def draw(self, context):
		layout = self.layout
		props = context.scene.cad_props

		col = layout.column(align=True)
		col.label(text="Dimensions")
		col.operator("cad.add_linear_dimension", icon='DRIVER_DISTANCE')
		col.operator("cad.add_angular_dimension", icon='DRIVER_ROTATIONAL_DIFFERENCE')

		col.separator()
		col.label(text="Drafting")
		col.operator("cad.draw_line", icon='MESH_DATA')

		col.separator()
		col.label(text="Settings")
		col.prop(props, "unit_system")
		col.prop(props, "precision")
		col.prop(props, "text_size")
		col.prop(props, "show_arrows")
		col.prop(props, "dim_color")

class CAD_PT_openscad_panel(Panel):
	bl_label = "OpenSCAD"
	bl_idname = "CAD_PT_openscad"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = "CAD"
	bl_parent_id = "CAD_PT_main"

	def draw(self, context):
		layout = self.layout
		col = layout.column(align=True)
		col.operator("cad.import_openscad", icon='IMPORT')
		col.operator("cad.export_openscad", icon='EXPORT')
		col.label(text="Set OpenSCAD path in Preferences", icon='INFO')

# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------
classes = (
	CADPreferences,
	CADSceneProps,
	CAD_OT_add_linear_dimension,
	CAD_OT_add_angular_dimension,
	CAD_OT_draw_line,
	CAD_OT_import_openscad,
	CAD_OT_export_openscad,
	CAD_PT_main_panel,
	CAD_PT_openscad_panel,
)

def register():
	for cls in classes:
		bpy.utils.register_class(cls)
	bpy.types.Scene.cad_props = PointerProperty(type=CADSceneProps)

def unregister():
	for cls in reversed(classes):
		bpy.utils.unregister_class(cls)
	del bpy.types.Scene.cad_props

if __name__ == "__main__":
	register()
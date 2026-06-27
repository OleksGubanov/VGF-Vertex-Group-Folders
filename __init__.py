# SPDX-License-Identifier: GPL-3.0-or-later
bl_info = {
    "name": "VGF: Vertex Group Folders",
    "author": "Oleksandr Gubanov (Zingless)",
    "version": (1, 0, 2),
    "blender": (4, 2, 0),
    "location": "Properties > Object Data > Vertex Groups",
    "description": "Organize lists of Vertex Groups into folders",
    "category": "Object",
}

import bpy
import uuid

# =========================================================
# 1. CONSTANTS & UI STRINGS
# =========================================================
ROOT_UID = "ROOT"
ITEM_FOLDER = "FOLDER"
ITEM_GROUP = "GROUP"

# Localization and UI Strings
UI_STR_FOLDER = "Folder"
UI_STR_GROUP = "Vertex Group"
UI_STR_ROOT = "Root"
UI_STR_ITEM = "Item"
UI_STR_PENDING = "[Pending Sync]"
UI_STR_SYNC_SUCCESS = "Vertex Groups Synced"
UI_STR_COPY_SUCCESS = "Vertex groups copied successfully"
UI_STR_WARN_NO_MESH = "No other mesh objects selected!"
UI_STR_WARN_MISMATCH = "Vertex count mismatch with {name}! Check topology."

# Tooltips and Descriptions
DESC_ADD_FOLDER = "Create a new folder to organize vertex groups"
DESC_ADD_GROUP = "Add a new vertex group to the object"
DESC_REMOVE_ITEM = "Remove the selected folder or vertex group"
DESC_MOVE_UP = "Move the selected item up within its folder"
DESC_MOVE_DOWN = "Move the selected item down within its folder"
DESC_TOGGLE_FOLDER = "Expand or collapse folder contents"
DESC_SYNC = "Synchronize structure with native vertex groups"
DESC_COPY_SEL = "Copy vertex groups to other selected mesh objects"
DESC_MOVE_TO_FOLDER = "Move to folder"
DESC_ACTIVE_ITEM = "Active folder or vertex group"

def new_uid():
    """Generate a unique identifier for Vertex Group Folder nodes."""
    return uuid.uuid4().hex

# =========================================================
# 2. NATIVE ADAPTER (Blender API Facade)
# =========================================================
class NativeAdapter:
    """Handles native Blender API calls."""
    
    @staticmethod
    def get_groups(obj):
        return obj.vertex_groups

    @staticmethod
    def add_group(obj):
        obj.vertex_groups.new(name="Group")

    @staticmethod
    def remove_active_group(obj):
        if obj.vertex_groups.active:
            obj.vertex_groups.remove(obj.vertex_groups.active)

    @staticmethod
    def remove_all_groups(obj):
        obj.vertex_groups.clear()

    @staticmethod
    def remove_unlocked_groups(obj):
        for vg in reversed(obj.vertex_groups):
            if not vg.lock_weight:
                obj.vertex_groups.remove(vg)

    @staticmethod
    def duplicate_group():
        bpy.ops.object.vertex_group_copy()

    @staticmethod
    def mirror_group(use_topology=False):
        bpy.ops.object.vertex_group_mirror(
            mirror_weights=True, 
            flip_group_names=True, 
            use_topology=use_topology
        )

    @staticmethod
    def sort_groups_name():
        bpy.ops.object.vertex_group_sort(sort_type='NAME')

    @staticmethod
    def sort_groups_bone():
        bpy.ops.object.vertex_group_sort(sort_type='BONE_HIERARCHY')

    @staticmethod
    def lock_all(obj, action='LOCK'):
        for vg in obj.vertex_groups:
            if action == 'LOCK':
                vg.lock_weight = True
            elif action == 'UNLOCK':
                vg.lock_weight = False
            elif action == 'INVERT':
                vg.lock_weight = not vg.lock_weight

    @staticmethod
    def set_active_index(obj, idx):
        if 0 <= idx < len(obj.vertex_groups):
            if obj.vertex_groups.active_index != idx:
                obj.vertex_groups.active_index = idx

    @staticmethod
    def rename_group(obj, old_name, new_name):
        vg = obj.vertex_groups.get(old_name)
        if vg:
            vg.name = new_name
            return vg.name
        return old_name

# =========================================================
# 3. SYNC SERVICE (State Synchronization)
# =========================================================
class SyncService:
    """Manages synchronization between Blender's native groups and custom logic."""

    @staticmethod
    def sync_order_from_native(obj):
        native_vgs = list(NativeAdapter.get_groups(obj))
        native_order = {vg.name: i for i, vg in enumerate(native_vgs)}

        children_map = {}
        for node in obj.zls_vgf_nodes:
            children_map.setdefault(node.parent_uid, []).append(node)

        for parent_uid, children in children_map.items():
            folders = [n for n in children if n.node_type == ITEM_FOLDER]
            groups = [n for n in children if n.node_type == ITEM_GROUP]

            folders.sort(key=lambda x: x.sort_key)
            groups.sort(key=lambda x: native_order.get(x.name, 999999))

            group_indices = [i for i, n in enumerate(children) if n.node_type == ITEM_GROUP]
            for i, group in zip(group_indices, groups):
                children[i] = group 

            for i, node in enumerate(children):
                node.sort_key = i

    @staticmethod
    def reconcile(obj, target_parent_uid=ROOT_UID):
        active_uid = CommandController._get_active_uid(obj)
        native_vgs = list(NativeAdapter.get_groups(obj))
        native_vg_names = {vg.name for vg in native_vgs}
        changed = False

        for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
            node = obj.zls_vgf_nodes[i]
            if node.node_type == ITEM_GROUP and node.name not in native_vg_names:
                obj.zls_vgf_nodes.remove(i)
                changed = True

        our_vg_names = {node.name for node in obj.zls_vgf_nodes if node.node_type == ITEM_GROUP}
        for vg in native_vgs:
            if vg.name not in our_vg_names:
                node = obj.zls_vgf_nodes.add()
                node.uid = new_uid()
                node.node_type = ITEM_GROUP
                node.name = vg.name
                node.parent_uid = target_parent_uid
                node.sort_key = 999999
                changed = True

        native_active_idx = NativeAdapter.get_groups(obj).active_index
        if 0 <= native_active_idx < len(native_vgs):
            native_active_name = native_vgs[native_active_idx].name
            active_node = next((n for n in obj.zls_vgf_nodes if n.node_type == ITEM_GROUP and n.name == native_active_name), None)
            if active_node:
                active_uid = active_node.uid

        if changed:
            SyncService.normalize_sort(obj)

        SyncService.rebuild_ui(obj)

        if active_uid:
            CommandController._restore_selection(obj, active_uid)

    @staticmethod
    def normalize_sort(obj):
        children_map = {}
        for node in obj.zls_vgf_nodes:
            children_map.setdefault(node.parent_uid, []).append(node)
            
        for bucket in children_map.values():
            bucket.sort(key=lambda x: x.sort_key)
            for i, node in enumerate(bucket):
                if node.sort_key != i:
                    node.sort_key = i

    @staticmethod
    def rebuild_ui(obj):
        obj.zls_vgf_ui_rows.clear()
        
        def walk(parent_uid, depth):
            children = [n for n in obj.zls_vgf_nodes if n.parent_uid == parent_uid]
            children.sort(key=lambda x: x.sort_key)
            
            for child in children:
                row = obj.zls_vgf_ui_rows.add()
                row.uid = child.uid
                row.depth = depth
                if child.node_type == ITEM_FOLDER and child.is_expanded:
                    walk(child.uid, depth + 1)
                    
        walk(ROOT_UID, 0)

# =========================================================
# 4. CONTROLLER / COMMANDS
# =========================================================
class CommandController:
    """Handles operational logic for the user interface interactions."""

    @staticmethod
    def _get_active_uid(obj):
        if hasattr(obj, 'zls_vgf_ui_rows') and obj.zls_vgf_ui_rows and 0 <= obj.zls_vgf_active < len(obj.zls_vgf_ui_rows):
            return obj.zls_vgf_ui_rows[obj.zls_vgf_active].uid
        return None

    @staticmethod
    def execute_add_folder(obj):
        target_uid = CommandController._get_active_target_folder(obj)
        node = obj.zls_vgf_nodes.add()
        node.uid = new_uid()
        node.node_type = ITEM_FOLDER
        node.name = UI_STR_FOLDER
        node.parent_uid = target_uid
        node.sort_key = 999999
        
        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        CommandController._restore_selection(obj, node.uid)

    @staticmethod
    def execute_add_group(obj):
        target_uid = CommandController._get_active_target_folder(obj)
        NativeAdapter.add_group(obj)
        SyncService.reconcile(obj, target_parent_uid=target_uid)

    @staticmethod
    def execute_remove_active(obj):
        idx = obj.zls_vgf_active
        if not (0 <= idx < len(obj.zls_vgf_ui_rows)):
            return
        
        uid = obj.zls_vgf_ui_rows[idx].uid
        node = CommandController.get_node(obj, uid)
        if not node:
            return

        fallback_idx = max(0, idx - 1)
        fallback_uid = obj.zls_vgf_ui_rows[fallback_idx].uid if len(obj.zls_vgf_ui_rows) > 1 else None

        if node.node_type == ITEM_GROUP:
            vg_idx = NativeAdapter.get_groups(obj).find(node.name)
            if vg_idx != -1:
                NativeAdapter.set_active_index(obj, vg_idx)
                NativeAdapter.remove_active_group(obj)
        
        elif node.node_type == ITEM_FOLDER:
            uids_to_del = set()
            if node.is_expanded:
                for child in [n for n in obj.zls_vgf_nodes if n.parent_uid == node.uid]:
                    child.parent_uid = node.parent_uid
                    child.sort_key = 999999
                uids_to_del.add(node.uid)
            else:
                def gather(c_uid):
                    uids_to_del.add(c_uid)
                    for child in [n for n in obj.zls_vgf_nodes if n.parent_uid == c_uid]:
                        if child.node_type == ITEM_FOLDER:
                            gather(child.uid)
                        else:
                            uids_to_del.add(child.uid)
                gather(node.uid)
                
                for del_uid in uids_to_del:
                    del_node = CommandController.get_node(obj, del_uid)
                    if del_node and del_node.node_type == ITEM_GROUP:
                        vg = NativeAdapter.get_groups(obj).get(del_node.name)
                        if vg:
                            NativeAdapter.get_groups(obj).remove(vg)
            
            for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
                if obj.zls_vgf_nodes[i].uid in uids_to_del:
                    obj.zls_vgf_nodes.remove(i)

        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        SyncService.reconcile(obj)

        if fallback_uid and CommandController.get_node(obj, fallback_uid):
            CommandController._restore_selection(obj, fallback_uid)
        else:
            obj.zls_vgf_active = max(0, min(obj.zls_vgf_active, len(obj.zls_vgf_ui_rows) - 1))

    @staticmethod
    def execute_rename(obj, uid, new_name):
        node = CommandController.get_node(obj, uid)
        if not node or node.name == new_name:
            return

        active_uid = CommandController._get_active_uid(obj)

        if node.node_type == ITEM_GROUP:
            final_name = NativeAdapter.rename_group(obj, node.name, new_name)
            node.name = final_name
        else:
            node.name = new_name
            
        SyncService.rebuild_ui(obj)
        if active_uid:
            CommandController._restore_selection(obj, active_uid)

    @staticmethod
    def execute_move(obj, direction):
        idx = obj.zls_vgf_active
        if not (0 <= idx < len(obj.zls_vgf_ui_rows)):
            return
        
        uid = obj.zls_vgf_ui_rows[idx].uid
        node = CommandController.get_node(obj, uid)
        if not node:
            return

        siblings = [n for n in obj.zls_vgf_nodes if n.parent_uid == node.parent_uid]
        siblings.sort(key=lambda x: x.sort_key)
        
        try:
            pos = siblings.index(node)
        except ValueError:
            return

        if direction == 'UP' and pos > 0:
            other = siblings[pos - 1]
            node.sort_key, other.sort_key = other.sort_key, node.sort_key
        elif direction == 'DOWN' and pos < len(siblings) - 1:
            other = siblings[pos + 1]
            node.sort_key, other.sort_key = other.sort_key, node.sort_key

        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        CommandController._restore_selection(obj, uid)

    @staticmethod
    def execute_change_parent(obj, uid, new_parent_uid):
        node = CommandController.get_node(obj, uid)
        if not node or node.parent_uid == new_parent_uid:
            return

        if node.node_type == ITEM_FOLDER and new_parent_uid != ROOT_UID:
            curr = new_parent_uid
            while curr != ROOT_UID:
                if curr == node.uid:
                    return 
                p_node = CommandController.get_node(obj, curr)
                if not p_node:
                    break
                curr = p_node.parent_uid

        active_uid = CommandController._get_active_uid(obj)
        node.parent_uid = new_parent_uid
        node.sort_key = 999999
        
        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        
        if active_uid:
            CommandController._restore_selection(obj, active_uid)

    @staticmethod
    def get_node(obj, uid):
        for n in obj.zls_vgf_nodes:
            if n.uid == uid:
                return n
        return None

    @staticmethod
    def _get_active_target_folder(obj):
        if obj.zls_vgf_ui_rows and 0 <= obj.zls_vgf_active < len(obj.zls_vgf_ui_rows):
            act_uid = obj.zls_vgf_ui_rows[obj.zls_vgf_active].uid
            act_node = CommandController.get_node(obj, act_uid)
            if act_node:
                return act_node.uid if act_node.node_type == ITEM_FOLDER else act_node.parent_uid
        return ROOT_UID

    @staticmethod
    def _restore_selection(obj, target_uid):
        for i, row in enumerate(obj.zls_vgf_ui_rows):
            if row.uid == target_uid:
                obj.zls_vgf_active = i
                break

# =========================================================
# 5. MODEL (Data Representation)
# =========================================================
def zls_vgf_ui_name_get(self):
    return self.name

def zls_vgf_ui_name_set(self, value):
    if getattr(self, "_is_updating", False): return
    self._is_updating = True
    CommandController.execute_rename(self.id_data, self.uid, value)
    self._is_updating = False

# Global cache to prevent memory cleanup (Garbage Collection)
_folder_enum_cache = []

def folder_enum_generator(self, context):
    global _folder_enum_cache
    obj = context.object if context else bpy.context.object
    
    items = [(ROOT_UID, UI_STR_ROOT, "", 'OUTLINER_COLLECTION', 0)]
    if obj:
        idx = 1
        for n in obj.zls_vgf_nodes:
            if n.node_type == ITEM_FOLDER:
                items.append((n.uid, n.name, "", 'FILE_FOLDER', idx))
                idx += 1
                
    _folder_enum_cache = items
    return _folder_enum_cache

def folder_dropdown_get(self):
    obj = self.id_data
    if self.parent_uid == ROOT_UID:
        return 0
        
    idx = 1
    for n in obj.zls_vgf_nodes:
        if n.node_type == ITEM_FOLDER:
            if n.uid == self.parent_uid:
                return idx
            idx += 1
    return 0

def folder_dropdown_set(self, value):
    obj = self.id_data
    if value == 0:
        CommandController.execute_change_parent(obj, self.uid, ROOT_UID)
        return
        
    idx = 1
    for n in obj.zls_vgf_nodes:
        if n.node_type == ITEM_FOLDER:
            if idx == value:
                CommandController.execute_change_parent(obj, self.uid, n.uid)
                return
            idx += 1

class ZLSVGF_Node(bpy.types.PropertyGroup):
    uid: bpy.props.StringProperty()
    node_type: bpy.props.EnumProperty(
        items=[
            (ITEM_FOLDER, UI_STR_FOLDER, ""), 
            (ITEM_GROUP, UI_STR_GROUP, "")
        ]
    )
    name: bpy.props.StringProperty()
    parent_uid: bpy.props.StringProperty(default=ROOT_UID)
    sort_key: bpy.props.IntProperty(default=0)
    is_expanded: bpy.props.BoolProperty(default=True)
    
    folder_name: bpy.props.StringProperty(
        get=zls_vgf_ui_name_get, 
        set=zls_vgf_ui_name_set, 
        description=UI_STR_FOLDER
    )
    group_name: bpy.props.StringProperty(
        get=zls_vgf_ui_name_get, 
        set=zls_vgf_ui_name_set, 
        description=UI_STR_GROUP
    )
    ui_parent_dropdown: bpy.props.EnumProperty(
        items=folder_enum_generator, 
        get=folder_dropdown_get, 
        set=folder_dropdown_set, 
        name="", 
        description=DESC_MOVE_TO_FOLDER
    )

class ZLSVGF_UIRow(bpy.types.PropertyGroup):
    uid: bpy.props.StringProperty()
    depth: bpy.props.IntProperty()

def zls_vgf_on_active_update(self, context):
    obj = context.object
    if not obj or not hasattr(obj, 'zls_vgf_ui_rows'):
        return
        
    idx = obj.zls_vgf_active
    if 0 <= idx < len(obj.zls_vgf_ui_rows):
        uid = obj.zls_vgf_ui_rows[idx].uid
        node = CommandController.get_node(obj, uid)
        if node and node.node_type == ITEM_GROUP:
            vg_idx = NativeAdapter.get_groups(obj).find(node.name)
            NativeAdapter.set_active_index(obj, vg_idx)

# =========================================================
# 6. OPERATORS (UI Triggers)
# =========================================================
class ZLSVGF_OT_add_folder(bpy.types.Operator):
    bl_idname = "zls_vgf.add_folder"
    bl_label = "Add Folder"
    bl_description = DESC_ADD_FOLDER
    bl_options = {'UNDO'}

    def execute(self, context):
        CommandController.execute_add_folder(context.object)
        return {'FINISHED'}

class ZLSVGF_OT_add_group(bpy.types.Operator):
    bl_idname = "zls_vgf.add_group"
    bl_label = "Add Vertex Group"
    bl_description = DESC_ADD_GROUP
    bl_options = {'UNDO'}

    def execute(self, context):
        CommandController.execute_add_group(context.object)
        return {'FINISHED'}

class ZLSVGF_OT_remove_item(bpy.types.Operator):
    bl_idname = "zls_vgf.remove_item"
    bl_label = "Remove Item"
    bl_description = DESC_REMOVE_ITEM
    bl_options = {'UNDO'}

    def execute(self, context):
        CommandController.execute_remove_active(context.object)
        return {'FINISHED'}

class ZLSVGF_OT_move_item_up(bpy.types.Operator):
    bl_idname = "zls_vgf.move_item_up"
    bl_label = "Move Item Up"
    bl_description = DESC_MOVE_UP
    bl_options = {'UNDO'}

    def execute(self, context):
        CommandController.execute_move(context.object, 'UP')
        return {'FINISHED'}

class ZLSVGF_OT_move_item_down(bpy.types.Operator):
    bl_idname = "zls_vgf.move_item_down"
    bl_label = "Move Item Down"
    bl_description = DESC_MOVE_DOWN
    bl_options = {'UNDO'}

    def execute(self, context):
        CommandController.execute_move(context.object, 'DOWN')
        return {'FINISHED'}

class ZLSVGF_OT_toggle_folder(bpy.types.Operator):
    bl_idname = "zls_vgf.toggle_folder"
    bl_label = "Toggle Folder"
    bl_description = DESC_TOGGLE_FOLDER
    bl_options = {'UNDO'}
    
    uid: bpy.props.StringProperty()

    def execute(self, context):
        node = CommandController.get_node(context.object, self.uid)
        if node:
            node.is_expanded = not node.is_expanded
            SyncService.rebuild_ui(context.object)
        return {'FINISHED'}

class ZLSVGF_OT_sync(bpy.types.Operator):
    bl_idname = "zls_vgf.sync"
    bl_label = "Sync Vertex Groups"
    bl_description = DESC_SYNC
    bl_options = {'UNDO'}

    def execute(self, context):
        SyncService.reconcile(context.object)
        self.report({'INFO'}, UI_STR_SYNC_SUCCESS)
        return {'FINISHED'}

class ZLSVGF_OT_copy_to_selected(bpy.types.Operator):
    bl_idname = "zls_vgf.copy_to_selected"
    bl_label = "Copy Vertex Groups to Selected"
    bl_description = DESC_COPY_SEL
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        sel_meshes = [o for o in context.selected_objects if o.type == 'MESH' and o != obj]

        if not sel_meshes:
            self.report({'WARNING'}, UI_STR_WARN_NO_MESH)
            return {'CANCELLED'}

        for m in sel_meshes:
            if len(m.data.vertices) != len(obj.data.vertices):
                msg = UI_STR_WARN_MISMATCH.format(name=m.name)
                self.report({'WARNING'}, msg)
                return {'CANCELLED'}

        try:
            bpy.ops.object.vertex_group_copy_to_selected()
            self.report({'INFO'}, UI_STR_COPY_SUCCESS)
        except RuntimeError as e:
            self.report({'WARNING'}, str(e))
            return {'CANCELLED'}

        return {'FINISHED'}

class ZLSVGF_OT_actions(bpy.types.Operator):
    bl_idname = "zls_vgf.actions"
    bl_label = "Run Special Operation"
    bl_options = {'UNDO'}
    
    action: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        action = properties.action
        if action == 'SORT_NAME': return "Sort vertex groups alphabetically by name"
        if action == 'SORT_BONE': return "Sort vertex groups based on armature bone hierarchy"
        if action == 'DUPLICATE': return "Make a copy of the active vertex group"
        if action == 'MIRROR': return "Mirror weights of the active vertex group to the opposite side"
        if action == 'MIRROR_TOPO': return "Mirror weights using topology symmetry"
        if action == 'DEL_ALL': return "Delete all vertex groups from this object"
        if action == 'DEL_UNLOCKED': return "Delete all vertex groups that are not locked"
        if action == 'LOCK_ALL': return "Lock all vertex groups to prevent changes"
        if action == 'UNLOCK_ALL': return "Unlock all vertex groups for editing"
        if action == 'LOCK_INVERT': return "Invert the lock state of all vertex groups"
        return "Vertex Group operation"

    def execute(self, context):
        obj = context.object
        target_uid = CommandController._get_active_target_folder(obj)
        active_uid = CommandController._get_active_uid(obj)
        
        if self.action == 'SORT_NAME': 
            NativeAdapter.sort_groups_name()
            SyncService.sync_order_from_native(obj)
        elif self.action == 'SORT_BONE': 
            NativeAdapter.sort_groups_bone()
            SyncService.sync_order_from_native(obj)
        elif self.action == 'DUPLICATE': 
            NativeAdapter.duplicate_group()
        elif self.action == 'MIRROR': 
            NativeAdapter.mirror_group(False)
        elif self.action == 'MIRROR_TOPO': 
            NativeAdapter.mirror_group(True)
        elif self.action == 'DEL_ALL': 
            NativeAdapter.remove_all_groups(obj)
        elif self.action == 'DEL_UNLOCKED': 
            NativeAdapter.remove_unlocked_groups(obj)
        elif self.action == 'LOCK_ALL': 
            NativeAdapter.lock_all(obj, 'LOCK')
        elif self.action == 'UNLOCK_ALL': 
            NativeAdapter.lock_all(obj, 'UNLOCK')
        elif self.action == 'LOCK_INVERT': 
            NativeAdapter.lock_all(obj, 'INVERT')
        
        SyncService.reconcile(obj, target_parent_uid=target_uid)
        
        if active_uid:
            CommandController._restore_selection(obj, active_uid)
            
        return {'FINISHED'}

# =========================================================
# 7. VIEW (UIList & Panel)
# =========================================================
class ZLSVGF_MT_actions(bpy.types.Menu):
    bl_idname = "ZLSVGF_MT_actions"
    bl_label = "Vertex Group Actions"

    def draw(self, context):
        layout = self.layout
        layout.operator("zls_vgf.actions", text="Sort by Name", icon='SORTALPHA').action = 'SORT_NAME'
        layout.operator("zls_vgf.actions", text="Sort by Bone Hierarchy").action = 'SORT_BONE'
        layout.separator()
        layout.operator("zls_vgf.actions", text="Duplicate Vertex Group").action = 'DUPLICATE'
        layout.operator("zls_vgf.copy_to_selected") 
        layout.operator("zls_vgf.actions", text="Mirror Vertex Group").action = 'MIRROR'
        layout.operator("zls_vgf.actions", text="Mirror Vertex Group (Topology)").action = 'MIRROR_TOPO'
        layout.separator()
        op = layout.operator("object.vertex_group_remove_from", text="Remove from All Groups")
        op.use_all_groups = True
        op = layout.operator("object.vertex_group_remove_from", text="Clear Active Group")
        op.use_all_verts = True
        layout.separator()
        layout.operator("zls_vgf.actions", text="Delete All Unlocked Groups").action = 'DEL_UNLOCKED'
        layout.operator("zls_vgf.actions", text="Delete All Groups").action = 'DEL_ALL'
        layout.separator()
        layout.operator("zls_vgf.actions", text="Lock All").action = 'LOCK_ALL'
        layout.operator("zls_vgf.actions", text="Unlock All").action = 'UNLOCK_ALL'
        layout.operator("zls_vgf.actions", text="Lock Invert All").action = 'LOCK_INVERT'

class ZLSVGF_UL_items(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj = data
        node = CommandController.get_node(obj, item.uid)
        if not node:
            return

        row = layout.row(align=True)
        for _ in range(item.depth):
            row.label(text="", icon='BLANK1')
            
        row = row.row(align=True)

        if node.node_type == ITEM_FOLDER:
            op = row.operator(
                "zls_vgf.toggle_folder", 
                text="", 
                emboss=False, 
                icon='TRIA_DOWN' if node.is_expanded else 'TRIA_RIGHT'
            )
            op.uid = node.uid
            row.prop(node, "folder_name", text="", icon='FILE_FOLDER', emboss=False)
            
            row_right = row.row(align=True)
            row_right.alignment = 'RIGHT'
            row_right.label(text="", icon='BLANK1')
            row_right.prop(node, "ui_parent_dropdown", text="", icon='ARROW_LEFTRIGHT', emboss=False)
        else:
            row.label(text="", icon='GROUP_VERTEX')
            vg = NativeAdapter.get_groups(obj).get(node.name)
            
            if vg:
                row.prop(node, "group_name", text="", emboss=False)
                row_right = row.row(align=True)
                row_right.alignment = 'RIGHT'
                row_right.prop(
                    vg, 
                    "lock_weight", 
                    text="", 
                    icon='LOCKED' if vg.lock_weight else 'UNLOCKED', 
                    emboss=False
                )
                row_right.prop(node, "ui_parent_dropdown", text="", icon='ARROW_LEFTRIGHT', emboss=False)
            else:
                row.label(text=UI_STR_PENDING, icon='ERROR')

class ZLSVGF_PT_panel(bpy.types.Panel):
    bl_label = "Vertex Group Folders"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"

    @classmethod
    def poll(cls, context):
        return context.object and context.object.type in {'MESH', 'LATTICE', 'ARMATURE'}

    def draw(self, context):
        layout = self.layout
        obj = context.object
        if not obj:
            return

        layout.operator("zls_vgf.sync", icon='FILE_REFRESH', text="Sync From Object")
        layout.separator()

        row = layout.row()
        row.template_list("ZLSVGF_UL_items", "vertex_group_folders", obj, "zls_vgf_ui_rows", obj, "zls_vgf_active", rows=7)

        col = row.column(align=True)
        col.operator("zls_vgf.add_folder", icon='NEWFOLDER', text="")
        col.separator()
        col.operator("zls_vgf.add_group", icon='ADD', text="")
        col.operator("zls_vgf.remove_item", icon='REMOVE', text="")
        col.separator()
        col.menu("ZLSVGF_MT_actions", icon='DOWNARROW_HLT', text="")
        col.separator()
        col.operator("zls_vgf.move_item_up", icon='TRIA_UP', text="")
        col.operator("zls_vgf.move_item_down", icon='TRIA_DOWN', text="")

        if obj.mode in {'EDIT', 'PAINT_WEIGHT'}:
            is_group_active = False
            
            if obj.zls_vgf_ui_rows and 0 <= obj.zls_vgf_active < len(obj.zls_vgf_ui_rows):
                node = CommandController.get_node(obj, obj.zls_vgf_ui_rows[obj.zls_vgf_active].uid)
                if node and node.node_type == ITEM_GROUP:
                    is_group_active = True
                    
            layout.separator()

            main_row = layout.row()
            main_row.enabled = is_group_active

            sub_row1 = main_row.row(align=True)
            sub_row1.operator("object.vertex_group_assign", text="Assign")
            sub_row1.operator("object.vertex_group_remove_from", text="Remove")

            sub_row2 = main_row.row(align=True)
            sub_row2.operator("object.vertex_group_select", text="Select")
            sub_row2.operator("object.vertex_group_deselect", text="Deselect")

            layout.prop(context.tool_settings, "vertex_group_weight", text="Weight")
            layout.prop(context.tool_settings, "use_auto_normalize", text="Auto Normalize")

# =========================================================
# REGISTRATION
# =========================================================
classes = (
    ZLSVGF_Node,
    ZLSVGF_UIRow,
    ZLSVGF_MT_actions,
    ZLSVGF_OT_add_folder,
    ZLSVGF_OT_add_group,
    ZLSVGF_OT_remove_item,
    ZLSVGF_OT_move_item_up,
    ZLSVGF_OT_move_item_down,
    ZLSVGF_OT_toggle_folder,
    ZLSVGF_OT_sync,
    ZLSVGF_OT_copy_to_selected,
    ZLSVGF_OT_actions,
    ZLSVGF_UL_items,
    ZLSVGF_PT_panel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
        
    bpy.types.Object.zls_vgf_nodes = bpy.props.CollectionProperty(type=ZLSVGF_Node)
    bpy.types.Object.zls_vgf_ui_rows = bpy.props.CollectionProperty(type=ZLSVGF_UIRow)
    bpy.types.Object.zls_vgf_active = bpy.props.IntProperty(
        name=UI_STR_ITEM,
        description=DESC_ACTIVE_ITEM,
        update=zls_vgf_on_active_update
    )

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
        
    if hasattr(bpy.types.Object, "zls_vgf_nodes"):
        del bpy.types.Object.zls_vgf_nodes
    if hasattr(bpy.types.Object, "zls_vgf_ui_rows"):
        del bpy.types.Object.zls_vgf_ui_rows
    if hasattr(bpy.types.Object, "zls_vgf_active"):
        del bpy.types.Object.zls_vgf_active

if __name__ == "__main__":
    register()

# SPDX-License-Identifier: GPL-3.0-or-later
bl_info = {
    "name": "VGF: Vertex Group Folders",
    "author": "Oleksandr Gubanov (Zingless)",
    "version": (1, 1, 0),
    "blender": (5, 1, 0),
    "location": "Properties > Object Data > Vertex Groups",
    "description": "Advanced tree-based manager for Vertex Groups",
    "category": "Object",
}

import bpy
import uuid

# =========================================================
# 1. CONSTANTS & UI STRINGS
# =========================================================
ROOT_UID    = "ROOT"
ITEM_FOLDER = "FOLDER"
ITEM_GROUP  = "GROUP"

UI_STR_FOLDER       = "Folder"
UI_STR_GROUP        = "Vertex Group"
UI_STR_ROOT         = "Root"
UI_STR_ITEM         = "Item"
UI_STR_PENDING      = "[Pending Sync]"
UI_STR_SYNC_SUCCESS = "Vertex Groups Synced"
UI_STR_COPY_SUCCESS = "Vertex groups copied successfully"
UI_STR_WARN_NO_MESH = "No other mesh objects selected!"
UI_STR_WARN_MISMATCH = "Vertex count mismatch with {name}! Check topology."

DESC_ADD_FOLDER    = "Create a new folder to organize vertex groups"
DESC_ADD_GROUP     = "Add a new vertex group to the object"
DESC_REMOVE_ITEM   = "Remove the selected item (promotes children if expanded, deletes if collapsed)"
DESC_MOVE_UP       = "Move the selected item up within its parent"
DESC_MOVE_DOWN     = "Move the selected item down within its parent"
DESC_TOGGLE_FOLDER = "Expand or collapse item contents"
DESC_SYNC          = "Synchronize structure with native vertex groups"
DESC_COPY_SEL      = "Copy vertex groups to other selected mesh objects"
DESC_MOVE_TO_PARENT = "Move to another folder or group (Type to Search)"
DESC_ACTIVE_ITEM   = "Active item"
DESC_DUPLICATE     = "Make a copy of the active vertex group, placed in the same node"
DESC_DEL_ALL       = "Delete all vertex groups and clear all tree nodes"
DESC_DEL_UNLOCKED  = "Delete all unlocked vertex groups and their tree nodes"


def new_uid():
    """Generate a unique identifier for VGF nodes."""
    return uuid.uuid4().hex


# =========================================================
# 2. AUTO-SYNC STATE
# =========================================================
_vg_state_cache: dict[str, tuple] = {}

def _vgf_state_snapshot(obj) -> tuple:
    """Order-aware snapshot of the native vertex_groups list."""
    return tuple(vg.name for vg in obj.vertex_groups)


# =========================================================
# 3. NATIVE ADAPTER (zero bpy.ops calls)
# =========================================================
class NativeAdapter:
    @staticmethod
    def get_groups(obj):
        return obj.vertex_groups

    @staticmethod
    def add_group(obj, name="Group"):
        return obj.vertex_groups.new(name=name)

    @staticmethod
    def remove_group(obj, vg):
        obj.vertex_groups.remove(vg)

    @staticmethod
    def remove_active_group(obj):
        vg = obj.vertex_groups.active
        if vg:
            obj.vertex_groups.remove(vg)

    @staticmethod
    def remove_all_groups(obj):
        obj.vertex_groups.clear()

    @staticmethod
    def remove_unlocked_groups(obj):
        for vg in reversed(list(obj.vertex_groups)):
            if not vg.lock_weight:
                obj.vertex_groups.remove(vg)

    @staticmethod
    def duplicate_group(obj):
        active_vg = obj.vertex_groups.active
        if not active_vg:
            return None

        new_vg = obj.vertex_groups.new(name=active_vg.name + "_copy")
        for v in obj.data.vertices:
            try:
                weight = active_vg.weight(v.index)
                new_vg.add([v.index], weight, 'REPLACE')
            except RuntimeError:
                pass 
        obj.vertex_groups.active_index = new_vg.index
        return new_vg

    @staticmethod
    def copy_groups_to_object(src, dst):
        for src_vg in src.vertex_groups:
            dst_vg = (
                dst.vertex_groups[src_vg.name]
                if src_vg.name in dst.vertex_groups
                else dst.vertex_groups.new(name=src_vg.name)
            )
            for v in src.data.vertices:
                try:
                    dst_vg.add([v.index], src_vg.weight(v.index), 'REPLACE')
                except RuntimeError:
                    pass

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
# 4. SYNC SERVICE
# =========================================================
class SyncService:
    @staticmethod
    def sync_order_from_native(obj):
        native_order = {vg.name: i for i, vg in enumerate(NativeAdapter.get_groups(obj))}
        children_map: dict[str, list] = {}
        
        for node in obj.zls_vgf_nodes:
            children_map.setdefault(node.parent_uid, []).append(node)

        for children in children_map.values():
            folders = [n for n in children if n.node_type == ITEM_FOLDER]
            groups  = [n for n in children if n.node_type == ITEM_GROUP]

            folders.sort(key=lambda x: x.sort_key)
            groups.sort(key=lambda x: native_order.get(x.name, 999_999))

            group_slots = [i for i, n in enumerate(children) if n.node_type == ITEM_GROUP]
            for slot, group in zip(group_slots, groups):
                children[slot] = group

            for i, node in enumerate(children):
                node.sort_key = i

        _vg_state_cache[obj.name] = _vgf_state_snapshot(obj)

    @staticmethod
    def reconcile(obj, target_parent_uid=ROOT_UID):
        active_uid  = CommandController._get_active_uid(obj)
        native_vgs  = list(NativeAdapter.get_groups(obj))
        native_names = {vg.name for vg in native_vgs}
        changed = False

        uids_to_remove = set()
        for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
            node = obj.zls_vgf_nodes[i]
            if node.node_type == ITEM_GROUP and node.name not in native_names:
                uids_to_remove.add(node.uid)
                obj.zls_vgf_nodes.remove(i)
                changed = True

        if uids_to_remove:
            for node in obj.zls_vgf_nodes:
                if node.parent_uid in uids_to_remove:
                    node.parent_uid = ROOT_UID
                    node.sort_key = 999_999
                    changed = True

        our_names = {n.name for n in obj.zls_vgf_nodes if n.node_type == ITEM_GROUP}
        for vg in native_vgs:
            if vg.name not in our_names:
                node            = obj.zls_vgf_nodes.add()
                node.uid        = new_uid()
                node.node_type  = ITEM_GROUP
                node.name       = vg.name
                node.parent_uid = target_parent_uid
                node.sort_key   = 999_999
                changed = True

        native_active_idx = NativeAdapter.get_groups(obj).active_index
        if 0 <= native_active_idx < len(native_vgs):
            native_active_name = native_vgs[native_active_idx].name
            active_node = next(
                (n for n in obj.zls_vgf_nodes
                 if n.node_type == ITEM_GROUP and n.name == native_active_name),
                None,
            )
            if active_node:
                active_uid = active_node.uid

        if changed:
            SyncService.normalize_sort(obj)

        SyncService.rebuild_ui(obj)

        if active_uid:
            CommandController._restore_selection(obj, active_uid)

        _vg_state_cache[obj.name] = _vgf_state_snapshot(obj)

    @staticmethod
    def normalize_sort(obj):
        children_map: dict[str, list] = {}
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
                row       = obj.zls_vgf_ui_rows.add()
                row.uid   = child.uid
                row.depth = depth
                if child.is_expanded:
                    walk(child.uid, depth + 1)

        walk(ROOT_UID, 0)


# =========================================================
# 5. DEPSGRAPH AUTO-SYNC HANDLER
# =========================================================
@bpy.app.handlers.persistent
def _vgf_depsgraph_update(scene, depsgraph):
    for update in depsgraph.updates:
        if not isinstance(update.id, bpy.types.Object):
            continue
        try:
            obj = update.id
        except Exception:
            continue

        if not hasattr(obj, 'zls_vgf_nodes') or not obj.zls_vgf_nodes:
            continue

        current = _vgf_state_snapshot(obj)
        cached  = _vg_state_cache.get(obj.name)

        if cached == current:
            continue

        if cached is not None and frozenset(cached) == frozenset(current):
            SyncService.sync_order_from_native(obj)
            SyncService.rebuild_ui(obj)
            _vg_state_cache[obj.name] = current
        else:
            SyncService.reconcile(obj)

@bpy.app.handlers.persistent
def _vgf_on_load_post(*_args):
    _vg_state_cache.clear()


# =========================================================
# 6. CONTROLLER / COMMANDS
# =========================================================
class CommandController:
    @staticmethod
    def _get_active_uid(obj):
        rows = getattr(obj, 'zls_vgf_ui_rows', None)
        if rows and 0 <= obj.zls_vgf_active < len(rows):
            return rows[obj.zls_vgf_active].uid
        return None

    @staticmethod
    def _get_active_target_parent(obj):
        rows = getattr(obj, 'zls_vgf_ui_rows', None)
        if rows and 0 <= obj.zls_vgf_active < len(rows):
            uid  = rows[obj.zls_vgf_active].uid
            node = CommandController.get_node(obj, uid)
            if node:
                return node.uid
        return ROOT_UID

    @staticmethod
    def _restore_selection(obj, target_uid):
        for i, row in enumerate(obj.zls_vgf_ui_rows):
            if row.uid == target_uid:
                obj.zls_vgf_active = i
                return

    @staticmethod
    def get_node(obj, uid):
        for n in obj.zls_vgf_nodes:
            if n.uid == uid:
                return n
        return None

    @staticmethod
    def _get_armature(obj):
        if obj.parent and obj.parent.type == 'ARMATURE':
            return obj.parent
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object:
                return mod.object
        return None

    @staticmethod
    def execute_add_folder(obj):
        target_uid      = CommandController._get_active_target_parent(obj)
        node            = obj.zls_vgf_nodes.add()
        node.uid        = new_uid()
        node.node_type  = ITEM_FOLDER
        node.name       = UI_STR_FOLDER
        node.parent_uid = target_uid
        node.sort_key   = 999_999

        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        CommandController._restore_selection(obj, node.uid)

    @staticmethod
    def execute_add_group(obj):
        target_uid = CommandController._get_active_target_parent(obj)
        NativeAdapter.add_group(obj)
        SyncService.reconcile(obj, target_parent_uid=target_uid)

    @staticmethod
    def execute_duplicate_group(obj):
        target_uid = CommandController._get_active_target_parent(obj)
        new_vg     = NativeAdapter.duplicate_group(obj)
        if new_vg is None:
            return
        SyncService.reconcile(obj, target_parent_uid=target_uid)

    @staticmethod
    def execute_remove_active(obj):
        idx = obj.zls_vgf_active
        if not (0 <= idx < len(obj.zls_vgf_ui_rows)):
            return

        uid  = obj.zls_vgf_ui_rows[idx].uid
        node = CommandController.get_node(obj, uid)
        if not node:
            return

        fallback_idx = max(0, idx - 1)
        fallback_uid = (
            obj.zls_vgf_ui_rows[fallback_idx].uid
            if len(obj.zls_vgf_ui_rows) > 1 else None
        )

        uids_to_del_vg = set()
        uids_to_del_nodes = set()

        if node.is_expanded:
            for child in [n for n in obj.zls_vgf_nodes if n.parent_uid == node.uid]:
                child.parent_uid = node.parent_uid
                child.sort_key = 999_999
            uids_to_del_nodes.add(node.uid)
            if node.node_type == ITEM_GROUP:
                uids_to_del_vg.add(node.name)
        else:
            def gather(c_uid):
                uids_to_del_nodes.add(c_uid)
                c_node = CommandController.get_node(obj, c_uid)
                if c_node and c_node.node_type == ITEM_GROUP:
                    uids_to_del_vg.add(c_node.name)
                for child in [n for n in obj.zls_vgf_nodes if n.parent_uid == c_uid]:
                    gather(child.uid)
            gather(node.uid)

        for vg_name in uids_to_del_vg:
            vg = NativeAdapter.get_groups(obj).get(vg_name)
            if vg:
                NativeAdapter.get_groups(obj).remove(vg)

        for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
            if obj.zls_vgf_nodes[i].uid in uids_to_del_nodes:
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
            node.name  = final_name
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

        uid  = obj.zls_vgf_ui_rows[idx].uid
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

        if new_parent_uid != ROOT_UID:
            curr = new_parent_uid
            while curr != ROOT_UID:
                if curr == node.uid:
                    return
                p_node = CommandController.get_node(obj, curr)
                if not p_node:
                    break
                curr = p_node.parent_uid

        active_uid       = CommandController._get_active_uid(obj)
        node.parent_uid  = new_parent_uid
        node.sort_key    = 999_999

        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)

        if active_uid:
            CommandController._restore_selection(obj, active_uid)

    @staticmethod
    def execute_sort_alphabetically(obj):
        children_map = {}
        for node in obj.zls_vgf_nodes:
            children_map.setdefault(node.parent_uid, []).append(node)

        for bucket in children_map.values():
            bucket.sort(key=lambda n: n.name.lower())
            for i, node in enumerate(bucket):
                node.sort_key = i

        SyncService.rebuild_ui(obj)
        return True, ""

    @staticmethod
    def execute_sort_by_bones(obj):
        arm = CommandController._get_armature(obj)
        if not arm:
            return False, "No Armature found for bone sorting."

        def get_bone_hierarchy_order(armature):
            order = []
            def walk(bone):
                order.append(bone.name)
                for child in bone.children:
                    walk(child)
            roots = [b for b in armature.data.bones if not b.parent]
            for r in roots:
                walk(r)
            return order

        bone_order = get_bone_hierarchy_order(arm)
        if not bone_order:
            return False, "Armature has no bones."

        bone_order_dict = {name: i for i, name in enumerate(bone_order)}

        children_map = {}
        for node in obj.zls_vgf_nodes:
            children_map.setdefault(node.parent_uid, []).append(node)

        for bucket in children_map.values():
            def get_sort_key(n):
                return (bone_order_dict.get(n.name, 999999), n.name.lower())
            bucket.sort(key=get_sort_key)
            for i, node in enumerate(bucket):
                node.sort_key = i

        SyncService.rebuild_ui(obj)
        return True, ""

    @staticmethod
    def execute_build_bone_hierarchy(obj):
        arm = CommandController._get_armature(obj)
        if not arm:
            return False, "No Armature found to build hierarchy."

        vg_nodes = {n.name: n for n in obj.zls_vgf_nodes if n.node_type == ITEM_GROUP}
        changed = False

        for vg_name, node in vg_nodes.items():
            bone = arm.data.bones.get(vg_name)
            if bone:
                parent_bone = bone.parent
                parent_uid = ROOT_UID
                
                # Traverse up to find the closest bone that actually has a corresponding Vertex Group
                while parent_bone:
                    if parent_bone.name in vg_nodes:
                        parent_uid = vg_nodes[parent_bone.name].uid
                        break
                    parent_bone = parent_bone.parent

                if node.parent_uid != parent_uid:
                    node.parent_uid = parent_uid
                    node.sort_key = 999_999
                    changed = True
            else:
                if node.parent_uid != ROOT_UID:
                    node.parent_uid = ROOT_UID
                    node.sort_key = 999_999
                    changed = True

        if changed:
            SyncService.normalize_sort(obj)
            SyncService.rebuild_ui(obj)
        return True, ""

    @staticmethod
    def execute_reset_tree(obj):
        for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
            if obj.zls_vgf_nodes[i].node_type == ITEM_FOLDER:
                obj.zls_vgf_nodes.remove(i)
                
        for node in obj.zls_vgf_nodes:
            node.parent_uid = ROOT_UID
            node.sort_key = 999_999
            
        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        return True


# =========================================================
# 7. MODEL  (PropertyGroups)
# =========================================================
def zls_vgf_ui_name_get(self):
    return self.name

def zls_vgf_ui_name_set(self, value):
    if getattr(self, "_is_updating", False):
        return
    self._is_updating = True
    CommandController.execute_rename(self.id_data, self.uid, value)
    self._is_updating = False

_parent_enum_cache = []

def parent_enum_generator(self, context):
    global _parent_enum_cache
    obj = context.object if context else bpy.context.object

    items = [(ROOT_UID, UI_STR_ROOT, "", 'OUTLINER_COLLECTION', 0)]
    if obj:
        idx = 1
        for n in obj.zls_vgf_nodes:
            icon = 'FILE_FOLDER' if n.node_type == ITEM_FOLDER else 'GROUP_VERTEX'
            items.append((n.uid, n.name, "Folder" if n.node_type == ITEM_FOLDER else "Vertex Group", icon, idx))
            idx += 1

    _parent_enum_cache = items
    return _parent_enum_cache

def parent_dropdown_get(self):
    obj = self.id_data
    if self.parent_uid == ROOT_UID:
        return 0
    idx = 1
    for n in obj.zls_vgf_nodes:
        if n.uid == self.parent_uid:
            return idx
        idx += 1
    return 0

def parent_dropdown_set(self, value):
    obj = self.id_data
    if value == 0:
        CommandController.execute_change_parent(obj, self.uid, ROOT_UID)
        return
    idx = 1
    for n in obj.zls_vgf_nodes:
        if idx == value:
            CommandController.execute_change_parent(obj, self.uid, n.uid)
            return
        idx += 1


class ZLSVGF_Node(bpy.types.PropertyGroup):
    uid:        bpy.props.StringProperty()
    node_type:  bpy.props.EnumProperty(
        items=[(ITEM_FOLDER, UI_STR_FOLDER, ""), (ITEM_GROUP, UI_STR_GROUP, "")]
    )
    name:       bpy.props.StringProperty()
    parent_uid: bpy.props.StringProperty(default=ROOT_UID)
    sort_key:   bpy.props.IntProperty(default=0)
    is_expanded: bpy.props.BoolProperty(default=True)

    folder_name: bpy.props.StringProperty(
        get=zls_vgf_ui_name_get, set=zls_vgf_ui_name_set,
        description=UI_STR_FOLDER,
    )
    group_name: bpy.props.StringProperty(
        get=zls_vgf_ui_name_get, set=zls_vgf_ui_name_set,
        description=UI_STR_GROUP,
    )
    ui_parent_dropdown: bpy.props.EnumProperty(
        items=parent_enum_generator,
        get=parent_dropdown_get, set=parent_dropdown_set,
        name="", description=DESC_MOVE_TO_PARENT,
    )

class ZLSVGF_UIRow(bpy.types.PropertyGroup):
    uid:   bpy.props.StringProperty()
    depth: bpy.props.IntProperty()


def zls_vgf_on_active_update(self, context):
    obj = context.object
    if not obj or not hasattr(obj, 'zls_vgf_ui_rows'):
        return
    idx = obj.zls_vgf_active
    if 0 <= idx < len(obj.zls_vgf_ui_rows):
        uid  = obj.zls_vgf_ui_rows[idx].uid
        node = CommandController.get_node(obj, uid)
        if node and node.node_type == ITEM_GROUP:
            vg_idx = NativeAdapter.get_groups(obj).find(node.name)
            NativeAdapter.set_active_index(obj, vg_idx)


# =========================================================
# 8. OPERATORS
# =========================================================
class ZLSVGF_OT_add_folder(bpy.types.Operator):
    bl_idname   = "zls_vgf.add_folder"
    bl_label    = "Add Folder"
    bl_description = DESC_ADD_FOLDER
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        CommandController.execute_add_folder(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_add_group(bpy.types.Operator):
    bl_idname   = "zls_vgf.add_group"
    bl_label    = "Add Vertex Group"
    bl_description = DESC_ADD_GROUP
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        CommandController.execute_add_group(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_remove_item(bpy.types.Operator):
    bl_idname   = "zls_vgf.remove_item"
    bl_label    = "Remove Item"
    bl_description = DESC_REMOVE_ITEM
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        CommandController.execute_remove_active(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_move_item_up(bpy.types.Operator):
    bl_idname   = "zls_vgf.move_item_up"
    bl_label    = "Move Item Up"
    bl_description = DESC_MOVE_UP
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        CommandController.execute_move(context.object, 'UP')
        return {'FINISHED'}


class ZLSVGF_OT_move_item_down(bpy.types.Operator):
    bl_idname   = "zls_vgf.move_item_down"
    bl_label    = "Move Item Down"
    bl_description = DESC_MOVE_DOWN
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        CommandController.execute_move(context.object, 'DOWN')
        return {'FINISHED'}


class ZLSVGF_OT_toggle_folder(bpy.types.Operator):
    bl_idname   = "zls_vgf.toggle_folder"
    bl_label    = "Toggle Folder"
    bl_description = DESC_TOGGLE_FOLDER
    bl_options  = {'REGISTER', 'UNDO'}

    uid: bpy.props.StringProperty()

    def execute(self, context):
        node = CommandController.get_node(context.object, self.uid)
        if node:
            node.is_expanded = not node.is_expanded
            SyncService.rebuild_ui(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_sort_alphabetically(bpy.types.Operator):
    bl_idname   = "zls_vgf.sort_alphabetically"
    bl_label    = "Sort Alphabetically"
    bl_description = "Recursively sort the internal tree structure alphabetically"
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        CommandController.execute_sort_alphabetically(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_sort_by_bones(bpy.types.Operator):
    bl_idname   = "zls_vgf.sort_by_bones"
    bl_label    = "Sort by Bones"
    bl_description = "Sort internal groups matching the Armature bone order"
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        success, msg = CommandController.execute_sort_by_bones(context.object)
        if not success:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


class ZLSVGF_OT_build_bone_hierarchy(bpy.types.Operator):
    bl_idname   = "zls_vgf.build_bone_hierarchy"
    bl_label    = "Build Bone Hierarchy"
    bl_description = "Restructure the VGF tree according to Armature bone hierarchy"
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        success, msg = CommandController.execute_build_bone_hierarchy(context.object)
        if not success:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


class ZLSVGF_OT_reset_tree(bpy.types.Operator):
    bl_idname   = "zls_vgf.reset_tree"
    bl_label    = "Reset Tree Structure"
    bl_description = "Flatten the tree and remove all folders"
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        CommandController.execute_reset_tree(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_sync(bpy.types.Operator):
    bl_idname   = "zls_vgf.sync"
    bl_label    = "Sync Vertex Groups"
    bl_description = DESC_SYNC
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        SyncService.reconcile(obj)
        self.report({'INFO'}, UI_STR_SYNC_SUCCESS)
        return {'FINISHED'}


class ZLSVGF_OT_duplicate_group(bpy.types.Operator):
    bl_idname   = "zls_vgf.duplicate_group"
    bl_label    = "Duplicate Vertex Group"
    bl_description = DESC_DUPLICATE
    bl_options  = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return (
            obj is not None
            and obj.type in {'MESH', 'LATTICE', 'ARMATURE'}
            and obj.vertex_groups.active is not None
        )

    def execute(self, context):
        CommandController.execute_duplicate_group(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_delete_all_groups(bpy.types.Operator):
    bl_idname   = "zls_vgf.delete_all_groups"
    bl_label    = "Delete All Vertex Groups"
    bl_description = DESC_DEL_ALL
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        NativeAdapter.remove_all_groups(obj)

        for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
            if obj.zls_vgf_nodes[i].node_type == ITEM_GROUP:
                obj.zls_vgf_nodes.remove(i)

        _vg_state_cache[obj.name] = _vgf_state_snapshot(obj)
        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        return {'FINISHED'}


class ZLSVGF_OT_delete_unlocked_groups(bpy.types.Operator):
    bl_idname   = "zls_vgf.delete_unlocked_groups"
    bl_label    = "Delete All Unlocked Groups"
    bl_description = DESC_DEL_UNLOCKED
    bl_options  = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        unlocked = {vg.name for vg in obj.vertex_groups if not vg.lock_weight}
        NativeAdapter.remove_unlocked_groups(obj)

        for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
            n = obj.zls_vgf_nodes[i]
            if n.node_type == ITEM_GROUP and n.name in unlocked:
                obj.zls_vgf_nodes.remove(i)

        _vg_state_cache[obj.name] = _vgf_state_snapshot(obj)
        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        return {'FINISHED'}


class ZLSVGF_OT_copy_to_selected(bpy.types.Operator):
    bl_idname   = "zls_vgf.copy_to_selected"
    bl_label    = "Copy Vertex Groups to Selected"
    bl_description = DESC_COPY_SEL
    bl_options  = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        src = context.active_object
        targets = [o for o in context.selected_objects if o.type == 'MESH' and o != src]

        if not targets:
            self.report({'WARNING'}, UI_STR_WARN_NO_MESH)
            return {'CANCELLED'}

        for dst in targets:
            if len(dst.data.vertices) != len(src.data.vertices):
                self.report({'WARNING'}, UI_STR_WARN_MISMATCH.format(name=dst.name))
                return {'CANCELLED'}

        for dst in targets:
            NativeAdapter.copy_groups_to_object(src, dst)

        self.report({'INFO'}, UI_STR_COPY_SUCCESS)
        return {'FINISHED'}


# =========================================================
# 9. VIEW  (UIList, Menu, Panel)
# =========================================================
class ZLSVGF_MT_actions(bpy.types.Menu):
    bl_idname = "ZLSVGF_MT_actions"
    bl_label  = "Vertex Group Actions"

    def draw(self, context):
        layout = self.layout

        layout.operator("zls_vgf.sort_alphabetically", icon='SORTALPHA')
        layout.operator("zls_vgf.sort_by_bones", icon='BONE_DATA')
        layout.operator("zls_vgf.build_bone_hierarchy", icon='OUTLINER_OB_ARMATURE')
        layout.operator("zls_vgf.reset_tree", icon='LOOP_BACK')
        layout.separator()

        layout.operator("zls_vgf.duplicate_group", text="Duplicate Vertex Group")
        layout.operator("zls_vgf.copy_to_selected")

        layout.operator("object.vertex_group_mirror", text="Mirror Vertex Group")
        layout.operator(
            "object.vertex_group_mirror",
            text="Mirror Vertex Group (Topology)",
        ).use_topology = True
        layout.separator()

        layout.operator(
            "object.vertex_group_remove_from",
            text="Remove from All Groups",
        ).use_all_groups = True
        layout.operator(
            "object.vertex_group_remove_from",
            text="Clear Active Group",
        ).use_all_verts = True
        layout.separator()

        layout.operator("zls_vgf.delete_unlocked_groups", text="Delete All Unlocked Groups")
        layout.operator("zls_vgf.delete_all_groups",      text="Delete All Groups")
        layout.separator()

        layout.operator("object.vertex_group_lock", text="Lock All").action   = 'LOCK'
        layout.operator("object.vertex_group_lock", text="Unlock All").action = 'UNLOCK'
        layout.operator("object.vertex_group_lock", text="Lock Invert All").action = 'INVERT'


class ZLSVGF_UL_items(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj  = data
        node = CommandController.get_node(obj, item.uid)
        if not node:
            return

        has_children = any(n.parent_uid == node.uid for n in obj.zls_vgf_nodes)

        row = layout.row(align=True)
        for _ in range(item.depth):
            row.label(text="", icon='BLANK1')

        row = row.row(align=True)

        # UI Automatically converts nodes to visual containers if they have children
        if node.node_type == ITEM_FOLDER or has_children:
            op = row.operator(
                "zls_vgf.toggle_folder", text="", emboss=False,
                icon='TRIA_DOWN' if node.is_expanded else 'TRIA_RIGHT',
            )
            op.uid = node.uid
        else:
            row.label(text="", icon='BLANK1')

        if node.node_type == ITEM_FOLDER:
            row.prop(node, "folder_name", text="", icon='FILE_FOLDER', emboss=False)
            rr = row.row(align=True)
            rr.alignment = 'RIGHT'
            rr.prop(node, "ui_parent_dropdown", text="", icon='ARROW_LEFTRIGHT', emboss=False)
        else:
            vg = NativeAdapter.get_groups(obj).get(node.name)
            if vg:
                row.prop(node, "group_name", text="", icon='GROUP_VERTEX', emboss=False)
                rr = row.row(align=True)
                rr.alignment = 'RIGHT'
                rr.prop(
                    vg, "lock_weight", text="",
                    icon='LOCKED' if vg.lock_weight else 'UNLOCKED',
                    emboss=False,
                )
                rr.prop(node, "ui_parent_dropdown", text="", icon='ARROW_LEFTRIGHT', emboss=False)
            else:
                row.label(text=node.name + " " + UI_STR_PENDING, icon='ERROR')


class ZLSVGF_PT_panel(bpy.types.Panel):
    bl_label       = "Vertex Group Folders"
    bl_space_type  = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context     = "data"

    @classmethod
    def poll(cls, context):
        return context.object and context.object.type in {'MESH', 'LATTICE', 'ARMATURE'}

    def draw(self, context):
        layout = self.layout
        obj    = context.object
        if not obj:
            return

        row = layout.row()
        row.template_list(
            "ZLSVGF_UL_items", "vertex_group_folders",
            obj, "zls_vgf_ui_rows",
            obj, "zls_vgf_active",
            rows=7,
        )

        col = row.column(align=True)
        col.operator("zls_vgf.add_folder",    icon='NEWFOLDER', text="")
        col.separator()
        col.operator("zls_vgf.add_group",     icon='ADD',       text="")
        col.operator("zls_vgf.remove_item",   icon='REMOVE',    text="")
        col.separator()
        col.menu("ZLSVGF_MT_actions",         icon='DOWNARROW_HLT', text="")
        col.separator()
        col.operator("zls_vgf.move_item_up",   icon='TRIA_UP',   text="")
        col.operator("zls_vgf.move_item_down", icon='TRIA_DOWN', text="")
        col.separator()
        col.operator("zls_vgf.sync",           icon='FILE_REFRESH', text="")

        if obj.mode in {'EDIT', 'PAINT_WEIGHT'}:
            is_group_active = False
            rows = obj.zls_vgf_ui_rows
            if rows and 0 <= obj.zls_vgf_active < len(rows):
                node = CommandController.get_node(obj, rows[obj.zls_vgf_active].uid)
                if node and node.node_type == ITEM_GROUP:
                    is_group_active = True

            layout.separator()
            main_row = layout.row()
            main_row.enabled = is_group_active

            sub1 = main_row.row(align=True)
            sub1.operator("object.vertex_group_assign",      text="Assign")
            sub1.operator("object.vertex_group_remove_from", text="Remove")

            sub2 = main_row.row(align=True)
            sub2.operator("object.vertex_group_select",   text="Select")
            sub2.operator("object.vertex_group_deselect", text="Deselect")

            layout.prop(context.tool_settings, "vertex_group_weight", text="Weight")
            layout.prop(context.tool_settings, "use_auto_normalize",  text="Auto Normalize")


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
    ZLSVGF_OT_sort_alphabetically,
    ZLSVGF_OT_sort_by_bones,
    ZLSVGF_OT_build_bone_hierarchy,
    ZLSVGF_OT_reset_tree,
    ZLSVGF_OT_sync,
    ZLSVGF_OT_duplicate_group,
    ZLSVGF_OT_delete_all_groups,
    ZLSVGF_OT_delete_unlocked_groups,
    ZLSVGF_OT_copy_to_selected,
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
        update=zls_vgf_on_active_update,
    )

    if _vgf_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_vgf_depsgraph_update)
    if _vgf_on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_vgf_on_load_post)


def unregister():
    if _vgf_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_vgf_depsgraph_update)
    if _vgf_on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_vgf_on_load_post)

    _vg_state_cache.clear()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    for attr in ("zls_vgf_nodes", "zls_vgf_ui_rows", "zls_vgf_active"):
        if hasattr(bpy.types.Object, attr):
            delattr(bpy.types.Object, attr)


if __name__ == "__main__":
    register()
# SPDX-License-Identifier: GPL-3.0-or-later
bl_info = {
    "name": "VGF: Vertex Group Folders",
    "author": "Oleksandr Gubanov (Zingless)",
    "version": (1, 0, 3),
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
DESC_REMOVE_ITEM   = "Remove the selected folder or vertex group"
DESC_MOVE_UP       = "Move the selected item up within its folder"
DESC_MOVE_DOWN     = "Move the selected item down within its folder"
DESC_TOGGLE_FOLDER = "Expand or collapse folder contents"
DESC_SYNC          = "Synchronize structure with native vertex groups"
DESC_COPY_SEL      = "Copy vertex groups to other selected mesh objects"
DESC_MOVE_TO_FOLDER = "Move to folder"
DESC_ACTIVE_ITEM   = "Active folder or vertex group"
DESC_DUPLICATE     = "Make a copy of the active vertex group, placed in the same folder"
DESC_DEL_ALL       = "Delete all vertex groups and clear all folder nodes"
DESC_DEL_UNLOCKED  = "Delete all unlocked vertex groups and their folder nodes"


def new_uid():
    """Generate a unique identifier for VGF nodes."""
    return uuid.uuid4().hex


# =========================================================
# 2. AUTO-SYNC STATE (module-level, shared with SyncService)
# =========================================================

# Cache: obj.name -> tuple of vg names in native order
# Tuple is order-aware → catches both add/remove AND sort/mirror reorder events.
_vg_state_cache: dict[str, tuple] = {}


def _vgf_state_snapshot(obj) -> tuple:
    """Order-aware snapshot of the native vertex_groups list."""
    return tuple(vg.name for vg in obj.vertex_groups)


# =========================================================
# 3. NATIVE ADAPTER  (pure Python API — zero bpy.ops calls)
# =========================================================
class NativeAdapter:
    """
    Wraps direct Blender data API calls.
    Never calls bpy.ops — all operations go through the data layer.
    """

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
        """
        Pure-Python duplicate: creates a copy of the active vertex group
        with all weights, without calling bpy.ops.object.vertex_group_copy.
        Returns the new VertexGroup, or None if no active group.
        """
        active_vg = obj.vertex_groups.active
        if not active_vg:
            return None

        # obj.vertex_groups.new() auto-resolves name collisions ("_copy", "_copy.001" …)
        new_vg = obj.vertex_groups.new(name=active_vg.name + "_copy")

        # Copy per-vertex weights
        for v in obj.data.vertices:
            try:
                weight = active_vg.weight(v.index)
                new_vg.add([v.index], weight, 'REPLACE')
            except RuntimeError:
                pass  # vertex not in source group — skip

        obj.vertex_groups.active_index = new_vg.index
        return new_vg

    @staticmethod
    def copy_groups_to_object(src, dst):
        """
        Pure-Python copy of all vertex groups from src to dst.
        dst must have the same vertex count as src.
        """
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
    """Manages synchronisation between Blender's native groups and VGF nodes."""

    @staticmethod
    def sync_order_from_native(obj):
        """
        Reorder GROUP nodes inside each folder bucket to match
        the current native vertex_groups ordering.
        Called after sort or mirror operations.
        """
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

        # Update cache so the depsgraph handler does not re-fire
        _vg_state_cache[obj.name] = _vgf_state_snapshot(obj)

    @staticmethod
    def reconcile(obj, target_parent_uid=ROOT_UID):
        """
        Full two-way reconcile:
        • Removes VGF nodes whose native group no longer exists.
        • Adds VGF nodes for native groups not yet tracked, placing them
          under target_parent_uid.
        • Rebuilds the UI list.
        """
        active_uid  = CommandController._get_active_uid(obj)
        native_vgs  = list(NativeAdapter.get_groups(obj))
        native_names = {vg.name for vg in native_vgs}
        changed = False

        # ── Remove stale GROUP nodes ──────────────────────────────────────
        for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
            node = obj.zls_vgf_nodes[i]
            if node.node_type == ITEM_GROUP and node.name not in native_names:
                obj.zls_vgf_nodes.remove(i)
                changed = True

        # ── Add missing GROUP nodes ───────────────────────────────────────
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

        # ── Sync active selection ─────────────────────────────────────────
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

        # Update cache — prevents depsgraph handler from double-firing
        _vg_state_cache[obj.name] = _vgf_state_snapshot(obj)

    @staticmethod
    def normalize_sort(obj):
        """Compact sort_key values for every bucket so they are 0-based and gapless."""
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
        """Flatten the folder tree into the UIList rows collection."""
        obj.zls_vgf_ui_rows.clear()

        def walk(parent_uid, depth):
            children = [n for n in obj.zls_vgf_nodes if n.parent_uid == parent_uid]
            children.sort(key=lambda x: x.sort_key)
            for child in children:
                row       = obj.zls_vgf_ui_rows.add()
                row.uid   = child.uid
                row.depth = depth
                if child.node_type == ITEM_FOLDER and child.is_expanded:
                    walk(child.uid, depth + 1)

        walk(ROOT_UID, 0)


# =========================================================
# 5. DEPSGRAPH AUTO-SYNC HANDLER
# =========================================================

@bpy.app.handlers.persistent
def _vgf_depsgraph_update(scene, depsgraph):
    """
    Lightweight depsgraph_update_post handler.

    Detects two kinds of native vertex_group changes and reacts accordingly:
    • Add / Remove  → full SyncService.reconcile()
    • Pure reorder  → SyncService.sync_order_from_native() + rebuild_ui()
      (e.g. after object.vertex_group_sort or object.vertex_group_mirror)

    Operators that call reconcile / sync_order_from_native directly already
    update _vg_state_cache, so this handler is effectively a no-op for them.
    """
    for update in depsgraph.updates:
        if not isinstance(update.id, bpy.types.Object):
            continue
        try:
            obj = update.id
        except Exception:
            continue

        # Skip objects with no VGF data
        if not hasattr(obj, 'zls_vgf_nodes') or not obj.zls_vgf_nodes:
            continue

        current = _vgf_state_snapshot(obj)
        cached  = _vg_state_cache.get(obj.name)

        if cached == current:
            continue  # nothing changed — fast exit

        # Distinguish reorder from add/remove
        if cached is not None and frozenset(cached) == frozenset(current):
            # Same names, different order → sync order only
            SyncService.sync_order_from_native(obj)
            SyncService.rebuild_ui(obj)
            _vg_state_cache[obj.name] = current
        else:
            # Groups added or removed → full reconcile
            # New groups land at ROOT so the user can move them manually.
            SyncService.reconcile(obj)


@bpy.app.handlers.persistent
def _vgf_on_load_post(*_args):
    """Clear the state cache after a file load so fresh snapshots are taken."""
    _vg_state_cache.clear()


# =========================================================
# 6. CONTROLLER / COMMANDS
# =========================================================
class CommandController:
    """Business-logic layer — translates UI intents into data mutations."""

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _get_active_uid(obj):
        rows = getattr(obj, 'zls_vgf_ui_rows', None)
        if rows and 0 <= obj.zls_vgf_active < len(rows):
            return rows[obj.zls_vgf_active].uid
        return None

    @staticmethod
    def _get_active_target_folder(obj):
        rows = getattr(obj, 'zls_vgf_ui_rows', None)
        if rows and 0 <= obj.zls_vgf_active < len(rows):
            uid  = rows[obj.zls_vgf_active].uid
            node = CommandController.get_node(obj, uid)
            if node:
                return node.uid if node.node_type == ITEM_FOLDER else node.parent_uid
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

    # ── commands ─────────────────────────────────────────────────────────

    @staticmethod
    def execute_add_folder(obj):
        target_uid      = CommandController._get_active_target_folder(obj)
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
        target_uid = CommandController._get_active_target_folder(obj)
        NativeAdapter.add_group(obj)
        SyncService.reconcile(obj, target_parent_uid=target_uid)

    @staticmethod
    def execute_duplicate_group(obj):
        """
        Duplicate the active vertex group via pure Python API and
        register the copy inside the same folder as the original.
        """
        target_uid = CommandController._get_active_target_folder(obj)
        new_vg     = NativeAdapter.duplicate_group(obj)
        if new_vg is None:
            return
        # reconcile picks up the new VG and places it in target_uid
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

        if node.node_type == ITEM_GROUP:
            vg_idx = NativeAdapter.get_groups(obj).find(node.name)
            if vg_idx != -1:
                NativeAdapter.set_active_index(obj, vg_idx)
                NativeAdapter.remove_active_group(obj)

        elif node.node_type == ITEM_FOLDER:
            uids_to_del: set[str] = set()

            if node.is_expanded:
                # Expanded folder: promote children to parent, delete only the folder node
                for child in [n for n in obj.zls_vgf_nodes if n.parent_uid == node.uid]:
                    child.parent_uid = node.parent_uid
                    child.sort_key   = 999_999
                uids_to_del.add(node.uid)
            else:
                # Collapsed folder: delete folder and ALL descendants recursively
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

        # Guard against circular folder references
        if node.node_type == ITEM_FOLDER and new_parent_uid != ROOT_UID:
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


# Global list to prevent GC of the enum items tuple
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
        items=folder_enum_generator,
        get=folder_dropdown_get, set=folder_dropdown_set,
        name="", description=DESC_MOVE_TO_FOLDER,
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
    bl_options  = {'UNDO'}

    def execute(self, context):
        CommandController.execute_add_folder(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_add_group(bpy.types.Operator):
    bl_idname   = "zls_vgf.add_group"
    bl_label    = "Add Vertex Group"
    bl_description = DESC_ADD_GROUP
    bl_options  = {'UNDO'}

    def execute(self, context):
        CommandController.execute_add_group(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_remove_item(bpy.types.Operator):
    bl_idname   = "zls_vgf.remove_item"
    bl_label    = "Remove Item"
    bl_description = DESC_REMOVE_ITEM
    bl_options  = {'UNDO'}

    def execute(self, context):
        CommandController.execute_remove_active(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_move_item_up(bpy.types.Operator):
    bl_idname   = "zls_vgf.move_item_up"
    bl_label    = "Move Item Up"
    bl_description = DESC_MOVE_UP
    bl_options  = {'UNDO'}

    def execute(self, context):
        CommandController.execute_move(context.object, 'UP')
        return {'FINISHED'}


class ZLSVGF_OT_move_item_down(bpy.types.Operator):
    bl_idname   = "zls_vgf.move_item_down"
    bl_label    = "Move Item Down"
    bl_description = DESC_MOVE_DOWN
    bl_options  = {'UNDO'}

    def execute(self, context):
        CommandController.execute_move(context.object, 'DOWN')
        return {'FINISHED'}


class ZLSVGF_OT_toggle_folder(bpy.types.Operator):
    bl_idname   = "zls_vgf.toggle_folder"
    bl_label    = "Toggle Folder"
    bl_description = DESC_TOGGLE_FOLDER
    bl_options  = {'UNDO'}

    uid: bpy.props.StringProperty()

    def execute(self, context):
        node = CommandController.get_node(context.object, self.uid)
        if node:
            node.is_expanded = not node.is_expanded
            SyncService.rebuild_ui(context.object)
        return {'FINISHED'}


class ZLSVGF_OT_sync(bpy.types.Operator):
    bl_idname   = "zls_vgf.sync"
    bl_label    = "Sync Vertex Groups"
    bl_description = DESC_SYNC
    bl_options  = {'UNDO'}

    def execute(self, context):
        obj = context.object
        SyncService.reconcile(obj)
        self.report({'INFO'}, UI_STR_SYNC_SUCCESS)
        return {'FINISHED'}


class ZLSVGF_OT_duplicate_group(bpy.types.Operator):
    """
    Duplicate active vertex group via pure Python — places the copy
    in the same VGF folder as the original.
    """
    bl_idname   = "zls_vgf.duplicate_group"
    bl_label    = "Duplicate Vertex Group"
    bl_description = DESC_DUPLICATE
    bl_options  = {'UNDO'}

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
    bl_options  = {'UNDO'}

    def execute(self, context):
        obj = context.object
        NativeAdapter.remove_all_groups(obj)

        # Remove every GROUP node from the folder tree
        for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
            if obj.zls_vgf_nodes[i].node_type == ITEM_GROUP:
                obj.zls_vgf_nodes.remove(i)

        # Update cache before depsgraph fires
        _vg_state_cache[obj.name] = _vgf_state_snapshot(obj)

        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        return {'FINISHED'}


class ZLSVGF_OT_delete_unlocked_groups(bpy.types.Operator):
    bl_idname   = "zls_vgf.delete_unlocked_groups"
    bl_label    = "Delete All Unlocked Groups"
    bl_description = DESC_DEL_UNLOCKED
    bl_options  = {'UNDO'}

    def execute(self, context):
        obj = context.object

        # Collect names BEFORE removal so we can clean up the node tree
        unlocked = {vg.name for vg in obj.vertex_groups if not vg.lock_weight}
        NativeAdapter.remove_unlocked_groups(obj)

        for i in range(len(obj.zls_vgf_nodes) - 1, -1, -1):
            n = obj.zls_vgf_nodes[i]
            if n.node_type == ITEM_GROUP and n.name in unlocked:
                obj.zls_vgf_nodes.remove(i)

        # Update cache before depsgraph fires
        _vg_state_cache[obj.name] = _vgf_state_snapshot(obj)

        SyncService.normalize_sort(obj)
        SyncService.rebuild_ui(obj)
        return {'FINISHED'}


class ZLSVGF_OT_copy_to_selected(bpy.types.Operator):
    """
    Copy vertex groups (with weights) from the active object to all
    other selected mesh objects. Pure-Python — no bpy.ops dependency.
    """
    bl_idname   = "zls_vgf.copy_to_selected"
    bl_label    = "Copy Vertex Groups to Selected"
    bl_description = DESC_COPY_SEL
    bl_options  = {'UNDO'}

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

        # ── Sort — native operators, no wrapper needed ────────────────────
        # The depsgraph handler auto-syncs the folder order after these.
        layout.operator(
            "object.vertex_group_sort",
            text="Sort by Name", icon='SORTALPHA',
        ).sort_type = 'NAME'
        layout.operator(
            "object.vertex_group_sort",
            text="Sort by Bone Hierarchy",
        ).sort_type = 'BONE_HIERARCHY'
        layout.separator()

        # ── Duplicate — custom (places copy in the same folder) ───────────
        layout.operator("zls_vgf.duplicate_group", text="Duplicate Vertex Group")
        layout.operator("zls_vgf.copy_to_selected")

        # ── Mirror — native operators; depsgraph picks up the new group ───
        layout.operator("object.vertex_group_mirror", text="Mirror Vertex Group")
        layout.operator(
            "object.vertex_group_mirror",
            text="Mirror Vertex Group (Topology)",
        ).use_topology = True
        layout.separator()

        # ── Weight assignment — native operators, no sync needed ──────────
        layout.operator(
            "object.vertex_group_remove_from",
            text="Remove from All Groups",
        ).use_all_groups = True
        layout.operator(
            "object.vertex_group_remove_from",
            text="Clear Active Group",
        ).use_all_verts = True
        layout.separator()

        # ── Delete — custom (cleans up folder nodes) ──────────────────────
        layout.operator("zls_vgf.delete_unlocked_groups", text="Delete All Unlocked Groups")
        layout.operator("zls_vgf.delete_all_groups",      text="Delete All Groups")
        layout.separator()

        # ── Lock — native operators, no sync needed ───────────────────────
        layout.operator("object.vertex_group_lock", text="Lock All").action   = 'LOCK'
        layout.operator("object.vertex_group_lock", text="Unlock All").action = 'UNLOCK'
        layout.operator("object.vertex_group_lock", text="Lock Invert All").action = 'INVERT'


class ZLSVGF_UL_items(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj  = data
        node = CommandController.get_node(obj, item.uid)
        if not node:
            return

        row = layout.row(align=True)
        for _ in range(item.depth):
            row.label(text="", icon='BLANK1')

        row = row.row(align=True)

        if node.node_type == ITEM_FOLDER:
            op      = row.operator(
                "zls_vgf.toggle_folder", text="", emboss=False,
                icon='TRIA_DOWN' if node.is_expanded else 'TRIA_RIGHT',
            )
            op.uid  = node.uid
            row.prop(node, "folder_name", text="", icon='FILE_FOLDER', emboss=False)

            rr = row.row(align=True)
            rr.alignment = 'RIGHT'
            rr.label(text="", icon='BLANK1')
            rr.prop(node, "ui_parent_dropdown", text="", icon='ARROW_LEFTRIGHT', emboss=False)

        else:
            row.label(text="", icon='GROUP_VERTEX')
            vg = NativeAdapter.get_groups(obj).get(node.name)

            if vg:
                row.prop(node, "group_name", text="", emboss=False)
                rr = row.row(align=True)
                rr.alignment = 'RIGHT'
                rr.prop(
                    vg, "lock_weight", text="",
                    icon='LOCKED' if vg.lock_weight else 'UNLOCKED',
                    emboss=False,
                )
                rr.prop(node, "ui_parent_dropdown", text="", icon='ARROW_LEFTRIGHT', emboss=False)
            else:
                row.label(text=UI_STR_PENDING, icon='ERROR')


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
        # Sync is a fallback icon-button, less prominent than a full-width button
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
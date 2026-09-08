import atexit
import os
import time

import bpy

ADDON_VERSION = "0.1.0"

IDLE_THRESHOLD = 60.0    # seconds with no mouse click before the timer pauses
TICK_INTERVAL = 1.0      # seconds between accounting ticks
SAVE_INTERVAL = 60.0     # seconds between autosaves to the sidecar file

# Module-level state - not stored as bpy properties since it needs to keep
# ticking via a modal operator/timer regardless of which object/scene is active.
_state = {
    "total_seconds": 0.0,
    "last_tick": 0.0,
    "last_click": 0.0,
    "last_save": 0.0,
    "paused": True,
    "running": False,
}


def get_sidecar_path(filepath):
    base, _ext = os.path.splitext(filepath)
    return base + "_time.txt"


def format_hms(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s)


def load_saved_total(filepath):
    path = get_sidecar_path(filepath)
    if not os.path.isfile(path):
        return 0.0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("SECONDS="):
                    return float(line.strip().split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return 0.0


def save_total(filepath, total_seconds):
    if not filepath:
        return
    path = get_sidecar_path(filepath)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("SECONDS=%.1f\n" % total_seconds)
            f.write("Time spent on %s: %s\n" % (os.path.basename(filepath), format_hms(total_seconds)))
            f.write("Last updated: %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except OSError:
        pass


def flush_current():
    filepath = bpy.data.filepath
    if filepath:
        save_total(filepath, _state["total_seconds"])


def reset_state_for_current_file():
    filepath = bpy.data.filepath
    now = time.time()
    _state["total_seconds"] = load_saved_total(filepath) if filepath else 0.0
    _state["last_tick"] = now
    _state["last_click"] = now
    _state["last_save"] = now
    _state["paused"] = False


def tick():
    now = time.time()
    dt = now - _state["last_tick"]
    _state["last_tick"] = now

    idle = now - _state["last_click"]
    _state["paused"] = idle > IDLE_THRESHOLD

    if not _state["paused"]:
        _state["total_seconds"] += dt

    if now - _state["last_save"] >= SAVE_INTERVAL:
        _state["last_save"] = now
        flush_current()


def register_click():
    _state["last_click"] = time.time()


class BBPT_OT_modal_timer(bpy.types.Operator):
    """Runs for the lifetime of the Blender session, tracking mouse clicks
    and accumulating active time. Reinstated automatically after every file
    load - never exposed as a manual start/stop control."""
    bl_idname = "bb_project_timer.modal_timer"
    bl_label = "BB Project Timer (internal)"
    bl_options = {'INTERNAL'}

    _wm_timer = None

    def modal(self, context, event):
        if not _state["running"]:
            return {'FINISHED'}

        if event.type == 'TIMER':
            tick()
            _redraw_timer_panels(context)
        elif event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'} and event.value == 'PRESS':
            register_click()

        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        self._wm_timer = context.window_manager.event_timer_add(TICK_INTERVAL, window=context.window)
        context.window_manager.modal_handler_add(self)
        _state["running"] = True
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        if self._wm_timer is not None:
            context.window_manager.event_timer_remove(self._wm_timer)
            self._wm_timer = None
        _state["running"] = False


def _redraw_timer_panels(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def ensure_modal_running():
    if _state["running"]:
        return
    try:
        bpy.ops.bb_project_timer.modal_timer('INVOKE_DEFAULT')
    except RuntimeError:
        pass


def _start_modal_deferred():
    reset_state_for_current_file()
    ensure_modal_running()
    return None  # one-shot


def on_load_pre(dummy1, dummy2):
    flush_current()


def on_load_post(dummy1, dummy2):
    # bpy.ops is not always safe to call directly from a handler, so defer by one tick.
    bpy.app.timers.register(_start_modal_deferred, first_interval=0.0)


class BBPT_PT_panel(bpy.types.Panel):
    bl_label = "Project Timer"
    bl_idname = "BBPT_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tool"

    def draw(self, context):
        layout = self.layout
        filepath = bpy.data.filepath

        if not filepath:
            layout.label(text="Save the file to start tracking", icon='ERROR')
            return

        status = "Paused (idle)" if _state["paused"] else "Tracking"
        icon = 'PAUSE' if _state["paused"] else 'REC'
        layout.label(text=status, icon=icon)

        col = layout.column()
        col.label(text="Time on this file:")
        col.label(text=format_hms(_state["total_seconds"]))

        layout.separator()
        layout.label(text=os.path.basename(get_sidecar_path(filepath)), icon='FILE_TEXT')


classes = (
    BBPT_OT_modal_timer,
    BBPT_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.app.handlers.load_pre.append(on_load_pre)
    bpy.app.handlers.load_post.append(on_load_post)
    atexit.register(flush_current)

    # Blender may already have a file loaded when the add-on is enabled.
    bpy.app.timers.register(_start_modal_deferred, first_interval=0.0)


def unregister():
    _state["running"] = False
    flush_current()

    try:
        atexit.unregister(flush_current)
    except ValueError:
        pass

    if on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(on_load_pre)
    if on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

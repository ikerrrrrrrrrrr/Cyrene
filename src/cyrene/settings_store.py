"""Runtime settings store — persists user preferences that can be changed via Web UI.

Delegates to the encrypted config_store for all read/write operations.
"""

from cyrene import config_store as _store

# Re-export for callers that import directly from settings_store
get = _store.get_setting
set_ = _store.set_setting
get_all = _store.get_all_settings
reset_all = _store.reset_all
get_spawn_policy = _store.get_spawn_policy
get_models = _store.get_models
save_models = _store.save_models
get_vision_models = _store.get_vision_models
save_vision_models = _store.save_vision_models
get_secondary_model = _store.get_secondary_model
save_secondary_model = _store.save_secondary_model
is_tool_enabled = _store.is_tool_enabled
get_enabled_tools = _store.get_enabled_tools
save_enabled_tools = _store.save_enabled_tools
get_workspace_history = _store.get_workspace_history
add_workspace_to_history = _store.add_workspace_to_history
is_workspace_active = _store.is_workspace_active
set_workspace_active = _store.set_workspace_active
get_write_permission_mode = _store.get_write_permission_mode
set_write_permission_mode = _store.set_write_permission_mode
is_soul_active = _store.is_soul_active
set_soul_active = _store.set_soul_active

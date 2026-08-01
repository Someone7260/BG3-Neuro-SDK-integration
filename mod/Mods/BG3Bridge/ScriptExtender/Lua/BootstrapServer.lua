-- =============================================================================
-- BG3Bridge :: BootstrapServer.lua  v0.4.0
-- Server-side entry point for the BG3SE Bridge.
--
-- OUTPUT FILE (SE sandboxed path):
--   %LocalAppData%\Larian Studios\Baldur's Gate 3\Script Extender\bg3_state.json
-- =============================================================================

local MOD_TAG     = "[BG3Bridge]"
local MOD_VERSION = "v0.4.0"
local OUTPUT_FILE = "bg3_state.json"

-- ---------------------------------------------------------------------------
-- Boot banner  (first thing printed â€” confirms the file loaded at all)
-- ---------------------------------------------------------------------------
Ext.Utils.Print(MOD_TAG .. " ============================================")
Ext.Utils.Print(MOD_TAG .. " Bridge loading  " .. MOD_VERSION .. "  @ " .. tostring(Ext.Utils.MonotonicTime and Ext.Utils.MonotonicTime() or "?") .. " ms")
Ext.Utils.Print(MOD_TAG .. " Output: " .. OUTPUT_FILE)
Ext.Utils.Print(MOD_TAG .. " ============================================")

-- ---------------------------------------------------------------------------
-- Global Persistent State
-- ---------------------------------------------------------------------------
local BG3State = {
    sequence_id          = 0,
    last_event           = "None",
    last_event_timestamp = 0,
    dialog_active        = false,
    dialog_name          = "",
    dialog_instanceID    = "",
    dialog_actors        = {},
    combat_active        = false,
    combat_guid          = ""
}

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

local function safe_str(v)
    if v == nil then return "nil" end
    return tostring(v)
end

local function get_actor_display_name(actor)
    if not actor or actor == "" then return "Unknown" end
    local ok, name = pcall(function()
        local entity = Ext.Entity.Get(actor)
        if entity and entity.DisplayName then
            -- DisplayNameComponent.Name (current API); fall back to NameKey for older SE builds
            local raw = entity.DisplayName.Name or (entity.DisplayName.NameKey and entity.DisplayName.NameKey.Handle)
            if raw then
                local handle = (type(raw) == "userdata" and raw.Handle) or raw
                local str = Ext.Loca.GetTranslatedString(handle)
                if str and str ~= "" then return str end
            end
        end
        return nil
    end)
    if ok and name then return name end
    return safe_str(actor)
end

local function collect_actors(instanceID)
    local actors = {}
    local ok, rows = pcall(function()
        return Osi.DB_Dialogs:Get(nil, instanceID, nil, nil)
    end)
    if ok and rows then
        for _, row in ipairs(rows) do
            local actor = row[3]
            if actor and actor ~= "" then
                table.insert(actors, get_actor_display_name(actor))
            end
        end
    end
    return actors
end

local in_combat_party = {}

local function is_party_member(actor)
    local ok, rows = pcall(function() return Osi.DB_PartyMembers:Get(nil) end)
    if ok and rows then
        local act_str = safe_str(actor)
        for _, row in ipairs(rows) do
            if safe_str(row[1]) == act_str then return true end
        end
    end
    return false
end

-- ---------------------------------------------------------------------------
-- State flush  (console + disk, never leaks into the data payload)
-- ---------------------------------------------------------------------------

local function flush_state(event_name)
    BG3State.sequence_id          = BG3State.sequence_id + 1
    BG3State.last_event           = event_name
    BG3State.last_event_timestamp = (Ext.Utils.MonotonicTime and Ext.Utils.MonotonicTime()) or 0

    local ok_json, json_str = pcall(Ext.Json.Stringify, BG3State, { Beautify = true })
    if not ok_json then
        json_str = '{"error":"json_stringify_failed"}'
        Ext.Utils.PrintError(MOD_TAG .. " [FLUSH] JSON stringify failed!")
    end

    local byte_count = #json_str
    local ok_save, save_err = pcall(Ext.IO.SaveFile, OUTPUT_FILE, json_str)
    if ok_save then
        -- Ext.Utils.Print(MOD_TAG .. " [FLUSH] OK  event=" .. event_name .. "  seq=" .. tostring(BG3State.sequence_id) .. "  bytes=" .. tostring(byte_count))
    else
        Ext.Utils.PrintError(MOD_TAG .. " [FLUSH] FAILED  event=" .. event_name
            .. "  err=" .. safe_str(save_err))
    end
end

-- ---------------------------------------------------------------------------
-- Event Handlers
-- ---------------------------------------------------------------------------

local function on_dialog_started(dialog, instanceID)
    BG3State.dialog_active     = true
    BG3State.dialog_name       = safe_str(dialog)
    BG3State.dialog_instanceID = safe_str(instanceID)
    BG3State.dialog_actors     = collect_actors(instanceID)
    Ext.Utils.Print(MOD_TAG .. " [EVENT] DialogStarted: dialog=" .. safe_str(dialog))
    flush_state("DialogStarted")
    
    -- Notify client to start lightweight UI polling
    pcall(function() Ext.Net.BroadcastMessage("BG3_DialogStatus", "started") end)
end

local function on_dialog_actor_joined(dialog, instanceID, actor, speakerIndex)
    if BG3State.dialog_instanceID == safe_str(instanceID) then
        local act_str = get_actor_display_name(actor)
        local found   = false
        for _, v in ipairs(BG3State.dialog_actors) do
            if v == act_str then found = true; break end
        end
        if not found then table.insert(BG3State.dialog_actors, act_str) end
    end
    -- Ext.Utils.Print(MOD_TAG .. " [EVENT] DialogActorJoined: actor=" .. get_actor_display_name(actor))
    flush_state("DialogActorJoined")
end

local function on_dialog_ended(dialog, instanceID)
    BG3State.dialog_active     = false
    BG3State.dialog_name       = ""
    BG3State.dialog_instanceID = ""
    BG3State.dialog_actors     = {}
    Ext.Utils.Print(MOD_TAG .. " [EVENT] DialogEnded: dialog=" .. safe_str(dialog))
    flush_state("DialogEnded")

    -- Notify client to stop UI polling
    pcall(function() Ext.Net.BroadcastMessage("BG3_DialogStatus", "ended") end)
end

local function on_entered_combat(object, combatGuid)
    if Osi.IsCharacter(object) ~= 1 then return end
    if not is_party_member(object) then return end
    local act_str = safe_str(object)
    in_combat_party[act_str]  = true
    BG3State.combat_active = true
    BG3State.combat_guid   = safe_str(combatGuid)
    Ext.Utils.Print(MOD_TAG .. " [EVENT] EnteredCombat: actor=" .. get_actor_display_name(object))
    flush_state("EnteredCombat")
end

local function on_left_combat(object, combatGuid)
    if Osi.IsCharacter(object) ~= 1 then return end
    if not is_party_member(object) then return end
    local act_str = safe_str(object)
    in_combat_party[act_str] = nil
    local still_in = false
    for _, v in pairs(in_combat_party) do if v then still_in = true; break end end
    if not still_in then
        BG3State.combat_active = false
        BG3State.combat_guid   = ""
    end
    Ext.Utils.Print(MOD_TAG .. " [EVENT] LeftCombat: actor=" .. get_actor_display_name(object))
    flush_state("LeftCombat")
end

-- ---------------------------------------------------------------------------
-- Register listeners  (each wrapped individually so one failure can't block others)
-- ---------------------------------------------------------------------------

local function safe_register(event, arity, handler, label)
    local ok, err = pcall(function()
        Ext.Osiris.RegisterListener(event, arity, "after", handler)
    end)
    if ok then
        Ext.Utils.Print(MOD_TAG .. " [INIT] Registered: " .. label)
    else
        Ext.Utils.PrintError(MOD_TAG .. " [INIT] FAILED to register " .. label .. "  err=" .. safe_str(err))
    end
end

safe_register("DialogStarted",    2, on_dialog_started,      "DialogStarted/2")
safe_register("DialogActorJoined",4, on_dialog_actor_joined, "DialogActorJoined/4")
safe_register("DialogEnded",      2, on_dialog_ended,        "DialogEnded/2")
safe_register("EnteredCombat",    2, on_entered_combat,      "EnteredCombat/2")
safe_register("LeftCombat",       2, on_left_combat,         "LeftCombat/2")

-- ---------------------------------------------------------------------------
-- Heartbeat  (every ~60 s of game-time ticks; confirms the mod is still alive)
-- ---------------------------------------------------------------------------

local _heartbeat_last = (Ext.Utils.MonotonicTime and Ext.Utils.MonotonicTime()) or 0
local HEARTBEAT_MS = 60000

Ext.Events.Tick:Subscribe(function()
    local now = (Ext.Utils.MonotonicTime and Ext.Utils.MonotonicTime()) or 0
    if (now - _heartbeat_last) >= HEARTBEAT_MS then
        _heartbeat_last = now
        Ext.Utils.Print(MOD_TAG .. " [HEARTBEAT] alive  seq=" .. tostring(BG3State.sequence_id)
            .. "  dialog=" .. tostring(BG3State.dialog_active)
            .. "  combat=" .. tostring(BG3State.combat_active))
    end
end)

-- ---------------------------------------------------------------------------
-- Done
-- ---------------------------------------------------------------------------
Ext.Utils.Print(MOD_TAG .. " Bridge ready. All listeners registered.")

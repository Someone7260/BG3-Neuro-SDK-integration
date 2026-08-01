-- =============================================================================
-- BG3Bridge :: BootstrapClient.lua  v0.6.0
-- Client-side polling (Currently Disabled - UI scraping blocked by NoesisGUI)
-- =============================================================================

local MOD_TAG = "[BG3Bridge]"

Ext.Utils.Print(MOD_TAG .. " [CLIENT] Bootstrap loaded. UI Scraping is intentionally disabled.")

-- Network listener left intact for future client-side signaling if needed.
Ext.RegisterNetListener("BG3_DialogStatus", function(channel, payload)
    if payload == "started" then
        Ext.Utils.Print(MOD_TAG .. " [CLIENT] Notified: Dialog STARTED.")
    else
        Ext.Utils.Print(MOD_TAG .. " [CLIENT] Notified: Dialog ENDED.")
    end
end)

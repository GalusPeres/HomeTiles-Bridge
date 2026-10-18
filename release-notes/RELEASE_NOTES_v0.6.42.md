# HomeTiles Bridge v0.6.42

Existing Switch tiles can now control automation, input_boolean, fan, humidifier,
remote and siren entities alongside switches and lights. Automations are
enabled/disabled; Fan and Siren require both Home Assistant on/off capabilities.
The existing Scene tile can press button and input_button entities alongside
scene and script activation.

Commands use each domain's fixed Home Assistant action, validate configured
targets and availability, and ignore retained messages. Never-pressed buttons
with an unknown timestamp remain usable. Scene/action aliases survive reordered
selections and colliding object names. Removed switch entities replace stale
retained state during startup and live removal.

Existing configurations, MQTT topics, ordinary switch state payloads and
scene/script aliases remain compatible. Updated firmware provides complete
availability handling in the existing Switch tile and popup, plus translated
editor labels. See the README compatibility table for exact behavior and limits.

Regression coverage includes service dispatch, feature combinations, alias
persistence, missing/unavailable entities, startup snapshots, live removal and
retained command rejection. End-to-end behavior still needs hardware/HA testing.

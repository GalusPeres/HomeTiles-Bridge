# Domain constants shared across integration modules.

DOMAIN = "tab5_lvgl"

CONF_BASE_TOPIC = "base_topic"
CONF_HA_PREFIX = "ha_prefix"
CONF_SCENE_MAP = "scene_map"
CONF_SCENE_MAP_TEXT = "scene_map_text"
CONF_SCENE_ENTITIES = "scene_entities"
CONF_DEVICE_ID = "device_id"
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL = "model"
CONF_DEVICE_NAME = "device_name"
CONF_LOCAL_IO = "local_io"

CONF_ENERGY_ELECTRICITY = "energy_electricity"
CONF_ENERGY_GAS = "energy_gas"
CONF_ENERGY_WATER = "energy_water"

CONF_SENSORS = "sensors"
CONF_BINARY_SENSORS = "binary_sensors"
CONF_WEATHERS = "weathers"
CONF_LIGHTS = "lights"
CONF_SWITCHES = "switches"
CONF_MEDIA_PLAYERS = "media_players"
CONF_CLIMATES = "climates"
CONF_COVERS = "covers"
CONF_CAMERAS = "cameras"

DEFAULT_BASE = "hometiles"
DEFAULT_PREFIX = "ha/statestream"
SERVICE_PUBLISH_SNAPSHOT = "publish_snapshot"

TOPIC_DISPLAY_BRIGHTNESS = "display_brightness"
TOPIC_SCREENSAVER_BRIGHTNESS = "screensaver_brightness"
TOPIC_DISPLAY_ROTATE = "display_rotate"
TOPIC_DISPLAY_SLEEP = "display_sleep"
TOPIC_SLEEP_MAINS = "sleep_mains"
TOPIC_SLEEP_BATTERY = "sleep_battery"
TOPIC_SENSOR_SOC = "soc_pct"

SLEEP_OPTIONS = ["5 s", "15 s", "30 s", "60 s", "5 min", "15 min", "30 min", "60 min", "Nie"]

CONFIG_TOPIC_ROOT = "tab5_lvgl/config"
CONFIG_TOPIC_SUB = f"{CONFIG_TOPIC_ROOT}/+/bridge"
HISTORY_REQUEST_SUFFIX = "history/request"
HISTORY_RESPONSE_SUFFIX = "history/response"
ENERGY_REQUEST_SUFFIX = "energy/request"
ENERGY_RESPONSE_SUFFIX = "energy/response"
WEATHER_REQUEST_SUFFIX = "weather/request"

CONF_NUMBERS = "numbers"
CONF_SELECTS = "selects"
CONF_DATETIMES = "datetimes"

# On-demand still images from a panel's own camera (not CONF_CAMERAS, which
# lists Home Assistant cameras shown on the panel).
TOPIC_LOCAL_CAMERA = "local_camera"
# Below Home Assistant's CAMERA_IMAGE_TIMEOUT (10 s) so a cached frame can
# still be returned before the frontend request is abandoned.
LOCAL_CAMERA_REQUEST_TIMEOUT_S = 6.0
LOCAL_CAMERA_MIN_AGE_S = 1.5
LOCAL_CAMERA_MAX_BYTES = 256 * 1024
LOCAL_CAMERA_FRAME_INTERVAL_S = 2.0
LOCAL_CAMERA_STALE_FALLBACK_S = 60.0
LOCAL_CAMERA_WARNING_INTERVAL_S = 60.0

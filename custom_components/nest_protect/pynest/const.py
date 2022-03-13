"""Constants used by PyNest."""

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.120 Safari/537.36"

# / URL for refresh token generation
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Client ID of the Nest iOS application
CLIENT_ID = "733249279899-1gpkq9duqmdp55a7e5lft1pr2smumdla.apps.googleusercontent.com"

# App launch API endpoint
APP_LAUNCH_URL_FORMAT = "https://home.nest.com/api/0.1/user/{user_id}/app_launch"

NEST_AUTH_URL_JWT = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"

# General Nest information: "structure"
# Thermostats: "device", "shared",
# Protect: "topaz"
# Temperature sensors: "kryptonite"

BUCKET_TYPES = [
    "structure",
    # Protect
    "topaz",
    # Areas
    "where",
]

KNOWN_BUCKET_TYPES = [
    "buckets",
    "structure",
    "shared",
    "topaz",
    "device",
    "rcs_settings",
    "kryptonite",
    "quartz",
    "track",
    "where",
]

KNOWN_BUCKET_VERSION = []

NEST_REQUEST = {
    "known_bucket_types": BUCKET_TYPES,
    "known_bucket_versions": KNOWN_BUCKET_VERSION,
}

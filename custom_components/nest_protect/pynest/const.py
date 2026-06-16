"""Constants used by PyNest."""

from .enums import BucketType, Environment
from .models import NestEnvironment

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.120 Safari/537.36"

NEST_ENVIRONMENTS: dict[str, NestEnvironment] = {
    Environment.PRODUCTION: NestEnvironment(
        name="Google Account",
        client_id="733249279899-1gpkq9duqmdp55a7e5lft1pr2smumdla.apps.googleusercontent.com",  # Nest iOS application
        host="https://home.nest.com",
    ),
    Environment.FIELDTEST: NestEnvironment(
        name="Google Account (Field Test)",
        client_id="384529615266-57v6vaptkmhm64n9hn5dcmkr4at14p8j.apps.googleusercontent.com",  # Test Flight Beta Nest iOS application
        host="https://home.ft.nest.com",
    ),
}

DEFAULT_NEST_ENVIRONMENT = NEST_ENVIRONMENTS[Environment.PRODUCTION]

# / URL for refresh token generation
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Android "master token" flow. This reproduces how the Nest/Google mobile apps stay
# logged in: a master token (aas_et/...) that never expires unless the password is
# changed or access is revoked. From it we mint short-lived nest-account access
# tokens. The app package + signing cert are the Google Home app's (the same ones
# used by glocaltokens); only the requested service/scope is Nest-specific.
EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"
GOOGLE_HOME_APP = "com.google.android.apps.chromecast.app"
GOOGLE_OAUTH_CLIENT_SIG = "24bb24c05e47e0aefa68a58a766179d9b613a600"
NEST_ACCOUNT_OAUTH_SERVICE = "oauth2:https://www.googleapis.com/auth/nest-account"

# App launch API endpoint
APP_LAUNCH_URL_FORMAT = "{host}/api/0.1/user/{user_id}/app_launch"
NEST_AUTH_URL_JWT = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"

NEST_REQUEST = {
    "known_bucket_types": [
        BucketType.KRYPTONITE,
        BucketType.STRUCTURE,
        BucketType.TOPAZ,
        BucketType.WHERE,
        BucketType.USER,
    ],
    "known_bucket_versions": [],
}

FULL_NEST_REQUEST = {
    "known_bucket_types": [
        BucketType.BUCKETS,
        BucketType.METADATA,
        BucketType.KRYPTONITE,
        BucketType.STRUCTURE,
        BucketType.TOPAZ,
        BucketType.WHERE,
        BucketType.USER,
        BucketType.DEMAND_RESPONSE,
        BucketType.WIDGET_TRACK,
        BucketType.OCCUPANCY,
        BucketType.MESSAGE,
        BucketType.MESSAGE_CENTER,
        BucketType.LINK,
        BucketType.SAFETY,
        BucketType.SAFETY_SUMMARY,
        BucketType.DEVICE_ALERT_DIALOG,
        BucketType.QUARTZ,
        BucketType.TOPAZ_RESOURCE,
        BucketType.TRACK,
        BucketType.TRIP,
        BucketType.STRUCTURE_METADATA,
        BucketType.USER,
        BucketType.WIDGET_TRACK,
    ],
    "known_bucket_versions": [],
}

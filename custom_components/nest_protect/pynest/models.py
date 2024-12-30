"""Models used by PyNest."""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
from typing import Any

from .enums import BucketType


@dataclass
class NestLimits:
    """Nest Limits."""

    thermostats_per_structure: int
    structures: int
    smoke_detectors_per_structure: int
    smoke_detectors: int
    thermostats: int


@dataclass
class NestUrls:
    """Nest Urls."""

    rubyapi_url: str
    czfe_url: str
    log_upload_url: str
    transport_url: str
    weather_url: str
    support_url: str
    direct_transport_url: str


@dataclass
class NestResponse:
    """Class that reflects a Nest API response."""

    access_token: float
    email: str
    expires_in: str
    userid: str
    is_superuser: bool
    language: str
    weave: dict[str, str]
    user: str
    is_staff: bool
    error: dict | None = None
    urls: NestUrls = field(default_factory=NestUrls)
    limits: NestLimits = field(default_factory=NestLimits)

    _2fa_state: str = None
    _2fa_enabled: bool = None
    _2fa_state_changed: str = None

    def is_expired(self):
        """Check if session is expired."""
        # Tue, 01-Mar-2022 23:15:55 GMT
        expiry_date = datetime.datetime.strptime(
            self.expires_in, "%a, %d-%b-%Y %H:%M:%S %Z"
        )

        if expiry_date <= datetime.datetime.now():
            return True

        return False


@dataclass
class Bucket:
    """Class that reflects a Nest API response."""

    object_key: str
    object_revision: int
    object_timestamp: int
    # value: dict[str, Any]
    value: dict[str, Any] | TopazBucketValue | WhereBucketValue
    type: str = ""

    def __post_init__(self):
        """Set the bucket type during post init."""
        self.type = BucketType(self.object_key.split(".")[0])

        # if self.type == BucketType.TOPAZ:
        #     self.value = TopazBucketValue(**self.value)
        if self.type == BucketType.WHERE:
            if isinstance(self.value, WhereBucketValue):
                # It's already the correct type, no need to reinitialize
                pass
            else:
                # Convert dictionary to WhereBucketValue instance
                self.value = WhereBucketValue(**self.value)


@dataclass
class Where:
    """TODO."""

    name: str
    where_id: str


@dataclass
class BucketValue:
    """Nest Protect values."""

    # def __iter__(self):
    #     return (getattr(self, field.name) for field in fields(self))


@dataclass
class WhereBucketValue(BucketValue):
    """Nest Protect values."""

    wheres: list[Where] = field(default_factory=Where)

    def __post_init__(self):
        """TODO."""
        self.wheres = [Where(**w) for w in self.wheres] if self.wheres else []


@dataclass
class WhereBucket(Bucket):
    """Class that reflects a Nest API response."""

    object_key: str
    object_revision: str
    object_timestamp: str
    value: WhereBucketValue = field(default_factory=WhereBucketValue)


@dataclass
class TopazBucketValue(BucketValue):
    """Nest Protect values."""

    spoken_where_id: str
    creation_time: int
    installed_locale: str
    ntp_green_led_brightness: int
    component_buzzer_test_passed: bool
    wifi_ip_address: str
    wired_led_enable: bool
    wifi_regulatory_domain: str
    co_blame_duration: int
    is_rcs_capable: bool
    fabric_id: str
    battery_health_state: int
    steam_detection_enable: bool
    hushed_state: bool
    capability_level: float
    home_alarm_link_type: int
    model: str
    component_smoke_test_passed: bool
    component_speaker_test_passed: bool
    removed_from_base: bool
    smoke_sequence_number: int
    home_away_input: bool
    device_locale: str
    co_blame_threshold: int
    kl_software_version: str
    component_us_test_passed: bool
    auto_away: bool
    night_light_enable: bool
    component_als_test_passed: bool
    speaker_test_results: 32768
    wired_or_battery: int
    is_rcs_used: bool
    replace_by_date_utc_secs: int
    certification_body: 2
    component_pir_test_passed: bool
    structure_id: str
    software_version: str
    component_hum_test_passed: bool
    home_alarm_link_capable: bool
    night_light_brightness: int
    device_external_color: str
    latest_manual_test_end_utc_secs: int
    smoke_status: int
    latest_manual_test_start_utc_secs: int
    component_temp_test_passed: bool
    home_alarm_link_connected: bool
    co_status: int
    heat_status: int
    product_id: int
    night_light_continuous: bool
    co_previous_peak: int
    auto_away_decision_time_secs: int
    component_co_test_passed: bool
    where_id: str
    serial_number: str
    component_heat_test_passed: bool
    latest_manual_test_cancelled: bool
    thread_mac_address: str
    resource_id: str
    buzzer_test_results: int
    wifi_mac_address: str
    line_power_present: bool
    gesture_hush_enable: bool
    device_born_on_date_utc_secs: int
    ntp_green_led_enable: bool
    component_led_test_passed: bool
    co_sequence_number: int
    thread_ip_address: list[str]
    component_wifi_test_passed: bool
    heads_up_enable: bool
    battery_level: int
    last_audio_self_test_end_utc_secs: int
    last_audio_self_test_start_utc_secs: int


@dataclass
class TopazBucket(Bucket):
    """Class that reflects a Nest API response."""

    value: TopazBucketValue = field(default_factory=TopazBucketValue)


@dataclass
class GoogleAuthResponse:
    """Class that reflects a Google Auth response."""

    access_token: str
    scope: str
    token_type: str
    expires_in: int
    id_token: str
    expiry_date: datetime.datetime = field(init=False)

    def __post_init__(self):
        """Set the expiry date during post init."""
        self.expiry_date = datetime.datetime.now() + datetime.timedelta(
            seconds=self.expires_in
        )

    def is_expired(self):
        """Check if access token is expired."""
        if self.expiry_date <= datetime.datetime.now():
            return True

        return False


@dataclass
class GoogleAuthResponseForCookies(GoogleAuthResponse):
    """Class that reflects a Google Auth response for cookies."""

    login_hint: str
    session_state: dict[str, dict[str, str]] = field(default_factory=dict)


# TODO rewrite to snake_case
@dataclass
class NestAuthClaims:
    """TODO."""

    subject: Any | None = None
    expirationTime: str | None = None
    policyId: str | None = None
    structureConstraint: str | None = None


@dataclass
class NestAuthResponse:
    """TODO."""

    jwt: str | None = None
    claims: NestAuthClaims = field(default_factory=NestAuthClaims)
    error: dict | None = None


@dataclass
class NestEnvironment:
    """Class to describe a Nest environment."""

    name: str
    client_id: str
    host: str


@dataclass
class Weather:
    """TODO."""

    icon: str
    sunrise: str
    sunset: str
    temp_c: str


@dataclass
class Location:
    """TODO."""

    city: str
    country: str
    state: str
    zip: str


@dataclass
class WeatherForStructures:
    """TODO."""

    current: Weather
    location: Location


@dataclass
class ServiceUrls:
    """TODO."""

    czfe_url: str
    direct_transport_url: str
    log_upload_url: str
    rubyapi_url: str
    support_url: str
    transport_url: str
    weather_url: str


@dataclass
class Weave:
    """TODO."""

    access_token: str
    pairing_token: str
    service_config: str


@dataclass
class Limits:
    """TODO."""

    smoke_detectors: int
    smoke_detectors_per_structure: int
    structures: int
    thermostats: int
    thermostats_per_structure: int


@dataclass
class FirstDataAPIResponse:
    """TODO."""

    weather_for_structures: dict[str, WeatherForStructures]
    service_urls: dict[str, ServiceUrls | Weave | Limits]
    _2fa_enabled: bool
    updated_buckets: list[Bucket]

    def __post_init__(self):
        """TODO."""
        self.updated_buckets = (
            [Bucket(**b) for b in self.updated_buckets] if self.updated_buckets else []
        )

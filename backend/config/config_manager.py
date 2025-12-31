import enum
import json
import os
import sys
from typing import Final, NotRequired, TypedDict

import pydash
import yaml
from sqlalchemy import URL
from yaml.loader import SafeLoader

from config import (
    DB_HOST,
    DB_NAME,
    DB_PASSWD,
    DB_PORT,
    DB_QUERY_JSON,
    DB_USER,
    LIBRARY_BASE_PATH,
    RETROARCH_CORES_PATH,
    RETROARCH_ENABLED,
    RETROARCH_MAX_SESSIONS,
    ROMM_BASE_PATH,
    ROMM_DB_DRIVER,
)
from exceptions.config_exceptions import ConfigNotWritableException
from logger.formatter import BLUE
from logger.formatter import highlight as hl
from logger.logger import log

ROMM_USER_CONFIG_PATH: Final = f"{ROMM_BASE_PATH}/config"
ROMM_USER_CONFIG_FILE: Final = f"{ROMM_USER_CONFIG_PATH}/config.yml"
SQLITE_DB_BASE_PATH: Final = f"{ROMM_BASE_PATH}/database"


class EjsControlsButton(TypedDict):
    """EmulatorJS controller button mapping.

    Attributes:
        value: Keyboard key binding for this button.
        value2: Controller button binding for this button.
    """

    value: NotRequired[str]  # Keyboard key
    value2: NotRequired[str]  # Controller button


class MetadataMediaType(enum.StrEnum):
    """Supported media types for ROM metadata and artwork.

    These values correspond to different types of visual assets
    that can be associated with ROMs during scanning.
    """

    BEZEL = "bezel"
    BOX2D = "box2d"
    BOX2D_BACK = "box2d_back"
    BOX3D = "box3d"
    MIXIMAGE = "miximage"
    PHYSICAL = "physical"
    SCREENSHOT = "screenshot"
    TITLE_SCREEN = "title_screen"
    MARQUEE = "marquee"
    LOGO = "logo"
    FANART = "fanart"
    VIDEO = "video"
    MANUAL = "manual"


class EjsControls(TypedDict):
    """EmulatorJS controller configuration for up to 4 players.

    Each player slot (_0 through _3) maps button numbers to their bindings.
    Button numbers are core-specific and correspond to the emulator's input map.

    Attributes:
        _0: Player 1 button mappings.
        _1: Player 2 button mappings.
        _2: Player 3 button mappings.
        _3: Player 4 button mappings.
    """

    _0: dict[int, EjsControlsButton]  # button_number -> EjsControlsButton
    _1: dict[int, EjsControlsButton]
    _2: dict[int, EjsControlsButton]
    _3: dict[int, EjsControlsButton]


EjsOption = dict[str, str]  # option_name -> option_value


class NetplayICEServer(TypedDict):
    """ICE server configuration for WebRTC netplay.

    Used to configure STUN/TURN servers for peer-to-peer connections
    in EmulatorJS netplay mode.

    Attributes:
        urls: STUN or TURN server URL (e.g., 'stun:stun.example.com:3478').
        username: Username for TURN authentication (optional for STUN).
        credential: Password for TURN authentication (optional for STUN).
    """

    urls: str
    username: NotRequired[str]
    credential: NotRequired[str]


class Config:
    """Application configuration container.

    Holds all configuration values loaded from config.yml and environment
    variables. Values are set during initialization by ConfigManager.

    Attributes:
        CONFIG_FILE_MOUNTED: Whether the config.yml file exists.
        CONFIG_FILE_WRITABLE: Whether the config.yml file is writable.
        EXCLUDED_PLATFORMS: List of platform slugs to exclude from scanning.
        ROMS_FOLDER_NAME: Name of the ROMs folder in the library.
        RETROARCH_ENABLED: Whether RetroArch streaming is enabled.
        RETROARCH_MAX_SESSIONS: Maximum concurrent streaming sessions.
        RETROARCH_CORES_PATH: Path to libretro cores directory.
        RETROARCH_PLATFORM_CORES: Mapping of platform slugs to core names.
    """

    CONFIG_FILE_MOUNTED: bool
    CONFIG_FILE_WRITABLE: bool
    EXCLUDED_PLATFORMS: list[str]
    EXCLUDED_SINGLE_EXT: list[str]
    EXCLUDED_SINGLE_FILES: list[str]
    EXCLUDED_MULTI_FILES: list[str]
    EXCLUDED_MULTI_PARTS_EXT: list[str]
    EXCLUDED_MULTI_PARTS_FILES: list[str]
    PLATFORMS_BINDING: dict[str, str]
    PLATFORMS_VERSIONS: dict[str, str]
    ROMS_FOLDER_NAME: str
    FIRMWARE_FOLDER_NAME: str
    SKIP_HASH_CALCULATION: bool
    HIGH_PRIO_STRUCTURE_PATH: str
    EJS_DEBUG: bool
    EJS_CACHE_LIMIT: int | None
    EJS_DISABLE_AUTO_UNLOAD: bool
    EJS_DISABLE_BATCH_BOOTUP: bool
    EJS_NETPLAY_ENABLED: bool
    EJS_NETPLAY_ICE_SERVERS: list[NetplayICEServer]
    EJS_SETTINGS: dict[str, EjsOption]  # core_name -> EjsOption
    EJS_CONTROLS: dict[str, EjsControls]  # core_name -> EjsControls
    RETROARCH_ENABLED: bool
    RETROARCH_MAX_SESSIONS: int
    RETROARCH_CORES_PATH: str
    RETROARCH_PLATFORM_CORES: dict[str, str]  # platform_slug -> core_name
    SCAN_METADATA_PRIORITY: list[str]
    SCAN_ARTWORK_PRIORITY: list[str]
    SCAN_REGION_PRIORITY: list[str]
    SCAN_LANGUAGE_PRIORITY: list[str]
    SCAN_MEDIA: list[str]

    def __init__(self, **entries):
        """Initialize Config with the given entries.

        Args:
            **entries: Configuration key-value pairs to set as attributes.
        """
        self.__dict__.update(entries)
        self.HIGH_PRIO_STRUCTURE_PATH = f"{LIBRARY_BASE_PATH}/{self.ROMS_FOLDER_NAME}"


class ConfigManager:
    """
    Parse and load the user configuration from the config.yml file.
    If config.yml is not found, uses default configuration values.

    The config file will be created automatically when configuration is updated.
    """

    _self = None
    _raw_config: dict = {}
    _config_file_mounted: bool = False
    _config_file_writable: bool = False

    def __new__(cls, *args, **kwargs):
        if cls._self is None:
            cls._self = super().__new__(cls, *args, **kwargs)

        return cls._self

    # Tests require custom config path
    def __init__(self, config_file: str = ROMM_USER_CONFIG_FILE):
        self.config_file = config_file

        try:
            # Check if the config file is mounted
            with open(self.config_file, "r") as cf:
                self._config_file_mounted = True
                self._raw_config = yaml.load(cf, Loader=SafeLoader) or {}

            # Also check if the config file is writable
            self._config_file_writable = os.access(self.config_file, os.W_OK)
        except FileNotFoundError:
            log.critical(
                "Config file not found! Any changes made to the configuration will not persist after the application restarts."
            )
        except PermissionError:
            log.warning(
                "Config file not writable! Any changes made to the configuration will not persist after the application restarts."
            )
        finally:
            # Set the config to default values
            self._parse_config()
            self._validate_config()

    @staticmethod
    def get_db_engine() -> URL:
        """Builds the database connection string using environment variables

        Returns:
            str: database connection string
        """

        if ROMM_DB_DRIVER == "mariadb":
            driver = "mariadb+mariadbconnector"
        elif ROMM_DB_DRIVER == "mysql":
            driver = "mysql+mysqlconnector"
        elif ROMM_DB_DRIVER == "postgresql":
            driver = "postgresql+psycopg"
        else:
            log.critical(f"{hl(ROMM_DB_DRIVER)} database not supported")
            sys.exit(3)

        if not DB_USER or not DB_PASSWD:
            log.critical(
                "Missing database credentials, check your environment variables!"
            )
            sys.exit(3)

        query: dict[str, str] = {}
        if DB_QUERY_JSON:
            try:
                query = json.loads(DB_QUERY_JSON)
            except ValueError as exc:
                log.critical(f"Invalid JSON in DB_QUERY_JSON: {exc}")
                sys.exit(3)

        return URL.create(
            drivername=driver,
            username=DB_USER,
            password=DB_PASSWD,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            query=query,
        )

    def _parse_config(self):
        """Parses each entry in the config.yml"""

        self.config = Config(
            CONFIG_FILE_MOUNTED=self._config_file_mounted,
            CONFIG_FILE_WRITABLE=self._config_file_writable,
            EXCLUDED_PLATFORMS=pydash.get(self._raw_config, "exclude.platforms", []),
            EXCLUDED_SINGLE_EXT=[
                e.lower()
                for e in pydash.get(
                    self._raw_config, "exclude.roms.single_file.extensions", []
                )
            ],
            EXCLUDED_SINGLE_FILES=pydash.get(
                self._raw_config, "exclude.roms.single_file.names", []
            ),
            EXCLUDED_MULTI_FILES=pydash.get(
                self._raw_config, "exclude.roms.multi_file.names", []
            ),
            EXCLUDED_MULTI_PARTS_EXT=[
                e.lower()
                for e in pydash.get(
                    self._raw_config, "exclude.roms.multi_file.parts.extensions", []
                )
            ],
            EXCLUDED_MULTI_PARTS_FILES=pydash.get(
                self._raw_config, "exclude.roms.multi_file.parts.names", []
            ),
            PLATFORMS_BINDING=pydash.get(self._raw_config, "system.platforms", {}),
            PLATFORMS_VERSIONS=pydash.get(self._raw_config, "system.versions", {}),
            ROMS_FOLDER_NAME=pydash.get(
                self._raw_config, "filesystem.roms_folder", "roms"
            ),
            FIRMWARE_FOLDER_NAME=pydash.get(
                self._raw_config, "filesystem.firmware_folder", "bios"
            ),
            SKIP_HASH_CALCULATION=pydash.get(
                self._raw_config, "filesystem.skip_hash_calculation", False
            ),
            EJS_DEBUG=pydash.get(self._raw_config, "emulatorjs.debug", False),
            EJS_CACHE_LIMIT=pydash.get(
                self._raw_config, "emulatorjs.cache_limit", None
            ),
            EJS_DISABLE_AUTO_UNLOAD=pydash.get(
                self._raw_config, "emulatorjs.disable_auto_unload", False
            ),
            EJS_DISABLE_BATCH_BOOTUP=pydash.get(
                self._raw_config, "emulatorjs.disable_batch_bootup", False
            ),
            EJS_NETPLAY_ENABLED=pydash.get(
                self._raw_config, "emulatorjs.netplay.enabled", False
            ),
            EJS_NETPLAY_ICE_SERVERS=pydash.get(
                self._raw_config, "emulatorjs.netplay.ice_servers", []
            ),
            EJS_SETTINGS=pydash.get(self._raw_config, "emulatorjs.settings", {}),
            EJS_CONTROLS=self._get_ejs_controls(),
            # RetroArch: Environment variables take precedence over config.yml
            RETROARCH_ENABLED=RETROARCH_ENABLED
            if RETROARCH_ENABLED
            else pydash.get(self._raw_config, "retroarch.enabled", False),
            RETROARCH_MAX_SESSIONS=RETROARCH_MAX_SESSIONS
            if RETROARCH_MAX_SESSIONS != 3
            else pydash.get(self._raw_config, "retroarch.max_sessions", 3),
            RETROARCH_CORES_PATH=RETROARCH_CORES_PATH
            if RETROARCH_CORES_PATH != "/usr/lib/libretro"
            else pydash.get(
                self._raw_config, "retroarch.cores_path", "/usr/lib/libretro"
            ),
            RETROARCH_PLATFORM_CORES=pydash.get(
                self._raw_config, "retroarch.platform_cores", {}
            ),
            SCAN_METADATA_PRIORITY=pydash.get(
                self._raw_config,
                "scan.priority.metadata",
                [
                    "igdb",
                    "moby",
                    "ss",
                    "ra",
                    "launchbox",
                    "gamelist",
                    "hasheous",
                    "tgdb",
                    "flashpoint",
                    "hltb",
                ],
            ),
            SCAN_ARTWORK_PRIORITY=pydash.get(
                self._raw_config,
                "scan.priority.artwork",
                [
                    "igdb",
                    "moby",
                    "ss",
                    "ra",
                    "launchbox",
                    "gamelist",
                    "hasheous",
                    "tgdb",
                    "flashpoint",
                    "hltb",
                ],
            ),
            SCAN_REGION_PRIORITY=pydash.get(
                self._raw_config,
                "scan.priority.region",
                ["us", "wor", "ss", "eu", "jp"],
            ),
            SCAN_LANGUAGE_PRIORITY=pydash.get(
                self._raw_config,
                "scan.priority.language",
                ["en", "fr"],
            ),
            SCAN_MEDIA=pydash.get(
                self._raw_config,
                "scan.media",
                [
                    "box2d",
                    "screenshot",
                    "manual",
                ],
            ),
        )

    def _get_ejs_controls(self) -> dict[str, EjsControls]:
        """Get EJS controls with default player entries for each core.

        Parses the emulatorjs.controls section from config.yml and converts
        it to the EjsControls TypedDict format with player slots _0 through _3.

        Returns:
            Dictionary mapping core names to their control configurations.
        """
        raw_controls = pydash.get(self._raw_config, "emulatorjs.controls", {})
        controls = {}

        for core, core_controls in raw_controls.items():
            # Create EjsControls object with default empty player dictionaries
            controls[core] = EjsControls(
                _0=core_controls.get(0, {}),
                _1=core_controls.get(1, {}),
                _2=core_controls.get(2, {}),
                _3=core_controls.get(3, {}),
            )

        return controls

    def _format_ejs_controls_for_yaml(
        self,
    ) -> dict[str, dict[int, dict[int, EjsControlsButton]]]:
        """Format EJS controls back to YAML structure for saving.

        Converts the internal EjsControls format (with _0, _1, etc. keys)
        back to the YAML-friendly format (with 0, 1, etc. integer keys).

        Returns:
            Dictionary suitable for YAML serialization.
        """
        yaml_controls = {}

        for core, controls in self.config.EJS_CONTROLS.items():
            yaml_controls[core] = {
                0: controls["_0"],
                1: controls["_1"],
                2: controls["_2"],
                3: controls["_3"],
            }

        return yaml_controls

    def _validate_config(self):
        """Validate the parsed configuration values.

        Checks that all configuration values have the expected types and
        valid values. Exits the application with an error if validation fails.

        Raises:
            SystemExit: If any configuration value is invalid.
        """
        if not isinstance(self.config.EXCLUDED_PLATFORMS, list):
            log.critical("Invalid config.yml: exclude.platforms must be a list")
            sys.exit(3)

        if not isinstance(self.config.EXCLUDED_SINGLE_EXT, list):
            log.critical(
                "Invalid config.yml: exclude.roms.single_file.extensions must be a list"
            )
            sys.exit(3)

        if not isinstance(self.config.EXCLUDED_SINGLE_FILES, list):
            log.critical(
                "Invalid config.yml: exclude.roms.single_file.names must be a list"
            )
            sys.exit(3)

        if not isinstance(self.config.EXCLUDED_MULTI_FILES, list):
            log.critical(
                "Invalid config.yml: exclude.roms.multi_file.names must be a list"
            )
            sys.exit(3)

        if not isinstance(self.config.EXCLUDED_MULTI_PARTS_EXT, list):
            log.critical(
                "Invalid config.yml: exclude.roms.multi_file.parts.extensions must be a list"
            )
            sys.exit(3)

        if not isinstance(self.config.EXCLUDED_MULTI_PARTS_FILES, list):
            log.critical(
                "Invalid config.yml: exclude.roms.multi_file.parts.names must be a list"
            )
            sys.exit(3)

        if not isinstance(self.config.PLATFORMS_BINDING, dict):
            log.critical("Invalid config.yml: system.platforms must be a dictionary")
            sys.exit(3)
        else:
            for fs_slug, slug in self.config.PLATFORMS_BINDING.items():
                if slug is None:
                    log.critical(
                        f"Invalid config.yml: system.platforms.{fs_slug} must be a string"
                    )
                    sys.exit(3)

        if not isinstance(self.config.PLATFORMS_VERSIONS, dict):
            log.critical("Invalid config.yml: system.versions must be a dictionary")
            sys.exit(3)
        else:
            for fs_slug, slug in self.config.PLATFORMS_VERSIONS.items():
                if slug is None:
                    log.critical(
                        f"Invalid config.yml: system.versions.{fs_slug} must be a string"
                    )
                    sys.exit(3)

        if not isinstance(self.config.ROMS_FOLDER_NAME, str):
            log.critical("Invalid config.yml: filesystem.roms_folder must be a string")
            sys.exit(3)

        if self.config.ROMS_FOLDER_NAME == "":
            log.critical(
                "Invalid config.yml: filesystem.roms_folder cannot be an empty string"
            )
            sys.exit(3)

        if not isinstance(self.config.FIRMWARE_FOLDER_NAME, str):
            log.critical(
                "Invalid config.yml: filesystem.firmware_folder must be a string"
            )
            sys.exit(3)

        if self.config.FIRMWARE_FOLDER_NAME == "":
            log.critical(
                "Invalid config.yml: filesystem.firmware_folder cannot be an empty string"
            )
            sys.exit(3)

        if not isinstance(self.config.EJS_DEBUG, bool):
            log.critical("Invalid config.yml: emulatorjs.debug must be a boolean")
            sys.exit(3)

        if not isinstance(self.config.EJS_NETPLAY_ENABLED, bool):
            log.critical(
                "Invalid config.yml: emulatorjs.netplay.enabled must be a boolean"
            )
            sys.exit(3)

        if self.config.EJS_CACHE_LIMIT is not None and not isinstance(
            self.config.EJS_CACHE_LIMIT, int
        ):
            log.critical(
                "Invalid config.yml: emulatorjs.cache_limit must be an integer"
            )
            sys.exit(3)

        if not isinstance(self.config.EJS_DISABLE_AUTO_UNLOAD, bool):
            log.critical(
                "Invalid config.yml: emulatorjs.disable_auto_unload must be a boolean"
            )
            sys.exit(3)

        if not isinstance(self.config.EJS_DISABLE_BATCH_BOOTUP, bool):
            log.critical(
                "Invalid config.yml: emulatorjs.disable_batch_bootup must be a boolean"
            )
            sys.exit(3)

        if not isinstance(self.config.EJS_NETPLAY_ICE_SERVERS, list):
            log.critical(
                "Invalid config.yml: emulatorjs.netplay.ice_servers must be a list"
            )
            sys.exit(3)

        if not isinstance(self.config.EJS_SETTINGS, dict):
            log.critical("Invalid config.yml: emulatorjs.settings must be a dictionary")
            sys.exit(3)
        else:
            for core, options in self.config.EJS_SETTINGS.items():
                if not isinstance(options, dict):
                    log.critical(
                        f"Invalid config.yml: emulatorjs.settings.{core} must be a dictionary"
                    )
                    sys.exit(3)

        if not isinstance(self.config.EJS_CONTROLS, dict):
            log.critical("Invalid config.yml: emulatorjs.controls must be a dictionary")
            sys.exit(3)
        else:
            for core, controls in self.config.EJS_CONTROLS.items():
                if not isinstance(controls, dict):
                    log.critical(
                        f"Invalid config.yml: emulatorjs.controls.{core} must be a dictionary"
                    )
                    sys.exit(3)

                for player, buttons in controls.items():
                    if not isinstance(buttons, dict):
                        log.critical(
                            f"Invalid config.yml: emulatorjs.controls.{core}.{player} must be a dictionary"
                        )
                        sys.exit(3)

                    for button, value in buttons.items():
                        if not isinstance(value, dict):
                            log.critical(
                                f"Invalid config.yml: emulatorjs.controls.{core}.{player}.{button} must be a dictionary"
                            )
                            sys.exit(3)

        if not isinstance(self.config.SCAN_METADATA_PRIORITY, list):
            log.critical("Invalid config.yml: scan.priority.metadata must be a list")
            sys.exit(3)

        if not isinstance(self.config.SCAN_ARTWORK_PRIORITY, list):
            log.critical("Invalid config.yml: scan.priority.artwork must be a list")
            sys.exit(3)

        if not isinstance(self.config.SCAN_REGION_PRIORITY, list):
            log.critical("Invalid config.yml: scan.priority.region must be a list")
            sys.exit(3)

        if not isinstance(self.config.SCAN_LANGUAGE_PRIORITY, list):
            log.critical("Invalid config.yml: scan.priority.language must be a list")
            sys.exit(3)

        if not isinstance(self.config.SCAN_MEDIA, list):
            log.critical("Invalid config.yml: scan.media must be a list")
            sys.exit(3)

        for media in self.config.SCAN_MEDIA:
            if media not in MetadataMediaType:
                log.critical(
                    f"Invalid config.yml: scan.media.{media} is not a valid media type"
                )
                sys.exit(3)

    def get_config(self) -> Config:
        """Get the current configuration, reloading from file if available.

        Re-reads the config.yml file to pick up any changes, then parses
        and validates the configuration before returning.

        Returns:
            The current Config object with all settings.
        """
        try:
            with open(self.config_file, "r") as config_file:
                self._raw_config = yaml.load(config_file, Loader=SafeLoader) or {}
        except FileNotFoundError:
            log.debug("Config file not found!")

        self._parse_config()
        self._validate_config()

        return self.config

    def _update_config_file(self) -> None:
        """Write the current configuration to the config.yml file.

        Converts all config values back to YAML format and saves them.
        Creates the config directory if it doesn't exist.

        Raises:
            ConfigNotWritableException: If the config file is not writable.
        """
        if not self._config_file_writable:
            log.warning("Config file not writable, skipping config file update")
            raise ConfigNotWritableException

        self._raw_config = {
            "exclude": {
                "platforms": self.config.EXCLUDED_PLATFORMS,
                "roms": {
                    "single_file": {
                        "extensions": self.config.EXCLUDED_SINGLE_EXT,
                        "names": self.config.EXCLUDED_SINGLE_FILES,
                    },
                    "multi_file": {
                        "names": self.config.EXCLUDED_MULTI_FILES,
                        "parts": {
                            "extensions": self.config.EXCLUDED_MULTI_PARTS_EXT,
                            "names": self.config.EXCLUDED_MULTI_PARTS_FILES,
                        },
                    },
                },
            },
            "filesystem": {
                "roms_folder": self.config.ROMS_FOLDER_NAME,
                "firmware_folder": self.config.FIRMWARE_FOLDER_NAME,
            },
            "system": {
                "platforms": self.config.PLATFORMS_BINDING,
                "versions": self.config.PLATFORMS_VERSIONS,
            },
            "emulatorjs": {
                "debug": self.config.EJS_DEBUG,
                "cache_limit": self.config.EJS_CACHE_LIMIT,
                "disable_auto_unload": self.config.EJS_DISABLE_AUTO_UNLOAD,
                "disable_batch_bootup": self.config.EJS_DISABLE_BATCH_BOOTUP,
                "netplay": {
                    "enabled": self.config.EJS_NETPLAY_ENABLED,
                    "ice_servers": self.config.EJS_NETPLAY_ICE_SERVERS,
                },
                "settings": self.config.EJS_SETTINGS,
                "controls": self._format_ejs_controls_for_yaml(),
            },
            "scan": {
                "priority": {
                    "metadata": self.config.SCAN_METADATA_PRIORITY,
                    "artwork": self.config.SCAN_ARTWORK_PRIORITY,
                    "region": self.config.SCAN_REGION_PRIORITY,
                    "language": self.config.SCAN_LANGUAGE_PRIORITY,
                },
            },
        }

        try:
            # Ensure the config directory exists
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)

            with open(self.config_file, "w+") as config_file:
                yaml.dump(self._raw_config, config_file)
        except PermissionError as exc:
            log.critical("Config file not writable, skipping config file update")
            raise ConfigNotWritableException from exc

    def add_platform_binding(self, fs_slug: str, slug: str) -> None:
        """Add a filesystem to platform slug binding.

        Maps a folder name in the library to a specific platform identifier.

        Args:
            fs_slug: Filesystem folder name (e.g., 'nintendo-64').
            slug: Platform identifier (e.g., 'n64').
        """
        platform_bindings = self.config.PLATFORMS_BINDING
        if fs_slug in platform_bindings:
            log.warning(f"Binding for {hl(fs_slug)} already exists")
            return None

        platform_bindings[fs_slug] = slug
        self.config.PLATFORMS_BINDING = platform_bindings
        self._update_config_file()

    def remove_platform_binding(self, fs_slug: str) -> None:
        """Remove a filesystem to platform slug binding.

        Args:
            fs_slug: Filesystem folder name to remove from bindings.
        """
        platform_bindings = self.config.PLATFORMS_BINDING

        try:
            del platform_bindings[fs_slug]
        except KeyError:
            pass

        self.config.PLATFORMS_BINDING = platform_bindings
        self._update_config_file()

    def add_platform_version(self, fs_slug: str, slug: str) -> None:
        """Add a platform version binding.

        Maps a folder name to a specific version/variant of a platform.

        Args:
            fs_slug: Filesystem folder name.
            slug: Version identifier.
        """
        platform_versions = self.config.PLATFORMS_VERSIONS
        if fs_slug in platform_versions:
            log.warning(f"Version for {hl(fs_slug)} already exists")
            return None

        platform_versions[fs_slug] = slug
        self.config.PLATFORMS_VERSIONS = platform_versions
        self._update_config_file()

    def remove_platform_version(self, fs_slug: str) -> None:
        """Remove a platform version binding.

        Args:
            fs_slug: Filesystem folder name to remove from version bindings.
        """
        platform_versions = self.config.PLATFORMS_VERSIONS

        try:
            del platform_versions[fs_slug]
        except KeyError:
            pass

        self.config.PLATFORMS_VERSIONS = platform_versions
        self._update_config_file()

    def add_exclusion(self, exclusion_type: str, exclusion_value: str):
        """Add an exclusion rule for scanning.

        Args:
            exclusion_type: Config attribute name (e.g., 'EXCLUDED_PLATFORMS').
            exclusion_value: Value to add to the exclusion list.
        """
        config_item = self.config.__getattribute__(exclusion_type)
        if exclusion_value in config_item:
            log.warning(
                f"{hl(exclusion_value)} already excluded in {hl(exclusion_type, color=BLUE)}"
            )
            return None

        config_item.append(exclusion_value)
        self.config.__setattr__(exclusion_type, config_item)
        self._update_config_file()

    def remove_exclusion(self, exclusion_type: str, exclusion_value: str):
        """Remove an exclusion rule from scanning.

        Args:
            exclusion_type: Config attribute name (e.g., 'EXCLUDED_PLATFORMS').
            exclusion_value: Value to remove from the exclusion list.
        """
        config_item = self.config.__getattribute__(exclusion_type)

        try:
            config_item.remove(exclusion_value)
        except ValueError:
            pass

        self.config.__setattr__(exclusion_type, config_item)
        self._update_config_file()


config_manager = ConfigManager()

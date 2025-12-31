"""Configuration response models for the config API endpoint."""

from typing import TypedDict

from config.config_manager import EjsControls, NetplayICEServer


class ConfigResponse(TypedDict):
    """Response model containing all RomM configuration settings.

    This TypedDict represents the complete configuration state returned
    by the /config endpoint, including file system settings, exclusions,
    platform bindings, emulator options, and scanning preferences.

    Attributes:
        CONFIG_FILE_MOUNTED: Whether the config.yml file exists.
        CONFIG_FILE_WRITABLE: Whether the config.yml file is writable.
        EXCLUDED_PLATFORMS: Platform slugs excluded from scanning.
        EXCLUDED_SINGLE_EXT: File extensions excluded for single-file ROMs.
        EXCLUDED_SINGLE_FILES: File names excluded for single-file ROMs.
        EXCLUDED_MULTI_FILES: File names excluded for multi-file ROMs.
        EXCLUDED_MULTI_PARTS_EXT: Extensions excluded for multi-file ROM parts.
        EXCLUDED_MULTI_PARTS_FILES: File names excluded for multi-file ROM parts.
        PLATFORMS_BINDING: Mapping of filesystem slugs to platform slugs.
        PLATFORMS_VERSIONS: Mapping of filesystem slugs to version identifiers.
        SKIP_HASH_CALCULATION: Whether to skip hash calculation during scanning.
        EJS_DEBUG: Enable EmulatorJS debug mode.
        EJS_CACHE_LIMIT: Maximum cache size for EmulatorJS.
        EJS_DISABLE_AUTO_UNLOAD: Disable automatic core unloading in EmulatorJS.
        EJS_DISABLE_BATCH_BOOTUP: Disable batch bootup in EmulatorJS.
        EJS_NETPLAY_ENABLED: Enable EmulatorJS netplay feature.
        EJS_NETPLAY_ICE_SERVERS: ICE servers for WebRTC netplay.
        EJS_SETTINGS: Per-core EmulatorJS settings.
        EJS_CONTROLS: Per-core EmulatorJS controller mappings.
        RETROARCH_ENABLED: Enable RetroArch streaming feature.
        RETROARCH_MAX_SESSIONS: Maximum concurrent RetroArch streaming sessions.
        RETROARCH_CORES_PATH: Path to libretro cores directory.
        RETROARCH_PLATFORM_CORES: Mapping of platform slugs to core names.
        SCAN_METADATA_PRIORITY: Priority order for metadata sources.
        SCAN_ARTWORK_PRIORITY: Priority order for artwork sources.
        SCAN_REGION_PRIORITY: Priority order for region selection.
        SCAN_LANGUAGE_PRIORITY: Priority order for language selection.
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
    SKIP_HASH_CALCULATION: bool
    EJS_DEBUG: bool
    EJS_CACHE_LIMIT: int | None
    EJS_DISABLE_AUTO_UNLOAD: bool
    EJS_DISABLE_BATCH_BOOTUP: bool
    EJS_NETPLAY_ENABLED: bool
    EJS_NETPLAY_ICE_SERVERS: list[NetplayICEServer]
    EJS_SETTINGS: dict[str, dict[str, str]]
    EJS_CONTROLS: dict[str, EjsControls]
    RETROARCH_ENABLED: bool
    RETROARCH_MAX_SESSIONS: int
    RETROARCH_CORES_PATH: str
    RETROARCH_PLATFORM_CORES: dict[str, str]
    SCAN_METADATA_PRIORITY: list[str]
    SCAN_ARTWORK_PRIORITY: list[str]
    SCAN_REGION_PRIORITY: list[str]
    SCAN_LANGUAGE_PRIORITY: list[str]

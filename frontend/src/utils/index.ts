import cronstrue from "cronstrue";
import { formatDistanceToNow } from "date-fns";
import { storeToRefs } from "pinia";
import { computed } from "vue";
import { useDisplay } from "vuetify";
import type { RomFileSchema, RomUserStatus } from "@/__generated__";
import type { Config } from "@/stores/config";
import type { Heartbeat } from "@/stores/heartbeat";
import storeNavigation from "@/stores/navigation";
import type { SimpleRom } from "@/stores/roms";

/**
 * Views configuration object.
 */
export const views: Record<
  number,
  {
    view: string;
    icon: string;
    "size-xl": number;
    "size-lg": number;
    "size-md": number;
    "size-sm": number;
    "size-cols": number;
  }
> = {
  0: {
    view: "small",
    icon: "mdi-view-comfy",
    "size-cols": 4,
    "size-sm": 2,
    "size-md": 2,
    "size-lg": 1,
    "size-xl": 1,
  },
  1: {
    view: "big",
    icon: "mdi-view-module",
    "size-cols": 6,
    "size-sm": 3,
    "size-md": 3,
    "size-lg": 2,
    "size-xl": 2,
  },
  2: {
    view: "list",
    icon: "mdi-view-list",
    "size-cols": 12,
    "size-sm": 12,
    "size-md": 12,
    "size-lg": 12,
    "size-xl": 12,
  },
};

/**
 * Get icon associated to role.
 *
 * @param role The role as string.
 * @returns The mdi icon string.
 */
export function getRoleIcon(role: string) {
  switch (role) {
    case "admin":
      return "mdi-shield-crown-outline";
    case "editor":
      return "mdi-file-edit-outline";
    case "viewer":
      return "mdi-book-open-variant-outline";
    default:
      return "mdi-account";
  }
}

/**
 * Default path for user avatars.
 */
export const defaultAvatarPath = "/assets/default/user.svg";

/**
 * Normalize a string by converting it to lowercase and removing diacritics.
 *
 * @param s The string to normalize.
 * @returns The normalized string.
 */
export function normalizeString(s: string) {
  return s
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");
}

/**
 * Convert a cron expression to a human-readable string.
 *
 * @param expression The cron expression to convert.
 * @returns The human-readable string.
 */
export function convertCronExperssion(expression: string) {
  let convertedExpression = cronstrue.toString(expression, { verbose: true });
  convertedExpression =
    convertedExpression.charAt(0).toLocaleLowerCase() +
    convertedExpression.substr(1);
  return convertedExpression;
}

/**
 * Generate a download link for ROM content.
 *
 * @param rom The ROM object.
 * @param files Optional array of file names to include in the download.
 * @returns The download link.
 */
export function getDownloadPath({
  rom,
  fileIDs = [],
}: {
  rom: SimpleRom;
  fileIDs?: number[];
}) {
  const queryParams = new URLSearchParams();
  if (fileIDs.length > 0) {
    queryParams.append("file_ids", fileIDs.join(","));
  }
  return `/api/roms/${rom.id}/content/${rom.fs_name}?${queryParams.toString()}`;
}

export function getDownloadLink({
  rom,
  fileIDs = [],
}: {
  rom: SimpleRom;
  fileIDs?: number[];
}) {
  return `${window.location.origin}${encodeURI(
    getDownloadPath({ rom, fileIDs }),
  )}`;
}

/**
 * Format bytes as human-readable text.
 *
 * @param bytes Number of bytes.
 * @param decimals Number of decimal places to display.
 * @returns Formatted string.
 */
export function formatBytes(bytes: number, decimals = 2) {
  if (bytes === 0) return "0 Bytes";
  const k = 1024;
  const dm = Math.max(0, decimals);
  const sizes = ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}

/**
 * Convert locale format from app format (fr_FR) to browser format (fr-FR).
 *
 * @param locale The locale string (e.g., "fr_FR" or "fr-FR").
 * @returns The browser-compatible locale string (e.g., "fr-FR").
 */
export function toBrowserLocale(locale: string): string {
  return locale.replace("_", "-");
}

/**
 * Format a timestamp to a human-readable string.
 *
 * @param timestamp The timestamp to format.
 * @param locale The locale to use for formatting (e.g., "fr_FR" or "fr-FR"). Defaults to "en-US".
 * @returns The formatted timestamp.
 */
export function formatTimestamp(
  timestamp: string | null,
  locale: string = "en-US",
) {
  if (!timestamp) return "-";

  const date = new Date(timestamp);
  return date.toLocaleString(toBrowserLocale(locale));
}

/**
 * Format a date to a relative time string (e.g., "3 days ago").
 * @param date The date to format.
 * @returns The relative time string.
 */
export function formatRelativeDate(date: string | Date) {
  return formatDistanceToNow(new Date(date), { addSuffix: true });
}

/**
 * Convert a region code to an emoji.
 *
 * @param region The region code.
 * @returns The corresponding emoji.
 */
export function regionToEmoji(region: string) {
  switch (region.toLowerCase()) {
    case "as":
    case "australia":
      return "🇦🇺";
    case "a":
    case "asia":
      return "🌏";
    case "b":
    case "bra":
    case "brazil":
      return "🇧🇷";
    case "c":
    case "canada":
      return "🇨🇦";
    case "ch":
    case "chn":
    case "china":
      return "🇨🇳";
    case "e":
    case "eu":
    case "eur":
    case "europe":
      return "🇪🇺";
    case "f":
    case "france":
      return "🇫🇷";
    case "fn":
    case "finland":
      return "🇫🇮";
    case "g":
    case "germany":
      return "🇩🇪";
    case "gr":
    case "greece":
      return "🇬🇷";
    case "h":
    case "holland":
      return "🇳🇱";
    case "hk":
    case "hong kong":
      return "🇭🇰";
    case "i":
    case "italy":
      return "🇮🇹";
    case "j":
    case "jp":
    case "japan":
      return "🇯🇵";
    case "k":
    case "korea":
      return "🇰🇷";
    case "nl":
    case "netherlands":
      return "🇳🇱";
    case "no":
    case "norway":
      return "🇳🇴";
    case "pd":
    case "public domain":
      return "🇵🇱";
    case "r":
    case "russia":
      return "🇷🇺";
    case "s":
    case "spain":
      return "🇪🇸";
    case "sw":
    case "sweden":
      return "🇸🇪";
    case "t":
    case "taiwan":
      return "🇹🇼";
    case "u":
    case "us":
    case "usa":
      return "🇺🇸";
    case "uk":
    case "england":
      return "🇬🇧";
    case "unk":
    case "unknown":
      return "🌎";
    case "unl":
    case "unlicensed":
      return "🌎";
    case "w":
    case "global":
    case "world":
      return "🌎";
    default:
      return region;
  }
}

/**
 * Convert a language code to an emoji.
 *
 * @param language The language code.
 * @returns The corresponding emoji.
 */
export function languageToEmoji(language: string) {
  switch (language.toLowerCase()) {
    case "af":
    case "afrikaans":
      return "🇿🇦";
    case "ar":
    case "arabic":
      return "🇦🇪";
    case "be":
    case "belarusian":
      return "🇧🇾";
    case "bg":
    case "bulgarian":
      return "🇧🇬";
    case "ca":
    case "catalan":
      return "🇦🇩";
    case "cs":
    case "czech":
      return "🇨🇿";
    case "da":
    case "danish":
      return "🇩🇰";
    case "de":
    case "german":
      return "🇩🇪";
    case "el":
    case "greek":
      return "🇬🇷";
    case "en":
    case "english":
      return "🇬🇧";
    case "es":
    case "spanish":
      return "🇪🇸";
    case "et":
    case "estonian":
      return "🇪🇪";
    case "fi":
    case "finnish":
      return "🇫🇮";
    case "fr":
    case "french":
      return "🇫🇷";
    case "he":
    case "hebrew":
      return "🇮🇱";
    case "hi":
    case "hindi":
      return "🇮🇳";
    case "hr":
    case "croatian":
      return "🇭🇷";
    case "hu":
    case "hungarian":
      return "🇭🇺";
    case "hy":
    case "armenian":
      return "🇦🇲";
    case "id":
    case "indonesian":
      return "🇮🇩";
    case "is":
    case "icelandic":
      return "🇮🇸";
    case "it":
    case "italian":
      return "🇮🇹";
    case "ja":
    case "japanese":
      return "🇯🇵";
    case "ko":
    case "korean":
      return "🇰🇷";
    case "la":
    case "latin":
      return "🇻🇦";
    case "lt":
    case "lithuanian":
      return "🇱🇹";
    case "lv":
    case "latvian":
      return "🇱🇻";
    case "mk":
    case "macedonian":
      return "🇲🇰";
    case "nl":
    case "dutch":
      return "🇳🇱";
    case "no":
    case "norwegian":
      return "🇳🇴";
    case "pl":
    case "polish":
      return "🇵🇱";
    case "pt":
    case "portuguese":
      return "🇵🇹";
    case "ro":
    case "romanian":
      return "🇷🇴";
    case "ru":
    case "russian":
      return "🇷🇺";
    case "sk":
    case "slovak":
      return "🇸🇰";
    case "sl":
    case "slovenian":
      return "🇸🇮";
    case "sq":
    case "albanian":
      return "🇦🇱";
    case "sr":
    case "serbian":
      return "🇷🇸";
    case "sv":
    case "swedish":
      return "🇸🇪";
    case "th":
    case "thai":
      return "🇹🇭";
    case "tr":
    case "turkish":
      return "🇹🇷";
    case "uk":
    case "ukrainian":
      return "🇺🇦";
    case "vi":
    case "vietnamese":
      return "🇻🇳";
    case "zh":
    case "chinese":
      return "🇨🇳";
    case "nolang":
    case "no language":
      return "🌎";
    default:
      return language;
  }
}

/**
 * Map of supported EJS cores for each platform.
 */
const _EJS_CORES_MAP: Record<string, string[]> = {
  "3do": ["opera"],
  acpc: ["cap32", "crocods"],
  amiga: ["puae"],
  "amiga-cd32": ["puae"],
  arcade: [
    "mame2003",
    "mame2003_plus",
    "fbneo",
    "fbalpha2012_cps1",
    "fbalpha2012_cps2",
  ],
  neogeoaes: ["fbneo"],
  neogeomvs: ["fbneo"],
  atari2600: ["stella2014"],
  "atari-2600-plus": ["stella2014"],
  atari5200: ["a5200"],
  atari7800: ["prosystem"],
  "c-plus-4": ["vice_xplus4"],
  c64: ["vice_x64sc", "vice_x64"],
  cpet: ["vice_xpet"],
  "commodore-64c": ["vice_x64sc", "vice_x64"],
  c128: ["vice_x128"],
  "commmodore-128": ["vice_x128"],
  colecovision: ["gearcoleco"],
  doom: ["prboom"],
  dos: ["dosbox_pure"],
  jaguar: ["virtualjaguar"],
  lynx: ["handy"],
  "atari-lynx-mkii": ["handy"],
  "neo-geo-pocket": ["mednafen_ngp"],
  "neo-geo-pocket-color": ["mednafen_ngp"],
  nes: ["fceumm", "nestopia"],
  famicom: ["fceumm", "nestopia"],
  fds: ["fceumm", "nestopia"],
  "game-televisison": ["fceumm"],
  "new-style-nes": ["fceumm"],
  n64: ["mupen64plus_next", "parallel_n64"],
  "ique-player": ["mupen64plus_next"],
  nds: ["melonds", "desmume2015"],
  "nintendo-ds-lite": ["melonds", "desmume2015"],
  "nintendo-dsi": ["melonds", "desmume2015"],
  "nintendo-dsi-xl": ["melonds", "desmume2015"],
  gb: ["gambatte", "mgba"],
  "game-boy-pocket": ["gambatte", "mgba"],
  "game-boy-light": ["gambatte", "mgba"],
  gba: ["mgba"],
  "game-boy-adavance-sp": ["mgba"],
  "game-boy-micro": ["mgba"],
  gbc: ["gambatte", "mgba"],
  "pc-fx": ["mednafen_pcfx"],
  psx: ["pcsx_rearmed", "mednafen_psx_hw"],
  "philips-cd-i": ["same_cdi"],
  psp: ["ppsspp"],
  segacd: ["genesis_plus_gx", "picodrive"],
  sega32: ["picodrive"],
  gamegear: ["genesis_plus_gx"],
  sms: ["genesis_plus_gx"],
  "sega-mark-iii": ["genesis_plus_gx"],
  "sega-game-box-9": ["genesis_plus_gx"],
  "sega-master-system-ii": ["genesis_plus_gx", "smsplus"],
  "master-system-super-compact": ["genesis_plus_gx"],
  "master-system-girl": ["genesis_plus_gx"],
  genesis: ["genesis_plus_gx"],
  "sega-mega-drive-2-slash-genesis": ["genesis_plus_gx"],
  "sega-mega-jet": ["genesis_plus_gx"],
  "mega-pc": ["genesis_plus_gx"],
  "tera-drive": ["genesis_plus_gx"],
  "sega-nomad": ["genesis_plus_gx"],
  saturn: ["yabause"],
  snes: ["snes9x"],
  sfam: ["snes9x"],
  "super-nintendo-original-european-version": ["snes9x"],
  "super-famicom-shvc-001": ["snes9x"],
  "super-famicom-jr-model-shvc-101": ["snes9x"],
  "new-style-super-nes-model-sns-101": ["snes9x"],
  tg16: ["mednafen_pce"],
  "vic-20": ["vice_xvic"],
  virtualboy: ["beetle_vb"],
  wonderswan: ["mednafen_wswan"],
  swancrystal: ["mednafen_wswan"],
  "wonderswan-color": ["mednafen_wswan"],
  zsx: ["fuse"],
} as const;

export type EJSPlatformSlug = keyof typeof _EJS_CORES_MAP;

/**
 * Get the supported EJS cores for a given platform.
 *
 * @param platformSlug The platform slug.
 * @returns An array of supported cores.
 */
export function getSupportedEJSCores(platformSlug: string): string[] {
  return _EJS_CORES_MAP[platformSlug.toLowerCase() as EJSPlatformSlug] || [];
}

/**
 * Map of supported RetroArch cores for each platform.
 * Cores are prefixed with "ra-" to distinguish them from EmulatorJS cores.
 * Only includes platforms with well-maintained RetroArch cores.
 */
const _RETROARCH_CORES_MAP: Record<string, string[]> = {
  // Nintendo consoles
  nes: ["ra-fceumm", "ra-nestopia", "ra-mesen"],
  famicom: ["ra-fceumm", "ra-nestopia", "ra-mesen"],
  fds: ["ra-fceumm", "ra-nestopia"],
  snes: ["ra-snes9x", "ra-bsnes"],
  sfam: ["ra-snes9x", "ra-bsnes"],
  n64: ["ra-mupen64plus_next", "ra-parallel_n64"],
  "64dd": ["ra-mupen64plus_next"],
  ngc: ["ra-dolphin"],
  wii: ["ra-dolphin"],
  wiiu: ["ra-cemu"],
  switch: ["ra-yuzu"],

  // Nintendo handhelds
  gb: ["ra-gambatte", "ra-sameboy", "ra-mgba"],
  gbc: ["ra-gambatte", "ra-sameboy", "ra-mgba"],
  gba: ["ra-mgba", "ra-vba_next"],
  nds: ["ra-desmume", "ra-melonds"],
  "3ds": ["ra-citra"],
  "pokemon-mini": ["ra-pokemini"],
  virtualboy: ["ra-beetle_vb"],

  // Sega consoles
  genesis: ["ra-genesis_plus_gx", "ra-picodrive"],
  "sega-mega-drive-2-slash-genesis": ["ra-genesis_plus_gx", "ra-picodrive"],
  "sega-mega-jet": ["ra-genesis_plus_gx", "ra-picodrive"],
  "mega-pc": ["ra-genesis_plus_gx", "ra-picodrive"],
  "tera-drive": ["ra-genesis_plus_gx", "ra-picodrive"],
  "sega-nomad": ["ra-genesis_plus_gx", "ra-picodrive"],
  sega32: ["ra-picodrive"],
  segacd: ["ra-genesis_plus_gx"],
  segacd32: ["ra-genesis_plus_gx", "ra-picodrive"],
  saturn: ["ra-beetle_saturn", "ra-yabause", "ra-kronos"],
  dc: ["ra-flycast", "ra-redream"],
  sms: ["ra-genesis_plus_gx", "ra-picodrive"],
  "sega-mark-iii": ["ra-genesis_plus_gx"],
  "sega-master-system-ii": ["ra-genesis_plus_gx"],
  "master-system-super-compact": ["ra-genesis_plus_gx"],
  "master-system-girl": ["ra-genesis_plus_gx"],
  gamegear: ["ra-genesis_plus_gx"],
  sg1000: ["ra-genesis_plus_gx"],
  sc3000: ["ra-bluemsx"],
  "sega-pico": ["ra-picodrive"],
  pico: ["ra-picodrive"],

  // Sony consoles
  psx: ["ra-pcsx_rearmed", "ra-beetle_psx_hw", "ra-swanstation"],
  ps2: ["ra-pcsx2"],
  ps3: ["ra-rpcs3"],
  psp: ["ra-ppsspp"],
  psvita: ["ra-vita3k"],

  // Atari
  atari2600: ["ra-stella"],
  atari5200: ["ra-a5200"],
  atari7800: ["ra-prosystem"],
  atari8bit: ["ra-atari800"],
  atari800: ["ra-atari800"],
  "atari-st": ["ra-hatari"],
  lynx: ["ra-handy", "ra-beetle_lynx"],
  jaguar: ["ra-virtualjaguar"],
  "atari-jaguar-cd": ["ra-virtualjaguar"],

  // NEC
  tg16: ["ra-beetle_pce", "ra-mednafen_pce_fast"],
  "turbografx-cd": ["ra-beetle_pce"],
  supergrafx: ["ra-beetle_supergrafx"],
  "pc-fx": ["ra-beetle_pcfx"],
  "pc-8800-series": ["ra-quasi88"],
  "pc-9800-series": ["ra-np2kai"],

  // Arcade
  arcade: ["ra-fbneo", "ra-mame", "ra-mame2003_plus"],
  "neo-geo-cd": ["ra-fbneo", "ra-neocd"],
  neogeoaes: ["ra-fbneo"],
  neogeomvs: ["ra-fbneo"],
  "neo-geo-pocket": ["ra-beetle_ngp"],
  "neo-geo-pocket-color": ["ra-beetle_ngp"],

  // Other consoles
  "3do": ["ra-opera"],
  colecovision: ["ra-bluemsx", "ra-gearcoleco"],
  intellivision: ["ra-freeintv"],
  odyssey: ["ra-o2em"],
  "odyssey-2": ["ra-o2em"],
  vectrex: ["ra-vecx"],
  "fairchild-channel-f": ["ra-freechaf"],
  "bally-astrocade": ["ra-mame"],
  astrocade: ["ra-mame"],
  "philips-cd-i": ["ra-same_cdi"],

  // Computers
  c64: ["ra-vice_x64"],
  "vic-20": ["ra-vice_xvic"],
  c128: ["ra-vice_x128"],
  "c-plus-4": ["ra-vice_xplus4"],
  c16: ["ra-vice_xplus4"],
  amiga: ["ra-puae"],
  "amiga-cd32": ["ra-puae"],
  "amiga-cd": ["ra-puae"],
  "commodore-cdtv": ["ra-puae"],
  acpc: ["ra-cap32", "ra-crocods"],
  "amstrad-gx4000": ["ra-cap32"],
  zxs: ["ra-fuse"],
  zx81: ["ra-eightyone"],
  zx80: ["ra-eightyone"],
  "zx-spectrum-next": ["ra-fuse"],
  msx: ["ra-bluemsx", "ra-fmsx"],
  msx2: ["ra-bluemsx"],
  msx2plus: ["ra-bluemsx"],
  "msx-turbo": ["ra-bluemsx"],
  dos: ["ra-dosbox_pure", "ra-dosbox_svn"],
  "sharp-x68000": ["ra-px68k"],
  x1: ["ra-x1"],
  "pc-6001": ["ra-pc6001"],
  "fm-7": ["ra-xm7"],
  "fm-towns": ["ra-tsugaru"],
  bbcmicro: ["ra-b-em"],
  "apple-iigs": ["ra-mame"],
  appleii: ["ra-mame"],
  scummvm: ["ra-scummvm"],

  // Handhelds
  wonderswan: ["ra-beetle_wswan"],
  "wonderswan-color": ["ra-beetle_wswan"],
  swancrystal: ["ra-beetle_wswan"],
  "g-and-w": ["ra-gw"],
  "game-dot-com": ["ra-gamecom"],
  "mega-duck-slash-cougar-boy": ["ra-sameduck"],
  supervision: ["ra-potator"],
  hartung: ["ra-potator"],
  palmtex: ["ra-potator"],
  gamate: ["ra-mame"],
  microvision: ["ra-mame"],

  // Additional systems with RetroArch support
  "casio-loopy": ["ra-mame"],
  "casio-pv-1000": ["ra-mame"],
  multivision: ["ra-mame"],
  creativision: ["ra-mame"],
  "epoch-cassette-vision": ["ra-mame"],
  "epoch-super-cassette-vision": ["ra-mame"],
  pocketstation: ["ra-mame"],
  vmu: ["ra-vemulator"],
  uzebox: ["ra-uzem"],
  arduboy: ["ra-arduboy"],
  pokitto: ["ra-mame"],
  "wasm-4": ["ra-wasm4"],
  "philips-vg-5000": ["ra-mame"],
  aquarius: ["ra-mame"],
  oric: ["ra-oricutron"],
  atmos: ["ra-oricutron"],
  "jupiter-ace": ["ra-mame"],
  "sam-coupe": ["ra-mame"],
  "thomson-mo5": ["ra-theodore"],
  "thomson-to": ["ra-theodore"],
  "tomy-tutor": ["ra-mame"],
  "sord-m5": ["ra-mame"],
  spectravideo: ["ra-bluemsx"],
  "super-acan": ["ra-mame"],
  "super-vision-8000": ["ra-potator"],
  "rca-studio-ii": ["ra-mame"],
  "interton-vc-4000": ["ra-mame"],
  "vc-4000": ["ra-mame"],
  "videopac-g7400": ["ra-o2em"],
  "atari-xegs": ["ra-atari800"],
  "trs-80": ["ra-mame"],
  "trs-80-color-computer": ["ra-mame"],
  cpet: ["ra-vice_xpet"],
  atom: ["ra-mame"],
  "acorn-electron": ["ra-elkulator"],
  "acorn-archimedes": ["ra-mame"],
  "dragon-32-slash-64": ["ra-mame"],
  "camputers-lynx": ["ra-mame"],
  enterprise: ["ra-mame"],
  galaksija: ["ra-mame"],
  "colour-genie": ["ra-mame"],
  "smc-777": ["ra-mame"],
  mtx512: ["ra-mame"],
  "memotech-mtx": ["ra-mame"],
  "sinclair-ql": ["ra-mame"],
  "tatung-einstein": ["ra-mame"],
  exelvision: ["ra-mame"],
  laser200: ["ra-mame"],
  "ti-994a": ["ra-mame"],
  "ti-99": ["ra-mame"],
  colecoadam: ["ra-mame"],
  hrx: ["ra-mame"],
  "exidy-sorcerer": ["ra-mame"],
} as const;

export type RetroArchPlatformSlug = keyof typeof _RETROARCH_CORES_MAP;

/**
 * Get supported cores for a platform (EmulatorJS + RetroArch).
 * RetroArch cores have "ra-" prefix and are only included if RetroArch is enabled.
 *
 * @param platformSlug The platform slug.
 * @param retroarchEnabled Whether RetroArch streaming is enabled.
 * @returns An array of supported cores.
 */
export function getSupportedCores(
  platformSlug: string,
  retroarchEnabled: boolean = false
): string[] {
  const ejsCores = getSupportedEJSCores(platformSlug);

  if (!retroarchEnabled) {
    return ejsCores;
  }

  const raCores = _RETROARCH_CORES_MAP[
    platformSlug.toLowerCase() as RetroArchPlatformSlug
  ] || [];

  return [...ejsCores, ...raCores];
}

/**
 * Check if a core is a RetroArch core (prefixed with "ra-").
 *
 * @param core The core name.
 * @returns True if it's a RetroArch core, false otherwise.
 */
export function isRetroArchCore(core: string): boolean {
  return core.startsWith("ra-");
}

/**
 * Get the actual RetroArch core name (without "ra-" prefix).
 *
 * @param core The core name with prefix.
 * @returns The core name without prefix.
 */
export function getRetroArchCoreName(core: string): string {
  return core.replace(/^ra-/, "");
}

/**
 * Normalize a core/emulator name for comparison purposes.
 * Removes the "ra-" prefix if present, making EmulatorJS and RetroArch
 * cores with the same base name comparable (e.g., "ra-melonds" -> "melonds").
 *
 * @param core The core name (with or without "ra-" prefix).
 * @returns The normalized core name.
 */
export function normalizeCoreName(core: string | null | undefined): string {
  if (!core) return "";
  return core.replace(/^ra-/, "");
}

/**
 * Check if two cores are compatible (same base core name).
 * Handles the "ra-" prefix difference between EmulatorJS and RetroArch.
 *
 * @param core1 First core name.
 * @param core2 Second core name.
 * @returns True if cores are compatible.
 */
export function areCoresCompatible(
  core1: string | null | undefined,
  core2: string | null | undefined
): boolean {
  return normalizeCoreName(core1) === normalizeCoreName(core2);
}

/**
 * Check if a given EJS core requires threads enabled.
 *
 * @param core The core name.
 * @returns True if threads are required, false otherwise.
 */
export function areThreadsRequiredForEJSCore(core: string): boolean {
  return ["dosbox_pure", "ppsspp"].includes(core);
}

const canvas = document.createElement("canvas");
const gl =
  canvas.getContext("webgl") || canvas.getContext("experimental-webgl");

/**
 * Check if EJS emulation is supported for a given platform.
 *
 * @param platformSlug The platform slug.
 * @param heartbeat The heartbeat object.
 * @param config Optional configuration object.
 * @returns True if supported, false otherwise.
 */
export function isEJSEmulationSupported(
  platformSlug: string,
  heartbeat: Heartbeat,
  config?: Config,
) {
  if (heartbeat.EMULATION.DISABLE_EMULATOR_JS) return false;

  const slug = config?.PLATFORMS_VERSIONS[platformSlug] || platformSlug;
  return (
    getSupportedEJSCores(slug).length > 0 && gl instanceof WebGLRenderingContext
  );
}

// This is a workaround to set the control scheme for Sega systems using the same cores
const _EJS_CONTROL_SCHEMES = {
  segacd: "segaCD",
  sega32: "sega32x",
  gamegear: "segaGG",
  sms: "segaMS",
  "sega-mark-iii": "segaMS",
  "sega-master-system-ii": "segaMS",
  "master-system-super-compact": "segaMS",
  "master-system-girl": "segaMS",
  genesis: "segaMD",
  "sega-mega-drive-2-slash-genesis": "segaMD",
  "sega-mega-jet": "segaMD",
  "mega-pc": "segaMD",
  "tera-drive": "segaMD",
  "sega-nomad": "segaMD",
  saturn: "segaSaturn",
};

type EJSControlSlug = keyof typeof _EJS_CONTROL_SCHEMES;

/**
 * Get the control scheme for a given platform.
 *
 * @param platformSlug The platform slug.
 * @returns The control scheme.
 */
export function getControlSchemeForPlatform(
  platformSlug: string,
): string | null {
  return platformSlug in _EJS_CONTROL_SCHEMES
    ? _EJS_CONTROL_SCHEMES[platformSlug as EJSControlSlug]
    : null;
}

/**
 * Check if Ruffle emulation is supported for a given platform.
 *
 * @param platformSlug The platform slug.
 * @param heartbeat The heartbeat object.
 * @param config Optional configuration object.
 * @returns True if supported, false otherwise.
 */
export function isRuffleEmulationSupported(
  platformSlug: string,
  heartbeat: Heartbeat,
  config?: Config,
) {
  if (heartbeat.EMULATION.DISABLE_RUFFLE_RS) return false;

  const slug = config?.PLATFORMS_VERSIONS[platformSlug] || platformSlug;
  return ["flash", "browser"].includes(slug.toLowerCase());
}

type PlayingStatus = RomUserStatus | "backlogged" | "now_playing" | "hidden";

/**
 * Map of ROM statuses to their corresponding emoji, text, and i18n key.
 */
export const romStatusMap: Record<
  PlayingStatus,
  { emoji: string; text: string; i18nKey: string }
> = {
  backlogged: {
    emoji: "🔜",
    text: "Backlogged",
    i18nKey: "rom.status-backlogged",
  },
  now_playing: {
    emoji: "🕹️",
    text: "Now Playing",
    i18nKey: "rom.status-now-playing",
  },
  incomplete: {
    emoji: "🚧",
    text: "Incomplete",
    i18nKey: "rom.status-incomplete",
  },
  finished: { emoji: "🏁", text: "Finished", i18nKey: "rom.status-finished" },
  completed_100: {
    emoji: "💯",
    text: "Completed 100%",
    i18nKey: "rom.status-completed-100",
  },
  retired: { emoji: "🏴", text: "Retired", i18nKey: "rom.status-retired" },
  never_playing: {
    emoji: "🚫",
    text: "Never Playing",
    i18nKey: "rom.status-never-playing",
  },
  hidden: { emoji: "👻", text: "Hidden", i18nKey: "rom.status-hidden" },
};

/**
 * Inverse map of ROM statuses from text to status key.
 */
const inverseRomStatusMap = Object.fromEntries(
  Object.entries(romStatusMap).map(([key, value]) => [value.text, key]),
) as Record<string, PlayingStatus>;

/**
 * Get the emoji for a given ROM status.
 *
 * @param status The ROM status.
 * @returns The corresponding emoji.
 */
export function getEmojiForStatus(status: PlayingStatus) {
  if (status) {
    return romStatusMap[status].emoji;
  } else {
    return null;
  }
}

/**
 * Get the text for a given ROM status.
 *
 * @param status The ROM status.
 * @returns The corresponding text.
 */
export function getTextForStatus(status: PlayingStatus): string | null {
  return romStatusMap[status]?.text ?? null;
}

/**
 * Get the i18n key for a given ROM status.
 *
 * @param status The ROM status.
 * @returns The corresponding i18n key (e.g., "rom.status-backlogged").
 */
export function getI18nKeyForStatus(status: PlayingStatus): string | null {
  return romStatusMap[status]?.i18nKey ?? null;
}

/**
 * Get the status key for a given text.
 *
 * @param text The text to convert.
 * @returns The corresponding status key.
 */
export function getStatusKeyForText(text: string | null) {
  if (!text) return null;
  return inverseRomStatusMap[text];
}

export function isNintendoDSFile(rom: SimpleRom): boolean {
  return ["cia", "nds", "3ds", "dsi"].includes(rom.fs_extension.toLowerCase());
}

export function getNintendoDSFiles(rom: SimpleRom): RomFileSchema[] {
  return rom.files.filter((file) => {
    const fileName = file.file_name.toLowerCase();
    return (
      fileName.endsWith(".cia") ||
      fileName.endsWith(".nds") ||
      fileName.endsWith(".3ds") ||
      fileName.endsWith(".dsi")
    );
  });
}

/**
 * Check if a ROM is a valid NDS/3DS/DSi game
 * @param rom The ROM object.
 * @returns {boolean} True if the ROM is a valid game, otherwise false.
 */
export function isNintendoDSRom(rom: SimpleRom): boolean {
  if (
    !["3ds", "nds", "new-nintendo-3ds", "nintendo-dsi"].includes(
      rom.platform_slug,
    )
  )
    return false;

  const hasValidExtension = isNintendoDSFile(rom);
  const hasValidFile = getNintendoDSFiles(rom).length > 0;

  return hasValidExtension || hasValidFile;
}

export function calculateMainLayoutWidth() {
  const { smAndDown } = useDisplay();
  const navigationStore = storeNavigation();
  const { mainBarCollapsed } = storeToRefs(navigationStore);
  const calculatedWidth = computed(() => {
    return smAndDown.value
      ? "calc(100% - 16px) !important"
      : mainBarCollapsed.value
        ? "calc(100% - 76px) !important"
        : "calc(100% - 106px) !important";
  });

  return { calculatedWidth };
}

/**
 * Get the icon for a given platform category.
 *
 * @param category The platform category.
 * @returns The corresponding icon.
 */
export function platformCategoryToIcon(category: string) {
  if (!category) return "";
  switch (category.toLowerCase()) {
    case "console":
      return "mdi-gamepad-variant";
    case "computer":
      return "mdi-desktop-classic";
    case "portable console":
      return "mdi-nintendo-game-boy";
    case "arcade":
      return "mdi-gamepad-circle";
    case "operating system":
      return "mdi-monitor-shimmer";
    case "platform":
      return "mdi-desktop-tower-monitor";
    case "unknown":
    default:
      return "";
  }
}

export const FRONTEND_RESOURCES_PATH = "/assets/romm/resources";

export const CD_BASED_SYSTEMS = new Set([
  "3do", // 3DO
  "amiga-cd32", // Amiga CD32
  "atari-jaguar-cd", // Atari Jaguar CD
  "philips-cd-i", // Philips CD-i
  "commodore-cdtv", // Commodore CDTV
  "dc", // Dreamcast
  "fm-towns", // FM Towns
  "hyperscan", // HyperScan
  "laseractive", // LaserActive
  "neo-geo-cd", // Neo Geo CD
  "ngc", // Nintendo GameCube
  "pc-fx", // PC-FX
  "psx", // PlayStation
  "ps2", // PlayStation 2
  "ps3", // PlayStation 3
  "ps4", // PlayStation 4
  "ps5", // PlayStation 5
  "psp", // PlayStation Portable
  "segacd", // Sega CD
  "series-x-s", // Xbox Series X/S
  "saturn", // Sega Saturn
  "super-nes-cd-rom-system", // Super NES CD-ROM System
  "tandy-vis", // Tandy Video Information System
  "tg16", // TurboGrafx-16
  "vflash", // V.Flash
  "wii", // Wii
  "wiiu", // Wii U
  "xbox", // Xbox
  "xbox360", // Xbox 360
  "xboxone", // Xbox One
]);

export function isCDBasedSystem(platformSlug: string): boolean {
  return CD_BASED_SYSTEMS.has(platformSlug.toLowerCase());
}

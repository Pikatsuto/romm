/**
 * RetroArch Player Menu Component
 *
 * EmulatorJS-style overlay menu for RetroArch streaming player.
 * Provides quick access to save states and settings.
 *
 * Features:
 * - Auto-hiding menu bar with mouse activity detection
 * - Quick save/load, screenshot, pause, restart controls
 * - Video, audio, input, and performance settings
 * - Settings persistence in localStorage per core
 *
 * @component
 */
<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from "vue";
import { useDisplay } from "vuetify";
import type { StateSchema } from "@/__generated__";
import type { DetailedRom } from "@/stores/roms";
import AssetCard from "@/components/common/Game/AssetCard.vue";

/** Component props */
const props = defineProps<{
  /** ROM data for display in menu bar */
  rom: DetailedRom;
  /** Current libretro core name */
  core: string;
  /** Whether player is in fullscreen mode */
  isFullscreen: boolean;
  /** Container element for attaching dialogs (needed for fullscreen) */
  container?: HTMLElement;
  /** Whether the player is rotated (horizontal core in portrait fullscreen) */
  needsRotation?: boolean;
}>();

/** Vuetify display breakpoints */
const { mdAndUp } = useDisplay();

/** Portrait mode detection */
const isPortrait = ref(window.innerHeight > window.innerWidth);

function updateOrientation() {
  isPortrait.value = window.innerHeight > window.innerWidth;
}

/** Component events */
const emit = defineEmits<{
  /** Toggle fullscreen mode */
  fullscreen: [];
  /** Create a quick save state */
  quickSave: [];
  /** Load the last quick save state */
  quickLoad: [];
  /** Load a specific state by ID */
  loadState: [stateId: number];
  /** Save state and exit to game details */
  saveAndQuit: [];
  /** Restart the emulated game */
  restart: [];
  /** Take a screenshot */
  screenshot: [];
  /** Toggle pause state */
  togglePause: [];
  /** Settings were changed (triggers sync to backend) */
  settingsChanged: [settings: typeof settings.value];
  /** Exit player without saving */
  exit: [];
  /** Toggle rotation override */
  toggleRotation: [];
}>();

// Menu visibility state
/** Whether the main menu modal is open */
const showMenu = ref(false);
/** Whether the settings dialog is open */
const showSettings = ref(false);
/** Whether the load state dialog is open */
const showLoadStateDialog = ref(false);
/** Current settings tab (video, audio, input, performance, core) */
const settingsTab = ref("video");
/** Whether the top menu bar is visible */
const menuBarVisible = ref(true);
/** Whether the game is currently paused */
const isPaused = ref(false);
/** Timeout ID for auto-hiding the menu bar */
let hideMenuTimeout: number | null = null;

/**
 * RetroArch settings persisted in localStorage per core.
 * These control video, audio, input, and performance options.
 */
const settings = ref({
  // Video settings
  /** Aspect ratio mode: auto, 4:3, 16:9, 1:1, core */
  aspectRatio: "auto",
  /** Scale by integer multiples only */
  integerScale: false,
  /** Enable bilinear filtering for smooth scaling */
  bilinearFilter: true,
  /** Screen rotation in degrees: 0, 90, 180, 270 */
  rotation: 0,

  // Audio settings
  /** Volume level 0-100 */
  volume: 100,
  /** Enable audio output */
  audioEnable: true,

  // Performance settings
  /** Enable rewind feature (impacts performance) */
  rewindEnable: false,
  /** Frame skip count 0-10 */
  frameskip: 0,
  /** Fast forward speed multiplier 1.5-10.0 */
  fastForwardRatio: 2.0,

  // Input settings
  /** Analog stick dead zone 0-0.5 */
  analogDeadzone: 0.15,
  /** Show on-screen touch controls */
  inputOverlay: false,
});

/** Delay in ms before auto-hiding the menu bar */
const MENU_HIDE_DELAY_MS = 3000;

// Settings persistence (localStorage per core)
function getStorageKey() {
  return `retroarch-settings-${props.core}`;
}

function loadSettings() {
  try {
    const stored = localStorage.getItem(getStorageKey());
    if (stored) {
      const parsed = JSON.parse(stored);
      Object.assign(settings.value, parsed);
      console.log(`[RetroArch] Loaded settings for core ${props.core}:`, settings.value);
    }
  } catch (err) {
    console.error("[RetroArch] Failed to load settings:", err);
  }
}

function saveSettings() {
  try {
    localStorage.setItem(getStorageKey(), JSON.stringify(settings.value));
  } catch (err) {
    console.error("[RetroArch] Failed to save settings:", err);
  }
}

watch(
  settings,
  (newSettings: typeof settings.value) => {
    saveSettings();
    emit("settingsChanged", newSettings);
  },
  { deep: true }
);

function toggleMenu() {
  showMenu.value = !showMenu.value;
  if (showMenu.value) {
    showMenuBar();
    clearHideMenuTimeout();
  }
}

function closeMenu() {
  showMenu.value = false;
  showSettings.value = false;
}

function handleQuickSave() {
  emit("quickSave");
  closeMenu();
}

function handleQuickLoad() {
  // Open the load state dialog to let user choose which state to load
  showLoadStateDialog.value = true;
  showMenu.value = false;
}

function handleStateSelected(state: StateSchema) {
  emit("loadState", state.id);
  showLoadStateDialog.value = false;
}

function closeLoadStateDialog() {
  showLoadStateDialog.value = false;
}

function handleRestart() {
  emit("restart");
  closeMenu();
}

function handleScreenshot() {
  emit("screenshot");
  closeMenu();
}

function handlePause() {
  isPaused.value = !isPaused.value;
  emit("togglePause");
  closeMenu();
}

function handleSaveAndQuit() {
  emit("saveAndQuit");
}

function handleFullscreen() {
  emit("fullscreen");
}

function handleExit() {
  emit("exit");
}

function showMenuBar() {
  menuBarVisible.value = true;
  resetHideMenuTimeout();
}

function hideMenuBar() {
  // Don't hide if any dialog is open
  if (showMenu.value || showSettings.value || showLoadStateDialog.value) return;
  menuBarVisible.value = false;
}

function resetHideMenuTimeout() {
  clearHideMenuTimeout();
  hideMenuTimeout = window.setTimeout(() => {
    hideMenuBar();
  }, MENU_HIDE_DELAY_MS);
}

function clearHideMenuTimeout() {
  if (hideMenuTimeout !== null) {
    clearTimeout(hideMenuTimeout);
    hideMenuTimeout = null;
  }
}

function handleMouseMove() {
  showMenuBar();
}

onMounted(() => {
  loadSettings();
  resetHideMenuTimeout();
  window.addEventListener("resize", updateOrientation);
});

onUnmounted(() => {
  clearHideMenuTimeout();
  window.removeEventListener("resize", updateOrientation);
});

// Expose handler to parent component
defineExpose({
  handleMouseMove,
});
</script>

<template>
  <!-- Top Menu Bar -->
  <div class="menu-bar" :class="{ visible: menuBarVisible || showMenu || showSettings }">
    <div class="menu-bar-left">
      <v-btn
        icon="mdi-menu"
        size="small"
        variant="text"
        color="white"
        @click="toggleMenu"
      />
      <span class="game-title">{{ rom.name }}</span>
      <v-chip size="x-small" color="primary" class="ml-2">{{ core }}</v-chip>
    </div>

    <div class="menu-bar-right">
      <template v-if="!isPortrait">
        <!-- Hidden in portrait mode - accessible via menu -->
        <v-btn
          :icon="isPaused ? 'mdi-play' : 'mdi-pause'"
          size="small"
          variant="text"
          color="white"
          :title="isPaused ? 'Resume' : 'Pause'"
          @click="handlePause"
        />
        <v-btn
          icon="mdi-restart"
          size="small"
          variant="text"
          color="white"
          title="Restart"
          @click="handleRestart"
        />
        <v-btn
          icon="mdi-camera"
          size="small"
          variant="text"
          color="white"
          title="Screenshot"
          @click="handleScreenshot"
        />
        <v-btn
          icon="mdi-content-save"
          size="small"
          variant="text"
          color="white"
          title="Quick Save (F2)"
          @click="handleQuickSave"
        />
        <v-btn
          icon="mdi-folder-open"
          size="small"
          variant="text"
          color="white"
          title="Quick Load (F4)"
          @click="handleQuickLoad"
        />
        <v-btn
          :icon="isFullscreen ? 'mdi-fullscreen-exit' : 'mdi-fullscreen'"
          size="small"
          variant="text"
          color="white"
          :title="isFullscreen ? 'Exit Fullscreen (F11)' : 'Fullscreen (F11)'"
          @click="handleFullscreen"
        />
        <v-btn
          icon="mdi-cog"
          size="small"
          variant="text"
          color="white"
          title="Settings"
          @click="showSettings = !showSettings"
        />
        <v-btn
          icon="mdi-content-save-move"
          size="small"
          variant="text"
          color="white"
          title="Save & Quit"
          @click="handleSaveAndQuit"
        />
        <v-btn
          icon="mdi-close"
          size="small"
          variant="text"
          color="white"
          title="Exit (ESC)"
          @click="handleExit"
        />
      </template>
    </div>
  </div>

  <!-- Main Menu Modal -->
  <v-dialog v-model="showMenu" max-width="500" :attach="container" :content-class="needsRotation ? 'dialog-rotated' : ''" @click:outside="closeMenu">
    <v-card class="menu-card">
      <v-card-title class="d-flex align-center pa-4">
        <v-icon class="mr-2">mdi-menu</v-icon>
        Menu
        <v-spacer />
        <v-btn icon="mdi-close" variant="text" size="small" @click="closeMenu" />
      </v-card-title>

      <v-divider />

      <v-list>
        <v-list-item :prepend-icon="isPaused ? 'mdi-play' : 'mdi-pause'" @click="handlePause">
          <v-list-item-title>{{ isPaused ? 'Resume' : 'Pause' }}</v-list-item-title>
        </v-list-item>

        <v-list-item prepend-icon="mdi-restart" @click="handleRestart">
          <v-list-item-title>Restart Game</v-list-item-title>
        </v-list-item>

        <v-list-item prepend-icon="mdi-camera" @click="handleScreenshot">
          <v-list-item-title>Take Screenshot</v-list-item-title>
        </v-list-item>

        <v-divider class="my-2" />

        <v-list-item prepend-icon="mdi-content-save" @click="handleQuickSave">
          <v-list-item-title>Quick Save</v-list-item-title>
          <template #append>
            <v-chip size="x-small" variant="outlined">F2</v-chip>
          </template>
        </v-list-item>

        <v-list-item prepend-icon="mdi-folder-open" @click="handleQuickLoad">
          <v-list-item-title>Quick Load</v-list-item-title>
          <template #append>
            <v-chip size="x-small" variant="outlined">F4</v-chip>
          </template>
        </v-list-item>

        <v-divider class="my-2" />

        <v-list-item prepend-icon="mdi-cog" @click="showSettings = true; showMenu = false">
          <v-list-item-title>Settings</v-list-item-title>
        </v-list-item>

        <v-list-item :prepend-icon="isFullscreen ? 'mdi-fullscreen-exit' : 'mdi-fullscreen'" @click="handleFullscreen(); closeMenu()">
          <v-list-item-title>{{ isFullscreen ? 'Exit Fullscreen' : 'Fullscreen' }}</v-list-item-title>
          <template #append>
            <v-chip size="x-small" variant="outlined">F11</v-chip>
          </template>
        </v-list-item>

        <v-list-item
          v-if="isPortrait"
          :prepend-icon="needsRotation ? 'mdi-screen-rotation-lock' : 'mdi-screen-rotation'"
          @click="emit('toggleRotation'); closeMenu()"
        >
          <v-list-item-title>{{ needsRotation ? 'Disable Rotation' : 'Enable Rotation' }}</v-list-item-title>
        </v-list-item>

        <v-divider class="my-2" />

        <v-list-item prepend-icon="mdi-content-save-move" @click="handleSaveAndQuit">
          <v-list-item-title>Save & Quit</v-list-item-title>
        </v-list-item>

        <v-list-item
          prepend-icon="mdi-arrow-left"
          @click="handleExit"
          class="text-error"
        >
          <v-list-item-title>Exit to Game Details</v-list-item-title>
        </v-list-item>
      </v-list>
    </v-card>
  </v-dialog>

  <!-- Settings Modal -->
  <v-dialog v-model="showSettings" max-width="800" :attach="container" :content-class="needsRotation ? 'dialog-rotated' : ''" @click:outside="closeMenu">
    <v-card>
      <v-card-title class="d-flex align-center pa-4">
        <v-icon class="mr-2">mdi-cog</v-icon>
        Settings
        <v-spacer />
        <v-btn icon="mdi-close" variant="text" size="small" @click="showSettings = false" />
      </v-card-title>

      <v-divider />

      <v-card-text class="pa-0">
        <v-tabs v-model="settingsTab" bg-color="transparent" color="primary">
          <v-tab value="video">
            <v-icon start>mdi-monitor</v-icon>
            Video
          </v-tab>
          <v-tab value="audio">
            <v-icon start>mdi-volume-high</v-icon>
            Audio
          </v-tab>
          <v-tab value="input">
            <v-icon start>mdi-gamepad-variant</v-icon>
            Input
          </v-tab>
          <v-tab value="performance">
            <v-icon start>mdi-speedometer</v-icon>
            Performance
          </v-tab>
        </v-tabs>

        <v-divider />

        <v-window v-model="settingsTab" class="pa-4">
          <!-- Video Settings -->
          <v-window-item value="video">
            <v-list density="compact">
              <!-- Aspect Ratio -->
              <v-list-item>
                <v-list-item-title>Aspect Ratio</v-list-item-title>
                <v-list-item-subtitle>Display aspect ratio</v-list-item-subtitle>
                <template #append>
                  <v-select
                    v-model="settings.aspectRatio"
                    :items="['auto', '4:3', '16:9', '16:10', '1:1', 'core']"
                    variant="outlined"
                    density="compact"
                    hide-details
                    style="width: 120px"
                  />
                </template>
              </v-list-item>

              <!-- Integer Scale -->
              <v-list-item>
                <v-list-item-title>Integer Scale</v-list-item-title>
                <v-list-item-subtitle>Scale by integer multiples only</v-list-item-subtitle>
                <template #append>
                  <v-switch
                    v-model="settings.integerScale"
                    color="primary"
                    hide-details
                    density="compact"
                  />
                </template>
              </v-list-item>

              <!-- Bilinear Filter -->
              <v-list-item>
                <v-list-item-title>Bilinear Filtering</v-list-item-title>
                <v-list-item-subtitle>Smooth scaling filter</v-list-item-subtitle>
                <template #append>
                  <v-switch
                    v-model="settings.bilinearFilter"
                    color="primary"
                    hide-details
                    density="compact"
                  />
                </template>
              </v-list-item>

              <!-- Rotation -->
              <v-list-item>
                <v-list-item-title>Screen Rotation</v-list-item-title>
                <v-list-item-subtitle>Rotate display output</v-list-item-subtitle>
                <template #append>
                  <v-select
                    v-model="settings.rotation"
                    :items="[
                      { title: 'Normal', value: 0 },
                      { title: '90°', value: 90 },
                      { title: '180°', value: 180 },
                      { title: '270°', value: 270 },
                    ]"
                    variant="outlined"
                    density="compact"
                    hide-details
                    style="width: 120px"
                  />
                </template>
              </v-list-item>
            </v-list>
          </v-window-item>

          <!-- Audio Settings -->
          <v-window-item value="audio">
            <v-list density="compact">
              <!-- Audio Enable -->
              <v-list-item>
                <v-list-item-title>Enable Audio</v-list-item-title>
                <v-list-item-subtitle>Toggle audio output</v-list-item-subtitle>
                <template #append>
                  <v-switch
                    v-model="settings.audioEnable"
                    color="primary"
                    hide-details
                    density="compact"
                  />
                </template>
              </v-list-item>

              <!-- Volume -->
              <v-list-item>
                <v-list-item-title>Volume</v-list-item-title>
                <v-list-item-subtitle>Audio volume level ({{ settings.volume }}%)</v-list-item-subtitle>
                <template #append>
                  <v-slider
                    v-model="settings.volume"
                    min="0"
                    max="100"
                    step="5"
                    hide-details
                    color="primary"
                    thumb-label
                    style="width: 200px"
                    :disabled="!settings.audioEnable"
                  />
                </template>
              </v-list-item>
            </v-list>
          </v-window-item>

          <!-- Input Settings -->
          <v-window-item value="input">
            <v-list density="compact">
              <!-- Analog Deadzone -->
              <v-list-item>
                <v-list-item-title>Analog Deadzone</v-list-item-title>
                <v-list-item-subtitle>Gamepad analog stick deadzone ({{ (settings.analogDeadzone * 100).toFixed(0) }}%)</v-list-item-subtitle>
                <template #append>
                  <v-slider
                    v-model="settings.analogDeadzone"
                    min="0"
                    max="0.5"
                    step="0.05"
                    hide-details
                    color="primary"
                    thumb-label
                    style="width: 200px"
                  />
                </template>
              </v-list-item>

              <!-- Input Overlay -->
              <v-list-item>
                <v-list-item-title>Input Overlay (Mobile)</v-list-item-title>
                <v-list-item-subtitle>Show on-screen controls for touchscreen</v-list-item-subtitle>
                <template #append>
                  <v-switch
                    v-model="settings.inputOverlay"
                    color="primary"
                    hide-details
                    density="compact"
                  />
                </template>
              </v-list-item>

              <!-- Controller Mapping -->
              <v-list-item>
                <v-list-item-title>Controller Mapping</v-list-item-title>
                <v-list-item-subtitle>Configure button layout</v-list-item-subtitle>
                <template #append>
                  <v-btn size="small" variant="tonal" disabled>
                    Configure
                  </v-btn>
                </template>
              </v-list-item>
            </v-list>
          </v-window-item>

          <!-- Performance Settings -->
          <v-window-item value="performance">
            <v-list density="compact">
              <!-- Rewind -->
              <v-list-item>
                <v-list-item-title>Rewind</v-list-item-title>
                <v-list-item-subtitle>Enable state rewinding (impacts performance)</v-list-item-subtitle>
                <template #append>
                  <v-switch
                    v-model="settings.rewindEnable"
                    color="primary"
                    hide-details
                    density="compact"
                  />
                </template>
              </v-list-item>

              <!-- Frameskip -->
              <v-list-item>
                <v-list-item-title>Frameskip</v-list-item-title>
                <v-list-item-subtitle>Skip frames for better performance ({{ settings.frameskip }})</v-list-item-subtitle>
                <template #append>
                  <v-slider
                    v-model="settings.frameskip"
                    min="0"
                    max="10"
                    step="1"
                    hide-details
                    color="primary"
                    thumb-label
                    style="width: 200px"
                  />
                </template>
              </v-list-item>

              <!-- Fast Forward Ratio -->
              <v-list-item>
                <v-list-item-title>Fast Forward Ratio</v-list-item-title>
                <v-list-item-subtitle>Speed multiplier for fast forward ({{ settings.fastForwardRatio.toFixed(1) }}x)</v-list-item-subtitle>
                <template #append>
                  <v-slider
                    v-model="settings.fastForwardRatio"
                    min="1.5"
                    max="10"
                    step="0.5"
                    hide-details
                    color="primary"
                    thumb-label
                    style="width: 200px"
                  />
                </template>
              </v-list-item>
            </v-list>
          </v-window-item>
        </v-window>
      </v-card-text>
    </v-card>
  </v-dialog>

  <!-- Load State Dialog -->
  <v-dialog
    v-model="showLoadStateDialog"
    :width="mdAndUp ? '60vw' : '95vw'"
    max-width="800"
    :attach="container"
    :content-class="needsRotation ? 'dialog-rotated' : ''"
    @click:outside="closeLoadStateDialog"
  >
    <v-card>
      <v-card-title class="d-flex align-center pa-4">
        <v-icon class="mr-2">mdi-folder-open</v-icon>
        Load State
        <v-spacer />
        <v-btn icon="mdi-close" variant="text" size="small" @click="closeLoadStateDialog" />
      </v-card-title>

      <v-divider />

      <v-card-text class="pa-4" style="max-height: 60vh; overflow-y: auto;">
        <v-row v-if="rom.user_states && rom.user_states.length > 0" no-gutters>
          <v-col
            v-for="state in rom.user_states"
            :key="state.id"
            cols="6"
            sm="4"
            md="3"
            class="pa-1"
          >
            <AssetCard
              :asset="state"
              type="state"
              :rom="rom"
              :show-hover-actions="false"
              @click="handleStateSelected(state)"
            />
          </v-col>
        </v-row>
        <div v-else class="text-center py-8">
          <v-icon size="64" color="grey">mdi-file-question-outline</v-icon>
          <p class="text-h6 mt-4 text-grey">No save states found</p>
          <p class="text-body-2 text-grey">Create a save state first using Quick Save</p>
        </div>
      </v-card-text>

      <v-divider />

      <v-card-actions class="justify-center pa-3">
        <v-btn variant="tonal" @click="closeLoadStateDialog">
          Cancel
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.menu-bar {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 48px;
  background: linear-gradient(to bottom, rgba(0, 0, 0, 0.7), transparent);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  z-index: 100;
  opacity: 0;
  transition: opacity 0.3s ease;
  pointer-events: none;
}

.menu-bar:hover,
.menu-bar.visible {
  opacity: 1;
  pointer-events: all;
}

.menu-bar-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.menu-bar-right {
  display: flex;
  align-items: center;
  gap: 4px;
}

.game-title {
  color: white;
  font-size: 14px;
  font-weight: 500;
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.5);
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.menu-card {
  background: rgba(var(--v-theme-surface));
}

.settings-slider {
  margin-top: 0 !important;
}

/* Show menu bar on hover at top of screen */
.retroarch-container:hover .menu-bar {
  opacity: 1;
  pointer-events: all;
}

/* Always show menu bar when menu/settings is open */
.menu-bar.menu-open {
  opacity: 1;
  pointer-events: all;
}
</style>

<!-- Global styles for dialog rotation (not scoped because dialogs render outside component) -->
<style>
/* Rotate dialogs when player is in rotated fullscreen mode */
.dialog-rotated {
  rotate: 90deg;
  /* Swap width/height constraints for rotated view */
  width: 90vh !important;
}

.dialog-rotated .v-card {
  max-height: 75vw;
  overflow-y: auto;
}
</style>
<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from "vue";
import type { DetailedRom } from "@/stores/roms";

const props = defineProps<{
  rom: DetailedRom;
  core: string;
  isFullscreen: boolean;
  coreOptionsFromBackend: Record<string, string>; // Core options loaded from backend
}>();

const emit = defineEmits<{
  fullscreen: [];
  quickSave: [];
  quickLoad: [];
  saveAndQuit: [];
  restart: [];
  screenshot: [];
  togglePause: [];
  settingsChanged: [settings: typeof settings.value];
  exit: [];
}>();

const showMenu = ref(false);
const showSettings = ref(false);
const settingsTab = ref("video");
const menuBarVisible = ref(true);
const isPaused = ref(false);
let hideMenuTimeout: number | null = null;

// RetroArch settings (persisted in localStorage per core)
const settings = ref({
  // Video
  aspectRatio: "auto", // auto, 4:3, 16:9, 1:1, core-provided
  integerScale: false,
  bilinearFilter: true,
  rotation: 0, // 0, 90, 180, 270

  // Audio
  volume: 100,
  audioEnable: true,

  // Performance
  rewindEnable: false,
  frameskip: 0, // 0-10
  fastForwardRatio: 2.0, // 1.5-10.0

  // Input
  analogDeadzone: 0.15,
  inputOverlay: false,
});

// Core-specific options from config (EJS_SETTINGS)
const coreOptions = ref<Record<string, string | boolean>>({});

const MENU_HIDE_DELAY_MS = 3000; // Hide menu after 3 seconds of inactivity

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
    console.log(`[RetroArch] Saved settings for core ${props.core}:`, settings.value);
  } catch (err) {
    console.error("[RetroArch] Failed to save settings:", err);
  }
}

// Core options persistence (localStorage per core)
function getCoreOptionsStorageKey() {
  return `retroarch-core-options-${props.core}`;
}

function loadCoreOptions() {
  try {
    console.log(`[RetroArch] Loading core options from backend for ${props.core}:`, props.coreOptionsFromBackend);

    // Try to load saved overrides from localStorage
    const stored = localStorage.getItem(getCoreOptionsStorageKey());
    if (stored) {
      try {
        const parsed = JSON.parse(stored);
        // Merge backend options with localStorage overrides
        coreOptions.value = { ...props.coreOptionsFromBackend, ...parsed };
        console.log(`[RetroArch] Merged with localStorage overrides:`, coreOptions.value);
      } catch (err) {
        // If parsing fails, just use backend options
        coreOptions.value = props.coreOptionsFromBackend;
      }
    } else {
      // Use options from backend directly
      coreOptions.value = props.coreOptionsFromBackend;
    }

    console.log(`[RetroArch] Final core options loaded (${Object.keys(coreOptions.value).length} options):`, coreOptions.value);
  } catch (err) {
    console.error("[RetroArch] Failed to load core options:", err);
    coreOptions.value = {};
  }
}

function saveCoreOptions() {
  try {
    localStorage.setItem(getCoreOptionsStorageKey(), JSON.stringify(coreOptions.value));
    console.log(`[RetroArch] Saved core options for ${props.core}:`, coreOptions.value);
  } catch (err) {
    console.error("[RetroArch] Failed to save core options:", err);
  }
}

// Watch settings changes and persist + emit
watch(
  settings,
  (newSettings: typeof settings.value) => {
    saveSettings();
    emit("settingsChanged", newSettings);
  },
  { deep: true }
);

// Watch core options changes and persist + emit
watch(
  coreOptions,
  () => {
    saveCoreOptions();
    emit("settingsChanged", settings.value);
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
  emit("quickLoad");
  closeMenu();
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
  // Don't hide if menu or settings dialog is open
  if (showMenu.value || showSettings.value) return;
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
  // Load saved settings for this core
  loadSettings();
  // Load core-specific options
  loadCoreOptions();
  // Start the auto-hide timer on mount
  resetHideMenuTimeout();
});

onUnmounted(() => {
  clearHideMenuTimeout();
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
    </div>
  </div>

  <!-- Main Menu Modal -->
  <v-dialog v-model="showMenu" max-width="500" @click:outside="closeMenu">
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
  <v-dialog v-model="showSettings" max-width="600" @click:outside="closeMenu">
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
          <v-tab value="core" v-if="Object.keys(coreOptions).length > 0">
            <v-icon start>mdi-chip</v-icon>
            Core Options
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

          <!-- Core Options -->
          <v-window-item value="core" v-if="Object.keys(coreOptions).length > 0">
            <v-list density="compact">
              <v-list-subheader class="text-caption">
                {{ core }} specific options
              </v-list-subheader>

              <!-- Dynamic core options -->
              <v-list-item
                v-for="(value, key) in coreOptions"
                :key="key"
              >
                <v-list-item-title>{{ key }}</v-list-item-title>
                <template #append>
                  <!-- Boolean option -->
                  <v-switch
                    v-if="typeof value === 'boolean'"
                    v-model="coreOptions[key]"
                    color="primary"
                    hide-details
                    density="compact"
                  />
                  <!-- String option -->
                  <v-text-field
                    v-else
                    v-model="coreOptions[key]"
                    variant="outlined"
                    density="compact"
                    hide-details
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
"""
RetroArch Save/State Synchronization Service

Handles synchronization of saves and states between RetroArch session
directories and the RomM database/filesystem.
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from config import ASSETS_BASE_PATH
from handler.database import db_save_handler, db_state_handler, db_user_handler
from handler.database import db_screenshot_handler
from handler.filesystem.assets_handler import FSAssetsHandler
from models.assets import Save, State, Screenshot

logger = logging.getLogger(__name__)

fs_assets_handler = FSAssetsHandler()


def _get_rom_name_from_path(rom_path: str) -> str:
    """Extract ROM name without extension from path."""
    return Path(rom_path).stem


def restore_save_to_session(
    save_id: int,
    user_id: int,
    session_saves_dir: Path,
    rom_path: str,
) -> bool:
    """Restore a save file from RomM to the session directory.

    Args:
        save_id: ID of the save to restore.
        user_id: User ID owning the save.
        session_saves_dir: Session's saves directory.
        rom_path: Path to the ROM file (used to determine save filename).

    Returns:
        True if save was restored successfully, False otherwise.
    """
    try:
        save = db_save_handler.get_save(user_id=user_id, id=save_id)
        if not save:
            logger.warning(f"Save {save_id} not found for user {user_id}")
            return False

        source_path = Path(ASSETS_BASE_PATH) / save.full_path
        if not source_path.exists():
            logger.warning(f"Save file not found: {source_path}")
            return False

        # RetroArch expects save files named after the ROM
        rom_name = _get_rom_name_from_path(rom_path)
        dest_filename = f"{rom_name}{save.file_extension}"
        dest_path = session_saves_dir / dest_filename

        shutil.copy2(source_path, dest_path)
        logger.info(f"Restored save {save_id} to {dest_path}")
        return True

    except Exception as e:
        logger.error(f"Failed to restore save {save_id}: {e}")
        return False


def restore_state_to_session(
    state_id: int,
    user_id: int,
    session_states_dir: Path,
    rom_path: str,
) -> Optional[str]:
    """Restore a state file from RomM to the session directory.

    Args:
        state_id: ID of the state to restore.
        user_id: User ID owning the state.
        session_states_dir: Session's states directory.
        rom_path: Path to the ROM file (used to determine state filename).

    Returns:
        Path to the restored state file for RetroArch to load, or None on failure.
    """
    try:
        state = db_state_handler.get_state(user_id=user_id, id=state_id)
        if not state:
            logger.warning(f"State {state_id} not found for user {user_id}")
            return None

        source_path = Path(ASSETS_BASE_PATH) / state.full_path

        if not source_path.exists():
            logger.warning(f"State file not found: {source_path}")
            return None

        # Preserve the original extension from the saved state
        # RetroArch uses .state, .state0, .state1, etc. depending on slot
        rom_name = _get_rom_name_from_path(rom_path)
        original_ext = state.file_extension  # e.g., ".state0", ".state", etc.
        dest_filename = f"{rom_name}{original_ext}"
        dest_path = session_states_dir / dest_filename

        shutil.copy2(source_path, dest_path)

        dest_size = dest_path.stat().st_size
        logger.info(f"Restored state {state_id} to {dest_path} ({dest_size} bytes)")
        return str(dest_path)

    except Exception as e:
        logger.error(f"Failed to restore state {state_id}: {e}")
        return None


def sync_saves_to_romm(
    session_saves_dir: Path,
    user_id: int,
    rom_id: int,
    platform_slug: str,
    emulator: str,
) -> int:
    """Sync all saves from session directory to RomM.

    Args:
        session_saves_dir: Session's saves directory.
        user_id: User ID to associate saves with.
        rom_id: ROM ID to associate saves with.
        platform_slug: Platform slug for file organization.
        emulator: Emulator/core name.

    Returns:
        Number of saves synced.
    """
    synced_count = 0

    try:
        if not session_saves_dir.exists():
            return 0

        user = db_user_handler.get_user(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return 0

        # Get destination path in RomM assets
        dest_rel_path = fs_assets_handler.build_saves_file_path(
            user=user,
            platform_fs_slug=platform_slug,
            rom_id=rom_id,
            emulator=emulator,
        )
        dest_abs_path = Path(ASSETS_BASE_PATH) / dest_rel_path
        dest_abs_path.mkdir(parents=True, exist_ok=True)

        for save_file in session_saves_dir.iterdir():
            if not save_file.is_file():
                continue

            try:
                dest_file = dest_abs_path / save_file.name
                shutil.copy2(save_file, dest_file)

                # Check if save already exists in DB
                existing_save = db_save_handler.get_save_by_filename(
                    user_id=user_id,
                    rom_id=rom_id,
                    file_name=save_file.name,
                )

                file_size = save_file.stat().st_size
                file_ext = save_file.suffix
                file_name_no_ext = save_file.stem

                if existing_save:
                    # Update existing save
                    db_save_handler.update_save(
                        id=existing_save.id,
                        data={
                            "file_size_bytes": file_size,
                            "missing_from_fs": False,
                        },
                    )
                    logger.debug(f"Updated save: {save_file.name}")
                else:
                    # Create new save record
                    new_save = Save(
                        file_name=save_file.name,
                        file_name_no_tags=save_file.name,
                        file_name_no_ext=file_name_no_ext,
                        file_extension=file_ext,
                        file_path=dest_rel_path,
                        file_size_bytes=file_size,
                        rom_id=rom_id,
                        user_id=user_id,
                        emulator=emulator,
                    )
                    db_save_handler.add_save(new_save)
                    logger.debug(f"Created new save: {save_file.name}")

                synced_count += 1

            except Exception as e:
                logger.error(f"Failed to sync save {save_file.name}: {e}")

        if synced_count > 0:
            logger.info(f"Synced {synced_count} saves to RomM for ROM {rom_id}")

    except Exception as e:
        logger.error(f"Failed to sync saves: {e}")

    return synced_count


def sync_states_to_romm(
    session_states_dir: Path,
    user_id: int,
    rom_id: int,
    platform_slug: str,
    emulator: str,
) -> int:
    """Sync all states from session directory to RomM.

    Args:
        session_states_dir: Session's states directory.
        user_id: User ID to associate states with.
        rom_id: ROM ID to associate states with.
        platform_slug: Platform slug for file organization.
        emulator: Emulator/core name.

    Returns:
        Number of states synced.
    """
    synced_count = 0

    try:
        if not session_states_dir.exists():
            return 0

        user = db_user_handler.get_user(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return 0

        # Get destination path in RomM assets
        dest_rel_path = fs_assets_handler.build_states_file_path(
            user=user,
            platform_fs_slug=platform_slug,
            rom_id=rom_id,
            emulator=emulator,
        )
        dest_abs_path = Path(ASSETS_BASE_PATH) / dest_rel_path
        dest_abs_path.mkdir(parents=True, exist_ok=True)

        for state_file in session_states_dir.iterdir():
            if not state_file.is_file():
                continue

            # Only sync .state files (RetroArch save states)
            if not state_file.suffix.startswith(".state"):
                continue

            try:
                dest_file = dest_abs_path / state_file.name
                shutil.copy2(state_file, dest_file)

                # Check if state already exists in DB
                existing_state = db_state_handler.get_state_by_filename(
                    user_id=user_id,
                    rom_id=rom_id,
                    file_name=state_file.name,
                )

                file_size = state_file.stat().st_size
                file_ext = state_file.suffix
                file_name_no_ext = state_file.stem

                if existing_state:
                    # Update existing state
                    db_state_handler.update_state(
                        id=existing_state.id,
                        data={
                            "file_size_bytes": file_size,
                            "missing_from_fs": False,
                        },
                    )
                    logger.debug(f"Updated state: {state_file.name}")
                else:
                    # Create new state record
                    new_state = State(
                        file_name=state_file.name,
                        file_name_no_tags=state_file.name,
                        file_name_no_ext=file_name_no_ext,
                        file_extension=file_ext,
                        file_path=dest_rel_path,
                        file_size_bytes=file_size,
                        rom_id=rom_id,
                        user_id=user_id,
                        emulator=emulator,
                    )
                    db_state_handler.add_state(new_state)
                    logger.debug(f"Created new state: {state_file.name}")

                synced_count += 1

            except Exception as e:
                logger.error(f"Failed to sync state {state_file.name}: {e}")

        if synced_count > 0:
            logger.info(f"Synced {synced_count} states to RomM for ROM {rom_id}")

    except Exception as e:
        logger.error(f"Failed to sync states: {e}")

    return synced_count


def sync_screenshots_to_romm(
    session_screenshots_dir: Path,
    user_id: int,
    rom_id: int,
    platform_slug: str,
) -> int:
    """Sync all screenshots from session directory to RomM.

    Args:
        session_screenshots_dir: Session's screenshots directory.
        user_id: User ID to associate screenshots with.
        rom_id: ROM ID to associate screenshots with.
        platform_slug: Platform slug for file organization.

    Returns:
        Number of screenshots synced.
    """
    synced_count = 0

    try:
        if not session_screenshots_dir.exists():
            return 0

        user = db_user_handler.get_user(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return 0

        # Get destination path in RomM assets
        dest_rel_path = fs_assets_handler.build_screenshots_file_path(
            user=user,
            platform_fs_slug=platform_slug,
            rom_id=rom_id,
        )
        dest_abs_path = Path(ASSETS_BASE_PATH) / dest_rel_path
        dest_abs_path.mkdir(parents=True, exist_ok=True)

        for screenshot_file in session_screenshots_dir.iterdir():
            if not screenshot_file.is_file():
                continue

            # Only sync PNG files
            if screenshot_file.suffix.lower() != ".png":
                continue

            try:
                dest_file = dest_abs_path / screenshot_file.name
                shutil.copy2(screenshot_file, dest_file)

                # Check if screenshot already exists in DB
                existing_screenshot = db_screenshot_handler.get_screenshot(
                    filename=screenshot_file.name,
                    rom_id=rom_id,
                    user_id=user_id,
                )

                file_size = screenshot_file.stat().st_size
                file_ext = screenshot_file.suffix
                file_name_no_ext = screenshot_file.stem

                if existing_screenshot:
                    # Update existing screenshot
                    db_screenshot_handler.update_screenshot(
                        id=existing_screenshot.id,
                        data={
                            "file_size_bytes": file_size,
                            "missing_from_fs": False,
                        },
                    )
                    logger.debug(f"Updated screenshot: {screenshot_file.name}")
                else:
                    # Create new screenshot record
                    new_screenshot = Screenshot(
                        file_name=screenshot_file.name,
                        file_name_no_tags=screenshot_file.name,
                        file_name_no_ext=file_name_no_ext,
                        file_extension=file_ext,
                        file_path=dest_rel_path,
                        file_size_bytes=file_size,
                        rom_id=rom_id,
                        user_id=user_id,
                    )
                    db_screenshot_handler.add_screenshot(new_screenshot)
                    logger.debug(f"Created new screenshot: {screenshot_file.name}")

                synced_count += 1

            except Exception as e:
                logger.error(f"Failed to sync screenshot {screenshot_file.name}: {e}")

        if synced_count > 0:
            logger.info(
                f"Synced {synced_count} screenshots to RomM for ROM {rom_id}"
            )

    except Exception as e:
        logger.error(f"Failed to sync screenshots: {e}")

    return synced_count


def sync_session_to_romm(
    session_saves_dir: Path,
    session_states_dir: Path,
    session_screenshots_dir: Path,
    user_id: int,
    rom_id: int,
    platform_slug: str,
    emulator: str,
) -> tuple[int, int, int]:
    """Sync all saves, states, and screenshots from a session to RomM.

    Args:
        session_saves_dir: Session's saves directory.
        session_states_dir: Session's states directory.
        session_screenshots_dir: Session's screenshots directory.
        user_id: User ID to associate assets with.
        rom_id: ROM ID to associate assets with.
        platform_slug: Platform slug for file organization.
        emulator: Emulator/core name.

    Returns:
        Tuple of (saves_synced, states_synced, screenshots_synced).
    """
    saves_synced = sync_saves_to_romm(
        session_saves_dir=session_saves_dir,
        user_id=user_id,
        rom_id=rom_id,
        platform_slug=platform_slug,
        emulator=emulator,
    )

    states_synced = sync_states_to_romm(
        session_states_dir=session_states_dir,
        user_id=user_id,
        rom_id=rom_id,
        platform_slug=platform_slug,
        emulator=emulator,
    )

    screenshots_synced = sync_screenshots_to_romm(
        session_screenshots_dir=session_screenshots_dir,
        user_id=user_id,
        rom_id=rom_id,
        platform_slug=platform_slug,
    )

    return saves_synced, states_synced, screenshots_synced
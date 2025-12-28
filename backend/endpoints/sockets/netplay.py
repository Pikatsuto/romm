import asyncio
from typing import Any, NotRequired, TypedDict

from endpoints.netplay import DEFAULT_MAX_PLAYERS
from handler.netplay_handler import NetplayPlayerInfo, NetplayRoom, netplay_handler
from handler.socket_handler import netplay_socket_handler


class RoomDataExtra(TypedDict):
    sessionid: str | None
    userid: str | None
    playerId: str | None
    room_name: NotRequired[str]
    game_id: NotRequired[str]
    domain: NotRequired[str]
    player_name: NotRequired[str]
    room_password: NotRequired[str]


class RoomData(TypedDict):
    extra: RoomDataExtra
    maxPlayers: NotRequired[int]


@netplay_socket_handler.socket_server.on("open-room")  # type: ignore
async def open_room(sid: str, data: RoomData):
    extra_data = data["extra"]

    session_id = extra_data.get("sessionid")
    player_id = extra_data.get("userid") or extra_data.get("playerId")

    if not session_id or not player_id:
        return "Invalid data: sessionId and playerId required"

    if await netplay_handler.get(session_id):
        return "Room already exists"

    new_room = NetplayRoom(
        owner=sid,
        players={
            player_id: NetplayPlayerInfo(
                socketId=sid,
                player_name=extra_data.get("player_name") or f"Player {player_id}",
                userid=extra_data.get("userid"),
                playerId=extra_data.get("playerId"),
            )
        },
        peers=[],
        room_name=extra_data.get("room_name") or f"Room {session_id}",
        game_id=extra_data.get("game_id") or "default",
        domain=extra_data.get("domain", None),
        password=extra_data.get("room_password", None),
        max_players=data.get("maxPlayers") or DEFAULT_MAX_PLAYERS,
    )
    await netplay_handler.set(session_id, new_room)

    await netplay_socket_handler.socket_server.enter_room(sid, session_id)
    await netplay_socket_handler.socket_server.save_session(
        sid,
        {
            "session_id": session_id,
            "player_id": player_id,
        },
    )
    await netplay_socket_handler.socket_server.emit(
        "users-updated", new_room["players"], room=session_id
    )


@netplay_socket_handler.socket_server.on("join-room")  # type: ignore
async def join_room(sid: str, data: RoomData):
    extra_data = data["extra"]

    session_id = extra_data.get("sessionid")
    player_id = extra_data.get("userid") or extra_data.get("playerId")

    if not session_id or not player_id:
        return "Invalid data: sessionId and playerId required"

    current_room = await netplay_handler.get(session_id)
    if not current_room:
        return "Room not found"

    if current_room["password"] and current_room["password"] != extra_data.get(
        "room_password"
    ):
        return "Incorrect password"

    if len(current_room["players"].keys()) >= current_room["max_players"]:
        return "Room is full"

    current_room["players"][player_id] = NetplayPlayerInfo(
        socketId=sid,
        player_name=extra_data.get("player_name") or f"Player {player_id}",
        userid=extra_data.get("userid"),
        playerId=extra_data.get("playerId"),
    )
    await netplay_handler.set(session_id, current_room)

    await netplay_socket_handler.socket_server.enter_room(sid, session_id)
    await netplay_socket_handler.socket_server.save_session(
        sid,
        {
            "session_id": session_id,
            "player_id": player_id,
        },
    )
    await netplay_socket_handler.socket_server.emit(
        "users-updated", current_room["players"], room=session_id
    )

    return None, current_room["players"]


async def _handle_leave(sid: str, session_id: str, player_id: str):
    current_room = await netplay_handler.get(session_id)
    if not current_room:
        return

    current_room["players"].pop(player_id, None)

    if not current_room["players"]:
        await netplay_handler.delete([session_id])
        # Notify clients that the room is now empty
        await netplay_socket_handler.socket_server.emit(
            "users-updated", {}, room=session_id
        )
        return

    if sid == current_room["owner"]:
        # Owner left, assign a new one
        remaining_players = list(current_room["players"].values())
        if remaining_players:
            current_room["owner"] = remaining_players[0]["socketId"]

    await netplay_handler.set(session_id, current_room)
    await netplay_socket_handler.socket_server.emit(
        "users-updated", current_room["players"], room=session_id
    )


@netplay_socket_handler.socket_server.on("leave-room")  # type: ignore
async def leave_room(sid: str):
    stored_session = await netplay_socket_handler.socket_server.get_session(sid)
    session_id = stored_session.get("session_id")
    player_id = stored_session.get("player_id")

    if session_id and player_id:
        await _handle_leave(sid, session_id, player_id)
        await netplay_socket_handler.socket_server.leave_room(sid, session_id)


class WebRTCSignalData(TypedDict, total=False):
    target: str
    candidate: Any
    offer: Any
    answer: Any
    requestRenegotiate: bool


@netplay_socket_handler.socket_server.on("webrtc-signal")  # type: ignore
async def webrtc_signal(sid: str, data: WebRTCSignalData):
    target = data.get("target")
    request_renegotiate = data.get("requestRenegotiate", False)

    if request_renegotiate:
        if not target:
            return
        await netplay_socket_handler.socket_server.emit(
            "webrtc-signal",
            {"sender": sid, "requestRenegotiate": True},
            to=target,
        )
    else:
        if not target:
            return  # drop message—no recipient
        await netplay_socket_handler.socket_server.emit(
            "webrtc-signal",
            {
                "sender": sid,
                "candidate": data.get("candidate"),
                "offer": data.get("offer"),
                "answer": data.get("answer"),
            },
            to=target,
        )


@netplay_socket_handler.socket_server.on("webrtc-signal-error")  # type: ignore
async def webrtc_signal_error(_sid: str, _error: str, _data: Any):
    pass


@netplay_socket_handler.socket_server.on("disconnect")  # type: ignore
async def disconnect(sid: str):
    stored_session = await netplay_socket_handler.socket_server.get_session(sid)
    session_id = stored_session.get("session_id")
    player_id = stored_session.get("player_id")

    if session_id and player_id:
        await _handle_leave(sid, session_id, player_id)


async def _broadcast_to_room(sid: str, event: str, data: Any):
    stored_session = await netplay_socket_handler.socket_server.get_session(sid)
    session_id = stored_session.get("session_id")
    if session_id:
        await netplay_socket_handler.socket_server.emit(
            event, data, room=session_id, skip_sid=sid
        )


@netplay_socket_handler.socket_server.on("data-message")  # type: ignore
async def data_message(sid: str, data: Any):
    await _broadcast_to_room(sid, "data-message", data)


@netplay_socket_handler.socket_server.on("snapshot")  # type: ignore
async def snapshot(sid: str, data: Any):
    await _broadcast_to_room(sid, "snapshot", data)


@netplay_socket_handler.socket_server.on("input")  # type: ignore
async def input(sid: str, data: Any):
    await _broadcast_to_room(sid, "input", data)


# RetroArch streaming events


@netplay_socket_handler.socket_server.on("retroarch-ice-candidate")  # type: ignore
async def retroarch_ice_candidate(sid: str, data: dict):
    """Handle ICE candidate exchange for RetroArch WebRTC streaming.

    This event is emitted by the frontend to exchange ICE candidates
    for establishing the WebRTC connection with the RetroArch daemon.

    Args:
        sid: Socket ID of the sender
        data: Contains session_id and candidate information
    """
    from handler.redis_handler import async_cache

    session_id = data.get("session_id")
    candidate = data.get("candidate")

    if not session_id or not candidate:
        return

    # Publish to Redis for daemon to consume
    import json

    await async_cache.publish(
        f"retroarch:ice:{session_id}",
        json.dumps({"sid": sid, "candidate": candidate}),
    )


@netplay_socket_handler.socket_server.on("retroarch-input")  # type: ignore
async def retroarch_input(sid: str, data: dict):
    """Handle input events for RetroArch streaming sessions.

    This event forwards keyboard, mouse, and gamepad inputs
    to the RetroArch daemon for processing.

    Args:
        sid: Socket ID of the sender
        data: Contains session_id and input event information
    """
    from handler.redis_handler import async_cache

    session_id = data.get("session_id")
    input_event = data.get("event")

    if not session_id or not input_event:
        return

    # Publish to Redis pubsub for real-time delivery to daemon
    import json

    await async_cache.publish(
        f"retroarch:input:{session_id}",
        json.dumps(input_event),
    )


@netplay_socket_handler.socket_server.on("retroarch-get-core-options")  # type: ignore
async def retroarch_get_core_options(sid: str, data: dict):
    """Retrieve core options from RetroArch instance.

    This event requests the currently available core options from the
    running RetroArch instance via the network command API.

    Args:
        sid: Socket ID of the sender
        data: Contains session_id

    Returns:
        Dict of core options {option_name: option_value}
    """
    from handler.redis_handler import async_cache
    import json

    session_id = data.get("session_id")

    if not session_id:
        await netplay_socket_handler.socket_server.emit(
            "retroarch-core-options",
            {"session_id": session_id, "options": {}},
            to=sid,
        )
        return

    # Request core options from daemon via Redis
    request_key = f"retroarch:get_core_options:{session_id}"
    response_key = f"retroarch:core_options:{session_id}"

    # Set request flag
    await async_cache.set(request_key, "1", ex=10)

    # Wait for response (poll with timeout)
    options = {}
    for _ in range(20):  # Wait up to 2 seconds
        await asyncio.sleep(0.1)
        options_json = await async_cache.get(response_key)
        if options_json:
            try:
                options = json.loads(options_json)
                await async_cache.delete(response_key)
                break
            except json.JSONDecodeError:
                pass

    # Send response back to client
    await netplay_socket_handler.socket_server.emit(
        "retroarch-core-options",
        {"session_id": session_id, "options": options},
        to=sid,
    )

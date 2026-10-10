"""Separate stdio MCP for local Sonos speakers and explicit room/group controls."""

import asyncio
import sys
from typing import Annotated, Callable, Optional, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StrictStr

from .client import (
    ControlResult, FavoritesPage, PlaybackAction, QueuePage, QueueRevision,
    SonosClient, SonosError, SpeakerInventory, SpeakerState, ensure_runtime,
)


mcp = MCPServer(
    "sonos-mcp",
    instructions=(
        "Discover local Sonos speakers and identify targets by their exact RINCON "
        "UID, never a room name or an old IP. Read state before deciding which "
        "device or group to control. Playback, source selection, and queue changes "
        "affect the selected speaker's entire CURRENT group; report all affected "
        "speakers. Volume and mute affect only the selected visible room, including "
        "its bonded speakers, not every room in the playback group. "
        "Only perform the specific change requested by the user. Never test "
        "controls, start audio, clear queues, regroup rooms, or change volume "
        "without an explicit request. Never separate stereo pairs or control "
        "satellites independently. Queue indexes are zero-based; use the exact "
        "coordinator_id/update_id revision returned by a fresh queue read. "
        "Playing a queueable favorite appends it, then starts the appended item; "
        "it does not clear the existing queue. Appending alone does not start audio. "
        "Honor unavailable actions and music-service limitations. Do not invent "
        "missing state, treat an unseen speaker as powered off, or equate an "
        "acknowledgement with audible sound. An uncertain or partially completed "
        "write may already have taken effect: read current state, never blindly "
        "repeat the tool call. Source URIs in output have credentials/query "
        "parameters redacted and are not necessarily reusable playback URLs."
    ),
)
_client = SonosClient()
_READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
)
_DESIRED_STATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
)
_Result = TypeVar("_Result")
Index = Annotated[int, Field(strict=True, ge=0)]
PageLimit = Annotated[int, Field(strict=True, ge=1, le=100)]


async def _call(method: Callable[..., _Result], *args, **kwargs) -> _Result:
    try:
        return await asyncio.to_thread(method, *args, **kwargs)
    except SonosError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Discover local Sonos speakers and groups", annotations=_READ)
async def list_sonos_speakers(
    refresh: Annotated[bool, Field(strict=True)] = True,
) -> SpeakerInventory:
    """Discover speakers by stable UID, including reported bonded/hidden zones.

    Duplicate names are labels, not identities. refresh=false returns cached
    metadata only. Inspect observation times; unseen does not mean powered off.
    Neither discovery nor cached listing sends any control command.
    """
    return await _call(_client.list_speakers, refresh)


@mcp.tool(title="Read one Sonos speaker's details and current room/group state", annotations=_READ)
async def get_sonos_speaker(speaker_id: StrictStr) -> SpeakerState:
    """Verify a UID at its current address and read details, volume, and playback.

    Playback belongs to the reported coordinator/group. A bonded secondary's
    rendering state belongs to its visible room. Missing values are not zero.
    """
    return await _call(_client.get_speaker, speaker_id)


@mcp.tool(title="List existing Sonos favorites", annotations=_READ)
async def list_sonos_favorites(
    speaker_id: StrictStr, start: Index = 0, limit: PageLimit = 50,
) -> FavoritesPage:
    """Read one bounded favorites page and exact favorite IDs.

    Playability labels describe metadata, not successful service authentication
    or playback. Keep unsupported favorites visible with their reason.
    """
    return await _call(_client.list_favorites, speaker_id, start, limit)


@mcp.tool(title="Read the current Sonos group's queue and revision", annotations=_READ)
async def get_sonos_queue(
    speaker_id: StrictStr, start: Index = 0, limit: PageLimit = 50,
) -> QueuePage:
    """Read a bounded queue page from the selected speaker's current coordinator.

    Indexes are zero-based. Preserve queue_revision (coordinator_id and
    update_id) for edits and selection; a stale revision must be refreshed.
    """
    return await _call(_client.get_queue, speaker_id, start, limit)


@mcp.tool(title="Control the current Sonos group's playback", annotations=_WRITE)
async def set_sonos_playback(
    speaker_id: StrictStr, action: PlaybackAction,
) -> ControlResult:
    """Explicitly play, pause, stop, advance, or go back on the current group.

    This may start or interrupt audio on every group member. Previous may
    restart the current track. Next/previous are never safe to blindly repeat.
    """
    return await _call(_client.set_playback, speaker_id, action)


@mcp.tool(title="Set one Sonos room's volume", annotations=_DESIRED_STATE)
async def set_sonos_volume(
    speaker_id: StrictStr, volume: Annotated[int, Field(strict=True, ge=0, le=100)],
) -> ControlResult:
    """Set an explicit 0-100 volume on one visible room, not its playback group.

    Use only the requested level and room. A matching setting is a no-op.
    Device restrictions remain in force; read-back must confirm the setting.
    """
    return await _call(_client.set_volume, speaker_id, volume)


@mcp.tool(title="Set one Sonos room's mute state", annotations=_DESIRED_STATE)
async def set_sonos_mute(
    speaker_id: StrictStr, muted: Annotated[bool, Field(strict=True)],
) -> ControlResult:
    """Explicitly mute or unmute one visible room, never toggle or bulk-mute.

    Unmuting can make active playback audible. A matching state is a no-op.
    """
    return await _call(_client.set_mute, speaker_id, muted)


@mcp.tool(title="Play an HTTP(S) media URL on a Sonos group", annotations=_WRITE)
async def play_sonos_uri(speaker_id: StrictStr, uri: StrictStr) -> ControlResult:
    """Replace the current group's source and start the explicitly supplied URL.

    The speaker must be able to reach and decode the media. No embedded URL
    credentials, file paths, or Sonos control URIs. Does not clear the queue.
    If source selection succeeds but playback fails, do not repeat blindly.
    """
    return await _call(_client.play_uri, speaker_id, uri)


@mcp.tool(title="Play an existing Sonos favorite on the current group", annotations=_WRITE)
async def play_sonos_favorite(
    speaker_id: StrictStr, favorite_id: StrictStr,
) -> ControlResult:
    """Play an exact favorite ID using its current Sonos-provided metadata.

    Streams play directly. Queueable tracks/containers are appended and then
    selected; existing queue entries are never implicitly cleared. A later
    playback failure can leave successfully appended content in the queue.
    """
    return await _call(_client.play_favorite, speaker_id, favorite_id)


@mcp.tool(title="Join a Sonos room to another room's current group", annotations=_WRITE)
async def join_sonos_group(
    speaker_id: StrictStr, target_speaker_id: StrictStr,
) -> ControlResult:
    """Move only the explicitly selected visible room to the target's group.

    Both UIDs must be in the same discovered household. This can interrupt
    audio and change queue ownership. Never break stereo pairs or move other
    source-group rooms implicitly. Report before/after membership.
    """
    return await _call(_client.join_group, speaker_id, target_speaker_id)


@mcp.tool(title="Remove a Sonos room from its playback group", annotations=_WRITE)
async def leave_sonos_group(speaker_id: StrictStr) -> ControlResult:
    """Make the selected visible room standalone, preserving its bonded members.

    This can interrupt audio and change coordinator/queue ownership. An
    already standalone room is a no-op. It never separates a stereo pair.
    """
    return await _call(_client.leave_group, speaker_id)


@mcp.tool(title="Append a URL or favorite to a Sonos group's queue", annotations=_WRITE)
async def add_sonos_queue_item(
    speaker_id: StrictStr, uri: Optional[StrictStr] = None, favorite_id: Optional[StrictStr] = None,
) -> ControlResult:
    """Append exactly one URL or queueable favorite without starting playback.

    Supply exactly one source. Containers may add multiple tracks; inspect
    tracks_added and the insertion index. Stream favorites cannot be queued.
    This is not idempotent: never replay an uncertain append.
    """
    return await _call(_client.add_queue_item, speaker_id, uri, favorite_id)


@mcp.tool(title="Remove one Sonos queue item with a revision check", annotations=_WRITE)
async def remove_sonos_queue_item(
    speaker_id: StrictStr, index: Index, queue_revision: QueueRevision,
) -> ControlResult:
    """Remove one zero-based item from the selected room's current group queue.

    Requires the exact queue revision from a fresh read. This may interrupt
    the current track. Stale or zero (unguarded) revisions fail instead of
    removing another item.
    """
    return await _call(_client.remove_queue_item, speaker_id, index, queue_revision)


@mcp.tool(title="Move one Sonos queue item with a revision check", annotations=_WRITE)
async def move_sonos_queue_item(
    speaker_id: StrictStr, from_index: Index, to_index: Index, queue_revision: QueueRevision,
) -> ControlResult:
    """Move an item to the requested final zero-based index in the current queue.

    Both indexes must exist. Requires the coordinator-bound queue revision.
    A destructive move needs a nonzero revision. Moving to the same index is
    a no-op; never replay an uncertain move.
    """
    return await _call(_client.move_queue_item, speaker_id, from_index, to_index, queue_revision)


@mcp.tool(title="Explicitly clear the current Sonos group's queue", annotations=_WRITE)
async def clear_sonos_queue(
    speaker_id: StrictStr, queue_revision: QueueRevision,
) -> ControlResult:
    """Destructively remove the queue contents only when explicitly requested.

    Requires a fresh nonzero coordinator-bound revision for a nonempty queue
    and uses guarded removal, not an unguarded clear. Empty is a no-op.
    """
    return await _call(_client.clear_queue, speaker_id, queue_revision)


@mcp.tool(title="Play a selected Sonos queue item", annotations=_WRITE)
async def play_sonos_queue_item(
    speaker_id: StrictStr, index: Index, queue_revision: QueueRevision,
) -> ControlResult:
    """Select a zero-based queue item and start playback on the current group.

    Requires a fresh queue revision. Source selection, seek, and play are
    separate steps, not an atomic transaction with other Sonos controllers.
    Partial or uncertain outcomes must be inspected, not automatically retried.
    """
    return await _call(_client.play_queue_item, speaker_id, index, queue_revision)


def main() -> None:
    try:
        ensure_runtime()
    except SonosError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

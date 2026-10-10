# Local Sonos MCP

[Repository setup](../../README.md#setup) | [Maintenance guidance](AGENTS.md).
Run checkout commands below from the repository root.

`sonos-mcp` is a separate **local stdio server** using the community-maintained
[SoCo library](https://soco.readthedocs.io/en/stable/). It supports Python 3.10+
and the existing package's optional `sonos` extra. Other MCPs do not import or
require SoCo. There is no separate REST endpoint, cloud login, account-service
browser, background event listener, or automatic playback.

## Installation

For development from a checkout:

```bash
uv run --locked --extra sonos --inexact sonos-mcp
```

`--inexact` preserves already-installed optional packages in a shared checkout
environment. For a persistent installation alongside other device MCPs, use a
dedicated environment instead:

```bash
UV_PROJECT_ENVIRONMENT="$HOME/.local/share/electro/sonos-mcp" \
  uv sync --locked --extra sonos --no-editable \
  --reinstall-package estonia-nordpool-prices
"$HOME/.local/share/electro/sonos-mcp/bin/sonos-mcp"
```

Use a new destination or one already belonging to this integration. This
installs a non-editable copy of the checkout with locked dependencies without
pruning another MCP's environment. Re-run the installation after updating the
source; `--reinstall-package` rebuilds the project rather than keeping an
older non-editable copy. Missing optional dependencies produce an actionable
stderr error;
importing or starting the server neither discovers nor controls speakers.

## Discovery, identity, and readings

Discovery uses Sonos SSDP on UDP 1900; device requests use LAN HTTP port 1400.
The host and speakers need multicast and direct LAN connectivity. Guest
isolation, VPNs, VLANs, firewalls, and macOS local-network permissions can
prevent access. Discovery does not fall back to sweeping subnets.
SoCo's discovery normally returns the first responding household's topology,
not an exhaustive inventory of separate S1/S2 households on the same network.

Control targets are exact **RINCON UIDs returned by discovery**, not room names
or stored IP addresses. Any discovered compatible speaker can be selected;
there is no hardcoded device count or enrollment list. A synthetic example UID
is `RINCON_02000000000101400`. Real device IDs, addresses, names, media metadata,
and diagnostic captures must not be copied into public fixtures or logs.

The backend reads the device description's fresh UDN rather than trusting
SoCo's IP-cached `uid`, including when SDK speaker-info refresh is requested.
Changing addresses permit one bounded rediscovery **before** dispatch.
Commands use SoCo's SOAP formatting with bounded, non-retrying requests that
do not follow redirects. No raw UPnP-command tool is exposed.

Inventory includes model/firmware/hardware metadata, last-seen times, current
known groups, and available physical/bonded zone identities. The in-memory
cache is not an enrollment database. `refresh=false` is cached metadata only;
unseen devices are not assumed powered off. Group topology has its own
observation time and can be older than the latest discovery attempt.

Fresh speaker state reports the room/coordinator relationship, volume, mute,
transport state, source, available transport actions, and available current
track title/artist/album, duration, position, and zero-based queue index.
Unsupported or unavailable optional values are null with reasons, not zero.
A bonded secondary's playback/rendering belongs to its visible room and is
labeled accordingly. Media URI output redacts credentials, queries, and
fragments; these display URIs are not necessarily reusable playback URLs.

## Tools

All inputs named `speaker_id` or `target_speaker_id` require exact UIDs.
Pagination uses `start=0`, `limit=50` by default; limits are 1-100.

| Tool | Inputs and behavior |
| --- | --- |
| `list_sonos_speakers` | Optional strict boolean `refresh` (default `true`); inventory, availability, and known groups. No playback/settings commands. |
| `get_sonos_speaker` | `speaker_id`; fresh physical-unit details and labeled room/group playback and rendering state. |
| `list_sonos_favorites` | `speaker_id`, `start`, `limit`; exact favorite IDs, media type, and stream/queue/unsupported metadata classification. |
| `get_sonos_queue` | `speaker_id`, `start`, `limit`; current coordinator's queue, counts, continuation, and `queue_revision`. |
| `set_sonos_playback` | `speaker_id`, `action`: `play`, `pause`, `stop`, `next`, or `previous`; operate on the current group. |
| `set_sonos_volume` | `speaker_id`, strict integer `volume` from 0-100; set only the selected visible room. |
| `set_sonos_mute` | `speaker_id`, strict boolean `muted`; explicitly set that room's mute state, never toggle. |
| `play_sonos_uri` | `speaker_id`, `uri`; select a reachable HTTP(S) media URL and start the current group. No queue clearing. |
| `play_sonos_favorite` | `speaker_id`, `favorite_id`; resolve its current referenced Sonos metadata and play the stream or append/select queueable content. |
| `join_sonos_group` | `speaker_id`, `target_speaker_id`; join the selected visible room to the target's current group in the same household. |
| `leave_sonos_group` | `speaker_id`; make the selected visible room standalone, retaining bonded members. |
| `add_sonos_queue_item` | `speaker_id`, exactly one of `uri` or `favorite_id`; append queueable content without starting playback. |
| `remove_sonos_queue_item` | `speaker_id`, `index`, `queue_revision`; remove one current queue item with a native revision guard. |
| `move_sonos_queue_item` | `speaker_id`, `from_index`, `to_index`, `queue_revision`; move one item to its requested **final** index. |
| `clear_sonos_queue` | `speaker_id`, `queue_revision`; explicitly remove the current queue contents using guarded range removal. |
| `play_sonos_queue_item` | `speaker_id`, `index`, `queue_revision`; select and start one current queue item. |

## Group and media semantics

**Playback, source selection, and queue operations affect every speaker in the
selected room's current group.** The server refreshes topology, verifies the
coordinator, and reports all affected IDs. Volume/mute remain local to the
selected visible room, including its bonded members, not every room sharing
playback.

Only issue the specific control requested by the user. Reading/discovery never
starts audio, changes volume, or regroups rooms. Bridges and bonded secondaries
cannot be independently controlled. There are no stereo-pair creation/removal,
party-mode, group-volume, EQ, alarm, reboot, or firmware-update tools.

Joining/leaving can interrupt playback and change coordinator or queue
ownership. Results show before/after membership; they do not promise that
audio or queues have been preserved or restored. The server does not silently
move additional source-group rooms or regroup across incompatible households.

Plain media URLs must be HTTP(S), without embedded username/password
credentials, and reachable/decodable by the speaker. The MCP does not fetch
media or artwork. A streaming-service webpage is not necessarily a playable
URL, and some radio streams require Sonos-specific metadata; use an existing
compatible favorite where appropriate.

Favorites use their exact `FV:2/...` ID and referenced DIDL metadata, not a
guessed title or service URL. A stream is selected directly. A queueable track
or container is **appended**, then playback starts at the returned insertion
index. Existing entries are never implicitly cleared. Containers may add
multiple tracks, reported as `tracks_added`. Appending alone never starts audio;
non-queueable stream favorites must use `play_sonos_favorite`.

SoCo and Sonos firmware have music-service compatibility limits. Listing a
favorite does not verify authentication or successful playback. Unsupported
media and expired/missing service authorization produce explicit errors, not
cloud-login workarounds or claims that music is playing.

## Queue revisions and uncertain outcomes

All MCP queue indexes are **zero-based**. Obtain a fresh revision from
`get_sonos_queue` and pass the complete value back, for example:

```json
{
  "speaker_id": "RINCON_02000000000101400",
  "index": 1,
  "queue_revision": {
    "coordinator_id": "RINCON_02000000000101400",
    "update_id": 7
  }
}
```

These IDs and the revision are synthetic; use the actual returned values.
The revision binds the Sonos UpdateID to its coordinator so the same counter
on a different group's queue cannot authorize an edit. Stale references fail
before dispatch. Indexes must exist; moving to the same index and clearing an
already empty queue are no-ops.

Remove, reorder, and nonempty clear use native UpdateID-checked operations.
Clearing uses `RemoveTrackRangeFromQueue`, not the unguarded
`RemoveAllTracksFromQueue`. A reported UpdateID of **0** is not accepted as a
destructive guard because Sonos uses zero to bypass the device-side check;
read-only access, selection, and genuine no-ops remain available.

Source selection, queue selection/seek, and play are separate operations,
not transactions with other Sonos apps. The server serializes its own writes,
checks current targets/revisions between steps, and checks the source/item
again before starting playback. External controllers can still race; no
cross-controller atomicity is claimed.

Control results separate `command_sent`, `acknowledged`, and `verified`,
include `completed_steps`, affected IDs, relevant resulting state/queue, and
membership information. Verification means the requested device-reported
postcondition was observed, not that sound was independently heard or that the
state will remain unchanged.

A timeout after dispatch, partial sequence, or failed read-back is an MCP tool
error stating that the operation may already have taken effect. Only bounded
read-back attempts are repeated, **never commands**. Inspect current state
before deciding on another action; replaying an append, next/previous, or
multi-step favorite playback could duplicate or change content.

## Local Copilot registration

After checking the installed stdio server with read-only tools, add only the
new entry to the existing private `~/.copilot/mcp-config.json`. Replace the
example command with the actual absolute path; `~` is not shell-expanded in
the command field. Preserve other entries and do not put device captures,
credentials, or private inventory in source.

```json
{
  "mcpServers": {
    "sonos-mcp": {
      "type": "stdio",
      "command": "/absolute/path/to/home/.local/share/electro/sonos-mcp/bin/sonos-mcp",
      "args": [],
      "tools": [
        "list_sonos_speakers", "get_sonos_speaker",
        "list_sonos_favorites", "get_sonos_queue",
        "set_sonos_playback", "set_sonos_volume", "set_sonos_mute",
        "play_sonos_uri", "play_sonos_favorite",
        "join_sonos_group", "leave_sonos_group",
        "add_sonos_queue_item", "remove_sonos_queue_item",
        "move_sonos_queue_item", "clear_sonos_queue", "play_sonos_queue_item"
      ],
      "timeout": 300000
    }
  }
}
```

An existing Copilot session may need to reload its MCP configuration.
Registration does not authorize unsolicited physical control tests.

## Sonos validation

```bash
uv run --locked --extra sonos --inexact python -m unittest discover \
  -s tests -p 'test_sonos*.py' -v
```

Tests use synthetic identities and mocked LAN calls. They cover discovery and
cached/unavailable metadata, fresh identity checks, bonded/group semantics,
strict inputs, metadata and pagination, exact queue transformations, revision
conflicts, partial/uncertain writes, serialization through cancellation, MCP
schemas, and optional-runtime startup. SDK-dependent tests skip explicitly
without the extra; base schema/startup checks remain available.

Live acceptance is discovery, details, topology, and bounded favorites/queue
reads through the installed MCP process. Mocked controls and read-only
acceptance are not proof of live control or service playback. Any later
physical test needs a specific requested speaker/action and must account for
current playback, volume, groups, and queue contents.

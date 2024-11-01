"""
The MIT License (MIT)

Copyright (c) 2015-2021 Rapptz
Copyright (c) 2021-present Pycord Development

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, ClassVar, Generator, TypeVar

from . import enums, utils
from .asset import Asset
from .automod import AutoModAction, AutoModTriggerMetadata
from .colour import Colour
from .invite import Invite
from .mixins import Hashable
from .object import Object
from .permissions import PermissionOverwrite, Permissions

__all__ = (
    "AuditLogDiff",
    "AuditLogChanges",
    "AuditLogEntry",
)


if TYPE_CHECKING:
    import datetime

    from . import abc
    from .emoji import GuildEmoji
    from .guild import Guild
    from .member import Member
    from .role import Role
    from .scheduled_events import ScheduledEvent
    from .stage_instance import StageInstance
    from .state import ConnectionState
    from .sticker import GuildSticker
    from .threads import Thread
    from .types.audit_log import AuditLogChange as AuditLogChangePayload
    from .types.audit_log import AuditLogEntry as AuditLogEntryPayload
    from .types.automod import AutoModAction as AutoModActionPayload
    from .types.automod import AutoModTriggerMetadata as AutoModTriggerMetadataPayload
    from .types.channel import PermissionOverwrite as PermissionOverwritePayload
    from .types.role import Role as RolePayload
    from .types.snowflake import Snowflake
    from .user import User


def _transform_permissions(entry: AuditLogEntry, data: str) -> Permissions:
    return Permissions(int(data))


def _transform_color(entry: AuditLogEntry, data: int) -> Colour:
    return Colour(data)


def _transform_snowflake(entry: AuditLogEntry, data: Snowflake) -> int:
    return int(data)


def _transform_channel(
    entry: AuditLogEntry, data: Snowflake | None
) -> abc.GuildChannel | Object | None:
    if data is None:
        return None
    return entry.guild.get_channel(int(data)) or Object(id=data)


def _transform_channels(
    entry: AuditLogEntry, data: list[Snowflake] | None
) -> list[abc.GuildChannel | Object] | None:
    if data is None:
        return None
    return [_transform_channel(entry, channel) for channel in data]


def _transform_roles(
    entry: AuditLogEntry, data: list[Snowflake] | None
) -> list[Role | Object] | None:
    if data is None:
        return None
    return [entry.guild.get_role(int(r)) or Object(id=r) for r in data]


def _transform_member_id(
    entry: AuditLogEntry, data: Snowflake | None
) -> Member | User | None:
    if data is None:
        return None
    return entry._get_member(int(data))


def _transform_guild_id(entry: AuditLogEntry, data: Snowflake | None) -> Guild | None:
    if data is None:
        return None
    return entry._state._get_guild(data)


def _transform_overwrites(
    entry: AuditLogEntry, data: list[PermissionOverwritePayload]
) -> list[tuple[Object, PermissionOverwrite]]:
    overwrites = []
    for elem in data:
        allow = Permissions(int(elem["allow"]))
        deny = Permissions(int(elem["deny"]))
        ow = PermissionOverwrite.from_pair(allow, deny)

        ow_type = elem["type"]
        ow_id = int(elem["id"])
        target = None
        if ow_type == 0:
            target = entry.guild.get_role(ow_id)
        elif ow_type == 1:
            target = entry._get_member(ow_id)

        if target is None:
            target = Object(id=ow_id)

        overwrites.append((target, ow))

    return overwrites


def _transform_icon(entry: AuditLogEntry, data: str | None) -> Asset | None:
    if data is None:
        return None
    return Asset._from_guild_icon(entry._state, entry.guild.id, data)


def _transform_avatar(entry: AuditLogEntry, data: str | None) -> Asset | None:
    if data is None:
        return None
    return Asset._from_avatar(entry._state, entry._target_id, data)  # type: ignore


def _transform_scheduled_event_image(
    entry: AuditLogEntry, data: str | None
) -> Asset | None:
    if data is None:
        return None
    return Asset._from_scheduled_event_image(entry._state, entry._target_id, data)


def _guild_hash_transformer(
    path: str,
) -> Callable[[AuditLogEntry, str | None], Asset | None]:
    def _transform(entry: AuditLogEntry, data: str | None) -> Asset | None:
        if data is None:
            return None
        return Asset._from_guild_image(entry._state, entry.guild.id, data, path=path)

    return _transform


T = TypeVar("T", bound=enums.Enum)


def _enum_transformer(enum: type[T]) -> Callable[[AuditLogEntry, int], T]:
    def _transform(entry: AuditLogEntry, data: int) -> T:
        return enums.try_enum(enum, data)

    return _transform


def _transform_type(
    entry: AuditLogEntry, data: int
) -> enums.ChannelType | enums.StickerType:
    if entry.action.name.startswith("sticker_"):
        return enums.try_enum(enums.StickerType, data)
    else:
        return enums.try_enum(enums.ChannelType, data)


def _transform_actions(
    entry: AuditLogEntry, data: list[AutoModActionPayload] | None
) -> list[AutoModAction] | None:
    if data is None:
        return None
    else:
        return [AutoModAction.from_dict(d) for d in data]


def _transform_trigger_metadata(
    entry: AuditLogEntry, data: AutoModTriggerMetadataPayload | None
) -> AutoModTriggerMetadata | None:
    if data is None:
        return None
    else:
        return AutoModTriggerMetadata.from_dict(data)


class AuditLogDiff:
    def __len__(self) -> int:
        return len(self.__dict__)

    def __iter__(self) -> Generator[tuple[str, Any]]:
        yield from self.__dict__.items()

    def __repr__(self) -> str:
        values = " ".join("%s=%r" % item for item in self.__dict__.items())
        return f"<AuditLogDiff {values}>"

    if TYPE_CHECKING:

        def __getattr__(self, item: str) -> Any: ...

        def __setattr__(self, key: str, value: Any) -> Any: ...


Transformer = Callable[["AuditLogEntry", Any], Any]


class AuditLogChanges:
    TRANSFORMERS: ClassVar[dict[str, tuple[str | None, Transformer | None]]] = {
        "verification_level": (None, _enum_transformer(enums.VerificationLevel)),
        "explicit_content_filter": (None, _enum_transformer(enums.ContentFilter)),
        "allow": (None, _transform_permissions),
        "deny": (None, _transform_permissions),
        "permissions": (None, _transform_permissions),
        "id": (None, _transform_snowflake),
        "color": ("colour", _transform_color),
        "owner_id": ("owner", _transform_member_id),
        "inviter_id": ("inviter", _transform_member_id),
        "channel_id": ("channel", _transform_channel),
        "afk_channel_id": ("afk_channel", _transform_channel),
        "system_channel_id": ("system_channel", _transform_channel),
        "widget_channel_id": ("widget_channel", _transform_channel),
        "rules_channel_id": ("rules_channel", _transform_channel),
        "public_updates_channel_id": ("public_updates_channel", _transform_channel),
        "permission_overwrites": ("overwrites", _transform_overwrites),
        "splash_hash": ("splash", _guild_hash_transformer("splashes")),
        "banner_hash": ("banner", _guild_hash_transformer("banners")),
        "discovery_splash_hash": (
            "discovery_splash",
            _guild_hash_transformer("discovery-splashes"),
        ),
        "icon_hash": ("icon", _transform_icon),
        "avatar_hash": ("avatar", _transform_avatar),
        "rate_limit_per_user": ("slowmode_delay", None),
        "guild_id": ("guild", _transform_guild_id),
        "tags": ("emoji", None),
        "default_message_notifications": (
            "default_notifications",
            _enum_transformer(enums.NotificationLevel),
        ),
        "rtc_region": (None, _enum_transformer(enums.VoiceRegion)),
        "video_quality_mode": (None, _enum_transformer(enums.VideoQualityMode)),
        "privacy_level": (None, _enum_transformer(enums.StagePrivacyLevel)),
        "format_type": (None, _enum_transformer(enums.StickerFormatType)),
        "type": (None, _transform_type),
        "status": (None, _enum_transformer(enums.ScheduledEventStatus)),
        "entity_type": (
            "location_type",
            _enum_transformer(enums.ScheduledEventLocationType),
        ),
        "command_id": ("command_id", _transform_snowflake),
        "image_hash": ("image", _transform_scheduled_event_image),
        "trigger_type": (None, _enum_transformer(enums.AutoModTriggerType)),
        "event_type": (None, _enum_transformer(enums.AutoModEventType)),
        "actions": (None, _transform_actions),
        "trigger_metadata": (None, _transform_trigger_metadata),
        "exempt_roles": (None, _transform_roles),
        "exempt_channels": (None, _transform_channels),
    }

    def __init__(
        self,
        entry: AuditLogEntry,
        data: list[AuditLogChangePayload],
        *,
        state: ConnectionState,
    ):
        self.before = AuditLogDiff()
        self.after = AuditLogDiff()

        for elem in sorted(data, key=lambda i: i["key"]):
            attr = elem["key"]

            # special cases for role/trigger_metadata add/remove
            if attr == "$add":
                self._handle_role(self.before, self.after, entry, elem["new_value"])  # type: ignore
                continue
            elif attr == "$remove":
                self._handle_role(self.after, self.before, entry, elem["new_value"])  # type: ignore
                continue
            elif attr in [
                "$add_keyword_filter",
                "$add_regex_patterns",
                "$add_allow_list",
            ]:
                self._handle_trigger_metadata(
                    self.before, self.after, entry, elem["new_value"], attr  # type: ignore
                )
                continue
            elif attr in [
                "$remove_keyword_filter",
                "$remove_regex_patterns",
                "$remove_allow_list",
            ]:
                self._handle_trigger_metadata(
                    self.after, self.before, entry, elem["new_value"], attr  # type: ignore
                )
                continue

            try:
                key, transformer = self.TRANSFORMERS[attr]
            except (ValueError, KeyError):
                transformer = None
            else:
                if key:
                    attr = key

            transformer: Transformer | None

            try:
                before = elem["old_value"]
            except KeyError:
                before = None
            else:
                if transformer:
                    before = transformer(entry, before)

            if attr == "location" and hasattr(self.before, "location_type"):
                from .scheduled_events import ScheduledEventLocation

                if (
                    self.before.location_type
                    is enums.ScheduledEventLocationType.external
                ):
                    before = ScheduledEventLocation(state=state, value=before)
                elif hasattr(self.before, "channel"):
                    before = ScheduledEventLocation(
                        state=state, value=self.before.channel
                    )

            setattr(self.before, attr, before)

            try:
                after = elem["new_value"]
            except KeyError:
                after = None
            else:
                if transformer:
                    after = transformer(entry, after)

            if attr == "location" and hasattr(self.after, "location_type"):
                from .scheduled_events import ScheduledEventLocation

                if (
                    self.after.location_type
                    is enums.ScheduledEventLocationType.external
                ):
                    after = ScheduledEventLocation(state=state, value=after)
                elif hasattr(self.after, "channel"):
                    after = ScheduledEventLocation(
                        state=state, value=self.after.channel
                    )

            setattr(self.after, attr, after)

        # add an alias
        if hasattr(self.after, "colour"):
            self.after.color = self.after.colour
            self.before.color = self.before.colour
        if hasattr(self.after, "expire_behavior"):
            self.after.expire_behaviour = self.after.expire_behavior
            self.before.expire_behaviour = self.before.expire_behavior

    def __repr__(self) -> str:
        return f"<AuditLogChanges before={self.before!r} after={self.after!r}>"

    def _handle_role(
        self,
        first: AuditLogDiff,
        second: AuditLogDiff,
        entry: AuditLogEntry,
        elem: list[RolePayload],
    ) -> None:
        if not hasattr(first, "roles"):
            setattr(first, "roles", [])

        data = []
        g: Guild = entry.guild  # type: ignore

        for e in elem:
            role_id = int(e["id"])
            role = g.get_role(role_id)

            if role is None:
                role = Object(id=role_id)
                role.name = e["name"]  # type: ignore

            data.append(role)

        setattr(second, "roles", data)

    def _handle_trigger_metadata(
        self,
        first: AuditLogDiff,
        second: AuditLogDiff,
        entry: AuditLogEntry,
        elem: list[AutoModTriggerMetadataPayload],
        attr: str,
    ) -> None:
        if not hasattr(first, "trigger_metadata"):
            setattr(first, "trigger_metadata", None)

        key = attr.split("_", 1)[-1]
        data = {key: elem}
        tm = AutoModTriggerMetadata.from_dict(data)

        setattr(second, "trigger_metadata", tm)


class _AuditLogProxyMemberPrune:
    delete_member_days: int
    members_removed: int


class _AuditLogProxyMemberMoveOrMessageDelete:
    channel: abc.GuildChannel
    count: int


class _AuditLogProxyMemberDisconnect:
    count: int


class _AuditLogProxyPinAction:
    channel: abc.GuildChannel
    message_id: int


class _AuditLogProxyStageInstanceAction:
    channel: abc.GuildChannel


class AuditLogEntry(Hashable):
    r"""Represents an Audit Log entry.

    You retrieve these via :meth:`Guild.audit_logs`.

    .. container:: operations

        .. describe:: x == y

            Checks if two entries are equal.

        .. describe:: x != y

            Checks if two entries are not equal.

        .. describe:: hash(x)

            Returns the entry's hash.

    .. versionchanged:: 1.7
        Audit log entries are now comparable and hashable.

    Attributes
    -----------
    action: :class:`AuditLogAction`
        The action that was done.
    user: Optional[:class:`abc.User`]
        The user who initiated this action. Usually a :class:`Member`\, unless gone
        then it's a :class:`User`.
    id: :class:`int`
        The entry ID.
    target: Any
        The target that got changed. The exact type of this depends on
        the action being done.
    reason: Optional[:class:`str`]
        The reason this action was done.
    extra: Any
        Extra information that this entry has that might be useful.
        For most actions, this is ``None``. However, in some cases it
        contains extra information. See :class:`AuditLogAction` for
        which actions have this field filled out.
    """

    def __init__(
        self, *, users: dict[int, User], data: AuditLogEntryPayload, guild: Guild
    ):
        self._state = guild._state
        self.guild = guild
        self._users = users
        self._from_data(data)

    def _from_data(self, data: AuditLogEntryPayload) -> None:
        self.action = enums.try_enum(enums.AuditLogAction, data["action_type"])
        self.id = int(data["id"])

        # this key is technically not usually present
        self.reason = data.get("reason")
        self.extra = data.get("options")

        if isinstance(self.action, enums.AuditLogAction) and self.extra:
            if self.action is enums.AuditLogAction.member_prune:
                # member prune has two keys with useful information
                self.extra: _AuditLogProxyMemberPrune = type(
                    "_AuditLogProxy", (), {k: int(v) for k, v in self.extra.items()}
                )()
            elif (
                self.action is enums.AuditLogAction.member_move
                or self.action is enums.AuditLogAction.message_delete
            ):
                channel_id = int(self.extra["channel_id"])
                elems = {
                    "count": int(self.extra["count"]),
                    "channel": self.guild.get_channel(channel_id)
                    or Object(id=channel_id),
                }
                self.extra: _AuditLogProxyMemberMoveOrMessageDelete = type(
                    "_AuditLogProxy", (), elems
                )()
            elif self.action is enums.AuditLogAction.member_disconnect:
                # The member disconnect action has a dict with some information
                elems = {
                    "count": int(self.extra["count"]),
                }
                self.extra: _AuditLogProxyMemberDisconnect = type(
                    "_AuditLogProxy", (), elems
                )()
            elif self.action.name.endswith("pin"):
                # the pin actions have a dict with some information
                channel_id = int(self.extra["channel_id"])
                elems = {
                    "channel": self.guild.get_channel(channel_id)
                    or Object(id=channel_id),
                    "message_id": int(self.extra["message_id"]),
                }
                self.extra: _AuditLogProxyPinAction = type(
                    "_AuditLogProxy", (), elems
                )()
            elif self.action.name.startswith("overwrite_"):
                # the overwrite_ actions have a dict with some information
                instance_id = int(self.extra["id"])
                the_type = self.extra.get("type")
                if the_type == "1":
                    self.extra = self._get_member(instance_id)
                elif the_type == "0":
                    role = self.guild.get_role(instance_id)
                    if role is None:
                        role = Object(id=instance_id)
                        role.name = self.extra.get("role_name")  # type: ignore
                    self.extra: Role = role
            elif self.action.name.startswith("stage_instance"):
                channel_id = int(self.extra["channel_id"])
                elems = {
                    "channel": self.guild.get_channel(channel_id)
                    or Object(id=channel_id)
                }
                self.extra: _AuditLogProxyStageInstanceAction = type(
                    "_AuditLogProxy", (), elems
                )()

        self.extra: (
            _AuditLogProxyMemberPrune
            | _AuditLogProxyMemberMoveOrMessageDelete
            | _AuditLogProxyMemberDisconnect
            | _AuditLogProxyPinAction
            | _AuditLogProxyStageInstanceAction
            | Member
            | User
            | None
            | Role
        )

        # this key is not present when the above is present, typically.
        # It's a list of { new_value: a, old_value: b, key: c }
        # where new_value and old_value are not guaranteed to be there depending
        # on the action type, so let's just fetch it for now and only turn it
        # into meaningful data when requested
        self._changes = data.get("changes", [])

        self.user = self._get_member(utils._get_as_snowflake(data, "user_id"))  # type: ignore
        self._target_id = utils._get_as_snowflake(data, "target_id")

    def _get_member(self, user_id: int) -> Member | User | None:
        return self.guild.get_member(user_id) or self._users.get(user_id)

    def __repr__(self) -> str:
        return f"<AuditLogEntry id={self.id} action={self.action} user={self.user!r}>"

    @utils.cached_property
    def created_at(self) -> datetime.datetime:
        """Returns the entry's creation time in UTC."""
        return utils.snowflake_time(self.id)

    @utils.cached_property
    def target(
        self,
    ) -> (
        Guild
        | abc.GuildChannel
        | Member
        | User
        | Role
        | Invite
        | GuildEmoji
        | StageInstance
        | GuildSticker
        | Thread
        | Object
        | None
    ):
        try:
            converter = getattr(self, f"_convert_target_{self.action.target_type}")
        except AttributeError:
            return Object(id=self._target_id)
        else:
            return converter(self._target_id)

    @utils.cached_property
    def category(self) -> enums.AuditLogActionCategory:
        """The category of the action, if applicable."""
        return self.action.category

    @utils.cached_property
    def changes(self) -> AuditLogChanges:
        """The list of changes this entry has."""
        obj = AuditLogChanges(self, self._changes, state=self._state)
        del self._changes
        return obj

    @utils.cached_property
    def before(self) -> AuditLogDiff:
        """The target's prior state."""
        return self.changes.before

    @utils.cached_property
    def after(self) -> AuditLogDiff:
        """The target's subsequent state."""
        return self.changes.after

    def _convert_target_guild(self, target_id: int) -> Guild:
        return self.guild

    def _convert_target_channel(self, target_id: int) -> abc.GuildChannel | Object:
        return self.guild.get_channel(target_id) or Object(id=target_id)

    def _convert_target_user(self, target_id: int) -> Member | User | None:
        return self._get_member(target_id)

    def _convert_target_role(self, target_id: int) -> Role | Object:
        return self.guild.get_role(target_id) or Object(id=target_id)

    def _convert_target_invite(self, target_id: int) -> Invite:
        # invites have target_id set to null
        # so figure out which change has the full invite data
        changeset = (
            self.before
            if self.action is enums.AuditLogAction.invite_delete
            else self.after
        )

        fake_payload = {
            "max_age": changeset.max_age,
            "max_uses": changeset.max_uses,
            "code": changeset.code,
            "temporary": changeset.temporary,
            "uses": changeset.uses,
        }

        obj = Invite(state=self._state, data=fake_payload, guild=self.guild, channel=changeset.channel)  # type: ignore
        try:
            obj.inviter = changeset.inviter
        except AttributeError:
            pass
        return obj

    def _convert_target_emoji(self, target_id: int) -> GuildEmoji | Object:
        return self._state.get_emoji(target_id) or Object(id=target_id)

    def _convert_target_message(self, target_id: int) -> Member | User | None:
        return self._get_member(target_id)

    def _convert_target_stage_instance(self, target_id: int) -> StageInstance | Object:
        return self.guild.get_stage_instance(target_id) or Object(id=target_id)

    def _convert_target_sticker(self, target_id: int) -> GuildSticker | Object:
        return self._state.get_sticker(target_id) or Object(id=target_id)

    def _convert_target_thread(self, target_id: int) -> Thread | Object:
        return self.guild.get_thread(target_id) or Object(id=target_id)

    def _convert_target_scheduled_event(
        self, target_id: int
    ) -> ScheduledEvent | Object:
        return self.guild.get_scheduled_event(target_id) or Object(id=target_id)

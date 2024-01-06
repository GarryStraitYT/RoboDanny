from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, MutableMapping, NamedTuple, Optional, Set, TypedDict
from typing_extensions import Self, Annotated
from discord.ext import commands
from discord import app_commands
import discord
import random
import logging
from lru import LRU
import yarl
import io
import re

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .utils.context import GuildContext, Context
    from bot import RoboDanny


def can_use_spoiler():
    def predicate(ctx: GuildContext) -> bool:
        if ctx.guild is None:
            raise commands.BadArgument('Cannot be used in private messages.')

        my_permissions = ctx.channel.permissions_for(ctx.guild.me)
        if not (my_permissions.read_message_history and my_permissions.manage_messages and my_permissions.add_reactions):
            raise commands.BadArgument(
                'Need Read Message History, Add Reactions and Manage Messages '
                'to permission to use this. Sorry if I spoiled you.'
            )
        return True

    return commands.check(predicate)


SPOILER_EMOJI_ID = 430469957042831371


class ConvertibleUnit(NamedTuple):
    # (value) -> (converted, unit)
    formula: Callable[[float], tuple[float, str]]
    capture: str


UNIT_CONVERSIONS: dict[str, ConvertibleUnit] = {
    'km': ConvertibleUnit(lambda v: (v * 0.621371, 'mi'), r'km|(?:kilometer|kilometre)s?'),
    'm': ConvertibleUnit(lambda v: (v * 3.28084, 'ft'), r'm|(?:meter|metre)s?'),
    'ft': ConvertibleUnit(lambda v: (v * 0.3048, 'm'), r'ft|feet|foot'),
    'cm': ConvertibleUnit(lambda v: (v * 0.393701, 'in'), r'cm|(?:centimeter|centimetre)s?'),
    'in': ConvertibleUnit(lambda v: (v * 2.54, 'cm'), r'in|inch(?:es)?'),
    'mi': ConvertibleUnit(lambda v: (v * 1.60934, 'km'), r'mi|miles?'),
    'kg': ConvertibleUnit(lambda v: (v * 2.20462, 'lb'), r'kg|kilograms?'),
    'lb': ConvertibleUnit(lambda v: (v * 0.453592, 'kg'), r'(?:lb|pound)s?'),
    'L': ConvertibleUnit(lambda v: (v * 0.264172, 'gal'), r'l|(?:liter|litre)s?'),
    'gal': ConvertibleUnit(lambda v: (v * 3.78541, 'L'), r'gal|gallons?'),
    'C': ConvertibleUnit(lambda v: (v * 1.8 + 32, 'F'), r'c|°c|celsius'),
    'F': ConvertibleUnit(lambda v: ((v - 32) / 1.8, 'C'), r'f|°f|fahrenheit'),
}

UNIT_CONVERSION_REGEX_COMPONENT = '|'.join(f'(?P<{name}>{unit.capture})' for name, unit in UNIT_CONVERSIONS.items())
UNIT_CONVERSION_REGEX = re.compile(
    rf'(?P<value>\-?[0-9]+(?:[,.][0-9]+)?)\s*(?:{UNIT_CONVERSION_REGEX_COMPONENT})\b', re.IGNORECASE
)


class Unit(NamedTuple):
    value: float
    unit: str

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        match = UNIT_CONVERSION_REGEX.match(argument)
        if match is None:
            raise commands.BadArgument('Could not find a unit')

        value = float(match.group('value'))
        unit = match.lastgroup
        if unit is None:
            raise commands.BadArgument('Could not find a unit')

        return cls(value, unit)

    def converted(self) -> Unit:
        return Unit(*UNIT_CONVERSIONS[self.unit].formula(self.value))

    @property
    def display_unit(self) -> str:
        # Work around the fact that ° can't be used in group names
        if self.unit in ('F', 'C'):
            return f'°{self.unit}'
        return f' {self.unit}'


class UnitCollector(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> set[Unit]:
        units = set()
        for match in UNIT_CONVERSION_REGEX.finditer(argument):
            value = float(match.group('value'))
            unit = match.lastgroup
            if unit is None:
                raise commands.BadArgument('Could not find a unit')

            units.add(Unit(value, unit))

        if not units:
            raise commands.BadArgument('Could not find a unit')

        return units


class RedditMediaURL:
    def __init__(self, url: yarl.URL):
        self.url: yarl.URL = url
        self.filename: str = url.parts[1] + '.mp4'

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        try:
            url = yarl.URL(argument)
        except Exception as e:
            raise commands.BadArgument('Not a valid URL.')

        headers = {
            'User-Agent': 'Discord:RoboDanny:v4.0 (by /u/Rapptz)',
        }
        await ctx.typing()
        if url.host == 'v.redd.it':
            # have to do a request to fetch the 'main' URL.
            async with ctx.session.get(url, headers=headers) as resp:
                url = resp.url

        is_valid_path = url.host and url.host.endswith('.reddit.com')
        if not is_valid_path:
            raise commands.BadArgument('Not a reddit URL.')

        # Now we go the long way
        async with ctx.session.get(url / '.json', headers=headers) as resp:
            if resp.status != 200:
                raise commands.BadArgument(f'Reddit API failed with {resp.status}.')

            data = await resp.json()
            try:
                submission = data[0]['data']['children'][0]['data']
            except (KeyError, TypeError, IndexError):
                raise commands.BadArgument('Could not fetch submission.')

            try:
                media = submission['media']['reddit_video']
            except (KeyError, TypeError):
                try:
                    # maybe it's a cross post
                    crosspost = submission['crosspost_parent_list'][0]
                    media = crosspost['media']['reddit_video']
                except (KeyError, TypeError, IndexError):
                    raise commands.BadArgument('Could not fetch media information.')

            try:
                fallback_url = yarl.URL(media['fallback_url'])
            except KeyError:
                raise commands.BadArgument('Could not fetch fall back URL.')

            return cls(fallback_url)


class SpoilerCacheData(TypedDict):
    author_id: int
    channel_id: int
    title: str
    text: Optional[str]
    attachments: list[discord.Attachment]


class SpoilerCache:
    __slots__ = ('author_id', 'channel_id', 'title', 'text', 'attachments')

    def __init__(self, data: SpoilerCacheData):
        self.author_id: int = data['author_id']
        self.channel_id: int = data['channel_id']
        self.title: str = data['title']
        self.text: Optional[str] = data['text']
        self.attachments: list[discord.Attachment] = data['attachments']

    def has_single_image(self) -> bool:
        return bool(self.attachments) and self.attachments[0].filename.lower().endswith(('.gif', '.png', '.jpg', '.jpeg'))

    def to_embed(self, bot: RoboDanny) -> discord.Embed:
        embed = discord.Embed(title=f'{self.title} Spoiler', colour=0x01AEEE)
        if self.text:
            embed.description = self.text

        if self.has_single_image():
            if self.text is None:
                embed.title = f'{self.title} Spoiler Image'
            embed.set_image(url=self.attachments[0].url)
            attachments = self.attachments[1:]
        else:
            attachments = self.attachments

        if attachments:
            value = '\n'.join(f'[{a.filename}]({a.url})' for a in attachments)
            embed.add_field(name='Attachments', value=value, inline=False)

        user = bot.get_user(self.author_id)
        if user:
            embed.set_author(name=str(user), icon_url=user.display_avatar.url)

        return embed

    def to_spoiler_embed(self, ctx: Context, storage_message: discord.abc.Snowflake) -> discord.Embed:
        description = 'This spoiler has been hidden. Press the button to reveal it!'
        embed = discord.Embed(title=f'{self.title} Spoiler', description=description)
        if self.has_single_image() and self.text is None:
            embed.title = f'{self.title} Spoiler Image'

        embed.set_footer(text=storage_message.id)
        embed.colour = 0x01AEEE
        embed.set_author(name=ctx.author, icon_url=ctx.author.display_avatar.url)
        return embed


class SpoilerCooldown(commands.CooldownMapping):
    def __init__(self):
        super().__init__(commands.Cooldown(1, 10.0), commands.BucketType.user)

    def _bucket_key(self, tup: tuple[int, int]) -> tuple[int, int]:
        return tup

    def is_rate_limited(self, message_id: int, user_id: int) -> bool:
        # This is a lie but it should just work as-is
        bucket = self.get_bucket((message_id, user_id))  # type: ignore
        return bucket is not None and bucket.update_rate_limit() is not None


class FeedbackModal(discord.ui.Modal, title='Submit Feedback'):
    summary = discord.ui.TextInput(label='Summary', placeholder='A brief explanation of what you want')
    details = discord.ui.TextInput(label='Details', style=discord.TextStyle.long, required=False)

    def __init__(self, cog: Buttons) -> None:
        super().__init__()
        self.cog: Buttons = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = self.cog.feedback_channel
        if channel is None:
            await interaction.response.send_message('Could not submit your feedback, sorry about this', ephemeral=True)
            return

        embed = self.cog.get_feedback_embed(interaction, summary=str(self.summary), details=self.details.value)
        await channel.send(embed=embed)
        await interaction.response.send_message('Successfully submitted feedback', ephemeral=True)


class SpoilerView(discord.ui.View):
    def __init__(self, cog: Buttons) -> None:
        super().__init__(timeout=None)
        self.cog: Buttons = cog

    @discord.ui.button(
        label='Reveal Spoiler',
        style=discord.ButtonStyle.grey,
        emoji=discord.PartialEmoji(name='spoiler', id=430469957042831371),
        custom_id='cogs:buttons:reveal_spoiler',
    )
    async def reveal_spoiler(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.message is not None
        assert interaction.channel_id is not None

        cache = await self.cog.get_spoiler_cache(interaction.channel_id, interaction.message.id)
        if cache is not None:
            embed = cache.to_embed(self.cog.bot)
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label='Jump to Spoiler', url=interaction.message.jump_url))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message('Could not find this message in storage', ephemeral=True)


class Buttons(commands.Cog):
    """Buttons that make you feel."""

    def __init__(self, bot: RoboDanny):
        self.bot: RoboDanny = bot
        self._spoiler_cache: MutableMapping[int, SpoilerCache] = LRU(128)
        self._spoiler_cooldown = SpoilerCooldown()
        self._spoiler_view = SpoilerView(self)
        bot.add_view(self._spoiler_view)

    def cog_unload(self) -> None:
        self._spoiler_view.stop()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{RADIO BUTTON}')

    @property
    def feedback_channel(self) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(182325885867786241)
        if guild is None:
            return None

        return guild.get_channel(263814407191134218)  # type: ignore

    @property
    def storage_channel(self) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(182325885867786241)
        if guild is None:
            return None

        return guild.get_channel(430229522340773899)  # type: ignore

    @commands.command(hidden=True)
    async def feelgood(self, ctx: Context):
        """press"""
        await ctx.send('*pressed*')

    @commands.command(hidden=True)
    async def feelbad(self, ctx: Context):
        """depress"""
        await ctx.send('*depressed*')

    @commands.command()
    async def love(self, ctx: Context):
        """What is love?"""
        responses = [
            'https://www.youtube.com/watch?v=HEXWRTEbj1I',
            'https://www.youtube.com/watch?v=i0p1bmr0EmE',
            'an intense feeling of deep affection',
            'something we don\'t have',
        ]

        response = random.choice(responses)
        await ctx.send(response)

    @commands.command(hidden=True)
    async def bored(self, ctx: Context):
        """boredom looms"""
        await ctx.send('http://i.imgur.com/BuTKSzf.png')

    def get_feedback_embed(
        self,
        obj: Context | discord.Interaction,
        *,
        summary: str,
        details: Optional[str] = None,
    ) -> discord.Embed:
        e = discord.Embed(title='Feedback', colour=0x738BD7)

        if details is not None:
            e.description = details
            e.title = summary[:256]
        else:
            e.description = summary

        if obj.guild is not None:
            e.add_field(name='Server', value=f'{obj.guild.name} (ID: {obj.guild.id})', inline=False)

        if obj.channel is not None:
            e.add_field(name='Channel', value=f'{obj.channel} (ID: {obj.channel.id})', inline=False)

        if isinstance(obj, discord.Interaction):
            e.timestamp = obj.created_at
            user = obj.user
        else:
            e.timestamp = obj.message.created_at
            user = obj.author

        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.set_footer(text=f'Author ID: {user.id}')
        return e

    @commands.command()
    @commands.cooldown(rate=1, per=60.0, type=commands.BucketType.user)
    async def feedback(self, ctx: Context, *, content: str):
        """Gives feedback about the bot.

        This is a quick way to request features or bug fixes
        without being in the bot's server.

        The bot will communicate with you via PM about the status
        of your request if possible.

        You can only request feedback once a minute.
        """

        channel = self.feedback_channel
        if channel is None:
            return

        e = self.get_feedback_embed(ctx, summary=content)
        await channel.send(embed=e)
        await ctx.send(f'{ctx.tick(True)} Successfully sent feedback')

    @app_commands.command(name='feedback')
    async def feedback_slash(self, interaction: discord.Interaction):
        """Give feedback about the bot directly to the owner."""

        await interaction.response.send_modal(FeedbackModal(self))

    @commands.command()
    @commands.is_owner()
    async def pm(self, ctx: Context, user_id: int, *, content: str):
        user = self.bot.get_user(user_id) or (await self.bot.fetch_user(user_id))

        fmt = (
            content + '\n\n*This is a DM sent because you had previously requested feedback or I found a bug'
            ' in a command you used, I do not monitor this DM. Responses to this DM are not mirrored anywhere.*'
        )
        try:
            await user.send(fmt)
        except:
            await ctx.send(f'Could not PM user by ID {user_id}.')
        else:
            await ctx.send('PM successfully sent.')

    async def redirect_post(self, ctx: Context, title, text):
        storage = self.storage_channel
        if storage is None:
            raise RuntimeError('Spoiler storage was not found')

        supported_attachments = ('.png', '.jpg', '.jpeg', '.webm', '.gif', '.mp4', '.txt')
        if not all(attach.filename.lower().endswith(supported_attachments) for attach in ctx.message.attachments):
            raise RuntimeError(f'Unsupported file in attachments. Only {", ".join(supported_attachments)} supported.')

        files = []
        total_bytes = 0
        max_mib = 25 * 1024 * 1024
        for attach in ctx.message.attachments:
            async with ctx.session.get(attach.url) as resp:
                if resp.status != 200:
                    continue

                content_length = int(resp.headers['Content-Length'])

                # file too big, skip it
                if (total_bytes + content_length) > max_mib:
                    continue

                total_bytes += content_length
                fp = io.BytesIO(await resp.read())
                files.append(discord.File(fp, filename=attach.filename))

            if total_bytes >= max_mib:
                break

        # on mobile, messages that are deleted immediately sometimes persist client side
        await asyncio.sleep(0.2)
        await ctx.message.delete()
        data = discord.Embed(title=title)
        if text:
            data.description = text

        data.set_author(name=ctx.author.id)
        data.set_footer(text=ctx.channel.id)

        try:
            message = await storage.send(embed=data, files=files)
        except discord.HTTPException as e:
            raise RuntimeError(f'Sorry. Could not store message due to {e.__class__.__name__}: {e}.') from e

        to_dict: SpoilerCacheData = {
            'author_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'attachments': message.attachments,
            'title': title,
            'text': text,
        }

        cache = SpoilerCache(to_dict)
        return message, cache

    async def get_spoiler_cache(self, channel_id: int, message_id: int) -> Optional[SpoilerCache]:
        try:
            return self._spoiler_cache[message_id]
        except KeyError:
            pass

        storage = self.storage_channel
        if storage is None:
            return None

        # slow path requires 2 lookups
        # first is looking up the message_id of the original post
        # to get the embed footer information which points to the storage message ID
        # the second is getting the storage message ID and extracting the information from it
        channel: Optional[discord.abc.Messageable] = self.bot.get_channel(channel_id)  # type: ignore
        if not channel:
            return None

        try:
            original_message = await channel.fetch_message(message_id)
            storage_message_id = int(original_message.embeds[0].footer.text)  # type: ignore  # Guarded by exception
            message = await storage.fetch_message(storage_message_id)
        except:
            # this message is probably not the proper format or the storage died
            return None

        data = message.embeds[0]
        to_dict: SpoilerCacheData = {
            'author_id': int(data.author.name),  # type: ignore
            'channel_id': int(data.footer.text),  # type: ignore
            'attachments': message.attachments,
            'title': data.title,
            'text': None if not data.description else data.description,
        }
        cache = SpoilerCache(to_dict)
        self._spoiler_cache[message_id] = cache
        return cache

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.emoji.id != SPOILER_EMOJI_ID:
            return

        if self._spoiler_cooldown.is_rate_limited(payload.message_id, payload.user_id):
            return

        user = self.bot.get_user(payload.user_id) or (await self.bot.fetch_user(payload.user_id))
        if not user or user.bot:
            return

        cache = await self.get_spoiler_cache(payload.channel_id, payload.message_id)

        if cache is not None:
            embed = cache.to_embed(self.bot)
            await user.send(embed=embed)

    @commands.command()
    @can_use_spoiler()
    async def spoiler(self, ctx: Context, title: str, *, text: Optional[str] = None):
        """Marks your post a spoiler with a title.

        Once your post is marked as a spoiler it will be
        automatically deleted and the bot will send a message
        to those who opt-in to view the spoiler.

        The only media types supported are png, gif, jpeg, mp4,
        and webm.

        Only 25MiB of total media can be uploaded at once.
        Sorry, Discord limitation.

        To opt-in to a post's spoiler you must press the button.
        """

        if len(title) > 100:
            return await ctx.send('Sorry. Title has to be shorter than 100 characters.')

        try:
            storage_message, cache = await self.redirect_post(ctx, title, text)
        except Exception as e:
            return await ctx.send(str(e))

        spoiler_message = await ctx.send(embed=cache.to_spoiler_embed(ctx, storage_message), view=self._spoiler_view)
        self._spoiler_cache[spoiler_message.id] = cache

    @commands.command(usage='<url>')
    @commands.cooldown(1, 5.0, commands.BucketType.member)
    async def vreddit(self, ctx: Context, *, reddit: RedditMediaURL):
        """Downloads a v.redd.it submission.

        Regular reddit URLs or v.redd.it URLs are supported.
        """

        filesize = ctx.guild.filesize_limit if ctx.guild else 8388608
        async with ctx.session.get(reddit.url) as resp:
            if resp.status != 200:
                return await ctx.send('Could not download video.')

            if int(resp.headers['Content-Length']) >= filesize:
                return await ctx.send('Video is too big to be uploaded.')

            data = await resp.read()
            await ctx.send(file=discord.File(io.BytesIO(data), filename=reddit.filename))

    @vreddit.error
    async def on_vreddit_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))

    @commands.command(name='convert')
    async def _convert(self, ctx: Context, *, values: Annotated[Set[Unit], UnitCollector] = None):
        """Converts between various units.

        Supported unit conversions:

        - km <-> mi
        - m <-> ft
        - cm <-> in
        - kg <-> lb
        - L <-> gal
        - °C <-> °F
        """

        if values is None:
            reply = ctx.replied_message
            if reply is None:
                return await ctx.send('You need to provide some values to convert or reply to a message with values.')

            values = await UnitCollector().convert(ctx, reply.content)

        pairs: list[tuple[str, str]] = []
        for value in values:
            original = f'{value.value:g}{value.display_unit}'
            converted = value.converted()
            pairs.append((original, f'{converted.value:g}{converted.display_unit}'))

        # Pad for width since this is monospace
        width = max(len(original) for original, _ in pairs)
        fmt = '\n'.join(f'{original:<{width}} -> {converted}' for original, converted in pairs)
        await ctx.send(f'```\n{fmt}\n```')

    @_convert.error
    async def on_convert_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))


async def setup(bot: RoboDanny):
    await bot.add_cog(Buttons(bot))

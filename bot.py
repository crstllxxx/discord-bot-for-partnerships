import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ads-bot")


@dataclass
class AdItem:
    """Валидируемая модель одного рекламного сообщения."""

    name: str
    invite_url: str
    description: str


@dataclass
class AdsFile:
    """Валидируемая модель файла с рекламой серверов."""

    ads: list[AdItem]


@dataclass
class BotConfig:
    """Валидируемая модель конфигурации канала и расписания."""

    channel_id: int
    timezone: str
    post_times: list[str]
    post_interval_days: int


@dataclass
class RuntimeState:
    """Состояние рантайма для ротации рекламных объявлений."""

    next_ad_index: int = 0


class AdsBot(discord.Client):
    """Discord-бот для публикации реклам серверов по расписанию."""

    def __init__(self, config_path: Path, ads_path: Path) -> None:
        """Инициализируем бота, командное дерево и планировщик задач."""

        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)

        self.tree = discord.app_commands.CommandTree(self)
        self.config_path = config_path
        self.ads_path = ads_path
        self.scheduler = AsyncIOScheduler()
        self.runtime_state = RuntimeState()
        self.config: BotConfig | None = None
        self.ads_data: AdsFile | None = None
        self.lock = asyncio.Lock()
        self._register_commands()

    def _load_json_file(self, path: Path) -> dict[str, Any]:
        """Читаем JSON-файл и возвращаем словарь с безопасной обработкой ошибок."""

        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Файл не найден: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Ошибка JSON в файле {path}: {exc}") from exc

    def _save_json_file(self, path: Path, data: dict[str, Any]) -> None:
        """Безопасно сохраняем JSON в файл через временный файл и замену."""

        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temp_path, path)
        except OSError as exc:
            raise OSError(f"Не удалось сохранить файл {path}: {exc}") from exc

    def _validate_non_empty_str(self, value: Any, field_name: str, max_len: int) -> str:
        """Проверяем, что значение является непустой строкой с допустимой длиной."""

        if not isinstance(value, str):
            raise ValueError(f"Поле '{field_name}' должно быть строкой.")

        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Поле '{field_name}' не должно быть пустым.")

        if len(normalized) > max_len:
            raise ValueError(f"Поле '{field_name}' не должно превышать {max_len} символов.")

        return normalized

    def _validate_invite_url(self, value: Any) -> str:
        """Проверяем, что ссылка-приглашение является корректным HTTP/HTTPS URL."""

        url = self._validate_non_empty_str(value, "invite_url", max_len=500)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Поле 'invite_url' должно содержать корректный HTTP/HTTPS URL.")

        return url

    def _validate_post_times(self, value: Any) -> list[str]:
        """Проверяем формат времени HH:MM, тип списка и отсутствие дублей."""

        if not isinstance(value, list) or not value:
            raise ValueError("Поле 'post_times' должно быть непустым списком времени HH:MM.")

        validated: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("Каждое значение в 'post_times' должно быть строкой формата HH:MM.")

            normalized = item.strip()
            try:
                datetime.strptime(normalized, "%H:%M")
            except ValueError as exc:
                raise ValueError(f"Некорректное время '{item}'. Используйте HH:MM.") from exc
            validated.append(normalized)

        if len(set(validated)) != len(validated):
            raise ValueError("В 'post_times' есть дублирующиеся значения.")

        return sorted(validated)

    def load_config(self) -> BotConfig:
        """Загружаем и валидируем конфигурацию расписания и канала."""

        raw = self._load_json_file(self.config_path)
        if not isinstance(raw, dict):
            raise ValueError("Некорректный config.json: корневой элемент должен быть объектом.")

        try:
            channel_id = raw.get("channel_id")
            if not isinstance(channel_id, int) or channel_id <= 0:
                raise ValueError("Поле 'channel_id' должно быть положительным числом.")

            timezone = self._validate_non_empty_str(raw.get("timezone"), "timezone", max_len=100)
            post_times = self._validate_post_times(raw.get("post_times"))
            post_interval_days = raw.get("post_interval_days", 1)
            if not isinstance(post_interval_days, int) or post_interval_days < 1 or post_interval_days > 31:
                raise ValueError("Поле 'post_interval_days' должно быть целым числом от 1 до 31.")
            ZoneInfo(timezone)

            config = BotConfig(
                channel_id=channel_id,
                timezone=timezone,
                post_times=post_times,
                post_interval_days=post_interval_days,
            )
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Неизвестный timezone: {raw.get('timezone')}") from exc

        return config

    def load_ads(self) -> AdsFile:
        """Загружаем и валидируем список рекламных объявлений."""

        raw = self._load_json_file(self.ads_path)
        if not isinstance(raw, dict):
            raise ValueError("Некорректный ads.json: корневой элемент должен быть объектом.")

        ads_raw = raw.get("ads")
        if not isinstance(ads_raw, list):
            raise ValueError("Поле 'ads' должно быть списком.")

        ads: list[AdItem] = []
        for idx, item in enumerate(ads_raw):
            if not isinstance(item, dict):
                raise ValueError(f"Элемент ads[{idx}] должен быть объектом.")

            name = self._validate_non_empty_str(item.get("name"), f"ads[{idx}].name", max_len=100)
            invite_url = self._validate_invite_url(item.get("invite_url"))
            description = self._validate_non_empty_str(
                item.get("description"),
                f"ads[{idx}].description",
                max_len=3500,
            )
            ads.append(AdItem(name=name, invite_url=invite_url, description=description))

        return AdsFile(ads=ads)

    def save_ads(self, ads: AdsFile) -> None:
        """Сохраняем объявления в ads.json в совместимом формате."""

        payload = {
            "ads": [
                {
                    "name": ad.name,
                    "invite_url": ad.invite_url,
                    "description": ad.description,
                }
                for ad in ads.ads
            ]
        }
        self._save_json_file(self.ads_path, payload)

    async def add_ad(self, name: str, invite_url: str, description: str) -> None:
        """Добавляем новое объявление в ads.json и обновляем данные в памяти."""

        async with self.lock:
            current_ads = self.load_ads()
            ad_item = AdItem(
                name=self._validate_non_empty_str(name, "name", max_len=100),
                invite_url=self._validate_invite_url(invite_url),
                description=self._validate_non_empty_str(description, "description", max_len=3500),
            )
            current_ads.ads.append(ad_item)
            self.save_ads(current_ads)
            self.ads_data = current_ads

    async def remove_ad(self, ad_number: int) -> AdItem:
        """Удаляем объявление по номеру (1-based) и возвращаем удалённую запись."""

        async with self.lock:
            current_ads = self.load_ads()
            if not current_ads.ads:
                raise ValueError("Список рекламы пуст, удалять нечего.")

            if ad_number < 1 or ad_number > len(current_ads.ads):
                raise ValueError(
                    f"Некорректный номер. Доступно объявлений: {len(current_ads.ads)}."
                )

            removed_ad = current_ads.ads.pop(ad_number - 1)
            self.save_ads(current_ads)
            self.ads_data = current_ads

            if current_ads.ads:
                self.runtime_state.next_ad_index %= len(current_ads.ads)
            else:
                self.runtime_state.next_ad_index = 0

            return removed_ad

    async def remove_ad_by_name(self, ad_name: str) -> AdItem:
        """Удаляем объявление по названию (без учёта регистра)."""

        normalized_name = self._validate_non_empty_str(ad_name, "ad_name", max_len=100)

        async with self.lock:
            current_ads = self.load_ads()
            if not current_ads.ads:
                raise ValueError("Список рекламы пуст, удалять нечего.")

            matches = [
                idx for idx, ad in enumerate(current_ads.ads) if ad.name.casefold() == normalized_name.casefold()
            ]
            if not matches:
                raise ValueError(f"Реклама с названием '{normalized_name}' не найдена.")

            if len(matches) > 1:
                raise ValueError(
                    "Найдено несколько реклам с таким названием. Используйте удаление по номеру."
                )

            removed_ad = current_ads.ads.pop(matches[0])
            self.save_ads(current_ads)
            self.ads_data = current_ads

            if current_ads.ads:
                self.runtime_state.next_ad_index %= len(current_ads.ads)
            else:
                self.runtime_state.next_ad_index = 0

            return removed_ad

    async def list_ads(self) -> list[AdItem]:
        """Возвращаем снимок текущего списка рекламных объявлений."""

        async with self.lock:
            current_ads = self.load_ads()
            return list(current_ads.ads)

    async def resolve_ad_selection(self, ad_number: int | None, ad_name: str | None) -> AdItem:
        """Возвращаем выбранное объявление по номеру или названию."""

        if (ad_number is None and ad_name is None) or (ad_number is not None and ad_name is not None):
            raise ValueError("Укажите либо ad_number, либо ad_name.")

        async with self.lock:
            current_ads = self.load_ads()

        if not current_ads.ads:
            raise ValueError("Список рекламы пуст.")

        if ad_number is not None:
            if ad_number < 1 or ad_number > len(current_ads.ads):
                raise ValueError(f"Некорректный номер. Доступно объявлений: {len(current_ads.ads)}.")
            return current_ads.ads[ad_number - 1]

        normalized_name = self._validate_non_empty_str(ad_name or "", "ad_name", max_len=100)
        for ad in current_ads.ads:
            if ad.name.casefold() == normalized_name.casefold():
                return ad

        raise ValueError(f"Реклама с названием '{normalized_name}' не найдена.")

    async def reload_data(self) -> None:
        """Перезагружаем конфигурацию, рекламу и пересобираем расписание."""

        async with self.lock:
            self.config = self.load_config()
            self.ads_data = self.load_ads()
            self._rebuild_jobs()
            logger.info("Конфигурация и реклама успешно перезагружены.")

    def _rebuild_jobs(self) -> None:
        """Обновляем задания планировщика в соответствии с post_times."""

        if self.config is None:
            return

        self.scheduler.remove_all_jobs()
        tz = ZoneInfo(self.config.timezone)
        day_expression = "*" if self.config.post_interval_days == 1 else f"*/{self.config.post_interval_days}"

        for idx, post_time in enumerate(self.config.post_times):
            hour, minute = post_time.split(":")
            self.scheduler.add_job(
                self.send_scheduled_ad,
                trigger="cron",
                day=day_expression,
                hour=int(hour),
                minute=int(minute),
                timezone=tz,
                id=f"ads_job_{idx}",
                replace_existing=True,
            )

        if not self.scheduler.running:
            self.scheduler.start()

        logger.info(
            "Расписание обновлено. Интервал дней: %s. Времена: %s",
            self.config.post_interval_days,
            ", ".join(self.config.post_times),
        )

    async def send_scheduled_ad(self) -> None:
        """Отправляем следующее рекламное сообщение в указанный канал."""

        async with self.lock:
            if self.config is None or self.ads_data is None:
                logger.warning("Попытка отправки без загруженных данных.")
                return

            if not self.ads_data.ads:
                logger.warning("Список ads пуст, отправка пропущена.")
                return

            channel = self.get_channel(self.config.channel_id)
            if not isinstance(channel, discord.TextChannel):
                logger.error("Канал с ID %s недоступен или не является текстовым.", self.config.channel_id)
                return

            ad = self.ads_data.ads[self.runtime_state.next_ad_index % len(self.ads_data.ads)]
            self.runtime_state.next_ad_index += 1

        await self._send_ad_to_channel(ad, channel)

    async def _send_ad_to_channel(self, ad: AdItem, channel: discord.TextChannel) -> None:
        """Отправляем выбранное рекламное объявление в конкретный канал."""

        if self.config is None:
            raise ValueError("Конфигурация не загружена.")

        embed = discord.Embed(
            title=f"Реклама сервера: {ad.name}",
            description=ad.description,
            color=discord.Color.blue(),
            timestamp=datetime.now(tz=ZoneInfo(self.config.timezone)),
        )
        embed.add_field(name="Ссылка-приглашение", value=str(ad.invite_url), inline=False)

        try:
            await channel.send(embed=embed)
            logger.info("Отправлена реклама сервера: %s", ad.name)
        except discord.DiscordException as exc:
            logger.exception("Ошибка отправки рекламного сообщения: %s", exc)
            raise RuntimeError(f"Не удалось отправить рекламу: {exc}") from exc

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        """Проверяем, что команду выполняет администратор сервера."""

        if not isinstance(interaction.user, discord.Member):
            return False

        return interaction.user.guild_permissions.administrator

    def _register_commands(self) -> None:
        """Регистрируем slash-команды для управления ботом."""

        @self.tree.command(name="ads_reload", description="Перезагрузить config.json и ads.json")
        async def ads_reload(interaction: discord.Interaction) -> None:
            """Команда для перезагрузки файлов без перезапуска бота."""

            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Недостаточно прав. Нужны права администратора.",
                    ephemeral=True,
                )
                return

            try:
                await self.reload_data()
                await interaction.response.send_message("Данные успешно перезагружены.", ephemeral=True)
            except Exception as exc:
                logger.exception("Ошибка в /ads_reload: %s", exc)
                await interaction.response.send_message(
                    f"Ошибка перезагрузки: {exc}",
                    ephemeral=True,
                )

        @self.tree.command(name="ads_send_now", description="Отправить рекламу сейчас")
        async def ads_send_now(interaction: discord.Interaction) -> None:
            """Команда для ручной отправки рекламы в текущий момент."""

            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Недостаточно прав. Нужны права администратора.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await self.send_scheduled_ad()
                await interaction.followup.send("Рекламное сообщение отправлено.", ephemeral=True)
            except Exception as exc:
                logger.exception("Ошибка в /ads_send_now: %s", exc)
                await interaction.followup.send(f"Ошибка отправки: {exc}", ephemeral=True)

        @self.tree.command(name="ads_send_specific", description="Отправить выбранную рекламу")
        @discord.app_commands.describe(
            ad_number="Номер объявления из /ads_list (начиная с 1)",
            ad_name="Название объявления для отправки",
            target_channel="Канал отправки (если не указан — канал из config.json)",
        )
        async def ads_send_specific(
            interaction: discord.Interaction,
            ad_number: int | None = None,
            ad_name: str | None = None,
            target_channel: discord.TextChannel | None = None,
        ) -> None:
            """Команда для отправки конкретной рекламы по номеру или названию."""

            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Недостаточно прав. Нужны права администратора.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                selected_ad = await self.resolve_ad_selection(ad_number, ad_name)

                channel = target_channel
                if channel is None:
                    if self.config is None:
                        await interaction.followup.send("Конфигурация не загружена.", ephemeral=True)
                        return
                    resolved_channel = self.get_channel(self.config.channel_id)
                    if not isinstance(resolved_channel, discord.TextChannel):
                        await interaction.followup.send(
                            "Канал из config.json недоступен или не является текстовым.",
                            ephemeral=True,
                        )
                        return
                    channel = resolved_channel

                await self._send_ad_to_channel(selected_ad, channel)
                await interaction.followup.send(
                    f"Реклама '{selected_ad.name}' отправлена в {channel.mention}.",
                    ephemeral=True,
                )
            except Exception as exc:
                logger.exception("Ошибка в /ads_send_specific: %s", exc)
                await interaction.followup.send(f"Ошибка отправки: {exc}", ephemeral=True)

        @self.tree.command(name="ads_preview", description="Предпросмотр выбранной рекламы")
        @discord.app_commands.describe(
            ad_number="Номер объявления из /ads_list (начиная с 1)",
            ad_name="Название объявления для предпросмотра",
        )
        async def ads_preview(
            interaction: discord.Interaction,
            ad_number: int | None = None,
            ad_name: str | None = None,
        ) -> None:
            """Показываем предпросмотр конкретной рекламы без отправки в канал."""

            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Недостаточно прав. Нужны права администратора.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                selected_ad = await self.resolve_ad_selection(ad_number, ad_name)

                preview_embed = discord.Embed(
                    title=f"Предпросмотр рекламы: {selected_ad.name}",
                    description=selected_ad.description,
                    color=discord.Color.gold(),
                )
                preview_embed.add_field(
                    name="Ссылка-приглашение",
                    value=str(selected_ad.invite_url),
                    inline=False,
                )
                await interaction.followup.send(embed=preview_embed, ephemeral=True)
            except Exception as exc:
                logger.exception("Ошибка в /ads_preview: %s", exc)
                await interaction.followup.send(f"Ошибка предпросмотра: {exc}", ephemeral=True)

        @ads_send_specific.autocomplete("ad_name")
        async def ads_send_specific_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[discord.app_commands.Choice[str]]:
            """Автодополнение названия рекламы для команды отдельной отправки."""

            try:
                ads = await self.list_ads()
            except Exception:
                return []

            current_cf = current.casefold().strip()
            names = []
            for ad in ads:
                if current_cf and current_cf not in ad.name.casefold():
                    continue
                if ad.name not in names:
                    names.append(ad.name)

            return [discord.app_commands.Choice(name=name[:100], value=name) for name in names[:25]]

        @self.tree.command(name="ads_add", description="Добавить новую рекламу в ads.json")
        @discord.app_commands.describe(
            name="Название сервера",
            invite_url="Ссылка-приглашение (https://...)",
            description="Текст рекламы",
        )
        async def ads_add(
            interaction: discord.Interaction,
            name: str,
            invite_url: str,
            description: str,
        ) -> None:
            """Команда для добавления рекламного объявления через Discord-клиент."""

            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Недостаточно прав. Нужны права администратора.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await self.add_ad(name=name, invite_url=invite_url, description=description)
                await interaction.followup.send("Реклама добавлена в ads.json.", ephemeral=True)
            except Exception as exc:
                logger.exception("Ошибка в /ads_add: %s", exc)
                await interaction.followup.send(f"Ошибка добавления: {exc}", ephemeral=True)

        @self.tree.command(name="ads_list", description="Показать список реклам с номерами")
        async def ads_list(interaction: discord.Interaction) -> None:
            """Команда для просмотра списка рекламных объявлений."""

            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Недостаточно прав. Нужны права администратора.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                ads = await self.list_ads()
                if not ads:
                    await interaction.followup.send("Список рекламы пуст.", ephemeral=True)
                    return

                lines = [f"{idx}. {ad.name}" for idx, ad in enumerate(ads, start=1)]
                body = "\n".join(lines)
                if len(body) > 1700:
                    body = body[:1700] + "\n..."

                await interaction.followup.send(
                    f"Текущие объявления:\n```\n{body}\n```",
                    ephemeral=True,
                )
            except Exception as exc:
                logger.exception("Ошибка в /ads_list: %s", exc)
                await interaction.followup.send(f"Ошибка получения списка: {exc}", ephemeral=True)

        @self.tree.command(name="ads_remove", description="Удалить рекламу по номеру или названию")
        @discord.app_commands.describe(
            ad_number="Номер объявления из /ads_list (начиная с 1)",
            ad_name="Название объявления для удаления",
        )
        async def ads_remove(
            interaction: discord.Interaction,
            ad_number: int | None = None,
            ad_name: str | None = None,
        ) -> None:
            """Команда для удаления рекламы по номеру или названию."""

            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Недостаточно прав. Нужны права администратора.",
                    ephemeral=True,
                )
                return

            if (ad_number is None and ad_name is None) or (ad_number is not None and ad_name is not None):
                await interaction.response.send_message(
                    "Укажите либо ad_number, либо ad_name.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                if ad_number is not None:
                    removed_ad = await self.remove_ad(ad_number)
                else:
                    removed_ad = await self.remove_ad_by_name(ad_name or "")

                await interaction.followup.send(
                    f"Удалена реклама: {removed_ad.name}",
                    ephemeral=True,
                )
            except Exception as exc:
                logger.exception("Ошибка в /ads_remove: %s", exc)
                await interaction.followup.send(f"Ошибка удаления: {exc}", ephemeral=True)

        @ads_remove.autocomplete("ad_name")
        async def ads_remove_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[discord.app_commands.Choice[str]]:
            """Автодополнение названия рекламы для команды удаления."""

            try:
                ads = await self.list_ads()
            except Exception:
                return []

            current_cf = current.casefold().strip()
            names = []
            for ad in ads:
                if current_cf and current_cf not in ad.name.casefold():
                    continue
                if ad.name not in names:
                    names.append(ad.name)

            return [discord.app_commands.Choice(name=name[:100], value=name) for name in names[:25]]

    async def on_ready(self) -> None:
        """Событие готовности клиента: загружаем данные и синхронизируем команды."""

        logger.info("Бот запущен как %s", self.user)

        try:
            await self.reload_data()
            await self.tree.sync()
            logger.info("Slash-команды синхронизированы.")
        except Exception as exc:
            logger.exception("Ошибка инициализации в on_ready: %s", exc)


async def main() -> None:
    """Точка входа приложения: загружаем токен и запускаем бота."""

    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Переменная DISCORD_TOKEN не задана в .env")

    base_dir = Path(__file__).resolve().parent
    config_path = base_dir / "config.json"
    ads_path = base_dir / "ads.json"

    bot = AdsBot(config_path=config_path, ads_path=ads_path)
    try:
        await bot.start(token)
    finally:
        if bot.scheduler.running:
            bot.scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем.")
